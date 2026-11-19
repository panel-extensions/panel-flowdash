"""The dashboard tools driven by a scripted model: artifacts, approvals and the user's rights."""

import asyncio
import os
import re

import param
import pytest
import yaml

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


@register(page=False, component=True, title="Text", provides=[{"key": "text", "type": "str"}])
def text(config):
    config.text = "http://y"
    return "text"


class QueryConfig(param.Parameterized):
    sql = param.String(default="")
    limit = param.Integer(default=10)


@register(
    page=False,
    component=True,
    title="Query",
    config_schema=QueryConfig,
    aliases=["Old/query"],
    port_aliases={"query": "sql", "source": "table"},
    provides=[{"key": "rows", "type": "str"}],
    requires=[{"key": "table", "type": "str", "required": False}],
)
def query(config, instance_config):
    return "query"


class SourceConfig(param.Parameterized):
    value = param.String(default="FROM_A")


@register(
    page=False,
    component=True,
    title="Source A",
    config_schema=SourceConfig,
    provides=[{"key": "text", "type": "str"}],
)
def source_a(config, instance_config):
    config.text = instance_config.value
    return "a"


@register(page=False, component=True, title="Source B", provides=[{"key": "text", "type": "str"}])
def source_b(config):
    config.text = "FROM_B"
    return "b"


class SinkConfig(param.Parameterized):
    n = param.Number(default=0)
    options = param.Dict(default={})


@register(
    page=False,
    component=True,
    title="Sink",
    config_schema=SinkConfig,
    requires=[
        {"key": "table", "type": "str", "required": False},
        {"key": "tables", "type": "str", "multiple": True, "required": False},
    ],
)
def sink(config, instance_config):
    return "sink"


