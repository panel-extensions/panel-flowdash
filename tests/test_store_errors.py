"""Tests for how the app shell reports a store that refuses or fails an operation."""

import logging
import sys

import panel as pn
import pytest

from panel_flowdash import DashboardStoreError, JSONFolderDashboardStore, Permission
from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import MemoryDashboardStore
from tests.test_navigation import FakeLocation, _create_project

REFUSAL = "Dashboards cannot be changed right now."


class RefusingStore(MemoryDashboardStore):
    """Raises ``error`` from every store operation named in ``refuse``."""

    def __init__(self):
        super().__init__()
        self.refuse: set[str] = set()
        self.error: Exception = DashboardStoreError(REFUSAL)

    def _check(self, operation):
        if operation in self.refuse:
            raise self.error

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
            app._create_new_dashboard("Fresh")

        assert app.notices == [("error", "Could not create the dashboard: disk full")]
        record = next(r for r in caplog.records if r.message == "Failed to create dashboard")
        assert record.exc_info[1] is store.error
        assert app._current_dashboard is None

    async def test_refusal_is_not_logged_as_an_error(self, make_app, caplog):
        store = RefusingStore()
        store.refuse.add("create")
        app = await make_app(store)

        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            app._create_new_dashboard("Fresh")

        assert not caplog.records

    async def test_read_only_json_folder_refusal_is_shown(self, make_app, tmp_path):
        app = await make_app(JSONFolderDashboardStore(tmp_path / "dashboards", read_only=True))

        app._create_new_dashboard("Fresh")

        assert app.notices == [("error", "Dashboards are read-only on this server.")]
        assert app._current_dashboard is None


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


class TestShareRefused:
    async def test_refusal_is_shown_and_dialog_stays_open(self, make_app):
        store = RefusingStore()
        app, model = await _app_with_dashboard(make_app, store)
        app._open_share_dialog(model.dashboard_id, model.title)
        app._share_allow_users.value = ["someone"]
        store.refuse.add("share")

        app._on_share_confirm(None)

        assert app.notices == [("error", REFUSAL)]
        assert app._share_dialog.open
        assert app._share_context == {"dashboard_id": model.dashboard_id}
        assert app._current_dashboard.permission == Permission()
        assert store.load_dashboard(app._user_id, model.dashboard_id).permission == Permission()

    async def test_false_return_is_reported(self, make_app):
        app, model = await _app_with_dashboard(make_app, DecliningStore())
        app._open_share_dialog(model.dashboard_id, model.title)
        app._share_allow_users.value = ["someone"]

        app._on_share_confirm(None)

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
        app._navigate_to = navigated.append
        app._request_navigation("/")
        assert app._unsaved_dialog.open
        store.refuse.add("save")

        save_button = app._unsaved_dialog.objects[0].objects[1].objects[0]
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
        app._navigate_to = navigated.append
        app._request_navigation("/")

        save_button = app._unsaved_dialog.objects[0].objects[1].objects[0]
        save_button.clicks += 1

        assert navigated == ["/"]
        assert not app._dirty
