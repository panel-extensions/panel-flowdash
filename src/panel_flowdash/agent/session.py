"""The agent of one FlowDashApp session: its runner, tools, palette actions and approvals.

Needs the ``panel-flowdash[agent]`` extra.
"""

from __future__ import annotations

import asyncio
import logging
import re
import typing as t
import uuid

import panel as pn
import panel_material_ui as pmui
from panel_agents import AgentDrawer, RunState
from panel_agents.adapters.pydantic_ai import ActionToolset, AgentRunner
from panel_agents.events import RunStarted

from panel_flowdash.explore import EXPLORE_RUN_PREFIX, is_explore_run
from panel_flowdash.ui.text import text_typography
from panel_flowdash.util import confirm

logger = logging.getLogger(__name__)

from panel_flowdash.history import components_of

from .actions import palette_actions
from .assistant import create_agent, create_explore_agent
from .chrome import SessionChrome
from .highlight import ChangeHighlighter
from .live import LiveDashboardOps
from .models import agent_model_choice
from .ops import OperationError, read_revision
from .tools import ARTIFACT_KIND, FlowDashDeps, approval_policy

if t.TYPE_CHECKING:
    from panel_agents import Artifact, ArtifactVersion
    from pydantic_ai import Agent

    from panel_flowdash.app import FlowDashApp


