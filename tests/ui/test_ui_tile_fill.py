"""UI tests that a chart sized to its container fills its tile and follows resizes."""

import panel as pn
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import update_tile

from .util import serve_component

pytest.importorskip("playwright")

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "data": {"values": [{"x": "A", "y": 3}, {"x": "B", "y": 5}, {"x": "C", "y": 2}]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "x", "type": "nominal"},
        "y": {"field": "y", "type": "quantitative"},
    },
}


@register(component=True, page=False, title="Container chart")
class ContainerChart(Viewer):
    def __panel__(self):
        return pn.pane.Vega({**SPEC, "width": "container", "height": "container"})


@register(component=True, page=False, title="Fixed chart")
class FixedChart(Viewer):
    def __panel__(self):
        return pn.pane.Vega({**SPEC, "width": 200, "height": 150})


CHART, FIXED = "Demo/container_chart", "Demo/fixed_chart"

# The space a tile keeps around its view: the body's margins, the header
# above it and Vega's padding and axes inside the chart.
SLACK = 120


def _boxes(page, editor, instance_id):
    key = next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)
    tile = page.locator(f".{key}")
    chart = tile.locator(".vega-embed").first
    chart.wait_for(state="visible", timeout=15000)
    return tile.bounding_box(), chart.bounding_box()


def _fills(page, editor, instance_id):
    tile, chart = _boxes(page, editor, instance_id)
    return (
        tile["width"] - SLACK < chart["width"] <= tile["width"]
        and tile["height"] - SLACK < chart["height"] <= tile["height"]
    )


def _open(page, editable):
    editor = FlowDash(
        {CHART: ContainerChart, FIXED: FixedChart},
        include_builtin_components=False,
        notifications=False,
        editable=editable,
    )
    chart_id = editor.add_component(CHART)
    fixed_id = editor.add_component(FIXED)
    update_tile(editor, chart_id, width=50.0, height=360)
    update_tile(editor, fixed_id, width=50.0, height=360)
    editor.dirty = False
    if editable:
        editor.mode = "dashboard"
    page.set_viewport_size({"width": 1400, "height": 900})
    serve_component(page, editor)
    return editor, chart_id, fixed_id


@pytest.mark.parametrize("editable", [False, True], ids=["view", "edit"])
def test_container_chart_fills_its_tile_and_follows_resizes(page, editable):
    editor, chart_id, _ = _open(page, editable)
    wait_until(lambda: _fills(page, editor, chart_id), page, timeout=10000)
    _, before = _boxes(page, editor, chart_id)

    update_tile(editor, chart_id, width=100.0, height=520)
    wait_until(lambda: _fills(page, editor, chart_id), page, timeout=10000)
    _, wider = _boxes(page, editor, chart_id)
    assert wider["width"] > before["width"] * 1.5
    assert wider["height"] > before["height"] + 100

    page.set_viewport_size({"width": 1000, "height": 900})
    wait_until(lambda: _fills(page, editor, chart_id), page, timeout=10000)
    _, narrower = _boxes(page, editor, chart_id)
    assert narrower["width"] < wider["width"] - 200


def test_chart_with_a_fixed_size_keeps_it_in_its_tile(page):
    editor, _, fixed_id = _open(page, False)
    wait_until(lambda: _boxes(page, editor, fixed_id)[1]["width"] > 0, page, timeout=10000)
    tile, chart = _boxes(page, editor, fixed_id)
    assert chart["width"] < 300 < tile["width"] - SLACK
    assert chart["height"] < 250
