"""UI tests of adding an answer from Ask in view mode, which hands it to the assistant in edit mode."""

import os
import re
from pathlib import Path

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_agents import AgentChat
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.agent import FlowDashDeps, dashboard_toolset
from panel_flowdash.agent.highlight import TILE_CLASSES
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .util import AGENT_RUN_TIMEOUT, DASHBOARD_LOAD_TIMEOUT, ask_button, serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

MENTIONS = "mentions" in AgentChat.param
QUESTION = "Ask a question about this dashboard…"
PHONE = 390
# Screenshots for review, when FLOWDASH_SHOTS names a folder.
SHOTS = os.environ.get("FLOWDASH_SHOTS")


@register(page=False, component=True, title="Panel", default_size={"width": 600, "height": 360})
def panel(config):
    return "panel tile"


@register(page=False, component=True, title="Answer", default_size={"width": 600, "height": 300})
def answer(config):
    return "answer tile"


COMPONENTS = {"Test/panel": panel, "Test/answer": answer}


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _assistant(prompts):
    """The assistant, recording what it is asked."""

    async def stream(messages, info):
        request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
        prompts.extend(p.content for p in request.parts if isinstance(p, UserPromptPart))
        yield "Noted."

    return Agent(FunctionModel(stream_function=stream), deps_type=FlowDashDeps)


