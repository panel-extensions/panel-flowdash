"""UI tests: every way of leaving a dashboard with unsaved changes asks about them first."""

from urllib.parse import urlparse

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


@register(page=False, component=True, title="Note", description="A short note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/note": note}


def _path(page):
    return urlparse(page.url).path


def _store():
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [DashboardItem(instance_id="n1", component_id="Test/note", x=0, y=0)]
    store.save_dashboard(model)
    return store, model.dashboard_id


def _serve_dirty(page, store, dashboard_id, **params):
    """Open the dashboard in edit mode and add a component without saving it."""
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False, **params
        )
        apps.append(app)
        return app

    routes = {"/": factory, "/components": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=f"/dash/{dashboard_id}?edit=true")
    page.get_by_role("button", name="Done").wait_for()
    app = apps[0]
    app._editor.add_component("Test/note")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    return app


def _unsaved_dialog(page):
    dialog = page.get_by_role("dialog").filter(has_text="Save changes before leaving?")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Added Note")).to_be_visible()
    return dialog


def _palette(page, query):
    page.keyboard.press("Control+k")
    search = page.get_by_role("combobox", name=PLACEHOLDER)
    expect(search).to_be_focused()
    search.press_sequentially(query)
    return search


def _stored_items(store, dashboard_id):
    return len(store._load_any(dashboard_id).items)


def test_palette_current_dashboard_asks_and_keeps_editing(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant)

    search = _palette(page, "Plant")
    expect(page.get_by_role("option").first).to_contain_text("Current dashboard")
    search.press("Enter")

    _unsaved_dialog(page).get_by_role("button", name="Keep editing").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert app._editor.editable
    assert app._dirty
    assert page.url.endswith("?edit=true")


def test_breadcrumb_current_dashboard_asks_and_leaves_to_view_mode(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant)

    page.get_by_role("button", name="Plant").click()
    page.get_by_role("menuitem", name="Plant").click()

    _unsaved_dialog(page).get_by_role("button", name="Leave without saving").click()
    page.get_by_role("button", name="Edit").wait_for()
    wait_until(lambda: "edit=true" not in page.url, page, timeout=10000)
    wait_until(lambda: len(app._editor._tile_items) == 1, page, timeout=10000)
    assert not app._dirty
    assert _stored_items(store, plant) == 1


@pytest.mark.parametrize("entry", ["rail", "breadcrumb", "palette"])
def test_leaving_for_another_page_asks(page, entry):
    store, plant = _store()
    app = _serve_dirty(page, store, plant)

    if entry == "rail":
        page.get_by_role("button", name="Components", exact=True).click()
    elif entry == "breadcrumb":
        page.get_by_role("button", name="Dashboards", exact=True).last.click()
    else:
        _palette(page, "All dashboards").press("Enter")

    _unsaved_dialog(page).get_by_role("button", name="Keep editing").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert _path(page) == f"/dash/{plant}"
    assert app._dirty


def _create_from_dialog(page, title):
    dialog = page.get_by_role("dialog").filter(has_text="Create dashboard")
    dialog.get_by_label("Name", exact=True).fill(title)
    dialog.get_by_role("button", name="Create", exact=True).click()


def test_drawer_new_dashboard_asks_then_leaves(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant, nav_variant="left")
    app._nav_drawer.open = True

    page.get_by_text("New Dashboard").first.click()
    _create_from_dialog(page, "Depot")

    dialog = _unsaved_dialog(page)
    assert not store.title_exists(app._user_id, "Depot")
    dialog.get_by_role("button", name="Leave without saving").click()

    wait_until(lambda: store.title_exists(app._user_id, "Depot"), page, timeout=10000)
    wait_until(lambda: app._current_dashboard.title == "Depot", page, timeout=10000)
    wait_until(lambda: _path(page) != f"/dash/{plant}", page, timeout=10000)
    assert _stored_items(store, plant) == 1


def test_menubar_new_dashboard_asks_then_saves(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant, nav_variant="menubar")

    page.get_by_role("button", name="Dashboards").click()
    page.get_by_role("menuitem", name="New Dashboard").click()
    _create_from_dialog(page, "Depot")

    _unsaved_dialog(page).get_by_role("button", name="Save and leave").click()

    wait_until(lambda: _stored_items(store, plant) == 2, page, timeout=10000)
    wait_until(lambda: app._current_dashboard.title == "Depot", page, timeout=10000)
    assert app._editor._tile_items == []


def test_palette_offers_no_editor_actions_after_leaving_edit_mode(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant)
    page.get_by_role("button", name="Save", exact=True).click()
    wait_until(lambda: not app._dirty, page, timeout=10000)

    page.get_by_role("button", name="Components", exact=True).click()
    wait_until(lambda: _path(page) == "/components", page, timeout=10000)
    page.get_by_text("Building blocks for dashboards").wait_for()

    search = _palette(page, "Note")
    option = page.get_by_role("option").filter(has_text="Note").first
    expect(option).to_contain_text("A short note")
    expect(page.get_by_text("Add to this dashboard")).to_have_count(0)
    search.press("Enter")

    expect(page.get_by_role("dialog", name="Command palette")).to_have_count(0)
    page.wait_for_timeout(500)
    assert app._editor._tile_items == []
    assert not app._dirty
    assert not app._editor.editable


def test_leaving_the_page_asks_the_browser(page):
    store, plant = _store()
    app = _serve_dirty(page, store, plant)
    wait_until(lambda: app._leave_page_guard.active, page, timeout=10000)
    # Chromium only asks before unload once the user has interacted with the page.
    page.get_by_text("Unsaved changes").click()

    dialogs = []

    def dismiss(dialog):
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", dismiss)
    page.evaluate("() => { setTimeout(() => window.location.reload(), 0) }")
    wait_until(lambda: dialogs == ["beforeunload"], page, timeout=10000)
    expect(page.get_by_role("button", name="Done")).to_be_visible()
