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

HISTORY_RUN = "dashboard-earlier"
HISTORY_CELL = f"{HISTORY_RUN}:nodes"

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
    cells = {a.artifact_id: a for a in feed.state.artifacts}
    assert set(cells) == {"node:explored", HISTORY_CELL}
    # The drawer's request is listed, collapsed, among the turns made outside Explore.
    (drawn,) = [t for t in app._editor.turns if t.run == drawer_run]
    assert cells[HISTORY_CELL].value == {"nodes": [], "turns": [drawn.id]}


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
    explored, drawn, by_hand = editor.turns
    assert list(feed.state.runs) == [HISTORY_RUN, f"turn-{explored.id}"]
    assert _questions(feed.state) == ["How many are there?"]
    cells = {a.artifact_id: a for a in feed.state.artifacts}
    assert set(cells) == {"node:explored", HISTORY_CELL}
    assert cells[HISTORY_CELL].value["turns"] == [drawn.id, by_hand.id]


async def test_turns_asked_before_explore_had_its_own_agent_stay_in_a_collapsed_entry(
    store, monkeypatch
):
    from panel_flowdash.turns import agent_edits

    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    editor = app._editor
    with agent_edits(run="run-old", question="An old question"):
        editor.add_component("T/number", instance_id="old")
    editor.end_agent_turns()
    feed = editor.explore_feed
    feed.shown = True
    assert list(feed.state.runs) == [HISTORY_RUN]
    (old,) = editor.turns
    cell = next(a for a in feed.state.artifacts if a.artifact_id == HISTORY_CELL)
    assert cell.value == {"nodes": [], "turns": [old.id]}
    body = feed._render(cell)
    toggle, shown = body.objects
    assert not shown.visible and shown.objects == []
    toggle.clicks += 1
    assert shown.visible
    text, nodes = shown.objects
    assert text.object == "**Assistant**: An old question\n\n- Added old"
    assert nodes.objects[0].objects[0].label == "old"


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


def _gated(steps):
    """A model adding a node per step, each step waiting for its gate (an asyncio.Event or None)."""
    from pydantic_ai.models.function import DeltaToolCall

    from .test_agent_tools import _results

    async def stream(messages, info):
        done = len(_results(messages))
        if done >= len(steps):
            yield "Done."
            return
        name, gate = steps[done]
        if gate is not None:
            await gate.wait()
        args = f'{{"component": "T/number", "name": "{name}"}}'
        yield {0: DeltaToolCall(name="add_node", json_args=args, tool_call_id=f"{name}-call")}

    return Agent(
        FunctionModel(stream_function=stream),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )


async def _until(condition, task, timeout=10):
    import asyncio

    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        if task.done():
            task.result()
            raise AssertionError("The run ended before the condition held.")
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out")


@pytest.mark.parametrize("explore_last", [False, True])
async def test_runs_of_both_surfaces_at_once_keep_their_own_turn_and_version(
    store, monkeypatch, explore_last
):
    import asyncio

    resume = asyncio.Event()
    app, _ = _open(
        store,
        monkeypatch,
        agent=_gated([("d1", None), ("d2", resume)]),
        explore_agent=_gated([("e1", None)]),
    )
    session = app._agent_session
    editor = app._editor
    nodes = lambda: [i["instance_id"] for i in editor._tile_items]
    drawer = asyncio.ensure_future(session.runner.run("Add two numbers"))
    await _until(lambda: "d1" in nodes(), drawer)
    if explore_last:
        explore = asyncio.ensure_future(session.explore_runner.run("And one more?"))
        await _until(lambda: "e1" in nodes(), explore)
        resume.set()
        await drawer
        await explore
    else:
        await session.explore_runner.run("And one more?")
        resume.set()
        await drawer
    assert nodes() == ["d1", "e1", "d2"]
    drawn, explored = (
        next(t for t in editor.turns if t.run == session.runner.run_id),
        next(t for t in editor.turns if t.run == session.explore_runner.run_id),
    )
    assert len(editor.turns) == 2
    assert {n: editor.node_origin(n) for n in nodes()} == {
        "d1": drawn.id,
        "e1": explored.id,
        "d2": drawn.id,
    }
    (artifact,) = session.runner.state.artifacts
    runs = [v.run_id for v in artifact.versions if v.actor == "agent"]
    assert runs == [explored.run, drawn.run]
    # The latest version is the canvas as it is: nothing is lost to an older snapshot.
    assert all(f"\n  {n}:" in artifact.applied_version.value for n in nodes())


def _ask_approval(state, run_id, call_id):
    from panel_agents import events as ev

    state.apply_many(
        [
            ev.RunStarted(run_id=run_id),
            ev.ToolCallStart(tool_call_id=call_id, tool_name="save_dashboard", run_id=run_id),
            ev.ToolCallEnd(tool_call_id=call_id, run_id=run_id),
            ev.ApprovalRequested(
                tool_call_id=call_id,
                tool_name="save_dashboard",
                args={},
                description="Save the dashboard. It is shared.",
                run_id=run_id,
            ),
        ]
    )


async def test_an_assistant_approval_waiting_behind_explore_shows_a_notice(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    chrome = session.chrome
    editor.explore = True
    _ask_approval(session.runner.state, "run-a", "c1")
    assert not session.drawer.open
    assert chrome.notice.visible
    assert chrome._notice_text.object.startswith("The assistant is waiting for your approval")
    chrome.review()
    assert not editor.explore and session.drawer.open and not chrome.notice.visible


async def test_an_explore_approval_left_behind_shows_a_notice(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    chrome = session.chrome
    editor.explore = True
    _ask_approval(session.explore_runner.state, "explore-a", "c2")
    # Answered in the feed while Explore is shown.
    assert not chrome.notice.visible
    editor.explore = False
    assert chrome.notice.visible and not session.drawer.open
    assert chrome._notice_text.object.startswith("Explore is waiting for your approval")
    chrome.review()
    assert editor.explore and not chrome.notice.visible
    session.explore_runner.state.resolve_approval("c2", False)
    editor.explore = False
    assert not chrome.notice.visible


async def test_a_run_id_given_to_the_explore_runner_still_marks_it_as_explores(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    runner = app._agent_session.explore_runner
    await runner.run("How many are there?", run_id="mine")
    assert runner.run_id == f"{EXPLORE_RUN_PREFIX}mine"
    assert app._editor.turns[0].run == runner.run_id
