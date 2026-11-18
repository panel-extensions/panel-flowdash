"""UI tests for a chart's selection output: brushing, debouncing a drag and clearing."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import SELECTION_TYPE, FlowDash, Selection, link_selection, register
from panel_flowdash.selection import EMPTY

from .util import serve_component

pytest.importorskip("playwright")

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

FRAME = pd.DataFrame({"category": list("ABCDEF"), "price": [5, 15, 25, 35, 45, 55]})

SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "width": 300,
    "height": 200,
    "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
    "mark": "bar",
    "data": {"values": FRAME.to_dict("records")},
    "encoding": {
        "x": {"field": "category", "type": "nominal"},
        "y": {"field": "price", "type": "quantitative"},
    },
}

CHART, SINK = "UITest/SelectionChart", "UITest/SelectionSink"


@register(
    component=True,
    page=False,
    title="Selection chart",
    provides=[{"key": "selection", "type": SELECTION_TYPE}],
    default_size={"width": 420, "height": 300},
)
class SelectionChart(Viewer):
    selection = param.ClassSelector(class_=Selection, default=EMPTY)

    def __init__(self, **params):
        super().__init__(**params)
        self.pane = pn.pane.Vega(SPEC)
        self.link = link_selection(self, self.pane)

    def __panel__(self):
        return self.pane


@register(
    component=True,
    page=False,
    title="Selection sink",
    requires=[{"key": "selection", "type": SELECTION_TYPE, "required": False}],
)
class SelectionSink(Viewer):
    selection = param.ClassSelector(class_=Selection, default=None, allow_None=True)

    def __init__(self, **params):
        super().__init__(**params)
        self.received = []
        self.param.watch(lambda event: self.received.append(event.new), "selection")

    def __panel__(self):
        return pn.pane.Str(self.param.selection)


def _open(page):
    editor = FlowDash(
        {CHART: SelectionChart, SINK: SelectionSink},
        include_builtin_components=False,
        notifications=False,
        editable=False,
    )
    chart_id = editor.add_component(CHART)
    sink_id = editor.add_component(SINK)
    assert editor.connect(chart_id, "selection", sink_id, "selection") is True
    page.set_viewport_size({"width": 1400, "height": 900})
    serve_component(page, editor)
    canvas = page.locator(".vega-embed canvas, .vega-embed svg").first
    canvas.wait_for(state="visible", timeout=15000)
    page.wait_for_timeout(500)
    plot = canvas.bounding_box()
    return (
        editor,
        editor._component_instances[chart_id],
        editor._component_instances[sink_id],
        chart_id,
        plot,
    )


def _drag(page, plot, start, end, steps=30, pause=0):
    # The plot area is the 300x200 chart, right of the y axis; x positions are fractions of it.
    left = plot["x"] + plot["width"] - 300 - 5
    y = plot["y"] + 100
    page.mouse.move(left + 300 * start, y)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(left + 300 * (start + (end - start) * i / steps), y)
        if pause:
            page.wait_for_timeout(pause)
    page.mouse.up()
    return left, y


def test_brushing_reports_the_brushed_values_once_per_drag(page):
    _editor, _chart, sink, chart_id, plot = _open(page)

    _drag(page, plot, 0.05, 0.45)
    wait_until(lambda: sink.selection is not None and not sink.selection.empty, page, timeout=8000)
    page.wait_for_timeout(600)

    (predicate,) = sink.selection.predicates
    assert (predicate.field, predicate.op) == ("category", "in")
    assert predicate.values[0] == "A" and len(predicate.values) >= 2
    assert sink.selection.source == chart_id
    reports = [s for s in sink.received if s is not None and not s.empty]
    assert len(reports) == 1, reports


def test_clicking_outside_the_brush_clears_the_selection(page):
    _editor, _chart, sink, _chart_id, plot = _open(page)

    left, y = _drag(page, plot, 0.05, 0.45)
    wait_until(lambda: sink.selection is not None and not sink.selection.empty, page, timeout=8000)

    page.mouse.click(left + 300 * 0.9, y)
    wait_until(lambda: sink.selection.empty, page, timeout=8000)
