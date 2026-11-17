"""The default agent and its connection to a FlowDashApp session."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.assistant import INSTRUCTIONS, catalogue, create_agent
from panel_flowdash.agent.session import AgentSession
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from .test_agent_tools import _app, scripted


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)


@pytest.fixture
def store():
    return MemoryDashboardStore()


def test_no_agent_without_a_model(store, monkeypatch):
    assert _app(store, monkeypatch)._agent_session is None


def test_the_off_switch_wins_over_a_configured_model(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "test")
    assert _app(store, monkeypatch, agent=False)._agent_session is None


def test_a_configured_model_gives_the_default_agent(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "test")
    session = _app(store, monkeypatch)._agent_session
    assert isinstance(session, AgentSession)
    assert session.runner.agent.name == "FlowDash"
    assert session.runner.approval_policy.requires_approval("save_dashboard", {})


def test_the_catalogue_lists_only_components_the_user_may_place(store, monkeypatch):
    session = AgentSession(_app(store, monkeypatch), create_agent("test"))
    text = catalogue(session.ops)
    assert "- T/doubler (Doubler)\n  in(value: int) out(doubled: int)" in text
    assert "- T/scaled (Scaled)\n  in(value: int?) out(out: int) config(factor=1)" in text
    assert "T/secret" not in text


async def test_the_instructions_carry_the_catalogue(store, monkeypatch):
    agent = create_agent(TestModel(call_tools=[]))
    session = AgentSession(_app(store, monkeypatch), agent)
    async with agent.iter("hi", deps=FlowDashDeps(session.ops)) as run:
        async for _ in run:
            pass
    request = run.result.all_messages()[0]
    assert request.instructions.startswith(INSTRUCTIONS.splitlines()[0])
    assert "T/doubler (Doubler)" in request.instructions


async def test_the_agent_edits_the_open_dashboard_and_versions_can_be_applied(store, monkeypatch):
    model = store.create_dashboard("alice", "Live")
    app = _app(store, monkeypatch)
    app._load_dashboard_sync(model.dashboard_id)
    agent = Agent(
        scripted(
            ("add_node", {"component": "T/number"}),
            ("add_node", {"component": "T/doubler"}),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    session = AgentSession(app, agent)
    await session.runner.run("Build")
    assert session.runner.error is None, [
        c.error for c in session.runner.state.tool_calls.values()
    ]
    assert [i["component_id"] for i in app._editor._tile_items] == ["T/number", "T/doubler"]
    assert app._editor.editable and app._has_unsaved_changes()
    (artifact,) = session.runner.state.artifacts
    assert session.apply_version(artifact, artifact.versions[0])
    assert [i["component_id"] for i in app._editor._tile_items] == ["T/number"]
