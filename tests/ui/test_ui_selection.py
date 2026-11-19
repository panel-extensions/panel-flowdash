"""UI tests for a chart's selection output: brushing, debouncing a drag and clearing."""

import time

import numpy as np
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

SCATTER_FRAME = pd.DataFrame(
    {"x": np.linspace(0, 100, 200), "y": np.sin(np.linspace(0, 10, 200)) * 50 + 50}
)

SCATTER = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "width": 300,
    "height": 200,
    "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x", "y"]}}],
    "mark": "point",
    "data": {"values": SCATTER_FRAME.to_dict("records")},
    "encoding": {
        "x": {"field": "x", "type": "quantitative"},
        "y": {"field": "y", "type": "quantitative"},
    },
}

MONTHS = pd.DataFrame({"date": pd.date_range("2025-01-01", "2025-12-31", freq="D"), "value": 1})

MONTHLY = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "width": 300,
    "height": 200,
    "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
    "mark": "bar",
    "data": {
        "values": MONTHS.assign(
            date=(MONTHS.date - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
        ).to_dict("records")
    },
    "encoding": {
        "x": {"field": "date", "timeUnit": "yearmonth", "type": "temporal"},
        "y": {"aggregate": "sum", "field": "value", "type": "quantitative"},
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

    spec = SPEC

    debounce = 150

    def __init__(self, **params):
        super().__init__(**params)
        self.pane = pn.pane.Vega(self.spec)
        self.link = link_selection(self, self.pane, debounce=self.debounce)

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

    delay = 0

    def __init__(self, **params):
        super().__init__(**params)
        self.received = []
        self.param.watch(self._receive, "selection")

    def _receive(self, event):
        self.received.append((time.monotonic(), event.new))
        if self.delay:
            time.sleep(self.delay)

    def __panel__(self):
        return pn.pane.Str(self.param.selection)


def _open(page, chart=SelectionChart, sink=SelectionSink):
    editor = FlowDash(
        {CHART: chart, SINK: sink},
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


def _drag(page, plot, start, end, steps=30, pause=0, y0=0.5, y1=0.5):
    # The plot area is the 300x200 chart, right of the y axis; positions are fractions of it.
    left = plot["x"] + plot["width"] - 300 - 5
    top = plot["y"] + 5
    page.mouse.move(left + 300 * start, top + 200 * y0)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(
            left + 300 * (start + (end - start) * i / steps),
            top + 200 * (y0 + (y1 - y0) * i / steps),
        )
        if pause:
            page.wait_for_timeout(pause)
    page.mouse.up()
    return left, top + 200 * y1


def _reports(sink):
    return [s for _, s in sink.received if s is not None and not s.empty]


def test_brushing_reports_the_brushed_values_once_per_drag(page):
    _editor, _chart, sink, chart_id, plot = _open(page)

    _drag(page, plot, 0.05, 0.45)
    wait_until(lambda: sink.selection is not None and not sink.selection.empty, page, timeout=8000)
    page.wait_for_timeout(600)

    (predicate,) = sink.selection.predicates
    assert (predicate.field, predicate.op) == ("category", "in")
    assert predicate.values[0] == "A" and len(predicate.values) >= 2
    assert sink.selection.source == chart_id
    reports = _reports(sink)
    assert len(reports) == 1, reports


def test_clicking_outside_the_brush_clears_the_selection(page):
    _editor, _chart, sink, _chart_id, plot = _open(page)

    left, y = _drag(page, plot, 0.05, 0.45)
    wait_until(lambda: sink.selection is not None and not sink.selection.empty, page, timeout=8000)

    page.mouse.click(left + 300 * 0.9, y)
    wait_until(lambda: sink.selection.empty, page, timeout=8000)


class ScatterChart(SelectionChart):
    spec = SCATTER


@pytest.mark.parametrize("pause", [25, 60])
def test_a_slow_drag_on_a_continuous_chart_reports_about_once(page, pause):
    _editor, chart, sink, _chart_id, plot = _open(page, ScatterChart)
    moves = []
    chart.pane.selection.param.watch(lambda e: moves.append(e.new), "brush")

    _drag(page, plot, 0.1, 0.6, steps=30, pause=pause, y0=0.1, y1=0.8)
    wait_until(lambda: len(_reports(sink)) >= 1, page, timeout=8000)
    page.wait_for_timeout(600)

    (report,) = _reports(sink)
    assert report.fields == ["x", "y"]
    assert len(moves) <= 2, moves


class FastScatterChart(SelectionChart):
    spec = SCATTER
    debounce = 20


class SlowSink(SelectionSink):
    delay = 0.4


def test_selections_made_while_one_is_reported_are_coalesced(page):
    _editor, chart, sink, _chart_id, plot = _open(page, FastScatterChart, SlowSink)
    moves = []
    chart.pane.selection.param.watch(lambda e: moves.append(e.new), "brush")

    _drag(page, plot, 0.1, 0.6, steps=30, pause=60, y0=0.1, y1=0.8)
    released = time.monotonic()
    wait_until(
        lambda: bool(_reports(sink)) and _reports(sink)[-1] == chart.link.value,
        page,
        timeout=10000,
    )
    page.wait_for_timeout(1000)

    reports = _reports(sink)
    assert len(moves) >= 10, moves
    assert len(reports) <= len(moves) // 2, (len(reports), len(moves))
    assert reports[-1] == chart.link.value
    last_at = sink.received[-1][0]
    assert last_at - released < 1.0


class MonthlyChart(SelectionChart):
    spec = MONTHLY


def test_local_time_units_are_cut_in_the_browsers_timezone(browser):
    context = browser.new_context(timezone_id="America/New_York")
    page = context.new_page()
    try:
        _editor, _chart, sink, _chart_id, plot = _open(page, MonthlyChart)
        _drag(page, plot, 0.2, 0.55)
        wait_until(lambda: bool(_reports(sink)), page, timeout=8000)

        (predicate,) = _reports(sink)[-1].predicates
        start, end = predicate.values
        assert predicate.closed == "left"
        # New York midnight on the first of a month, in UTC.
        assert (start.day, start.hour) in ((1, 4), (1, 5)), start
        assert (end.day, end.hour) in ((1, 4), (1, 5)), end
    finally:
        context.close()
