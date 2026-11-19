"""UI tests of "How was this made?": a node the agent added shows the request that made it."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_agents import RunState, events as ev
from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.turns import agent_edits

from ..test_agent_ops import _editor
from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _ask_for_a_note(page):
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
    return editor, added


def test_the_inspector_shows_how_a_node_the_agent_added_was_made(page):
    _, added = _ask_for_a_note(page)
    page.get_by_role("button", name="Assistant", exact=True).click()
    page.locator(f".react-flow__node[data-id='{added}']").click()
    page.get_by_role("button", name="How was this made?").click()

    expect(page.get_by_text("How this was made", exact=True)).to_be_visible()
    view = page.locator(".span-view")
    expect(view.locator(".sv-question")).to_have_text("Add another note")
    expect(view.locator(".tool-call-card", has_text="add_node")).to_be_visible()
    expect(view.locator(".model-call-card").first).to_be_visible()
    expect(view.get_by_text("Published", exact=True)).to_be_visible()
    expect(page.locator(".flowdash-inspect-sheet .span-view")).to_have_count(0)
    # The Save and Done buttons stay reachable.
    expect(page.get_by_role("button", name="Done")).to_be_visible()

    page.get_by_role("button", name="Back").click()
    expect(page.locator(".span-view")).to_have_count(0)


def test_the_tile_menu_shows_it_and_a_viewer_sees_their_own_request_in_full(page):
    editor, _added = _ask_for_a_note(page)
    page.get_by_role("button", name="Assistant", exact=True).click()
    page.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="Save and leave").click()
    wait_until(lambda: not editor.editable, page, timeout=8000)

    page.locator(".flowdash-tile").last.hover()
    page.locator(".flowdash-tile").last.get_by_role("button", name=re.compile("^More for")).click()
    page.get_by_role("menuitem", name="How was this made?").click()
    sheet = page.locator(".flowdash-inspect-sheet")
    expect(sheet.locator(".sv-question")).to_have_text("Add another note")

    # Viewing only, the person who asked still sees their own request.
    editor.read_only = True
    expect(sheet.locator(".sv-question")).to_have_text("Add another note")
    page.get_by_role("button", name="Close inspector").click()
    page.get_by_role("button", name="Assistant", exact=True).click()
    page.locator(".run-details button").click()
    expect(page.get_by_role("dialog").locator(".sv-question")).to_have_text("Add another note")


def test_someone_elses_request_is_redacted_for_viewers_and_for_restricted_nodes(page):
    store = MemoryDashboardStore()
    editor = _editor(store, finance=True)
    editor.new_dashboard("Pay")
    with agent_edits(run="run-a", question="Double the number"):
        editor.add_component("T/number", instance_id="base")
    with agent_edits(run="run-s", question="Show salaries"):
        editor.add_component("T/secret", instance_id="secret")
    editor.agent_state = RunState.from_events(
        [
            ev.RunStarted(run_id="run-a", input="Double the number", timestamp=0),
            ev.ToolCallStart(tool_call_id="c", tool_name="add_node", run_id="run-a", timestamp=1),
            ev.ToolCallResult(tool_call_id="c", content="ok", run_id="run-a", timestamp=2),
            ev.RunFinished(run_id="run-a", timestamp=3),
        ]
    )
    editor.authorize_component = lambda entry: entry.app_id != "T/secret"
    editor.read_only = True
    editor.editable = False
    serve_component(page, editor)

    page.locator(".flowdash-tile").first.hover()
    page.get_by_role("button", name=re.compile("^More for")).first.click()
    page.get_by_role("menuitem", name="How was this made?").click()
    sheet = page.locator(".flowdash-inspect-sheet")
    expect(sheet.locator(".sv-question")).to_have_text("The question is hidden.")
    expect(sheet.locator(".tool-call-card", has_text="add_node")).to_be_visible()
    expect(sheet.get_by_text("Double the number")).to_have_count(0)

    editor.show_how_made("secret")
    expect(sheet.get_by_text("Made by the assistant", exact=False)).to_be_visible()
    expect(sheet.get_by_text("Show salaries")).to_have_count(0)
    expect(sheet.get_by_text("Added secret")).to_have_count(0)
