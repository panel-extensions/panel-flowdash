"""The agent drawer alongside the inspector, full screen and the command palette."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from panel_agents import events as ev
from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from .test_agent_tools import _app, scripted


@pytest.fixture
def session(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id)
    return app._agent_session


def request(state, call_id="t1"):
    state.apply_many(
        [
            ev.RunStarted(run_id="r"),
            ev.ApprovalRequested(
                tool_call_id=call_id,
                tool_name="save_dashboard",
                run_id="r",
                description="Save the open dashboard. The user approves each save.\nIt stores:\n- a",
            ),
        ]
    )


def test_the_inspector_folds_while_the_drawer_is_open(session):
    inspector = session.app._editor._inspector
    inspector.visible, inspector.collapsed = True, False
    session.drawer.open = True
    assert inspector.collapsed
    session.drawer.open = False
    assert not inspector.collapsed


def test_an_inspector_shown_while_the_drawer_is_open_is_folded(session):
    inspector = session.app._editor._inspector
    inspector.visible, inspector.collapsed = False, False
    session.drawer.open = True
    assert not inspector.collapsed
    inspector.visible = True
    assert inspector.collapsed
    session.drawer.open = False
    assert not inspector.collapsed


def test_an_inspector_the_user_reopened_stays_open(session):
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


def test_an_approval_in_full_screen_shows_a_notice_that_leads_to_the_drawer(session):
    editor = session.app._editor
    editor.fullscreen = True
    assert editor.fullscreen
    request(session.runner.state)
    chrome = session.chrome
    assert chrome.notice.visible
    assert (
        chrome._notice_text.object
        == "The assistant is waiting for your approval: Save the open dashboard"
    )
    chrome.review()
    assert not editor.fullscreen and not chrome.notice.visible
    assert session.drawer.open and session.drawer.tab == "chat"


def test_no_notice_outside_full_screen(session):
    request(session.runner.state)
    assert not session.chrome.notice.visible


def test_the_palette_offers_to_ask_the_assistant(session, monkeypatch):
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
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
    assert sent == ["Why is the map empty?"]
