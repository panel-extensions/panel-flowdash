"""UI tests: asking the assistant from the command palette, and the drawer beside the inspector."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.ui.command_palette import PLACEHOLDER

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with
from .util import ask_button, open_assistant, stable_box

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


def test_ask_looks_like_the_buttons_beside_it(page):
    # On a dashboard the bar's Ask stands in for the top bar's Assistant.
    store, (dash,) = _store_with("Plant")
    _serve_app(page, store, f"/dash/{dash}", agent=_agent())
    page.get_by_text("note tile").first.wait_for()
    expect(page.get_by_role("button", name="Assistant", exact=True)).to_have_count(0)

    keys = ["textTransform", "fontSize", "fontWeight", "lineHeight", "letterSpacing"]

    def look(button):
        style = button.evaluate(
            "(el, keys) => Object.fromEntries(keys.map(k => [k, getComputedStyle(el)[k]]))", keys
        )
        box = stable_box(button, page)
        return style, box["height"], round(box["y"] + box["height"] / 2)

    ask, edit = look(ask_button(page)), look(page.get_by_role("button", name="Edit", exact=True))
    assert ask[0]["textTransform"] == "none"
    assert ask == edit


def test_opening_the_drawer_folds_the_inspector_and_closing_restores_it(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=_agent())
    page.get_by_role("button", name="Done").wait_for()
    inspector = apps[0]._editor._inspector
    wait_until(lambda: inspector.visible and not inspector.collapsed, page, timeout=10000)

    open_assistant(page)
    wait_until(lambda: inspector.collapsed, page, timeout=10000)
    page.get_by_role("button", name="Close", exact=True).click()
    wait_until(lambda: not inspector.collapsed, page, timeout=10000)


def test_below_1600_px_the_drawer_and_the_sidecar_share_one_slot(page):
    page.set_viewport_size({"width": 1280, "height": 900})
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=_agent())
    page.get_by_role("button", name="Done").wait_for()
    editor = apps[0]._editor
    inspector, drawer = editor._inspector, apps[0]._agent_session.drawer
    wait_until(lambda: editor._split_view.storage.window_width == 1280, page, timeout=10000)
    wait_until(lambda: inspector.visible and not inspector.collapsed, page, timeout=10000)

    open_assistant(page)
    wait_until(lambda: drawer.open and inspector.collapsed, page, timeout=10000)
    # Opening the sidecar again takes the slot back from the drawer.
    show = page.get_by_role("button", name="Show inspector")
    expect(show).to_be_visible(timeout=10000)
    show.click()
    wait_until(lambda: not drawer.open and not inspector.collapsed, page, timeout=10000)
    # The person's choice holds across tabs.
    page.get_by_role("button", name="Hide inspector").first.click()
    wait_until(lambda: inspector.collapsed, page, timeout=10000)
    page.get_by_role("button", name="Layout", exact=True).click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=10000)
    expect(page.get_by_role("button", name="Show inspector")).to_be_visible(timeout=10000)
    assert inspector.collapsed and not drawer.open


def test_narrowing_the_window_leaves_one_of_the_drawer_and_the_sidecar(page):
    page.set_viewport_size({"width": 1920, "height": 900})
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=_agent())
    page.get_by_role("button", name="Done").wait_for()
    editor = apps[0]._editor
    inspector, drawer = editor._inspector, apps[0]._agent_session.drawer
    wait_until(lambda: inspector.visible and not inspector.collapsed, page, timeout=10000)
    open_assistant(page)
    wait_until(lambda: drawer.open, page, timeout=10000)
    assert not inspector.collapsed

    page.set_viewport_size({"width": 1280, "height": 900})
    wait_until(lambda: editor._split_view.storage.window_width == 1280, page, timeout=10000)
    wait_until(lambda: drawer.open and inspector.collapsed, page, timeout=10000)


def test_in_a_wide_window_the_drawer_opens_beside_the_sidecar(page):
    page.set_viewport_size({"width": 1920, "height": 900})
    store, (dash,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=_agent())
    page.get_by_role("button", name="Done").wait_for()
    editor = apps[0]._editor
    inspector, drawer = editor._inspector, apps[0]._agent_session.drawer
    page.get_by_role("button", name="Dataflow").click()
    wait_until(lambda: editor.mode == "wiring" and not inspector.collapsed, page, timeout=10000)
    open_assistant(page)
    wait_until(lambda: drawer.open, page, timeout=10000)
    page.wait_for_timeout(500)
    assert not inspector.collapsed
    # The Split tab needs more room than is left beside both, so the sidecar folds there.
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split" and inspector.collapsed, page, timeout=10000)
    assert drawer.open
