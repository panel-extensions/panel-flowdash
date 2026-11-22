"""UI tests that a chart sized to its container fills a tile the layout gave no height."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.ui.test_ui_tile_fill import CHART, ContainerChart

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# What the chart drew at and the size of the grid item holding it, for the
# charts on the tile grid.
DRAWN = """() => {
  function item(el) {
    for (let node = el; node; node = node.parentNode || node.host) {
      if (node.classList && node.classList.contains("muuri-grid-item")) return node
    }
    return null
  }
  const out = []
  for (const model of Bokeh.documents[0].all_models) {
    if (!model.type.endsWith("VegaPlot")) continue
    const view = Bokeh.index.find_one_by_id(model.id)
    if (!view || !view.vega_view || !view.el.isConnected) continue
    const tile = item(view.el)
    if (!tile) continue
    out.push({
      height: view.vega_view.height(),
      box: view.el.getBoundingClientRect().height,
      tile: tile.getBoundingClientRect().height,
    })
  }
  return out
}"""

# The space a tile keeps around its view, as in test_ui_tile_fill.
SLACK = 120


def _fills(page):
    charts = page.evaluate(DRAWN)
    if len(charts) != 1:
        return False
    chart = charts[0]
    return chart["height"] > 100 and chart["tile"] - SLACK < chart["box"] <= chart["tile"]


def _editor():
    editor = FlowDash(
        {CHART: ContainerChart}, notifications=False, include_builtin_components=False
    )
    editor.add_component(CHART)
    return editor


def _show(page, editor, tab, mode):
    page.get_by_role("button", name=tab, exact=True).click()
    wait_until(lambda: editor.mode == mode, page, timeout=8000)


def test_a_container_sized_chart_fills_its_tile_in_the_split(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    editor = _editor()
    editor.mode = "split"
    serve_component(page, editor)
    expect(page.locator(".gutter")).to_be_visible()

    wait_until(lambda: _fills(page), page, timeout=10000)


def test_a_container_sized_chart_fills_its_tile_after_switching_to_the_split(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    editor = _editor()
    serve_component(page, editor)

    _show(page, editor, "Layout", "dashboard")
    wait_until(lambda: _fills(page), page, timeout=10000)
    _show(page, editor, "Split", "split")
    expect(page.locator(".gutter")).to_be_visible()
    wait_until(lambda: _fills(page), page, timeout=10000)
    _show(page, editor, "Layout", "dashboard")
    _show(page, editor, "Split", "split")
    wait_until(lambda: _fills(page), page, timeout=10000)
