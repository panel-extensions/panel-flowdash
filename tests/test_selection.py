"""Tests for plot selections: the value, the Vega and HoloViews adapters and linking an output."""

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
    SELECTION_TYPE,
    FlowDash,
    Predicate,
    Selection,
    SelectionLink,
    current_node_id,
    from_holoviews,
    from_vega,
    link_selection,
    register,
)
from panel_flowdash.port_types import port_types_compatible
from panel_flowdash.selection import DEBOUNCE, EMPTY

FRAME = pd.DataFrame(
    {
        "category": ["A", "B", "C", "A"],
        "price": [5.0, 15.0, 25.0, 35.0],
        "date": pd.to_datetime(["2025-01-15", "2025-02-01", "2025-03-31", "2025-05-01"]),
    }
)


def ms(text):
    return pd.Timestamp(text, tz="UTC").value // 1_000_000


# ---------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------


def test_empty_selection_keeps_every_row():
    assert EMPTY.empty
    assert Selection(source="chart").empty
    assert Selection(source="chart") == Selection(source="chart")
    pd.testing.assert_frame_equal(EMPTY.filter(FRAME), FRAME)


def test_selection_keeps_rows_matching_every_predicate():
    selection = Selection(
        (Predicate("category", "in", ["A", "B"]), Predicate("price", "range", (10, 40)))
    )
    assert selection.filter(FRAME).price.tolist() == [15.0, 35.0]
    assert selection.fields == ["category", "price"]
    assert str(selection) == "category in [A, B]; 10 <= price <= 40"


def test_range_closed_on_the_left_leaves_out_its_end():
    both = Predicate("price", "range", (5, 25))
    left = Predicate("price", "range", (5, 25), closed="left")
    assert FRAME[both.mask(FRAME)].price.tolist() == [5.0, 15.0, 25.0]
    assert FRAME[left.mask(FRAME)].price.tolist() == [5.0, 15.0]
    assert str(left) == "5 <= price < 25"
    assert str(Predicate("price", "range", (None, 5))) == "price <= 5"
    assert str(Predicate("price", "range", (5, None))) == "price >= 5"


def test_open_ended_range():
    assert FRAME[Predicate("price", "range", (None, 15)).mask(FRAME)].price.tolist() == [5.0, 15.0]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"op": "like", "values": ("a",)}, "op is 'range' or 'in'"),
        ({"op": "range", "values": (1, 2, 3)}, r"takes \(start, end\)"),
        ({"op": "range", "values": (1, 2), "closed": "right"}, "closed on 'both' ends"),
    ],
)
def test_invalid_predicates_are_refused(kwargs, error):
    with pytest.raises(ValueError, match=error):
        Predicate("price", **kwargs)


def test_selections_compare_by_value():
    a = Selection((Predicate("category", "in", ["A"]),), source="chart")
    b = Selection([Predicate("category", "in", ("A",))], source="chart")
    assert a == b
    assert a != Selection((Predicate("category", "in", ["B"]),), source="chart")


def test_selection_round_trips_through_a_dict():
    selection = Selection(
        (Predicate("category", "in", ("A",)), Predicate("price", "range", (1, 2), closed="left")),
        source="chart",
    )
    assert Selection.from_dict(selection.to_dict()) == selection
    by_position = Selection(source="chart", index=(1, 2))
    assert Selection.from_dict(by_position.to_dict()) == by_position
    assert Selection.from_dict(EMPTY.to_dict()) == EMPTY


def test_selection_by_position_cannot_filter():
    selection = Selection(index=[0, 2])
    assert not selection.empty
    with pytest.raises(ValueError, match="by their position"):
        selection.filter(FRAME)


def test_selection_on_a_missing_column_says_which():
    with pytest.raises(ValueError, match="no column 'quantity'"):
        Selection((Predicate("quantity", "range", (1, 2)),)).filter(FRAME)


def test_selection_port_type_is_registered():
    assert SELECTION_TYPE == "selection"
    assert port_types_compatible("Selection", "selection")
    assert not port_types_compatible("Selection", "DataFrame")


# ---------------------------------------------------------------------------
# Vega-Lite
# ---------------------------------------------------------------------------


