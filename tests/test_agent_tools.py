"""The dashboard tools driven by a scripted model: artifacts, approvals and the user's rights."""

import asyncio
import os
import re

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


def added(index):
    """Args for a later call: the name of the node the *index*-th call added, with a port."""

    def name(results, port):
        return re.search(r"'([^']+)'", results[index]).group(1) + f".{port}"

    return name


def scripted(*calls):
    """A model that makes *calls*, one per step, then reports the last tool result.

    A call's args may be a callable taking the contents of the earlier tool
    results, for args that depend on them, such as node names.
    """

    def model(messages, info):
        results = _results(messages)
        if len(results) < len(calls):
            name, args = calls[len(results)]
            if callable(args):
                args = args([str(r.content) for r in results])
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
        (
            "connect",
            lambda r: {"source": added(0)(r, "value"), "target": added(1)(r, "value")},
        ),
    )
    output = await runner.run("Double a number")
    source, target = (f"{i['instance_id'].split('__')[1]}" for i in ops.editor._tile_items)
    assert output == f"Done: Connected {source}.value to {target}.value."
    (artifact,) = runner.state.artifacts
    assert artifact.kind == ARTIFACT_KIND and artifact.title == "Mine"
    assert len(artifact.versions) == 3 and artifact.applied_version.version == 3
    changes = diff(artifact.versions[1].value, artifact.versions[2].value, kind=ARTIFACT_KIND)
    assert [c.label for c in changes.changes] == [f"Connected {source}.value → {target}.value"]
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
    ops.replace_yaml(artifact.versions[0].value, revision=ops.revision())
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
        ops.share_dashboard(theirs.dashboard_id, add_viewers=["mallory"])
    ops.share_dashboard(
        mine.dashboard_id, add_viewers=["carol"], add_editors=["dave"], everyone=False
    )
    permission = store._load_any(mine.dashboard_id).permission
    assert permission.allow_users == {"carol"} and permission.allow_edit_users == {"dave"}
    ops.delete_dashboard(mine.dashboard_id)
    assert store._load_any(mine.dashboard_id) is None
    assert store._load_any(theirs.dashboard_id) is not None


class TestSharing:
    """Sharing through the agent keeps a dashboard private unless asked to open it."""

    @pytest.fixture
    async def shared(self, store, monkeypatch):
        mine = store.create_dashboard("alice", "Mine")
        return LiveDashboardOps(_app(store, monkeypatch)), mine.dashboard_id

    def _can_view(self, store, did, user):
        from panel_flowdash.auth import is_authorized

        model = store._load_any(did)
        identity = Identity(user=user, oauth_user=user)
        return is_authorized(model.permission, identity, default_allow=True, owner=model.owner)

    async def test_adding_an_editor_keeps_the_dashboard_private(self, shared, store):
        ops, did = shared
        ops.share_dashboard(did, everyone=False)
        access = ops.share_dashboard(did, add_editors=["bob"])
        assert access["everyone"] is False and access["editors"] == ["bob"]
        assert self._can_view(store, did, "bob")
        assert not self._can_view(store, did, "mallory")
        ops.share_dashboard(did)
        assert not self._can_view(store, did, "mallory")

    async def test_sharing_adds_to_the_existing_access(self, shared, store):
        ops, did = shared
        ops.share_dashboard(did, add_viewers=["carol"], everyone=False)
        access = ops.share_dashboard(did, add_viewers=["group:ops"], remove=["nobody"])
        assert access["viewers"] == ["carol", "group:ops"]
        assert ops.share_dashboard(did, remove=["carol"])["viewers"] == ["group:ops"]

    async def test_a_public_dashboard_says_so(self, shared, store):
        ops, did = shared
        assert ops.share_dashboard(did, add_editors=["bob"])["everyone"] is True

    async def test_only_everyone_true_opens_it(self, shared, store):
        ops, did = shared
        ops.share_dashboard(did, everyone=False)
        assert not self._can_view(store, did, "mallory")
        assert ops.share_dashboard(did, everyone=True)["everyone"] is True
        assert self._can_view(store, did, "mallory")


