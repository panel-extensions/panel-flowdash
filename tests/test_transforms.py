"""Tests for edge transforms: the step operations, graph wiring and persistence."""

import pandas as pd
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import ComponentSpec, DataflowGraph, FlowDash, InputPort, OutputPort, Transform
from panel_flowdash.transform_editor import _filter_value, describe
from panel_flowdash.transforms import (
    TRANSFORMS,
    StepInputError,
    apply_transform,
    available_transforms,
    parse_transform,
    summarize_transform,
)

PLANTS = pd.DataFrame(
    {"site": ["a", "b", "c", "a"], "region": ["N", "S", "N", "S"], "mw": [10, 20, 30, 40]}
)

TOTAL_MW = [{"op": "column", "column": "mw"}, {"op": "aggregate", "how": "sum"}]


def run(steps, value):
    return apply_transform(parse_transform(steps), value)


class TestOperations:
    async def test_column(self):
        assert run([{"op": "column", "column": "mw"}], PLANTS).tolist() == [10, 20, 30, 40]

    @pytest.mark.parametrize(
        ("comparison", "value", "expected"),
        [("==", "N", ["a", "c"]), (">", "N", ["b", "a"]), ("in", ["S"], ["b", "a"])],
    )
    async def test_filter(self, comparison, value, expected):
        steps = [{"op": "filter", "column": "region", "comparison": comparison, "value": value}]
        assert run(steps, PLANTS)["site"].tolist() == expected

    async def test_sort_frame_series_and_list(self):
        frame = run([{"op": "sort", "column": "mw", "ascending": False}], PLANTS)
        assert frame["mw"].tolist() == [40, 30, 20, 10]
        assert run([{"op": "sort"}], pd.Series([3, 1, 2])).tolist() == [1, 2, 3]
        assert run([{"op": "sort", "ascending": False}], [3, 1, 2]) == [3, 2, 1]

    async def test_head(self):
        assert len(run([{"op": "head", "n": 2}], PLANTS)) == 2
        assert run([{"op": "head", "n": 2}], (1, 2, 3)) == [1, 2]

    async def test_aggregate_returns_plain_python_values(self):
        total = run(TOTAL_MW, PLANTS)
        assert total == 100
        assert type(total) is int
        assert run([{"op": "aggregate", "how": "mean"}], [1, 2]) == 1.5

    async def test_unique_keeps_order_of_appearance(self):
        assert run([{"op": "column", "column": "site"}, {"op": "unique"}], PLANTS) == [
            "a",
            "b",
            "c",
        ]
        assert run([{"op": "unique"}], [2, 1, 2]) == [2, 1]

    async def test_round(self):
        assert run([{"op": "round", "digits": 1}], 3.14159) == 3.1
        assert run([{"op": "round"}], 2.6) == 3

    async def test_format_applies_spec_prefix_and_suffix(self):
        steps = [{"op": "format", "spec": ",.1f", "prefix": "~", "suffix": " MW"}]
        assert run(steps, 12345.67) == "~12,345.7 MW"

    async def test_format_cannot_reach_attributes_of_the_value(self):
        with pytest.raises(ValueError):
            run([{"op": "format", "spec": "{value.__class__}"}], 1)

    async def test_unset_value_passes_through(self):
        assert run(TOTAL_MW, None) is None


class TestParsing:
    async def test_steps_round_trip_through_to_dict(self):
        steps = parse_transform(TOTAL_MW)
        assert [step.to_dict() for step in steps] == TOTAL_MW

    @pytest.mark.parametrize(
        ("steps", "message"),
        [
            ([{"op": "explode"}], "Unknown transform 'explode'"),
            ([{"op": "head", "rows": 3}], "unknown arguments: rows"),
            ([{"op": "head", "n": -1}], "Transform 'head'"),
            (["head"], "must be a dict with an 'op'"),
        ],
    )
    async def test_invalid_steps_are_rejected(self, steps, message):
        with pytest.raises(ValueError, match=message):
            parse_transform(steps)

    async def test_available_transforms_follow_the_value(self):
        for_frame = {cls.op for cls in available_transforms(PLANTS)}
        for_number = {cls.op for cls in available_transforms(3.2)}
        assert {"column", "filter", "sort", "head"} <= for_frame
        assert "round" not in for_frame
        assert {"round", "format"} <= for_number
        assert "column" not in for_number

    async def test_subclass_registers_a_custom_operation(self):
        class Double(Transform):
            op = "test_double"

            def apply(self, value):
                return value * 2

        try:
            assert run([{"op": "test_double"}], 21) == 42
        finally:
            TRANSFORMS.pop("test_double")


def make_spec(component_id, inputs=None, outputs=None):
    return ComponentSpec(
        component_id=component_id,
        title=component_id,
        description=None,
        icon=None,
        tags=[],
        outputs=outputs or [],
        inputs=inputs or [],
        default_size=None,
    )