def _serve(page, *, width=1400, scheme="light", tiles=4, prompts=None):
    """Serve a dashboard of *tiles* tall panels, whose Explore agent answers with a tile of its own."""
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 844 if width == PHONE else 900})
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id=f"panel_{i}", component_id="Test/panel", x=0, y=200 * i)
        for i in range(tiles)
    ]
    store.save_dashboard(model)
    explore = Agent(
        scripted(("add_node", {"component": "Test/answer", "name": "memo"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=_assistant(prompts if prompts is not None else []),
            explore_agent=explore,
            page_options={"dark_theme": scheme == "dark"},
        )
        apps.append(app)
        return app

    serve_component(
        page, {"/": factory, "/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}"
    )
    page.get_by_text("panel tile").first.wait_for(timeout=DASHBOARD_LOAD_TIMEOUT)
    return apps


def _ask_explore(page, apps, phone=False):
    """Ask the Explore agent in view mode, Ask docked or, on a phone, the Explore view, and return the answer's card."""
    if phone:
        page.get_by_role("button", name="More actions").click()
        page.get_by_role("menuitem", name="Ask").click()
    else:
        ask = ask_button(page)
        expect(ask).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
        ask.click()
    composer = page.get_by_placeholder(QUESTION)
    composer.fill("Add a memo")
    composer.press("Enter")
    runner = apps[0]._agent_session.explore_runner
    wait_until(
        lambda: bool(runner.state.runs) and not runner.state.running,
        page,
        timeout=AGENT_RUN_TIMEOUT,
    )
    card = page.locator(".artifact-cell", has_text="memo")
    expect(card.get_by_role("button", name="Add to dashboard")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    return card


def _in_view(page, locator):
    box = locator.bounding_box()
    size = page.viewport_size
    return box is not None and box["y"] >= 0 and box["y"] + box["height"] <= size["height"]


def _notice(page):
    return page.locator(".fd-added-notice")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_an_answer_added_from_the_dock_is_handed_to_the_assistant(page, scheme):
    prompts = []
    apps = _serve(page, scheme=scheme, prompts=prompts)
    card = _ask_explore(page, apps)
    card.get_by_role("button", name="Add to dashboard").click()

    # Edit mode on the Layout tab, the new tile marked and scrolled into view below the others.
    tile = page.locator(f".{TILE_CLASSES['add']}")
    expect(tile).to_have_count(1, timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(tile.get_by_text("answer tile")).to_be_visible()
    wait_until(lambda: _in_view(page, tile), page, timeout=5000)
    editor, session = apps[0]._editor, apps[0]._agent_session
    assert editor.editable and editor.mode == "dashboard"
    expect(page.locator(".flowdash-explore")).to_have_count(0)
    # The assistant's drawer opens beside it, the new node mentioned in its input.
    expect(ask_button(page)).to_have_attribute("aria-pressed", "true")
    assistant = page.get_by_placeholder("Ask the agent…")
    expect(assistant).to_be_visible()
    notice = _notice(page)
    expect(notice).to_contain_text("Added memo to the dashboard")
    expect(notice.get_by_role("button", name="Undo")).to_be_visible()
    expect(notice.get_by_role("button", name="Assistant")).to_have_count(0)
    # The notice sits over the tiles, clear of the drawer.
    drawer_left = assistant.bounding_box()["x"]
    box = notice.bounding_box()
    assert box["x"] + box["width"] < drawer_left
    if MENTIONS:
        mention = page.locator(".agent-chat-mention")
        expect(mention).to_contain_text("memo")
        _shot(page, f"desktop-handover-{scheme}")
        assistant.fill("make it wider")
        assistant.press("Enter")
        expect(page.get_by_text("@memo make it wider", exact=True)).to_be_visible(
            timeout=AGENT_RUN_TIMEOUT
        )
        wait_until(lambda: any(p.endswith("@memo make it wider") for p in prompts), page)
        expect(mention).to_have_count(0)

    # Done brings Ask back beside the dashboard, where the card says where the answer went.
    page.get_by_role("button", name="Done").first.click()
    page.get_by_role("button", name="Save and leave").click()
    page.get_by_role("button", name="Edit", exact=True).wait_for()
    expect(page.locator(".flowdash-explore")).to_be_visible()
    expect(card.locator(".artifact-cell-done")).to_have_text(
        re.compile("On dashboard as memo$"), timeout=DASHBOARD_LOAD_TIMEOUT
    )
    expect(card.get_by_role("button", name="Add to dashboard")).to_have_count(0)
    expect(_notice(page)).to_be_hidden()
    assert editor.ask_docked and not session.drawer.open
    if scheme == "light":
        _shot(page, "desktop-on-dashboard-light")


def test_undo_takes_the_answer_back_and_returns_to_the_docked_feed(page):
    apps = _serve(page)
    card = _ask_explore(page, apps)
    card.get_by_role("button", name="Add to dashboard").click()
    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(
        1, timeout=DASHBOARD_LOAD_TIMEOUT
    )
    _notice(page).get_by_role("button", name="Undo").click()

    # Straight back to view mode, with no unsaved changes to ask about.
    page.get_by_role("button", name="Edit", exact=True).wait_for(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(page.get_by_role("button", name="Save and leave")).to_have_count(0)
    expect(page.locator(".flowdash-explore")).to_be_visible()
    expect(card.get_by_role("button", name="Add to dashboard")).to_be_visible()
    expect(page.get_by_text("answer tile")).to_have_count(1)
    editor = apps[0]._editor
    assert not editor.editable and not editor.dirty
    assert "memo" not in [i["instance_id"] for i in editor._tile_items]
    expect(_notice(page)).to_be_hidden()
    assert getattr(apps[0]._agent_session.drawer.chat, "mentions", []) == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_phone_keeps_the_new_tile_in_sight_and_opens_the_assistant_from_the_notice(page, scheme):
    apps = _serve(page, width=PHONE, scheme=scheme, tiles=2)
    card = _ask_explore(page, apps, phone=True)
    card.get_by_role("button", name="Add to dashboard").click()

    tile = page.locator(f".{TILE_CLASSES['add']}")
    expect(tile).to_have_count(1, timeout=DASHBOARD_LOAD_TIMEOUT)
    wait_until(lambda: _in_view(page, tile), page, timeout=5000)
    notice = _notice(page)
    expect(notice).to_have_text(re.compile("^Added memo"))
    expect(notice.get_by_role("button", name="Undo")).to_be_visible()
    box = notice.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE
    # Clear of the Layout tab's floating button.
    fab = page.get_by_role("button", name="Layout").filter(visible=True).last
    if fab.count():
        assert (
            box["y"] + box["height"] <= fab.bounding_box()["y"]
            or box["x"] + box["width"] <= fab.bounding_box()["x"]
        )
    session = apps[0]._agent_session
    assert not session.drawer.open
    _shot(page, f"phone-added-{scheme}")

    notice.get_by_role("button", name="Assistant").click()
    assistant = page.get_by_placeholder("Ask the agent…")
    expect(assistant).to_be_visible()
    # The notice makes way for the drawer, whose input it would cover.
    expect(notice).to_be_hidden()
    if MENTIONS:
        expect(page.locator(".agent-chat-mention")).to_contain_text("memo")
    _shot(page, f"phone-assistant-{scheme}")


def test_undo_on_a_phone_returns_to_the_explore_view(page):
    apps = _serve(page, width=PHONE, tiles=2)
    card = _ask_explore(page, apps, phone=True)
    card.get_by_role("button", name="Add to dashboard").click()
    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(
        1, timeout=DASHBOARD_LOAD_TIMEOUT
    )
    _notice(page).get_by_role("button", name="Undo").click()
    expect(card.get_by_role("button", name="Add to dashboard")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    editor = apps[0]._editor
    wait_until(lambda: not editor.editable and editor.surface == "explore", page)