def spec(**x):
    return {
        "mark": "bar",
        "encoding": {"x": x, "y": {"aggregate": "count", "type": "quantitative"}},
    }


def test_vega_nothing_selected():
    assert from_vega({}, "interval", spec(field="price", type="quantitative"), "c") == Selection(
        source="c"
    )
    assert from_vega(None, "point", None).empty


def test_vega_interval_on_a_continuous_field_is_a_range():
    selection = from_vega(
        {"price": [30.5, 10.25]}, "interval", spec(field="price", type="quantitative"), "chart"
    )
    assert selection == Selection((Predicate("price", "range", (10.25, 30.5)),), "chart")


def test_vega_interval_on_a_discrete_field_keeps_its_values():
    selection = from_vega(
        {"category": ["A", "B"]}, "interval", spec(field="category", type="nominal")
    )
    assert selection.predicates == (Predicate("category", "in", ("A", "B")),)


def test_vega_interval_on_two_fields():
    two = {
        "mark": "point",
        "encoding": {
            "x": {"field": "price", "type": "quantitative"},
            "y": {"field": "quantity", "type": "quantitative"},
        },
    }
    selection = from_vega({"price": [1, 2], "quantity": [3, 4]}, "interval", two)
    assert selection.fields == ["price", "quantity"]


def test_vega_temporal_values_are_datetimes():
    selection = from_vega(
        {"date": [ms("2025-01-02 12:00"), ms("2025-01-05")]},
        "interval",
        spec(field="date", type="temporal"),
    )
    assert selection.predicates == (
        Predicate("date", "range", (dt.datetime(2025, 1, 2, 12), dt.datetime(2025, 1, 5))),
    )


def test_vega_time_unit_field_is_mapped_and_snapped_to_whole_units():
    # Vega reports the field as yearmonth_date and highlights the months whose start the
    # brush covers: February to April here.
    selection = from_vega(
        {"yearmonth_date": [ms("2025-01-27 17:24"), ms("2025-04-22 21:24")]},
        "interval",
        spec(field="date", timeUnit="yearmonth", type="temporal"),
    )
    (predicate,) = selection.predicates
    assert predicate == Predicate(
        "date", "range", (dt.datetime(2025, 2, 1), dt.datetime(2025, 5, 1)), closed="left"
    )
    assert FRAME[predicate.mask(FRAME)].date.dt.month.tolist() == [2, 3]


def test_vega_time_unit_brush_starting_on_a_unit_start_keeps_that_unit():
    selection = from_vega(
        {"year_date": [ms("2024-01-01"), ms("2024-06-01")]},
        "interval",
        spec(field="date", timeUnit={"unit": "year"}, type="temporal"),
    )
    assert selection.predicates[0].values == (dt.datetime(2024, 1, 1), dt.datetime(2025, 1, 1))


def test_vega_time_unit_brush_covering_no_unit_start_selects_nothing():
    selection = from_vega(
        {"yearmonth_date": [ms("2025-01-10"), ms("2025-01-20")]},
        "interval",
        spec(field="date", timeUnit="yearmonth", type="temporal"),
    )
    assert FRAME[selection.predicates[0].mask(FRAME)].empty


def test_vega_local_time_units_are_cut_in_the_browsers_timezone():
    # Months in New York start at 05:00 UTC; Panel sends naive datetimes as UTC.
    selection = from_vega(
        {"yearmonth_date": [ms("2025-01-27"), ms("2025-03-10")]},
        "interval",
        spec(field="date", timeUnit="yearmonth", type="temporal"),
        timezone="America/New_York",
    )
    assert selection.predicates[0].values == (
        dt.datetime(2025, 2, 1, 5),
        dt.datetime(2025, 4, 1, 4),
    )


def test_vega_utc_time_units_are_cut_in_utc():
    selection = from_vega(
        {"utcyearmonth_date": [ms("2025-01-27"), ms("2025-03-10")]},
        "interval",
        spec(field="date", timeUnit="utcyearmonth", type="temporal"),
        timezone="America/New_York",
    )
    assert selection.predicates[0].values == (dt.datetime(2025, 2, 1), dt.datetime(2025, 4, 1))


