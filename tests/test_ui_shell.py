"""Tests for the app shell (top bar, navigation rail) and the dashboard bar."""

import re
import sys

import panel as pn
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import SidebarAction, SidebarContext, SidebarItem, SidebarSection
from panel_flowdash.app import COMPONENTS_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.auth import Identity
from panel_flowdash.dashboard_store import DashboardStore, MemoryDashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.sidebar import RESERVED_SECTION_IDS
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
        assert bar._actions.objects[-5:] == [
            editor._save_button,
            bar.done_button,
            bar.download_button,
            bar._phone_query,
            bar._narrow_query,
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
            bar._phone_query,
            bar._narrow_query,
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
            bar.mode_toggle,
            bar.refresh_button,
            bar.refresh_period,
            bar.fullscreen_button,
            bar.download_button,
            bar._phone_query,
            bar._narrow_query,
        ]
        assert list(bar.mode_toggle.options.values()) == ["explore", "report", "view"]

    async def test_a_phone_finds_the_view_actions_in_the_overflow_menu(self, editor):
        editor.editable = False
        bar = editor._dashboard_bar
        bar._phone_query.phone = True
        actions = [item["action"] if item else None for item in bar.overflow.items]
        assert actions == [
            "mode:explore",
            "mode:report",
            "mode:edit",
            "refresh",
            "fullscreen",
            None,
            "download",
            "download_yaml",
            "settings",
        ]
        bar._phone_query.phone = False
        assert [item["action"] for item in bar.overflow.items] == [
            "download",
            "download_yaml",
            "settings",
        ]

    async def test_a_narrow_window_finds_report_in_the_overflow_menu(self, editor):
        bar = editor._dashboard_bar
        bar._narrow_query.phone = True
        assert bar.overflow.items[0]["action"] == "tab:report"
        bar._on_overflow(bar.overflow.items[0])
        assert editor.surface == "report"
        assert all((item or {}).get("action") != "tab:report" for item in bar.overflow.items)

    async def test_a_reader_on_a_phone_gets_an_overflow_menu(self, editor):
        editor.param.update(editable=False, read_only=True)
        bar = editor._dashboard_bar
        bar._phone_query.phone = True
        assert bar.overflow in bar._actions.objects
        assert [item["action"] for item in bar.overflow.items] == [
            "mode:explore",
            "mode:report",
            "refresh",
            "fullscreen",
        ]

    async def test_a_phone_finds_the_edit_actions_in_the_overflow_menu(self, editor):
        bar = editor._dashboard_bar
        editor.mode = "wiring"
        bar._phone_query.phone = True
        phone = [item["action"] for item in bar.overflow.items[:5]]
        assert phone == ["tab:explore", "tab:report", "tab:dashboard", "settings", "done"]
        editor.dirty = True
        assert "discard" in [item["action"] for item in bar.overflow.items if item]

    async def test_the_overflow_menu_switches_modes_on_a_phone(self, editor):
        bar = editor._dashboard_bar
        bar._phone_query.phone = True
        bar._on_overflow({"action": "tab:dashboard"})
        assert editor.mode == "dashboard"
        bar._on_overflow({"action": "done"})
        assert not editor.editable
        bar._on_overflow({"action": "mode:explore"})
        assert editor.explore
        bar._on_overflow({"action": "mode:edit"})
        assert editor.editable and not editor.explore

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

    async def test_refresh_keeps_the_rail_buttons(self, app):
        _open(app)
        app.home_dashboard = "Plant"
        app._shell.refresh()
        buttons = dict(app._shell._rail_buttons)
        objects = list(app._shell.rail)
        _open(app, "Depot")
        app._refresh_sidebar_dashboards()
        assert app._shell._rail_buttons == buttons
        assert list(app._shell.rail) == objects
        labels = [i["label"] for i in buttons["dashboards"].items]
        assert labels == ["Depot", "Plant"]

    async def test_refresh_rebuilds_the_rail_when_its_destinations_change(self, app):
        buttons = dict(app._shell._rail_buttons)
        _open(app)
        app.home_dashboard = "Plant"
        app._shell.refresh()
        assert app._shell._rail_buttons["dashboards"] is not buttons["dashboards"]
        assert app._shell._rail_buttons["dashboards"].items[0]["label"] == "Plant"

    @pytest.mark.parametrize(
        ("path", "active"),
        [("/", "home"), (COMPONENTS_ROUTE, "components"), ("/Analytics/page", "pages")],
    )
    async def test_active_rail_item(self, app, path, active):
        app._shell.sync(path)
        assert app._shell._active() == active
        sx = app._shell._rail_buttons[active].sx
        assert sx["& .MuiButton-startIcon"]["color"] == "primary.text"

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
        bar = app._editor._dashboard_bar
        assert "edit" not in bar.mode_toggle.options.values()

    async def test_components_route_starts_from_an_empty_canvas(self, app):
        _open(app)
        app._editor.add_component("Analytics/selector")
        app._dirty = False
        pn.state.location.pathname = COMPONENTS_ROUTE
        await app._load_page_layout()
        assert app._editor._tile_items == []


