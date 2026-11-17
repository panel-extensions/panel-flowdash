"""The agent of one FlowDashApp session: its runner, tools, palette actions and approvals.

Needs the ``panel-flowdash[agent]`` extra.
"""

from __future__ import annotations

import typing as t

from panel_agents import AgentDrawer, RunState
from panel_agents.adapters.pydantic_ai import ActionToolset, AgentRunner

from .actions import palette_actions
from .assistant import create_agent
from .live import LiveDashboardOps
from .tools import FlowDashDeps, approval_policy

if t.TYPE_CHECKING:
    from panel_agents import Artifact, ArtifactVersion
    from pydantic_ai import Agent

    from panel_flowdash.app import FlowDashApp


def resolve_agent(agent: t.Any, model: str | None) -> Agent | None:
    """Return the agent a FlowDashApp's ``agent`` and ``agent_model`` options ask for, or None.

    ``False`` turns the agent off; ``None`` gives the default agent only when a
    model is configured; ``True`` requires one; a string is the default
    agent's model; anything else is a ready-made pydantic-ai ``Agent``.
    """
    from .assistant import default_model

    if agent is False:
        return None
    if agent is None or agent is True:
        model = model or default_model()
        if model is None:
            if agent is True:
                raise ValueError(
                    "FlowDashApp(agent=True) needs a model: set agent_model or "
                    "FLOWDASH_AGENT_MODEL, e.g. to 'openai:gpt-5'."
                )
            return None
        return create_agent(model)
    if isinstance(agent, str):
        return create_agent(agent)
    return agent


class AgentSession:
    """Connects an agent to one user's FlowDashApp session.

    The agent works through :class:`~panel_flowdash.agent.live.LiveDashboardOps`
    with the signed-in user's rights, can run what the command palette offers,
    and needs the user's approval to save, delete or share. Its chat, the
    versions it publishes and the pending approvals are in a drawer docked to
    the right of the page, opened from a button in the header. Every edit
    publishes a version of the dashboard YAML, and applying an earlier or
    later version loads it into the editor as an import.
    """

    def __init__(self, app: FlowDashApp, agent: Agent):
        self.app = app
        self.ops = LiveDashboardOps(app)
        self.actions = palette_actions(app)
        self.runner = AgentRunner(
            agent,
            RunState(),
            deps=FlowDashDeps(self.ops),
            approval_policy=approval_policy(),
            run_kwargs={"toolsets": [ActionToolset(self.actions)]},
        )
        self.drawer = AgentDrawer(
            self.runner, title="Assistant", variant="persistent", on_apply=self.apply_version
        )

    def dock(self, content):
        """Return the page *content* with the agent's drawer docked to its right."""
        self.drawer.main = content
        return self.drawer

    def apply_version(self, artifact: Artifact, version: ArtifactVersion) -> bool:
        """Make the open dashboard match a published *version* of it, as an import."""
        current = self.app._current_dashboard
        if current is None or artifact.artifact_id != f"dashboard:{current.dashboard_id}":
            raise ValueError("That version belongs to a dashboard that is not open.")
        self.ops.replace_yaml(version.value)
        return True
