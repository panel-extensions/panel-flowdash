"""The command palette as front-end actions: the agent runs what the palette offers."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from panel_agents import RunState
from panel_agents.adapters.pydantic_ai import ActionToolset, AgentRunner
from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.actions import palette_actions

from .test_agent_tools import _app, scripted


@pytest.fixture
def app(monkeypatch):
    store = MemoryDashboardStore()
    store.create_dashboard("alice", "Mine")
    return _app(store, monkeypatch)


def test_commands_are_the_palettes_items(app):
    registry = palette_actions(app)
    listed = {c["id"] for c in registry["list_commands"].function()}
    assert "action:theme" in listed
    assert any(cid.startswith("dashboard:") for cid in listed)
    assert [c["id"] for c in registry["list_commands"].function("theme")] == ["action:theme"]


async def test_running_a_command_takes_the_palettes_path(app):
    registry = palette_actions(app)
    dark = app._page.dark_theme
    assert await registry.invoke("run_command", {"command": "action:theme"}) == (
        f"Ran 'Switch to {'light' if dark else 'dark'} theme'."
    )
    assert app._page.dark_theme is not dark


async def test_commands_the_palette_does_not_offer_are_refused(app):
    registry = palette_actions(app)
    with pytest.raises(ValueError, match="does not offer 'action:save'"):
        await registry.invoke("run_command", {"command": "action:save"})


async def test_an_agent_drives_the_palette(app):
    agent = Agent(
        scripted(
            ("list_commands", {"query": "theme"}), ("run_command", {"command": "action:theme"})
        ),
        toolsets=[ActionToolset(palette_actions(app))],
    )
    dark = app._page.dark_theme
    output = await AgentRunner(agent, RunState(flush_interval=0)).run("Toggle the theme")
    assert output.startswith("Done: Ran 'Switch to")
    assert app._page.dark_theme is not dark


def test_saving_through_the_palette_needs_approval_too():
    from panel_flowdash.agent.tools import approval_policy

    policy = approval_policy()
    assert policy.requires_approval("run_command", {"command": "action:save"})
    assert not policy.requires_approval("run_command", {"command": "action:refresh"})
