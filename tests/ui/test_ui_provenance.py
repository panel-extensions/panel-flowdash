"""UI test of "How was this made?": a node the agent added shows the request that made it."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def test_a_node_the_agent_added_shows_how_it_was_made(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Add another note")
    chat.press("Enter")
    page.get_by_text(re.compile(r"Done: Added node 'note_[0-9a-f]{6}'\.")).wait_for(timeout=20000)
    editor = apps[0]._editor
    wait_until(lambda: len(editor._tile_items) == 2, page, timeout=20000)
    added = next(
        i["instance_id"] for i in editor._tile_items if editor.made_by_agent(i["instance_id"])
    )

    page.get_by_role("button", name="Assistant", exact=True).click()
    page.locator(f".react-flow__node[data-id='{added}']").click()
    page.get_by_role("button", name="How was this made?").click()

    sheet = page.locator(".flowdash-inspect-sheet")
    expect(sheet.get_by_text("How this was made")).to_be_visible()
    expect(sheet.locator(".sv-question")).to_have_text("Add another note")
    expect(sheet.locator(".tool-call-card", has_text="add_node")).to_be_visible()
    expect(sheet.locator(".model-call-card").first).to_be_visible()
    expect(sheet.get_by_text("Published", exact=True)).to_be_visible()
    expect(
        sheet.get_by_text(re.compile("^The steps of this run were not recorded"))
    ).to_be_hidden()
