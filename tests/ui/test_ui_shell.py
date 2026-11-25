"""UI tests for the app shell (top bar, navigation rail) and the dashboard bar."""

import json
import re
from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import (
    DashboardItem,
    MemoryDashboardStore,
    SidebarAction,
    SidebarItem,
    SidebarSection,
    register,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.editor import FlowDash

from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The app re-renders the editor as it loads a dashboard, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/note": note}


def _overflow(page):
    """The dashboard bar's overflow menu button."""
    return page.get_by_role("button", name="More actions")


def _path(page):
    return urlparse(page.url).path


def _store_with(*titles):
    store = MemoryDashboardStore()
    user = resolve_identity().user
    ids = []
    for title in titles:
        model = store.create_dashboard(user, title)
        model.items = [DashboardItem(instance_id="n1", component_id="Test/note", x=0, y=0)]
        store.save_dashboard(model)
        ids.append(model.dashboard_id)
    return store, ids


def _serve_app(page, store, suffix, apps=None, **params):
    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False, **params
        )
        if apps is not None:
            apps.append(app)
        return app

    routes = {"/": factory, "/components": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=suffix)


def test_edit_and_done_switch_modes_and_url(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps)

    page.get_by_text("note tile").wait_for()
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Done").wait_for()
    wait_until(lambda: page.url.endswith("?edit=true"), page, timeout=10000)
    assert apps[0]._editor.editable

    page.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="Refresh").wait_for()
    wait_until(lambda: "edit=true" not in page.url, page, timeout=10000)
    assert not apps[0]._editor.editable


def test_done_with_unsaved_changes_asks_and_discards(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps)

    expect(page.get_by_text("All changes saved")).to_be_visible()
    apps[0]._editor.add_component("Test/note")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()

    page.get_by_role("button", name="Done").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Leave without saving").click()

    page.get_by_role("button", name="Refresh").wait_for()
    wait_until(lambda: len(apps[0]._editor._tile_items) == 1, page, timeout=10000)
    assert not apps[0]._editor.dirty
    assert "edit=true" not in page.url


def test_breadcrumb_switches_dashboards(page):
    store, (plant, depot) = _store_with("Plant", "Depot")
    apps = []
    _serve_app(page, store, f"/dash/{plant}", apps)

    switcher = page.get_by_role("button", name="Plant")
    switcher.click()
    page.get_by_role("menuitem", name="Depot").click()

    wait_until(lambda: _path(page) == f"/dash/{depot}", page, timeout=10000)
    # Checked on the server: slow runners can take long to repaint the header.
    wait_until(lambda: apps[0]._shell.dashboard_switcher.label == "Depot", page, timeout=20000)
    assert apps[0]._current_dashboard.dashboard_id == depot


# Pages start at "/": the test server resolves bundles loaded after the first
# render relative to the page, which fails below a nested route like /dash/<id>.
def test_rail_navigates_and_marks_the_route(page):
    store, _ = _store_with("Plant")
    _serve_app(page, store, "/")

    page.get_by_role("button", name="Components").click()

    wait_until(lambda: _path(page) == "/components", page, timeout=10000)
    # The rail item and the breadcrumb's current page.
    expect(page.get_by_role("button", name="Components", exact=True)).to_have_count(2)
    page.get_by_text("Building blocks for dashboards").wait_for()
    page.get_by_role("button", name="Home", exact=True).last.click()
    wait_until(lambda: _path(page) == "/", page, timeout=10000)


def test_launcher_has_no_empty_sidebar_toggle(page):
    store, _ = _store_with("Plant")
    _serve_app(page, store, "/")

    page.get_by_role("button", name="Home", exact=True).wait_for()
    expect(page.get_by_role("button", name="Open drawer")).to_have_count(0)
    expect(page.get_by_role("button", name="Close drawer")).to_have_count(0)


def test_rail_collapses_behind_a_menu_button_on_narrow_screens(page):
    page.set_viewport_size({"width": 480, "height": 800})
    store, _ = _store_with("Plant")
    _serve_app(page, store, "/")

    expect(page.get_by_role("button", name="Components")).to_be_hidden()
    page.get_by_role("button", name="Open drawer").click()
    page.locator(".MuiListItemButton-root").filter(has_text="Components").click()
    wait_until(lambda: _path(page) == "/components", page, timeout=10000)
    # Navigating closes the drawer again.
    expect(page.locator(".MuiDrawer-paper.sidebar")).to_be_hidden()


def test_standalone_editor_view_mode_offers_edit(page):
    editor = FlowDash(COMPONENTS, notifications=False, editable=False)
    editor.add_component("Test/note")
    serve_component(page, editor)

    page.get_by_role("button", name="Refresh").wait_for()
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Done").wait_for()
    wait_until(lambda: editor.editable, page, timeout=10000)