WITH_FETCH = {
    **COMPONENTS,
    "T/fetch": fetch,
    "T/text": text,
    "T/query": query,
    "P/srca": source_a,
    "P/srcb": source_b,
    "P/sink": sink,
}


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
    # The editor's edit history publishes a version per edit into its state.
    return AgentRunner(
        agent,
        ops.editor.edit_history.state,
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


async def test_a_request_publishes_one_applied_version_with_a_semantic_diff(ops):
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
    # The dashboard as opened, then one version for the whole request.
    assert len(artifact.versions) == 2 and artifact.applied_version.version == 2
    request = artifact.versions[1]
    assert (request.actor, request.run_id) == ("agent", runner.run_id)
    assert request.metadata["tool_call_ids"] == ["call-1", "call-2", "call-3"]
    changes = diff(artifact.versions[0].value, request.value, kind=ARTIFACT_KIND)
    assert f"Connected {source}.value → {target}.value" in [c.label for c in changes.changes]
    assert ops.editor.dirty and ops.editor.graph.edges


async def test_list_components_describes_several_components_in_one_call(ops):
    runner = _runner(ops, ("list_components", {"components": ["T/number", "T/doubler", "T/nope"]}))
    output = await runner.run("What do these take?")
    assert '"component": "T/number"' in output and '"component": "T/doubler"' in output
    assert '"component": "T/scaled"' not in output
    assert output.endswith("No component T/nope you may place.")


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
    ops.editor.add_component("T/doubler")
    ops.editor.edit_history.flush()
    (artifact,) = runner.state.artifacts
    ops.replace_yaml(artifact.versions[1].value, revision=ops.revision())
    assert [i["component_id"] for i in ops.editor._tile_items] == ["T/number", "T/scaled"]


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

    @pytest.fixture
    async def wired(self, live):
        """A text node feeding a query node through a transform, and a number node."""
        _, ops = self._run(live)
        ops.add_node("T/text", name="t")
        ops.add_node("T/query", {"sql": "SELECT 1", "limit": 5}, name="f")
        ops.add_node("T/query", name="g")
        ops.add_node("T/number", name="n")
        ops.connect("t.text", "f.table", [{"op": "format", "prefix": "a"}])
        ops.connect("f.rows", "g.table")
        policy = approval_policy(sensitive_components=["T/query"], ops=ops)
        return ops, policy, yaml.safe_load(ops.get_dashboard())

    @staticmethod
    def _asks(policy, doc, tool="replace_dashboard"):
        text = doc if isinstance(doc, str) else yaml.safe_dump(doc, sort_keys=False)
        return policy.requires_approval(tool, {"dashboard_yaml": text})

    def _edited(self, doc, edit):
        doc = yaml.safe_load(yaml.safe_dump(doc))
        edit(doc)
        return doc

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda d: None, id="unchanged"),
            pytest.param(lambda d: d["nodes"]["n"].update(position=[5, 5]), id="move"),
            pytest.param(lambda d: d["nodes"].update(m={"component": "T/number"}), id="add-other"),
            pytest.param(lambda d: d["nodes"].pop("n"), id="remove-other"),
            pytest.param(lambda d: d["nodes"]["t"].update(position=[1, 1]), id="move-source"),
            pytest.param(lambda d: d["settings"].update(description="x"), id="settings"),
            pytest.param(lambda d: d["nodes"]["f"].update(component="Old/query"), id="former-id"),
            pytest.param(
                lambda d: d["nodes"]["g"].update(config={"sql": "", "limit": 10}),
                id="default-written-out",
            ),
            pytest.param(
                lambda d: d["edges"].append({"from": "f.rows", "to": "n.value"}),
                id="outgoing-edge",
            ),
            pytest.param(lambda d: d["edges"].reverse(), id="edge-order"),
        ],
    )
    @pytest.mark.parametrize("tool", ["preview_dashboard", "replace_dashboard"])
    async def test_yaml_leaving_sensitive_nodes_alone_does_not_ask(self, wired, edit, tool):
        _, policy, doc = wired
        assert not self._asks(policy, self._edited(doc, edit), tool)

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(
                lambda d: d["nodes"].update(h={"component": "T/query"}), id="add-sensitive"
            ),
            pytest.param(lambda d: d["nodes"].pop("g"), id="remove-sensitive"),
            pytest.param(lambda d: d["nodes"].update(h=d["nodes"].pop("g")), id="rename"),
            pytest.param(lambda d: d["nodes"]["g"].update(component="T/text"), id="to-other"),
            pytest.param(lambda d: d["nodes"]["n"].update(component="T/query"), id="to-sensitive"),
            pytest.param(
                lambda d: d["nodes"]["f"].update(config={"sql": "SELECT 2"}), id="config"
            ),
            pytest.param(lambda d: d["nodes"]["f"].pop("config"), id="config-to-default"),
            pytest.param(lambda d: d["edges"].pop(0), id="disconnect"),
            pytest.param(lambda d: d["edges"][0].update({"from": "f.rows"}), id="rewire"),
            pytest.param(
                lambda d: d["edges"].append({"from": "t.text", "to": "g.table"}), id="connect"
            ),
            pytest.param(
                lambda d: d["edges"][0]["transform"][0].update(prefix="b"), id="change-transform"
            ),
            pytest.param(lambda d: d["edges"][0].pop("transform"), id="drop-transform"),
            pytest.param(
                lambda d: d["edges"][1].update(transform=[{"op": "format", "suffix": "!"}]),
                id="add-transform",
            ),
            pytest.param(lambda d: d.pop("nodes"), id="empty"),
        ],
    )
    @pytest.mark.parametrize("tool", ["preview_dashboard", "replace_dashboard"])
    async def test_yaml_changing_a_sensitive_node_or_its_inputs_asks(self, wired, edit, tool):
        _, policy, doc = wired
        assert self._asks(policy, self._edited(doc, edit), tool)

    async def test_how_values_are_written_does_not_count(self, wired):
        _, policy, _ = wired

        def written(limit="5", step="{op: format, prefix: a}", sql="SELECT 1"):
            return (
                "flowdash: 1\nnodes:\n  t: {component: T/text}\n"
                f"  f: {{component: T/query, config: {{sql: {sql}, limit: {limit}}}}}\n"
                "  g: {component: T/query}\n  n: {component: T/number}\nedges:\n"
                f"  - {{from: t.text, to: f.table, transform: [{step}]}}\n"
                "  - {from: f.rows, to: g.table}\n"
            )

        assert not self._asks(policy, written())
        assert not self._asks(policy, written(limit="0x5"))
        assert not self._asks(policy, written(step="{prefix: a, op: format}"))
        assert not self._asks(policy, written(step="{op: format, prefix: a, suffix: ''}"))
        assert not self._asks(policy, written(sql="'SELECT 1'"))
        renamed = written().replace("sql: SELECT 1", "query: SELECT 1")
        assert not self._asks(policy, renamed.replace("to: f.table", "to: f.source"))
        assert self._asks(policy, written().replace("{sql:", "{query: SELECT 2, sql:"))
        assert self._asks(policy, written(limit="5.0"))
        assert self._asks(policy, written(limit="5.5"))
        assert self._asks(policy, written(limit="'5'"))
        assert self._asks(policy, written(step="{op: format, prefix: b}"))

    async def test_unreadable_or_missing_yaml_does_not_ask(self, wired):
        _, policy, _ = wired
        assert not self._asks(policy, "nodes: [unclosed", "preview_dashboard")
        assert not policy.requires_approval("preview_dashboard", {})

    async def test_editing_around_a_sensitive_node_runs_without_asking(self, live):
        _, ops = self._run(live)
        ops.add_node("T/fetch", {"url": "http://x"}, name="f")
        FETCHED.clear()
        doc = yaml.safe_load(ops.get_dashboard())
        doc["nodes"]["n"] = {"component": "T/number"}
        text = ops.get_dashboard().splitlines()[0] + "\n" + yaml.safe_dump(doc, sort_keys=False)
        runner, _ = self._run(live, ("replace_dashboard", {"dashboard_yaml": text}))
        output = await runner.run("Add a number")
        assert "Replaced the dashboard" in output
        assert not runner.state.approvals
        assert {i["component_id"] for i in live._editor._tile_items} == {"T/fetch", "T/number"}

    async def test_removing_a_sensitive_node_with_yaml_asks(self, live):
        _, ops = self._run(live)
        ops.add_node("T/fetch", {"url": "http://x"}, name="f")
        doc = yaml.safe_load(ops.get_dashboard())
        doc["nodes"] = {"n": {"component": "T/number"}}
        text = ops.get_dashboard().splitlines()[0] + "\n" + yaml.safe_dump(doc, sort_keys=False)
        runner, _ = self._run(live, ("replace_dashboard", {"dashboard_yaml": text}))
        await runner.run("Swap it")
        assert [r.tool_name for r in runner.state.approvals.values()] == ["replace_dashboard"]
        assert [i["component_id"] for i in live._editor._tile_items] == ["T/fetch"]

    @pytest.fixture
    async def sunk(self, live):
        """Two sources and a sensitive sink fed by the first, on its single and its list input."""
        _, ops = self._run(live)
        ops.add_node("P/srca", name="a")
        ops.add_node("P/srcb", name="b")
        ops.add_node("P/sink", {"n": 1, "options": {"x": 1, "y": 2}}, name="s")
        ops.connect("a.text", "s.table")
        ops.connect("a.text", "s.tables")
        ops.connect("b.text", "s.tables")
        policy = approval_policy(sensitive_components=["P/sink"], ops=ops)
        return ops, policy

    def _apply(self, ops, policy, edit):
        """Edit the canvas YAML, then return whether that asks and the sink's inputs and config."""
        doc = yaml.safe_load(ops.get_dashboard())
        edit(doc)
        text = ops.get_dashboard().splitlines()[0] + "\n" + yaml.safe_dump(doc, sort_keys=False)
        asks = self._asks(policy, text)
        ops.replace_yaml(text)
        state = ops.editor.graph.get_state(ops._instance("s"))
        return asks, state.table, list(state.tables), ops.node_config("s")

    async def test_the_sink_starts_from_the_first_source(self, sunk):
        ops, policy = sunk
        assert self._apply(ops, policy, lambda d: None) == (
            False,
            "FROM_A",
            ["FROM_A", "FROM_B"],
            {"n": 1, "options": {"x": 1, "y": 2}},
        )

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda d: d["nodes"]["a"].update(component="P/srcb"), id="retype"),
            pytest.param(
                lambda d: d["nodes"].update(a=d["nodes"]["b"], b=d["nodes"]["a"]), id="swap"
            ),
        ],
    )
    async def test_changing_the_component_feeding_a_sensitive_node_asks(self, sunk, edit):
        ops, policy = sunk
        asks, table, _, _ = self._apply(ops, policy, edit)
        assert table == "FROM_B" and asks

    async def test_reordering_the_edges_of_a_list_input_asks(self, sunk):
        ops, policy = sunk

        def swap(doc):
            doc["edges"][1:3] = doc["edges"][2:0:-1]

        asks, _, tables, _ = self._apply(ops, policy, swap)
        assert tables == ["FROM_B", "FROM_A"] and asks

    async def test_reordering_edges_into_different_inputs_does_not_ask(self, sunk):
        ops, policy = sunk

        def move(doc):
            doc["edges"].append(doc["edges"].pop(0))

        assert self._apply(ops, policy, move)[:3] == (False, "FROM_A", ["FROM_A", "FROM_B"])

    async def test_writing_an_int_as_a_float_asks(self, sunk):
        ops, policy = sunk
        asks, _, _, config = self._apply(
            ops, policy, lambda d: d["nodes"]["s"]["config"].update(n=1.0)
        )
        assert type(config["n"]) is float and asks

    async def test_reordering_the_keys_of_a_config_value_asks(self, sunk):
        ops, policy = sunk

        def reorder(doc):
            doc["nodes"]["s"]["config"]["options"] = {"y": 2, "x": 1}

        asks, _, _, config = self._apply(ops, policy, reorder)
        assert list(config["options"]) == ["y", "x"] and asks

    async def test_reordering_config_fields_does_not_ask(self, sunk):
        ops, policy = sunk

        def reorder(doc):
            config = doc["nodes"]["s"]["config"]
            doc["nodes"]["s"]["config"] = dict(reversed(config.items()))

        assert not self._apply(ops, policy, reorder)[0]

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
        assert agent.summary == "Added double. Connected base to double."
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
        assert ops.editor.turns == [], "a replace that changes nothing opens no turn"

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

    async def test_set_config_sets_values_of_unconnected_inputs(self, ops):
        editor = ops.editor
        editor.add_component("T/doubler", instance_id="double")
        runner = _runner(ops, ("set_config", {"name": "double", "inputs": {"value": 4}}))
        output = await runner.run("Set")
        assert "Updated the inputs of 'double'." in output
        assert editor.input_values("double") == {"value": 4}
        assert editor.graph.get_state("double").doubled == 8

    async def test_set_config_needs_config_or_inputs(self, ops):
        ops.editor.add_component("T/doubler", instance_id="double")
        runner = _runner(ops, ("set_config", {"name": "double"}))
        output = await runner.run("Set")
        assert "Pass config, inputs or both." in output

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

    async def test_a_failed_request_keeps_its_turn_and_the_next_opens_another(self, ops):
        def failing(messages, info):
            if sum(isinstance(m, ModelResponse) for m in messages):
                raise RuntimeError("The model went away")
            return ModelResponse(
                parts=[ToolCallPart("add_node", {"component": "T/number", "name": "first"})]
            )

        async def stream(messages, info):
            (part,) = failing(messages, info).parts
            yield {
                0: DeltaToolCall(
                    name=part.tool_name,
                    json_args=part.args_as_json_str(),
                    tool_call_id="call-1",
                )
            }

        agent = Agent(
            FunctionModel(failing, stream_function=stream),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset()],
        )
        runner = AgentRunner(agent, RunState(flush_interval=0), deps=FlowDashDeps(ops))
        runner.deps.start_request("Add one")
        await runner.run("Add one")
        runner.deps.end_request()
        assert runner.error is not None
        (failed,) = ops.editor.turns
        assert (failed.question, failed.summary) == ("Add one", "Added first.")
        again = _runner(ops, ("add_node", {"component": "T/number", "name": "second"}))
        await again.run("Add another")
        first, second = ops.editor.turns
        assert first == failed and second.run == again.run_id != failed.run
        assert ops.editor.node_origin("second") == second.id
