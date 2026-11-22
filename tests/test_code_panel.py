"""The code panel under the canvas or in the sidecar's Spec tab: draft, Run, problems on their lines and the result."""

import asyncio
import json
import re
import time
import types

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import CheckDiagnostic, FlowDash, register
from panel_flowdash.ui.code_field import CodeField
from panel_flowdash.ui.status_bar import select_on_canvas

TURBINES = pd.DataFrame(
    {
        "p_year": [2014, 2015, 2015, 2016, 2016, 2016],
        "p_name": ["a", "b", "c", "d", "e", "f"],
        "p_cap": [1.5, 2.0, 2.5, 1.0, 3.0, 2.0],
    }
)


@register(
    page=False, component=True, title="Turbines", provides=[{"key": "data", "type": "DataFrame"}]
)
class Turbines(Viewer):
    @param.output(param.DataFrame)
    def data(self):
        return TURBINES

    def __panel__(self):
        return "turbines"


def check_query(config, inputs):
    data = inputs.get("data")
    expr = config["expr"]
    if data is None or not expr.strip():
        return None
    for row, line in enumerate(expr.splitlines(), start=1):
        for match in re.finditer(r"[A-Za-z_]\w*", line):
            word = match.group()
            if word in data.columns or word in {"and", "or", "not", "in"}:
                continue
            close = [c for c in data.columns if sorted(c) == sorted(word)]
            return CheckDiagnostic(
                f"There is no column {word}.",
                field="expr",
                line=row,
                column=match.start() + 1,
                end_column=match.end() + 1,
                suggestion=close[0] if close else None,
            )
    return None


@register(
    page=False,
    component=True,
    title="Query",
    icon="code",
    config=["expr", "limit"],
    check_config=check_query,
    requires=[{"key": "data", "type": "DataFrame", "required": True}],
    provides=[{"key": "rows", "type": "DataFrame"}],
)
class Query(Viewer):
    data = param.DataFrame(default=None, allow_None=True)
    expr = param.String(
        default="p_year > 2014", metadata={"flowdash": {"widget": "code", "language": "python"}}
    )
    limit = param.Integer(default=100, bounds=(1, None))

    @param.output(param.DataFrame)
    @param.depends("data", "expr", "limit")
    def rows(self):
        if self.data is None:
            return None
        frame = self.data.query(self.expr) if self.expr.strip() else self.data
        return frame.head(self.limit)

    def __panel__(self):
        return "query"


@register(page=False, component=True, title="Note", config=["text"])
class Note(Viewer):
    text = param.String(
        default="# Hi", metadata={"flowdash": {"widget": "code", "language": "markdown"}}
    )

    def __panel__(self):
        return self.text


@register(
    page=False,
    component=True,
    title="Lazy query",
    icon="code",
    config=["expr"],
    requires=[{"key": "data", "type": "DataFrame", "required": True}],
    provides=[{"key": "pipeline"}, {"key": "rows", "type": "DataFrame"}],
)
class LazyQuery(Viewer):
    """A query with a lazy output beside its table, as a Lumen SQL node has."""

    data = param.DataFrame(default=None, allow_None=True)
    expr = param.String(
        default="p_year > 2014", metadata={"flowdash": {"widget": "code", "language": "python"}}
    )

    @param.output(param.String)
    @param.depends("expr")
    def pipeline(self):
        return f"query({self.expr!r})"

    @param.output(param.DataFrame)
    @param.depends("data", "expr")
    def rows(self):
        return None if self.data is None else self.data.query(self.expr)

    def __panel__(self):
        return "lazy"


@register(
    page=False,
    component=True,
    title="Defined",
    config=["table"],
    provides=[{"key": "data", "type": "DataFrame"}],
)
class Defined(Viewer):
    """A source without a spec field that shows its spec through ``__flowdash_definition__``."""

    table = param.String(default="turbines")

    @param.output(param.DataFrame)
    @param.depends("table")
    def data(self):
        return TURBINES

    def __flowdash_definition__(self):
        return ("yaml", f"type: file\ntables:\n  {self.table}: turbines.csv\n")

    def __panel__(self):
        return "defined"


COMPONENTS = {
    "D/turbines": Turbines,
    "D/query": Query,
    "D/note": Note,
    "D/lazy": LazyQuery,
    "D/defined": Defined,
}


def make_editor(**params):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False, **params)
    source = editor.add_component("D/turbines", position=(0, 0))
    query = editor.add_component("D/query", position=(320, 0))
    editor.connect(source, "data", query, "data")
    return editor, query


def make_lazy_editor(**params):
    """An editor with a node whose code panel offers two outputs, a pipeline and a table."""
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False, **params)
    source = editor.add_component("D/turbines", position=(0, 0))
    query = editor.add_component("D/lazy", position=(320, 0))
    editor.connect(source, "data", query, "data")
    return editor, query


