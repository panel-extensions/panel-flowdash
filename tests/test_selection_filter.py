"""Tests for the Filter by selection node."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import SELECTION_TYPE, FlowDash, Predicate, Selection, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.selection import EMPTY, SelectionFilter

FILTER = "Data/FilterBySelection"

FRAME = pd.DataFrame({"category": ["A", "B", "C", "A"], "price": [5.0, 15.0, 25.0, 35.0]})


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
class Rows(Viewer):
    data = param.DataFrame(default=FRAME)

    def __panel__(self):
        return pn.pane.Str("rows")


@register(component=True, page=False, provides=[{"key": "selection", "type": SELECTION_TYPE}])
class Picker(Viewer):
    selection = param.ClassSelector(class_=Selection, default=EMPTY)

    def __panel__(self):
        return pn.pane.Str("picker")


def dashboard():
    editor = FlowDash({"Test/Rows": Rows, "Test/Picker": Picker}, notifications=False)
    ops = DashboardOps(editor)
    ops.add_node("Test/Rows", {}, name="rows")
    ops.add_node("Test/Picker", {}, name="chart")
    ops.add_node(FILTER, {}, name="brushed")
    ops.connect("rows.data", "brushed.data")
    ops.connect("chart.selection", "brushed.selection")
    return ops


def node(ops, name):
    return ops.editor._component_instances[ops._instance(name)]


def filtered(ops):
    return ops.editor.graph.get_state(ops._instance("brushed")).filtered


def test_without_a_selection_every_row_passes():
    ops = dashboard()
    pd.testing.assert_frame_equal(filtered(ops), FRAME)
    assert node(ops, "brushed")._summary() == "All 4 rows"


def test_selection_keeps_the_matching_rows():
    ops = dashboard()
    node(ops, "chart").selection = Selection(
        (Predicate("category", "in", ("A",)), Predicate("price", "range", (0, 30)))
    )
    assert filtered(ops).price.tolist() == [5.0]
    assert node(ops, "brushed")._summary() == "1 of 4 rows: category in [A]; 0 <= price <= 30"

    node(ops, "chart").selection = Selection(source="chart")
    pd.testing.assert_frame_equal(filtered(ops), FRAME)


def test_filter_waits_for_its_data_but_not_for_a_selection():
    editor = FlowDash({}, notifications=False)
    spec = editor.component_specs[FILTER]
    assert [(p.name, p.required, p.blocking) for p in spec.inputs] == [
        ("data", True, True),
        ("selection", False, False),
    ]
    assert [(p.name, p.type) for p in spec.outputs] == [("filtered", "DataFrame")]


def test_selection_by_position_is_refused():
    instance = SelectionFilter(data=FRAME, selection=Selection(index=(0,)))
    with pytest.raises(ValueError, match="by their position"):
        instance.filtered()
    assert "by their position" in instance._summary()


def test_selection_on_a_missing_column_is_refused():
    instance = SelectionFilter(data=FRAME, selection=Selection((Predicate("x", "in", (1,)),)))
    with pytest.raises(ValueError, match="no column 'x'"):
        instance.filtered()


def test_selection_input_only_takes_selections():
    ops = dashboard()
    ops.editor.disconnect(
        ops._instance("chart"), "selection", ops._instance("brushed"), "selection"
    )
    with pytest.raises(OperationError, match="Type mismatch"):
        ops.connect("rows.data", "brushed.selection")


import datetime as dt
import json

import numpy as np

from panel_flowdash import from_holoviews

TIMES = pd.DataFrame(
    {
        "when": pd.date_range("2025-01-01", periods=4, freq="D"),
        "price": [1.0, np.nan, 3.0, 4.0],
        "kind": pd.Categorical(["a", "b", "a", "c"]),
        "size": pd.Categorical(["s", "m", "l", "m"], categories=["s", "m", "l"], ordered=True),
    }
)


def keep(predicate, frame=TIMES):
    return Selection((predicate,)).filter(frame)


def test_naive_bounds_are_read_as_utc_on_a_tz_aware_column():
    frame = TIMES.assign(when=TIMES.when.dt.tz_localize("UTC").dt.tz_convert("America/New_York"))
    kept = keep(
        Predicate(
            "when", "range", (dt.datetime(2025, 1, 2), dt.datetime(2025, 1, 3)), closed="left"
        ),
        frame,
    )
    assert kept.index.tolist() == [1]


def test_date_bounds_on_a_datetime_column():
    kept = keep(
        Predicate("when", "range", (dt.date(2025, 1, 2), dt.date(2025, 1, 4)), closed="left")
    )
    assert kept.index.tolist() == [1, 2]


@pytest.mark.parametrize(
    "bound", [np.datetime64("2025-01-02T00:00:00", "us"), np.datetime64("2025-01-02", "ns")]
)
def test_numpy_datetime_bounds(bound):
    assert keep(Predicate("when", "range", (bound, None))).index.tolist() == [1, 2, 3]


def test_missing_values_are_left_out():
    assert keep(Predicate("price", "range", (0, 10))).index.tolist() == [0, 2, 3]
    assert keep(Predicate("price", "in", (1.0, 3.0))).index.tolist() == [0, 2]


def test_ranges_on_categories():
    assert keep(Predicate("size", "range", ("m", "l"))).index.tolist() == [1, 2, 3]
    assert keep(Predicate("kind", "in", ("a",))).index.tolist() == [0, 2]
    with pytest.raises(ValueError, match="unordered categories"):
        keep(Predicate("kind", "range", ("a", "b")))


def test_values_that_cannot_be_compared_name_the_field_and_type():
    with pytest.raises(
        ValueError, match="'price' cannot be compared with the column's values of type float64"
    ):
        keep(Predicate("price", "range", ("a", "b")))


def test_tile_shows_why_a_selection_cannot_filter():
    lasso = from_holoviews(type("Lasso", (), {})(), None)
    instance = SelectionFilter(data=FRAME, selection=lasso)
    assert "lasso" in instance._summary()
    tz = TIMES.assign(when=TIMES.when.dt.tz_localize("UTC"))
    instance = SelectionFilter(
        data=tz, selection=Selection((Predicate("price", "range", ("a", "b")),))
    )
    assert "cannot be compared" in instance._summary()


@pytest.mark.parametrize(
    "value, kind",
    [
        (np.datetime64("2025-01-02T03:04:05", "us"), "datetime"),
        (np.datetime64("2025-01-02T03:04:05", "ns"), "datetime"),
        (pd.Timestamp("2025-01-02 03:04:05"), "datetime"),
        (pd.Timestamp("2025-01-02 03:04:05", tz="UTC"), "datetime"),
        (dt.datetime(2025, 1, 2, 3, 4, 5), "datetime"),
        (dt.date(2025, 1, 2), "date"),
        (np.datetime64("NaT", "ns"), None),
        (np.float64(1.5), None),
    ],
)
def test_dict_is_json_safe_for_every_temporal_type(value, kind):
    data = Predicate("x", "in", (value,)).to_dict()
    restored = Predicate.from_dict(json.loads(json.dumps(data)))
    assert data.get("types", [None]) == [kind]
    if kind is None:
        assert restored.values == ((None,) if isinstance(value, np.datetime64) else (1.5,))
    else:
        assert pd.Timestamp(restored.values[0]) == pd.Timestamp(value)


def test_temporal_bounds_are_shown_to_the_second():
    predicate = Predicate(
        "when",
        "range",
        (pd.Timestamp("2025-02-01"), dt.datetime(2025, 3, 1, 12, 30, 15, 999)),
        closed="left",
    )
    assert str(predicate) == "2025-02-01 <= when < 2025-03-01 12:30:15"
