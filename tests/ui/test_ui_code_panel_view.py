"""UI test for a node's own view shown on the result side of the code panel."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

DATA = pd.DataFrame({"year": [2014, 2015, 2016, 2017], "capacity": [5.0, 8.6, 8.2, 7.1]})

SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "year", "type": "ordinal"},
        "y": {"field": "capacity", "type": "quantitative"},
    },
}


@register(
    page=False, component=True, title="Rows", provides=[{"key": "data", "type": "DataFrame"}]
)
class Rows(Viewer):
    data = param.DataFrame(default=DATA)

    def __panel__(self):
        return pn.pane.Str("rows")


@register(page=False, component=True, title="Chart", config=["spec"], extensions=["vega"])
class Chart(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    spec = param.Dict(default=SPEC, metadata={"flowdash": {"widget": "spec"}})

    def __panel__(self):
        def render(spec, data):
            values = [] if data is None else data.to_dict("records")
            full = {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "width": "container",
                "height": "container",
                **spec,
                "data": {"values": values},
            }
            return pn.pane.Vega(full, sizing_mode="stretch_both")

        return pn.panel(
            pn.bind(render, self.param.spec, self.param.data), sizing_mode="stretch_both"
        )


# The size each chart in the code panel's view drew at, and how many marks it drew.
DRAWN = """() => {
  function inPanel(el) {
    for (let node = el; node; node = node.parentNode || node.host) {
      if (node.classList && node.classList.contains("fd-code-view")) return true
    }
    return false
  }
  const out = []
  for (const model of Bokeh.documents[0].all_models) {
    if (!model.type.endsWith("VegaPlot")) continue
    const view = Bokeh.index.find_one_by_id(model.id)
    if (!view || !view.vega_view || !view.el.isConnected || !inPanel(view.el)) continue
    const marks = view.vega_view.scenegraph().root.items[0].items
      .filter(group => group.role === "mark")
      .reduce((count, group) => count + group.items.length, 0)
    out.push({ width: view.vega_view.width(), height: view.vega_view.height(), marks })
  }
  return out
}"""


def test_the_view_of_a_spec_node_draws_its_chart(page):
    editor = FlowDash(
        {"T/rows": Rows, "T/chart": Chart}, notifications=False, include_builtin_components=False
    )
    rows = editor.add_component("T/rows", position=(0, 0))
    chart = editor.add_component("T/chart", position=(320, 0))
    editor.connect(rows, "data", chart, "data")
    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{chart}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    view = page.locator(".fd-code-panel .fd-code-view")
    expect(view).to_be_visible(timeout=10000)

    def drawn():
        charts = page.evaluate(DRAWN)
        return len(charts) == 1 and charts[0]["height"] > 50 and charts[0]["marks"] == len(DATA)

    wait_until(drawn, page, timeout=10000)
    box = view.locator(".vega-embed svg, .vega-embed canvas").first.bounding_box()
    assert box["width"] > 100 and box["height"] > 50

    # A run draws a new chart, which is sized to the panel too.
    editor._component_instances[chart].spec = {**SPEC, "mark": "point"}
    wait_until(drawn, page, timeout=10000)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Panel 1.11's Vega pane misses a narrowing container when a layout pass "
        "updated its box first, so ViewFrame still dispatches window resizes; "
        "drop ViewFrame's dispatch once this passes"
    ),
)
def test_the_chart_follows_its_container_without_window_resizes(page):
    editor = FlowDash(
        {"T/rows": Rows, "T/chart": Chart}, notifications=False, include_builtin_components=False
    )
    rows = editor.add_component("T/rows", position=(0, 0))
    chart = editor.add_component("T/chart", position=(320, 0))
    editor.connect(rows, "data", chart, "data")
    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, editor)
    # Drop the window resizes the splitter and ViewFrame dispatch, leaving the
    # chart only its own observation of its container.
    page.evaluate(
        """() => {
          const dispatch = window.dispatchEvent.bind(window)
          window.dispatchEvent = (event) => event.type === "resize" ? true : dispatch(event)
        }"""
    )
    page.locator(f".react-flow__node[data-id='{chart}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    expect(page.locator(".fd-code-panel .fd-code-view")).to_be_visible(timeout=10000)
    wait_until(lambda: len(page.evaluate(DRAWN)) == 1, page, timeout=10000)

    width = page.evaluate(DRAWN)[0]["width"]
    box = page.locator(".fd-code-panel-body .gutter").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(x + 20 * step, y)
        page.wait_for_timeout(20)
    page.mouse.up()
    wait_until(lambda: page.evaluate(DRAWN)[0]["width"] < width - 100, page, timeout=5000)
