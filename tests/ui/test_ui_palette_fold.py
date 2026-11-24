"""UI tests for folding the component palette beside the canvas, like the sidecar."""

import json

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.split_view import STORAGE_KEY

from .test_ui_phone import PHONE
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/note": note}

HIDE, SHOW = "Hide components", "Show components"

# The tag and ARIA name of the element with the keyboard focus, across shadow roots.
_FOCUSED = """() => {
  let el = document.activeElement
  while (el?.shadowRoot?.activeElement) { el = el.shadowRoot.activeElement }
  return [el?.tagName, el?.getAttribute("aria-label") || el?.getAttribute("placeholder")]
}"""


def _serve_editor(page, width=1400):
    page.set_viewport_size({"width": width, "height": 900})
    editors = []

    def factory():
        editor = FlowDash(
            COMPONENTS, notifications=False, include_builtin_components=False, user="alice"
        )
        editor.new_dashboard("Fold")
        editor.add_component("Test/note", instance_id="first")
        editor.mode = "wiring"
        editors.append(editor)
        return editor

    serve_component(page, {"/fold": factory}, suffix="/fold")
    wait_until(lambda: len(editors) == 1, page)
    expect(page.locator(".react-flow__node")).to_have_count(1, timeout=DASHBOARD_LOAD_TIMEOUT)
    return editors


def _search(page):
    return page.get_by_placeholder("Search components")


def _button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def _canvas_width(page):
    return stable_box(page.locator(".react-flow").first, page)["width"]


def _stored(page):
    raw = page.evaluate(f"localStorage.getItem({STORAGE_KEY + ':/fold'!r})")
    return None if raw is None else json.loads(raw)


# The id of the element a button's aria-controls points at, by element reference or by id.
_CONTROLS = """(button) => {
  const [target] = button.ariaControlsElements ?? []
  return target ? target.id : button.getAttribute("aria-controls")
}"""


def _controls(button):
    return button.evaluate(_CONTROLS)


def _focused(page):
    return page.evaluate(_FOCUSED)


def test_fold_and_unfold_with_the_buttons(page):
    editors = _serve_editor(page)
    editor = editors[0]
    expect(_search(page)).to_be_visible()
    fold = _button(page, HIDE)
    expect(fold).to_have_attribute("aria-expanded", "true")
    region_id = _controls(fold)
    assert region_id and region_id.startswith("fd-component-palette-")
    region = page.locator(f"#{region_id}")
    expect(region).to_have_attribute("role", "region")
    wide = _canvas_width(page)

    fold.click()
    unfold = _button(page, SHOW)
    expect(unfold).to_be_visible()
    expect(_search(page)).to_be_hidden()
    expect(unfold).to_have_attribute("aria-expanded", "false")
    assert _controls(unfold) == region_id
    wait_until(lambda: _focused(page) == ["BUTTON", SHOW], page, timeout=5000)
    wait_until(lambda: _canvas_width(page) > wide + 150, page, timeout=5000)
    assert editor._palette_drawer.open is False
    wait_until(lambda: (_stored(page) or {}).get("palette_open") is False, page)

    unfold.click()
    expect(_search(page)).to_be_visible()
    expect(fold).to_have_attribute("aria-expanded", "true")
    wait_until(lambda: _focused(page) == ["INPUT", "Search components"], page, timeout=5000)
    wait_until(lambda: abs(_canvas_width(page) - wide) < 2, page, timeout=5000)
    wait_until(lambda: (_stored(page) or {}).get("palette_open") is True, page)