async def settle(panel, seconds=3.0):
    deadline = time.monotonic() + seconds
    await asyncio.sleep(0.05)
    while time.monotonic() < deadline:
        draft = panel._draft
        if draft is None or not draft.busy:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


def rows(editor, node):
    return editor._dataflow_graph.get_state(node).rows


@pytest.fixture
def setup():
    return make_editor()


class TestOpening:
    def test_opens_for_a_code_field(self, setup):
        editor, query = setup
        assert editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert editor.code_panel_open
        assert (panel.instance_id, panel.field) == (query, "expr")
        assert panel.code.value == "p_year > 2014"
        assert panel.code.language == "python"

    def test_the_panel_spans_the_editor_under_the_canvas(self, setup):
        editor, query = setup
        view = editor._view
        assert not editor._code_dock.visible
        editor.open_code_panel(query, "expr")
        index = list(view).index(editor._code_dock)
        assert view[index - 1] is editor._editor_row
        assert editor._workspace_area[0] is editor._flow
        assert editor._code_dock.visible
        assert editor._code_dock.styles["height"] == "400px"
        assert editor._flow.min_height == 160

    def test_closing_hides_the_panel(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.close_code_panel()
        assert not editor.code_panel_open
        assert not editor._code_dock.visible
        assert editor._flow.min_height == 600

    def test_collapsed_shows_the_header(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.code_panel.collapsed = True
        assert editor._code_dock.styles["height"] == "48px"
        assert not editor._code_handle.visible
        assert not editor.code_panel._body.visible
        editor.code_panel.collapsed = False
        assert editor._code_dock.styles["height"] == "400px"

    def test_the_size_is_remembered_for_the_session(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor._code_handle.size = 520
        assert editor._code_dock.styles["height"] == "520px"
        editor.close_code_panel()
        editor.open_code_panel(query, "expr")
        assert editor._code_dock.styles["height"] == "520px"

    def test_split_tab_keeps_it(self, setup):
        editor, query = setup
        editor.mode = "split"
        editor.open_code_panel(query, "expr")
        assert editor._split_view.left[0] is editor._flow
        assert editor._code_dock.visible

    def test_hidden_in_the_layout_tab_and_back_after(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.mode = "dashboard"
        assert editor.code_panel_open
        assert not editor._code_dock.visible
        editor.mode = "wiring"
        assert editor._code_dock.visible

    @pytest.mark.parametrize(
        "change",
        [
            {"editable": False},
            {"preview": True},
        ],
    )
    def test_closes_for_viewers_and_previews(self, setup, change):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.param.update(change)
        assert not editor.code_panel_open

    def test_not_for_viewers(self):
        editor, query = make_editor(editable=False)
        assert not editor.open_code_panel(query, "expr")

    def test_the_layout_tab_opens_it_in_the_spec_tab(self, setup):
        editor, query = setup
        editor.mode = "dashboard"
        assert editor.open_code_panel(query, "expr")
        assert editor.mode == "dashboard"
        assert editor.code_panel.dock == "sidecar"
        assert not editor._code_dock.visible
        assert spec_tab(editor).objects == [editor.code_panel._layout]

    def test_a_narrow_window_opens_it_in_the_spec_tab(self, setup):
        editor, query = setup
        editor._split_view.storage.window_width = 800
        assert editor.open_code_panel(query, "expr")
        assert editor.code_panel.dock == "sidecar"
        assert not editor._code_dock.visible

    def test_a_narrowed_window_moves_it_into_the_spec_tab(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor._split_view.storage.window_width = 800
        assert editor.code_panel_open
        assert editor.code_panel.dock == "sidecar"
        assert spec_tab(editor).objects == [editor.code_panel._layout]
        assert not editor._code_dock.visible and not editor._code_dock.objects

    @pytest.mark.parametrize("field", ["limit", "nope"])
    def test_not_for_other_fields(self, setup, field):
        editor, query = setup
        assert not editor.open_code_panel(query, field)

    def test_not_for_unknown_nodes(self, setup):
        editor, _query = setup
        assert not editor.open_code_panel("missing", "expr")

    def test_removing_the_node_closes_it(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.remove_component(query)
        assert not editor.code_panel_open
        assert not editor._code_dock.visible

    def test_settings_stay_in_the_inspector(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor.open_code_panel(query, "expr")
        header = pn.Row(*editor.code_panel._header)
        assert not [
            w for w in header.select(pn.widgets.Widget) if getattr(w, "name", "") == "Limit"
        ]
        form = next(iter(pn.Column(*editor._inspector._body).select(pn.Param)))
        assert "limit" in form.parameters and "expr" not in form.parameters

    def test_the_field_defaults_to_the_nodes_spec(self, setup):
        editor, query = setup
        assert editor.open_code_panel(query)
        assert editor.code_panel.field == "expr"

    def test_switching_nodes_shows_the_other_text(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        editor.open_code_panel(note, "text")
        assert editor.code_panel.code.value == "# Hi"
        assert editor.code_panel.code.language == "markdown"


class TestRun:
    async def test_the_draft_is_held_until_run(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        panel.code.value_input = "p_year > 2015"
        assert panel.dirty
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year > 2014"
        panel.run()
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year > 2015"
        assert len(rows(editor, query)) == 3
        assert not panel.dirty
        assert panel._ran_at is not None

    async def test_run_takes_the_text_sent_with_the_key(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.run("p_year == 2016")
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year == 2016"

    async def test_run_is_one_edit(self, setup, monkeypatch):
        editor, query = setup
        recorded = []
        record = editor._record_edit
        monkeypatch.setattr(
            editor, "_record_edit", lambda *a, **k: (recorded.append(k), record(*a, **k))[1]
        )
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        panel.code.value_input = "p_year > 2015"
        panel.run()
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year > 2015"
        assert len(rows(editor, query)) == 3
        assert [k["args"]["keys"] for k in recorded] == [["expr"]]

    async def test_a_failed_check_keeps_the_draft_and_the_last_result(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        good = rows(editor, query)
        panel.code.value_input = "p_year > 2014\nand p_yaer < 2016"
        panel.run()
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year > 2014"
        assert rows(editor, query) is good
        assert panel.dirty
        assert panel.code.value_input.endswith("p_yaer < 2016")
        assert panel.tools.diagnostics == [
            {
                "row": 1,
                "column": 4,
                "end_column": 10,
                "text": "There is no column p_yaer.",
                "suggestion": "p_year",
            }
        ]
        assert panel.code.annotations[0]["row"] == 1
        assert "Not run, the check failed" in panel._result_footer.object
        assert "1 problem" in panel._code_status.object

    async def test_a_passing_run_clears_the_problems(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.run("p_yaer > 1")
        await settle(panel)
        assert panel.tools.diagnostics
        panel.run("p_year > 1")
        await settle(panel)
        assert panel.tools.diagnostics == []
        assert panel.code.annotations == []
        assert "Check passed" in panel._code_status.object

    async def test_a_failing_node_keeps_the_last_good_result(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        panel.run("p_year >")
        await settle(panel)
        assert panel._failed
        assert len(panel._good["rows"]) == 5
        assert "fd-code-run-error" in panel._result_body[0].css_classes

    async def test_run_on_change_runs_after_a_pause(self, setup, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.code_panel.RUN_DELAY", 0.05)
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.run_on_change = True
        panel.code.value_input = "p_year == 2015"
        await asyncio.sleep(0.2)
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year == 2015"

    async def test_without_run_on_change_nothing_runs(self, setup, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.code_panel.RUN_DELAY", 0.05)
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year == 2015"
        await asyncio.sleep(0.2)
        assert editor._component_instances[query].expr == "p_year > 2014"

    async def test_a_change_made_elsewhere_reaches_a_clean_draft(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        editor._apply_config_patch(query, {"expr": "p_cap > 2"})
        assert panel.code.value == "p_cap > 2"
        assert not panel.dirty

    async def test_a_change_made_elsewhere_keeps_an_edited_draft(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year == 2016"
        editor._apply_config_patch(query, {"expr": "p_cap > 2"})
        assert panel.code.value_input == "p_year == 2016"
        assert panel.dirty

    async def test_completions_are_the_input_columns(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await asyncio.sleep(0.2)
        assert [c["value"] for c in panel.tools.completions] == ["p_year", "p_name", "p_cap"]
        assert panel.tools.completions[0]["meta"] == "integer"

    def test_the_result_is_a_table(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert "5 rows &times; 3 columns" in panel._summary.object
        assert "fd-code-table" in panel._result_body[0].css_classes

    def test_the_table_hides_an_index_that_numbers_rows(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        assert editor.code_panel._result_body[0].index is False

    @pytest.mark.parametrize(
        ("frame", "shown"),
        [
            (TURBINES, False),
            (TURBINES[TURBINES.p_year > 2014], False),
            (TURBINES.sort_values("p_cap"), False),
            (TURBINES.iloc[[5, 0, 3]], False),
            (TURBINES.groupby("p_year").p_cap.sum().to_frame(), True),
            (TURBINES.groupby(["p_year", "p_name"]).p_cap.sum().to_frame(), True),
            (TURBINES.set_index(pd.date_range("2024-01-01", periods=6)), True),
            (TURBINES.set_index("p_name"), True),
        ],
        ids=["range", "filtered", "sorted", "iloc", "groupby", "multiindex", "datetime", "named"],
    )
    def test_an_index_with_data_is_shown(self, frame, shown):
        from panel_flowdash.ui.code_panel import _shows_index

        assert _shows_index(frame) is shown

    def test_code_and_result_are_split(self, setup):
        from panel_splitjs import HSplit

        from panel_flowdash.ui.code_panel import SPLIT_SIZES

        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert isinstance(panel._body, HSplit)
        assert list(panel._body) == [panel._code_column, panel._result_column]
        assert panel._body.sizes == SPLIT_SIZES
        assert SPLIT_SIZES[1] > SPLIT_SIZES[0]


class TestInspector:
    def test_edit_opens_the_panel(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        body = pn.Column(*editor._inspector._body)
        assert not list(body.select(CodeField))
        button = next(b for b in body.select(pmui.Button) if "fd-edit-spec" in b.css_classes)
        assert button.label == "Edit Expr"
        button.clicks += 1
        assert editor.code_panel_open
        assert editor.code_panel.field == "expr"

    def test_edit_shows_the_canvas_from_the_layout_tab(self, setup):
        editor, query = setup
        editor.mode = "dashboard"
        assert editor.edit_spec(query)
        assert editor.mode == "split"
        assert editor._code_dock.visible

    def test_edit_is_refused_to_viewers(self):
        editor, query = make_editor(editable=False)
        assert not editor.edit_spec(query)
        assert not editor.code_panel_open

    def test_a_narrow_window_edits_in_the_spec_tab_not_a_dialog(self, setup):
        editor, query = setup
        editor._split_view.storage.window_width = 700
        select_on_canvas(editor._flow, node_id=query)
        body = pn.Column(*editor._inspector._body)
        assert not list(body.select(CodeField))
        button = next(b for b in body.select(pmui.Button) if "fd-edit-spec" in b.css_classes)
        button.clicks += 1
        assert editor.mode == "wiring"
        assert active_tab(editor) == "Spec"
        assert spec_tab(editor).objects == [editor.code_panel._layout]
        assert editor.code_panel.field == "expr"


class TestDrafts:
    async def test_a_draft_is_held_when_another_field_opens(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year == 2016"
        editor.open_code_panel(note, "text")
        assert panel.code.value_input == "# Hi"
        assert panel.has_drafts
        assert editor._has_unsent_edits()
        editor.open_code_panel(query, "expr")
        assert panel.code.value_input == "p_year == 2016"
        assert panel.dirty
        assert editor._component_instances[query].expr == "p_year > 2014"

    @pytest.mark.parametrize(
        "leave",
        [
            lambda editor: editor.close_code_panel(),
            lambda editor: setattr(editor, "editable", False),
        ],
        ids=["close", "done"],
    )
    async def test_a_draft_survives_the_panel_closing(self, setup, leave):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.code_panel.code.value_input = "p_year == 2016"
        editor.dirty = False
        leave(editor)
        assert not editor.code_panel_open
        assert editor._has_unsent_edits()
        assert editor._dashboard_bar._save_state_text() == ("Edits not run", True)
        editor.param.update(editable=True)
        editor._split_view.storage.window_width = 1440
        editor.open_code_panel(query, "expr")
        assert editor.code_panel.code.value_input == "p_year == 2016"

    async def test_discard_puts_the_config_back(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year == 2016"
        assert panel.discard_button.visible
        panel.discard()
        assert panel.code.value_input == "p_year > 2014"
        assert not panel.dirty
        assert not editor._has_unsent_edits()
        assert not panel.discard_button.visible

    async def test_running_clears_the_held_draft(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.run("p_year == 2016")
        await settle(panel)
        editor.close_code_panel()
        assert not editor._has_unsent_edits()

    def test_removing_the_node_drops_its_drafts(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.code_panel.code.value_input = "p_year == 2016"
        editor.close_code_panel()
        editor.remove_component(query)
        assert not editor.code_panel.has_drafts

    async def test_undo_after_run_restores_the_config(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        editor.edit_history.flush()
        panel.run("p_year > 2015")
        await settle(panel)
        assert editor._component_instances[query].expr == "p_year > 2015"
        editor.edit_history.flush()
        assert editor.undo()
        assert editor._component_instances[query].expr == "p_year > 2014"
        assert panel.code.value_input == "p_year > 2014"
        assert not panel.dirty


class TestRunGuards:
    async def test_no_auto_run_while_one_is_in_flight(self, setup, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.code_panel.RUN_DELAY", 0.05)
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.run_on_change = True
        calls = []
        monkeypatch.setattr(panel, "run", lambda *a: calls.append(a))
        panel.running = True
        panel.code.value_input = "p_year == 2016"
        await asyncio.sleep(0.2)
        assert calls == []
        assert panel._run_again

    async def test_a_setting_changed_in_the_inspector_keeps_the_draft(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        panel.code.value_input = "p_year > 2015"
        editor._apply_config_patch(query, {"limit": 2})
        await settle(panel)
        assert panel.code.value_input == "p_year > 2015" and panel.dirty
        panel.run()
        await settle(panel)
        instance = editor._component_instances[query]
        assert (instance.expr, instance.limit) == ("p_year > 2015", 2)
        assert len(rows(editor, query)) == 2


def test_columns_count_utf16_units():
    from panel_flowdash.ui.code_panel import _utf16

    assert _utf16("a😀b", 2) == 3
    assert _utf16("abc", 2) == 2


class TestOutputs:
    def test_a_table_output_is_shown_before_others(self):
        from panel_flowdash.ui.code_panel import CodePanel

        node = types.SimpleNamespace(pipeline=object(), data=TURBINES)
        assert CodePanel._default_output(None, node, ["pipeline", "data"]) == "data"
        assert CodePanel._default_output(None, node, ["pipeline"]) == "pipeline"

    def test_outputs_are_labelled(self):
        editor, query = make_lazy_editor()
        editor.open_code_panel(query, "expr")
        outputs = editor.code_panel.outputs
        assert outputs.visible
        assert outputs.options == {"Pipeline": "pipeline", "Rows": "rows"}
        assert outputs.value == "rows"

    def test_output_labels_keep_their_case(self):
        from panel_flowdash.component_spec import OutputPort
        from panel_flowdash.ui.code_panel import _output_label

        assert _output_label(OutputPort("SQL_expr", label="SQL_expr")) == "SQL expr"
        assert _output_label(OutputPort("data")) == "Data"
        assert _output_label(OutputPort("rows", label="Result rows")) == "Result rows"


def test_code_drafts_follows_held_drafts():
    editor, query = make_editor()
    assert not editor.code_drafts
    editor.open_code_panel(query, "expr")
    editor.code_panel.code.value_input = "p_year == 2016"
    assert editor.code_drafts
    editor.close_code_panel()
    assert editor.code_drafts
    editor.drop_code_drafts()
    assert not editor.code_drafts
    editor.open_code_panel(query, "expr")
    assert editor.code_panel.code.value_input == "p_year > 2014"


class TestFollowsSelection:
    def test_selecting_another_node_shows_its_spec(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        select_on_canvas(editor._flow, node_id=note)
        panel = editor.code_panel
        assert (panel.instance_id, panel.field) == (note, "text")
        assert panel.code.value == "# Hi" and panel.code.visible

    def test_a_node_without_a_spec_shows_its_result(self, setup):
        editor, query = setup
        source = next(i["instance_id"] for i in editor._tile_items if i["instance_id"] != query)
        editor.open_code_panel(query, "expr")
        select_on_canvas(editor._flow, node_id=source)
        panel = editor.code_panel
        assert (panel.instance_id, panel.field) == (source, None)
        assert panel._empty.visible and "has no code or spec" in panel._empty.object
        assert not panel.code.visible and not panel.run_button.visible
        assert "6 rows" in panel._summary.object
        assert "fd-code-table" in panel._result_body[0].css_classes

    def test_a_node_without_a_spec_shows_its_definition_read_only(self, setup):
        editor, query = setup
        defined = editor.add_component("D/defined", position=(0, 300))
        editor.open_code_panel(query, "expr")
        select_on_canvas(editor._flow, node_id=defined)
        panel = editor.code_panel
        assert (panel.instance_id, panel.field) == (defined, None)
        assert panel.definition.visible and panel.definition.readonly
        assert panel.definition.language == "yaml"
        assert panel.definition.value == "type: file\ntables:\n  turbines: turbines.csv\n"
        assert not panel._empty.visible and not panel.code.visible
        assert not panel.run_button.visible and not panel.format_button.visible
        assert not panel.view_toggle.visible and not panel.run_switch.visible
        assert "Read only" in panel._code_header.object
        assert "YAML" in panel._language.object
        assert "6 rows" in panel._summary.object

        # A new output, e.g. after a settings change, may come from a new definition.
        editor._dataflow_graph.get_config_state(defined).table = "sales"
        assert "  sales: turbines.csv" in panel.definition.value

        select_on_canvas(editor._flow, node_id=query)
        assert not panel.definition.visible and panel.code.visible
        assert "Read only" not in panel._code_header.object
        source = next(
            i["instance_id"] for i in editor._tile_items if i["component_id"] == "D/turbines"
        )
        select_on_canvas(editor._flow, node_id=source)
        assert not panel.definition.visible and panel._empty.visible

    def test_a_failing_definition_hook_shows_the_empty_state(self, setup, monkeypatch):
        editor, _query = setup
        defined = editor.add_component("D/defined", position=(0, 300))
        instance = editor._component_instances[defined]
        monkeypatch.setattr(instance, "__flowdash_definition__", lambda: ["yaml", "x"])
        editor.open_code_panel(defined)
        panel = editor.code_panel
        assert not panel.definition.visible and panel._empty.visible

    def test_drafts_are_kept_per_node(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year == 2016"
        select_on_canvas(editor._flow, node_id=note)
        panel.code.value_input = "# Draft"
        select_on_canvas(editor._flow, node_id=query)
        assert panel.code.value_input == "p_year == 2016" and panel.dirty
        select_on_canvas(editor._flow, node_id=note)
        assert panel.code.value_input == "# Draft"
        assert editor._component_instances[query].expr == "p_year > 2014"
        assert editor._component_instances[note].text == "# Hi"

    def test_a_closed_panel_stays_closed(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        assert not editor.code_panel_open

    def test_clearing_or_widening_the_selection_keeps_the_node(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        editor._flow.selection = {"nodes": [], "edges": []}
        assert editor.code_panel.instance_id == query
        editor._flow.selection = {"nodes": [query, note], "edges": []}
        assert editor.code_panel.instance_id == query

    def test_following_keeps_a_collapsed_panel_collapsed(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        editor.code_panel.collapsed = True
        select_on_canvas(editor._flow, node_id=note)
        assert editor.code_panel.instance_id == note
        assert editor.code_panel.collapsed

    def test_following_does_not_take_the_keyboard(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        focus = editor.code_panel.tools.focus
        select_on_canvas(editor._flow, node_id=note)
        assert editor.code_panel.tools.focus == focus


def _upper(text):
    return "\n".join(line.strip().upper() for line in text.split(";") if line.strip())


@register(page=False, component=True, title="Formatted", icon="code", config=["sql"])
class Formatted(Viewer):
    sql = param.String(
        default="select 1; select 2",
        metadata={"flowdash": {"widget": "code", "language": "sql", "formatter": _upper}},
    )

    def __panel__(self):
        return "formatted"


def make_format_editor():
    editor = FlowDash(
        {**COMPONENTS, "D/formatted": Formatted},
        notifications=False,
        include_builtin_components=False,
    )
    node = editor.add_component("D/formatted", position=(0, 0))
    return editor, node


class TestFormat:
    def test_a_field_with_a_formatter_offers_format(self):
        editor, node = make_format_editor()
        editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        assert panel.format_button.visible
        assert panel.tools.can_format
        assert "Shift+Alt+F" in panel.format_button.description

    def test_without_a_formatter_there_is_no_format(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert not panel.format_button.visible
        assert not panel.tools.can_format

    def test_format_is_a_draft_until_run(self):
        editor, node = make_format_editor()
        editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.format()
        assert panel.code.value == panel.code.value_input == "SELECT 1\nSELECT 2"
        assert panel.dirty
        state = editor._dataflow_graph.get_config_state(node)
        assert state.sql == "select 1; select 2"
        panel.run()
        assert state.sql == "SELECT 1\nSELECT 2"
        assert not panel.dirty

    def test_the_shortcut_formats_the_text_sent(self):
        editor, node = make_format_editor()
        editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.tools._handle_msg({"type": "format", "text": "select 3"})
        assert panel.code.value_input == "SELECT 3"

    def test_formatting_again_after_an_undo_sends_the_text(self):
        # After Ctrl+Z in the browser the server's value is still the formatted text, so the
        # editor is not updated through it and the text is sent instead, once.
        editor, node = make_format_editor()
        editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.format()
        assert panel.tools.replacement == {}
        panel.format("select 1; select 2")
        assert panel.tools.replacement == {"text": "SELECT 1\nSELECT 2", "seq": 1}

    def test_a_failing_formatter_leaves_the_text(self):
        def broken(text):
            raise ValueError("unbalanced parenthesis")

        Formatted.param.sql.metadata["flowdash"]["formatter"] = broken
        try:
            editor, node = make_format_editor()
            editor.open_code_panel(node, "sql")
            panel = editor.code_panel
            panel.format()
            assert panel.code.value_input == "select 1; select 2"
            assert "unbalanced parenthesis" in panel._code_status.object
            assert not panel.dirty
        finally:
            Formatted.param.sql.metadata["flowdash"]["formatter"] = _upper

    def test_a_registered_language_formatter(self, setup):
        from panel_flowdash.ui.code_field import register_formatter

        editor, query = setup
        register_formatter("python", lambda text: text.replace(">", " > ").replace("  ", " "))
        try:
            editor.open_code_panel(query, "expr")
            panel = editor.code_panel
            assert panel.format_button.visible
            panel.format("p_year>2014")
            assert panel.code.value_input == "p_year > 2014"
            assert not panel.dirty
        finally:
            register_formatter("python", None)

    def test_not_for_viewers(self):
        editor, node = make_format_editor()
        editor.open_code_panel(node, "sql")
        editor.code_panel.editor.editable = False
        editor.code_panel.format("select 1")
        # Closed for a viewer, and nothing formatted.
        assert not editor.code_panel_open


def spec_tab(editor) -> pn.Column:
    """Return the shown component's Spec tab in the sidecar."""
    column = editor._inspector._spec_column
    assert column is not None
    return column


def active_tab(editor) -> str:
    inspector = editor._inspector
    return inspector._tab_labels[inspector._node_tabs.active]


class TestSpecTab:
    def test_moves_into_the_spec_tab_and_back(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert panel.dock_button.description == "Open in sidecar"
        assert editor._inspector._tab_labels == ["Settings", "Spec", "Data", "Tile"]
        assert "fd-spec-elsewhere" in spec_tab(editor)[0].css_classes
        panel.dock_button.clicks += 1
        assert panel.dock == "sidecar"
        assert active_tab(editor) == "Spec"
        assert spec_tab(editor).objects == [panel._layout]
        assert not editor._code_dock.visible and not editor._code_dock.objects
        assert editor._flow.min_height == 600
        assert type(panel._body).__name__ == "VSplit"
        assert panel.dock_button.description == "Open below the canvas"
        assert not panel.close_button.visible and not panel._title.visible
        panel.dock_button.clicks += 1
        assert panel.dock == "bottom"
        assert editor._code_dock.visible and panel._layout in editor._code_dock.objects
        assert panel._layout not in spec_tab(editor).objects
        assert type(panel._body).__name__ == "HSplit"

    def test_the_place_is_remembered_for_the_browser(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor.code_panel.toggle_dock()
        assert json.loads(editor._split_view.storage.value)["code_dock"] == "sidecar"
        other, query = make_editor()
        other._split_view._on_stored(editor._split_view.storage.value)
        other.open_code_panel(query, "expr")
        assert other.code_panel.dock == "sidecar"
        assert spec_tab(other).objects == [other.code_panel._layout]

    def test_docked_right_before_opens_in_the_spec_tab(self, setup):
        editor, query = setup
        editor._split_view._on_stored(json.dumps({"code_dock": "right"}))
        assert editor._split_view.preferences.code_dock == "sidecar"
        editor.open_code_panel(query, "expr")
        assert editor.code_panel.dock == "sidecar"

    def test_a_narrower_window_keeps_the_choice_of_under_the_canvas(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        editor._split_view.storage.window_width = 800
        assert panel.dock == "sidecar" and not panel.dock_button.visible
        assert editor._split_view.preferences.code_dock == "bottom"
        editor._split_view.storage.window_width = 1440
        assert panel.dock == "bottom" and editor._code_dock.visible

    def test_the_layout_tab_moves_it_into_the_spec_tab_and_back(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year > 2015"
        editor.mode = "dashboard"
        assert panel.dock == "sidecar" and panel.dock_button.visible
        assert panel.code.value == "p_year > 2015" and panel.dirty
        editor._inspector.open_tab(query, "Spec")
        assert spec_tab(editor).objects == [panel._layout]
        # Open below goes back to the canvas.
        panel.dock_button.clicks += 1
        assert editor.mode == "split"
        assert panel.dock == "bottom" and editor._code_dock.visible
        assert editor._split_view.preferences.code_dock == "bottom"

    def test_the_inspector_says_where_the_panel_is(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor.open_code_panel(query, "expr")
        caption = next(
            iter(
                pn.Column(*editor._inspector._body).select(
                    lambda o: "fd-spec-in-panel-caption" in getattr(o, "css_classes", [])
                )
            )
        )
        assert "under the canvas" in caption.object
        editor.code_panel.toggle_dock()
        assert "Spec tab" in caption.object

    def test_showing_the_spec_tab_opens_the_panel_on_the_node(self, setup):
        editor, query = setup
        editor._split_view.storage.window_width = 800
        select_on_canvas(editor._flow, node_id=query)
        assert not editor.code_panel_open
        editor._inspector.open_tab(query, "Spec")
        assert editor.code_panel.instance_id == query
        assert spec_tab(editor).objects == [editor.code_panel._layout]

    def test_the_spec_tab_offers_to_open_a_spec_under_the_canvas_here(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor._inspector.open_tab(query, "Spec")
        assert not editor.code_panel_open
        note = spec_tab(editor)[0]
        buttons = {b.label: b for b in note.select(pmui.Button)}
        assert set(buttons) == {"Open in sidecar", "Open below"}
        buttons["Open in sidecar"].clicks += 1
        assert editor.code_panel.dock == "sidecar"
        assert spec_tab(editor).objects == [editor.code_panel._layout]
        assert editor._split_view.preferences.code_dock == "sidecar"

    def test_one_editor_at_a_time(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        for _ in range(2):
            panel.toggle_dock()
            in_tab = panel._layout in spec_tab(editor).objects
            under = panel._layout in editor._code_dock.objects
            assert in_tab != under

    def test_the_draft_survives_the_move(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        panel.code.value_input = "p_year > 2015"
        panel.toggle_dock()
        assert panel.code.value == "p_year > 2015"
        assert panel.dirty
        panel.toggle_dock()
        assert panel.code.value == "p_year > 2015"
        assert panel.dirty

    def test_typed_text_is_kept_when_leaving_the_spec_tab(self, setup):
        editor, query = setup
        editor.open_spec_in_sidecar(query)
        panel = editor.code_panel
        panel.code.value_input = "p_year > 2015"
        editor._inspector._node_tabs.active = 0
        assert panel.code.value == "p_year > 2015"
        editor._inspector._node_tabs.active = 1
        assert spec_tab(editor).objects == [panel._layout] and panel.dirty

    def test_in_the_spec_tab_it_follows_the_selection(self, setup):
        editor, query = setup
        other = editor.add_component("D/query", position=(640, 0))
        editor.open_spec_in_sidecar(query)
        select_on_canvas(editor._flow, node_id=other)
        assert editor.code_panel.instance_id == other
        assert active_tab(editor) == "Spec"
        assert spec_tab(editor).objects == [editor.code_panel._layout]

    def test_a_definition_is_shown_read_only(self, setup):
        editor, _query = setup
        defined = editor.add_component("D/defined", position=(0, 300))
        editor._split_view.storage.window_width = 800
        select_on_canvas(editor._flow, node_id=defined)
        assert "Spec" in editor._inspector._tab_labels
        editor._inspector.open_tab(defined, "Spec")
        panel = editor.code_panel
        assert spec_tab(editor).objects == [panel._layout]
        assert panel.definition.visible and panel.definition.readonly
        assert "tables:" in panel.definition.value
        assert not panel.run_button.visible

    def test_no_spec_tab_without_a_spec_or_a_definition(self, setup):
        editor, _query = setup
        source = next(
            i["instance_id"] for i in editor._tile_items if i["component_id"] == "D/turbines"
        )
        select_on_canvas(editor._flow, node_id=source)
        assert "Spec" not in editor._inspector._tab_labels

    def test_no_spec_tab_for_viewers(self):
        editor, query = make_editor(editable=False)
        assert not editor._inspector._has_spec_tab(query, editor._component_specs["D/query"])


class Opaque:
    """An output no JSON pane can show, as a Lumen pipeline is."""

    def __repr__(self):
        return "Opaque()"


@register(
    page=False,
    component=True,
    title="Opaque query",
    icon="code",
    config=["expr"],
    requires=[{"key": "data", "type": "DataFrame", "required": True}],
    provides=[{"key": "rows", "type": "DataFrame"}, {"key": "pipeline"}, {"key": "result"}],
)
class OpaqueQuery(Viewer):
    data = param.DataFrame(default=None, allow_None=True)
    expr = param.String(
        default="p_year > 2014", metadata={"flowdash": {"widget": "code", "language": "python"}}
    )
    hook = True

    @param.output(param.DataFrame)
    @param.depends("data", "expr")
    def rows(self):
        return None if self.data is None else self.data.query(self.expr)

    @param.output(param.Parameter)
    def pipeline(self):
        return Opaque()

    @param.output(param.Parameter)
    def result(self):
        return {"pipeline": Opaque()}

    def __flowdash_preview__(self, output=None):
        if output == "pipeline" and self.hook:
            return TURBINES.head(2)
        return None

    def __panel__(self):
        return "opaque"


def make_opaque_editor():
    editor = FlowDash(
        {**COMPONENTS, "D/opaque": OpaqueQuery},
        notifications=False,
        include_builtin_components=False,
    )
    source = editor.add_component("D/turbines", position=(0, 0))
    node = editor.add_component("D/opaque", position=(320, 0))
    editor.connect(source, "data", node, "data")
    return editor, node


def _texts(panel):
    return " ".join(
        str(getattr(o, "object", ""))
        for o in pn.Column(*panel._result_body).select(lambda o: hasattr(o, "object"))
    )


class TestOutputPreviews:
    def test_the_dataframe_output_is_still_a_table(self):
        editor, node = make_opaque_editor()
        editor.open_code_panel(node, "expr")
        panel = editor.code_panel
        assert panel.outputs.value == "rows"
        assert "fd-code-table" in panel._result_body[0].css_classes

    def test_an_output_with_a_preview_hook_shows_its_preview(self):
        editor, node = make_opaque_editor()
        editor.open_code_panel(node, "expr")
        panel = editor.code_panel
        panel.outputs.value = "pipeline"
        assert not list(pn.Column(*panel._result_body).select(pn.pane.JSON))
        assert "2 rows" in _texts(panel)
        assert "fd-code-preview" in panel._result_body[0].css_classes

    @pytest.mark.parametrize("output", ["pipeline", "result"])
    def test_a_value_json_cannot_show_gets_a_caption(self, output):
        editor, node = make_opaque_editor()
        editor._component_instances[node].hook = False
        editor.open_code_panel(node, "expr")
        panel = editor.code_panel
        panel.outputs.value = output
        assert not list(pn.Column(*panel._result_body).select(pn.pane.JSON))
        # Named by its type: an object's own repr may hold secrets.
        assert "Opaque" in _texts(panel)
        assert "Opaque()" not in _texts(panel)
        panel.outputs.value = "rows"
        assert "fd-code-table" in panel._result_body[0].css_classes
