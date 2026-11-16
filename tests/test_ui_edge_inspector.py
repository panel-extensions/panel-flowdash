"""Tests for editing edge transforms in the inspector: steps, shapes, drafts and issues."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.transform_editor import (
    TransformEditor,
    check_steps,
    needs,
    shape,
    step_detail,
)
from panel_flowdash.transforms import TRANSFORMS, parse_transform
from panel_flowdash.ui.status_bar import canvas_issues, select_on_canvas

TURBINES = pd.DataFrame(
    {
        "state": ["TX", "TX", "CA", "IA"],
        "p_year": [2001, 2008, 2012, 2019],
        "t_cap": [1500, 2300, 3000, 2500],
    }
)


@register(
    page=False,
    component=True,
    title="Turbines",
    provides=[{"key": "turbines", "type": "DataFrame"}],
)
class Turbines(Viewer):
    frame = param.DataFrame(default=TURBINES)

    @param.output(param.DataFrame)
    @param.depends("frame")
    def turbines(self):
        return self.frame

    def __panel__(self):
        return "turbines"


@register(page=False, component=True, title="Chart")
class Chart(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return "chart"


@register(page=False, component=True, title="Total")
class Total(Viewer):
    total = param.Number(default=0)

    def __panel__(self):
        return "total"


COMPONENTS = {"Test/turbines": Turbines, "Test/chart": Chart, "Test/total": Total}


def _edge(transform=None, target=("Test/chart", "rows")):
    editor = FlowDash(COMPONENTS, notifications=False)
    src = editor.add_component("Test/turbines")
    dst = editor.add_component(target[0])
    assert editor.connect(src, "turbines", dst, target[1], transform) is True
    (edge_id,) = editor._edge_id_map
    select_on_canvas(editor._flow, edge_id=edge_id)
    return editor, editor._edge_id_map[edge_id], edge_id, editor._transform_editors[edge_id]


def _texts(viewable):
    return [str(t.object) for t in viewable.select(pmui.Typography)]


def _chip(editor, edge_id):
    return next(e for e in editor._flow.edges if e["id"] == edge_id)


class TestHelpers:
    def test_shape_leaves_out_unchanged_columns(self):
        assert shape(TURBINES) == "4 rows, 3 columns"
        assert shape(TURBINES.head(1), TURBINES) == "1 row"
        assert shape(TURBINES[["state"]], TURBINES) == "4 rows, 1 column"
        assert shape(TURBINES["t_cap"]) == "Series of 4 values"
        assert shape(1234) == "number 1,234"
        assert shape(None) == "no value yet"

    def test_needs_explains_unavailable_steps(self):
        assert needs(TRANSFORMS["column"], TURBINES) is None
        assert needs(TRANSFORMS["aggregate"], TURBINES) == (
            "Needs a Series or list; add Column first"
        )
        assert needs(TRANSFORMS["round"], [1, 2]) == "Needs a number or Series"
        assert needs(TRANSFORMS["aggregate"], None) is None

    def test_step_detail_reads_like_the_step(self):
        (filter_, sort, head) = parse_transform(
            [
                {"op": "filter", "column": "p_year", "comparison": ">=", "value": 2005},
                {"op": "sort", "column": "t_cap", "ascending": False},
                {"op": "head", "n": 500},
            ]
        )
        assert step_detail(filter_) == "p_year ≥ 2005"
        assert step_detail(sort) == "by t_cap, largest first"
        assert step_detail(head) == "500"

    def test_check_steps_suggests_the_closest_column(self):
        steps = parse_transform([{"op": "sort", "column": "t_capacity"}, {"op": "head"}])
        sort, head = check_steps(steps, TURBINES)
        assert sort.error == "No column named t_capacity."
        assert sort.suggestion == "t_cap"
        assert head.blocked_by == 0

    def test_check_steps_passes_incomplete_steps_through(self):
        (check,) = check_steps(parse_transform([{"op": "filter"}]), TURBINES)
        assert check.missing == "Choose a column and a value to apply this step."
        assert check.after is TURBINES


class TestEdgeInspector:
    async def test_selecting_an_edge_shows_the_connection_and_its_steps(self):
        editor, _, _, _ = _edge([{"op": "head", "n": 2}])
        texts = _texts(editor._inspector._body)
        assert "Connection" in texts
        assert "Turbines to Chart" in texts
        assert "turbines (DataFrame) into Rows (DataFrame)" in texts
        assert "**First rows** 2" in texts
        assert "In: 4 rows, 3 columns" in texts
        assert "Out: 2 rows" in texts

    async def test_the_canvas_shows_no_edge_editor(self):
        editor, _, edge_id, _ = _edge([{"op": "head", "n": 2}])
        assert editor._flow._edge_editors[edge_id] is None
        assert editor._flow._selected_editor is None

    async def test_disconnect_removes_the_edge(self):
        editor, key, _, _ = _edge()
        button = next(
            b
            for b in editor._inspector._body.select(pmui.IconButton)
            if b.description == "Remove connection"
        )
        button.clicks += 1
        assert editor.graph.get_edge_transform(*key) is None
        assert not editor._edge_id_map
        assert "Turbines to Chart" not in _texts(editor._inspector._body)

    async def test_steps_are_added_moved_and_removed(self):
        editor, key, _, view = _edge()
        view._add("head")
        view._add("sort")
        view._set_argument(1, "column", "t_cap")
        assert [s["op"] for s in editor.graph.get_edge_transform(*key)] == ["head", "sort"]

        view._move(1, -1)
        assert [s["op"] for s in editor.graph.get_edge_transform(*key)] == ["sort", "head"]

        view._remove(0)
        assert editor.graph.get_edge_transform(*key) == [{"op": "head", "n": 5}]

    async def test_steps_collapse_to_a_summary_line(self):
        _, _, _, view = _edge(
            [{"op": "filter", "column": "p_year", "comparison": ">=", "value": 2005}]
        )
        assert not view._body.select(pmui.TextInput)
        assert "**Filter rows** p\\_year ≥ 2005" in _texts(view._body)
        assert "Out: 3 rows" in _texts(view._body)

        view._toggle(0)
        (value,) = view._body.select(pmui.TextInput)
        assert value.value == "2005"

    async def test_new_filter_is_not_applied_until_complete(self):
        editor, key, edge_id, view = _edge([{"op": "head", "n": 3}])
        view._add("filter")
        assert editor.graph.get_edge_transform(*key) == [{"op": "head", "n": 3}]
        assert _chip(editor, edge_id)["label"] == "fx first 3"
        assert "Choose a column and a value to apply this step." in _texts(view._body)
        assert view.issue is None

        view._set_argument(1, "column", "state")
        assert editor.graph.get_edge_transform(*key) == [{"op": "head", "n": 3}]

        view._set_argument(1, "value", "TX")
        assert editor.graph.get_edge_transform(*key)[1]["value"] == "TX"
        assert len(editor.graph.get_state(key[2]).rows) == 2
        assert _chip(editor, edge_id)["label"] == "fx 2 steps"

    async def test_add_step_lists_every_operation_with_reasons(self):
        _, _, _, view = _edge()
        view._toggle_adding()
        buttons = {
            b.label: b for b in view._body.select(pmui.Button) if b.label in {"Aggregate", "Round"}
        }
        assert buttons["Aggregate"].disabled
        assert buttons["Round"].disabled
        texts = _texts(view._body)
        assert "Needs a Series or list; add Column first" in texts
        labels = [b.label for b in view._body.select(pmui.Button)]
        assert all(cls.label in labels for cls in TRANSFORMS.values())

        view._add("column")
        view._set_argument(0, "column", "t_cap")
        view._toggle_adding()
        aggregate = next(b for b in view._body.select(pmui.Button) if b.label == "Aggregate")
        assert not aggregate.disabled

    async def test_invalid_step_is_flagged_with_a_suggestion(self):
        editor, key, edge_id, view = _edge([{"op": "sort", "column": "t_cap", "ascending": False}])
        view._set_argument(0, "column", "t_capacity")

        texts = _texts(view._body)
        assert "No column named t\\_capacity. Did you mean t\\_cap?" in texts
        assert "Not applied yet. Chart keeps its last value until this is fixed." in texts
        assert editor.graph.get_edge_transform(*key)[0]["column"] == "t_cap"
        assert view.issue == "Sort step"
        assert "danger" in _chip(editor, edge_id)["style"]["stroke"]

        use = next(b for b in view._body.select(pmui.Button) if b.label == "Use t_cap")
        use.clicks += 1
        assert view.issue is None
        assert "danger" not in _chip(editor, edge_id)["style"]["stroke"]

    async def test_status_bar_counts_an_invalid_transform_and_selects_its_edge(self):
        editor, _, edge_id, view = _edge([{"op": "sort", "column": "t_cap"}])
        view._set_argument(0, "column", "t_capacity")
        bar = editor._status_bar
        assert bar.issues_button.label == "1 issue: Sort step on Turbines to Chart"
        assert bar.issues_button.color == "error"
        (issue,) = canvas_issues(editor)
        assert issue.edge_id == edge_id

        select_on_canvas(editor._flow)
        assert edge_id in editor._transform_editors
        assert bar.issues_button.label == "1 issue: Sort step on Turbines to Chart"
        bar.issues_button.clicks += 1
        assert editor._flow.selection["edges"] == [edge_id]

    async def test_target_rejection_is_explained_not_silent(self):
        editor, key, edge_id, view = _edge(
            [{"op": "column", "column": "t_cap"}, {"op": "aggregate"}],
            target=("Test/total", "total"),
        )
        view._add("format")
        assert editor.graph.get_edge_transform(*key) == [
            {"op": "column", "column": "t_cap"},
            {"op": "aggregate", "how": "sum"},
        ]
        assert view.issue == "Transform"
        texts = _texts(view._body)
        assert "Total does not accept text." in texts
        assert "Not applied yet. Total keeps its last value until this is fixed." in texts
        assert _chip(editor, edge_id)["label"] == "fx 2 steps"

    async def test_draft_survives_reselecting_until_reverted(self):
        editor, _, edge_id, view = _edge([{"op": "sort", "column": "t_cap"}])
        view._set_argument(0, "column", "t_capacity")
        select_on_canvas(editor._flow)
        select_on_canvas(editor._flow, edge_id=edge_id)
        assert view._steps[0].column == "t_capacity"

        revert = next(
            b for b in view._body.select(pmui.Button) if b.label == "Revert to the applied steps"
        )
        revert.clicks += 1
        assert view._steps[0].column == "t_cap"
        assert view.issue is None

    async def test_selecting_reloads_a_transform_changed_elsewhere(self):
        editor, key, edge_id, view = _edge([{"op": "head", "n": 2}])
        select_on_canvas(editor._flow)
        assert editor.set_edge_transform(*key, [{"op": "head", "n": 1}]) is True
        select_on_canvas(editor._flow, edge_id=edge_id)
        assert view._steps[0].n == 1

    async def test_shapes_follow_the_source(self):
        editor, key, _, view = _edge([{"op": "head", "n": 2}])
        editor.graph.get_state(key[0]).frame = TURBINES.head(1)
        assert "In: 1 row, 3 columns" in _texts(view._body)
        assert "Out: 1 row" in _texts(view._body)

    async def test_editor_is_disposed_with_its_edge(self):
        editor, key, edge_id, view = _edge([{"op": "head", "n": 2}])
        assert view._watched is not None
        editor.disconnect(*key)
        assert edge_id not in editor._transform_editors
        assert view._watched is None


class TestEdgeChip:
    @pytest.mark.parametrize(
        ("transform", "label"),
        [
            ([{"op": "head", "n": 500}], "fx first 500"),
            ([{"op": "head"}, {"op": "sort", "column": "t_cap"}, {"op": "head"}], "fx 3 steps"),
        ],
    )
    async def test_chip_names_one_step_or_counts_them(self, transform, label):
        editor, _, edge_id, _ = _edge(transform)
        chip = _chip(editor, edge_id)
        assert chip["label"] == label
        assert chip["style"]["strokeDasharray"]

    async def test_plain_edge_has_no_chip(self):
        editor, _, edge_id, _ = _edge()
        assert not _chip(editor, edge_id).get("label")


async def test_transform_editor_view_is_reused():
    editor, _, edge_id, view = _edge([{"op": "head"}])
    assert isinstance(view, TransformEditor)
    assert pn.panel(view) is view._body
    assert editor._transform_editor(edge_id) is view
