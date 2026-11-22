"""The agent drawer alongside the inspector, full screen and the command palette."""

import asyncio

import pytest

pytest.importorskip("pydantic_ai")

from panel_agents import events as ev
from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from .test_agent_tools import _app, scripted


@pytest.fixture
async def session(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id)
    return app._agent_session


def request(state, call_id="t1", description=None):
    state.apply_many(
        [
            ev.RunStarted(run_id="r"),
            ev.ApprovalRequested(
                tool_call_id=call_id,
                tool_name="save_dashboard",
                run_id="r",
                description=description
                or "Save the open dashboard. The user approves each save.\nIt stores:\n- a",
            ),
        ]
    )


async def test_the_inspector_folds_while_the_drawer_is_open(session):
    inspector = session.app._editor._inspector
    inspector.visible, inspector.collapsed = True, False
    session.drawer.open = True
    assert inspector.collapsed
    session.drawer.open = False
    assert not inspector.collapsed


async def test_an_inspector_shown_while_the_drawer_is_open_is_folded(session):
    inspector = session.app._editor._inspector
    inspector.visible, inspector.collapsed = False, False
    session.drawer.open = True
    assert not inspector.collapsed
    inspector.visible = True
    assert inspector.collapsed
    session.drawer.open = False
    assert not inspector.collapsed


async def test_an_inspector_the_user_reopened_stays_open(session):
    inspector = session.app._editor._inspector
    inspector.visible, inspector.collapsed = True, False
    session.drawer.open = True
    inspector.collapsed = False
    session.drawer.open = True
    session.drawer.open = False
    assert not inspector.collapsed
    inspector.collapsed = True
    session.drawer.open = True
    session.drawer.open = False
    assert inspector.collapsed


async def test_an_approval_in_full_screen_shows_a_notice_that_leads_to_the_drawer(session):
    editor = session.app._editor
    editor.fullscreen = True
    assert editor.fullscreen
    request(session.runner.state)
    chrome = session.chrome
    assert chrome.notice.visible
    assert (
        chrome._notice_text.object
        == r"The assistant is waiting for your approval\: Save the open dashboard"
    )
    chrome.review()
    assert not editor.fullscreen and not chrome.notice.visible
    assert session.drawer.open and session.drawer.tab == "chat"


async def test_no_notice_outside_full_screen(session):
    request(session.runner.state)
    assert not session.chrome.notice.visible


async def test_the_palette_offers_to_ask_the_assistant(session, monkeypatch):
    palette = session.app._command_palette
    assert palette is not None
    assert palette.palette.ask_label == "Ask the assistant"
    asked = []
    monkeypatch.setattr(session.chrome, "ask", asked.append)
    palette.ask = session.chrome.ask
    palette.run("ask:why is the map empty?")
    assert asked == ["why is the map empty?"]
    palette.ask = None
    assert palette.palette.ask_label is None
    palette.run("ask:ignored")
    assert asked == ["why is the map empty?"]


async def test_asking_opens_the_drawer_and_sends_the_question(session):
    sent = []

    async def submit(text):
        sent.append(text)

    session.drawer.chat.submit = submit
    session.chrome.ask("  Why is the map empty?  ")
    assert session.drawer.open and session.drawer.tab == "chat"
    for _ in range(5):
        await asyncio.sleep(0)
    assert sent == ["Why is the map empty?"]


async def test_the_notice_shows_a_tools_description_as_text(session):
    session.app._editor.fullscreen = True
    request(session.runner.state, description='<img src=x onerror="alert(1)"> Fetch. More.')
    text = session.chrome._notice_text
    assert "<img" not in text.object and "&lt;img" in text.object
    assert text.disable_math


async def test_an_approval_pending_when_entering_full_screen_shows_the_notice(session):
    request(session.runner.state)
    assert not session.chrome.notice.visible
    session.app._editor.fullscreen = True
    assert session.chrome.notice.visible
    session.app._editor.fullscreen = False
    assert not session.chrome.notice.visible


async def test_a_question_asked_while_the_assistant_is_busy_is_sent_after(session):
    sent = []

    async def submit(text):
        sent.append(text)

    session.drawer.chat.submit = submit
    state = session.runner.state
    state.running = True
    session.chrome.ask("And the table?")
    for _ in range(5):
        await asyncio.sleep(0)
    assert sent == [] and session.drawer.open
    state.running = False
    for _ in range(5):
        await asyncio.sleep(0)
    assert sent == ["And the table?"]


async def test_steps_are_labelled_and_a_narrow_window_overlays_the_drawer(session):
    from panel_flowdash.agent.chrome import OVERLAY_BELOW, TOOL_LABELS

    assert session.drawer.overlay_below == OVERLAY_BELOW
    labels = session.drawer.chat.tool_labels
    assert labels is TOOL_LABELS
    assert labels["set_config"]({"name": "kpi", "config": {"label": "x"}}) == "Change kpi: label"
    assert (
        labels["connect"]({"source": "filter.filtered", "target": "map.turbines"})
        == "Connect filter to map"
    )
    assert labels["add_node"]({"component": "Views/kpi"}) == "Add kpi"


async def test_every_tool_and_palette_action_has_a_label(session):
    from panel_flowdash.agent.chrome import TOOL_LABELS
    from panel_flowdash.agent.tools import dashboard_toolset

    names = set(dashboard_toolset().tools) | {action.name for action in session.actions}
    assert names <= set(TOOL_LABELS)


async def test_narrowing_the_window_leaves_one_of_the_drawer_and_the_sidecar_open(session):
    editor = session.app._editor
    inspector, drawer = editor._inspector, session.drawer
    inspector.visible = True
    editor._split_view.storage.window_width = 1920
    drawer.open = True
    assert not inspector.collapsed
    # Opened last, beside the drawer, while both fit.
    inspector.collapse_button.clicks += 1
    inspector.expand_button.clicks += 1
    assert drawer.open and not inspector.collapsed
    editor._split_view.storage.window_width = 1280
    assert drawer.open and inspector.collapsed
    # Opening the sidecar again takes the slot from the drawer.
    inspector.expand_button.clicks += 1
    assert not drawer.open and not inspector.collapsed


async def test_below_1600_px_opening_one_folds_the_other(session):
    editor = session.app._editor
    inspector, drawer = editor._inspector, session.drawer
    inspector.visible = True
    editor._split_view.storage.window_width = 1280
    drawer.open = True
    assert inspector.collapsed
    inspector.expand_button.clicks += 1
    assert not drawer.open and not inspector.collapsed
    drawer.open = True
    assert inspector.collapsed
    drawer.open = False
    assert not inspector.collapsed
