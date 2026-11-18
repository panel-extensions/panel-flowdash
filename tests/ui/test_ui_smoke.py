"""A smoke test through the redesigned app: launcher, view, edit and back."""

import re
from urllib.parse import urlparse

import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardEdge
from tests.test_editor import CHART, COMPONENTS, SHOUTER

from .util import DASHBOARD_LOAD_TIMEOUT

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _store():
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id="shout", component_id=SHOUTER, x=0, y=0),
        DashboardItem(instance_id="chart", component_id=CHART, x=320, y=0),
    ]
    model.edges = [DashboardEdge("shout", "shouted", "chart", "ticker")]
    store.save_dashboard(model)
    return store, model.dashboard_id


def test_launcher_view_edit_and_back(page):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text))
    store, dashboard_id = _store()

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )

    serve_component(page, {"/": factory, "/components": factory, "/dash/[^/]+": factory})

    card = page.locator(".flowdash-dashboard-card")
    expect(card).to_have_count(1)
    card.get_by_text("Plant", exact=True).click()
    wait_until(lambda: urlparse(page.url).path == f"/dash/{dashboard_id}", page)
    expect(page.get_by_text("chart", exact=True)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(page.locator(".flowdash-dashboard-card")).to_have_count(0)

    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Done").wait_for()
    wait_until(lambda: page.url.endswith("?edit=true"), page)
    expect(page.locator(".react-flow__node")).to_have_count(2)
    page.get_by_role("button", name="Layout").click()
    expect(page.locator(".react-flow__node")).to_have_count(0)
    expect(page.get_by_text("chart", exact=True)).to_be_visible()
    page.get_by_role("button", name="Dataflow").click()
    expect(page.locator(".react-flow__node")).to_have_count(2)

    page.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="Refresh").wait_for()
    wait_until(lambda: "edit=true" not in page.url, page)
    expect(page.get_by_text("chart", exact=True)).to_be_visible()

    page.locator(".header").get_by_role("button", name=re.compile("^Dashboards$")).first.click()
    wait_until(lambda: urlparse(page.url).path == "/", page)
    expect(page.locator(".flowdash-dashboard-card")).to_have_count(1)
    assert errors == []
