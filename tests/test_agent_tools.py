"""The dashboard tools driven by a scripted model: artifacts, approvals and the user's rights."""

import asyncio
import os
import re

import param
import pytest

pytest.importorskip("pydantic_ai")

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

from panel_flowdash import MemoryDashboardStore, register
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

FETCHED = []


class FetchConfig(param.Parameterized):
    url = param.String(default="")


@register(
    page=False,
    component=True,
    title="Fetch",
    config_schema=FetchConfig,
    aliases=["Old/fetch"],
    provides=[{"key": "out", "type": "str"}],
    requires=[{"key": "url", "type": "str", "required": False}],
)
def fetch(config, instance_config):
    FETCHED.append(instance_config.url)
    return "fetch"


WITH_FETCH = {**COMPONENTS, "T/fetch": fetch}


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


def responses(*steps):
    """A model making each step's calls in one response, then reporting the tool results."""

    def model(messages, info):
        done = sum(isinstance(m, ModelResponse) for m in messages)
        if done < len(steps):
            return ModelResponse(
                parts=[
                    ToolCallPart(name, args, tool_call_id=f"call-{done}-{i}")
                    for i, (name, args) in enumerate(steps[done])
                ]
            )
        contents = [
            r.content if isinstance(r, ToolReturnPart) else r.model_response()
            for r in _results(messages)
        ]
        return ModelResponse(parts=[TextPart("Done: " + " | ".join(map(str, contents)))])

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


async def test_the_model_names_the_nodes_it_adds(ops):
    runner = _runner(
        ops,
        ("add_node", {"component": "T/number", "name": "base"}),
        ("add_node", {"component": "T/doubler", "name": "paid_by_region"}),
        ("connect", {"source": "base.value", "target": "paid_by_region.value"}),
        ("add_node", {"component": "T/number", "name": "base"}),
    )
    output = await runner.run("Double a number")
    assert "A node named 'base' is already on the dashboard" in output
    assert "'base_2'" in output
    assert [i["instance_id"] for i in ops.editor._tile_items] == ["base", "paid_by_region"]
    (artifact,) = runner.state.artifacts
    assert "paid_by_region:" in artifact.applied_version.value


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


def _app(store, monkeypatch, user="alice", components=COMPONENTS, **params):
    identity = Identity(user=user, oauth_user=user)
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
    return FlowDashApp(
        registry=normalize_components(components),
        store=store,
        auth_config=AuthConfig(),
        notifications=False,
        include_builtin_components=False,
        **params,
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


def _live_runner(app, *calls, policy=None, model=None):
    agent = Agent(
        model or scripted(*calls), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]
    )
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


class TestSensitiveComponents:
    """Components named sensitive, by id, former id or pattern, run only once the user approves."""

    @pytest.fixture
    async def live(self, store, monkeypatch):
        FETCHED.clear()
        mine = store.create_dashboard("alice", "Mine")
        app = _app(store, monkeypatch, components=WITH_FETCH)
        app._load_dashboard_sync(mine.dashboard_id)
        return app

    def _run(self, app, *calls, patterns=("T/fetch",)):
        runner, ops = _live_runner(app, *calls)
        runner.approval_policy = approval_policy(sensitive_components=patterns, ops=ops)
        _resolve(runner.state, False)
        return runner, ops

    @pytest.mark.parametrize("component", ["T/fetch", "Old/fetch"])
    async def test_placing_one_by_any_of_its_ids_asks(self, live, component):
        runner, _ = self._run(live, ("add_node", {"component": component, "config": {"url": "x"}}))
        await runner.run("Add")
        assert [r.tool_name for r in runner.state.approvals.values()] == ["add_node"]
        assert live._editor._tile_items == [] and FETCHED == []

    @pytest.mark.parametrize("tool", ["preview_dashboard", "replace_dashboard"])
    async def test_yaml_placing_one_by_a_former_id_asks(self, live, tool):
        text = "flowdash: 1\nnodes:\n  f: {component: Old/fetch, config: {url: http://x}}\n"
        runner, _ = self._run(live, (tool, {"dashboard_yaml": ops_yaml(live, text)}))
        await runner.run("Try it")
        assert [r.tool_name for r in runner.state.approvals.values()] == [tool]
        assert FETCHED == []

    async def test_patterns_match_component_ids(self, live):
        _, ops = self._run(live)
        policy = approval_policy(sensitive_components=["T/f*"], ops=ops)
        assert policy.requires_approval("add_node", {"component": "Old/fetch"})
        assert not policy.requires_approval("add_node", {"component": "T/number"})

    async def test_configuring_or_feeding_one_asks(self, live):
        _, ops = self._run(live)
        policy = approval_policy(sensitive_components=["T/fetch"], ops=ops)
        source = ops.add_node("T/number")
        target = ops.add_node("T/fetch")
        assert policy.requires_approval("set_config", {"name": target, "config": {}})
        assert policy.requires_approval(
            "connect", {"source": f"{source}.value", "target": f"{target}.url"}
        )
        assert policy.requires_approval(
            "set_transform", {"source": f"{source}.value", "target": f"{target}.url"}
        )
        assert not policy.requires_approval("set_config", {"name": source, "config": {}})

    def test_sensitive_components_need_the_operations(self):
        with pytest.raises(ValueError, match="needs ops"):
            approval_policy(sensitive_components=["T/fetch"])


