"""Connecting a table or series output to a number input through transform steps."""

import pandas as pd
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register, register_port_type
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.editor import FlowDash
from panel_flowdash.transforms import apply_transform, parse_transform
from panel_flowdash.ui.transform_offer import bridge_kind, bridge_steps

from .test_config_choices import port_types_restored  # noqa: F401


@register(
    page=False, component=True, title="Rows", provides=[{"key": "rows", "type": "DataFrame"}]
)
class Rows(Viewer):
    @param.output(param.DataFrame)
    def rows(self):
        return pd.DataFrame({"state": ["TX", "CA"], "capacity": [1.5, 2.0], "count": [1, 2]})

    def __panel__(self):
        return "rows"


@register(
    page=False, component=True, title="Levels", provides=[{"key": "levels", "type": "Series"}]
)
class Levels(Viewer):
    @param.output(param.Series)
    def levels(self):
        return pd.Series([3, 4, 5])

    def __panel__(self):
        return "levels"


@register(page=False, component=True, title="Total")
class Total(Viewer):
    value = param.Number(default=None, allow_None=True)
    label = param.String(default="")

    def __panel__(self):
        return "total"


@register(
    page=False, component=True, title="Mixed", provides=[{"key": "rows", "type": "DataFrame"}]
)
class Mixed(Viewer):
    @param.output(param.DataFrame)
    def rows(self):
        return pd.DataFrame(
            {
                "id": [1, 2],
                "year": [2020, 2021],
                "capacity": [500.25, 1000.25],
                "empty": [float("nan"), float("nan")],
                "when": pd.to_datetime(["2020-01-01", "2021-01-01"]),
                "flag": [True, False],
                "state": ["TX", "CA"],
                "nullable": pd.array([1, None], dtype="Int64"),
            }
        )

    def __panel__(self):
        return "mixed"


@register(
    page=False,
    component=True,
    title="Turbines",
    provides=[{"key": "rows", "type": "tests.Turbines"}],
)
class Turbines(Viewer):
    @param.output(param.DataFrame)
    def rows(self):
        return pd.DataFrame({"capacity": [1.0, 2.0]})

    def __panel__(self):
        return "turbines"


COMPONENTS = {"D/rows": Rows, "D/levels": Levels, "D/total": Total, "D/turbines": Turbines}


@pytest.fixture
def editor():
    editor = FlowDash(
        COMPONENTS, notifications=False, include_builtin_components=False, history_coalesce=0
    )
    editor.new_dashboard("Mine")
    yield editor
    editor.dispose()


def _placed(editor):
    return editor.add_component("D/rows", instance_id="rows"), editor.add_component(
        "D/total", instance_id="total"
    )


def _drop(editor, source, source_port, target, target_port):
    """Report an edge drawn in the browser, as panel-reactflow does."""
    edge = {
        "id": f"{source}-{target}",
        "source": source,
        "sourceHandle": source_port,
        "target": target,
        "targetHandle": target_port,
    }
    editor._flow._handle_msg({"type": "edge_added", "edge": edge})


class TestBridge:
    @pytest.mark.parametrize(
        ("source", "target", "kind"),
        [
            ("DataFrame", "Number", "column"),
            ("pandas.DataFrame", "int", "column"),
            ("Series", "float", "aggregate"),
            ("DataFrame", "str", None),
            ("Number", "Number", None),
            ("str", "Number", None),
        ],
    )
    def test_only_tables_and_series_bridge_to_numbers(self, source, target, kind):
        assert bridge_kind(source, target) == kind

    def test_the_steps_pick_a_column_then_aggregate(self):
        assert bridge_steps("capacity", "mean") == [
            {"op": "column", "column": "capacity"},
            {"op": "aggregate", "how": "mean"},
        ]
        assert bridge_steps(None, "last") == [{"op": "aggregate", "how": "last"}]

    def test_last_takes_the_last_value(self):
        steps = parse_transform(bridge_steps(None, "last"))
        assert apply_transform(steps, pd.Series([1, 2, 7])) == 7
        assert apply_transform(steps, pd.Series([], dtype=float)) is None

    def test_a_bridgeable_drag_is_allowed_and_other_rules_still_apply(self, editor):
        rows, total = _placed(editor)
        assert editor._validate_connection(rows, "rows", total, "value") is None
        assert editor._validate_connection(rows, "rows", total, "label") is not None
        levels = editor.add_component("D/levels", instance_id="levels")
        editor.connect(levels, "levels", total, "value", bridge_steps(None, "sum"))
        assert "already has a connection" in editor._validate_connection(
            rows, "rows", total, "value"
        )


