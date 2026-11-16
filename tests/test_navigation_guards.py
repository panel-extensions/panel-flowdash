"""Every navigation or mode change that would discard edits asks about them first."""

import sys

import panel as pn
import pytest
from panel.util import edit_readonly

from panel_flowdash.app import COMPONENTS_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore

from .test_navigation import FakeLocation, _create_project


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store, notifications=False)
        await instance._ensure_components_loaded()
        # Navigations run the page load synchronously, so the tests can await it.
        instance._loads = []
        instance._load_page_layout = lambda: instance._loads.append(pn.state.location.pathname)
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _open(app, title="Plant", edit=True):
    model = app.store.create_dashboard(app._user_id, title)
    pn.state.location.param.update(
        pathname=f"{DASH_ROUTE_PREFIX}{model.dashboard_id}", search="?edit=true" if edit else ""
    )
    app._load_dashboard_sync(model.dashboard_id, edit=edit)
    return model


def _palette_subtitles(app, group="Components"):
    return {i["subtitle"] for i in app._command_palette.items() if i["group"] == group}


async def _arrive(app, path):
    pn.state.location.param.update(pathname=path, search="")
    await FlowDashApp._load_page_layout(app)


class TestLeavingADashboardLeavesEditMode:
    @pytest.mark.parametrize("path", ["/", COMPONENTS_ROUTE, "/Analytics/page", "/nowhere"])
    async def test_route_without_a_dashboard(self, app, path):
        _open(app)
        assert app._editor.editable

        await _arrive(app, path)

        assert app._current_dashboard is None
        assert not app._editor.editable
        assert "Add to this dashboard" not in _palette_subtitles(app)

    async def test_missing_dashboard(self, app):
        _open(app)

        await _arrive(app, f"{DASH_ROUTE_PREFIX}000000000000")

        assert app._current_dashboard is None
        assert not app._editor.editable

    async def test_palette_component_is_not_added_to_the_hidden_canvas(self, app):
        _open(app)
        await _arrive(app, "/")

        app._command_palette.run("component:Analytics/selector")

        assert app._editor._tile_items == []
        assert not app._dirty
        assert app._loads == [COMPONENTS_ROUTE]

    async def test_palette_only_adds_with_a_dashboard_open(self, app):
        app._editor.param.update(editable=True, read_only=False)
        assert app._current_dashboard is None
        assert not app._command_palette._editing()


class TestCurrentDashboardWhileEditing:
    @pytest.mark.parametrize("entry", ["palette", "switcher", "menu"])
    async def test_unsaved_changes_ask_first(self, app, entry):
        model = _open(app)
        path = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        app._editor.add_component("Analytics/selector")

        if entry == "palette":
            app._command_palette.run(f"dashboard:{model.dashboard_id}")
        elif entry == "switcher":
            app._shell._on_switch({"label": "Plant", "path": path})
        else:
            app._on_nav_click({"path": path})

        assert app._unsaved_dialog.open
        assert app._unsaved_view.changes == [("add_circle", "Added selector")]
        assert app._pending_navigation == (path, "")
        assert app._editor.editable
        assert app._dirty

    async def test_leave_reloads_the_saved_dashboard_in_view_mode(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")
        app._command_palette.run(f"dashboard:{model.dashboard_id}")

        app._unsaved_view.leave_button.clicks += 1

        assert not app._dirty
        assert pn.state.location.search == ""
        assert app._loads == [f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"]

    async def test_keep_editing_stays(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")
        app._command_palette.run(f"dashboard:{model.dashboard_id}")

        app._unsaved_view.stay_button.clicks += 1

        assert app._editor.editable
        assert app._dirty
        assert pn.state.location.search == "?edit=true"
        assert app._pending_navigation is None
        assert app._loads == []

    async def test_without_changes_switches_to_view_mode(self, app):
        model = _open(app)

        app._command_palette.run(f"dashboard:{model.dashboard_id}")

        assert not app._unsaved_dialog.open
        assert not app._editor.editable
        assert pn.state.location.search == ""

    async def test_in_view_mode_does_nothing(self, app):
        model = _open(app, edit=False)

        app._command_palette.run(f"dashboard:{model.dashboard_id}")

        assert not app._unsaved_dialog.open
        assert app._loads == []

    async def test_edit_action_on_the_dashboard_being_edited_does_nothing(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")

        app._on_dashboard_action("Edit", model.dashboard_id, model.title)

        assert not app._unsaved_dialog.open
        assert app._dirty
        assert app._loads == []


def _confirm_create(app, title):
    app._open_create_dialog()
    with edit_readonly(app._dialog_name_input):
        app._dialog_name_input.value_input = title
    app._dialog_confirm_button.clicks += 1


class TestCreateWithUnsavedChanges:
    async def test_asks_before_creating(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")

        _confirm_create(app, "Depot")

        assert not app._dialog.open
        assert app._unsaved_dialog.open
        assert app._unsaved_view.changes == [("add_circle", "Added selector")]
        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert not app.store.title_exists(app._user_id, "Depot")

    async def test_keep_editing_creates_nothing(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")
        _confirm_create(app, "Depot")

        app._unsaved_view.stay_button.clicks += 1

        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert app._dirty
        assert len(app._editor._tile_items) == 1
        assert not app.store.title_exists(app._user_id, "Depot")

    async def test_leave_creates_and_opens_the_new_dashboard(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")
        _confirm_create(app, "Depot")

        app._unsaved_view.leave_button.clicks += 1

        assert app._current_dashboard.title == "Depot"
        assert app._editor._tile_items == []
        assert app.store.load_dashboard(app._user_id, model.dashboard_id).items == []

    async def test_save_stores_the_edits_then_creates(self, app):
        model = _open(app)
        app._editor.add_component("Analytics/selector")
        _confirm_create(app, "Depot")

        app._unsaved_view.save_button.clicks += 1

        assert app._current_dashboard.title == "Depot"
        stored = app.store.load_dashboard(app._user_id, model.dashboard_id)
        assert [item.component_id for item in stored.items] == ["Analytics/selector"]

    async def test_without_changes_creates_right_away(self, app):
        _open(app)

        _confirm_create(app, "Depot")

        assert not app._unsaved_dialog.open
        assert app._current_dashboard.title == "Depot"


class TestLeavingThePage:
    async def test_browser_asks_only_with_unsaved_changes(self, app):
        guard = app._leave_page_guard
        assert guard in app._page_dialogs
        assert not guard.active

        _open(app)
        app._editor.add_component("Analytics/selector")
        assert guard.active

        app._save_current_dashboard()
        assert not guard.active

        app._editor.add_component("Analytics/chart")
        assert guard.active
        app._unsaved_view.show([])
        app._unsaved_view.leave_button.clicks += 1
        assert not guard.active