def resolve_agent(agent: t.Any, model: str | None, **kwargs: t.Any) -> Agent | None:
    """Return the agent a FlowDashApp's ``agent`` and ``agent_model`` options ask for, or None.

    ``False`` turns the agent off; ``None`` gives the default agent only when a
    model is configured; ``True`` requires one; a string is the default
    agent's model; anything else is a ready-made pydantic-ai ``Agent``.
    *kwargs* are passed on to `create_agent`.
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
        return create_agent(model, **kwargs)
    if isinstance(agent, str):
        return create_agent(agent, **kwargs)
    return agent


def resolve_explore_agent(
    explore_agent: t.Any, agent: Agent, model: t.Any = None, **kwargs: t.Any
) -> Agent | None:
    """Return the agent a FlowDashApp's ``explore_agent`` option asks for, or None.

    ``False`` turns asking in the Explore view off; ``None`` or ``True``
    gives the default Explore agent on *model*, or on the assistant *agent*'s
    own model; a string is the default Explore agent's model; anything else
    is a ready-made pydantic-ai ``Agent``. *kwargs* are passed on to
    `create_explore_agent`.
    """
    if explore_agent is False:
        return None
    if explore_agent is None or explore_agent is True:
        model = model if model is not None else getattr(agent, "model", None)
        if model is None:
            return None
        return create_explore_agent(model, **kwargs)
    if isinstance(explore_agent, str):
        return create_explore_agent(explore_agent, **kwargs)
    return explore_agent


class AgentSession:
    """Connects an agent to one user's FlowDashApp session.

    The agent works through :class:`~panel_flowdash.agent.live.LiveDashboardOps`
    with the signed-in user's rights, can run what the command palette offers,
    and needs the user's approval to save, delete or share. Its chat, the
    versions it publishes and the pending approvals are in a drawer docked to
    the right of the page, opened from a button in the header.

    With the editor's edit history on (``FlowDash.history``), the runner
    shares the history's ``RunState``, so the drawer's artifact view lists
    every version of the dashboard, the person's edits and the agent's,
    grouped by request, and its Undo, Redo and Revert buttons restore them
    as the editor's undo does. With it off, each edit tool publishes a
    version of the dashboard YAML, and applying an earlier or later version
    loads it into the editor as an import.
    """

    def __init__(
        self,
        app: FlowDashApp,
        agent: Agent,
        models: list | None = None,
        model: str | None = None,
        explore_agent: Agent | None = None,
        explore_model: t.Any = None,
    ):
        self.app = app
        self.ops = LiveDashboardOps(app)
        deps = FlowDashDeps(self.ops)
        self.actions = palette_actions(app, deps=deps)
        editor = app._editor
        settings = {"model_settings": app.agent_model_settings} if app.agent_model_settings else {}
        self.runner = _SessionRunner(
            self,
            agent,
            editor.edit_history.state if editor.history else RunState(),
            deps=deps,
            approval_policy=self._approval_policy(),
            run_kwargs={
                "toolsets": [ActionToolset(self.actions)] if app.agent_palette else [],
                **settings,
            },
        )
        editor.agent_state = self.runner.state
        # Runs applied live are this person's requests; a stored history is restored without notifying.
        self.runner.state.subscribe(self._note_run)
        # The Explore view asks its own agent, whose runs the drawer does not show. Its edits still
        # go through the edit history, which waits for its runs and versions each one when it ends.
        self.explore_runner = None
        if explore_agent is not None:
            self.explore_runner = _ExploreRunner(
                self,
                explore_agent,
                RunState(),
                deps=FlowDashDeps(self.ops),
                approval_policy=self._approval_policy(),
                run_kwargs=dict(settings),
            )
            self.explore_runner.state.subscribe(self._note_run)
            editor.edit_history.follow(self.explore_runner.state)
        # Background checks of restored versions, kept so they are not collected while they run.
        self._checks: set[asyncio.Task] = set()
        # A single model is named in the chat, with panel-agents versions that show one; there is
        # no pick to remember, and a remembered one is kept for when there is a choice again.
        chat_params = {"models": list(models), "model_id": model} if models else {}
        if len(models or ()) > 1:
            chat_params["remember_model"] = MODEL_KEY
        self.drawer = AgentDrawer(
            self.runner,
            title="Assistant",
            variant="persistent",
            on_apply=self.apply_version,
            # Someone else's request is redacted for people who may not see what it touched.
            redact_runs=editor.run_redacted,
            chat_params=chat_params,
        )
        if self.explore_runner is not None:
            if len(models or ()) > 1:
                # The Explore view's composer offers the same models and shares the drawer's pick.
                editor.explore_feed.attach(
                    self.explore_runner, chat_params=chat_params, model_chat=self.drawer.chat
                )
            else:
                # With nothing to pick, the composer names the Explore agent's own model.
                only = explore_model or agent_model_choice(explore_agent)
                editor.explore_feed.attach(
                    self.explore_runner,
                    chat_params={"models": [only], "model_id": only.id} if only else {},
                )
        editor.param.watch(self._on_explore, "explore")
        self._on_explore()
        self.highlighter = ChangeHighlighter(self)
        self.chrome = SessionChrome(self)
        self.history_notice = pmui.Alert(
            text_typography("", variant="body2", margin=0),
            severity="warning",
            variant="outlined",
            visible=False,
            margin=(8, 12, 0, 12),
        )
        if editor.history:
            editor.edit_history.on_load = self._on_history_loaded
            self.drawer.param.watch(self._on_drawer_open, "open")
            self._place_history_notice()

    @property
    def runners(self) -> list[_SessionRunner]:
        """The session's runners: the drawer's, then the Explore view's if it has an agent."""
        return [self.runner] + ([self.explore_runner] if self.explore_runner is not None else [])

    def _approval_policy(self):
        app = self.app
        return approval_policy(
            *app.agent_approval_rules,
            sensitive_components=app.agent_sensitive_components,
            ops=self.ops,
        )

    def _on_explore(self, *_events):
        """Close the drawer while Explore shows the chat, and bring back an approval left pending there."""
        exploring = self.app._editor.explore
        self.drawer.open_on_approval = not exploring
        if exploring:
            self.drawer.open = False
        elif self.runner.state.pending_approvals:
            self.drawer.open = True

    def _note_run(self, event):
        if isinstance(event, RunStarted) and event.parent_run_id is None:
            self.app._editor.note_own_run(event.run_id)

    def _place_history_notice(self):
        """Put the history notice above the drawer's body, or leave it out if the drawer has changed.

        AgentDrawer has no slot for a host's notices yet, so this reaches into its layout.
        """
        content = getattr(self.drawer, "_content", None)
        if not isinstance(content, pn.layout.ListLike):
            return
        body = getattr(self.drawer, "_body", None)
        index = next((i for i, obj in enumerate(content.objects) if obj is body), None)
        if index is None:
            content.append(self.history_notice)
        else:
            content.insert(index, self.history_notice)

    def _on_drawer_open(self, event=None):
        """Load the dashboard's stored history while the drawer is open, so it lists its versions."""
        history = self.app._editor.edit_history
        history.shown = self.drawer.open
        if history.shown:
            history.load()

    def _on_history_loaded(self):
        refused = self.app._editor.edit_history.refused
        if refused:
            self.history_notice.objects[0].object = refused
        self.history_notice.visible = bool(refused)

    def publish_user_edits(self):
        """Publish the open dashboard as a version if it changed since the agent last saw it.

        Runs before every request to the agent, so the versions include the
        user's own edits and the agent is told about them. With the edit
        history on, the edit in progress is published instead, since every
        other edit already is.
        """
        history = self.app._editor.edit_history
        if history.active:
            history.flush()
            return
        self._publish_changes()

    def publish_agent_layout(self, run_id: str | None):
        """Publish the tiles laid out when the agent's request *run_id* ended as its version.

        The editor lays out the tiles a request adds once it ends (see
        ``FlowDashDeps.start_request``), after its last tool published the
        dashboard. With the edit history on, that layout is part of the
        request's own version instead.
        """
        editor = self.ops.editor
        released, editor._layout_released = editor._layout_released, False
        if released and not editor.edit_history.active:
            self._publish_changes(run_id=run_id, actor="agent")

    def _publish_changes(self, **who):
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
            artifact_id, text, kind=ARTIFACT_KIND, title=current.title, applied=True, **who
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
        history = self.app._editor.edit_history
        if history.active:
            return await self._restore(history, artifact, version)
        current = self.app._current_dashboard
        if current is None or artifact.artifact_id != f"dashboard:{current.dashboard_id}":
            raise ValueError("That version belongs to a dashboard that is not open.")
        latest = artifact.applied_version
        based_on = read_revision(latest.value) if latest is not None else None
        if based_on != self.ops.revision() and not await self._confirm_overwrite(version):
            return False
        revision = self.ops.revision()
        try:
            issues = await self.ops.replace_yaml_async(version.value, revision=revision)
        except OperationError as exc:
            if self.ops.revision() != revision or self.app._current_dashboard is not current:
                raise ValueError(
                    "The dashboard changed while this version was being restored, so it was "
                    "not restored. Try again."
                ) from exc
            raise
        failed = [i for i in issues if i.severity == "error" and i.path.startswith("nodes.")]
        if failed:
            problems: dict[str, list[str]] = {}
            for issue in failed:
                problems.setdefault(issue.path.split(".")[1], []).append(issue.message)
            self._notify_failed_checks(version, problems)
        return True

    async def _restore(self, history, artifact: Artifact, version: ArtifactVersion) -> bool:
        """Restore *version* through the edit history and report its config checks afterwards.

        The version's components are imported off the event loop and the
        restore changes only what differs, so the drawer's button stops
        spinning as soon as the canvas matches. The config checks then run
        in the background, bounded by ``RESTORE_CHECK_TIMEOUT``, and tell
        the user which nodes fail. A refused restore is reported too.
        """
        editor = self.app._editor
        try:
            await editor.ensure_components_loaded_async(components_of(version.value))
            history.restore(artifact, version)
        except Exception as exc:
            self.app._notify("error", f"v{version.version} was not restored: {exc}", duration=8000)
            raise
        task = asyncio.ensure_future(self._report_checks(version))
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)
        return True

    async def _report_checks(self, version: ArtifactVersion):
        try:
            problems = await asyncio.wait_for(
                self.ops._canvas_problems_async(self.app._editor), RESTORE_CHECK_TIMEOUT
            )
        except TimeoutError:
            self.app._notify(
                "warning",
                f"Restored v{version.version}; its config checks did not finish in time.",
                duration=8000,
            )
            return
        except Exception:
            logger.exception("The config checks of a restored version failed")
            return
        if problems:
            names = self.ops._names()
            self._notify_failed_checks(
                version, {names.get(iid, iid): found for iid, found in problems.items()}
            )

    def _notify_failed_checks(self, version: ArtifactVersion, problems: dict[str, list[str]]):
        n = len(problems)
        self.app._notify(
            "warning",
            f"Restored v{version.version}, but {n} "
            f"{'node fails its' if n == 1 else 'nodes fail their'} config checks: "
            + "; ".join(
                f"{name}: {message}" for name, found in problems.items() for message in found
            ),
            duration=10000,
        )

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

    def _note_artifact_change(self, event):
        # A request's own version is published as its run ends; the model made those edits.
        if getattr(event, "actor", None) == "agent" and event.run_id == self.run_id:
            return
        # Merged as they come, so the notes stay short however long the person edits.
        super()._note_artifact_change(event)
        self._notes[:] = _collapse_notes(self._notes)

    def _run_id(self, given: str | None) -> str:
        return given or f"run-{uuid.uuid4().hex[:16]}"

    async def run(self, prompt=None, **kwargs):
        # The id is known before the run starts, so its turn, version and tile layout are
        # kept apart from those of the other surface's runner if both run at once.
        run_id = kwargs["run_id"] = self._run_id(kwargs.get("run_id"))
        self._session.ops.editor.end_turn()
        self._session.publish_user_edits()
        self.deps.start_request(prompt if isinstance(prompt, str) else None, run_id)
        try:
            return await super().run(prompt, **kwargs)
        finally:
            self.deps.end_request()
            self._session.publish_agent_layout(run_id)