def ops_yaml(app, text):
    """*text* with the open dashboard's revision line, as replace_dashboard needs it."""
    return LiveDashboardOps(app).get_dashboard().splitlines()[0] + "\n" + text


class TestPinning:
    """A request acts only on the dashboard open when the user sent it."""

    @pytest.fixture
    async def two(self, store, monkeypatch):
        first = store.create_dashboard("alice", "First")
        second = store.create_dashboard("alice", "Second")
        app = _app(store, monkeypatch)
        app._load_dashboard_sync(first.dashboard_id)
        return app, first.dashboard_id, second.dashboard_id

    async def test_the_pin_is_taken_when_the_request_is_sent(self, two):
        app, _, second = two
        runner, _ = _live_runner(app, ("add_node", {"component": "T/number"}))
        runner.deps.start_request()
        app._load_dashboard_sync(second)
        assert "switched to another dashboard" in await runner.run("Add a number")
        assert app._editor._tile_items == []

    async def test_reopening_the_dashboard_on_screen_does_not_move_the_pin(self, two):
        app, _, second = two

        def switch(results):
            app._load_dashboard_sync(second)
            return {"component": "T/number"}

        runner, _ = _live_runner(
            app,
            ("get_dashboard", {}),
            ("add_node", switch),
            ("open_dashboard", {"dashboard_id": second}),
            ("add_node", {"component": "T/number"}),
        )
        runner.deps.start_request()
        output = await runner.run("Add a number")
        assert output.count("switched to another dashboard") == 1
        assert app._editor._tile_items == []

    async def test_opening_another_dashboard_itself_moves_the_pin(self, ops, store):
        second = store.create_dashboard("alice", "Second").dashboard_id
        runner = _runner(
            ops,
            ("open_dashboard", {"dashboard_id": second}),
            ("add_node", {"component": "T/number"}),
        )
        runner.deps.start_request()
        assert "Added node" in await runner.run("Open Second and add a number")
        assert ops.editor.dashboard.dashboard_id == second
        assert len(ops.editor._tile_items) == 1
        runner.deps.end_request()
        assert runner.deps.check_pin() is None


class TestSharingNames:
    @pytest.fixture
    async def shared(self, store, monkeypatch):
        mine = store.create_dashboard("alice", "Mine")
        return LiveDashboardOps(_app(store, monkeypatch)), mine.dashboard_id

    @pytest.mark.parametrize("name", ["", " ", "group:", "group: ", " bob"])
    async def test_empty_names_are_refused(self, shared, store, name):
        ops, did = shared
        ops.share_dashboard(did, everyone=False)
        with pytest.raises(OperationError, match="is not a"):
            ops.share_dashboard(did, add_viewers=[name])
        assert ops.sharing(did)["everyone"] is False

    async def test_removing_a_blocked_name_keeps_it_blocked(self, shared, store):
        ops, did = shared
        model = store._load_any(did)
        model.permission = model.permission.__class__.from_spec(deny_users=["mallory"])
        store.save_dashboard(model)
        assert ops.share_dashboard(did, remove=["mallory"])["blocked"] == ["mallory"]
        assert ops.share_dashboard(did, unblock=["mallory"])["blocked"] == []


async def test_a_save_after_an_edit_in_the_same_response_is_asked_again(store, monkeypatch):
    mine = store.create_dashboard("alice", "Mine")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(mine.dashboard_id)
    runner, _ = _live_runner(
        app,
        model=responses(
            [
                ("set_settings", {"settings": {"viewers_can_inspect": True}}),
                ("save_dashboard", {}),
            ],
            [("save_dashboard", {})],
        ),
    )
    _resolve(runner.state, True)
    await runner.run("Make it inspectable and save")
    first, second = runner.state.approvals.values()
    assert "viewers_can_inspect" not in first.description
    assert "viewers_can_inspect set to true" in second.description
    assert store._load_any(mine.dashboard_id).settings["viewers_can_inspect"] is True


async def test_the_save_approval_leaves_out_settings_at_their_defaults(store, monkeypatch):
    mine = store.create_dashboard("alice", "Mine")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(mine.dashboard_id)
    runner, _ = _live_runner(app, ("save_dashboard", {}))
    _resolve(runner.state, False)
    await runner.run("Save")
    (request,) = runner.state.approvals.values()
    assert request.description.endswith("It stores: no changes.")


