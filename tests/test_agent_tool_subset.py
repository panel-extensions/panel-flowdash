"""An agent can be given a subset of the dashboard tools, its own instructions only, and a shorter catalogue."""

import pytest

pytest.importorskip("pydantic_ai")

from panel_agents.adapters.pydantic_ai import AgentRunner
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent import DashboardOps, FlowDashDeps
from panel_flowdash.agent.assistant import _REVISION, INSTRUCTIONS, SAFETY, create_agent
from panel_flowdash.agent.live import LiveDashboardOps
from panel_flowdash.agent.tools import TOOLS, approval_policy, dashboard_toolset
from panel_flowdash.editor import FlowDash

from .test_agent_ops import _editor
from .test_agent_tools import _app, _resolve, ops, scripted, store  # noqa: F401


async def _request(**params):
    seen = {}

    def respond(messages, info):
        seen["tools"] = [tool.name for tool in info.function_tools]
        seen["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("ok")])

    editor = FlowDash()
    try:
        agent = create_agent(FunctionModel(respond), **params)
        await agent.run("Hi", deps=FlowDashDeps(DashboardOps(editor)))
    finally:
        editor.dispose()
    return seen


def _runner(target, tools, *calls, policy=None):
    agent = create_agent(
        scripted(*calls), tools=tools, default_instructions=False, catalogue="none"
    )
    return AgentRunner(
        agent,
        target.editor.edit_history.state,
        deps=FlowDashDeps(target),
        approval_policy=policy or approval_policy(),
    )


async def test_default_agent_offers_every_tool_and_the_full_instructions():
    seen = await _request()
    assert seen["tools"] == [func.__name__ for func in TOOLS]
    instructions, catalogue = seen["instructions"].split("\n\nComponents you may place", 1)
    assert instructions + "\n" == INSTRUCTIONS
    assert "in(" in catalogue or "out(" in catalogue


async def test_subset_keeps_tool_order_and_collapses_duplicates():
    seen = await _request(tools=["set_config", "get_dashboard", "set_config"])
    assert seen["tools"] == ["get_dashboard", "set_config"]
    assert (await _request(tools=[]))["tools"] == []


async def test_own_instructions_keep_the_safety_rules():
    seen = await _request(
        default_instructions=False, instructions="Answer questions about data.", catalogue="none"
    )
    assert (
        seen["instructions"] == "Answer questions about data.\n\n" + (SAFETY + _REVISION).strip()
    )
    assert "never put\n  links or images" in seen["instructions"]
    assert all(rule in INSTRUCTIONS for rule in SAFETY.splitlines())


async def test_own_instructions_keep_the_revision_rule_only_with_replace_dashboard():
    rule = 'Keep the "# revision:" line'
    narrow = await _request(
        default_instructions=False, tools=["get_dashboard", "set_config"], catalogue="none"
    )
    assert rule not in narrow["instructions"]
    for tools in (None, ["get_dashboard", "replace_dashboard"]):
        seen = await _request(default_instructions=False, tools=tools, catalogue="none")
        assert rule in seen["instructions"]
        assert "list_commands" not in seen["instructions"]


async def test_compact_catalogue_points_to_list_components_only_when_offered():
    offered = await _request(catalogue="compact", default_instructions=False)
    assert "call list_components with a query" in offered["instructions"]
    assert "out(" not in offered["instructions"]
    without = await _request(
        catalogue="compact", default_instructions=False, tools=["get_dashboard"]
    )
    assert "Components you may place:\n" in without["instructions"]
    assert "list_components" not in without["instructions"]


async def test_agent_without_catalogue():
    seen = await _request(default_instructions=False, instructions="Only this.", catalogue="none")
    assert "Components you may place" not in seen["instructions"]


def test_unknown_tool_string_or_catalogue_is_refused():
    with pytest.raises(ValueError, match="Unknown dashboard tools: nope"):
        dashboard_toolset(["get_dashboard", "nope"])
    with pytest.raises(TypeError, match="not the string 'get_dashboard'"):
        dashboard_toolset("get_dashboard")
    with pytest.raises(ValueError, match="catalogue must be one of"):
        create_agent("test", catalogue="short")


@pytest.mark.parametrize("approved", [True, False])
async def test_save_in_a_subset_still_needs_approval_with_its_summary(ops, approved):  # noqa: F811
    runner = _runner(
        ops,
        ["add_node", "save_dashboard"],
        ("add_node", {"component": "T/number"}),
        ("save_dashboard", {}),
    )
    _resolve(runner.state, approved)
    output = await runner.run("Add a number and save")
    (request,) = runner.state.approvals.values()
    assert request.tool_name == "save_dashboard"
    assert "It stores:" in request.description
    assert ("Saved dashboard" in output) is approved


async def test_edit_in_a_subset_publishes_an_artifact_and_calls_listeners(ops):  # noqa: F811
    calls = []
    runner = _runner(ops, ["add_node"], ("add_node", {"component": "T/number"}))
    runner.deps.edit_listeners.append(lambda before, after: calls.append((before, after)))
    await runner.run("Add a number")
    assert len(calls) == 1
    (artifact,) = runner.state.artifacts
    assert artifact.versions


async def test_viewer_is_refused_in_a_subset(store):  # noqa: F811
    model = store.create_dashboard("bob", "Bob's")
    editor = _editor(store, access={model.dashboard_id: "view"})
    viewer = DashboardOps(editor)
    viewer.open_dashboard(model.dashboard_id)
    runner = _runner(viewer, ["add_node"], ("add_node", {"component": "T/number"}))
    assert "Refused: You may only view" in await runner.run("Add a number")


async def test_request_pin_holds_in_a_subset(monkeypatch):
    dashboards = MemoryDashboardStore()
    first = dashboards.create_dashboard("alice", "First")
    second = dashboards.create_dashboard("alice", "Second")
    app = _app(dashboards, monkeypatch)
    app._load_dashboard_sync(first.dashboard_id)
    runner = _runner(LiveDashboardOps(app), ["add_node"], ("add_node", {"component": "T/number"}))
    runner.deps.start_request()
    app._load_dashboard_sync(second.dashboard_id)
    assert "switched to another dashboard" in await runner.run("Add a number")


@pytest.mark.parametrize("palette", [True, False])
async def test_app_can_leave_the_palette_tools_out(monkeypatch, palette):
    seen = []

    async def stream(messages, info):
        seen.append({tool.name for tool in info.function_tools})
        yield "ok"

    app = _app(
        MemoryDashboardStore(),
        monkeypatch,
        agent=create_agent(FunctionModel(stream_function=stream)),
        agent_palette=palette,
    )
    await app._agent_session.runner.run("Hi")
    assert ({"list_commands", "run_command"} <= seen[0]) is palette
