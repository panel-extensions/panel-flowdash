"""Outlining what the assistant's edits added or changed."""

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.diff import dashboard_changes
from panel_flowdash.agent.highlight import GRID_CSS, TILE_CLASSES, changed_nodes
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from .test_agent_tools import _app, scripted

BEFORE = """
flowdash: 1
nodes:
  a: {component: T/number}
  b: {component: T/doubler, config: {factor: 1}}
  c: {component: T/doubler}
edges:
  - {from: a.value, to: b.value}
"""

AFTER = """
flowdash: 1
nodes:
  a: {component: T/number}
  b: {component: T/doubler, config: {factor: 2}}
  d: {component: T/doubler}
edges:
  - {from: a.value, to: d.value}
"""


def test_changed_nodes_marks_additions_config_and_rewired_targets():
    marks = changed_nodes(dashboard_changes(BEFORE, AFTER))
    assert marks == {"d": "add", "b": "change"}


async def test_an_agent_edit_outlines_the_new_node_until_the_next_run(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(
        scripted(("add_node", {"component": "T/number"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id)
    session = app._agent_session

    await session.runner.run("Add a number")

    ((instance_id, kind),) = session.highlighter.marked.items()
    assert kind == "add"
    node = next(n for n in app._editor._flow.nodes if n["id"] == instance_id)
    assert node["style"]["borderColor"].startswith("var(--mui-palette-success-main")
    app._editor._rebuild_tile_grid()
    tile = app._editor._inspect_tiles.get(instance_id)
    assert tile is not None and TILE_CLASSES["add"] in tile.css_classes
    assert GRID_CSS in app._editor._tile_grid.stylesheets

    session.runner.state.running = True
    assert session.highlighter.marked == {}
    assert TILE_CLASSES["add"] not in tile.css_classes
    node = next(n for n in app._editor._flow.nodes if n["id"] == instance_id)
    # Only the card's handle positions are left.
    assert all(key.startswith("--fd-port-") for key in node.get("style") or {})


async def test_leaving_edit_mode_clears_the_outlines(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(
        scripted(("add_node", {"component": "T/number"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id)
    session = app._agent_session

    await session.runner.run("Add a number")

    assert app._editor.editable
    ((instance_id, _),) = session.highlighter.marked.items()
    app._editor._rebuild_tile_grid()
    tile = app._editor._inspect_tiles[instance_id]
    assert TILE_CLASSES["add"] in tile.css_classes

    app._editor.editable = False

    assert session.highlighter.marked == {}
    assert TILE_CLASSES["add"] not in tile.css_classes
    node = next(n for n in app._editor._flow.nodes if n["id"] == instance_id)
    # Only the card's handle positions are left.
    assert all(key.startswith("--fd-port-") for key in node.get("style") or {})
    app._editor._rebuild_tile_grid()
    tiles = app._editor._inspect_tiles.values()
    assert not any(set(TILE_CLASSES.values()) & set(t.css_classes) for t in tiles)


async def test_discarding_the_changes_clears_the_outlines(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")

    def node_name(results):
        return {"name": app._agent_session.ops._names()[instance_id], "width": 50}

    agent = Agent(
        scripted(("place_tile", node_name)),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    editor = app._editor
    instance_id = editor.add_component("T/number")
    assert editor._on_save_clicked()
    session = app._agent_session

    await session.runner.run("Make the number tile wider")

    assert session.highlighter.marked == {instance_id: "change"}
    editor._dashboard_bar.discard()

    assert editor.editable
    assert session.highlighter.marked == {}
    node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
    # Only the card's handle positions are left.
    assert all(key.startswith("--fd-port-") for key in node.get("style") or {})
    editor._rebuild_tile_grid()
    tile = editor._inspect_tiles[instance_id]
    assert not set(TILE_CLASSES.values()) & set(tile.css_classes)


async def test_the_users_own_edits_during_a_run_are_not_outlined(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")

    def user_adds_a_node_meanwhile(results):
        app._editor.add_component("T/scaled")
        return {"component": "T/doubler"}

    agent = Agent(
        scripted(
            ("add_node", {"component": "T/number"}),
            ("add_node", user_adds_a_node_meanwhile),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    session = app._agent_session

    await session.runner.run("Add a number and a doubler")

    components = {i["instance_id"]: i["component_id"] for i in app._editor._tile_items}
    marked = {components[iid]: kind for iid, kind in session.highlighter.marked.items()}
    assert marked == {"T/number": "add", "T/doubler": "add"}


async def test_the_canvas_is_sent_again_only_when_the_marks_change(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(
        scripted(
            ("add_node", {"component": "T/number", "name": "a"}),
            ("add_node", {"component": "T/scaled", "name": "b"}),
            ("set_config", {"name": "b", "config": {"factor": 3}}),
            ("set_config", {"name": "a", "config": {}}),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    session = app._agent_session
    resyncs = []
    original = session.highlighter._resync_canvas
    monkeypatch.setattr(
        session.highlighter, "_resync_canvas", lambda ed: (resyncs.append(1), original(ed))
    )

    await session.runner.run("Add a number and a scaled number")

    # One per node added; editing a node already outlined as added changes nothing.
    assert len(resyncs) == 2
    assert sorted(session.highlighter.marked.values()) == ["add", "add"]
    for instance_id in session.highlighter.marked:
        node = next(n for n in app._editor._flow.nodes if n["id"] == instance_id)
        assert node["style"]["borderColor"].startswith("var(--mui-palette-success-main")