class TestOffer:
    def test_dropping_a_table_on_a_number_offers_steps(self, editor):
        rows, total = _placed(editor)
        _drop(editor, rows, "rows", total, "value")
        offer = editor._transform_offer
        assert offer.dialog.open
        assert offer.mapping == (rows, "rows", total, "value")
        assert editor.graph.edges == [] and editor._flow.edges == []
        assert list(offer._column.options.values()) == ["capacity", "count", "state"]
        assert offer._column.value == "capacity"
        assert offer._result.object == "Gives 3.50"
        assert "Rows of Rows is a table and Value of Total takes a number" in (
            offer._message.object
        )

    def test_the_preview_follows_the_choices(self, editor):
        rows, total = _placed(editor)
        _drop(editor, rows, "rows", total, "value")
        offer = editor._transform_offer
        offer._how.value = "max"
        assert offer._result.object == "Gives 2"
        offer._column.value = "state"
        assert list(offer._how.options.values()) == ["count"], "only count gives a number"
        assert offer._result.object == "Gives 2" and not offer._confirm.disabled
        offer._column.value = "capacity"
        assert "sum" in offer._how.options.values()

    def test_connect_creates_the_edge_with_the_steps(self, editor):
        rows, total = _placed(editor)
        _drop(editor, rows, "rows", total, "value")
        offer = editor._transform_offer
        offer._how.value = "mean"
        assert offer.confirm() is True
        assert not offer.dialog.open
        (edge,) = editor.graph.edges
        assert edge["transform"] == bridge_steps("capacity", "mean")
        assert editor._component_instances[total].value == 1.75
        assert editor.edit_history.versions[-1].metadata["op"] == "connect"
        assert editor.undo() and editor.graph.edges == []

    def test_cancel_connects_nothing(self, editor):
        rows, total = _placed(editor)
        _drop(editor, rows, "rows", total, "value")
        editor._transform_offer._cancel.clicks += 1
        assert not editor._transform_offer.dialog.open
        assert editor.graph.edges == []

    def test_a_series_needs_no_column(self, editor):
        levels = editor.add_component("D/levels", instance_id="levels")
        total = editor.add_component("D/total", instance_id="total")
        _drop(editor, levels, "levels", total, "value")
        offer = editor._transform_offer
        assert not offer._column.visible and not offer._column_text.visible
        assert offer.confirm() is True
        assert editor._component_instances[total].value == 12

    def test_a_table_without_a_value_yet_takes_a_typed_column(self, editor):
        rows, total = _placed(editor)
        editor.graph.get_state(rows).rows = None
        _drop(editor, rows, "rows", total, "value")
        offer = editor._transform_offer
        assert offer._column_text.visible and not offer._column.visible
        assert offer._confirm.disabled
        offer._column_text.value = "capacity"
        assert offer._result.object.startswith("The table has no value yet")
        assert not offer._confirm.disabled
        assert offer.confirm() is True
        assert editor.graph.edges[0]["transform"] == bridge_steps("capacity", "sum")

    def test_dropping_a_component_on_a_number_input_offers_steps(self, editor):
        total = editor.add_component("D/total", instance_id="total")
        editor._wire_dropped(
            editor.add_component("D/rows", instance_id="rows"),
            "D/rows",
            {"node_id": total, "handle_id": "value", "direction": "input"},
        )
        assert editor._transform_offer.mapping == ("rows", "rows", total, "value")


