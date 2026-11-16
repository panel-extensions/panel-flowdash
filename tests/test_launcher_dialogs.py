"""Tests for the launcher's create, rename and delete dashboard dialogs."""

import sys

import panel as pn
import pytest
from panel.util import edit_readonly

from panel_flowdash.app import FlowDashApp
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    MemoryDashboardStore,
)
from tests.test_navigation import FakeLocation, _create_project


class RecordingStore(MemoryDashboardStore):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def rename_dashboard(self, user_id, dashboard_id, new_title):
        self.calls.append("rename")
        return super().rename_dashboard(user_id, dashboard_id, new_title)

    def delete_dashboard(self, user_id, dashboard_id):
        self.calls.append("delete")
        return super().delete_dashboard(user_id, dashboard_id)


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        instance = FlowDashApp(project_dir=tmp_path, store=RecordingStore())
        await instance._ensure_components_loaded()
        instance._notify = lambda *args, **kwargs: None
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _type(widget, text):
    """Set ``value_input`` the way a keystroke in the browser does."""
    with edit_readonly(widget):
        widget.value_input = text


def _dashboard(app, title="Sales"):
    return app.store.create_dashboard(app._user_id, title)


def _error(app):
    return app._dialog_name_input.error_state, app._dialog_name_input.helper_text


class TestRenameDialog:
    async def test_unchanged_name_is_not_a_duplicate(self, app):
        dash = _dashboard(app)
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        assert _error(app) == (False, "")
        assert app._dialog_confirm_button.disabled is False

    async def test_unchanged_name_is_valid_while_editing(self, app):
        dash = _dashboard(app)
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        _type(app._dialog_name_input, "Sale")
        _type(app._dialog_name_input, "Sales")
        assert _error(app) == (False, "")
        assert app._dialog_confirm_button.disabled is False

    async def test_rename_after_create_dialog_is_valid(self, app):
        dash = _dashboard(app)
        app._open_create_dialog()
        app._dialog.open = False
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        assert _error(app) == (False, "")

    async def test_confirm_with_unchanged_name_closes_without_store_call(self, app):
        dash = _dashboard(app)
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        app._on_dialog_confirm(None)
        assert app._dialog.open is False
        assert app.store.calls == []

    async def test_other_dashboard_name_is_a_duplicate(self, app):
        dash = _dashboard(app)
        _dashboard(app, "Costs")
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        _type(app._dialog_name_input, "Costs")
        assert _error(app) == (True, "A dashboard with this name already exists.")
        assert app._dialog_confirm_button.disabled is True

    async def test_title_and_action(self, app):
        dash = _dashboard(app)
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        assert app._dialog.title == "Rename dashboard"
        assert app._dialog_confirm_button.label == "Rename"
        assert app._dialog_confirm_button.color == "primary"


class TestCreateDialog:
    async def test_no_error_before_typing(self, app):
        dash = _dashboard(app)
        app._open_rename_dialog(dash.dashboard_id, "Sales")
        app._dialog.open = False
        app._open_create_dialog()
        assert _error(app) == (False, "")
        assert app._dialog_name_input.value == ""

    async def test_error_after_clearing_typed_name(self, app):
        app._open_create_dialog()
        _type(app._dialog_name_input, "S")
        _type(app._dialog_name_input, "")
        assert _error(app) == (True, "Name cannot be empty.")

    async def test_confirm_disabled_until_name_is_valid(self, app):
        _dashboard(app)
        app._open_create_dialog()
        assert app._dialog_confirm_button.disabled is True
        _type(app._dialog_name_input, "Sales")
        assert app._dialog_confirm_button.disabled is True
        _type(app._dialog_name_input, "Sales 2")
        assert app._dialog_confirm_button.disabled is False
        _type(app._dialog_name_input, "   ")
        assert app._dialog_confirm_button.disabled is True

    async def test_enter_submits_valid_name(self, app):
        app._open_create_dialog()
        _type(app._dialog_name_input, "Fresh")
        app._dialog_name_input.param.trigger("enter_pressed")
        assert app._dialog.open is False
        assert app._current_dashboard.title == "Fresh"

    async def test_enter_ignores_invalid_name(self, app):
        _dashboard(app)
        app._open_create_dialog()
        _type(app._dialog_name_input, "Sales")
        app._dialog_name_input.param.trigger("enter_pressed")
        assert app._dialog.open is True
        assert [d.title for d in app.store.list_dashboards(app._user_id)] == ["Sales"]

    async def test_title_and_action(self, app):
        app._open_create_dialog()
        assert app._dialog.title == "Create dashboard"
        assert app._dialog_confirm_button.label == "Create"
        assert app._dialog_name_input.visible is True
        assert app._dialog_delete_input.visible is False


class TestDeleteDialog:
    async def test_is_a_typed_confirmation(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        assert app._dialog.title == "Delete Sales?"
        assert app._dialog_name_input.visible is False
        assert app._dialog_delete_input.visible is True
        assert app._dialog_delete_input.label == "Type the name to confirm"
        assert app._dialog_delete_input.value == ""
        assert app._dialog_confirm_button.label == "Delete dashboard"
        assert app._dialog_confirm_button.color == "error"
        assert app._dialog_confirm_button.disabled is True

    async def test_consequence_counts_components_and_connections(self, app):
        dash = _dashboard(app)
        dash.items = [
            DashboardItem(instance_id="a", component_id="x"),
            DashboardItem(instance_id="b", component_id="y"),
        ]
        dash.edges = [DashboardEdge("a", "out", "b", "in")]
        app.store.save_dashboard(dash)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        assert app._dialog_message.object == (
            "Its 2 components and 1 connection are removed for everyone it is "
            "shared with. This can't be undone."
        )
        assert app._dialog_message.visible is True

    async def test_consequence_for_empty_dashboard(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        assert app._dialog_message.object == (
            "It is removed for everyone it is shared with. This can't be undone."
        )

    async def test_enabled_only_on_exact_match(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        for text, disabled in (("sales", True), ("Sales ", True), ("Sales", False), ("", True)):
            _type(app._dialog_delete_input, text)
            assert app._dialog_confirm_button.disabled is disabled, text

    async def test_confirm_without_match_does_not_delete(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        _type(app._dialog_delete_input, "Sal")
        app._on_dialog_confirm(None)
        app._dialog_delete_input.param.trigger("enter_pressed")
        assert app.store.calls == []
        assert app._dialog.open is True

    async def test_enter_deletes_on_match(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        _type(app._dialog_delete_input, "Sales")
        app._dialog_delete_input.param.trigger("enter_pressed")
        assert app.store.calls == ["delete"]
        assert app._dialog.open is False
        assert app.store.list_dashboards(app._user_id) == []

    async def test_reopening_create_restores_name_field(self, app):
        dash = _dashboard(app)
        app._open_delete_dialog(dash.dashboard_id, "Sales")
        app._dialog.open = False
        app._open_create_dialog()
        assert app._dialog_name_input.visible is True
        assert app._dialog_delete_input.visible is False
        assert app._dialog_message.visible is False
        assert app._dialog_confirm_button.color == "primary"
