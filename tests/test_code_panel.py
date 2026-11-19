"""The code panel docked under the canvas: draft, Run, problems on their lines and the result."""

import asyncio
import re
import time

import pandas as pd
import panel as pn
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


COMPONENTS = {"D/turbines": Turbines, "D/query": Query, "D/note": Note}


def make_editor(**params):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False, **params)
    source = editor.add_component("D/turbines", position=(0, 0))
    query = editor.add_component("D/query", position=(320, 0))
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

    def test_the_canvas_sits_above_the_panel(self, setup):
        editor, query = setup
        stack = editor._workspace_area[0]
        assert stack is editor._canvas_stack
        assert not editor._code_dock.visible
        editor.open_code_panel(query, "expr")
        assert editor._workspace_area[0] is stack
        assert list(stack) == [editor._flow, editor._code_dock]
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

    def test_split_tab_docks_under_the_canvas_pane(self, setup):
        editor, query = setup
        editor.mode = "split"
        editor.open_code_panel(query, "expr")
        assert editor._split_view.left[0] is editor._canvas_stack
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

    def test_not_in_the_dashboard_tab(self, setup):
        editor, query = setup
        editor.mode = "dashboard"
        assert not editor.open_code_panel(query, "expr")

    def test_not_in_a_narrow_window(self, setup):
        editor, query = setup
        editor._split_view.storage.window_width = 800
        assert not editor.open_code_panel(query, "expr")

    def test_a_narrowed_window_closes_it(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        editor._split_view.storage.window_width = 800
        assert not editor.code_panel_open

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

    def test_other_fields_are_in_the_header(self, setup):
        editor, query = setup
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        assert panel._others == ["limit"]
        assert panel._fields_state.limit == 100

    def test_switching_nodes_shows_the_other_text(self, setup):
        editor, query = setup
        note = editor.add_component("D/note", position=(0, 300))
        editor.open_code_panel(query, "expr")
        editor.open_code_panel(note, "text")
        assert editor.code_panel.code.value == "# Hi"
        assert editor.code_panel._others == []


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

    async def test_run_applies_the_header_fields_as_one_edit(self, setup, monkeypatch):
        editor, query = setup
        recorded = []
        record = editor._record_edit
        monkeypatch.setattr(
            editor, "_record_edit", lambda *a, **k: (recorded.append(k), record(*a, **k))[1]
        )
        editor.open_code_panel(query, "expr")
        panel = editor.code_panel
        await settle(panel)
        panel._fields_state.limit = 2
        panel.code.value_input = "p_year > 2015"
        assert editor._component_instances[query].limit == 100
        panel.run()
        await settle(panel)
        instance = editor._component_instances[query]
        assert (instance.expr, instance.limit) == ("p_year > 2015", 2)
        assert len(rows(editor, query)) == 2
        assert [k["args"]["keys"] for k in recorded] == [["expr", "limit"]]

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


class TestInspector:
    def test_the_expand_button_opens_the_panel(self, setup):
        editor, query = setup
        select_on_canvas(editor._flow, node_id=query)
        field = next(iter(pn.Column(*editor._inspector._body).select(CodeField)))
        field._open()
        assert editor.code_panel_open
        assert editor.code_panel.field == "expr"
        assert not field._dialog.open

    def test_it_falls_back_to_the_dialog(self, setup):
        editor, query = setup
        editor._split_view.storage.window_width = 700
        select_on_canvas(editor._flow, node_id=query)
        field = next(iter(pn.Column(*editor._inspector._body).select(CodeField)))
        field._open()
        assert not editor.code_panel_open
        assert field._dialog.open
