"""Explore's answers, built in a hidden exploration beside the dashboard, and adding one to it."""

import asyncio

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent

from panel_flowdash import FlowDash, MemoryDashboardStore
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.exploration import ADD_LABEL, REMOVED, ExplorationOps
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.dashboard_store import DashboardItem
from panel_flowdash.ui import tile_layout

from .test_agent_ops import COMPONENTS
from .test_agent_tools import _app, scripted
from .test_editor import Inverse


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)
    monkeypatch.delenv("FLOWDASH_AGENT_MODELS", raising=False)


@pytest.fixture
def store():
    return MemoryDashboardStore()


def _agent(*calls):
    return Agent(scripted(*calls), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


def _exploring(name, source="base.value"):
    """An Explore agent doubling *source* in a node *name*."""
    return _agent(
        ("add_node", {"component": "T/doubler", "name": name}),
        ("connect", {"source": source, "target": f"{name}.value"}),
    )


def _open(store, monkeypatch, explore_agent, agent=None, **params):
    """Open a dashboard with a number ``base`` on it, made before any request."""
    model = store.create_dashboard("alice", "Live")
    model.items.append(DashboardItem("base", "T/number"))
    store.save_dashboard(model)
    app = _app(store, monkeypatch, agent=agent or _agent(), explore_agent=explore_agent, **params)
    app._load_dashboard_sync(model.dashboard_id)
    return app, model


def _nodes(editor):
    return [i["instance_id"] for i in editor._tile_items]


@pytest.fixture
def explore(request):
    """Operations on a dashboard with a number feeding an inverse, and on its exploration.

    Failures are carried along edges in tests marked ``failures``.
    """
    editor = FlowDash(
        components={**COMPONENTS, "Demo/inverse": Inverse},
        include_builtin_components=False,
        notifications=False,
        user="alice",
        propagate_failures=request.node.get_closest_marker("failures") is not None,
    )
    editor.new_dashboard("Mine")
    ops = DashboardOps(editor)
    ops.add_node("T/number", name="base")
    ops.add_node("Demo/inverse", name="inv")
    ops.connect("base.value", "inv.value")
    yield ops, ExplorationOps(ops)
    editor.dispose()


def test_the_exploration_reads_the_dashboard_through_mirrors(explore):
    ops, ex = explore
    assert ex.nodes() == ops.nodes()
    assert ex.node_config("inv") == ops.node_config("inv")
    # The mirrors are the dashboard's nodes, but not made by any of the exploration's turns.
    theirs = yaml.safe_load(ops.get_dashboard())["nodes"]
    for node in theirs.values():
        node.pop("origin")
    assert yaml.safe_load(ex.get_dashboard())["nodes"] == theirs
    assert ex.editor is not ops.editor
    # Nothing was instantiated: the mirror's output is the dashboard node's.
    assert ex.node_output("inv.inverse") == ops.node_output("inv.inverse")


@pytest.mark.failures
def test_answers_follow_the_dashboards_values_and_failures(explore):
    ops, ex = explore
    ex.add_node("T/doubler", name="twice")
    ex.connect("base.value", "twice.value")
    ex.add_node("Demo/inverse", name="back")
    ex.connect("inv.inverse", "back.value")
    graph = ex.editor.graph
    assert graph.get_state("twice").doubled == 42
    ops.editor.graph.get_state("base").value = 4
    assert graph.get_state("twice").doubled == 8
    assert graph.get_state("back").inverse == 4
    # A failure on the dashboard reaches the answers built on it, and they recover with it.
    ops.editor.graph.get_state("base").value = 0
    report = ex.preview()["nodes"]
    assert report["inv"]["status"] == "failed" and report["back"]["status"] == "failed"
    ops.editor.graph.get_state("base").value = 2
    assert ex.preview()["nodes"]["back"]["status"] == "ok"
    assert graph.get_state("back").inverse == 2
    # The dashboard has none of it.
    assert _nodes(ops.editor) == ["base", "inv"]


def test_a_removed_dashboard_node_leaves_a_frozen_mirror(explore):
    ops, ex = explore
    ex.add_node("T/doubler", name="twice")
    ex.connect("base.value", "twice.value")
    ops.editor.graph.get_state("base").value = 5
    state = ops.editor.graph.get_state("base")
    ops.remove_node("base")
    ex.sync()
    exploration = ex.exploration()
    assert exploration.mirrors["base"].status == REMOVED
    assert not state.param.watchers.get("value", {}).get("value")
    state.value = 6
    assert ex.editor.graph.get_state("twice").doubled == 10


def test_nodes_and_edges_added_to_the_dashboard_later_are_mirrored(explore):
    ops, ex = explore
    ex.exploration()
    ops.add_node("T/number", name="other")
    ops.add_node("T/doubler", name="dbl")
    ops.connect("other.value", "dbl.value")
    ex.sync()
    assert [n["name"] for n in ex.nodes()] == ["base", "inv", "other", "dbl"]
    edges = {(e["source"], e["target"]) for e in ex.editor.graph.edges}
    assert edges == {("base", "inv"), ("other", "dbl")}
    assert ex.editor.graph.get_state("dbl").doubled == 42


def test_a_reloaded_dashboard_is_followed_again(explore):
    ops, ex = explore
    ex.add_node("T/doubler", name="twice")
    ex.connect("base.value", "twice.value")
    ops.editor.load_model(ops.editor.to_model())
    ex.sync()
    ops.editor.graph.get_state("base").value = 7
    assert ex.editor.graph.get_state("twice").doubled == 14


def test_disposing_lets_go_of_the_dashboard(explore):
    ops, ex = explore
    state = ops.editor.graph.get_state("base")
    watchers = lambda: len(state.param.watchers.get("value", {}).get("value", []))
    ex.exploration()
    before = watchers()
    ex.dispose()
    assert watchers() == before - 1


@pytest.mark.parametrize(
    "call",
    [
        lambda ex: ex.set_config("base", {}),
        lambda ex: ex.connect("base.value", "inv.value"),
        lambda ex: ex.remove_node("inv"),
        lambda ex: ex.add_node("T/number"),
        lambda ex: ex.place_tile("inv"),
        lambda ex: ex.set_settings({}),
        lambda ex: ex.save(),
        lambda ex: ex.replace_yaml("nodes: {}"),
    ],
    ids=["config", "connect", "remove", "source", "layout", "settings", "save", "replace"],
)
def test_explore_cannot_change_the_dashboard(explore, call):
    ops, ex = explore
    before = ops.get_dashboard()
    with pytest.raises(PermissionError):
        call(ex)
    assert ops.get_dashboard() == before


def test_drafts_are_not_previewed(explore):
    from panel_flowdash.agent import OperationError

    _, ex = explore
    with pytest.raises(OperationError, match="without YAML"):
        ex.preview("nodes: {}")


@pytest.mark.parametrize("freeze", [True, False])
async def test_earlier_answers_are_frozen_unless_turned_off(store, monkeypatch, freeze):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    session.explorations.freeze_answers = freeze
    runner = session.explore_runner
    await runner.run("Double it")
    runner.agent = _agent(("remove_node", {"name": "twice"}))
    runner.messages = []
    await runner.run("Drop it")
    # The scripted model numbers its calls per run, so the second run's call is "call-1" again.
    call = runner.state.tool_calls["call-1"]
    exploration = session.explorations.exploration()
    if freeze:
        assert str(call.result).startswith("Refused: 'twice' is part of an earlier answer")
        assert "twice" in _nodes(exploration.editor)
    else:
        assert "twice" not in _nodes(exploration.editor)


async def test_an_explore_run_leaves_the_dashboard_and_its_history_alone(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    editor = app._editor
    busy = []
    session.explore_runner.state.param.watch(
        lambda e: busy.append(editor.edit_history.busy), "running"
    )
    await session.explore_runner.run("Double it")
    assert busy == [False, False]
    assert _nodes(editor) == ["base"] and editor.turns == [] and not editor.dirty
    assert session.runner.state.artifacts == [] or all(
        v.actor != "agent" for a in session.runner.state.artifacts for v in a.versions
    )
    # Its turn is the exploration's: what was asked and the nodes it made.
    exploration = session.explorations.exploration()
    (turn,) = exploration.editor.turns
    assert (turn.run, turn.question) == (session.explore_runner.run_id, "Double it")
    assert exploration.leaves(turn.run) == ["twice"]
    # The tools answer with their message and publish no version of the dashboard.
    assert not session.explore_runner.state.artifacts


async def test_answers_are_cards_of_the_exploration(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    feed = app._editor.explore_feed
    feed.shown = True
    await session.explore_runner.run("Double it")
    feed.sync()
    run = session.explore_runner.run_id
    cells = {a.artifact_id: a for a in feed.state.artifacts}
    card = cells[f"explore:{run}:twice"]
    assert card.value == {"exploration": run, "node": "twice"}
    assert card.title == "twice"
    value = feed._render(card).objects[1]
    assert value.objects[0].object == 42
    # The card follows the dashboard's data.
    app._editor.graph.get_state("base").value = 3
    assert value.objects[0].object == 6
    assert feed._placement(card) == {"exploration": run, "node": "twice"}
    assert feed._placed(card) is None and feed._showable(card) is None


async def test_adding_an_answer_is_one_turn_and_one_undo(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    editor = app._editor
    feed = editor.explore_feed
    feed.shown = True
    await session.explore_runner.run("Double it")
    feed.sync()
    run = session.explore_runner.run_id
    card = next(a for a in feed.state.artifacts if a.artifact_id == f"explore:{run}:twice")
    await feed.actions.invoke("add_to_dashboard", feed._placement(card))
    assert _nodes(editor) == ["base", "twice"]
    assert editor.graph.get_state("twice").doubled == 42
    assert {(e["source"], e["target"]) for e in editor.graph.edges} == {("base", "twice")}
    assert "twice" in [i["instance_id"] for i in tile_layout.grid_tiles(editor)]
    (turn,) = editor.turns
    assert (turn.actor, turn.run, turn.question) == ("agent", run, "Double it")
    assert editor.node_origin("twice") == turn.id
    (artifact,) = session.runner.state.artifacts
    assert [v.run_id for v in artifact.versions if v.actor == "agent"] == [run]
    assert feed._placed(card) == "On dashboard as twice"
    assert feed._placement(card) is None and feed._showable(card) == {"name": "twice"}
    assert editor.undo() and _nodes(editor) == ["base"]
    feed.refresh()
    assert feed._placed(card) is None and feed._placement(card) is not None


async def test_an_answer_built_from_a_chain_is_added_with_it_and_reuses_the_dashboards_nodes(
    store, monkeypatch
):
    app, _ = _open(
        store,
        monkeypatch,
        explore_agent=_agent(
            ("add_node", {"component": "T/doubler", "name": "twice"}),
            ("connect", {"source": "base.value", "target": "twice.value"}),
            ("add_node", {"component": "T/doubler", "name": "quad"}),
            ("connect", {"source": "twice.doubled", "target": "quad.value"}),
        ),
    )
    session = app._agent_session
    editor = app._editor
    await session.explore_runner.run("Quadruple it")
    # The dashboard gets a node like the chain's first, and a node named like its answer.
    ops = session.ops
    ops.add_node("T/doubler", name="dbl")
    ops.connect("base.value", "dbl.value")
    ops.add_node("T/scaled", name="quad")
    editor.end_turn()
    run = session.explore_runner.run_id
    exploration = session.explorations.exploration()
    assert exploration.leaves(run) == ["quad"]
    name = await session.add_answer(run, "quad")
    assert name == "quad_2"
    assert _nodes(editor) == ["base", "dbl", "quad", "quad_2"]
    assert ("dbl", "quad_2") in {(e["source"], e["target"]) for e in editor.graph.edges}
    assert editor.graph.get_state("quad_2").doubled == 84
    grid = [i["instance_id"] for i in tile_layout.grid_tiles(editor)]
    assert "quad_2" in grid
    assert editor.undo() and _nodes(editor) == ["base", "dbl", "quad"]


async def test_an_answer_on_a_removed_dashboard_node_brings_its_last_copy(store, monkeypatch):
    app, _ = _open(
        store,
        monkeypatch,
        explore_agent=_exploring("twice", source="mid.doubled"),
    )
    session = app._agent_session
    editor = app._editor
    session.ops.add_node("T/doubler", name="mid")
    session.ops.connect("base.value", "mid.value")
    editor.end_turn()
    await session.explore_runner.run("Double it again")
    run = session.explore_runner.run_id
    session.ops.remove_node("mid")
    editor.end_turn()
    feed = editor.explore_feed
    feed.shown = True
    card = next(a for a in feed.state.artifacts if a.artifact_id == f"explore:{run}:twice")
    note = feed._render(card).objects[0]
    assert note.visible and "mid was removed from the dashboard" in note.object
    await session.add_answer(run, "twice")
    assert _nodes(editor) == ["base", "mid", "twice"]
    assert {(e["source"], e["target"]) for e in editor.graph.edges} == {
        ("base", "mid"),
        ("mid", "twice"),
    }
    grid = [i["instance_id"] for i in tile_layout.grid_tiles(editor)]
    assert "twice" in grid and "mid" not in grid


@pytest.mark.parametrize("ending", ["finished", "cancelled", "failed"])
async def test_adding_waits_for_the_assistants_edits(store, monkeypatch, ending):
    from .test_explore_runner import _gated, _until

    a2, a_end = asyncio.Event(), asyncio.Event()
    second = {"finished": "a2", "cancelled": "a2", "failed": "FAIL"}[ending]
    app, _ = _open(
        store,
        monkeypatch,
        explore_agent=_exploring("twice"),
        agent=_gated([("a1", None), (second, a2)], end=a_end),
    )
    session = app._agent_session
    editor = app._editor
    feed = editor.explore_feed
    await session.explore_runner.run("Double it")
    run = session.explore_runner.run_id
    drawer = asyncio.ensure_future(session.runner.run("A please"))
    await _until(lambda: "a1" in _nodes(editor), drawer)
    adding = asyncio.ensure_future(session.add_answer(run, "twice"))
    await _until(lambda: feed._waiting.visible, adding)
    assert feed._waiting.object == "Waiting for the assistant's edit to finish…"
    assert _nodes(editor) == ["base", "a1"]
    if ending == "cancelled":
        drawer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drawer
    else:
        a2.set()
        a_end.set()
        await drawer
    assert await adding == "twice"
    assert not feed._waiting.visible
    a_nodes = ["a1", "a2"] if ending == "finished" else ["a1"]
    assert _nodes(editor) == ["base", *a_nodes, "twice"]
    a_run = session.runner.run_id
    assert [(t.run, t.question) for t in editor.turns] == [(a_run, "A please"), (run, "Double it")]
    assert editor.undo() and _nodes(editor) == ["base", *a_nodes]
    assert editor.undo() and _nodes(editor) == ["base"]


async def test_the_drawer_says_when_it_waits_for_an_answer_being_added(store, monkeypatch):
    from .test_explore_runner import _gated, _until

    app, model = _open(
        store, monkeypatch, explore_agent=_exploring("twice"), agent=_gated([("a1", None)])
    )
    session = app._agent_session
    await session.edit_lock.acquire(model.dashboard_id, "add-x", ADD_LABEL)
    drawer = asyncio.ensure_future(session.runner.run("A please"))
    await _until(lambda: session.wait_notice.visible, drawer)
    assert session.wait_notice.objects[0].object == f"Waiting for {ADD_LABEL} to finish…"
    session.edit_lock.release("add-x")
    await drawer
    assert not session.wait_notice.visible and _nodes(app._editor) == ["base", "a1"]


async def test_adding_while_the_assistant_edits_too_long_is_refused(store, monkeypatch):
    from .test_explore_runner import _gated, _until

    a_end = asyncio.Event()
    app, _ = _open(
        store,
        monkeypatch,
        explore_agent=_exploring("twice"),
        agent=_gated([("a1", None)], end=a_end),
    )
    session = app._agent_session
    session.edit_lock.timeout = 0.2
    await session.explore_runner.run("Double it")
    run = session.explore_runner.run_id
    drawer = asyncio.ensure_future(session.runner.run("A please"))
    await _until(lambda: "a1" in _nodes(app._editor), drawer)
    with pytest.raises(PermissionError, match="busy with the assistant's edit"):
        await session.add_answer(run, "twice")
    a_end.set()
    await drawer
    assert await session.add_answer(run, "twice") == "twice"
    assert session.edit_lock._holders == {}


async def test_show_leaves_explore_and_marks_the_tile(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    editor = app._editor
    editor.explore = True
    await session.explore_runner.run("Double it")
    await session.add_answer(session.explore_runner.run_id, "twice")
    shown = []
    monkeypatch.setattr(session.highlighter, "show", shown.append)
    await editor.explore_feed.actions.invoke("show_on_dashboard", {"name": "twice"})
    # Adding switched to edit mode; the tiles are shown rather than the canvas.
    assert not editor.explore and editor.editable and editor.mode == "dashboard"
    assert shown == [{"twice": "add"}]