def test_vega_year_weeks_start_on_sunday():
    # 2025-01-05 and 2025-01-12 are Sundays.
    selection = from_vega(
        {"yearweek_date": [ms("2025-01-04"), ms("2025-01-13")]},
        "interval",
        spec(field="date", timeUnit="yearweek", type="temporal"),
    )
    assert selection.predicates[0].values == (dt.datetime(2025, 1, 5), dt.datetime(2025, 1, 19))


@pytest.mark.parametrize(
    "unit", ["month", "hours", "monthdate", "quarter", "date", "day", "utcmonth"]
)
def test_vega_repeating_time_units_are_unsupported(unit):
    # Vega sends repeating units as dates in its dummy year 2012.
    selection = from_vega(
        {f"{unit}_date": [ms("2012-01-27"), ms("2012-04-22")]},
        "interval",
        spec(field="date", timeUnit=unit, type="temporal"),
    )
    assert not selection.empty
    with pytest.raises(ValueError, match=r"same .* in every year"):
        selection.filter(FRAME)


def test_vega_binned_field_with_a_step_snaps_to_the_bins_whose_midpoint_is_brushed():
    binned = spec(field="price", bin={"step": 10}, type="quantitative")
    # Bins [10, 20) and [20, 30) have their midpoints 15 and 25 inside the brush; [30, 40) has not.
    selection = from_vega({"price": [13, 34]}, "interval", binned)
    assert selection.predicates == (Predicate("price", "range", (10, 30), closed="left"),)


def test_vega_binned_field_with_an_anchor():
    binned = spec(field="price", bin={"step": 10, "anchor": 5}, type="quantitative")
    selection = from_vega({"price": [8, 26]}, "interval", binned)
    assert selection.predicates[0].values == (5, 25)


def test_vega_binned_brush_covering_no_midpoint_keeps_its_range():
    binned = spec(field="price", bin={"step": 10}, type="quantitative")
    selection = from_vega({"price": [11, 14]}, "interval", binned)
    assert selection.predicates == (Predicate("price", "range", (11, 14)),)


def test_vega_binned_field_of_a_chart_that_does_not_aggregate_keeps_the_brushed_range():
    unaggregated = {
        "mark": "tick",
        "encoding": {"x": {"field": "price", "bin": {"step": 10}, "type": "quantitative"}},
    }
    selection = from_vega({"price": [13, 34]}, "interval", unaggregated)
    assert selection.predicates == (Predicate("price", "range", (13, 34)),)


def test_vega_binned_field_without_a_step_keeps_the_brushed_range():
    binned = spec(field="price", bin={"maxbins": 30}, type="quantitative")
    selection = from_vega({"price": [24.3, 87.3]}, "interval", binned)
    assert selection.predicates == (Predicate("price", "range", (24.3, 87.3)),)


def test_vega_point_with_fields_keeps_the_values():
    selection = from_vega(
        [{"category": "A"}, {"category": "C"}, {"category": "A"}],
        "point",
        spec(field="category", type="nominal"),
    )
    assert selection.predicates == (Predicate("category", "in", ("A", "C")),)


def test_vega_point_without_fields_selects_by_position():
    selection = from_vega([1, 3], "point", spec(field="category", type="nominal"))
    assert selection.index == (0, 2)
    assert selection.predicates == ()


def test_vega_escaped_field_names():
    dotted = spec(field="nino3\\.4", type="quantitative")
    selection = from_vega({"nino3\\.4": [0, 1]}, "interval", dotted)
    assert selection.fields == ["nino3.4"]


def test_vega_fields_in_layers_are_found():
    layered = {
        "layer": [{"mark": "bar", "encoding": {"x": {"field": "category", "type": "nominal"}}}]
    }
    selection = from_vega({"category": ["A"]}, "interval", layered)
    assert selection.predicates == (Predicate("category", "in", ("A",)),)


# ---------------------------------------------------------------------------
# HoloViews
# ---------------------------------------------------------------------------


class _Dim(SimpleNamespace):
    pass


