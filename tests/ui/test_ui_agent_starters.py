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
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component

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


def _serve(page, width, scheme):
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


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, PHONE], ids=["desktop", "phone"])
def test_a_new_dashboard_opens_with_ask_and_starter_questions(page, width, scheme):
    _store, apps = _serve(page, width, scheme)
    _create(page)

    starters = _starters(page)
    expect(starters).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    buttons = starters.locator(f".{STARTER_CLASS}")
    expect(buttons).to_have_count(len(STARTERS))
    for button, question in zip(buttons.all(), STARTERS, strict=True):
        expect(button).to_have_text(re.compile(re.escape(question)))
    app = apps[-1]
    assert app._editor.editable and app._editor.ask
    chat = page.get_by_placeholder(re.compile("^Ask the agent")).filter(visible=True)
    expect(chat).to_be_visible()

    # Each question is one line within the drawer.
    drawer = page.locator(".MuiDrawer-paper").filter(has=starters).first
    drawer_box = drawer.bounding_box()
    for button in buttons.all():
        box = button.bounding_box()
        assert box["x"] >= drawer_box["x"] and box["x"] + box["width"] <= (
            drawer_box["x"] + drawer_box["width"] + 0.5
        )
        assert box["height"] < 40

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
