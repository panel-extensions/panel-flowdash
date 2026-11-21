"""The Explore view's own agent runner, apart from the assistant's in the drawer."""

import pytest

pytest.importorskip("pydantic_ai")

from panel_agents import ModelChoice
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.assistant import (
    EDITING,
    EXPLORE_INSTRUCTIONS,
    EXPLORE_TOOLS,
    create_explore_agent,
)
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.explore import EXPLORE_RUN_PREFIX, is_explore_run

from .test_agent_tools import _app, scripted


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)
    monkeypatch.delenv("FLOWDASH_AGENT_MODELS", raising=False)


@pytest.fixture
def store():
    return MemoryDashboardStore()


def answering(text):
    async def stream(messages, info):
        yield text

    return FunctionModel(stream_function=stream, model_name="answering")


def _adding(name):
    return Agent(
        scripted(("add_node", {"component": "T/number", "name": name})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )


def _open(store, monkeypatch, **params):
    model = store.create_dashboard("alice", "Live")
    app = _app(store, monkeypatch, **params)
    app._load_dashboard_sync(model.dashboard_id)
    return app, model


def _tools(agent):
    return {name for toolset in agent.toolsets for name in getattr(toolset, "tools", {})}


def _questions(state):
    return [m.content for m in state.items if getattr(m, "role", None) == "user"]


async def test_explore_asks_its_own_runner_with_its_own_state(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session = app._agent_session
    editor = app._editor
    explore = session.explore_runner
    assert explore is not None and explore is not session.runner
    assert explore.state is not session.runner.state
    assert session.runner.state is editor.edit_history.state
    assert editor.explore_feed.runner is explore
    assert session.runners == [session.runner, explore]


async def test_each_surface_shows_only_its_own_runs(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session = app._agent_session
    feed = app._editor.explore_feed
    feed.shown = True
    await session.explore_runner.run("How many are there?")
    await session.runner.run("Add a number")
    explore_run, drawer_run = session.explore_runner.run_id, session.runner.run_id
    assert is_explore_run(explore_run) and not is_explore_run(drawer_run)

    assert _questions(session.runner.state) == ["Add a number"]
    assert explore_run not in session.runner.state.runs
    feed.sync()
    assert _questions(feed.state) == ["How many are there?"]
    assert explore_run in feed.state.runs and drawer_run not in feed.state.runs
    assert set(a.artifact_id for a in feed.state.artifacts) == {"node:explored"}


async def test_turns_and_versions_are_recorded_per_runner(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session = app._agent_session
    editor = app._editor
    await session.explore_runner.run("How many are there?")
    await session.runner.run("Add a number")
    explored, drawn = editor.turns
    assert (explored.actor, explored.question) == ("agent", "How many are there?")
    assert explored.run == session.explore_runner.run_id
    assert explored.run.startswith(EXPLORE_RUN_PREFIX)
    assert (drawn.run, drawn.question) == (session.runner.run_id, "Add a number")
    assert editor.node_origin("explored") == explored.id
    assert editor.node_origin("drawn") == drawn.id

    # Each request is one version of the edit history, which the drawer lists.
    (artifact,) = session.runner.state.artifacts
    requests = [v for v in artifact.versions if v.actor == "agent"]
    assert [v.run_id for v in requests] == [explored.run, drawn.run]
    assert all(v.metadata["op"] == "request" for v in requests)
    assert editor.undo() and [i["instance_id"] for i in editor._tile_items] == ["explored"]
    assert editor.undo() and editor._tile_items == []

    # "How was this made?" reads each run's steps from the runner that ran it.
    assert editor._run_state(explored.run) is session.explore_runner.state
    assert editor._run_state(drawn.run) is session.runner.state


async def test_the_history_waits_for_explore_runs(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session = app._agent_session
    history = app._editor.edit_history
    seen = []
    session.explore_runner.state.param.watch(lambda e: seen.append(history.busy), "running")
    await session.explore_runner.run("How many are there?")
    assert seen == [True, False]
    assert not history.busy


async def test_explore_turns_are_told_apart_after_a_reload(store, monkeypatch):
    app, model = _open(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored")
    )
    session = app._agent_session
    await session.explore_runner.run("How many are there?")
    await session.runner.run("Add a number")
    app._editor.end_turn()
    app._editor.add_component("T/number", instance_id="by_hand")
    assert session.ops.save() == model.dashboard_id

    again, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    again._load_dashboard_sync(model.dashboard_id)
    editor = again._editor
    feed = editor.explore_feed
    feed.shown = True
    explored = editor.turns[0]
    assert list(feed.state.runs) == [f"turn-{explored.id}"]
    assert _questions(feed.state) == ["How many are there?"]
    assert {a.artifact_id for a in feed.state.artifacts} == {"node:explored"}


async def test_without_explore_asking_the_feed_records_every_turn(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=False)
    session = app._agent_session
    feed = app._editor.explore_feed
    assert session.explore_runner is None and feed.runner is None
    assert session.runners == [session.runner]
    await session.runner.run("Add a number")
    feed.shown = True
    assert feed.chat.readonly
    (drawn,) = app._editor.turns
    assert list(feed.state.runs) == [f"turn-{drawn.id}"]


async def test_the_default_explore_agent_shares_the_assistants_model(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "test")
    app, _ = _open(store, monkeypatch)
    session = app._agent_session
    explore = session.explore_runner.agent
    assert explore.name == "Explore"
    assert explore.model is not None and explore.model.model_name == "test"
    assert _tools(explore) == set(EXPLORE_TOOLS)
    assert EXPLORE_INSTRUCTIONS.splitlines()[0] in str(explore._instructions)
    # The assistant is told that analysis questions belong in the Explore view.
    assert EDITING.splitlines()[0] in str(session.runner.agent._instructions)


async def test_turning_explore_off_leaves_the_assistant_as_it_was(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "test")
    app, _ = _open(store, monkeypatch, explore_agent=False)
    session = app._agent_session
    assert session.explore_runner is None
    assert EDITING.splitlines()[0] not in str(session.runner.agent._instructions)


async def test_an_explore_agent_passed_in_is_used_as_is(store, monkeypatch):
    mine = _adding("explored")
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=mine)
    assert app._agent_session.explore_runner.agent is mine


async def test_an_assistant_passed_in_gets_an_explore_agent_on_its_model(store, monkeypatch):
    drawer = Agent(answering("Hi"), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    app, _ = _open(store, monkeypatch, agent=drawer)
    explore = app._agent_session.explore_runner.agent
    assert explore is not drawer and explore.model is drawer.model


async def test_no_explore_agent_without_a_model(store, monkeypatch):
    drawer = Agent(None, deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    app, _ = _open(store, monkeypatch, agent=drawer)
    assert app._agent_session.explore_runner is None


async def test_the_picked_model_is_shared_by_both_surfaces(store, monkeypatch):
    models = [
        ModelChoice("fast-model", label="Fast", model=answering("fast")),
        ModelChoice("deep-model", label="Deep", model=answering("deep")),
    ]
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), agent_models=models)
    session = app._agent_session
    feed = app._editor.explore_feed
    feed.shown = True
    drawer_chat, explore_chat = session.drawer.chat, feed.chat
    assert explore_chat.runner is session.explore_runner
    drawer_chat.model_id = "deep-model"
    assert explore_chat.model_id == "deep-model"
    assert session.runner.model is session.explore_runner.model is models[1].model
    explore_chat.model_id = "fast-model"
    assert drawer_chat.model_id == "fast-model"
    assert session.runner.model is session.explore_runner.model is models[0].model


def test_the_explore_agent_reads_the_dashboard_and_adds_nodes_only():
    agent = create_explore_agent(answering("Hi"))
    assert agent.name == "Explore"
    assert _tools(agent) == set(EXPLORE_TOOLS)


async def test_the_explore_agent_is_told_which_dashboard_is_open(store, monkeypatch):
    seen = []

    async def stream(messages, info):
        seen.append(messages[0].instructions)
        yield "Fine."

    app, _ = _open(store, monkeypatch, agent=_adding("drawn"))
    session = app._agent_session
    session.explore_runner.agent = create_explore_agent(FunctionModel(stream_function=stream))
    await session.explore_runner.run("What is this?")
    assert "The open dashboard is 'Live'." in seen[0]
    assert "T/number" in seen[0]
