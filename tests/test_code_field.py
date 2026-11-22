"""Config fields marked as code are edited in a compact code editor."""

import time

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.choice_widgets import plain_widget
from panel_flowdash.ui.code_field import CodeField, code_language
from panel_flowdash.ui.status_bar import select_on_canvas

from .test_config_choices import _settle

SQL = {"flowdash": {"widget": "code", "language": "sql"}}


@register(page=False, component=True, title="Query", config=["sql", "notes", "title"])
class Query(Viewer):
    sql = param.String(default="SELECT 1", metadata=SQL)
    notes = param.String(default=None, allow_None=True, metadata={"flowdash": {"widget": "code"}})
    title = param.String(default="")

    def __panel__(self):
        return self.sql


def checked(config, inputs):
    if "SLOW" in config["sql"]:
        time.sleep(0.3)
    return ["SQL must start with SELECT"] if not config["sql"].startswith("SELECT") else []


@register(page=False, component=True, title="Checked query", config=["sql"], check_config=checked)
class CheckedQuery(Viewer):
    sql = param.String(default="SELECT 1", metadata=SQL)

    def __panel__(self):
        return self.sql


@register(
    page=False,
    component=True,
    title="Schema query",
    config_schema={
        "properties": {
            "sql": {"type": "string", "default": "", "x-flowdash": SQL["flowdash"]},
            "limit": {"type": "integer", "default": 10},
        }
    },
)
def schema_query(config):
    return "schema"


def custom_form(values, schema, *, id, type, on_patch):
    field = CodeField(label="Sql", language="sql", value=values["sql"])
    field.param.watch(lambda event: on_patch({"sql": event.new}), "value")
    return pn.Column(field)


@register(
    page=False,
    component=True,
    title="Custom form query",
    config=["sql"],
    config_editor=custom_form,
    check_config=checked,
)
class CustomFormQuery(Viewer):
    sql = param.String(default="SELECT 1", metadata=SQL)

    def __panel__(self):
        return self.sql


COMPONENTS = {
    "D/query": Query,
    "D/checked": CheckedQuery,
    "D/schema": schema_query,
    "D/custom_form": CustomFormQuery,
}


@pytest.fixture
async def editor():
    editor = FlowDash(
        COMPONENTS,
        notifications=False,
        include_builtin_components=False,
        store=MemoryDashboardStore(),
        history_coalesce=0,
    )
    editor.new_dashboard("Queries")
    yield editor
    editor.dispose()


def _field(editor, node_id, name):
    """Return the code field the inspector shows where the code panel cannot edit it."""
    editor._code_panel_allowed = lambda: False
    select_on_canvas(editor._flow, node_id=node_id)
    fields = [
        w
        for w in pn.Column(*editor._inspector._body).select(CodeField)
        if name in (w.name, w.label)
    ]
    assert len(fields) == 1
    return fields[0]


class TestLanguage:
    def test_metadata_picks_the_language(self):
        assert code_language(Query.param.sql) == "sql"
        assert code_language(Query.param.notes) == "text"
        assert code_language(Query.param.title) is None
        assert code_language(param.List(metadata=SQL)) is None

    def test_a_code_field_gets_a_code_editor(self):
        assert plain_widget(Query.param.sql, "") == {"type": CodeField, "language": "sql"}
        assert plain_widget(Query.param.title, "") is None

    def test_a_schema_property_declares_it_with_x_flowdash(self):
        spec = build_component_spec(RegistryEntry.from_app(schema_query, app_id="D/schema"))
        state = spec.config_state_class
        assert code_language(state.param.sql) == "sql"
        assert code_language(state.param.limit) is None


