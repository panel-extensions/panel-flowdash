"""Tests for the app shell (top bar, navigation rail) and the dashboard bar."""

import re
import sys

import panel as pn
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash.app import COMPONENTS_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore, MemoryDashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.shell import DOCS_URL, HEADER_SX, Crumb
from tests.test_editor import COMPONENTS, SELECTOR
from tests.test_navigation import FakeLocation, _create_project


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False)


@pytest.fixture
def store_editor():
    store = MemoryDashboardStore()
    editor = FlowDash(COMPONENTS, notifications=False, store=store, user="alice")
    editor.new_dashboard("Plant")
    editor.add_component(SELECTOR)
    editor.save()
    return editor


def _labels(editor):
    return [item["label"] for item in editor._dashboard_bar.overflow.items if item]


class TestDashboardBarSections:
    async def test_edit_section(self, editor):
        bar = editor._dashboard_bar
        assert bar._actions.objects[0] is bar.mode_tabs
        for item in (bar.settings_button, bar.overflow, bar.discard_button, bar.done_button):
            assert item in bar._actions.objects
        assert bar._actions.objects[-3:] == [
            editor._save_button,
            bar.done_button,
            bar.download_button,
        ]
        assert bar.lead.objects == [bar.title, bar.save_state]

    async def test_view_section(self, editor):
        editor.editable = False
        bar = editor._dashboard_bar
        assert bar._actions.objects == [
            bar.mode_toggle,
            bar.refresh_button,
            bar.refresh_period,
            bar.fullscreen_button,
            bar.overflow,
            bar.download_button,
        ]
        assert bar.lead.objects == [bar.title]

    async def test_header_placement_leaves_the_title_to_the_host(self, editor):
        editor.editable = False
        bar = editor._dashboard_bar
        bar.placement = "header"
        assert bar.title not in bar.lead.objects
        assert not bar.__panel__().visible
        assert bar.lead.visible and bar.actions.visible
        editor.toolbar = False
        assert not bar.lead.visible
        assert not bar.actions.visible

    async def test_view_section_of_a_read_only_dashboard(self, editor):
        editor.param.update(editable=False, read_only=True)
        bar = editor._dashboard_bar
        assert bar._actions.objects == [
            bar.refresh_button,
            bar.refresh_period,
            bar.fullscreen_button,
            bar.download_button,
        ]

    async def test_title_follows_the_dashboard(self, store_editor):
        assert store_editor._dashboard_bar.title.object == "Plant"

    async def test_untitled_without_a_dashboard(self, editor):
        assert editor._dashboard_bar.title.object == "Untitled dashboard"

    async def test_tabs_switch_the_editor_mode(self, editor):
        editor._dashboard_bar.mode_tabs.value = "dashboard"
        assert editor.mode == "dashboard"
        editor.mode = "wiring"
        assert editor._dashboard_bar.mode_tabs.value == "wiring"

    async def test_toolbar_false_hides_the_bar(self, editor):
        editor.toolbar = False
        assert not editor._dashboard_bar.__panel__().visible

    async def test_no_preview_switch(self, editor):
        assert not hasattr(editor, "_preview_switch")


class TestOverflow:
    async def test_edit_items(self, editor):
        assert _labels(editor) == ["Download", "Download as YAML", "Import file"]
        editor.add_component(SELECTOR)
        assert _labels(editor) == ["Download", "Download as YAML", "Import file", "Clear canvas"]
        assert editor._dashboard_bar.overflow.stylesheets

    async def test_view_items(self, editor):
        editor.editable = False
        assert _labels(editor) == ["Download", "Download as YAML", "Settings"]
        editor.read_only = True
        assert _labels(editor) == []

    async def test_view_settings_opens_the_general_section(self, editor):
        editor.editable = False
        editor._settings_view.section = "refresh"
        editor._dashboard_bar._on_overflow({"action": "settings"})
        assert editor._settings_dialog.open
        assert editor._settings_view.section == "general"

    async def test_download_sends_the_file(self, editor):
        editor.new_dashboard("Wind farm")
        editor._dashboard_bar._on_overflow({"action": "download"})
        assert editor._download_button.filename == "Wind_farm.json"
        assert editor._download_button.data.startswith("data:")


