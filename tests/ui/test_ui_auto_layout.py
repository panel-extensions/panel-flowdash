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


# The boxes of the canvas's nodes by id, across shadow roots.
_NODE_BOXES = """() => {
    const boxes = {}
    const walk = (root) => root.querySelectorAll("*").forEach((el) => {
        if (el.classList.contains("react-flow__node")) {
            const r = el.getBoundingClientRect()
            boxes[el.dataset.id] = {x: r.x, y: r.y, width: r.width, height: r.height}
        }
        if (el.shadowRoot) walk(el.shadowRoot)
    })
    walk(document)
    return boxes
}"""


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
    # The nodes move as they are laid out and the view fits them, so all of
    # them are read at once until two reads agree.
    reads = []

    def settled():
        reads.append(page.evaluate(_NODE_BOXES))
        return len(reads) >= 3 and reads[-1] == reads[-2] == reads[-3]

    wait_until(settled, page, timeout=10000, interval=200)
    boxes = reads[-1]
    assert _overlapping(boxes) == []
    for edge in editor.graph.edges:
        source, target = boxes[edge["source"]], boxes[edge["target"]]
        assert source["x"] + source["width"] <= target["x"]

    page.get_by_role("button", name="Layout").click()
    count = len(editor._tile_items)
    wait_until(lambda: len(editor._grid_ids) == count, page, timeout=8000)
    tiles = page.locator(".muuri-grid-item")
    expect(tiles).to_have_count(count)
    hidden = {e["instance_id"] for e in editor.layout if e.get("visible") is False}
    # The source and the transform are plumbing, whose tiles start hidden.
    assert hidden == {"source", "combined"}
    tile_boxes = {
        tile_id: stable_box(tiles.nth(i), page)
        for i, tile_id in enumerate(editor._grid_ids)
        if tile_id not in hidden
    }
    assert _overlapping(tile_boxes) == []

    def top(name):
        return round(tile_boxes[name]["y"])

    # The control and the KPI share a row, then the charts side by side and the table.
    assert top("factor") == top("kpi") < top("trend") == top("split") < top("rows")
    assert tile_boxes["factor"]["x"] < tile_boxes["kpi"]["x"]
    assert tile_boxes["trend"]["x"] < tile_boxes["split"]["x"]
