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
from .test_exploration import _exploring, _open as _seeded


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
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
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
    assert set(cells) == {f"explore:{explore_run}:explored", HISTORY_CELL}
    # The drawer's request is listed, collapsed, among the turns made outside Explore.
    (drawn,) = app._editor.turns
    assert drawn.run == drawer_run
    assert cells[HISTORY_CELL].value == {"nodes": ["base"], "turns": [drawn.id]}


async def test_turns_and_versions_are_recorded_per_runner(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session = app._agent_session
    editor = app._editor
    await session.explore_runner.run("How many are there?")
    await session.runner.run("Add a number")
    # The Explore run made no turn of the dashboard; adding its answer does.
    (drawn,) = editor.turns
    await session.add_answer(session.explore_runner.run_id, "explored")
    drawn, added = editor.turns
    assert (added.actor, added.question) == ("agent", "How many are there?")
    assert added.run == session.explore_runner.run_id
    assert added.run.startswith(EXPLORE_RUN_PREFIX)
    assert (drawn.run, drawn.question) == (session.runner.run_id, "Add a number")
    assert editor.node_origin("explored") == added.id
    assert editor.node_origin("drawn") == drawn.id

    # The request and the added answer are a version each of the edit history, which the drawer lists.
    (artifact,) = session.runner.state.artifacts
    requests = [v for v in artifact.versions if v.actor == "agent"]
    assert [v.run_id for v in requests] == [drawn.run, added.run]
    assert all(v.metadata["op"] == "request" for v in requests)
    assert editor.undo() and [i["instance_id"] for i in editor._tile_items] == ["base", "drawn"]
    assert editor.undo() and [i["instance_id"] for i in editor._tile_items] == ["base"]

    # "How was this made?" reads each run's steps from the runner that ran it.
    assert editor._run_state(added.run) is session.explore_runner.state
    assert editor._run_state(drawn.run) is session.runner.state


async def test_explore_turns_are_told_apart_after_a_reload(store, monkeypatch):
    app, model = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session = app._agent_session
    await session.explore_runner.run("How many are there?")
    await session.add_answer(session.explore_runner.run_id, "explored")
    await session.runner.run("Add a number")
    app._editor.end_turn()
    app._editor.add_component("T/number", instance_id="by_hand")
    assert session.ops.save() == model.dashboard_id

    again = _app(store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored"))
    again._load_dashboard_sync(model.dashboard_id)
    editor = again._editor
    feed = editor.explore_feed
    feed.shown = True
    added, drawn, by_hand = editor.turns
    # Answers are kept for the session; the one added shows as the dashboard recorded it.
    assert list(feed.state.runs) == [HISTORY_RUN, f"turn-{added.id}"]
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


def _gated(steps, end=None):
    """A model adding a node per step, each waiting for its gate, then answering once *end* is set.

    A step named "FAIL" makes the model fail instead.
    """
    from pydantic_ai.models.function import DeltaToolCall

    from .test_agent_tools import _results

    async def stream(messages, info):
        results = _results(messages)
        if len(results) >= len(steps):
            if end is not None:
                await end.wait()
            yield f"Done: {results[-1].content}"
            return
        name, gate = steps[len(results)]
        if gate is not None:
            await gate.wait()
        if name == "FAIL":
            raise RuntimeError("The model failed")
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


async def test_a_request_never_waits_for_itself(store, monkeypatch):
    app, _ = _open(
        store,
        monkeypatch,
        agent=_gated([("a1", None), ("a2", None)]),
        explore_agent=_adding("explored"),
    )
    await app._agent_session.runner.run("A please")
    assert [i["instance_id"] for i in app._editor._tile_items] == ["a1", "a2"]
    assert app._agent_session.edit_lock._holders == {}


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
    # In view mode the feed comes back docked in Ask, beside the dashboard.
    assert editor.feed_shown and not chrome.notice.visible
    assert editor.ask_docked or editor.explore
    session.explore_runner.state.resolve_approval("c2", False)
    editor.param.update(explore=False, ask=False)
    assert not chrome.notice.visible


async def test_a_run_id_given_to_the_explore_runner_still_marks_it_as_explores(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    runner = app._agent_session.explore_runner
    await runner.run("How many are there?", run_id="mine")
    assert runner.run_id == f"{EXPLORE_RUN_PREFIX}mine"
    (turn,) = app._agent_session.explorations.exploration().editor.turns
    assert turn.run == runner.run_id


async def test_a_dismissed_approval_notice_stays_dismissed_and_counts_the_rest(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    chrome = session.chrome
    editor.explore = True
    _ask_approval(session.runner.state, "run-a", "c1")
    chrome.dismiss()
    assert not chrome.notice.visible
    editor.explore = False
    editor.explore = True
    assert not chrome.notice.visible
    # Another approval shows the notice again, counting the waiting ones it has not told of.
    _ask_approval(session.runner.state, "run-b", "c2")
    assert chrome.notice.visible
    assert chrome._notice_text.object.startswith("The assistant is waiting for your approval")
    _ask_approval(session.runner.state, "run-c", "c3")
    assert chrome._notice_text.object.startswith("The assistant is waiting for 2 approvals")


async def test_the_earlier_entry_stays_at_the_top_while_older_pages_are_hidden(store, monkeypatch):
    from panel_flowdash.turns import agent_edits

    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    editor = app._editor
    with agent_edits(run="run-old", question="An old question"):
        editor.add_component("T/number", instance_id="old")
    for i in range(4):
        editor.end_agent_turns()
        with agent_edits(run=f"explore-q{i}", question=f"Question {i}"):
            editor.add_component("T/number", instance_id=f"q{i}")
    editor.end_agent_turns()
    feed = editor.explore_feed
    feed.page_size = 2
    feed.shown = True
    runs = list(feed.state.runs)
    assert runs[0] == HISTORY_RUN and len(runs) == 3
    assert feed._earlier.label == "Show earlier requests (2 more)"
    feed.show_earlier()
    assert next(iter(feed.state.runs)) == HISTORY_RUN and len(feed.state.runs) == 5


async def test_a_drawer_run_is_never_taken_for_an_explore_question(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session = app._agent_session
    await session.runner.run("Add a number", run_id="explore-looking")
    feed = app._editor.explore_feed
    feed.shown = True
    (drawn,) = app._editor.turns
    assert drawn.run == "explore-looking"
    assert list(feed.state.runs) == [HISTORY_RUN]


async def test_the_drawer_is_ask_in_edit_mode(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    assert editor.assistant
    editor.editable = False
    editor.ask = True
    assert editor.ask_docked and not session.drawer.open
    editor.editable = True
    assert session.drawer.open and not editor.ask_docked
    session.drawer.open = False
    assert not editor.ask
    editor.ask = True
    assert session.drawer.open
    editor.editable = False
    assert editor.ask_docked and not session.drawer.open


async def test_the_drawer_opened_in_view_mode_stays_open_as_edit_mode_starts(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    editor.param.update(editable=False, ask=False)
    # Asked from the command palette; the assistant's first edit switches to edit mode.
    session.drawer.open = True
    editor.editable = True
    assert session.drawer.open and editor.ask


async def test_the_drawer_opens_at_the_width_last_dragged_to(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    preferences = editor._split_view.preferences
    assert session.drawer.width == preferences.assistant_width == 360
    session.drawer.width = 480
    assert preferences.assistant_width == 480
    preferences.assistant_width = 400
    assert session.drawer.width == 400


async def test_review_opens_the_drawer_in_place_of_the_docked_feed(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    chrome = session.chrome
    editor.param.update(editable=False, ask=True)
    _ask_approval(session.runner.state, "run-a", "c1")
    assert editor.ask_docked and not session.drawer.open and chrome.notice.visible
    chrome.review()
    assert session.drawer.open and not chrome.notice.visible
    assert not editor.ask and not editor.ask_docked


async def test_the_drawer_and_the_docked_feed_take_turns_in_view_mode(store, monkeypatch):
    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    editor.param.update(editable=False, ask=True)
    # Opened by asking from the command palette, say.
    session.drawer.open = True
    assert not editor.ask and not editor.ask_docked
    editor.ask = True
    assert editor.ask_docked and not session.drawer.open


async def _add_from_the_dock(store, monkeypatch):
    """Dock Ask in view mode, ask the Explore agent and add its answer from the card."""
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session, editor = app._agent_session, app._editor
    feed = editor.explore_feed
    editor.param.update(editable=False, ask=True)
    assert feed.docked
    await session.explore_runner.run("How many are there?")
    run = session.explore_runner.run_id
    feed.sync()
    card = next(a for a in feed.state.artifacts if a.artifact_id == f"explore:{run}:explored")
    await feed.actions.invoke("add_to_dashboard", feed._placement(card))
    return app, card


async def test_an_answer_added_from_the_docked_feed_is_shown_in_layout_and_ask_returns_after(
    store, monkeypatch
):
    app, card = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    # Edit mode on the Layout tab with the tile marked, and no drawer holding another conversation.
    assert editor.editable and editor.surface == "workspace" and editor.mode == "dashboard"
    assert session.highlighter.marked == {"explored": "add"}
    assert not editor.ask and not session.drawer.open
    # Done brings the docked feed back, where the card says where the answer went.
    app._on_dashboard_bar_mode("view")
    assert editor.ask_docked and not session.drawer.open
    feed = editor.explore_feed
    assert feed.docked and feed._placed(card) == "On dashboard as explored"
    # Docked beside the report, Show switches to the dashboard to point the tile out.
    editor.surface = "report"
    await feed.actions.invoke("show_on_dashboard", {"name": "explored"})
    assert editor.surface == "workspace" and editor.ask_docked


async def test_ask_turned_on_and_off_after_an_add_from_the_dock_stays_off(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    editor.ask = True
    assert session.drawer.open
    session.drawer.open = False
    assert not editor.ask
    app._on_dashboard_bar_mode("view")
    assert not editor.ask and not editor.ask_docked


async def test_an_approval_waiting_as_an_add_from_the_dock_starts_edit_mode_opens_the_drawer(
    store, monkeypatch
):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session, editor = app._agent_session, app._editor
    feed = editor.explore_feed
    editor.param.update(editable=False, ask=True)
    await session.explore_runner.run("How many are there?")
    run = session.explore_runner.run_id
    _ask_approval(session.runner.state, "run-a", "c1")
    await feed.actions.invoke("add_to_dashboard", {"exploration": run, "node": "explored"})
    assert editor.editable and editor.ask and session.drawer.open


async def test_a_hidden_output_shown_from_the_docked_feed_sets_ask_aside_too(store, monkeypatch):
    from panel_flowdash.turns import agent_edits
    from panel_flowdash.ui import tile_layout

    app, _ = _open(store, monkeypatch, agent=_adding("drawn"), explore_agent=_adding("explored"))
    session, editor = app._agent_session, app._editor
    with agent_edits(run="run-old", question="An old question"):
        editor.add_component("T/number", instance_id="old")
    editor.end_agent_turns()
    tile_layout.update_tile(editor, "old", visible=False)
    editor.param.update(editable=False, ask=True)
    await editor.explore_feed.actions.invoke("add_to_dashboard", {"name": "old"})
    assert editor.editable and editor.mode == "dashboard"
    assert not editor.ask and not session.drawer.open
    app._on_dashboard_bar_mode("view")
    assert editor.ask_docked