class TestSaveState:
    async def test_saved_state_after_loading(self, store_editor):
        store_editor.load("Plant")
        assert store_editor._dashboard_bar.save_state.object == "All changes saved"

    async def test_unsaved_changes(self, store_editor):
        store_editor.add_component(SELECTOR)
        assert store_editor._dashboard_bar.save_state.object == "Unsaved changes"
        assert not store_editor._save_button.disabled
        assert not store_editor._dashboard_bar.discard_button.disabled

    async def test_saved_time(self, store_editor):
        assert re.fullmatch(r"Saved \d\d:\d\d", store_editor._dashboard_bar.save_state.object)
        assert store_editor._save_button.disabled
        assert store_editor._dashboard_bar.discard_button.disabled

    async def test_read_only(self, store_editor):
        store_editor.read_only = True
        assert store_editor._dashboard_bar.save_state.object == "Read-only"
        assert store_editor._save_button.disabled

    async def test_read_only_without_a_dashboard_says_nothing(self, editor):
        editor.read_only = True
        assert editor._dashboard_bar.save_state.object == ""


class TestRearrangingIsAChange:
    async def test_moving_a_node(self, store_editor):
        store_editor._flow._emit("node_moved", {"type": "node_moved", "node_id": "x"})
        assert store_editor.dirty

    async def test_moving_a_node_in_view_mode_is_not(self, store_editor):
        store_editor.editable = False
        store_editor._flow._emit("node_moved", {"type": "node_moved", "node_id": "x"})
        assert not store_editor.dirty

    async def test_a_layout_from_the_browser(self, store_editor):
        store_editor.mode = "dashboard"
        grid = store_editor._tile_grid
        layout = [{"index": 0, "width": 50, "height": 400, "visible": True}]
        with set_curdoc(Document()):
            grid._process_events({"layout": layout})
        assert store_editor.dirty

    async def test_a_layout_set_by_the_editor_is_not(self, store_editor):
        store_editor.mode = "dashboard"
        store_editor.mode = "wiring"
        store_editor.mode = "dashboard"
        assert not store_editor.dirty


class TestModes:
    async def test_edit_request_without_handler(self, editor):
        editor.editable = False
        editor._dashboard_bar.mode_toggle.value = "edit"
        assert editor.editable
        assert editor._dashboard_bar.mode_toggle.value == "view"

    async def test_done_without_changes(self, editor):
        editor._dashboard_bar.done_button.clicks += 1
        assert not editor.editable

    async def test_done_with_changes_asks_first(self, editor):
        editor.add_component(SELECTOR)
        editor._dashboard_bar.done_button.clicks += 1
        assert editor.editable

    async def test_handler_receives_requests(self, editor):
        requests = []
        editor._dashboard_bar.on_mode = requests.append
        editor._dashboard_bar.done_button.clicks += 1
        editor.editable = False
        editor._dashboard_bar.mode_toggle.value = "edit"
        assert requests == ["view", "edit"]
        assert not editor.editable

    async def test_discard_reloads_the_saved_dashboard(self, store_editor):
        store_editor.add_component(SELECTOR)
        store_editor._dashboard_bar.discard()
        assert len(store_editor._tile_items) == 1
        assert not store_editor.dirty

    async def test_discard_without_a_store(self, editor):
        editor.add_component(SELECTOR)
        editor._dashboard_bar.discard()
        assert editor._tile_items == []
        assert not editor.dirty


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _open(app, title="Plant", edit=False):
    model = app.store.create_dashboard(app._user_id, title)
    path = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
    pn.state.location.pathname = path
    app._load_dashboard_sync(model.dashboard_id, edit=edit)
    return model


