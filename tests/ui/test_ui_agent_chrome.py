"""UI tests: asking the assistant from the command palette, and the drawer beside the inspector."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.ui.command_palette import PLACEHOLDER

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _agent():
    return Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


def test_a_question_typed_in_the_palette_goes_to_the_assistant(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=_agent())
    page.get_by_text("note tile").first.wait_for()

    page.keyboard.press("Control+k")
    search = page.get_by_role("combobox", name=PLACEHOLDER)
    search.press_sequentially("why is this tile empty")
    first = page.get_by_role("option").first
    expect(first).to_contain_text("Ask the assistant: “why is this tile empty”")
    expect(first).to_have_attribute("aria-selected", "true")
    search.press("Enter")

    wait_until(lambda: apps[0]._agent_session.drawer.open, page, timeout=10000)
    expect(page.get_by_text("why is this tile empty", exact=True)).to_be_visible(timeout=20000)


def test_a_short_query_that_matches_keeps_the_question_last(page):
    store, (dash,) = _store_with("Plant")
    _serve_app(page, store, f"/dash/{dash}", agent=_agent())
    page.get_by_text("note tile").first.wait_for()

    page.keyboard.press("Control+k")
    page.get_by_role("combobox", name=PLACEHOLDER).press_sequentially("plant")
    options = page.get_by_role("option")
    expect(options.last).to_contain_text("Ask the assistant: “plant”")
    expect(options.first).not_to_contain_text("Ask the assistant")


def test_opening_the_drawer_folds_the_inspector_and_closing_restores_it(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=_agent())
    page.get_by_role("button", name="Done").wait_for()
    inspector = apps[0]._editor._inspector
    wait_until(lambda: inspector.visible and not inspector.collapsed, page, timeout=10000)

    page.get_by_role("button", name="Assistant", exact=True).click()
    wait_until(lambda: inspector.collapsed, page, timeout=10000)
    page.get_by_role("button", name="Close", exact=True).click()
    wait_until(lambda: not inspector.collapsed, page, timeout=10000)
