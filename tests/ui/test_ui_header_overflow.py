"""UI tests that the compact top bar keeps every action on screen with a long dashboard title."""

import re

import pytest

from panel_flowdash import DashboardEdge, DashboardItem, MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .test_ui_header_layout import EDIT_ACTIONS, TOP_BAR, VIEW_ACTIONS
from .test_ui_view_header import COMPONENTS
from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

LONG_TITLE = "Texas Wind Fleet Commissioning and Capacity Review"

# The element actually hit at the centre of a box, through the shadow roots
# Panel renders into, and whether it belongs to the given element. A disabled
# button takes no pointer events, so it takes them for the check.
_HITS_ITSELF = """
(el) => {
  const pointerEvents = el.style.pointerEvents
  el.style.pointerEvents = "auto"
  const r = el.getBoundingClientRect()
  const x = r.x + r.width / 2, y = r.y + r.height / 2
  let hit = document.elementFromPoint(x, y)
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y)
    if (!inner || inner === hit) break
    hit = inner
  }
  el.style.pointerEvents = pointerEvents
  for (let node = hit; node; node = node.parentNode || node.host) {
    if (node === el) return true
  }
  return false
}
"""


def _store(title=LONG_TITLE):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, title)
    model.items = [
        DashboardItem(instance_id="picker", component_id="Test/picker", x=0, y=0),
        DashboardItem(instance_id="wired", component_id="Test/chart", x=300, y=0),
        DashboardItem(instance_id="waiting", component_id="Test/chart", x=300, y=200),
    ]
    model.edges = [DashboardEdge("picker", "picked", "wired", "unit")]
    store.save_dashboard(model)
    return store, model.dashboard_id


def _serve(page, width, edit):
    store, dash = _store()
    page.set_viewport_size({"width": width, "height": 900})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )

    suffix = f"/dash/{dash}" + ("?edit=true" if edit else "")
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix)
    if edit:
        expect(page.get_by_text("All changes saved")).to_be_visible()
    else:
        page.locator(".flowdash-tile").filter(visible=True).first.wait_for()


def _button(page, name):
    pattern = re.compile(rf"(^|\s){re.escape(name)}")
    return page.get_by_role("button", name=pattern).filter(visible=True).first


def _assert_on_top_and_inside(page, names):
    width = page.viewport_size["width"]
    for name in names:
        button = _button(page, name)
        expect(button).to_be_visible()
        box = stable_box(button, page)
        assert box["x"] >= 0, name
        assert box["x"] + box["width"] <= width + 1, f"{name} ends at {box['x'] + box['width']}"
        assert button.evaluate(_HITS_ITSELF), f"{name} is covered"


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
@pytest.mark.parametrize("width", [1024, 1280, 1440])
def test_every_action_stays_on_screen_with_a_long_title(page, width, edit):
    _serve(page, width, edit)

    _assert_on_top_and_inside(page, (EDIT_ACTIONS if edit else VIEW_ACTIONS) + TOP_BAR)


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
def test_the_title_truncates_and_keeps_the_full_title_in_a_tooltip(page, edit):
    _serve(page, 1024, edit)

    title = page.get_by_role("button", name=LONG_TITLE)
    expect(title).to_be_visible()
    assert title.evaluate("el => el.scrollWidth > el.clientWidth")
    assert title.evaluate("el => getComputedStyle(el).textOverflow") == "ellipsis"
    title.hover()
    expect(page.get_by_role("tooltip").filter(has_text=LONG_TITLE)).to_be_visible()


@pytest.mark.parametrize("width", [1024, 1180, 1280])
def test_settings_shows_its_icon(page, width):
    _serve(page, width, edit=True)

    settings = page.get_by_role("button", name="Settings", exact=True)
    box = stable_box(settings, page)
    assert box["width"] >= 24 and box["height"] >= 24
    icon = settings.locator(".material-icons, .MuiIcon-root, svg").first
    icon_box = icon.bounding_box()
    assert icon_box["width"] > 0 and icon_box["height"] > 0


# The visible text of a button, without the ligatures of its icons.
_VISIBLE_TEXT = """
(el) => {
  const icons = ".material-icons, .material-icons-outlined, .MuiIcon-root, svg, [aria-hidden=true]"
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let text = ""
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement
    if (parent.closest(icons) || !parent.checkVisibility()) continue
    text += " " + node.textContent
  }
  return text.replace(/\\s+/g, " ").trim()
}
"""


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
def test_top_bar_buttons_are_large_enough_and_named_by_their_text(page, edit):
    _serve(page, 1024, edit)
    # The dashboard's actions join the top bar after the page shows.
    for name in (EDIT_ACTIONS if edit else VIEW_ACTIONS) + TOP_BAR:
        expect(_button(page, name)).to_be_visible()

    buttons = page.locator(".MuiAppBar-root").first.get_by_role("button").filter(visible=True)
    assert buttons.count() > 8
    for i in range(buttons.count()):
        button = buttons.nth(i)
        box = button.bounding_box()
        text = button.evaluate(_VISIBLE_TEXT)
        assert box["width"] >= 24 and box["height"] >= 24, text or button.get_attribute(
            "aria-label"
        )
        if text:
            expect(button).to_have_accessible_name(re.compile(re.escape(text)))


def test_settings_icon_opens_the_settings(page):
    _serve(page, 1024, edit=True)

    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("dialog").get_by_role("button", name="Save settings")).to_be_visible()