class TestInspector:
    async def test_a_wide_window_edits_the_code_in_the_panel(self, editor):
        node = editor.add_component("D/query")
        select_on_canvas(editor._flow, node_id=node)
        body = pn.Column(*editor._inspector._body)
        assert not list(body.select(CodeField))
        button = next(b for b in body.select(pmui.IconButton) if "fd-edit-spec" in b.css_classes)
        button.clicks += 1
        assert (editor.code_panel.instance_id, editor.code_panel.field) == (node, "sql")
        editor.code_panel.field_toggle.value = "notes"
        assert editor.code_panel.field == "notes"

    async def test_editing_the_code_applies_the_config(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "SQL")
        assert field._large.language == "sql" and field._large.value == "SELECT 1"
        assert "SELECT" in field._snippet.object and "1 line" in field._status_html.object
        assert not field._large.on_keyup, "applied once editing ends, not per key"
        field._large.value = "SELECT 2\nFROM t"
        assert editor._component_instances[node].sql == "SELECT 2\nFROM t"
        assert editor._tile_items[0]["config"]["sql"] == "SELECT 2\nFROM t"
        assert "2 lines" in field._status_html.object

    async def test_the_large_editor_edits_the_same_text(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "SQL")
        field.open_dialog()
        assert field._dialog.open and field._dialog.title == "SQL"
        field._large.value = "SELECT 3"
        assert "SELECT" in field._snippet.object and "3" in field._snippet.object
        assert editor._component_instances[node].sql == "SELECT 3"

    @pytest.mark.parametrize("panel_opens", [True, False])
    async def test_edit_opens_the_code_panel_or_else_the_dialog(self, editor, panel_opens):
        node = editor.add_component("D/query")
        calls = []

        def open_code_panel(instance_id, field_name):
            calls.append((instance_id, field_name))
            return panel_opens

        editor.open_code_panel = open_code_panel
        field = _field(editor, node, "SQL")
        field.edit_button.clicks += 1
        assert calls == [(node, "sql")]
        assert field._dialog.open is not panel_opens

    async def test_code_fields_of_a_custom_form_open_the_code_panel(self, editor):
        node = editor.add_component("D/custom_form")
        calls = []
        editor.open_code_panel = lambda *args: calls.append(args) or False
        field = _field(editor, node, "Sql")
        await _settle()
        assert field.check_status == ("ok", "Check passed")
        field.edit_button.clicks += 1
        assert calls == [(node, "sql")] and field._dialog.open

    async def test_snippet_caps_at_five_lines_and_shows_total(self, editor):
        field = CodeField(
            label="Sql",
            language="sql",
            value="\n".join(f"SELECT {i}" for i in range(1, 8)),
        )
        assert field._snippet.object.count("fd-snip-ln") == 5
        assert "7 lines" in field._status_html.object

    async def test_a_checked_field_applies_once_the_check_passes(self, editor):
        node = editor.add_component("D/checked")
        field = _field(editor, node, "SQL")
        await _settle()
        assert field.check_status == ("ok", "Check passed")
        assert "Check passed" in field._status_html.object
        field._large.value = "DROP TABLE t"
        await _settle()
        assert editor._component_instances[node].sql == "SELECT 1"
        assert editor._inspector._status_dot.title == "Edit not applied"
        assert field.check_status == ("error", "1 problem")
        assert "1 problem" in field._status_html.object
        field._large.value = "SELECT 2"
        await _settle()
        assert editor._component_instances[node].sql == "SELECT 2"
        assert field.check_status == ("ok", "Check passed")

    async def test_an_unset_optional_field_shows_empty_and_undo_unsets_it(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "Notes")
        assert field._large.value == "" and field.value is None
        assert "Empty" in field._snippet.object and "Empty" in field._status_html.object
        field._large.value = "remember"
        assert editor._component_instances[node].notes == "remember"
        assert editor.undo()
        field = _field(editor, node, "Notes")
        assert editor._component_instances[node].notes is None
        assert field._large.value == "" and "Empty" in field._snippet.object

    async def test_a_disabled_field_opens_read_only(self, editor):
        edits = []
        field = CodeField(label="Sql", value="SELECT 1", disabled=True, on_edit=edits.append)
        assert field._large.readonly
        assert field.edit_button.label == "View" and not field.edit_button.disabled
        field.edit_button.clicks += 1
        assert field._dialog.open and edits == []
        field.param.update(disabled=False)
        field._dialog.open = False
        assert not field._large.readonly and field.edit_button.label == "Edit"
        field.edit_button.clicks += 1
        assert edits == [field] and not field._dialog.open

    async def test_typed_text_offers_save_until_it_is_applied(self, editor):
        node = editor.add_component("D/query")
        editor.save()
        field = _field(editor, node, "SQL")
        assert editor._dashboard_bar.save_button.disabled
        field._large.value_input = "SELECT 2"
        assert field.unsent and not editor._dashboard_bar.save_button.disabled
        field._large.value = "SELECT 2"
        assert not field.unsent

    async def test_typing_then_reverting_no_longer_offers_save(self, editor):
        node = editor.add_component("D/query")
        editor.save()
        field = _field(editor, node, "SQL")
        assert field._large.value_input == "SELECT 1"
        field._large.value_input = "SELECT 1x"
        assert not editor._dashboard_bar.save_button.disabled
        field._large.value_input = "SELECT 1"
        assert not field.unsent and editor._dashboard_bar.save_button.disabled

    async def test_leaving_a_field_with_unsent_text_stops_offering_save(self, editor):
        node = editor.add_component("D/query")
        other = editor.add_component("D/schema")
        editor.save()
        field = _field(editor, node, "SQL")
        field._large.value_input = "SELECT 9"
        assert not editor._dashboard_bar.save_button.disabled
        select_on_canvas(editor._flow, node_id=other)
        assert editor._dashboard_bar.save_button.disabled

    async def test_save_waits_for_the_check_of_an_edit(self, editor):
        node = editor.add_component("D/checked")
        editor.save()
        field = _field(editor, node, "SQL")
        await _settle()
        field._large.value = "SELECT 42 -- SLOW"
        editor._save_from_button()
        await _settle(0.1)
        assert editor._save_button.loading, "Save shows it waits for the check"
        await _settle(0.8)
        assert not editor._save_button.loading
        stored = editor.store.load_dashboard(editor.user, editor.dashboard.dashboard_id)
        assert stored.items[0].config["sql"] == "SELECT 42 -- SLOW"
        assert not editor.dirty

    async def test_the_builtin_markdown_text_is_edited_as_markdown(self):
        editor = FlowDash(notifications=False)
        node = editor.add_component("Text/Markdown")
        field = _field(editor, node, "Text")
        assert field._large.language == "markdown"
        editor.dispose()

    async def test_a_schema_field_gets_the_editor(self, editor):
        node = editor.add_component("D/schema")
        field = _field(editor, node, "SQL")
        assert field._large.language == "sql"
