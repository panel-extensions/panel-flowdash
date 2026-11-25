"""Adding an answer from Ask in view mode hands it to the assistant in edit mode."""

import asyncio

import pytest

pytest.importorskip("pydantic_ai")

from panel_agents import AgentChat
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.highlight import TILE_CLASSES

from .test_exploration import _agent, _exploring, _open as _seeded
from .test_explore_runner import _adding, _gated, _open, _until

needs_mentions = pytest.mark.skipif(
    "mentions" not in AgentChat.param, reason="panel-agents has no mentions yet"
)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)
    monkeypatch.delenv("FLOWDASH_AGENT_MODELS", raising=False)


@pytest.fixture
def store():
    return MemoryDashboardStore()


def _nodes(editor):
    return [i["instance_id"] for i in editor._tile_items]


def _mentions(session):
    return [m.label for m in getattr(session.drawer.chat, "mentions", [])]


async def _ask(app, **view):
    """Ask the Explore agent in view mode, with *view* set on the editor, and return the answer's card."""
    session, editor = app._agent_session, app._editor
    feed = editor.explore_feed
    editor.param.update(editable=False, **view)
    feed.shown = True
    await session.explore_runner.run("How many are there?")
    run = session.explore_runner.run_id
    feed.sync()
    return next(a for a in feed.state.artifacts if a.artifact_id == f"explore:{run}:explored")


async def _add(app, card):
    feed = app._editor.explore_feed
    await feed.actions.invoke("add_to_dashboard", feed._placement(card))


async def _add_from_the_dock(store, monkeypatch, **params):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored"), **params
    )
    card = await _ask(app, ask=True)
    assert app._editor.explore_feed.docked
    await _add(app, card)
    return app, card


async def test_an_answer_added_from_the_dock_is_handed_to_the_assistant_beside_its_tile(
    store, monkeypatch
):
    app, card = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    handover = session.handover
    # Edit mode on the Layout tab, with the new tile marked and the assistant open beside it.
    assert editor.editable and editor.surface == "workspace" and editor.mode == "dashboard"
    assert session.highlighter.marked == {"explored": "add"}
    assert editor.ask and session.drawer.open and session.drawer.tab == "chat"
    assert handover.notice.visible and handover.name == "explored"
    assert handover._text.object == "Added explored to the dashboard"
    assert handover.undo_button.visible and not handover.assistant_button.visible
    # Done brings the docked feed back, where the card says where the answer went.
    app._on_dashboard_bar_mode("view")
    assert editor.ask_docked and not session.drawer.open and not handover.notice.visible
    feed = editor.explore_feed
    assert feed._placed(card) == "On dashboard as explored" and feed._placement(card) is None
    # Docked beside the report, Show switches to the dashboard to point the tile out.
    editor.surface = "report"
    await feed.actions.invoke("show_on_dashboard", {"name": "explored"})
    assert editor.surface == "workspace" and editor.ask_docked and not editor.editable


@needs_mentions
async def test_the_assistant_is_handed_the_new_node_as_a_mention(store, monkeypatch):
    prompts = []

    async def stream(messages, info):
        request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
        prompts.extend(p.content for p in request.parts if isinstance(p, UserPromptPart))
        yield "Done."

    app, _ = _seeded(
        store,
        monkeypatch,
        agent=Agent(FunctionModel(stream_function=stream)),
        explore_agent=_exploring("explored"),
    )
    await _add(app, await _ask(app, ask=True))
    session = app._agent_session
    (mention,) = session.drawer.chat.mentions
    assert mention.label == "explored" and mention.written == "@explored"
    await session.drawer.chat.submit("make it a bar chart")
    assert prompts[-1].endswith("@explored make it a bar chart")
    assert _mentions(session) == []
    (question,) = [
        m.content for m in session.runner.state.items if getattr(m, "role", None) == "user"
    ]
    assert question == "@explored make it a bar chart"


