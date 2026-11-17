"""The dashboard tools driven by a scripted model: artifacts, approvals and the user's rights."""

import asyncio
import os

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from panel_agents import RunState, events as ev
from panel_agents.adapters.pydantic_ai import AgentRunner
from panel_agents.artifacts import diff
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.agent.live import LiveDashboardOps
from panel_flowdash.agent.tools import (
    ARTIFACT_KIND,
    FlowDashDeps,
    approval_policy,
    dashboard_toolset,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.component_library import normalize_components

from .test_agent_ops import COMPONENTS, _editor

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def _results(messages):
    return [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, (ToolReturnPart, RetryPromptPart))
    ]


def scripted(*calls):
    """A model that makes *calls*, one per step, then reports the last tool result."""

    def model(messages, info):
        results = _results(messages)
        if len(results) < len(calls):
            name, args = calls[len(results)]
            return ModelResponse(
                parts=[ToolCallPart(name, args, tool_call_id=f"call-{len(results) + 1}")]
            )
        last = results[-1]
        content = last.content if isinstance(last, ToolReturnPart) else last.model_response()
        return ModelResponse(parts=[TextPart(f"Done: {content}")])

    async def stream(messages, info):
        for i, part in enumerate(model(messages, info).parts):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {
                    i: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }

    return FunctionModel(model, stream_function=stream)


def _runner(ops, *calls):
    agent = Agent(scripted(*calls), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    return AgentRunner(
        agent,
        RunState(flush_interval=0),
        deps=FlowDashDeps(ops),
        approval_policy=approval_policy(),
    )


def _resolve(state, approved):
    def callback(event):
        if isinstance(event, ev.ApprovalRequested):
            asyncio.get_running_loop().call_soon(
                lambda: state.resolve_approval(event.tool_call_id, approved=approved)
            )

    state.subscribe(callback)


@pytest.fixture
def store():
    return MemoryDashboardStore()


@pytest.fixture
def ops(store):
    editor = _editor(store)
    editor.new_dashboard("Mine")
    yield DashboardOps(editor)
    editor.dispose()


async def test_each_edit_publishes_an_applied_version_with_a_semantic_diff(ops):
    runner = _runner(
        ops,
        ("add_node", {"component": "T/number"}),
        ("add_node", {"component": "T/doubler"}),
        ("connect", {"source": "number.value", "target": "doubler.value"}),
    )
    assert await runner.run("Double a number") == "Done: Connected number.value to doubler.value."
    (artifact,) = runner.state.artifacts
    assert artifact.kind == ARTIFACT_KIND and artifact.title == "Mine"
    assert len(artifact.versions) == 3 and artifact.applied == 2
    changes = diff(artifact.versions[1].value, artifact.versions[2].value, kind=ARTIFACT_KIND)
    assert [c.label for c in changes.changes] == ["Connected number.value → doubler.value"]
    assert ops.editor.dirty and ops.editor.graph.edges


async def test_mistakes_are_sent_back_for_the_model_to_fix(ops):
    runner = _runner(ops, ("add_node", {"component": "T/numbers"}))
    output = await runner.run("Add a number")
    assert "Unknown component 'T/numbers'. Did you mean 'T/number'" in output
    assert ops.editor._tile_items == []


@pytest.mark.parametrize("approved", [True, False])
async def test_saving_waits_for_the_users_approval(ops, store, approved):
    runner = _runner(ops, ("add_node", {"component": "T/number"}), ("save_dashboard", {}))
    _resolve(runner.state, approved)
    output = await runner.run("Add a number and save")
    request = next(iter(runner.state.approvals.values()))
    assert request.tool_name == "save_dashboard"
    stored = store._load_any(ops.editor.dashboard.dashboard_id)
    assert [i.component_id for i in stored.items] == (["T/number"] if approved else [])
    assert ("Saved dashboard" in output) is approved


async def test_a_viewers_agent_is_refused(store):
    model = store.create_dashboard("bob", "Bob's")
    editor = _editor(store, access={model.dashboard_id: "view"})
    ops = DashboardOps(editor)
    ops.open_dashboard(model.dashboard_id)
    runner = _runner(ops, ("add_node", {"component": "T/number"}))
    assert "Refused: You may only view this dashboard" in await runner.run("Add a number")
    assert editor._tile_items == [] and not runner.state.artifacts


async def test_reverting_a_version_restores_the_dashboard(ops):
    runner = _runner(
        ops,
        ("add_node", {"component": "T/number"}),
        ("add_node", {"component": "T/scaled", "config": {"factor": 2}}),
    )
    await runner.run("Build")
    (artifact,) = runner.state.artifacts
    ops.replace_yaml(artifact.versions[0].value)
    assert [i["component_id"] for i in ops.editor._tile_items] == ["T/number"]


def _app(store, monkeypatch, user="alice"):
    identity = Identity(user=user, oauth_user=user)
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
    return FlowDashApp(
        registry=normalize_components(COMPONENTS),
        store=store,
        auth_config=AuthConfig(),
        notifications=False,
        include_builtin_components=False,
    )


async def test_live_edits_switch_the_app_to_edit_mode(store, monkeypatch):
    model = store.create_dashboard("alice", "Live")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(model.dashboard_id)
    assert not app._editor.editable
    ops = LiveDashboardOps(app)
    ops.add_node("T/number")
    assert app._editor.editable and app._has_unsaved_changes()
    assert ops.save() == model.dashboard_id
    assert [i.component_id for i in store._load_any(model.dashboard_id).items] == ["T/number"]


async def test_live_edits_need_an_open_dashboard(store, monkeypatch):
    ops = LiveDashboardOps(_app(store, monkeypatch))
    with pytest.raises(OperationError, match="No dashboard is open"):
        ops.add_node("T/number")


async def test_live_delete_and_share_need_administrator_rights(store, monkeypatch):
    theirs = store.create_dashboard("bob", "Bob's")
    mine = store.create_dashboard("alice", "Mine")
    ops = LiveDashboardOps(_app(store, monkeypatch))
    with pytest.raises(PermissionError):
        ops.delete_dashboard(theirs.dashboard_id)
    with pytest.raises(PermissionError):
        ops.share_dashboard(theirs.dashboard_id, view_users=["mallory"])
    ops.share_dashboard(mine.dashboard_id, view_users=["carol"], edit_users=["dave"])
    permission = store._load_any(mine.dashboard_id).permission
    assert permission.allow_users == {"carol"} and permission.allow_edit_users == {"dave"}
    ops.delete_dashboard(mine.dashboard_id)
    assert store._load_any(mine.dashboard_id) is None
    assert store._load_any(theirs.dashboard_id) is not None
