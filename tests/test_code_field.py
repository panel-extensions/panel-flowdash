"""Config fields marked as code are edited in a compact code editor."""

import time

import panel as pn
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


COMPONENTS = {"D/query": Query, "D/checked": CheckedQuery, "D/schema": schema_query}


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
    select_on_canvas(editor._flow, node_id=node_id)
    fields = [w for w in pn.Column(*editor._inspector._body).select(CodeField) if w.name == name]
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
    async def test_editing_the_code_applies_the_config(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "Sql")
        assert field._editor.language == "sql" and field._editor.value == "SELECT 1"
        assert not field._editor.on_keyup, "applied once editing ends, not per key"
        field._editor.value = "SELECT 2\nFROM t"
        assert editor._component_instances[node].sql == "SELECT 2\nFROM t"
        assert editor._tile_items[0]["config"]["sql"] == "SELECT 2\nFROM t"

    async def test_the_large_editor_edits_the_same_text(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "Sql")
        field.expand_button.clicks += 1
        assert field._dialog.open and field._dialog.title == "Sql"
        field._large.value = "SELECT 3"
        assert field._editor.value == "SELECT 3"
        assert editor._component_instances[node].sql == "SELECT 3"

    async def test_a_checked_field_applies_once_the_check_passes(self, editor):
        node = editor.add_component("D/checked")
        field = _field(editor, node, "Sql")
        await _settle()
        field._editor.value = "DROP TABLE t"
        await _settle()
        assert editor._component_instances[node].sql == "SELECT 1"
        assert editor._inspector._status_chip.label == "Edit not applied"
        field._editor.value = "SELECT 2"
        await _settle()
        assert editor._component_instances[node].sql == "SELECT 2"

    async def test_an_unset_optional_field_shows_empty_and_undo_unsets_it(self, editor):
        node = editor.add_component("D/query")
        field = _field(editor, node, "Notes")
        assert field._editor.value == "" and field.value is None
        field._editor.value = "remember"
        assert editor._component_instances[node].notes == "remember"
        assert editor.undo()
        field = _field(editor, node, "Notes")
        assert editor._component_instances[node].notes is None
        assert field._editor.value == "" and field._large.value == ""

    async def test_a_disabled_field_is_read_only(self, editor):
        field = CodeField(label="Sql", value="SELECT 1", disabled=True)
        assert field._editor.readonly and field._large.readonly
        assert field.expand_button.disabled
        field.disabled = False
        assert not field._editor.readonly

    async def test_typed_text_offers_save_until_it_is_applied(self, editor):
        node = editor.add_component("D/query")
        editor.save()
        field = _field(editor, node, "Sql")
        assert editor._dashboard_bar.save_button.disabled
        field._editor.value_input = "SELECT 2"
        assert field.unsent and not editor._dashboard_bar.save_button.disabled
        field._editor.value = "SELECT 2"
        assert not field.unsent

    async def test_typing_then_reverting_no_longer_offers_save(self, editor):
        node = editor.add_component("D/query")
        editor.save()
        field = _field(editor, node, "Sql")
        for editor_ in (field._editor, field._large):
            assert editor_.value_input == "SELECT 1"
        field._editor.value_input = "SELECT 1x"
        assert not editor._dashboard_bar.save_button.disabled
        field._editor.value_input = "SELECT 1"
        assert not field.unsent and editor._dashboard_bar.save_button.disabled

    async def test_leaving_a_field_with_unsent_text_stops_offering_save(self, editor):
        node = editor.add_component("D/query")
        other = editor.add_component("D/schema")
        editor.save()
        field = _field(editor, node, "Sql")
        field._editor.value_input = "SELECT 9"
        assert not editor._dashboard_bar.save_button.disabled
        select_on_canvas(editor._flow, node_id=other)
        assert editor._dashboard_bar.save_button.disabled

    async def test_save_waits_for_the_check_of_an_edit(self, editor):
        node = editor.add_component("D/checked")
        editor.save()
        field = _field(editor, node, "Sql")
        await _settle()
        field._editor.value = "SELECT 42 -- SLOW"
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
        assert field._editor.language == "markdown"
        editor.dispose()

    async def test_a_schema_field_gets_the_editor(self, editor):
        node = editor.add_component("D/schema")
        field = _field(editor, node, "Sql")
        assert field._editor.language == "sql"
