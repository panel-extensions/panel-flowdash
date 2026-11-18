"""UI test of the agent drawer: chatting with the agent edits the open dashboard."""

import re

import panel as pn
import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import responses, scripted
from ..untrusted_payloads import ALL, PAYLOADS
from .test_ui_shell import COMPONENTS, _serve_app, _store_with
from .test_ui_text_math_escaping import _FOUND
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def test_the_agent_adds_a_component_from_the_drawer(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Add another note")
    chat.press("Enter")

    wait_until(lambda: len(apps[0]._editor._tile_items) == 2, page, timeout=20000)
    expect(page.get_by_text("Unsaved changes")).to_be_visible(timeout=20000)
    page.get_by_text(re.compile(r"Done: Added node 'note_[0-9a-f]{6}'\.")).wait_for(timeout=20000)
    assert apps[0]._editor.editable
    assert errors == []

    # Closed, the drawer takes no width: none of its controls is visible (so none can
    # take focus) and the editor's status bar, which spans the main area, reaches the
    # right edge of the window.
    page.get_by_role("button", name="Assistant", exact=True).click()
    expect(chat).to_be_hidden()
    expect(page.get_by_role("button", name="New conversation")).to_be_hidden()
    zoom = page.get_by_text(re.compile(r"^Zoom \d+%$"))
    width = page.viewport_size["width"]
    wait_until(
        lambda: (box := zoom.bounding_box()) and box["x"] + box["width"] > width - 120, page
    )


def test_untrusted_text_reaches_the_drawer_as_text(page):
    # Other people's titles, descriptions and config reach the drawer through
    # tool results, versions and approvals; none of it may run, load or link.
    store, (dash,) = _store_with("Plant " + PAYLOADS["raw_html"] + " " + PAYLOADS["dd_href"])
    model = store._load_any(dash)
    model.description = ALL
    store.save_dashboard(model)
    agent = Agent(
        responses(
            [("get_dashboard", {}), ("list_dashboards", {})],
            [("set_settings", {"settings": {"description": "new " + ALL}})],
            [("remove_node", {"name": PAYLOADS["raw_html"]})],
            [("save_dashboard", {})],
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "evil.test" in r.url else None)
    page.route("**/*evil.test*/**", lambda route: route.abort())
    apps = []

    def factory():
        pn.extension("mathjax")
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=agent,
        )
        apps.append(app)
        return app

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{dash}")
    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Tidy up " + PAYLOADS["raw_html"])
    chat.press("Enter")
    session = apps[0]._agent_session
    wait_until(lambda: bool(session.runner.state.pending_approvals), page, timeout=30000)
    for tab in ("chat", "artifacts", "approvals"):
        session.drawer.param.update(tab=tab)
        page.wait_for_timeout(1500)
        found = page.evaluate(_FOUND)
        assert found["math"] == 0, tab
        assert found["images"] == [], tab
        assert not [h for h in found["links"] if not h.startswith("https://evil.test/")], tab
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__pwned || []") == []
    assert requests == []


def test_the_full_screen_notice_shows_a_tools_description_as_text(page):
    # Third-party tools (MCP servers, a project's own) write their own descriptions.
    store, (dash,) = _store_with("Plant")

    def fetch_report(ctx: RunContext[FlowDashDeps]) -> str:
        if not ctx.tool_call_approved:
            raise ApprovalRequired
        return "ok"

    # Short enough to reach the notice whole.
    fetch_report.__doc__ = "<img src=x onerror=\"window.__pwned=['notice']\"> Fetch. More."
    agent = Agent(
        scripted(("fetch_report", {})), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]
    )
    agent.tool(fetch_report)
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "evil.test" in r.url else None)
    page.route("**/*evil.test*/**", lambda route: route.abort())
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Fetch the report")
    chat.press("Enter")
    session = apps[0]._agent_session
    wait_until(lambda: bool(session.runner.state.pending_approvals), page, timeout=30000)
    page.get_by_role("button", name="Full screen").first.click()
    notice = page.get_by_text(re.compile("^The assistant is waiting for your approval"))
    expect(notice).to_be_visible(timeout=10000)
    expect(notice).to_contain_text("<img")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__pwned || []") == []
    assert requests == []
