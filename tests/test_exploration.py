"""Explore's answers, built in a hidden exploration beside the dashboard, and adding one to it."""

import asyncio

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent

from panel_flowdash import FlowDash, MemoryDashboardStore, register
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
        lambda ex: ex.place_tile("inv"),
        lambda ex: ex.set_settings({}),
        lambda ex: ex.save(),
        lambda ex: ex.replace_yaml("nodes: {}"),
    ],
    ids=["config", "connect", "remove", "layout", "settings", "save", "replace"],
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


def test_a_mirror_follows_its_node_again_once_it_is_back(explore):
    ops, ex = explore
    ex.add_node("Demo/inverse", name="back")
    ex.connect("inv.inverse", "back.value")
    model = ops.editor.to_model()
    ops.remove_node("inv")
    ex.sync()
    exploration = ex.exploration()
    assert exploration.mirrors["inv"].status == REMOVED
    # Undone, e.g. by the history, which loads the dashboard as it was.
    ops.editor.load_model(model)
    ex.sync()
    assert exploration.mirrors["inv"].status == "live"
    assert {(e["source"], e["target"]) for e in ex.editor.graph.edges} == {
        ("base", "inv"),
        ("inv", "back"),
    }
    ops.editor.graph.get_state("base").value = 4
    assert ex.editor.graph.get_state("back").inverse == 4


def test_a_replaced_node_is_mirrored_apart_from_the_frozen_copy(explore):
    ops, ex = explore
    ex.add_node("Demo/inverse", name="back")
    ex.connect("inv.inverse", "back.value")
    ops.remove_node("inv")
    ops.add_node("T/doubler", name="inv")
    ops.connect("base.value", "inv.value")
    ex.sync()
    mirrors = ex.exploration().mirrors
    assert mirrors["inv"].status == "replaced"
    assert (mirrors["dashboard_inv"].source, mirrors["dashboard_inv"].status) == ("inv", "live")
    assert "dashboard_inv" in [n["name"] for n in ex.nodes()]
    assert ex.node_output("dashboard_inv.doubled") == 42
    # The replacement undone: the original follows again and the other mirror stops.
    ops.remove_node("inv")
    ops.add_node("Demo/inverse", name="inv")
    ops.connect("base.value", "inv.value")
    ex.sync()
    assert mirrors["inv"].status == "live" and mirrors["dashboard_inv"].status == "replaced"


def test_a_dashboard_node_named_like_an_answer_is_mirrored_under_another_name(explore):
    ops, ex = explore
    ex.add_node("T/doubler", name="twice")
    ex.connect("base.value", "twice.value")
    ops.add_node("T/doubler", name="twice")
    ops.connect("base.value", "twice.value")
    ex.sync()
    assert "dashboard_twice" in [n["name"] for n in ex.nodes()]
    assert ex.exploration().mirrors["dashboard_twice"].source == "twice"
    assert not ex.exploration().is_mirror("twice")