class _Element:
    def __init__(self, frame, kdims, vdims):
        self._frame = frame
        self.kdims = [_Dim(name=n) for n in kdims]
        self.vdims = [_Dim(name=n) for n in vdims]

    def dframe(self):
        return self._frame


def _stream(name, **values):
    return type(name, (), {})() if not values else type(name, (), values)()


def test_holoviews_bounds_xy_are_ranges_on_the_first_two_dimensions():
    points = _Element(FRAME, ["price", "date"], ["category"])
    selection = from_holoviews(_stream("BoundsXY", bounds=(30, 2, 10, 1)), points, source="p")
    assert selection == Selection(
        (Predicate("price", "range", (10, 30)), Predicate("date", "range", (1, 2))), "p"
    )


def test_holoviews_bounds_x_and_y():
    points = _Element(FRAME, ["price"], ["date"])
    assert from_holoviews(_stream("BoundsX", boundsx=(1, 2)), points).fields == ["price"]
    assert from_holoviews(_stream("BoundsY", boundsy=(1, 2)), points).fields == ["date"]
    assert from_holoviews(_stream("BoundsXY", bounds=None), points).empty


def test_holoviews_selection1d_on_unique_keys_selects_their_values():
    bars = _Element(
        pd.DataFrame({"category": ["A", "B", "C"], "price": [1, 2, 3]}), ["category"], ["price"]
    )
    selection = from_holoviews(_stream("Selection1D", index=[2, 0]), bars)
    assert selection.predicates == (Predicate("category", "in", ("C", "A")),)


def test_holoviews_selection1d_on_repeated_keys_selects_by_position_or_a_given_key():
    points = _Element(FRAME, ["category"], ["price"])
    assert from_holoviews(_stream("Selection1D", index=[0, 1]), points).index == (0, 1)
    selection = from_holoviews(_stream("Selection1D", index=[0, 1]), points, key="price")
    assert selection.predicates == (Predicate("price", "in", (5.0, 15.0)),)
    assert from_holoviews(_stream("Selection1D", index=[]), points).empty


def test_holoviews_bounds_on_a_categorical_axis_are_unsupported():
    bars = _Element(FRAME, ["category"], ["price"])
    selection = from_holoviews(_stream("BoundsXY", bounds=(0, 0, 1, 10)), bars)
    with pytest.raises(ValueError, match="categorical axis 'category'"):
        selection.filter(FRAME)


def test_holoviews_selection1d_on_a_continuous_key_selects_by_position():
    curve = _Element(pd.DataFrame({"x": [0.5, 1.5, 2.5], "y": [1, 2, 3]}), ["x"], ["y"])
    assert from_holoviews(_stream("Selection1D", index=[1]), curve).index == (1,)


def test_holoviews_lasso_is_unsupported():
    selection = from_holoviews(_stream("Lasso", geometry=[[0, 0], [1, 1], [1, 0]]), None)
    with pytest.raises(ValueError, match="lasso"):
        selection.check_filterable()


def test_holoviews_dynamic_map_uses_the_element_it_shows_last():
    frame = pd.DataFrame({"category": ["A", "B"], "price": [1, 2]})
    dmap = type("DynamicMap", (), {"last": _Element(frame, ["category"], ["price"])})()
    selection = from_holoviews(_stream("Selection1D", index=[1]), dmap)
    assert selection.predicates == (Predicate("category", "in", ("B",)),)


def test_holoviews_unknown_stream_is_refused():
    with pytest.raises(TypeError, match="PointerXY"):
        from_holoviews(_stream("PointerXY", x=1), None)


def test_holoviews_streams():
    hv = pytest.importorskip("holoviews")
    points = hv.Points(FRAME, ["price", "date"], ["category"])
    bounds = hv.streams.BoundsXY(source=points)
    link = SelectionLink().link_stream(bounds, points)
    bounds.event(bounds=(10, 0, 30, 1))
    assert link.value.fields == ["price", "date"]

    bars = hv.Bars(FRAME.groupby("category").price.sum().reset_index(), "category", "price")
    picked = hv.streams.Selection1D(source=bars)
    link.link_stream(picked, bars)
    picked.event(index=[1])
    assert link.value.predicates == (Predicate("category", "in", ("B",)),)
    link.unlink()
    picked.event(index=[0])
    assert link.value.predicates == (Predicate("category", "in", ("B",)),)


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