def test_the_fold_is_remembered_for_the_browser(page):
    editors = _serve_editor(page)
    _button(page, HIDE).click()
    wait_until(lambda: (_stored(page) or {}).get("palette_open") is False, page)

    page.reload()
    wait_until(lambda: len(editors) == 2, page)
    expect(_button(page, SHOW)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(_search(page)).to_be_hidden()
    wait_until(lambda: editors[1]._palette_drawer.open is False, page)


def test_slash_opens_the_folded_palette_and_focuses_its_search(page):
    editors = _serve_editor(page)
    _button(page, HIDE).click()
    expect(_button(page, SHOW)).to_be_visible()
    page.locator(".react-flow__pane").click(position={"x": 400, "y": 400})

    page.keyboard.press("/")
    expect(_search(page)).to_be_visible()
    wait_until(lambda: _focused(page) == ["INPUT", "Search components"], page, timeout=5000)
    # The key opened the palette rather than being typed into its search.
    expect(_search(page)).to_have_value("")
    page.keyboard.type("note")
    wait_until(lambda: editors[0]._palette_panel.query == "note", page, timeout=5000)


def test_slash_leaves_the_folded_palette_alone_in_the_layout_tab(page):
    editors = _serve_editor(page)
    _button(page, HIDE).click()
    expect(_button(page, SHOW)).to_be_visible()
    editors[0].mode = "dashboard"
    expect(_button(page, SHOW)).to_be_hidden()
    page.locator("body").press("/")
    page.wait_for_timeout(300)
    assert editors[0]._palette_drawer.open is False


def test_drag_from_the_palette_after_unfolding_it(page):
    editors = _serve_editor(page)
    _button(page, HIDE).click()
    _button(page, SHOW).click()
    item = page.locator(".MuiListItemButton-root[draggable='true']").filter(has_text="Note")
    item.drag_to(page.locator(".react-flow__pane"), target_position={"x": 80, "y": 80})
    wait_until(lambda: len(editors[0]._flow.nodes) == 2, page, timeout=8000)


def _serve_app(page, width):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [DashboardItem(instance_id="n1", component_id="Test/note", x=0, y=0)]
    store.save_dashboard(model)
    page.set_viewport_size({"width": width, "height": 844})
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    path = f"/dash/{model.dashboard_id}"
    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"{path}?edit=true")
    page.get_by_role("button", name="Done").wait_for(timeout=DASHBOARD_LOAD_TIMEOUT)
    return apps


def test_the_command_palette_opens_the_folded_palette_and_focuses_its_search(page):
    apps = _serve_app(page, 1400)
    _button(page, HIDE).click()
    expect(_button(page, SHOW)).to_be_visible()
    wait_until(lambda: not apps[0]._editor._palette_drawer.open, page)

    page.keyboard.press("Control+k")
    combobox = page.get_by_role("combobox", name="Search dashboards, components and actions")
    combobox.press_sequentially("show components")
    expect(page.get_by_role("option").first).to_contain_text(SHOW)
    combobox.press("Enter")
    expect(_search(page)).to_be_visible()
    wait_until(lambda: _focused(page) == ["INPUT", "Search components"], page, timeout=5000)


def test_a_folded_palette_still_shows_in_the_phone_sheet(page):
    apps = _serve_app(page, 1400)
    _button(page, HIDE).click()
    wait_until(lambda: not apps[0]._editor._palette_drawer.open, page)

    page.set_viewport_size({"width": PHONE, "height": 844})
    add = page.get_by_role("button", name="Add component")
    expect(add).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(_button(page, SHOW)).to_be_hidden()
    add.click()
    expect(page.get_by_role("dialog", name="Add component")).to_be_visible()
    expect(_search(page)).to_be_visible()
    # The sheet has its own close button, so the palette's fold button is not shown there.
    expect(_button(page, HIDE)).to_be_hidden()
    page.get_by_role("dialog", name="Add component").get_by_role("button", name="Close").click()


@pytest.mark.xfail(
    reason="The palette moved back from the phone's sheet is lost at the column's next "
    "change, often in Chromium under load and always in Firefox and WebKit, also on main",
    strict=False,
)
def test_the_fold_survives_a_round_trip_through_the_phone_layout(page):
    apps = _serve_app(page, 1400)
    _button(page, HIDE).click()
    wait_until(lambda: not apps[0]._editor._palette_drawer.open, page)
    page.set_viewport_size({"width": PHONE, "height": 844})
    expect(page.get_by_role("button", name="Add component")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )

    page.set_viewport_size({"width": 1400, "height": 844})
    expect(_button(page, SHOW)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(_search(page)).to_be_hidden()
    # Labelled again after the round trip through the phone layout.
    expect(_button(page, SHOW)).to_have_attribute("aria-expanded", "false")
    _button(page, SHOW).click()
    wait_until(lambda: apps[0]._editor._palette_drawer.open, page)
    expect(_search(page)).to_be_visible()
    expect(_button(page, HIDE)).to_have_attribute("aria-expanded", "true")
    assert _controls(_button(page, HIDE)).startswith("fd-component-palette-")
