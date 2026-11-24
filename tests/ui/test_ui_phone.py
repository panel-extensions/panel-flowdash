"""UI tests that a dashboard fits a phone's screen, with its top bar actions in reach."""

import pytest

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .test_ui_header_overflow import _HITS_ITSELF, _button, _store
from .test_ui_view_header import COMPONENTS
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

PHONE = 390

# The visible buttons and tiles that reach past either side of the window,
# through the shadow roots Panel renders into.
_OFF_SCREEN = """() => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  const width = window.innerWidth
  const items = roots.flatMap((r) => [...r.querySelectorAll("button, .flowdash-tile")])
  // The canvas pans, so its nodes may lie beyond the window.
  const onCanvas = (el) => {
    for (let node = el; node; node = node.parentElement || node.getRootNode().host) {
      if (node.classList && node.classList.contains("react-flow")) return true
    }
    return false
  }
  return items.filter((el) => !onCanvas(el)).flatMap((el) => {
    const box = el.getBoundingClientRect()
    if (!box.width || !box.height || getComputedStyle(el).visibility === "hidden") return []
    if (box.left >= -1 && box.right <= width + 1) return []
    return [`${el.getAttribute("aria-label") || el.className} at ${box.left}..${box.right}`]
  })
}"""

# What stays in the top bar on a phone; the other actions are in the overflow menu.
# On a dashboard the bar's Ask stands in for the top bar's Assistant, and a phone
# finds Ask in the overflow menu.
_PHONE_TOP_BAR = {
    False: ["More actions", "Search", "Toggle theme"],
    True: ["Share", "More actions", "Save", "Search", "Toggle theme"],
}


def _serve(page, width, edit):
    store, dash = _store()
    page.set_viewport_size({"width": width, "height": 844})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]),
        )

    suffix = f"/dash/{dash}" + ("?edit=true" if edit else "")
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix)
    if edit:
        expect(page.locator(".react-flow__node").first).to_be_visible(
            timeout=DASHBOARD_LOAD_TIMEOUT
        )
    else:
        page.locator(".flowdash-tile").filter(visible=True).first.wait_for()
    for name in _PHONE_TOP_BAR[edit]:
        stable_box(_button(page, name), page)


def _menu_entries(page):
    _button(page, "More actions").click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    entries = [
        item.inner_text().split("\n")[-1]
        for item in menu.get_by_role("menuitem").all()
        if item.is_visible()
    ]
    page.keyboard.press("Escape")
    expect(menu.filter(visible=True)).to_have_count(0)
    return entries


def test_a_dashboard_fits_a_phone_screen(page):
    _serve(page, PHONE, False)

    assert page.evaluate("document.documentElement.scrollWidth") == PHONE
    assert page.evaluate(_OFF_SCREEN) == []


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
def test_the_top_bar_fits_a_phone_screen(page, edit):
    _serve(page, PHONE, edit)

    assert page.evaluate("document.documentElement.scrollWidth") == PHONE
    bar = page.locator("header.MuiAppBar-root")
    assert bar.bounding_box()["height"] <= 64
    for name in _PHONE_TOP_BAR[edit]:
        button = _button(page, name)
        box = button.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE, f"{name} is off screen"
        assert button.evaluate(_HITS_ITSELF), f"{name} is covered"


@pytest.mark.parametrize(
    ("edit", "entries"),
    [
        (False, ["Ask", "Report", "Edit", "Refresh now", "Full screen"]),
        (True, ["Ask", "Report", "Layout", "Settings", "Done editing"]),
    ],
    ids=["view", "edit"],
)
def test_a_phone_finds_the_hidden_actions_in_the_overflow_menu(page, edit, entries):
    _serve(page, PHONE, edit)

    assert _menu_entries(page)[: len(entries)] == entries
    hidden = ["Dashboard", "Ask about this dashboard", "Edit", "Refresh now", "Full screen"]
    for name in [*hidden, "Dataflow", "Layout", "Done"]:
        expect(
            page.get_by_role("button", name=name, exact=True).filter(visible=True)
        ).to_have_count(0)


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
def test_wider_screens_keep_the_actions_out_of_the_overflow_menu(page, edit):
    _serve(page, 1024, edit)

    entries = _menu_entries(page)
    for name in ["Ask", "Edit", "Refresh now", "Full screen", "Dataflow", "Done editing"]:
        assert name not in entries


def test_the_overflow_menu_switches_a_phone_to_edit_mode(page):
    _serve(page, PHONE, False)

    _button(page, "More actions").click()
    page.get_by_role("menuitem", name="Edit").click()
    expect(_button(page, "Save")).to_be_visible()
    _button(page, "More actions").click()
    page.get_by_role("menuitem", name="Done editing").click()
    # Sharing is an edit action.
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_hidden()
