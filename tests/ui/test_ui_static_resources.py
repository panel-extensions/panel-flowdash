"""UI tests that a dashboard's route loads extension resources from the server root."""

from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .test_ui_header_overflow import _store
from .test_ui_view_header import COMPONENTS
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

TITLE = "Resources"


def _serve(page, suffix):
    store, dash = _store(TITLE)

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]),
        )

    responses = []
    page.on("response", lambda response: responses.append(response))
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix.format(dash=dash))
    return dash, responses


def _extension_requests(responses):
    found = [r for r in responses if "/static/extensions/" in r.url]
    assert found
    for response in found:
        path = urlparse(response.url).path
        assert path.startswith("/static/extensions/"), f"{path} is not under the server root"
        assert response.status < 400, f"{response.status} {path}"
    return found


def test_a_dashboard_route_loads_its_resources_from_the_root(page):
    _, responses = _serve(page, "/dash/{dash}")
    expect(page.locator(".flowdash-tile").first).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)

    _extension_requests(responses)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Panel 1.11 requests the stylesheets of tiles rendered after an in-app route "
        "change relative to the new route, e.g. /dash/static/extensions/..., which "
        "`flowdash serve` answers with a catch-all static route; drop that route once this passes"
    ),
)
def test_opening_a_dashboard_from_home_loads_its_resources_from_the_root(page):
    dash, responses = _serve(page, "/")
    page.get_by_role("button", name="Dashboards").click()
    page.get_by_text(TITLE, exact=True).first.click()
    opened = len(responses)
    wait_until(lambda: page.evaluate("location.pathname") == f"/dash/{dash}", page, timeout=10000)
    expect(page.locator(".flowdash-tile").first).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)

    # The dashboard's own stylesheets are requested after the route changed.
    assert _extension_requests(responses[opened:])
