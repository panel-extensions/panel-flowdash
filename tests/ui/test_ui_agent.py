"""UI test of the agent drawer: chatting with the agent edits the open dashboard."""

import re

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