class TestRefused:
    def test_a_refused_drop_is_explained(self, editor, monkeypatch):
        notes = []
        monkeypatch.setattr(editor, "_notify", lambda *args, **kwargs: notes.append(args))
        editor._flow._handle_msg(
            {
                "type": "connection_refused",
                "source": "a",
                "sourceHandle": "out",
                "target": "b",
                "targetHandle": "in",
                "reason": "Type mismatch: no.",
            }
        )
        assert notes == [("warning", "Not connected: Type mismatch: no.")]

    def test_a_refusal_without_a_reason_still_says_so(self, editor, monkeypatch):
        notes = []
        monkeypatch.setattr(editor, "_notify", lambda *args, **kwargs: notes.append(args))
        editor._on_connection_refused({})
        assert notes == [("warning", "Not connected: the port refuses it.")]

    def test_an_edge_the_graph_refuses_is_removed_from_the_page(self, editor, monkeypatch):
        sent = []
        monkeypatch.setattr(editor._flow, "_send_msg", sent.append)
        rows, total = _placed(editor)
        editor.add_component("D/levels", instance_id="levels")
        editor.connect("levels", "levels", total, "value", bridge_steps(None, "sum"))
        # Taken since the drag started: the browser drew it, the graph refuses it.
        monkeypatch.setattr(editor, "_validate_connection", lambda *args, **kwargs: None)
        _drop(editor, rows, "rows", total, "value")
        assert {"type": "edge_rejected", "edge_id": f"{rows}-{total}"}.items() <= sent[-1].items()
        assert not editor._transform_offer.dialog.open
        assert len(editor.graph.edges) == 1


class TestBuiltinKPI:
    @pytest.fixture
    def kpi_editor(self):
        editor = FlowDash(
            {**COMPONENTS, "D/mixed": Mixed}, notifications=False, history_coalesce=0
        )
        yield editor
        editor.dispose()

    def test_only_results_the_kpi_can_show_can_be_connected(self, kpi_editor):
        editor = kpi_editor
        mixed = editor.add_component("D/mixed", instance_id="mixed")
        kpi = editor.add_component("Widgets/KPI", instance_id="kpi")
        _drop(editor, mixed, "rows", kpi, "value")
        offer = editor._transform_offer
        assert offer._column.value == "capacity", "a measure rather than an id or a year"
        for column in ("when", "flag", "state"):
            offer._column.value = column
            assert list(offer._how.options.values()) == ["count"]
        offer._column.value = "empty"
        offer._how.value = "mean"
        assert offer._confirm.disabled and "not a number" in offer._result.object
        offer._column.value = "nullable"
        offer._how.value = "last"
        assert offer._result.object == "This gives <NA>, not a number the input can show."
        offer._column.value = "capacity"
        offer._how.value = "sum"
        assert offer._result.object == "Gives 1,500.50"
        assert offer._result.styles["color"] != "var(--mui-palette-error-main)", (
            "a valid choice after a refused one is not shown as an error"
        )
        assert offer.confirm() is True
        assert editor._component_instances[kpi].value == 1500.5

    def test_closing_forgets_the_connection(self, kpi_editor):
        editor = kpi_editor
        rows = editor.add_component("D/rows", instance_id="rows")
        kpi = editor.add_component("Widgets/KPI", instance_id="kpi")
        _drop(editor, rows, "rows", kpi, "value")
        editor._transform_offer.dialog.open = False
        assert editor._transform_offer.mapping is None


class TestSubtypes:
    @pytest.mark.usefixtures("port_types_restored")
    def test_a_registered_dataframe_subtype_bridges(self, editor):
        register_port_type("tests.Turbines", subtype_of=["DataFrame"])
        assert bridge_kind("tests.Turbines", "Number") == "column"
        assert bridge_kind("any", "Number") is None
        turbines = editor.add_component("D/turbines", instance_id="turbines")
        total = editor.add_component("D/total", instance_id="total")
        _drop(editor, turbines, "rows", total, "value")
        assert editor._transform_offer.mapping == (turbines, "rows", total, "value")
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match="aggregate"):
            ops.connect("turbines.rows", "total.value")


class TestAgent:
    def test_a_refused_connection_suggests_the_steps(self, editor):
        _placed(editor)
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match=r'"op": "column".*"op": "aggregate"'):
            ops.connect("rows.rows", "total.value")
        ops.connect("rows.rows", "total.value", bridge_steps("count", "sum"))
        assert editor._component_instances["total"].value == 3
