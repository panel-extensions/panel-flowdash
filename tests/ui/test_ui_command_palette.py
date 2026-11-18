"""UI tests for the Ctrl/Cmd+K command palette and the keyboard shortcuts."""

import time
from urllib.parse import urlparse

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The app re-renders the editor as it loads a dashboard, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

PLACEHOLDER = "Search dashboards, components and actions"


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


@register(page=False, component=True, title="Name field")
def name_field(config):
    return pmui.TextInput(label="Name", placeholder="Type a name")


COMPONENTS = {"Test/note": note, "Test/name_field": name_field}


def _store_with(*titles, component="Test/note"):
    store = MemoryDashboardStore()
    user = resolve_identity().user
    ids = []
    for title in titles:
        model = store.create_dashboard(user, title)
        model.items = [DashboardItem(instance_id="n1", component_id=component, x=0, y=0)]
        store.save_dashboard(model)
        ids.append(model.dashboard_id)
    return store, ids


def _serve_app(page, store, suffix, apps):
    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    routes = {"/": factory, "/components": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=suffix)


def _dialog(page):
    return page.get_by_role("dialog", name="Command palette")


def _open_with_keyboard(page):
    page.keyboard.press("Control+k")
    expect(_dialog(page)).to_be_visible()
    return page.get_by_role("combobox", name=PLACEHOLDER)


def _path(page):
    return urlparse(page.url).path


def test_ctrl_k_opens_and_enter_switches_dashboards(page):
    store, (plant, depot) = _store_with("Plant", "Depot")
    apps = []
    _serve_app(page, store, f"/dash/{plant}", apps)
    page.get_by_text("note tile").wait_for()

    search = _open_with_keyboard(page)
    expect(search).to_be_focused()
    expect(page.get_by_role("option").filter(has_text="Current dashboard")).to_contain_text(
        "Plant"
    )
    search.press_sequentially("dpt")
    options = page.get_by_role("option")
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text("Depot")
    expect(options.first.locator("mark")).to_have_count(3)
    expect(options.first).to_have_attribute("aria-selected", "true")

    search.press("Enter")
    expect(_dialog(page)).to_have_count(0)
    wait_until(lambda: _path(page) == f"/dash/{depot}", page, timeout=10000)
    wait_until(lambda: apps[0]._current_dashboard.dashboard_id == depot, page, timeout=10000)


def test_enter_before_the_items_load_runs_the_match_once_they_do(page):
    store, (plant, depot) = _store_with("Plant", "Depot")
    apps = []
    _serve_app(page, store, f"/dash/{plant}", apps)
    page.get_by_text("note tile").wait_for()
    palette = apps[0]._command_palette.palette
    items = palette.on_open

    def slow_items():
        # The items take a while to reach the browser, as on a slow server.
        time.sleep(0.5)
        return items()

    palette.on_open = slow_items

    search = _open_with_keyboard(page)
    search.press_sequentially("dpt")
    search.press("Enter")
    expect(_dialog(page)).to_have_count(0)
    wait_until(lambda: _path(page) == f"/dash/{depot}", page, timeout=10000)


def test_field_opens_arrows_move_and_escape_closes(page):
    store, (plant,) = _store_with("Plant")
    _serve_app(page, store, f"/dash/{plant}", [])
    page.get_by_text("note tile").wait_for()

    page.get_by_role("button", name="Search", exact=True).click()
    search = page.get_by_role("combobox", name=PLACEHOLDER)
    expect(search).to_be_focused()
    expect(page.get_by_role("option").filter(has_text="Refresh all tiles")).to_be_visible()
    expect(page.get_by_label("Keyboard shortcuts")).to_contain_text("Back to view mode")

    first = page.get_by_role("option").first
    expect(first).to_have_attribute("aria-selected", "true")
    search.press("ArrowDown")
    expect(first).to_have_attribute("aria-selected", "false")
    expect(page.get_by_role("option").nth(1)).to_have_attribute("aria-selected", "true")
    search.press("ArrowUp")
    expect(first).to_have_attribute("aria-selected", "true")

    search.press_sequentially("zzzz")
    expect(page.get_by_text("No results")).to_be_visible()

    search.press("Escape")
    expect(_dialog(page)).to_have_count(0)
    assert "edit=true" not in page.url


def test_shortcuts_switch_modes_and_are_ignored_while_typing(page):
    store, (plant,) = _store_with("Plant", component="Test/name_field")
    apps = []
    _serve_app(page, store, f"/dash/{plant}", apps)
    field = page.get_by_placeholder("Type a name")
    field.wait_for()

    field.click()
    field.press_sequentially("re")
    expect(field).to_have_value("re")
    page.wait_for_timeout(500)
    assert "edit=true" not in page.url
    assert not apps[0]._editor.editable

    field.blur()
    page.keyboard.press("e")
    wait_until(lambda: page.url.endswith("?edit=true"), page, timeout=10000)
    page.get_by_role("button", name="Done").wait_for()

    page.keyboard.press("Escape")
    wait_until(lambda: "edit=true" not in page.url, page, timeout=10000)
    page.get_by_role("button", name="Refresh").wait_for()
    assert not apps[0]._editor.editable


def test_add_from_the_palette_and_save_with_ctrl_s(page):
    store, (plant,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{plant}?edit=true", apps)
    page.get_by_role("button", name="Done").wait_for()

    search = _open_with_keyboard(page)
    search.press_sequentially("name field")
    option = page.get_by_role("option").first
    expect(option).to_contain_text("Add to this dashboard")
    search.press("Enter")
    wait_until(lambda: len(apps[0]._editor._tile_items) == 2, page, timeout=10000)
    wait_until(lambda: apps[0]._editor.dirty, page, timeout=10000)

    page.keyboard.press("Control+s")
    wait_until(lambda: not apps[0]._editor.dirty, page, timeout=10000)
    assert len(store._load_any(plant).items) == 2


def test_question_mark_lists_the_shortcuts(page):
    store, (plant,) = _store_with("Plant")
    _serve_app(page, store, f"/dash/{plant}", [])
    page.get_by_text("note tile").wait_for()

    page.keyboard.press("?")
    shortcuts = page.get_by_role("dialog", name="Keyboard shortcuts")
    expect(shortcuts).to_contain_text("Search and commands")
    expect(shortcuts).to_contain_text("Save")
    page.keyboard.press("Escape")
    expect(shortcuts).to_have_count(0)
