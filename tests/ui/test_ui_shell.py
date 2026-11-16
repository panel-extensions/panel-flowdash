"""UI tests for the app shell (top bar, navigation rail) and the dashboard bar."""

import json
import re
from urllib.parse import urlparse

import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.editor import FlowDash

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
    dialog.get_by_role("button", name="Discard").click()

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
    page.get_by_role("button", name="Depot").wait_for()
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
    page.get_by_text("Drag components here").wait_for()
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
    page.get_by_role("button", name="Open navigation").click()
    page.locator(".MuiListItemButton-root").filter(has_text="Components").click()
    wait_until(lambda: _path(page) == "/components", page, timeout=10000)


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
    node = page.locator(".react-flow__node").first
    box = node.bounding_box()
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
        page.get_by_role("menuitem", name="Download").click()
    assert download.value.suggested_filename == "Wired_pair.json"
    path = tmp_path / "export.json"
    download.value.save_as(path)
    assert json.loads(path.read_text())["items"][0]["component_id"] == "Test/note"

    _overflow(page).click()
    clear = page.get_by_role("menuitem", name="Clear canvas")
    download_item = page.get_by_role("menuitem", name="Download")
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
    assert editor.dirty