BRUSH = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
    "mark": "bar",
    "data": {"values": FRAME[["category", "price"]].to_dict("records")},
    "encoding": {
        "x": {"field": "category", "type": "nominal"},
        "y": {"aggregate": "sum", "field": "price", "type": "quantitative"},
    },
}


def test_link_reports_equal_selections_once():
    link = SelectionLink(source="chart")
    seen = []
    link.param.watch(lambda e: seen.append(e.new), "value")
    a = Selection((Predicate("category", "in", ["A"]),), "chart")
    link.push(a)
    link.push(Selection((Predicate("category", "in", ["A"]),), "chart"))
    link.clear()
    link.clear()
    assert seen == [a, Selection(source="chart")]


def test_link_reports_only_the_latest_of_selections_made_while_one_is_reported():
    link = SelectionLink()
    seen = []
    a, b, c = (Selection((Predicate("x", "in", [v]),)) for v in "abc")

    def report(event):
        seen.append(event.new)
        if event.new == a:
            link.push(b)
            link.push(c)

    link.param.watch(report, "value")
    link.push(a)
    assert seen == [a, c]
    assert link.value == c


def test_link_vega_follows_the_pane_and_sets_its_debounce():
    pane = pn.pane.Vega(BRUSH)
    link = SelectionLink(source="chart").link_vega(pane)
    assert pane.debounce == DEBOUNCE

    pane.selection.param.update(brush={"category": ["A", "B"]})
    assert link.value == Selection((Predicate("category", "in", ("A", "B")),), "chart")

    pane.selection.param.update(brush={})
    assert link.value.empty


def test_link_vega_keeps_a_debounce_set_on_the_pane():
    pane = pn.pane.Vega(BRUSH, debounce=500)
    SelectionLink(debounce=150).link_vega(pane)
    assert pane.debounce == 500


def test_link_vega_clears_when_the_pane_gets_another_chart():
    pane = pn.pane.Vega(BRUSH)
    link = SelectionLink().link_vega(pane)
    pane.selection.param.update(brush={"category": ["A"]})
    other = dict(
        BRUSH, params=[{"name": "pick", "select": {"type": "point", "fields": ["category"]}}]
    )
    pane.object = other
    assert link.value.empty
    pane.selection.param.update(pick=[{"category": "C"}])
    assert link.value.predicates == (Predicate("category", "in", ("C",)),)


def test_relinking_stops_following_the_old_pane():
    old, new = pn.pane.Vega(BRUSH), pn.pane.Vega(BRUSH)
    link = SelectionLink().link_vega(old)
    link.link_vega(new)
    old.selection.param.update(brush={"category": ["A"]})
    assert link.value.empty
    new.selection.param.update(brush={"category": ["B"]})
    assert link.value.predicates == (Predicate("category", "in", ("B",)),)


CHART_ID = "Test/BrushChart"


@register(
    component=True,
    page=False,
    title="Brush chart",
    provides=[{"key": "selection", "type": SELECTION_TYPE}],
)
class BrushChart(Viewer):
    selection = param.ClassSelector(class_=Selection, default=EMPTY)

    def __init__(self, **params):
        super().__init__(**params)
        self.created_for = current_node_id()
        self.pane = pn.pane.Vega(BRUSH)
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

    def __panel__(self):
        return pn.pane.Str(str(self.selection))


def test_link_selection_gives_a_component_a_selection_output_stamped_with_its_node():
    editor = FlowDash(
        {CHART_ID: BrushChart, "Test/SelectionSink": SelectionSink},
        include_builtin_components=False,
        notifications=False,
    )
    chart_id = editor.add_component(CHART_ID)
    sink_id = editor.add_component("Test/SelectionSink")
    assert editor.connect(chart_id, "selection", sink_id, "selection") is True
    chart = editor._component_instances[chart_id]
    sink = editor._component_instances[sink_id]

    assert chart.created_for == chart_id
    assert editor.node_id(chart) == chart_id
    assert editor.node_id(object()) is None

    chart.pane.selection.param.update(brush={"category": ["A"]})
    assert sink.selection == Selection((Predicate("category", "in", ("A",)),), chart_id)

    chart.link.link_vega(pn.pane.Vega(BRUSH))
    assert sink.selection == Selection(source=chart_id)


