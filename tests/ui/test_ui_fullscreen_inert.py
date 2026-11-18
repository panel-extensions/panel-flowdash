"""UI tests for full screen: the page behind the dashboard is inert while it is shown."""

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.util import confirm

from .util import INERT_COUNT, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

ANSWERS = []


@register(page=False, component=True, title="Note")
def note(config):
    ask = pmui.Button(label="Ask")
    ask.on_click(lambda _e: confirm("Sure?", on_confirm=lambda _e: ANSWERS.append("yes")))
    return pn.Column("note tile", pmui.Button(label="Tile button"), ask)


@register(page=False, component=True, title="Picker", sidebar=True)
def picker(config):
    return pmui.Select(label="Pick", options=["a", "b", "c"])


COMPONENTS = {"Test/note": note, "Test/picker": picker}

# The focused element, looked up through shadow roots, as "behind" when it is
# outside the dashboard, "page" at the end of the page, else its label.
_FOCUSED = """() => {
  let node = document.activeElement
  while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement
  if (!node || node === document.body || node === document.documentElement) return "page"
  const label = (node.getAttribute("aria-label") || node.textContent || "").trim()
  for (let el = node; el; el = el.parentNode ?? el.host) {
    if (el instanceof Element && el.classList.contains("fd-presenting")) return label
  }
  return "behind"
}"""


def _serve(page, query="?kiosk=1"):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id="n0", component_id="Test/note", x=0, y=0),
        DashboardItem(instance_id="p0", component_id="Test/picker", x=0, y=0),
    ]
    store.save_dashboard(model)
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}{query}")
    page.get_by_text("note tile").first.wait_for()
    return apps[0]


def _accessible(page) -> list[tuple[str, str]]:
    """The roles and names in the browser's accessibility tree, which leaves out inert content."""
    session = page.context.new_cdp_session(page)
    try:
        nodes = session.send("Accessibility.getFullAXTree")["nodes"]
    finally:
        session.detach()
    return [
        (node["role"]["value"], node.get("name", {}).get("value", ""))
        for node in nodes
        if not node.get("ignored")
    ]


def _presenting(page, app):
    wait_until(lambda: app._editor.fullscreen, page, timeout=10000)
    wait_until(lambda: page.evaluate(INERT_COUNT) > 0, page)
    # The tile's buttons and the overlay render after the tile's text.
    expect(page.get_by_role("button", name="Tile button")).to_be_visible()
    expect(page.get_by_role("button", name="Exit full screen")).to_be_attached()


def test_tab_and_shift_tab_stay_in_the_dashboard(page):
    app = _serve(page)
    _presenting(page, app)
    forward = []
    for _ in range(12):
        page.keyboard.press("Tab")
        forward.append(page.evaluate(_FOCUSED))
    backward = []
    for _ in range(16):
        page.keyboard.press("Shift+Tab")
        backward.append(page.evaluate(_FOCUSED))
    assert "behind" not in forward + backward, (forward, backward)
    assert "Tile button" in forward
    assert "Exit full screen" in forward
    assert "Tile button" in backward


def test_screen_readers_only_see_the_dashboard(page):
    app = _serve(page)
    _presenting(page, app)
    tree = _accessible(page)
    roles = {role for role, _ in tree}
    names = {name for _, name in tree}
    assert not roles & {"banner", "navigation"}
    assert "Dashboards" not in names
    assert {"Tile button", "Pick", "Exit full screen"} <= names

    page.get_by_role("button", name="Exit full screen").click()
    wait_until(lambda: not app._editor.fullscreen, page, timeout=10000)
    wait_until(lambda: page.evaluate(INERT_COUNT) == 0, page)
    roles = {role for role, _ in _accessible(page)}
    assert {"banner", "navigation"} <= roles


def test_leaving_keeps_what_was_inert_before(page):
    app = _serve(page, query="")
    marked = """() => {
      const roots = [document]
      for (let i = 0; i < roots.length; i++) {
        for (const el of roots[i].querySelectorAll("*")) if (el.shadowRoot) roots.push(el.shadowRoot)
      }
      for (const root of roots) {
        const header = root.querySelector("header")
        if (header) return header
      }
    }"""
    page.evaluate(f"() => {{ ({marked})().inert = true }}")
    app._editor.fullscreen = True
    _presenting(page, app)
    app._editor.fullscreen = False
    wait_until(lambda: page.evaluate(INERT_COUNT) == 1, page)
    assert page.evaluate(f"() => ({marked})().inert")


def test_menus_selects_and_dialogs_work_in_full_screen(page):
    ANSWERS.clear()
    app = _serve(page)
    _presenting(page, app)

    page.get_by_role("button", name="More for Note").click()
    item = page.get_by_role("menuitem", name="Show in editor")
    item.click(trial=True)
    page.mouse.click(600, 700)
    expect(item).to_be_hidden()

    page.get_by_role("combobox", name="Pick").click()
    page.get_by_role("menuitem", name="b").click()
    expect(page.get_by_role("combobox", name="Pick")).to_have_text("b")

    page.get_by_role("button", name="Ask").click()
    confirm = page.get_by_role("button", name="Confirm")
    confirm.click()
    wait_until(lambda: ANSWERS == ["yes"], page)
    # The server closes the dialog before calling on_confirm, but the browser
    # unmounts it a round trip and a transition later, and its focus trap then
    # restores the focus, which would take it from a palette opened before.
    expect(confirm).to_have_count(0)

    page.keyboard.press("Control+k")
    palette = page.get_by_role("combobox", name="Search")
    expect(palette).to_be_focused()
    page.keyboard.press("Escape")
    expect(palette).to_be_hidden()
    assert app._editor.fullscreen
