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
    assert node(ops, "brushed")._summary() == "1 of 4 rows: category in [A]; price in [0, 30]"

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