SPECS = {
    "plants": make_spec("plants", outputs=[OutputPort(name="frame", type="DataFrame")]),
    "kpi": make_spec(
        "kpi",
        inputs=[InputPort(name="total", type="Number", parameter=param.Number(default=0))],
    ),
    "labels": make_spec("labels", inputs=[InputPort(name="items", type="List")]),
}


@pytest.fixture
def graph():
    errors = []
    graph = DataflowGraph(SPECS, on_error=lambda *args: errors.append(args))
    graph.errors = errors
    graph.add_node("p", "plants").frame = PLANTS
    graph.add_node("k", "kpi")
    return graph


class TestGraphTransforms:
    async def test_transformed_values_propagate(self, graph):
        assert graph.add_edge("p", "frame", "k", "total", TOTAL_MW) is True
        assert graph.get_state("k").total == 100

        graph.get_state("p").frame = PLANTS.head(2)
        assert graph.get_state("k").total == 30

    async def test_transform_replaces_the_static_type_check(self, graph):
        assert "Type mismatch" in graph.add_edge("p", "frame", "k", "total")
        assert graph.add_edge("p", "frame", "k", "total", TOTAL_MW) is True

    async def test_transformed_value_is_checked_against_the_target(self, graph):
        steps = [{"op": "column", "column": "site"}, {"op": "unique"}]
        reason = graph.validate_connection("p", "frame", "k", "total", steps)
        assert "rejects the current value of 'frame'" in reason
        assert graph.add_edge("p", "frame", "k", "total", steps) == reason
        assert graph.edges == []

    async def test_transform_failing_on_current_value_is_rejected(self, graph):
        steps = [{"op": "column", "column": "missing"}]
        reason = graph.add_edge("p", "frame", "k", "total", steps)
        assert reason.startswith("Transform fails on the current value of 'frame'")
        assert graph.edges == []

    async def test_invalid_transform_is_rejected(self, graph):
        reason = graph.add_edge("p", "frame", "k", "total", [{"op": "explode"}])
        assert reason == "Invalid transform: Unknown transform 'explode'."

    async def test_failing_step_is_reported_and_skipped(self, graph):
        graph.add_edge("p", "frame", "k", "total", TOTAL_MW)

        graph.get_state("p").frame = PLANTS.drop(columns="mw")
        assert graph.get_state("k").total == 100
        assert [e[:4] for e in graph.errors] == [("p", "frame", "k", "total")]
        assert isinstance(graph.errors[0][4], KeyError)

        graph.get_state("p").frame = PLANTS.tail(1)
        assert graph.get_state("k").total == 40

    async def test_set_edge_transform_replaces_the_transform(self, graph):
        graph.add_edge("p", "frame", "k", "total", TOTAL_MW)
        max_mw = [{"op": "column", "column": "mw"}, {"op": "aggregate", "how": "max"}]

        assert graph.set_edge_transform("p", "frame", "k", "total", max_mw) is True
        assert graph.get_state("k").total == 40
        assert graph.get_edge_transform("p", "frame", "k", "total") == max_mw
        assert graph.edges[0]["transform"] == max_mw

        graph.get_state("p").frame = PLANTS.head(2)
        assert graph.get_state("k").total == 20

    async def test_rejected_transform_keeps_the_previous_one(self, graph):
        graph.add_edge("p", "frame", "k", "total", TOTAL_MW)
        reason = graph.set_edge_transform("p", "frame", "k", "total", [])
        assert reason.startswith("Connection rejected:")
        assert graph.get_edge_transform("p", "frame", "k", "total") == TOTAL_MW

        graph.get_state("p").frame = PLANTS.head(1)
        assert graph.get_state("k").total == 10

    async def test_replaced_and_removed_transforms_stop_running(self, graph):
        graph.add_edge("p", "frame", "k", "total", TOTAL_MW)
        graph.set_edge_transform("p", "frame", "k", "total", [{"op": "column", "column": "mw"}])
        graph.set_edge_transform("p", "frame", "k", "total", TOTAL_MW)
        graph.get_state("p").frame = PLANTS.drop(columns="mw")
        assert len(graph.errors) == 1

        graph.remove_edge("p", "frame", "k", "total")
        graph.get_state("p").frame = PLANTS.drop(columns=["mw", "site"])
        assert len(graph.errors) == 1

    async def test_set_edge_transform_on_missing_edge(self, graph):
        assert graph.set_edge_transform("p", "frame", "k", "total", TOTAL_MW) == "Edge not found."

    async def test_stage_expressions_preview_each_step(self, graph):
        graph.add_edge("p", "frame", "k", "total", TOTAL_MW)
        stage = lambda i: graph.get_edge_expression("p", "frame", "k", "total", stage=i).rx.value
        assert stage(0) is PLANTS
        assert stage(1).tolist() == [10, 20, 30, 40]
        assert stage(2) == stage(-1) == 100

    async def test_list_input_collects_each_edge_transformed(self, graph):
        graph.add_node("q", "plants").frame = PLANTS.tail(1)
        graph.add_node("l", "labels")
        sites = [{"op": "column", "column": "site"}, {"op": "aggregate", "how": "nunique"}]
        assert graph.add_edge("p", "frame", "l", "items", sites) is True
        assert graph.add_edge("q", "frame", "l", "items", TOTAL_MW) is True
        assert graph.get_state("l").items == [3, 40]


