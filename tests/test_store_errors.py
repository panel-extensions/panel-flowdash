"""Tests for how the app shell reports a store that refuses or fails an operation."""

import logging
import sys

import panel as pn
import pytest

from panel_flowdash import DashboardStoreError, JSONFolderDashboardStore, Permission
from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.editor import FlowDash
from tests.test_navigation import FakeLocation, _create_project

REFUSAL = "Dashboards cannot be changed right now."
LOGGED = "Could not {} the dashboard. The error has been logged."


class RefusingStore(MemoryDashboardStore):
    """Raises ``error`` from every store operation named in ``refuse``."""

    def __init__(self):
        super().__init__()
        self.refuse: set[str] = set()
        self.error: Exception = DashboardStoreError(REFUSAL)

    def _check(self, operation):
        if operation in self.refuse:
            raise self.error

    def get_owner(self, dashboard_id):
        self._check("get_owner")
        return super().get_owner(dashboard_id)

    def create_dashboard(self, user_id, title):
        self._check("create")
        return super().create_dashboard(user_id, title)

    def save_dashboard(self, dashboard):
        self._check("save")
        super().save_dashboard(dashboard)

    def delete_dashboard(self, user_id, dashboard_id):
        self._check("delete")
        return super().delete_dashboard(user_id, dashboard_id)

    def rename_dashboard(self, user_id, dashboard_id, new_title):
        self._check("rename")
        return super().rename_dashboard(user_id, dashboard_id, new_title)

    def set_permission(self, dashboard_id, permission):
        self._check("share")
        return super().set_permission(dashboard_id, permission)


class DecliningStore(MemoryDashboardStore):
    """Reports failure through the return value instead of raising."""

    def delete_dashboard(self, user_id, dashboard_id):
        return False

    def rename_dashboard(self, user_id, dashboard_id, new_title):
        return False

    def set_permission(self, dashboard_id, permission):
        return False


class VanishingStore(MemoryDashboardStore):
    """Loses the dashboard to another session between the access check and the write."""

    def delete_dashboard(self, user_id, dashboard_id):
        super().delete_dashboard(user_id, dashboard_id)
        return False

    def rename_dashboard(self, user_id, dashboard_id, new_title):
        super().delete_dashboard(user_id, dashboard_id)
        return False


@pytest.fixture
def location():
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        yield pn.state._location
    finally:
        pn.state._location = previous


@pytest.fixture
def make_app(tmp_path, location):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))

    async def make(store):
        instance = FlowDashApp(project_dir=tmp_path, store=store)
        await instance._ensure_components_loaded()
        instance.notices = []
        notify = lambda severity, message, duration=3000: instance.notices.append(
            (severity, message)
        )
        instance._notify = notify
        instance._editor._notify = notify
        return instance

    try:
        yield make
    finally:
        sys.path.remove(str(tmp_path))


async def _app_with_dashboard(make_app, store, title="Sales"):
    app = await make_app(store)
    model = store.create_dashboard(app._user_id, title)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    app.notices.clear()
    return app, model


def _menu_labels(app):
    return [item["label"] for item in app._menu_list.items[-1]["items"]]


class TestCreateRefused:
    async def test_refusal_is_shown_and_state_is_kept(self, make_app, location):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._editor.add_component("Analytics/selector")
        sidebar = list(app._sidebar_container.objects)
        menu = app._menu_list.items
        main = list(app._page.main)
        location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        store.refuse.add("create")

        app._create_new_dashboard("Fresh")

        assert app.notices == [("error", REFUSAL)]
        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert len(app._editor._flow.nodes) == 1
        assert app._dirty
        assert list(app._sidebar_container.objects) == sidebar
        assert app._menu_list.items == menu
        assert list(app._page.main) == main
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        assert [m.title for m in store.list_dashboards(app._user_id)] == ["Sales"]

    async def test_unexpected_error_is_logged_with_traceback(self, make_app, caplog):
        store = RefusingStore()
        store.refuse.add("create")
        store.error = OSError("disk full")
        app = await make_app(store)

        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            assert app._create_new_dashboard("Fresh") is False

        assert app.notices == [("error", LOGGED.format("create"))]
        assert "disk full" not in app.notices[0][1]
        record = next(r for r in caplog.records if r.message == "Failed to create dashboard")
        assert record.exc_info[1] is store.error
        assert app._current_dashboard is None

    async def test_refusal_is_logged_at_info_only(self, make_app, caplog):
        store = RefusingStore()
        store.refuse.add("create")
        app = await make_app(store)

        with caplog.at_level(logging.INFO, logger="panel_flowdash"):
            app._create_new_dashboard("Fresh")

        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.INFO, f"Store refused to create dashboard: {REFUSAL}")
        ]

    async def test_read_only_json_folder_refusal_is_shown(self, make_app, tmp_path):
        app = await make_app(JSONFolderDashboardStore(tmp_path / "dashboards", read_only=True))

        app._create_new_dashboard("Fresh")

        assert app.notices == [("error", "Dashboards are read-only on this server.")]
        assert app._current_dashboard is None


