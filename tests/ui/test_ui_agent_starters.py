"""UI tests of a new dashboard: it opens in edit mode with Ask on and questions to start from."""

import os
import re

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.agent import FlowDashDeps, dashboard_toolset
from panel_flowdash.agent.starters import STARTER_CLASS, STARTERS_CLASS
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .test_ui_phone import PHONE
from .test_ui_shell import COMPONENTS as SHELL_COMPONENTS
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Wind Turbines", tags=["source"])
class Turbines(Viewer):
    @param.output(param.DataFrame)
    def turbines(self):
        return None

    def __panel__(self):
        return "turbines"


@register(page=False, component=True, title="State Population", tags=["source"])
class Population(Viewer):
    @param.output(param.DataFrame)
    def population(self):
        return None

    def __panel__(self):
        return "population"


COMPONENTS = {**SHELL_COMPONENTS, "Data/turbines": Turbines, "Data/population": Population}

STARTERS = [
    "What's in Wind Turbines?",
    "Add a table of State Population",
    "Chart the key numbers in Wind Turbines",
]

# Where the screenshots of these tests go, if anywhere.
SHOTS = os.environ.get("FLOWDASH_STARTER_SHOTS")


def _serve(page, width, scheme, **params):
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 844 if width == PHONE else 900})
    store = MemoryDashboardStore()
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=agent,
            page_options={"dark_theme": scheme == "dark"},
            **params,
        )
        apps.append(app)
        return app

    serve_component(page, {"/": factory, "/dash/[^/]+": factory})
    return store, apps


def _create(page, title="Fresh"):
    page.get_by_role("button", name="New dashboard").filter(visible=True).first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill(title)
    dialog.get_by_role("button", name="Create", exact=True).click()
    # On a phone the assistant's drawer, which opens next, is a dialog too.
    expect(page.get_by_role("dialog").filter(has_text="Create dashboard")).to_have_count(
        0, timeout=DASHBOARD_LOAD_TIMEOUT
    )


def _starters(page):
    return page.locator(f".{STARTERS_CLASS}").filter(visible=True)


# The contrast of a starter's label with what is behind it, as WCAG computes it.
_CONTRAST = """(el) => {
  const parse = (c) => {
    const v = c.match(/[0-9.]+/g).map(Number)
    return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]
  }
  const over = ([r, g, b, a], [R, G, B]) => [r * a + R * (1 - a), g * a + G * (1 - a), b * a + B * (1 - a)]
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((v) => {
      v /= 255
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const button = el.querySelector("button") || el.shadowRoot.querySelector("button")
  // The backgrounds from the button out, until an opaque one, laid over each other.
  const layers = []
  let node = button
  while (node) {
    const color = parse(getComputedStyle(node).backgroundColor)
    if (color[3] > 0) layers.push(color)
    if (color[3] === 1) break
    node = node.parentElement || (node.getRootNode && node.getRootNode().host)
  }
  let background = [255, 255, 255]
  for (const layer of layers.reverse()) background = over(layer, background)
  const text = over(parse(getComputedStyle(button).color), background)
  const [a, b] = [lum(text), lum(background)].sort((x, y) => y - x)
  return (a + 0.05) / (b + 0.05)
}"""


def _open_assistant_on_phone(page):
    page.get_by_role("button", name="More actions").filter(visible=True).first.click()
    page.get_by_role("menuitem", name="Assistant").click()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, PHONE], ids=["desktop", "phone"])
def test_a_new_dashboard_opens_with_ask_and_starter_questions(page, width, scheme):
    _store, apps = _serve(page, width, scheme)
    _create(page)
    app = apps[-1]
    starters = _starters(page)
    if width == PHONE:
        # The drawer would cover the new canvas, so the questions wait in it.
        wait_until(lambda: app._editor.editable and app._agent_session.starters.shown, page)
        assert not app._editor.ask
        expect(starters).to_have_count(0)
        _open_assistant_on_phone(page)

    expect(starters).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    buttons = starters.locator(f".{STARTER_CLASS}")
    expect(buttons).to_have_count(len(STARTERS))
    for button, question in zip(buttons.all(), STARTERS, strict=True):
        expect(button).to_have_text(re.compile(re.escape(question)))
    assert app._editor.editable and app._editor.ask
    chat = page.get_by_placeholder(re.compile("^Ask the agent")).filter(visible=True)
    expect(chat).to_be_visible()

    # Each question fits the drawer, a touch target on a phone and one line elsewhere,
    # and its label reads clearly on the drawer.
    # Once the drawer has slid in.
    drawer_box = stable_box(page.locator(".MuiDrawer-paper").filter(has=starters).first, page)
    for button in buttons.all():
        box = button.bounding_box()
        assert box["x"] >= drawer_box["x"] and box["x"] + box["width"] <= (
            drawer_box["x"] + drawer_box["width"] + 0.5
        )
        assert box["height"] >= 44 if width == PHONE else box["height"] < 40
        assert button.evaluate(_CONTRAST) >= 4.5

    if SHOTS:
        # The drawer's icon font may still be loading on a page opened from the launcher.
        expect(page.get_by_role("button", name="New conversation")).to_be_visible()
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.wait_for_timeout(2000)
        page.screenshot(path=os.path.join(SHOTS, f"t222-{width}-{scheme}.png"))

    buttons.first.click()
    expect(starters).to_have_count(0)
    page.get_by_text(re.compile(r"Done: Added node 'note_[0-9a-f]{6}'\.")).wait_for(timeout=30000)
    wait_until(lambda: len(app._editor._tile_items) == 1, page)
    expect(page.get_by_text(STARTERS[0]).filter(visible=True).first).to_be_visible()


def test_a_long_question_wraps_within_a_phones_drawer(page):
    question = (
        "Which wind farms in the northern plains added the most capacity since 2015, "
        "and how does that compare with the national average?"
    )
    _store, apps = _serve(page, PHONE, "light", agent_starters=lambda context: [question])
    _create(page)
    wait_until(lambda: apps[-1]._agent_session.starters.shown, page, timeout=10000)
    _open_assistant_on_phone(page)
    button = _starters(page).locator(f".{STARTER_CLASS}")
    expect(button).to_have_text(re.compile("national average"))
    drawer = stable_box(page.locator(".MuiDrawer-paper").filter(has=_starters(page)).first, page)
    box = button.bounding_box()
    assert box["x"] >= drawer["x"] and box["x"] + box["width"] <= drawer["x"] + drawer["width"]
    assert box["height"] > 60
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE


def test_the_starters_leave_with_edit_mode_and_do_not_come_back(page):
    _store, apps = _serve(page, 1440, "light")
    _create(page)
    expect(_starters(page)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)

    page.get_by_role("button", name="Done", exact=True).filter(visible=True).click()
    page.get_by_role("button", name="Edit", exact=True).filter(visible=True).wait_for()
    page.get_by_role("button", name="Edit", exact=True).filter(visible=True).click()
    page.get_by_role("button", name="Done", exact=True).filter(visible=True).wait_for()
    page.wait_for_timeout(500)
    expect(_starters(page)).to_have_count(0)
    assert not apps[-1]._agent_session.starters.shown
    # Done gave view mode back its own Ask, off on the launcher, so Edit opens no drawer.
    assert not apps[-1]._editor.ask and not apps[-1]._agent_session.drawer.open