class Plants(Viewer):
    frame = param.DataFrame(default=PLANTS)

    @param.output(param.DataFrame)
    @param.depends("frame")
    def plants(self):
        return self.frame

    def __panel__(self):
        return "plants"


class Kpi(Viewer):
    total = param.Number(default=0)

    def __panel__(self):
        return str(self.total)


class Table(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return "table"


DEMO = {"Demo/plants": Plants, "Demo/kpi": Kpi, "Demo/table": Table}


class TestEditorTransforms:
    async def test_transform_survives_save_and_load(self):
        components = {"Demo/plants": Plants, "Demo/kpi": Kpi}
        editor = FlowDash(components, notifications=False)
        src = editor.add_component("Demo/plants")
        dst = editor.add_component("Demo/kpi")
        assert editor.connect(src, "plants", dst, "total", TOTAL_MW) is True
        assert editor.graph.get_state(dst).total == 100

        model = editor.to_model()
        assert model.edges[0].transform == TOTAL_MW

        reloaded = FlowDash(components, notifications=False)
        reloaded.load_model(model)
        assert reloaded.graph.get_edge_transform(src, "plants", dst, "total") == TOTAL_MW
        assert reloaded.graph.get_state(dst).total == 100

    async def test_set_edge_transform_marks_the_dashboard_dirty(self):
        editor = FlowDash({"Demo/plants": Plants, "Demo/kpi": Kpi}, notifications=False)
        src = editor.add_component("Demo/plants")
        dst = editor.add_component("Demo/kpi")
        editor.connect(src, "plants", dst, "total", TOTAL_MW)
        editor.dirty = False

        max_mw = [{"op": "column", "column": "mw"}, {"op": "aggregate", "how": "max"}]
        assert editor.set_edge_transform(src, "plants", dst, "total", max_mw) is True
        assert editor.dirty
        assert editor.graph.get_state(dst).total == 40


class TestStepInputs:
    async def test_step_refuses_values_it_does_not_accept(self):
        with pytest.raises(StepInputError, match="Round cannot be applied to a DataFrame"):
            run([{"op": "round"}], PLANTS)

    async def test_graph_rejects_steps_that_do_not_fit(self, graph):
        reason = graph.add_edge("p", "frame", "k", "total", [{"op": "aggregate"}])
        assert reason == (
            "Transform fails on the current value of 'frame': "
            "Aggregate cannot be applied to a DataFrame."
        )

    async def test_filter_without_value_keeps_every_row(self):
        assert len(run([{"op": "filter", "column": "site"}], PLANTS)) == len(PLANTS)

    async def test_summary_is_truncated(self):
        steps = parse_transform([{"op": "column", "column": "a_very_long_column_name"}] * 3)
        summary = summarize_transform(steps, max_length=20)
        assert len(summary) == 20
        assert summary.endswith("…")
        assert summarize_transform([]) is None


def _editor_with_edge(transform=TOTAL_MW, target=("Demo/kpi", "total")):
    editor = FlowDash(DEMO, notifications=False)
    src = editor.add_component("Demo/plants")
    dst = editor.add_component(target[0])
    assert editor.connect(src, "plants", dst, target[1], transform) is True
    (edge_id,) = editor._edge_id_map
    view = editor._make_edge_editor({}, None, id=edge_id, type=None, on_patch=None)
    return editor, (src, "plants", dst, target[1]), edge_id, view


def _texts(view):
    return [t.object for t in view._body.select(pmui.Typography)]


def _alerts(view):
    return [a.object for a in view._body.select(pmui.Alert)]


class TestTransformEditor:
    async def test_describe(self):
        assert describe(PLANTS) == "DataFrame · 4 rows × 3 cols"  # noqa: RUF001
        assert describe(PLANTS["mw"]) == "Series · 4 values"
        assert describe([1, 2]) == "list · 2 items"
        assert describe(1234) == "int · 1,234"
        assert describe(0.5) == "float · 0.5"
        assert describe(None) == "unset"

    async def test_filter_values_follow_the_column_type(self):
        assert _filter_value("20", ">", PLANTS["mw"]) == 20
        assert _filter_value("2.5", ">", PLANTS["mw"]) == 2.5
        assert _filter_value("20", "==", PLANTS["site"]) == "20"
        assert _filter_value("N, S", "in", PLANTS["region"]) == ["N", "S"]
        assert _filter_value(" ", "==", PLANTS["site"]) is None

    async def test_previews_show_the_value_after_each_step(self):
        _, _, _, view = _editor_with_edge()
        view.__panel__()
        texts = _texts(view)
        assert "DataFrame · 4 rows × 3 cols" in texts  # noqa: RUF001
        assert "Series · 4 values" in texts
        assert "int · 100" in texts
        assert "✓ Applied to total" in texts

    async def test_editing_an_argument_applies_it_and_updates_the_chip(self):
        editor, key, edge_id, view = _editor_with_edge()
        view.__panel__()
        how = next(w for w in view._body.select(pmui.Select) if w.label == "How")
        how.value = "max"

        assert editor.graph.get_state(key[2]).total == 40
        assert editor.graph.get_edge_transform(*key)[-1] == {"op": "aggregate", "how": "max"}
        edge = next(e for e in editor._flow.edges if e["id"] == edge_id)
        assert edge["label"] == "mw › max"  # noqa: RUF001
        assert editor.dirty

    async def test_rejected_draft_keeps_the_applied_transform(self):
        transform = [
            {"op": "column", "column": "site"},
            {"op": "unique"},
            {"op": "aggregate", "how": "count"},
        ]
        editor, key, edge_id, view = _editor_with_edge(transform)
        view.__panel__()
        view._remove(2)

        assert editor.graph.get_edge_transform(*key) == transform
        assert _alerts(view) == [
            "Not applied: total does not accept list · 3 items. "
            "The edge keeps its previous transform."
        ]

        editor._flow.selection = {"nodes": [], "edges": [edge_id]}
        assert _alerts(view) == []
        assert len(view._steps) == 3

    async def test_repeated_panel_calls_keep_the_same_widgets(self):
        _, _, _, view = _editor_with_edge()
        view.__panel__()
        widgets = view._body.objects
        view.__panel__()
        view.reset()
        assert view._body.objects is widgets

    async def test_step_that_does_not_fit_is_marked_and_blocks_adding(self):
        _, _, _, view = _editor_with_edge()
        view.__panel__()
        view._remove(0)

        assert "Aggregate cannot be applied to a DataFrame." in _texts(view)
        assert not view._body.select(pmui.MenuButton)
        assert _alerts(view) == [
            "Not applied: the step marked in red fails. The edge keeps its previous transform."
        ]

    async def test_add_step_defaults_to_the_first_column(self):
        editor, key, _, view = _editor_with_edge(transform=[], target=("Demo/table", "data"))
        view.__panel__()
        (menu,) = view._body.select(pmui.MenuButton)
        assert [item["op"] for item in menu.items][:4] == ["column", "filter", "sort", "head"]

        view._add("head", PLANTS)
        assert editor.graph.get_edge_transform(*key) == [{"op": "head", "n": 5}]
        view._add("filter", PLANTS)
        assert view._steps[1].column == "site"
        assert _alerts(view) == []

    async def test_filter_widget_coerces_numbers(self):
        editor, key, _, view = _editor_with_edge(
            transform=[{"op": "filter", "column": "mw", "comparison": ">"}, *TOTAL_MW]
        )
        view.__panel__()
        value = next(w for w in view._body.select(pmui.TextInput) if w.label == "Value")
        value.value = "15"

        assert editor.graph.get_edge_transform(*key)[0]["value"] == 15
        assert editor.graph.get_state(key[2]).total == 90

    async def test_previews_follow_the_source(self):
        editor, key, _, view = _editor_with_edge()
        view.__panel__()
        editor.graph.get_state(key[0]).frame = PLANTS.head(1)
        assert "DataFrame · 1 rows × 3 cols" in _texts(view)  # noqa: RUF001

    async def test_editor_is_disposed_with_its_edge(self):
        editor, key, edge_id, view = _editor_with_edge()
        view.__panel__()
        assert view._watched is not None

        editor.disconnect(*key)
        assert edge_id not in editor._transform_editors
        assert view._watched is None

    async def test_only_transformed_edges_carry_a_chip(self):
        editor, _, edge_id, _ = _editor_with_edge()
        src = editor.add_component("Demo/plants")
        dst = editor.add_component("Demo/table")
        assert editor.connect(src, "plants", dst, "data") is True
        plain = next(e for e in editor._flow.edges if e["id"] != edge_id)
        transformed = next(e for e in editor._flow.edges if e["id"] == edge_id)
        assert transformed["label"] == "mw › sum"  # noqa: RUF001
        assert transformed["style"]["strokeDasharray"]
        assert not plain.get("label")