class TestAppShell:
    async def test_rail_is_the_default(self, app):
        assert app.nav_variant == "rail"
        assert app._page.header == app._shell.header
        assert app._shell.search_slot in app._page.header
        assert app._page.header_color == "paper"
        assert list(app._page.rail) == app._shell.rail_content
        for key, value in HEADER_SX.items():
            assert app._page.sx[key] == value

    async def test_rail_items(self, app):
        items = {item["key"]: item for item in app._shell.rail_items()}
        assert list(items) == ["home", "dashboards", "components", "pages", "help"]
        assert items["components"]["path"] == COMPONENTS_ROUTE
        assert [i["label"] for i in items["pages"]["items"]] == ["Overview"]
        assert items["help"]["href"] == DOCS_URL

    async def test_dashboards_become_a_menu_with_a_home_dashboard(self, app):
        _open(app)
        app.home_dashboard = "Plant"
        items = {item["key"]: item for item in app._shell.rail_items()}
        assert [i["label"] for i in items["dashboards"]["items"]] == ["Plant"]

    @pytest.mark.parametrize(
        ("path", "active"),
        [("/", "home"), (COMPONENTS_ROUTE, "components"), ("/Analytics/page", "pages")],
    )
    async def test_active_rail_item(self, app, path, active):
        app._shell.sync(path)
        assert app._shell._active() == active
        sx = app._shell._rail_buttons[active].sx
        assert sx["& .MuiButton-startIcon"]["color"] == "primary.main"

    async def test_dashboard_crumbs_and_switcher(self, app):
        _open(app, "Depot")
        model = _open(app, "Plant")
        shell = app._shell
        assert shell._active() == "dashboards"
        assert shell.crumbs() == [Crumb("Dashboards", "/"), Crumb("Plant")]
        assert shell.dashboard_switcher in shell.breadcrumb.objects
        labels = [i["label"] for i in shell.dashboard_switcher.items if i]
        assert labels == ["Depot", "Plant", "All dashboards"]
        assert model.dashboard_id in shell.dashboard_switcher.items[1]["path"]

    async def test_crumb_waits_for_the_dashboard_to_load(self, app):
        _open(app, "Plant")
        app._shell.sync(f"{DASH_ROUTE_PREFIX}other")
        assert app._shell.crumbs()[-1] == Crumb("Dashboard")

    @pytest.mark.parametrize(
        ("path", "crumbs"),
        [
            ("/", []),
            (COMPONENTS_ROUTE, [Crumb("Home", "/"), Crumb("Components")]),
            ("/Analytics/page", [Crumb("Home", "/"), Crumb("Analytics"), Crumb("Overview")]),
            ("/nowhere", [Crumb("Home", "/")]),
        ],
    )
    async def test_route_crumbs(self, app, path, crumbs):
        app._shell.sync(path)
        assert app._shell.crumbs() == crumbs

    async def test_switching_dashboards_goes_through_navigation(self, app):
        _open(app)
        calls = []
        app._request_navigation = lambda path, search="": calls.append(path)
        app._shell._on_switch({"label": "All dashboards", "path": "/"})
        assert calls == ["/"]

    async def test_rename_updates_the_breadcrumb_and_bar(self, app):
        model = _open(app)
        app._rename_dashboard(model.dashboard_id, "Renamed")
        assert app._shell.dashboard_switcher.label == "Renamed"
        assert app._editor._dashboard_bar.title.object == "Renamed"

    async def test_page_content_is_not_wrapped(self, app):
        content = pn.pane.Markdown("x")
        assert app._nav_content(content) is content

    async def test_dashboard_parts_join_the_top_bar(self, app):
        bar = app._editor._dashboard_bar
        assert bar.placement == "header"
        assert list(app._shell.dashboard_actions) == []
        _open(app)
        app._shell.sync(pn.state.location.pathname)
        assert list(app._shell.dashboard_lead) == [bar.lead]
        assert list(app._shell.dashboard_actions) == [bar.actions]
        assert app._shell.search_slot[0].compact
        assert "@media (max-width: 1279.95px)" in app._page.sx

    async def test_dashboard_parts_leave_the_top_bar_off_the_dashboard(self, app):
        _open(app)
        app._shell.sync(pn.state.location.pathname)
        pn.state.location.pathname = COMPONENTS_ROUTE
        app._shell.sync(COMPONENTS_ROUTE)
        assert list(app._shell.dashboard_lead) == []
        assert list(app._shell.dashboard_actions) == []
        assert not app._shell.search_slot[0].compact

    async def test_full_header_layout_leaves_the_top_bar_alone(self, app):
        _open(app)
        app._editor.settings.header_layout = "full"
        app._shell.sync(pn.state.location.pathname)
        assert list(app._shell.dashboard_actions) == []
        assert not app._shell.search_slot[0].compact
        assert app._editor._dashboard_bar.__panel__().visible
        app._editor.settings.header_layout = "compact"
        assert list(app._shell.dashboard_actions) == [app._editor._dashboard_bar.actions]
        assert app._shell.search_slot[0].compact

    async def test_header_layout_reaches_the_editor(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            app = FlowDashApp(project_dir=tmp_path, store=store, header_layout="full")
            assert app._editor.header_layout == "full"
            assert app._editor._dashboard_bar.layout == "full"
        finally:
            sys.path.remove(str(tmp_path))

    async def test_controls_position_reaches_the_editor(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            app = FlowDashApp(project_dir=tmp_path, store=store, controls_position="left")
            assert app._editor.controls_position == "left"
            assert app._editor._view_controls.position == "left"
        finally:
            sys.path.remove(str(tmp_path))

    async def test_no_shell_in_other_variants(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            for variant in ("left", "right", "menubar"):
                app = FlowDashApp(project_dir=tmp_path, store=store, nav_variant=variant)
                assert app._shell is None
                assert app._editor._dashboard_bar.placement == "row"
                assert app._page.rail == []
                assert app._page.header_color == "primary"
        finally:
            sys.path.remove(str(tmp_path))


class TestAppSidebar:
    async def test_empty_by_default(self, app):
        assert app._page.sidebar == []

    async def test_app_items_come_before_dashboard_views(self, app):
        item, view = pn.pane.Markdown("app"), pn.pane.Markdown("filter")
        app._set_sidebar_views([view])
        app.sidebar = [item]
        assert app._page.sidebar == [item, view]
        app._set_sidebar_views([])
        assert app._page.sidebar == [item]
        app.sidebar = []
        assert app._page.sidebar == []


class TestAppModes:
    async def test_edit_request_sets_the_url(self, app):
        _open(app)
        app._on_dashboard_bar_mode("edit")
        assert app._editor.editable
        assert pn.state.location.search == "?edit=true"
        assert not app._share_button.visible

    async def test_view_request_without_changes(self, app):
        _open(app, edit=True)
        app._on_dashboard_bar_mode("view")
        assert not app._editor.editable
        assert pn.state.location.search == ""
        assert app._share_button.visible

    async def test_view_request_with_changes_asks(self, app):
        model = _open(app, edit=True)
        app._editor.add_component("Analytics/selector")
        app._on_dashboard_bar_mode("view")
        assert app._unsaved_dialog.open
        assert app._pending_navigation == (f"{DASH_ROUTE_PREFIX}{model.dashboard_id}", "")
        assert app._editor.editable

    async def test_bar_routes_through_the_app(self, app):
        assert app._editor._dashboard_bar.on_mode == app._on_dashboard_bar_mode

    async def test_view_mode_is_read_only_for_viewers(self, app):
        _open(app)
        app._dashboard_rights = lambda _dashboard: (False, False)
        app._show_view_mode()
        assert app._editor.read_only
        assert not app._share_button.visible
        assert app._editor._dashboard_bar.mode_toggle not in app._editor._dashboard_bar._actions

    async def test_components_route_starts_from_an_empty_canvas(self, app):
        _open(app)
        app._editor.add_component("Analytics/selector")
        app._dirty = False
        pn.state.location.pathname = COMPONENTS_ROUTE
        await app._load_page_layout()
        assert app._editor._tile_items == []
