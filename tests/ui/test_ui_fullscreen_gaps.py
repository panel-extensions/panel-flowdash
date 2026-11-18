"""UI tests for full screen: notifications, focus, touch and keyboard, and several editors."""

import panel as pn
import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.editor import FlowDash

from .util import INERT_COUNT, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/note": note}

# The deepest element under the centre of the first leaf whose text contains
# the argument, and whether it is that leaf or inside it.
_ON_TOP = """(text) => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  let found = null
  for (const root of roots) {
    found = [...root.querySelectorAll("*")].find(
      (el) => el.childElementCount === 0 && el.textContent.includes(text)
    )
    if (found) break
  }
  if (!found) return false
  const box = found.getBoundingClientRect()
  let top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
  while (top?.shadowRoot) {
    const inner = top.shadowRoot.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    if (!inner || inner === top) break
    top = inner
  }
  for (let node = top; node; node = node.parentNode ?? node.host) {
    if (node === found || found.contains(top)) return true
  }
  return false
}"""

# Whether the focused element, looked up through shadow roots, is on the page
# behind the dashboard rather than in it (or nothing, at the end of the page).
_FOCUS_BEHIND = """() => {
  let node = document.activeElement
  while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement
  if (!node || node === document.body || node === document.documentElement) return false
  for (; node; node = node.parentNode ?? node.host) {
    if (node instanceof Element && node.classList.contains("fd-presenting")) return false
  }
  return true
}"""

_TOGGLE = "document.dispatchEvent(new CustomEvent('flowdash:fullscreen', {cancelable: true}))"


def _serve(page, query="", notifications=False):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [DashboardItem(instance_id="n0", component_id="Test/note", x=0, y=0)]
    store.save_dashboard(model)
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=notifications
        )
        apps.append(app)
        return app

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}{query}")
    page.get_by_text("note tile").first.wait_for()
    return apps[0]


def _native(page) -> bool:
    return page.evaluate("document.fullscreenElement !== null")


def test_notifications_show_in_native_full_screen(page):
    app = _serve(page, notifications=True)
    page.get_by_role("button", name="Full screen").click()
    wait_until(lambda: _native(page), page)
    wait_until(lambda: app._editor.fullscreen, page)

    doc = app._editor._view._models and next(iter(app._editor._view._models.values()))[0].document
    doc.add_next_tick_callback(lambda: pn.state.notifications.info("Refresh failed", duration=0))

    wait_until(lambda: page.evaluate(_ON_TOP, "Refresh failed"), page, timeout=10000)


def test_focus_stays_in_the_dashboard_in_the_window(page):
    app = _serve(page, query="?kiosk=1")
    wait_until(lambda: app._editor.fullscreen, page, timeout=10000)
    # The browser makes the page behind inert once it renders the presentation layout.
    wait_until(lambda: page.evaluate(INERT_COUNT) > 0, page)
    assert not _native(page)
    for key in ["Tab"] * 6 + ["Shift+Tab"] * 3:
        page.keyboard.press(key)
        assert not page.evaluate(_FOCUS_BEHIND), key


def test_a_tap_or_a_key_reveals_the_overlay(page):
    app = _serve(page, query="?kiosk=1")
    wait_until(lambda: app._editor.fullscreen, page, timeout=10000)
    overlay = page.locator(".fd-fullscreen-overlay")
    page.mouse.move(200, 200)
    expect(overlay).to_have_css("opacity", "0", timeout=6000)

    # A tap on a touch screen gives a pointerdown without a move first.
    page.mouse.down()
    page.mouse.up()
    expect(overlay).to_have_css("opacity", "1")

    expect(overlay).to_have_css("opacity", "0", timeout=6000)
    page.keyboard.press("Shift")
    expect(overlay).to_have_css("opacity", "1")


def test_toggle_targets_the_editor_holding_the_focus(page):
    store = MemoryDashboardStore()
    editors = []

    def factory():
        pair = [
            FlowDash(COMPONENTS, store=store, notifications=False, editable=False)
            for _ in range(2)
        ]
        for i, editor in enumerate(pair):
            editor.new_dashboard(f"Board {i}")
            editor.add_component("Test/note")
            editor.editable = False
        editors.extend(pair)
        return pn.Row(*pair, sizing_mode="stretch_both")

    serve_component(page, factory)
    page.get_by_text("note tile").first.wait_for()
    first, second = editors

    # What F and the palette's action dispatch.
    page.get_by_role("button", name="Refresh").nth(1).focus()
    page.evaluate(_TOGGLE)
    wait_until(lambda: second.fullscreen, page)
    assert not first.fullscreen

    page.evaluate(_TOGGLE)
    wait_until(lambda: not second.fullscreen, page)
    assert not first.fullscreen