def _live_runner(app, *calls, policy=None):
    agent = Agent(scripted(*calls), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    ops = LiveDashboardOps(app)
    return (
        AgentRunner(
            agent,
            RunState(flush_interval=0),
            deps=FlowDashDeps(ops),
            approval_policy=policy or approval_policy(),
        ),
        ops,
    )


@pytest.mark.parametrize("approved", [True, False])
async def test_deleting_and_sharing_wait_for_approval_and_show_the_title(
    store, monkeypatch, approved
):
    mine = store.create_dashboard("alice", "Quarterly")
    runner, _ = _live_runner(
        _app(store, monkeypatch),
        (
            "share_dashboard",
            {"dashboard_id": mine.dashboard_id, "title": "Quarterly", "everyone": True},
        ),
        ("delete_dashboard", {"dashboard_id": mine.dashboard_id, "title": "Quarterly"}),
    )
    _resolve(runner.state, approved)
    await runner.run("Share and delete")
    requests = list(runner.state.approvals.values())
    assert [r.tool_name for r in requests] == ["share_dashboard", "delete_dashboard"]
    assert all(r.args["title"] == "Quarterly" for r in requests)
    stored = store._load_any(mine.dashboard_id)
    assert (stored is None) is approved
    if not approved:
        assert stored.permission == mine.permission


async def test_a_wrong_title_is_sent_back(store, monkeypatch):
    mine = store.create_dashboard("alice", "Quarterly")
    runner, _ = _live_runner(
        _app(store, monkeypatch),
        ("delete_dashboard", {"dashboard_id": mine.dashboard_id, "title": "Scratch"}),
    )
    _resolve(runner.state, True)
    output = await runner.run("Delete Scratch")
    assert "is titled 'Quarterly', not 'Scratch'" in output
    assert store._load_any(mine.dashboard_id) is not None


async def test_the_save_approval_lists_what_is_stored(store, monkeypatch):
    mine = store.create_dashboard("alice", "Mine")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(mine.dashboard_id)
    runner, _ = _live_runner(
        app,
        ("add_node", {"component": "T/number"}),
        ("set_settings", {"settings": {"viewers_can_inspect": True}}),
        ("save_dashboard", {}),
    )
    _resolve(runner.state, False)
    await runner.run("Build and save")
    (request,) = runner.state.approvals.values()
    assert "It stores:" in request.description and "Added number_" in request.description
    assert "viewers_can_inspect" in request.description
    assert "changes who can see the dashboard's data" in request.description


async def test_sensitive_components_need_approval(store, monkeypatch):
    mine = store.create_dashboard("alice", "Mine")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(mine.dashboard_id)
    runner, ops = _live_runner(app, ("add_node", {"component": "T/scaled"}))
    runner.approval_policy = approval_policy(sensitive_components=["T/scaled"], ops=ops)
    _resolve(runner.state, False)
    await runner.run("Add")
    assert [r.tool_name for r in runner.state.approvals.values()] == ["add_node"]
    assert app._editor._tile_items == []
    ops.add_node("T/scaled")
    name = next(iter(ops._names().values()))
    assert runner.approval_policy.requires_approval("set_config", {"name": name})
    assert runner.approval_policy.requires_approval(
        "replace_dashboard",
        {"dashboard_yaml": "flowdash: 1\nnodes:\n  x: {component: T/scaled}\n"},
    )


async def test_a_run_stops_when_the_user_opens_another_dashboard(store, monkeypatch):
    first = store.create_dashboard("alice", "First")
    second = store.create_dashboard("alice", "Second")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(first.dashboard_id)

    def switch(results):
        app._load_dashboard_sync(second.dashboard_id)
        return {"component": "T/number"}

    runner, _ = _live_runner(app, ("get_dashboard", {}), ("add_node", switch))
    output = await runner.run("Add a number")
    assert "switched to another dashboard" in output
    assert app._editor._tile_items == []


async def test_replacing_needs_the_revision_line(ops):
    runner = _runner(ops, ("replace_dashboard", {"dashboard_yaml": "flowdash: 1\n"}))
    assert "keep its '# revision:' line" in await runner.run("Clear")


def test_dashboard_versions_render_as_yaml_code():
    import panel as pn
    from panel_agents.components import render

    view = render("flowdash: 1\n# revision: abc\n", kind=ARTIFACT_KIND)
    assert isinstance(view, pn.widgets.CodeEditor) and view.language == "yaml"