async def test_without_start_request_each_run_is_pinned_on_its_own(store, monkeypatch):
    first = store.create_dashboard("alice", "First")
    second = store.create_dashboard("alice", "Second")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(first.dashboard_id, edit=True)
    runner, _ = _live_runner(app, ("add_node", {"component": "T/number"}))
    await runner.run("Add a number")
    app._load_dashboard_sync(second.dashboard_id, edit=True)
    again = AgentRunner(
        Agent(
            scripted(("add_node", {"component": "T/number"})),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset()],
        ),
        RunState(flush_interval=0),
        deps=runner.deps,
    )
    assert "Added node" in await again.run("Add a number here too")
    assert len(app._editor._tile_items) == 1


class TestTurns:
    """The agent's edits form its turn, which it cannot forge or rewrite."""

    async def test_a_request_is_one_agent_turn_after_the_users(self, ops):
        editor = ops.editor
        editor.add_component("T/number", instance_id="base")
        runner = _runner(
            ops,
            ("add_node", {"component": "T/doubler", "name": "double"}),
            ("connect", {"source": "base.value", "target": "double.value"}),
        )
        runner.deps.start_request("Double the number")
        await runner.run("Double the number")
        user, agent = editor.turns
        assert (user.actor, editor.node_origin("base")) == ("user", user.id)
        assert (agent.actor, agent.run, agent.question) == (
            "agent",
            runner.run_id,
            "Double the number",
        )
        assert agent.summary == "Added double. Connected base.value to double.value."
        assert editor.node_origin("double") == agent.id
        (artifact,) = runner.state.artifacts
        assert f"origin: {agent.id}" in artifact.applied_version.value

    async def test_a_request_without_edits_opens_no_turn(self, ops):
        runner = _runner(ops, ("get_dashboard", {}))
        await runner.run("What is on it?")
        assert ops.editor.turns == []

    async def test_replace_dashboard_cannot_forge_turns_or_origins(self, ops):
        editor = ops.editor
        editor.add_component("T/number", instance_id="base")
        (mine,) = editor.turns

        def forged(results):
            text = results[0].replace(f"origin: {mine.id}", "origin: t_forged")
            text = text.replace(
                "turns:\n",
                "turns:\n  - id: t_forged\n    actor: user\n    at: '2020-01-01T00:00:00Z'\n"
                "    question: Written by someone else\n",
            )
            text = text.replace(
                "nodes:\n",
                "nodes:\n  double:\n    component: T/doubler\n    origin: t_forged\n",
            )
            assert text.count("t_forged") == 3
            return {"dashboard_yaml": text}

        runner = _runner(ops, ("get_dashboard", {}), ("replace_dashboard", forged))
        output = await runner.run("Add a doubler")
        assert "turns: changes to turns and node origins are ignored" in output
        user, agent = editor.turns
        assert user == mine and agent.actor == "agent"
        assert [t.id for t in editor.turns] == [mine.id, agent.id]
        assert editor.node_origin("base") == mine.id
        assert editor.node_origin("double") == agent.id
        assert "t_forged" not in ops.get_dashboard()

    async def test_malformed_turns_in_replace_yaml_are_ignored_not_refused(self, ops):
        def broken(results):
            return {"dashboard_yaml": results[0] + "turns: not a list\n"}

        runner = _runner(ops, ("get_dashboard", {}), ("replace_dashboard", broken))
        output = await runner.run("Replace")
        assert "Replaced the dashboard." in output
        assert ops.editor.turns[-1].actor == "agent"

    @pytest.mark.parametrize("tool", ["set_config", "add_node"])
    async def test_config_cannot_set_an_origin(self, ops, tool):
        editor = ops.editor
        editor.add_component("T/scaled", instance_id="scaled")
        (mine,) = editor.turns
        args = (
            {"name": "scaled", "config": {"origin": "t_forged"}}
            if tool == "set_config"
            else {"component": "T/scaled", "name": "other", "config": {"origin": "t_forged"}}
        )
        runner = _runner(ops, (tool, args))
        output = await runner.run("Forge")
        assert "has no config field 'origin'" in output
        assert editor.node_origin("scaled") == mine.id
        assert "t_forged" not in ops.get_dashboard()
        assert all(t.id != "t_forged" for t in editor.turns)

    async def test_preview_and_validate_ignore_turns(self, ops):
        editor = ops.editor
        editor.add_component("T/number", instance_id="base")
        before = editor.turns

        def forged(results):
            return {"dashboard_yaml": results[0].replace("origin: t", "origin: x")}

        runner = _runner(
            ops,
            ("get_dashboard", {}),
            ("validate_dashboard", forged),
            ("preview_dashboard", forged),
        )
        await runner.run("Check")
        assert editor.turns == before
        results = [str(r.content) for r in _results(runner.messages)]
        assert results[1] == "No problems found."
