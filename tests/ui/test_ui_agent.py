"""UI test of the agent drawer: chatting with the agent edits the open dashboard."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with

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
    # The drawer's toggle is a robot icon in the top bar.
    page.get_by_text("smart_toy", exact=True).click(force=True)
    chat = page.get_by_role("textbox").last
    chat.fill("Add another note")
    chat.press("Enter")

    wait_until(lambda: len(apps[0]._editor._tile_items) == 2, page, timeout=20000)
    expect(page.get_by_text("Unsaved changes")).to_be_visible(timeout=20000)
    page.get_by_text("Done: Added node 'note'.").wait_for(timeout=20000)
    assert apps[0]._editor.editable
    assert errors == []