def test_removing_the_node_unlinks_its_selection():
    editor = FlowDash(
        {CHART_ID: BrushChart}, include_builtin_components=False, notifications=False
    )
    chart_id = editor.add_component(CHART_ID)
    chart = editor._component_instances[chart_id]
    editor.remove_component(chart_id)
    chart.pane.selection.param.update(brush={"category": ["A"]})
    assert chart.selection.empty


def test_link_selection_takes_an_explicit_source():
    holder = SelectionSink()
    pane = pn.pane.Vega(BRUSH)
    link_selection(holder, pane, source=lambda: "chart_1")
    pane.selection.param.update(brush={"category": ["A"]})
    assert holder.selection.source == "chart_1"


def test_current_node_id_outside_creation_is_none():
    assert current_node_id() is None


def test_selection_dict_is_json_safe():
    import json

    import numpy as np

    selection = Selection(
        (
            Predicate(
                "date",
                "range",
                (dt.datetime(2025, 2, 1), pd.Timestamp("2025-05-01")),
                closed="left",
            ),
            Predicate("day", "in", (dt.date(2025, 1, 1),)),
            Predicate("price", "range", (np.float64(1.5), np.int64(3))),
        ),
        source="chart",
    )
    restored = Selection.from_dict(json.loads(json.dumps(selection.to_dict())))
    assert restored == selection
    assert restored.predicates[0].values == (dt.datetime(2025, 2, 1), dt.datetime(2025, 5, 1))
    assert restored.predicates[1].values == (dt.date(2025, 1, 1),)


class _Overlay:
    def __init__(self, *layers):
        self._layers = layers

    def values(self):
        return list(self._layers)


def test_holoviews_overlay_uses_its_layer_when_the_layers_agree():
    points = _Element(FRAME, ["price", "date"], ["category"])
    overlay = type("Overlay", (_Overlay,), {})(points, points)
    selection = from_holoviews(_stream("BoundsXY", bounds=(1, 2, 3, 4)), overlay)
    assert selection.fields == ["price", "date"]


def test_holoviews_overlay_of_different_layers_is_unsupported():
    overlay = type("Overlay", (_Overlay,), {})(
        _Element(FRAME, ["price", "date"], []), _Element(FRAME, ["category"], ["price"])
    )
    selection = from_holoviews(_stream("BoundsXY", bounds=(1, 2, 3, 4)), overlay)
    with pytest.raises(ValueError, match="pass that layer as the element"):
        selection.check_filterable()


def test_holoviews_bounds_y_across_bar_heights_is_unsupported():
    frame = pd.DataFrame({"x": [1.0, 2.0], "count": [3, 4]})
    bars = type("Bars", (_Element,), {})(frame, ["x"], ["count"])
    selection = from_holoviews(_stream("BoundsY", boundsy=(0, 3)), bars)
    with pytest.raises(ValueError, match="heights"):
        selection.check_filterable()


def test_holoviews_box_on_a_histogram_keeps_only_its_x_range():
    frame = pd.DataFrame({"x": [1.0, 2.0], "count": [3, 4]})
    histogram = type("Histogram", (_Element,), {})(frame, ["x"], ["count"])
    selection = from_holoviews(_stream("BoundsXY", bounds=(1, 0, 2, 3)), histogram)
    assert selection.predicates == (Predicate("x", "range", (1, 2)),)


def test_holoviews_overlay_stream():
    hv = pytest.importorskip("holoviews")
    points = hv.Points(FRAME, ["price", "date"], ["category"])
    overlay = points * points
    bounds = hv.streams.BoundsXY(source=overlay)
    link = SelectionLink().link_stream(bounds, overlay)
    bounds.event(bounds=(10, 0, 30, 1))
    assert link.value.fields == ["price", "date"]
