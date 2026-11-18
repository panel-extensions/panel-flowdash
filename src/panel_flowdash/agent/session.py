"""The agent of one FlowDashApp session: its runner, tools, palette actions and approvals.

Needs the ``panel-flowdash[agent]`` extra.
"""

from __future__ import annotations

import asyncio
import typing as t

import panel as pn
from panel_agents import AgentDrawer, RunState
from panel_agents.adapters.pydantic_ai import ActionToolset, AgentRunner

from panel_flowdash.util import confirm

from .actions import palette_actions
from .assistant import create_agent
from .chrome import SessionChrome
from .highlight import ChangeHighlighter
from .live import LiveDashboardOps
from .ops import read_revision
from .tools import ARTIFACT_KIND, FlowDashDeps, approval_policy

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
    from .assistant import NO_MODEL, default_model

    if agent is False:
        return None
    if agent is None or agent is True:
        model = model or default_model()
        if model is None:
            if agent is True:
                raise ValueError(NO_MODEL)
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
        deps = FlowDashDeps(self.ops)
        self.actions = palette_actions(app, deps=deps)
        self.runner = _SessionRunner(
            self,
            agent,
            RunState(),
            deps=deps,
            approval_policy=approval_policy(
                *app.agent_approval_rules,
                sensitive_components=app.agent_sensitive_components,
                ops=self.ops,
            ),
            run_kwargs={
                "toolsets": [ActionToolset(self.actions)] if app.agent_palette else [],
                **(
                    {"model_settings": app.agent_model_settings}
                    if app.agent_model_settings
                    else {}
                ),
            },
        )
        self.drawer = AgentDrawer(
            self.runner, title="Assistant", variant="persistent", on_apply=self.apply_version
        )
        self.highlighter = ChangeHighlighter(self)
        self.chrome = SessionChrome(self)

    def publish_user_edits(self):
        """Publish the open dashboard as a version if it changed since the agent last saw it.

        Runs before every request to the agent, so the versions include the
        user's own edits and the agent is told about them.
        """
        current = self.app._current_dashboard
        if current is None:
            return
        artifact_id = f"dashboard:{current.dashboard_id}"
        state = self.runner.state
        artifact = state.artifacts[artifact_id] if artifact_id in state.artifacts else None
        latest = artifact.applied_version if artifact is not None else None
        if latest is None or read_revision(latest.value) == self.ops.revision():
            return
        try:
            text = self.ops.get_dashboard()
        except PermissionError:
            return
        state.publish_artifact(
            artifact_id, text, kind=ARTIFACT_KIND, title=current.title, applied=True
        )

    def dock(self, content):
        """Return the page *content* with the agent's drawer docked to its right."""
        self.drawer.main = content
        return self.drawer

    async def apply_version(self, artifact: Artifact, version: ArtifactVersion) -> bool:
        """Make the open dashboard match a published *version* of it, as an import.

        When the canvas has changes the agent did not publish, typically the
        user's own edits, the user confirms that applying the version drops them.
        """
        current = self.app._current_dashboard
        if current is None or artifact.artifact_id != f"dashboard:{current.dashboard_id}":
            raise ValueError("That version belongs to a dashboard that is not open.")
        latest = artifact.applied_version
        based_on = read_revision(latest.value) if latest is not None else None
        if based_on != self.ops.revision() and not await self._confirm_overwrite(version):
            return False
        self.ops.replace_yaml(version.value, revision=self.ops.revision())
        return True

    async def _confirm_overwrite(self, version: ArtifactVersion) -> bool:
        if pn.state.curdoc is None or pn.state.curdoc.session_context is None:
            return False
        answer = asyncio.get_running_loop().create_future()

        def decide(value):
            def handler(_event):
                if not answer.done():
                    answer.set_result(value)

            return handler

        confirm(
            f"Apply version {version.version}?",
            "The dashboard has changes made after the assistant's last edit. "
            "Applying this version replaces them.",
            on_confirm=decide(True),
            on_cancel=decide(False),
            confirm_label="Apply and drop changes",
            cancel_label="Keep my changes",
        )
        return await answer


class _SessionRunner(AgentRunner):
    """Publishes the user's own edits before each request, so versions stay truthful.

    Each request is pinned to the dashboard open when the user sent it, and
    ends the turn of the user's own edits, so those made after it form a new one.
    """

    def __init__(self, session: AgentSession, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = session

    async def run(self, prompt=None, **kwargs):
        self._session.ops.editor.end_turn()
        self._session.publish_user_edits()
        self.deps.start_request(prompt if isinstance(prompt, str) else None)
        try:
            return await super().run(prompt, **kwargs)
        finally:
            self.deps.end_request()