def test_save_follows_unsaved_changes(page):
    store, (dash,) = _store_with("Plant")
    editor = FlowDash(
        COMPONENTS, notifications=False, store=store, dashboard=dash, user=resolve_identity().user
    )
    serve_component(page, editor)

    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_disabled()
    box = stable_box(page.locator(".react-flow__node").first, page)
    page.mouse.move(box["x"] + 20, box["y"] + 8)
    page.mouse.down()
    page.mouse.move(box["x"] + 120, box["y"] + 80, steps=8)
    page.mouse.up()
    expect(save).to_be_enabled()

    save.click()
    expect(page.get_by_text(re.compile(r"Saved \d\d:\d\d"))).to_be_visible()
    expect(save).to_be_disabled()


def test_discard_reloads_the_saved_dashboard(page):
    store, (dash,) = _store_with("Plant")
    editors = []

    # Built in the session, so the editor lays out the session's page dialog.
    def app():
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            store=store,
            dashboard=dash,
            user=resolve_identity().user,
        )
        editor.add_component("Test/note")
        editors.append(editor)
        return editor

    serve_component(page, app)
    editor = editors[0]

    page.get_by_role("button", name="Discard").click()
    page.get_by_role("dialog").get_by_role("button", name="Discard changes").click()

    wait_until(lambda: len(editor._tile_items) == 1 and not editor.dirty, page, timeout=10000)
    expect(page.get_by_role("button", name="Discard", exact=True)).to_be_disabled()


def test_overflow_downloads_and_clears(page, tmp_path):
    editor = FlowDash(COMPONENTS, notifications=False)
    editor.new_dashboard("Wired pair")
    editor.add_component("Test/note")
    serve_component(page, editor)

    with page.expect_download() as download:
        _overflow(page).click()
        page.get_by_role("menuitem", name=re.compile(r"Download$")).click()
    assert download.value.suggested_filename == "Wired_pair.json"
    path = tmp_path / "export.json"
    download.value.save_as(path)
    assert json.loads(path.read_text())["items"][0]["component_id"] == "Test/note"

    _overflow(page).click()
    clear = page.get_by_role("menuitem", name="Clear canvas")
    download_item = page.get_by_role("menuitem", name=re.compile(r"Download$"))
    style = "el => getComputedStyle(el).color"
    assert clear.evaluate(style) != download_item.evaluate(style)
    clear.click()
    page.get_by_role("dialog").get_by_role("button", name="Clear canvas").click()
    wait_until(lambda: editor._tile_items == [], page, timeout=10000)


def test_import_file_from_the_overflow_menu(page, tmp_path):
    source = FlowDash(COMPONENTS, notifications=False)
    added = source.add_component("Test/note")
    path = tmp_path / "board.json"
    path.write_text(json.dumps(source.export_dashboard()))
    editors = []

    def app():
        editors.append(FlowDash(COMPONENTS, notifications=False))
        return editors[0]

    serve_component(page, app)
    editor = editors[0]

    _overflow(page).click()
    page.get_by_role("menuitem", name="Import file").click()
    page.get_by_role("dialog").locator("input[type=file]").set_input_files(path)

    wait_until(
        lambda: [i["instance_id"] for i in editor._tile_items] == [added], page, timeout=10000
    )
    # The import marks the editor dirty once loading, which places the tiles, has finished.
    wait_until(lambda: editor.dirty, page, timeout=10000)