class TestDashboardDialog:
    async def test_create_refusal_keeps_dialog_open_with_inline_error(self, make_app):
        store = RefusingStore()
        app = await make_app(store)
        app._open_create_dialog()
        app._dialog_name_input.value = "Fresh"
        store.refuse.add("create")

        app._on_dialog_confirm(None)

        assert app.notices == [("error", REFUSAL)]
        assert app._dialog.open
        assert app._dialog_context == {"action": "create"}
        assert app._dialog_name_input.error_state
        assert app._dialog_name_input.helper_text == REFUSAL

    async def test_create_closes_dialog_on_success(self, make_app):
        app = await make_app(RefusingStore())
        app._open_create_dialog()
        app._dialog_name_input.value = "Fresh"

        app._on_dialog_confirm(None)

        assert not app._dialog.open
        assert app._dialog_context == {}
        assert app._current_dashboard.title == "Fresh"

    async def test_rename_refusal_keeps_dialog_open_with_inline_error(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._open_rename_dialog(model.dashboard_id, "Sales")
        app._dialog_name_input.value = "Renamed"
        store.refuse.add("rename")

        app._on_dialog_confirm(None)

        assert app._dialog.open
        assert app._dialog_context == {"action": "rename", "dashboard_id": model.dashboard_id}
        assert app._dialog_name_input.error_state
        assert app._dialog_name_input.helper_text == REFUSAL

    async def test_rename_closes_dialog_on_success(self, make_app):
        app, model = await _app_with_dashboard(make_app, RefusingStore())
        app._open_rename_dialog(model.dashboard_id, "Sales")
        app._dialog_name_input.value = "Renamed"

        app._on_dialog_confirm(None)

        assert not app._dialog.open
        assert app._dialog_context == {}
        assert app._current_dashboard.title == "Renamed"

    async def test_delete_refusal_keeps_dialog_open(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._open_delete_dialog(model.dashboard_id, "Sales")
        app._dialog_delete_input.value = "Sales"
        store.refuse.add("delete")

        app._on_dialog_confirm(None)

        assert app.notices == [("error", REFUSAL)]
        assert app._dialog.open
        assert app._dialog_context == {"action": "delete", "dashboard_id": model.dashboard_id}


class TestRenameRefused:
    async def test_refusal_is_shown_and_title_is_kept(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        menu = app._menu_list.items
        store.refuse.add("rename")

        app._rename_dashboard(model.dashboard_id, "Renamed")

        assert app.notices == [("error", REFUSAL)]
        assert app._current_dashboard.title == "Sales"
        assert store.load_dashboard(app._user_id, model.dashboard_id).title == "Sales"
        assert app._menu_list.items == menu

    async def test_false_return_is_reported(self, make_app):
        app, model = await _app_with_dashboard(make_app, DecliningStore())

        app._rename_dashboard(model.dashboard_id, "Renamed")

        assert app.notices == [("error", "The dashboard could not be renamed.")]
        assert app._current_dashboard.title == "Sales"

    async def test_vanished_dashboard_leaves_the_menu(self, make_app):
        app, model = await _app_with_dashboard(make_app, VanishingStore())
        app._refresh_sidebar_dashboards()
        assert "Sales" in _menu_labels(app)

        assert app._rename_dashboard(model.dashboard_id, "Renamed") is False

        assert "Sales" not in _menu_labels(app)

    async def test_owner_lookup_failure_is_handled(self, make_app, caplog):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        store.refuse.add("get_owner")
        store.error = OSError("connection lost")

        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            assert app._rename_dashboard(model.dashboard_id, "Renamed") is False

        assert app.notices == [("error", LOGGED.format("rename"))]
        assert any(r.message == "Failed to rename dashboard" for r in caplog.records)
        assert app._current_dashboard.title == "Sales"


class TestDeleteRefused:
    async def test_refusal_is_shown_and_dashboard_stays_loaded(self, make_app, location):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._editor.add_component("Analytics/selector")
        location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        store.refuse.add("delete")

        app._delete_dashboard(model.dashboard_id)

        assert app.notices == [("error", REFUSAL)]
        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert len(app._editor._flow.nodes) == 1
        assert app._dirty
        assert store.load_dashboard(app._user_id, model.dashboard_id) is not None
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"

    async def test_false_return_is_reported(self, make_app):
        app, model = await _app_with_dashboard(make_app, DecliningStore())

        app._delete_dashboard(model.dashboard_id)

        assert app.notices == [("error", "The dashboard could not be deleted.")]
        assert app._current_dashboard.dashboard_id == model.dashboard_id

    async def test_vanished_dashboard_leaves_the_menu(self, make_app):
        app, model = await _app_with_dashboard(make_app, VanishingStore())
        app._refresh_sidebar_dashboards()
        assert "Sales" in _menu_labels(app)

        assert app._delete_dashboard(model.dashboard_id) is False

        assert "Sales" not in _menu_labels(app)

    async def test_owner_lookup_failure_is_handled(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        store.refuse.add("get_owner")

        assert app._delete_dashboard(model.dashboard_id) is False

        assert app.notices == [("error", REFUSAL)]
        assert store.load_dashboard(app._user_id, model.dashboard_id) is not None


class TestShareRefused:
    async def test_refusal_is_shown_and_dialog_stays_open(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._open_share_dialog(model.dashboard_id, model.title)
        app._editor._settings_view._add_input.value = "someone"
        store.refuse.add("share")

        app._editor._settings_view._save_button.clicks += 1

        assert app.notices == [("error", REFUSAL)]
        assert app._share_dialog.open
        assert app._editor._settings_view._target_id == model.dashboard_id
        assert app._current_dashboard.permission == Permission()
        assert store.load_dashboard(app._user_id, model.dashboard_id).permission == Permission()

    async def test_false_return_is_reported(self, make_app):
        app, model = await _app_with_dashboard(make_app, DecliningStore())
        app._open_share_dialog(model.dashboard_id, model.title)
        app._editor._settings_view._add_input.value = "someone"

        app._editor._settings_view._save_button.clicks += 1

        assert app.notices == [("error", "Sharing could not be updated.")]
        assert app._share_dialog.open
        assert app._current_dashboard.permission == Permission()


class TestSaveRefused:
    async def test_refusal_is_shown_and_canvas_stays_dirty(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._editor.add_component("Analytics/selector")
        store.refuse.add("save")

        assert app._save_current_dashboard() is False

        assert app.notices == [("error", REFUSAL)]
        assert app._dirty
        assert store.load_dashboard(app._user_id, model.dashboard_id).items == []

    async def test_save_and_continue_stays_when_save_is_refused(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._editor.add_component("Analytics/selector")
        navigated = []
        app._navigate_to = lambda path, search="": navigated.append(path)
        app._request_navigation("/")
        assert app._unsaved_dialog.open
        store.refuse.add("save")

        save_button = app._unsaved_view.save_button
        save_button.clicks += 1

        assert navigated == []
        assert app._pending_navigation is None
        assert app._dirty
        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert ("error", REFUSAL) in app.notices

    async def test_save_and_continue_navigates_after_saving(self, make_app):
        store = RefusingStore()
        app, _ = await _app_with_dashboard(make_app, store)
        app._editor.add_component("Analytics/selector")
        navigated = []
        app._navigate_to = lambda path, search="": navigated.append(path)
        app._request_navigation("/")

        save_button = app._unsaved_view.save_button
        save_button.clicks += 1

        assert navigated == ["/"]
        assert not app._dirty


class TestEditorSave:
    @pytest.fixture
    def editor(self):
        store = RefusingStore()
        editor = FlowDash({}, notifications=False, store=store, user="alice")
        editor.new_dashboard("Sales")
        editor.notices = []
        editor._notify = lambda severity, message, duration=3000: editor.notices.append(
            (severity, message)
        )
        return editor

    def test_runtime_error_from_store_is_logged_not_read_only(self, editor, caplog):
        editor.store.refuse.add("save")
        editor.store.error = RuntimeError("database is locked")

        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            assert editor._on_save_clicked() is False

        assert editor.notices == [("error", LOGGED.format("save"))]
        record = next(r for r in caplog.records if r.message == "Failed to save dashboard")
        assert record.exc_info[1] is editor.store.error

    def test_read_only_shows_read_only_message(self, editor, caplog):
        editor.read_only = True

        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            assert editor._on_save_clicked() is False

        assert editor.notices == [("error", "This dashboard is read-only.")]
        assert not caplog.records
