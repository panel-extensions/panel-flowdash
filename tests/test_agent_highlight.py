"""Outlining what the assistant's edits added or changed."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.diff import dashboard_changes
from panel_flowdash.agent.highlight import TILE_CLASSES, changed_nodes
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

    session.runner.state.running = True
    assert session.highlighter.marked == {}
    assert TILE_CLASSES["add"] not in tile.css_classes
    node = next(n for n in app._editor._flow.nodes if n["id"] == instance_id)
    assert "style" not in node