def test_custom_sidebar_section_in_rail_and_narrow_drawer(page):
    store, _ = _store_with("Plant")
    report_titles = {"weekly": "Weekly Summary", "monthly": "Monthly KPI"}

    def list_reports(_ctx):
        return [
            SidebarItem(
                title=report_titles["weekly"],
                url="/reports/weekly",
                subtitle="Owner: alice",
            ),
            SidebarItem(
                title=report_titles["monthly"],
                url="/reports/monthly",
                subtitle="Updated today",
            ),
            SidebarItem(title="Q1 view", url="/reports/view?id=q1", subtitle="Query 1"),
            SidebarItem(title="Q2 view", url="/reports/view?id=q2", subtitle="Query 2"),
            *[
                SidebarItem(title=title, url=f"/reports/{slug}")
                for slug, title in report_titles.items()
                if slug not in ("weekly", "monthly")
            ],
        ]

    def report_view(_ctx, subpath):
        import panel as pn

        if subpath == "view":
            search = pn.state.location.search if pn.state.location is not None else ""
            return pn.pane.Markdown(f"Report viewer ({search})")
        if not subpath:
            return pn.pane.Markdown("All reports index")
        return pn.pane.Markdown(f"{report_titles.get(subpath, subpath)} body")

    def create_report(ctx):
        report_titles["q3"] = "Q3 Forecast"
        ctx.refresh()
        ctx.navigate("/reports/q3")

    reports = SidebarSection(
        id="reports",
        label="Reports",
        icon="assessment",
        order=10,
        route="reports",
        view=report_view,
        items=list_reports,
        new_action=SidebarAction(label="New report", action=create_report),
    )
    routes = FlowDashApp.build_routes(
        registry=normalize_components(COMPONENTS),
        store=store,
        notifications=False,
        sections=[reports],
    )
    page.set_viewport_size({"width": 1280, "height": 800})
    serve_component(page, routes, suffix="/")

    nav = page.get_by_role("navigation")
    header = page.locator("header.MuiAppBar-root")

    # Section routes never appear in Pages.
    expect(nav.get_by_role("button", name="Pages", exact=True)).to_have_count(0)

    # Desktop rail shows Reports, opens its menu and navigates with active highlight and crumbs.
    reports_button = nav.get_by_role("button", name="Reports", exact=True)
    expect(reports_button).to_be_visible()
    reports_button.click()
    expect(page.get_by_role("menuitem", name="Weekly Summary")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Monthly KPI")).to_be_visible()
    expect(page.get_by_role("menuitem", name="New report")).to_be_visible()

    page.get_by_role("menuitem", name="Weekly Summary").click()
    wait_until(lambda: _path(page) == "/reports/weekly", page, timeout=10000)
    page.get_by_text("Weekly Summary body").wait_for()
    expect(header.get_by_role("button", name="Reports", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="Weekly Summary", exact=True)).to_be_visible()
    # Active rail button gets the non-transparent pill highlight on its icon.
    wait_until(
        lambda: reports_button.locator(".MuiButton-startIcon").evaluate(
            "el => getComputedStyle(el).backgroundColor !== 'rgba(0, 0, 0, 0)'"
        ),
        page,
        timeout=10000,
    )

    # Reloading a section route directly serves the section view with crumbs intact.
    page.reload()
    page.wait_for_function(
        "() => window.Bokeh?.documents?.length > 0 && Bokeh.documents.every((d) => d.is_idle)",
        timeout=30000,
    )
    page.locator("#loader").wait_for(state="hidden", timeout=30000)
    page.get_by_text("Weekly Summary body").wait_for(timeout=15000)
    expect(header.get_by_role("button", name="Reports", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="Weekly Summary", exact=True)).to_be_visible()

    # An item added outside this session appears when the rail menu opens.
    report_titles["ext"] = "Shared External"
    reports_button.click()
    expect(page.get_by_role("menuitem", name="Shared External")).to_be_visible(timeout=10000)

    # "+ New" creates an item, refreshes the section and navigates to it.
    page.get_by_role("menuitem", name="New report").click()
    wait_until(lambda: _path(page) == "/reports/q3", page, timeout=10000)
    page.get_by_text("Q3 Forecast body").wait_for()

    # Query-string items highlight distinctly and show the right crumb.
    reports_button.click()
    page.get_by_role("menuitem", name="Q2 view").click()
    wait_until(lambda: page.url.endswith("/reports/view?id=q2"), page, timeout=10000)
    page.get_by_text("Report viewer (?id=q2)").wait_for()
    expect(header.get_by_role("button", name="Q2 view", exact=True)).to_be_visible()

    # At 390 px the section moves into the drawer, highlights only Q2 view, and navigates.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(reports_button).to_be_hidden()
    page.get_by_role("button", name="Open drawer").click()
    drawer = page.locator(".MuiDrawer-paper.sidebar")
    expect(drawer.get_by_text("Reports", exact=True)).to_be_visible()
    expect(
        drawer.locator(".MuiListItemButton-root")
        .filter(has_text="Weekly Summary")
        .filter(has_text="Owner: alice")
    ).to_be_visible()
    expect(drawer.get_by_text("Q3 Forecast")).to_be_visible()
    expect(drawer.get_by_text("New report")).to_be_visible()
    q1_row = drawer.locator(".MuiListItemButton-root").filter(has_text="Q1 view")
    q2_row = drawer.locator(".MuiListItemButton-root").filter(has_text="Q2 view")
    expect(q2_row).to_have_class(re.compile(r"Mui-selected"))
    expect(q1_row).not_to_have_class(re.compile(r"Mui-selected"))

    drawer.locator(".MuiListItemButton-root").filter(has_text="Monthly KPI").click()
    wait_until(lambda: _path(page) == "/reports/monthly", page, timeout=10000)
    page.get_by_text("Monthly KPI body").wait_for()
    expect(drawer).to_be_hidden()