class TestSidebarSections:
    async def test_default_sections_is_only_dashboards_with_same_items(self, app):
        assert [section.id for section in app.sections] == ["dashboards"]
        dashboards = app.sections[0]
        assert dashboards.label == "Dashboards"
        assert dashboards.icon == "space_dashboard"
        assert dashboards.order == 0

        plant = _open(app, "Plant")
        depot = _open(app, "Depot")
        ctx = app._sidebar_context(dashboards)
        items = dashboards.items(ctx)
        assert items == [
            SidebarItem(
                title="Depot", url=f"{DASH_ROUTE_PREFIX}{depot.dashboard_id}", icon="dashboard"
            ),
            SidebarItem(
                title="Plant", url=f"{DASH_ROUTE_PREFIX}{plant.dashboard_id}", icon="dashboard"
            ),
        ]
        assert [i["label"] for i in app._shell._dashboard_items()] == ["Depot", "Plant"]

    async def test_registering_second_section_orders_and_lists_items_per_user(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        previous = pn.state._location
        pn.state._location = FakeLocation()
        try:
            store = DashboardStore(tmp_path / "test.db")
            seen_contexts: list[SidebarContext] = []

            def list_reports(ctx: SidebarContext):
                seen_contexts.append(ctx)
                return [
                    SidebarItem(
                        title=f"{ctx.user.user}'s weekly",
                        url="/Analytics/page",
                        subtitle=f"Owner: {ctx.user.user}",
                    ),
                ]

            reports = SidebarSection(
                id="reports",
                label="Reports",
                icon="assessment",
                order=10,
                items=list_reports,
            )
            data = SidebarSection(
                id="data",
                label="Data",
                icon="storage",
                order=20,
                items=lambda _ctx: [SidebarItem(title="Tables", url="/data/tables")],
            )
            app = FlowDashApp(project_dir=tmp_path, store=store, sections=[data, reports])

            assert seen_contexts and seen_contexts[-1].app is app
            assert seen_contexts[-1].user == app._identity
            assert seen_contexts[-1].section == reports
            assert [s.id for s in app.sections] == ["dashboards", "reports", "data"]
            keys = [item["key"] for item in app._shell.rail_items()]
            assert keys == ["home", "dashboards", "reports", "data", "components", "pages", "help"]

            pinned = SidebarSection(
                id="pinned",
                label="Pinned",
                icon="push_pin",
                order=-1,
                path="/pinned",
            )
            app.add_section(pinned)
            assert [s.id for s in app.sections] == ["pinned", "dashboards", "reports", "data"]
            assert [item["key"] for item in app._shell.rail_items()][:5] == [
                "home",
                "pinned",
                "dashboards",
                "reports",
                "data",
            ]

            app._identity = Identity(user="alice")
            app.refresh_sections("reports")
            rail_by_key = {item["key"]: item for item in app._shell.rail_items()}
            assert rail_by_key["reports"]["items"] == [
                {
                    "label": "alice's weekly",
                    "icon": None,
                    "path": "/Analytics/page",
                    "secondary": "Owner: alice",
                }
            ]
            drawer_by_key = {item["key"]: item for item in app._shell._nav_list.items}
            assert drawer_by_key["reports"]["selectable"] is False
            assert drawer_by_key["reports"]["items"][0]["secondary"] == "Owner: alice"

            app._identity = Identity(user="bob")
            app.refresh_sections("reports")
            rail_by_key = {item["key"]: item for item in app._shell.rail_items()}
            assert rail_by_key["reports"]["items"][0]["label"] == "bob's weekly"
            assert rail_by_key["reports"]["items"][0]["secondary"] == "Owner: bob"

            removed = app.remove_section("pinned")
            assert removed is pinned
            assert "pinned" not in [item["key"] for item in app._shell.rail_items()]
        finally:
            pn.state._location = previous
            sys.path.remove(str(tmp_path))

    async def test_assigning_sections_normalizes_dedupes_and_keeps_dashboards(self, app):
        first = SidebarSection(id="reports", label="Old Reports", icon="assessment", order=20)
        second = SidebarSection(id="reports", label="Reports", icon="assessment", order=10)
        data = SidebarSection(id="data", label="Data", icon="storage", order=30, path="/data")

        app.sections = [data, first, second]

        assert [s.id for s in app.sections] == ["dashboards", "reports", "data"]
        assert app.sections[1].label == "Reports"
        assert [i["key"] for i in app._shell.rail_items()][:4] == [
            "home",
            "dashboards",
            "reports",
            "data",
        ]

    async def test_visible_predicate_receives_context_and_hides_section(self, app):
        seen: list[SidebarContext] = []

        def is_visible(ctx: SidebarContext) -> bool:
            seen.append(ctx)
            return ctx.app.is_admin or "analysts" in ctx.user.groups

        restricted = SidebarSection(
            id="catalog",
            label="Data",
            icon="storage",
            order=15,
            items=lambda _ctx: [SidebarItem(title="Sources", url="/catalog/sources")],
            visible=is_visible,
        )
        app._is_admin = False
        app._identity = Identity(user="viewer", groups=frozenset())
        app.add_section(restricted)

        assert seen and seen[-1].app is app and seen[-1].section == restricted
        assert "catalog" not in [item["key"] for item in app._shell.rail_items()]
        assert "catalog" not in app._shell._rail_buttons
        assert "catalog" not in [item["key"] for item in app._shell._nav_list.items]

        app._is_admin = True
        app.refresh_sections("catalog")

        assert "catalog" in [item["key"] for item in app._shell.rail_items()]
        assert "catalog" in app._shell._rail_buttons
        assert "catalog" in [item["key"] for item in app._shell._nav_list.items]

    async def test_new_action_receives_context_and_supports_routes(self, app):
        received: list[SidebarContext] = []
        reports = SidebarSection(
            id="reports",
            label="Reports",
            icon="assessment",
            order=10,
            items=lambda _ctx: [SidebarItem(title="Q1", url="/reports/q1")],
            new_action=SidebarAction(label="New report", action=lambda ctx: received.append(ctx)),
        )
        app.add_section(reports)

        rail_reports = next(i for i in app._shell.rail_items() if i["key"] == "reports")
        assert [i["label"] if i is not None else None for i in rail_reports["items"]] == [
            "Q1",
            None,
            "New report",
        ]
        app._shell._on_menu_item(rail_reports["items"][-1])
        assert len(received) == 1
        assert received[0].app is app and received[0].user == app._identity

        app._page.sidebar_open = True
        drawer_reports = next(i for i in app._shell._nav_list.items if i["key"] == "reports")
        app._shell._on_nav_list_click(drawer_reports["items"][-1])
        assert len(received) == 2
        assert not app._page.sidebar_open

        navigated = []
        app._request_navigation = lambda path, search="": navigated.append((path, search))
        route_section = SidebarSection(
            id="data",
            label="Data",
            icon="storage",
            order=20,
            new_action=SidebarAction(label="Add source", action="/data/new?kind=sql"),
        )
        app.add_section(route_section)
        rail_data = next(i for i in app._shell.rail_items() if i["key"] == "data")
        assert [i["label"] for i in rail_data["items"]] == ["Add source"]
        app._shell._on_menu_item(rail_data["items"][0])
        assert navigated == [("/data/new", "?kind=sql")]

    async def test_query_string_route_matching_and_breadcrumbs(self, app):
        reports = SidebarSection(
            id="reports",
            label="Reports",
            icon="assessment",
            order=10,
            route="reports",
            view=lambda ctx, subpath: f"view:{subpath}",
            items=lambda _ctx: [
                SidebarItem(title="Q1 report", url="/reports/view?id=q1"),
                SidebarItem(title="Q2 report", url="/reports/view?id=q2"),
                SidebarItem(title="Overview", url="/reports/overview"),
            ],
        )
        app.add_section(reports)
        reports_idx = next(
            i for i, item in enumerate(app._shell._nav_list.items) if item["key"] == "reports"
        )

        # Exact path + query matches distinguish q1 from q2.
        app._shell.sync("/reports/view?id=q2")
        assert app._shell._active() == "reports"
        assert app._shell._nav_list.active == (reports_idx, 1)
        assert app._shell.crumbs() == [
            Crumb("Home", "/"),
            Crumb("Reports", "/reports"),
            Crumb("Q2 report"),
        ]

        app._shell.sync("/reports/view", "?id=q1")
        assert app._shell._nav_list.active == (reports_idx, 0)
        assert app._shell.crumbs() == [
            Crumb("Home", "/"),
            Crumb("Reports", "/reports"),
            Crumb("Q1 report"),
        ]

        # Path-only fallback matches a query-less item when extra query params appear.
        app._shell.sync("/reports/overview?tab=details")
        assert app._shell._nav_list.active == (reports_idx, 2)
        assert app._shell.crumbs() == [
            Crumb("Home", "/"),
            Crumb("Reports", "/reports"),
            Crumb("Overview"),
        ]

        # Unknown query on a path with only query-specific items highlights neither item.
        app._shell.sync("/reports/view?id=unknown")
        assert app._shell._active() == "reports"
        assert app._shell._nav_list.active is None
        assert app._shell.crumbs() == [Crumb("Home", "/"), Crumb("Reports")]

    async def test_refresh_sections_updates_cache_without_requerying_on_navigation(self, app):
        store_items = [SidebarItem(title="Q1", url="/reports/q1")]
        calls = 0

        def list_reports(_ctx: SidebarContext):
            nonlocal calls
            calls += 1
            return list(store_items)

        reports = SidebarSection(
            id="reports",
            label="Reports",
            icon="assessment",
            order=10,
            items=list_reports,
        )
        app.add_section(reports)
        assert calls == 1

        # Calling rail_items() and sync() uses cached items and does not mutate state.
        before_cache = dict(app._shell._section_items_cache)
        _ = app._shell.rail_items()
        app._shell.sync("/reports/q1")
        app._shell.sync("/components")
        assert calls == 1
        assert app._shell._section_items_cache == before_cache

        # A new item added later appears once refresh_sections() is called.
        store_items.append(SidebarItem(title="Q2", url="/reports/q2"))
        app.refresh_sections("reports")
        assert calls == 2
        assert [i["label"] for i in app._shell._rail_buttons["reports"].items] == ["Q1", "Q2"]
        drawer_reports = next(i for i in app._shell._nav_list.items if i["key"] == "reports")
        assert [i["label"] for i in drawer_reports["items"]] == ["Q1", "Q2"]

        # Opening the rail menu or narrow drawer also re-fetches fresh items.
        store_items.append(SidebarItem(title="Q3", url="/reports/q3"))
        app._shell._on_rail_menu_open("reports")
        assert calls == 3
        assert [i["label"] for i in app._shell._rail_buttons["reports"].items] == [
            "Q1",
            "Q2",
            "Q3",
        ]

        # Opening the desktop sidebar (narrow=False) must not re-fetch sections.
        store_items.append(SidebarItem(title="Q4", url="/reports/q4"))
        assert not app._shell._menu_observer.narrow
        app._page.sidebar_open = True
        assert calls == 3
        app._page.sidebar_open = False

        # Opening the narrow-screen drawer (narrow=True) re-fetches fresh items.
        app._shell._menu_observer.narrow = True
        app._page.sidebar_open = True
        assert calls == 4
        assert [i["label"] for i in app._shell._rail_buttons["reports"].items] == [
            "Q1",
            "Q2",
            "Q3",
            "Q4",
        ]

    async def test_cross_section_exact_match_beats_earlier_queryless_fallback(self, app):
        sec_a = SidebarSection(
            id="sec_a",
            label="Section A",
            icon="folder",
            order=10,
            items=lambda _ctx: [SidebarItem(title="All views", url="/shared/view")],
        )
        sec_b = SidebarSection(
            id="sec_b",
            label="Section B",
            icon="assessment",
            order=20,
            items=lambda _ctx: [SidebarItem(title="Scoped view", url="/shared/view?scope=b")],
        )
        app.add_section(sec_a)
        app.add_section(sec_b)
        a_idx = next(
            i for i, item in enumerate(app._shell._nav_list.items) if item["key"] == "sec_a"
        )
        b_idx = next(
            i for i, item in enumerate(app._shell._nav_list.items) if item["key"] == "sec_b"
        )

        app._shell.sync("/shared/view?scope=b")
        assert app._shell._active() == "sec_b"
        assert app._shell._nav_list.active == (b_idx, 0)
        assert app._shell.crumbs() == [
            Crumb("Home", "/"),
            Crumb("Section B"),
            Crumb("Scoped view"),
        ]

        app._shell.sync("/shared/view?scope=other")
        assert app._shell._active() == "sec_a"
        assert app._shell._nav_list.active == (a_idx, 0)
        assert app._shell.crumbs() == [Crumb("Home", "/"), Crumb("Section A"), Crumb("All views")]

    async def test_ctx_navigate_opens_external_urls_in_browser(self, app):
        internal: list[tuple[str, str]] = []
        external: list[str] = []
        app._request_navigation = lambda path, search="": internal.append((path, search))
        app._shell._menu_observer.open_url = lambda url: external.append(url)

        ctx = app._sidebar_context(app.sections[0])
        ctx.navigate("https://example.com/docs?v=1")
        ctx.navigate("http://example.org/help")
        ctx.navigate("/reports/q4?tab=2")

        assert external == ["https://example.com/docs?v=1", "http://example.org/help"]
        assert internal == [("/reports/q4", "?tab=2")]

    async def test_ctx_still_admin_rechecks_live_admin_status(self, app):
        app._is_admin = True
        live = [True]
        app._still_admin = lambda: live[0]
        ctx = app._sidebar_context(app.sections[0])

        assert ctx.is_admin is True
        assert ctx.still_admin() is True

        live[0] = False
        assert ctx.is_admin is True
        assert ctx.still_admin() is False

    async def test_build_routes_accepts_callable_sections_and_rejects_clashes(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            reports = SidebarSection(
                id="reports",
                label="Reports",
                icon="assessment",
                route="reports",
                view=lambda _ctx, subpath: f"body:{subpath}",
            )
            routes = FlowDashApp.build_routes(
                project_dir=tmp_path, store=store, sections=lambda: [reports]
            )
            assert "/reports" in routes
            assert any(pattern.startswith("/reports/") for pattern in routes)

            clashing = SidebarSection(
                id="analytics_ext",
                label="Analytics",
                icon="analytics",
                route="Analytics",
                view=lambda _ctx, _p: "clash",
            )
            with pytest.raises(ValueError, match="clashes with registry page section"):
                FlowDashApp.build_routes(project_dir=tmp_path, store=store, sections=[clashing])

            nested = SidebarSection(
                id="sub_reports",
                label="Sub Reports",
                icon="assessment",
                route="reports/sub",
                view=lambda _ctx, _p: "nested",
            )
            with pytest.raises(ValueError, match="overlaps with section"):
                FlowDashApp.build_routes(
                    project_dir=tmp_path, store=store, sections=[reports, nested]
                )
        finally:
            sys.path.remove(str(tmp_path))

    async def test_callable_path_switches_between_direct_link_and_menu(self, app):
        use_landing = [True]
        seen_ctx: list[SidebarContext] = []

        def resolve_path(ctx: SidebarContext) -> str | None:
            seen_ctx.append(ctx)
            return "/catalog" if use_landing[0] else None

        catalog = SidebarSection(
            id="catalog",
            label="Data",
            icon="storage",
            order=20,
            path=resolve_path,
            items=lambda _ctx: [SidebarItem(title="Tables", url="/catalog/tables")],
        )
        app.add_section(catalog)

        assert seen_ctx and seen_ctx[-1].app is app and seen_ctx[-1].section == catalog
        rail_cat = next(i for i in app._shell.rail_items() if i["key"] == "catalog")
        assert rail_cat["path"] == "/catalog"
        assert "items" not in rail_cat

        use_landing[0] = False
        app.refresh_sections("catalog")
        rail_cat = next(i for i in app._shell.rail_items() if i["key"] == "catalog")
        assert "path" not in rail_cat
        assert [i["label"] for i in rail_cat["items"]] == ["Tables"]

    async def test_section_route_serves_view_and_stays_out_of_pages(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        previous = pn.state._location
        pn.state._location = FakeLocation()
        try:
            store = DashboardStore(tmp_path / "test.db")
            seen: list[tuple[str, str]] = []

            def report_view(ctx: SidebarContext, subpath: str):
                seen.append((ctx.user.user, subpath))
                return pn.pane.Markdown(f"Report body: {subpath or 'index'}")

            reports = SidebarSection(
                id="reports",
                label="Reports",
                icon="assessment",
                order=10,
                route="reports",
                view=report_view,
                items=lambda _ctx: [SidebarItem(title="Q4 Report", url="/reports/q4")],
            )
            routes = FlowDashApp.build_routes(
                project_dir=tmp_path, store=store, sections=[reports]
            )
            assert "/reports" in routes
            assert any(pattern.startswith("/reports/") for pattern in routes)

            pn.state.location.pathname = "/reports/q4"
            app = routes["/reports"]()
            # Section routes never appear in Pages.
            pages_entry = next(i for i in app._shell.rail_items() if i["key"] == "pages")
            assert all("/reports" not in i["path"] for i in pages_entry["items"] if i)

            await app._load_page_layout()
            assert seen[-1] == (app._identity.user, "q4")
            assert app._shell._active() == "reports"
            assert app._shell.crumbs() == [
                Crumb("Home", "/"),
                Crumb("Reports", "/reports"),
                Crumb("Q4 Report"),
            ]

            pn.state.location.pathname = "/reports"
            await app._load_page_layout()
            assert seen[-1] == (app._identity.user, "")
            assert app._shell.crumbs() == [Crumb("Home", "/"), Crumb("Reports")]

            # Hidden section refuses direct route access.
            secret_called = []
            secret = SidebarSection(
                id="secret",
                label="Secret",
                icon="lock",
                route="secret",
                view=lambda _ctx, _p: secret_called.append(True) or "classified",
                visible=lambda _ctx: False,
            )
            app.add_section(secret)
            pn.state.location.pathname = "/secret/top"
            await app._load_page_layout()
            assert secret_called == []
            assert "flowdash-empty-state" in app._page.main[0][0].css_classes
        finally:
            pn.state._location = previous
            sys.path.remove(str(tmp_path))

    async def test_bad_extension_items_are_logged_and_skipped(self, app, caplog):
        broken = SidebarSection(
            id="broken",
            label="Broken",
            icon="warning",
            order=30,
            items=lambda _ctx: [
                SidebarItem(title="Valid", url="/valid"),
                {"title": "Not a SidebarItem"},  # type: ignore[list-item]
            ],
        )
        with caplog.at_level("WARNING", logger="panel_flowdash"):
            app.add_section(broken)

        rail_broken = next(i for i in app._shell.rail_items() if i["key"] == "broken")
        assert [i["label"] for i in rail_broken["items"]] == ["Valid"]
        assert "Skipping invalid item" in caplog.text

    async def test_section_spec_validation_and_reserved_ids(self, app):
        with pytest.raises(ValueError, match="title"):
            SidebarItem(title=" ", url="/x")
        with pytest.raises(ValueError, match="url"):
            SidebarItem(title="X", url="")
        with pytest.raises(TypeError, match="callable or a route"):
            SidebarAction(label="New", action=123)  # type: ignore[arg-type]
        for bad_id in ("", " ", "my reports", "reports.v1", "-bad", "a/b"):
            with pytest.raises(ValueError, match="id"):
                SidebarSection(id=bad_id, label="X", icon="star")
        for reserved_id in sorted(RESERVED_SECTION_IDS):
            with pytest.raises(ValueError, match="reserved"):
                SidebarSection(id=reserved_id, label="X", icon="star")
        with pytest.raises(ValueError, match="static 'path'"):
            SidebarSection(
                id="combo",
                label="Combo",
                icon="star",
                path="/combo",
                items=lambda _ctx: [],
            )
        with pytest.raises(ValueError, match=r"route and SidebarSection\.view"):
            SidebarSection(id="only_route", label="R", icon="star", route="r")
        with pytest.raises(ValueError, match=r"route and SidebarSection\.view"):
            SidebarSection(id="only_view", label="V", icon="star", view=lambda _c, _p: "hi")
        for reserved_route in (
            "admin/extra",
            "static",
            "static/assets",
            "login",
            "logout",
            "ws",
            "metadata",
            "autoload.js",
        ):
            with pytest.raises(ValueError, match="reserved route prefix"):
                SidebarSection(
                    id="bad_route",
                    label="B",
                    icon="star",
                    route=reserved_route,
                    view=lambda _c, _p: "",
                )
        with pytest.raises(ValueError, match="clashes with registry page section"):
            app.add_section(
                SidebarSection(
                    id="analytics_ext",
                    label="Analytics",
                    icon="analytics",
                    route="Analytics",
                    view=lambda _c, _p: "",
                )
            )
        with pytest.raises(TypeError, match="SidebarSection"):
            app.add_section("not-a-section")  # type: ignore[arg-type]
