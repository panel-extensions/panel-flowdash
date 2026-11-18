"""UI test: a graph the agent's operations build shows without overlaps, from left to right."""

import itertools

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash import FlowDash
from panel_flowdash.agent import DashboardOps

from ..test_agent_ops import COMPONENTS
from ..test_editor_auto_layout import EXTRA, GRAPH
from .util import serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _overlapping(boxes):
    return [
        (a, b)
        for (a, p), (b, q) in itertools.combinations(boxes.items(), 2)
        if p["x"] < q["x"] + q["width"]
        and q["x"] < p["x"] + p["width"]
        and p["y"] < q["y"] + q["height"]
        and q["y"] < p["y"] + p["height"]
    ]


def test_an_agent_built_graph_and_its_tiles_do_not_overlap(page):
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA},
        include_builtin_components=False,
        notifications=False,
    )
    ops = DashboardOps(editor)
    serve_component(page, editor)

    for name, args in GRAPH:
        getattr(ops, name)(**args)
    editor._fit_canvas()

    nodes = page.locator(".react-flow__node")
    expect(nodes).to_have_count(len(editor._tile_items))
    ids = [item["instance_id"] for item in editor._tile_items]
    boxes = {
        node_id: stable_box(page.locator(f'.react-flow__node[data-id="{node_id}"]'), page)
        for node_id in ids
    }
    assert _overlapping(boxes) == []
    for edge in editor.graph.edges:
        source, target = boxes[edge["source"]], boxes[edge["target"]]
        assert source["x"] + source["width"] <= target["x"]

    page.get_by_role("button", name="Layout").click()
    count = len(editor._tile_items)
    wait_until(lambda: len(editor._grid_ids) == count, page, timeout=8000)
    tiles = page.locator(".muuri-grid-item")
    expect(tiles).to_have_count(count)
    tile_boxes = {
        tile_id: stable_box(tiles.nth(i), page) for i, tile_id in enumerate(editor._grid_ids)
    }
    assert _overlapping(tile_boxes) == []

    def top(name):
        return round(tile_boxes[name]["y"])

    # The control and the KPI share a row, then the charts side by side and the table. The
    # source and transform tiles go last, unless the grid moves one up into a gap.
    assert top("factor") == top("kpi") < top("trend") == top("split") < top("rows")
    assert tile_boxes["factor"]["x"] < tile_boxes["kpi"]["x"]
    assert tile_boxes["trend"]["x"] < tile_boxes["split"]["x"]