class _ExploreRunner(_SessionRunner):
    """Runs the Explore view's agent, with run ids that mark its turns as the Explore view's."""

    def _run_id(self, given: str | None) -> str:
        if given:
            # A run id given by the caller is marked too, so its turn is still known as Explore's.
            return given if is_explore_run(given) else f"{EXPLORE_RUN_PREFIX}{given}"
        return f"{EXPLORE_RUN_PREFIX}{uuid.uuid4().hex[:16]}"


_PUBLISHED_NOTE = re.compile(
    r"The app published versions? (\d+)(?: to (\d+))? of the artifact (.+?)"
    r"( and now holds (?:it|the latest))?\."
)


def _collapse_notes(notes: list[str]) -> list[str]:
    """Merge the runner's notes about consecutive versions of one artifact into one.

    With the edit history every edit the person makes is a version, so the
    model would otherwise read a line for each drag before the next request.
    """
    collapsed: list[str] = []
    previous: tuple[str, str] | None = None
    for note in notes:
        match = _PUBLISHED_NOTE.fullmatch(note)
        if match is None:
            collapsed.append(note)
            previous = None
            continue
        first, last, name, held = match[1], match[2] or match[1], match[3], match[4]
        if previous is not None and previous[1] == name:
            first = previous[0]
            collapsed.pop()
        if first == last:
            collapsed.append(note)
        else:
            held = " and now holds the latest" if held else ""
            collapsed.append(
                f"The app published versions {first} to {last} of the artifact {name}{held}."
            )
        previous = (first, name)
    return collapsed


#: The localStorage key under which the browser remembers the model a person picked.
MODEL_KEY = "flowdash-agent-model"

#: Seconds the drawer waits for the config checks of a restored version before it gives up on them.
RESTORE_CHECK_TIMEOUT = 15
