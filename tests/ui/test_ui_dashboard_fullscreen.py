"""UI tests for showing a whole dashboard in full screen."""

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The app re-renders the editor as it loads a dashboard, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


@register(page=False, component=True, title="Name field")
def name_field(config):
    return pmui.TextInput(label="Name", placeholder="Type a name")


COMPONENTS = {"Test/note": note, "Test/name_field": name_field}


def _serve(page, *, components=("Test/note",), query="", header_layout="compact"):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id=f"n{i}", component_id=component, x=0, y=i)
        for i, component in enumerate(components)
    ]
    store.save_dashboard(model)
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            header_layout=header_layout,
        )
        apps.append(app)
        return app

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}{query}")
    page.get_by_text("note tile").wait_for()
    return apps[0]


def _native(page) -> bool:
    return page.evaluate("document.fullscreenElement !== null")


def _presenting(page, app):
    """Wait until the dashboard alone fills the viewport, the app's chrome out of sight."""
    wait_until(lambda: app._editor.fullscreen, page, timeout=10000)
    expect(page.get_by_role("button", name="Refresh")).to_be_hidden()
    expect(page.get_by_role("button", name="Exit full screen")).to_be_attached()
    viewport = page.viewport_size
    wait_until(
        lambda: (
            page.locator(".fd-presenting").bounding_box()
            == {"x": 0, "y": 0, "width": viewport["width"], "height": viewport["height"]}
        ),
        page,
    )
    # The tiles reflow to the screen: no navigation rail or header beside or above them.
    tile = page.get_by_text("note tile").bounding_box()
    assert tile["x"] < 60 and tile["y"] < 120


def _restored(page, app):
    wait_until(lambda: not app._editor.fullscreen, page, timeout=10000)
    expect(page.get_by_role("button", name="Refresh")).to_be_visible()
    expect(page.locator(".fd-presenting")).to_have_count(0)
    wait_until(lambda: not _native(page), page)


@pytest.mark.parametrize("header_layout", ["compact", "full"])
def test_button_enters_full_screen_and_leaving_it_restores_the_layout(page, header_layout):
    app = _serve(page, header_layout=header_layout)
    page.get_by_role("button", name="Full screen").click()
    wait_until(lambda: _native(page), page)
    _presenting(page, app)
    # Esc and the browser's own control leave full screen in the browser.
    page.evaluate("document.exitFullscreen()")
    _restored(page, app)


@pytest.mark.parametrize("header_layout", ["compact", "full"])
def test_overlay_shows_health_and_exit_without_the_title(page, header_layout):
    app = _serve(page, header_layout=header_layout)
    # Before full screen the health text is only in the dashboard bar.
    expect(page.get_by_text("1/1 up to date", exact=True)).to_have_count(1)
    page.get_by_role("button", name="Full screen").click()
    _presenting(page, app)
    page.mouse.move(200, 200)
    overlay = page.get_by_role("region", name="Plant, full screen controls")
    expect(overlay).not_to_contain_text("Plant")
    expect(overlay).to_contain_text("1/1 up to date")
    expect(overlay).to_have_css("opacity", "1")
    # One line: the health chip's dot and text sit side by side.
    assert stable_box(overlay, page)["height"] <= 48
    page.get_by_role("button", name="Exit full screen").click()
    _restored(page, app)


def test_overlay_hides_while_the_pointer_rests(page):
    app = _serve(page)
    page.get_by_role("button", name="Full screen").click()
    _presenting(page, app)
    overlay = page.locator(".fd-fullscreen-overlay")
    page.mouse.move(200, 200)
    expect(overlay).to_have_css("opacity", "1")
    expect(overlay).to_have_css("opacity", "0", timeout=6000)
    page.mouse.move(220, 220)
    expect(overlay).to_have_css("opacity", "1")


def test_f_shortcut_toggles_full_screen(page):
    app = _serve(page)
    page.keyboard.press("f")
    wait_until(lambda: _native(page), page)
    _presenting(page, app)
    page.keyboard.press("f")
    _restored(page, app)


def test_f_is_ignored_while_typing(page):
    app = _serve(page, components=("Test/note", "Test/name_field"))
    field = page.get_by_placeholder("Type a name")
    field.click()
    field.press_sequentially("fff")
    expect(field).to_have_value("fff")
    page.wait_for_timeout(300)
    assert not app._editor.fullscreen
    assert not _native(page)


def test_command_palette_entry(page):
    app = _serve(page)
    page.keyboard.press("Control+k")
    search = page.get_by_role("combobox", name="Search dashboards, components and actions")
    search.press_sequentially("full screen")
    expect(page.get_by_role("option").first).to_contain_text("Full screen")
    search.press("Enter")
    wait_until(lambda: _native(page), page)
    _presenting(page, app)
    # The palette opens inside the full screen element, so it can be seen and typed in.
    page.keyboard.press("Control+k")
    search = page.get_by_role("combobox", name="Search dashboards, components and actions")
    expect(search).to_be_visible()
    expect(search).to_be_focused()
    search.press_sequentially("exit full")
    expect(page.get_by_role("option").first).to_contain_text("Exit full screen")
    search.press("Enter")
    _restored(page, app)


def test_edit_mode_leaves_full_screen(page):
    app = _serve(page)
    page.keyboard.press("f")
    _presenting(page, app)
    page.keyboard.press("e")
    wait_until(lambda: app._editor.editable, page, timeout=10000)
    wait_until(lambda: not app._editor.fullscreen, page)
    wait_until(lambda: not _native(page), page)
    page.keyboard.press("f")
    page.wait_for_timeout(300)
    assert not app._editor.fullscreen


def test_kiosk_link_opens_the_presentation_layout(page):
    app = _serve(page, query="?kiosk=1")
    _presenting(page, app)
    # Without a gesture the browser grants no real full screen; the layout covers the window.
    assert not _native(page)
    page.mouse.move(200, 200)
    overlay = page.get_by_role("region", name="Plant, full screen controls")
    expect(overlay).to_contain_text("1/1 up to date")
    expect(overlay).not_to_contain_text("Plant")
    page.keyboard.press("Escape")
    _restored(page, app)
    wait_until(lambda: "kiosk" not in page.url, page)