async def test_an_answer_the_dashboard_shows_already_adds_nothing(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    editor = app._editor
    session.ops.add_node("T/doubler", name="dbl")
    session.ops.connect("base.value", "dbl.value")
    session.ops.place_tile("dbl", visible=True)
    editor.end_turn()
    turns = list(editor.turns)
    await session.explore_runner.run("Double it")
    run = session.explore_runner.run_id
    (artifact,) = session.runner.state.artifacts
    versions = len(artifact.versions)
    feed = editor.explore_feed
    feed.shown = True
    card = next(a for a in feed.state.artifacts if a.artifact_id == f"explore:{run}:twice")
    await feed.actions.invoke("add_to_dashboard", feed._placement(card))
    assert _nodes(editor) == ["base", "dbl"] and editor.turns == turns
    assert len(artifact.versions) == versions
    assert feed._placed(card) == "Already on dashboard as dbl"
    assert feed._showable(card) == {"name": "dbl"}


async def test_an_answer_the_dashboard_has_but_hides_is_shown_as_one_turn(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    editor = app._editor
    session.ops.add_node("T/doubler", name="dbl")
    session.ops.connect("base.value", "dbl.value")
    session.ops.place_tile("dbl", visible=False)
    editor.end_turn()
    await session.explore_runner.run("Double it")
    run = session.explore_runner.run_id
    assert await session.add_answer(run, "twice") == "dbl"
    assert _nodes(editor) == ["base", "dbl"]
    assert "dbl" in [i["instance_id"] for i in tile_layout.grid_tiles(editor)]
    assert editor.turns[-1].run == run


async def test_adding_twice_at_once_adds_once(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    await session.explore_runner.run("Double it")
    run = session.explore_runner.run_id
    first, second = await asyncio.gather(
        session.add_answer(run, "twice"), session.add_answer(run, "twice")
    )
    assert first == second == "twice"
    assert _nodes(app._editor) == ["base", "twice"]
    assert len(app._editor.turns) == 1
    # Adding it again later changes nothing either.
    assert await session.add_answer(run, "twice") == "twice"
    assert len(app._editor.turns) == 1


async def test_a_failed_add_takes_back_what_it_wrote(store, monkeypatch):
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
    run = session.explore_runner.run_id
    connect = session.ops._connect

    def failing(source, target, transform=None):
        if target.startswith("quad"):
            raise RuntimeError("The connection failed")
        return connect(source, target, transform)

    monkeypatch.setattr(session.ops, "_connect", failing)
    with pytest.raises(RuntimeError, match="connection failed"):
        await session.add_answer(run, "quad")
    assert _nodes(editor) == ["base"]
    assert {(e["source"], e["target"]) for e in editor.graph.edges} == set()
    versions = [
        v for a in session.runner.state.artifacts for v in a.versions if v.actor == "agent"
    ]
    assert versions == [] and editor.turns == []
    assert session.edit_lock._holders == {}
    monkeypatch.setattr(session.ops, "_connect", connect)
    assert await session.add_answer(run, "quad") == "quad"
    assert _nodes(editor) == ["base", "twice", "quad"]


async def test_only_the_latest_answers_stay_live(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    session.explorations.MAX_RUNS = 1
    runner = session.explore_runner
    await runner.run("Double it")
    first = runner.run_id
    runner.agent = _exploring("again")
    runner.messages = []
    await runner.run("Double it again")
    exploration = session.explorations.exploration()
    assert exploration.made_by(first) == [] and exploration.leaves(runner.run_id) == ["again"]


async def test_opening_another_dashboard_drops_the_exploration(store, monkeypatch):
    app, model = _open(store, monkeypatch, explore_agent=_exploring("twice"))
    session = app._agent_session
    await session.explore_runner.run("Double it")
    exploration = session.explorations.exploration()
    state = app._editor.graph.get_state("base")
    other = store.create_dashboard("alice", "Other")
    app._load_dashboard_sync(other.dashboard_id)
    assert model.dashboard_id not in session.explorations.explorations()
    assert exploration.editor._tile_items == []
    assert not state.param.watchers.get("value", {}).get("value")


def _with_sources(store, monkeypatch, *calls):
    from .test_agent_tools import WITH_FETCH

    return _open(store, monkeypatch, explore_agent=_agent(*calls), components=WITH_FETCH)


async def test_explore_adds_a_source_the_host_offers_as_it_is(store, monkeypatch):
    app, _ = _with_sources(
        store,
        monkeypatch,
        ("add_node", {"component": "P/srca", "name": "srca"}),
        ("add_node", {"component": "T/query", "name": "rows"}),
        ("connect", {"source": "srca.text", "target": "rows.table"}),
    )
    session = app._agent_session
    editor = app._editor
    runner = session.explore_runner
    await runner.run("Query source A")
    run = runner.run_id
    exploration = session.explorations.exploration()
    assert exploration.leaves(run) == ["rows"]
    assert _nodes(editor) == ["base"]
    assert await session.add_answer(run, "rows") == "rows"
    assert _nodes(editor) == ["base", "srca", "rows"]
    # Now on the dashboard, the source is not added again: the agent is told to build on it.
    # Its mirror takes another name, since the exploration's own node keeps "srca".
    runner.agent = _agent(("add_node", {"component": "P/srca"}))
    runner.messages = []
    await runner.run("Again")
    assert "The dashboard's node 'dashboard_srca' is this source already" in str(
        runner.state.tool_calls["call-1"].result
    )


async def test_explore_cannot_add_a_source_with_config_of_its_own(store, monkeypatch):
    app, _ = _with_sources(
        store, monkeypatch, ("add_node", {"component": "P/srca", "config": {"value": "x"}})
    )
    runner = app._agent_session.explore_runner
    await runner.run("Load something else")
    assert str(runner.state.tool_calls["call-1"].result).startswith(
        "Refused: 'Source A' with this"
    )
    assert app._agent_session.explorations.exploration().made_by(runner.run_id) == []


async def test_a_source_the_dashboard_has_is_not_loaded_again(store, monkeypatch):
    app, _ = _with_sources(store, monkeypatch, ("add_node", {"component": "T/number"}))
    runner = app._agent_session.explore_runner
    await runner.run("Load the number")
    assert "The dashboard's node 'base' is this source already" in str(
        runner.state.tool_calls["call-1"].result
    )


async def test_an_answer_follows_a_dashboard_node_brought_back_by_undo(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("twice", source="mid.doubled"))
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
    exploration = session.explorations.exploration()
    assert exploration.mirrors["mid"].status == REMOVED
    assert editor.undo()
    feed.sync()
    assert exploration.mirrors["mid"].status == "live"
    assert exploration.changed_since(run, "twice") == []
    editor.graph.get_state("base").value = 5
    assert exploration.editor.graph.get_state("twice").doubled == 20
    # Added now, it reads the dashboard's node rather than a frozen copy.
    await session.add_answer(run, "twice")
    assert _nodes(editor) == ["base", "mid", "twice"]
    assert ("mid", "twice") in {(e["source"], e["target"]) for e in editor.graph.edges}


async def test_opening_another_dashboard_during_an_add_leaves_it_alone(store, monkeypatch):
    app, model = _open(
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
    run = session.explore_runner.run_id
    other = store.create_dashboard("alice", "Other")
    other.items.append(DashboardItem("twice", "T/number"))
    store.save_dashboard(other)
    place = session.ops._place_node

    def switching(spec, config, position, name):
        added = place(spec, config, position, name)
        if added == "quad":
            # As if the person opened another dashboard between two writes.
            editor.load_model(store._load_any(other.dashboard_id))
        return added

    monkeypatch.setattr(session.ops, "_place_node", switching)
    with pytest.raises(Exception, match="Another dashboard was opened"):
        await session.add_answer(run, "quad")
    assert editor.dashboard.dashboard_id == other.dashboard_id
    assert _nodes(editor) == ["twice"] and editor.graph.get_state("twice").value == 21
    assert [i.instance_id for i in store._load_any(model.dashboard_id).items] == ["base"]
    assert session.edit_lock._holders == {}


async def test_a_pruned_answer_keeps_a_card_and_an_added_one_its_place(store, monkeypatch):
    app, _ = _open(store, monkeypatch, explore_agent=_exploring("first"))
    session = app._agent_session
    session.explorations.MAX_RUNS = 1
    runner = session.explore_runner
    await runner.run("One")
    first = runner.run_id
    runner.agent = _exploring("second")
    runner.messages = []
    await runner.run("Two")
    second = runner.run_id
    runner.agent = _exploring("third")
    runner.messages = []
    await session.add_answer(second, "second")
    await runner.run("Three")
    feed = app._editor.explore_feed
    feed.shown = True
    cells = {a.artifact_id: a for a in feed.state.artifacts}
    pruned, added = cells[f"explore:{first}:first"], cells[f"explore:{second}:second"]
    text = feed._render(pruned).object
    assert "no longer live; ask the question again" in text and text.endswith("One")
    assert feed._placement(pruned) is None
    assert feed._placed(added) == "On dashboard as second"
    assert feed._showable(added) == {"name": "second"}
    from panel_flowdash.agent import OperationError

    with pytest.raises(OperationError, match="no longer available"):
        await session.add_answer(first, "first")


def _sources_dashboard(store, monkeypatch, configs, explore_agent):
    from .test_agent_tools import WITH_FETCH

    model = store.create_dashboard("alice", "Live")
    for name, config in configs.items():
        model.items.append(DashboardItem(name, "P/srca", config=config))
    store.save_dashboard(model)
    app = _app(
        store, monkeypatch, agent=_agent(), explore_agent=explore_agent, components=WITH_FETCH
    )
    app._load_dashboard_sync(model.dashboard_id)
    return app


_QUERY_A = (
    ("add_node", {"component": "P/srca", "name": "source_a"}),
    ("add_node", {"component": "T/query", "name": "rows"}),
    ("connect", {"source": "source_a.text", "target": "rows.table"}),
)


async def test_a_source_the_dashboard_has_with_the_same_config_is_reused(store, monkeypatch):
    app = _sources_dashboard(
        store, monkeypatch, {"custom": {"value": "x"}, "plain": {}}, _agent(*_QUERY_A[:1])
    )
    runner = app._agent_session.explore_runner
    await runner.run("Load A")
    # Of the two, the one with the same config is named.
    assert "The dashboard's node 'plain' is this source already" in str(
        runner.state.tool_calls["call-1"].result
    )


async def test_a_source_with_other_config_on_the_dashboard_is_added_apart(store, monkeypatch):
    app = _sources_dashboard(store, monkeypatch, {"srca": {"value": "x"}}, _agent(*_QUERY_A))
    session = app._agent_session
    editor = app._editor
    await session.explore_runner.run("Query A")
    run = session.explore_runner.run_id
    exploration = session.explorations.exploration()
    # The answer reads its own Source A, with the default config, not the dashboard's.
    assert exploration.leaves(run) == ["rows"]
    assert ("source_a", "rows") in {
        (e["source"], e["target"]) for e in exploration.editor.graph.edges
    }
    assert await session.add_answer(run, "rows") == "rows"
    assert _nodes(editor) == ["srca", "source_a", "rows"]
    assert ("source_a", "rows") in {(e["source"], e["target"]) for e in editor.graph.edges}
    assert next(i for i in editor._tile_items if i["instance_id"] == "source_a")["config"] == {}


async def test_adding_reuses_the_dashboards_source_with_the_same_config(store, monkeypatch):
    app = _sources_dashboard(store, monkeypatch, {"custom": {"value": "x"}}, _agent(*_QUERY_A))
    session = app._agent_session
    editor = app._editor
    await session.explore_runner.run("Query A")
    run = session.explore_runner.run_id
    # The dashboard gets Source A as offered after the answer was built.
    session.ops.add_node("P/srca", name="plain")
    editor.end_turn()
    assert await session.add_answer(run, "rows") == "rows"
    assert _nodes(editor) == ["custom", "plain", "rows"]
    assert ("plain", "rows") in {(e["source"], e["target"]) for e in editor.graph.edges}


async def test_a_source_nothing_reads_is_not_an_answer(store, monkeypatch):
    app, _ = _with_sources(store, monkeypatch, ("add_node", {"component": "P/srca"}))
    runner = app._agent_session.explore_runner
    await runner.run("Load A")
    exploration = app._agent_session.explorations.exploration()
    assert len(exploration.made_by(runner.run_id)) == 1
    assert exploration.leaves(runner.run_id) == []


def _slow_check(config, inputs):
    import time

    time.sleep(0.2)
    return []


@register(
    page=False,
    component=True,
    title="Slow",
    provides=[{"key": "doubled", "type": "int"}],
    requires=[{"key": "value", "type": "int", "required": True}],
    check_config=_slow_check,
)
def slow(config):
    def update(*_):
        config.doubled = None if config.value is None else config.value * 2

    config.param.watch(update, "value")
    update()
    return "slow"


async def test_adding_runs_the_config_checks_off_the_event_loop(store, monkeypatch):
    import time

    calls = [("add_node", {"component": "T/slow", "name": "s0"})]
    calls.append(("connect", {"source": "base.value", "target": "s0.value"}))
    for i in range(1, 4):
        calls.append(("add_node", {"component": "T/slow", "name": f"s{i}"}))
        calls.append(("connect", {"source": f"s{i - 1}.doubled", "target": f"s{i}.value"}))
    app, _ = _open(
        store,
        monkeypatch,
        explore_agent=_agent(*calls),
        components={**COMPONENTS, "T/slow": slow},
    )
    session = app._agent_session
    await session.explore_runner.run("Slowly")
    run = session.explore_runner.run_id
    gaps, done = [], asyncio.Event()

    async def tick():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.ensure_future(tick())
    assert await session.add_answer(run, "s3") == "s3"
    done.set()
    await ticker
    assert _nodes(app._editor) == ["base", "s0", "s1", "s2", "s3"]
    # Four checks of 0.2 s ran, but the loop never waited on one.
    assert max(gaps) < 0.15, max(gaps)