async def test_undo_takes_the_add_back_and_returns_to_the_docked_feed(store, monkeypatch):
    app, card = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    assert "explored" in _nodes(editor) and editor.dirty
    session.handover.undo()
    assert _nodes(editor) == ["base"] and not editor.dirty
    assert not editor.editable and editor.ask_docked and not session.drawer.open
    assert not session.handover.notice.visible and _mentions(session) == []
    feed = editor.explore_feed
    assert feed._placed(card) is None and feed._placement(card) is not None


async def test_without_the_edit_history_no_undo_is_offered(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    editor = app._editor
    editor.history = False
    await _add(app, await _ask(app, ask=True))
    handover = app._agent_session.handover
    assert handover.notice.visible and not handover.undo_button.visible
    handover.undo()
    assert "explored" in _nodes(editor) and editor.editable


async def test_another_edit_withdraws_the_undo_and_keeps_the_mention(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    handover = session.handover
    editor.add_component("T/number", instance_id="other")
    assert not handover.notice.visible and handover.name is None
    handover.undo()
    assert "explored" in _nodes(editor) and editor.editable
    if "mentions" in AgentChat.param:
        assert _mentions(session) == ["explored"]
    # The mention goes with edit mode.
    app._on_dashboard_bar_mode("view")
    assert _mentions(session) == []


async def test_closing_the_assistant_after_a_handover_leaves_view_mode_its_own_ask(
    store, monkeypatch
):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    session.drawer.open = False
    assert not editor.ask
    # The tile and its Undo stay until the next edit.
    assert session.handover.notice.visible
    app._on_dashboard_bar_mode("view")
    assert editor.ask_docked and not session.drawer.open


async def test_a_hidden_output_shown_from_the_dock_is_handed_over_too(store, monkeypatch):
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
    assert editor.ask and session.drawer.open and session.handover.name == "old"
    app._on_dashboard_bar_mode("view")
    assert editor.ask_docked


async def test_on_a_phone_the_tile_stays_in_sight_until_the_assistant_is_asked_for(
    store, monkeypatch
):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session, editor = app._agent_session, app._editor
    editor._dashboard_bar._phone_query.phone = True
    # On a phone Ask in view mode is the Explore view itself.
    card = await _ask(app, surface="explore")
    await _add(app, card)
    handover = session.handover
    assert editor.editable and editor.surface == "workspace" and editor.mode == "dashboard"
    assert not editor.ask and not session.drawer.open
    assert handover.notice.visible and handover.assistant_button.visible
    # The drawer covers the page, its input included, so the notice makes way for it.
    handover.open_assistant()
    assert editor.ask and session.drawer.open and not handover.notice.visible
    session.drawer.open = False
    # Done goes back to the Explore view the answer was added from.
    app._on_dashboard_bar_mode("view")
    assert editor.surface == "explore"


async def test_undo_on_a_phone_returns_to_the_explore_view(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    editor = app._editor
    editor._dashboard_bar._phone_query.phone = True
    await _add(app, await _ask(app, surface="explore"))
    app._agent_session.handover.undo()
    assert not editor.editable and editor.surface == "explore" and _nodes(editor) == ["base"]


async def test_going_elsewhere_in_edit_mode_keeps_the_person_there(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    editor = app._editor
    editor._dashboard_bar._phone_query.phone = True
    await _add(app, await _ask(app, surface="explore"))
    editor.surface = "report"
    app._on_dashboard_bar_mode("view")
    assert editor.surface == "report"


async def test_an_add_in_edit_mode_hands_nothing_over(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session, editor = app._agent_session, app._editor
    card = await _ask(app, surface="explore")
    editor.editable = True
    await _add(app, card)
    assert editor.surface == "explore" and not session.handover.notice.visible
    assert _mentions(session) == []


async def test_the_notice_is_centred_over_the_page_beside_the_drawer(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session = app._agent_session
    notice = session.handover.notice
    width = session.drawer.width
    assert notice.styles["left"] == f"calc((100vw - {width}px) / 2)"
    session.drawer.open = False
    assert notice.styles["left"] == "calc((100vw - 0px) / 2)"


async def test_the_added_tile_is_scrolled_into_view(store, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "panel_flowdash.ui.settled_scroll.SettledScroll._send_msg",
        lambda self, msg: sent.append(msg),
    )
    await _add_from_the_dock(store, monkeypatch)
    assert sent == [{"css_class": TILE_CLASSES["add"], "timeout": 3000}]


async def test_a_failed_add_stays_where_it_was_asked(store, monkeypatch):
    app, _ = _seeded(
        store, monkeypatch, agent=_adding("drawn"), explore_agent=_exploring("explored")
    )
    session, editor = app._agent_session, app._editor
    editor._dashboard_bar._phone_query.phone = True
    card = await _ask(app, surface="explore")
    feed = editor.explore_feed

    async def refuse(run, node):
        assert editor.surface == "workspace" and editor.mode == "dashboard"
        raise PermissionError("Busy.")

    feed._add = refuse
    with pytest.raises(PermissionError):
        await _add(app, card)
    assert not editor.editable and editor.surface == "explore" and editor.mode == "wiring"
    assert not session.handover.notice.visible


def _scaling(name, factor):
    """An Explore agent scaling ``base`` by *factor* in a node *name*."""
    return (
        ("add_node", {"component": "T/scaled", "name": name, "config": {"factor": factor}}),
        ("connect", {"source": "base.value", "target": f"{name}.value"}),
    )


async def test_undo_keeps_the_unsaved_flag_of_another_add_made_meanwhile(store, monkeypatch):
    from panel_flowdash.agent.exploration import ExplorationOps

    app, _ = _seeded(
        store,
        monkeypatch,
        agent=_adding("drawn"),
        explore_agent=_agent(*_scaling("qa", 2), *_scaling("qb", 3)),
    )
    session, editor = app._agent_session, app._editor
    feed = editor.explore_feed
    editor.param.update(editable=False, ask=True)
    feed.shown = True
    await session.explore_runner.run("Two answers")
    run = session.explore_runner.run_id
    feed.sync()
    cards = {
        a.artifact_id.rsplit(":", 1)[1]: a
        for a in feed.state.artifacts
        if a.artifact_id.startswith(f"explore:{run}:")
    }
    gate, checks = asyncio.Event(), []
    check = ExplorationOps._check

    async def slow_check(self, exploration, plan):
        checks.append(plan)
        if len(checks) == 1:
            await gate.wait()
        return await check(self, exploration, plan)

    monkeypatch.setattr(ExplorationOps, "_check", slow_check)
    # Both adds start in view mode, with nothing unsaved.
    first = asyncio.ensure_future(_add(app, cards["qa"]))
    await _until(lambda: len(checks) == 1, first)
    second = asyncio.ensure_future(_add(app, cards["qb"]))
    await _until(lambda: feed._waiting.visible or len(checks) == 2, second)
    gate.set()
    await first
    await second
    assert set(_nodes(editor)) == {"base", "qa", "qb"}
    session.handover.undo()
    # Undo took back one add; the other is still unsaved, so edit mode stays.
    assert len(_nodes(editor)) == 2 and editor.dirty and editor.editable


async def test_undo_keeps_the_unsaved_flag_of_an_assistant_edit_the_add_waited_for(
    store, monkeypatch
):
    a2, a_end = asyncio.Event(), asyncio.Event()
    app, _ = _seeded(
        store,
        monkeypatch,
        agent=_gated([("a1", None), ("a2", a2)], end=a_end),
        explore_agent=_exploring("explored"),
    )
    session, editor = app._agent_session, app._editor
    card = await _ask(app, ask=True)
    drawer = asyncio.ensure_future(session.runner.run("A please"))
    await _until(lambda: "a1" in _nodes(editor), drawer)
    # Saved and back in view mode while the request still runs.
    assert editor._on_save_clicked()
    app._on_dashboard_bar_mode("view")
    assert not editor.editable and not editor.dirty
    editor.ask = True
    adding = asyncio.ensure_future(_add(app, card))
    await _until(lambda: editor.explore_feed._waiting.visible, adding)
    a2.set()
    await _until(lambda: "a2" in _nodes(editor), drawer)
    a_end.set()
    await drawer
    await adding
    session.handover.undo()
    assert "a2" in _nodes(editor) and "explored" not in _nodes(editor)
    assert editor.dirty and editor.editable


async def test_undo_waits_for_the_assistants_request(store, monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    from panel_flowdash.agent.handover import UNDO_BUSY
    from panel_flowdash.agent.tools import FlowDashDeps

    gate = asyncio.Event()

    async def stream(messages, info):
        await gate.wait()
        yield "Done."

    app, _ = _seeded(
        store,
        monkeypatch,
        agent=Agent(FunctionModel(stream_function=stream), deps_type=FlowDashDeps),
        explore_agent=_exploring("explored"),
    )
    session, editor = app._agent_session, app._editor
    notes = []
    monkeypatch.setattr(app, "_notify", lambda *args, **kwargs: notes.append(args))
    await _add(app, await _ask(app, ask=True))
    handover = session.handover
    run = asyncio.ensure_future(session.drawer.chat.submit("make it a bar chart"))
    await _until(lambda: session.runner.state.running, run)
    assert handover.undo_button.disabled and handover.undo_button.description == UNDO_BUSY
    handover.undo()
    assert notes == [("info", UNDO_BUSY)] and "explored" in _nodes(editor)
    assert handover.notice.visible
    gate.set()
    await run
    assert not handover.undo_button.disabled


async def test_saving_keeps_the_mention(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    mentioned = _mentions(session)
    assert editor._on_save_clicked()
    assert _mentions(session) == mentioned
    assert editor.editable


async def test_a_wait_after_leaving_the_explore_view_shows_on_the_notice(store, monkeypatch):
    a_end = asyncio.Event()
    app, _ = _seeded(
        store,
        monkeypatch,
        agent=_gated([("a1", None)], end=a_end),
        explore_agent=_exploring("explored"),
    )
    session, editor = app._agent_session, app._editor
    editor._dashboard_bar._phone_query.phone = True
    card = await _ask(app, surface="explore")
    editor.editable = True
    drawer = asyncio.ensure_future(session.runner.run("A please"))
    await _until(lambda: "a1" in _nodes(editor), drawer)
    assert editor._on_save_clicked()
    app._on_dashboard_bar_mode("view")
    editor.surface = "explore"
    adding = asyncio.ensure_future(_add(app, card))
    handover = session.handover
    await _until(lambda: handover.notice.visible, adding)
    # The tiles are on screen, so the notice says what the add waits for.
    assert editor.surface == "workspace" and handover._text.object.startswith("Waiting for")
    assert not handover.undo_button.visible
    a_end.set()
    await drawer
    await adding
    assert handover.name == "explored" and handover.undo_button.visible


async def test_undo_after_saving_leaves_the_add_unsaved_in_edit_mode(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    dashboard_id = editor.dashboard.dashboard_id
    assert editor._on_save_clicked()
    stored = [i.instance_id for i in store._load_any(dashboard_id).items]
    assert "explored" in stored and session.handover.notice.visible
    session.handover.undo()
    # The store still holds the add, so taking it back is a change to save or discard.
    assert "explored" not in _nodes(editor) and editor.dirty and editor.editable
    assert "explored" in [i.instance_id for i in store._load_any(dashboard_id).items]


async def test_discarding_ends_the_handover(store, monkeypatch):
    app, _ = await _add_from_the_dock(store, monkeypatch)
    session, editor = app._agent_session, app._editor
    editor._dashboard_bar.discard()
    handover = session.handover
    assert not handover.notice.visible and handover.name is None and _mentions(session) == []
    handover.undo()
    assert "explored" not in _nodes(editor)
