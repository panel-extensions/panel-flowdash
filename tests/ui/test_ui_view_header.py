"""UI tests for the view-mode header, controls bar, pipeline strip and issue banner."""

import re

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import DashboardEdge, DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.ui.issue_banner import ISSUE_CLASS

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, sidebar=True, title="Unit Picker")
class UnitPicker(Viewer):
    """A control publishing the unit to show."""

    unit = param.String(default="kW")

    @param.output(param.String)
    @param.depends("unit")
    def picked(self):
        return self.unit

    def __panel__(self):
        return "unit picker control"


@register(page=False, component=True, title="Output Chart")
class OutputChart(Viewer):
    """A tile that needs a unit before it can draw."""

    unit = param.String(default=None, allow_None=True, metadata={"flowdash": {"required": True}})

    def __panel__(self):
        return "output chart tile"


COMPONENTS = {"Test/picker": UnitPicker, "Test/chart": OutputChart}

# Finds elements carrying a class through the nested shadow roots Panel renders into.
_DEEP_FIND = """
(cls) => {
  const found = []
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.classList.contains(cls)) { found.push(el) }
      if (el.shadowRoot) { walk(el.shadowRoot) }
    }
  }
  walk(document)
  return found.map((el) => el.style.outline || "none")
}
"""


def _store(description="", period=0, waiting=True):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id="picker", component_id="Test/picker", x=0, y=0),
        DashboardItem(instance_id="wired", component_id="Test/chart", x=300, y=0),
    ]
    if waiting:
        model.items.append(
            DashboardItem(instance_id="waiting", component_id="Test/chart", x=300, y=200)
        )
    model.edges = [DashboardEdge("picker", "picked", "wired", "unit")]
    model.settings = {"description": description, "refresh_period": period}
    store.save_dashboard(model)
    return store, model.dashboard_id


def _serve(page, store, dash, apps, notifications=False, header_layout="compact"):
    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=notifications,
            header_layout=header_layout,
        )
        apps.append(app)
        return app

    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=f"/dash/{dash}")
    page.get_by_text("output chart tile").first.wait_for()


def _refresh(page):
    return page.get_by_role("button", name=re.compile(r"^Refresh now"))


def _open_health(page):
    page.locator(".fd-health-chip").get_by_role("button").click()
    expect(page.locator(".MuiPopover-paper").last).to_be_visible()


@pytest.mark.parametrize("header_layout", ["compact", "full"])
def test_header_shows_description_health_and_refresh(page, header_layout):
    store, dash = _store(description="Output per unit, live.", period=900)
    _serve(page, store, dash, [], header_layout=header_layout)

    if header_layout == "compact":
        page.get_by_role("button", name="About this dashboard").click()
        about = page.get_by_role("dialog", name="About this dashboard")
        expect(about.get_by_text("Output per unit, live.")).to_be_visible()
        page.keyboard.press("Escape")
        expect(about).to_be_hidden()
    else:
        bar = page.locator(".fd-dashboard-bar")
        expect(bar.get_by_text("Output per unit, live.")).to_be_visible()
        expect(bar.get_by_text(re.compile(r"auto-refresh every 15 min$"))).to_be_visible()
    expect(page.locator(".fd-health-chip").get_by_text("1 waiting", exact=True)).to_be_visible()
    expect(_refresh(page)).to_have_accessible_name(re.compile(r"auto-refresh every 15 min\.$"))
    expect(page.get_by_role("button", name="Update automatically")).to_contain_text("15 min")


def test_period_menu_sets_the_refresh_period(page):
    store, dash = _store()
    apps = []
    _serve(page, store, dash, apps)

    period = page.get_by_role("button", name="Update automatically")
    expect(period).to_contain_text("Off")
    period.click()
    page.get_by_role("menuitem", name="Every 5 min").click()
    expect(period).to_contain_text("5 min")
    expect(_refresh(page)).to_have_accessible_name(re.compile(r"auto-refresh every 5 min\.$"))
    editor = apps[-1]._editor
    assert editor.settings.refresh_period == 300
    assert not editor.dirty


def test_controls_bar_holds_the_sidebar_components_and_the_pipeline(page):
    store, dash = _store()
    apps = []
    _serve(page, store, dash, apps)

    bar = page.locator(".fd-controls-bar")
    expect(bar.get_by_text("unit picker control")).to_be_visible()
    expect(page.locator(".fd-pipeline")).to_be_hidden()
    _open_health(page)
    pipeline = page.locator(".fd-pipeline")
    expect(pipeline.get_by_text("Pipeline")).to_be_visible()
    expect(pipeline.get_by_text("Unit Picker")).to_be_visible()
    expect(pipeline.get_by_text("Output Chart")).to_be_visible()
    assert apps[-1]._page.sidebar == []


def test_controls_bar_moves_to_the_sidebar_when_switched_off(page):
    store, dash = _store()

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            editor_options={"controls_bar": False},
        )

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{dash}")
    page.get_by_text("output chart tile").first.wait_for()
    expect(page.locator(".fd-controls-bar").get_by_text("unit picker control")).to_have_count(0)
    page.get_by_text("unit picker control").wait_for(state="attached")
    # The control moves into the page sidebar without blanking the tiles.
    expect(
        page.locator(".MuiDrawer-paper.sidebar").get_by_text("unit picker control")
    ).to_be_visible()
    expect(page.get_by_text("output chart tile").first).to_be_visible()


def test_issue_banner_scrolls_to_and_highlights_the_waiting_tile(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    _open_health(page)
    banner = page.locator(".fd-issue-banner")
    expect(banner.get_by_text("1 tile is waiting for input.")).to_be_visible()
    expect(banner.get_by_text("Other tiles show current data.")).to_be_visible()
    banner.get_by_role("button", name="Show issues").click()
    wait_until(lambda: page.evaluate(_DEEP_FIND, ISSUE_CLASS) != [], page, timeout=5000)
    outlines = page.evaluate(_DEEP_FIND, ISSUE_CLASS)
    assert len(outlines) == 1
    assert "solid" in outlines[0]


def test_no_banner_and_all_up_to_date_without_issues(page):
    store, dash = _store(waiting=False)
    _serve(page, store, dash, [])

    expect(page.locator(".fd-health-chip").get_by_text("1/1 up to date")).to_be_visible()
    _open_health(page)
    expect(page.get_by_text("Every tile shows current data.")).to_be_visible()
    expect(page.locator(".fd-issue-banner")).to_be_hidden()


def test_loading_a_dashboard_raises_no_notification(page):
    store, dash = _store()
    _serve(page, store, dash, [], notifications=True)

    page.wait_for_timeout(1000)
    expect(page.get_by_text("Loaded dashboard")).to_have_count(0)
