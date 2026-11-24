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
from panel_agents import AgentDrawer, ModelChoice, RunState
from panel_agents.adapters.pydantic_ai import ActionToolset, AgentRunner
from panel_agents.events import RunStarted

from panel_flowdash.ai import ModelEntry, UsageLedger, model_choice, select_model
from panel_flowdash.explore import EXPLORE_RUN_PREFIX, is_explore_run
from panel_flowdash.ui.text import text_typography
from panel_flowdash.util import confirm

logger = logging.getLogger(__name__)

from panel_flowdash.history import components_of
from panel_flowdash.value_text import error_text

from .actions import palette_actions
from .assistant import create_agent, create_explore_agent
from .chrome import SessionChrome
from .exploration import ExplorationOps
from .highlight import ChangeHighlighter
from .live import LiveDashboardOps
from .models import agent_model_choice
from .ops import OperationError, read_revision
from .starters import StarterQuestions
from .tools import ARTIFACT_KIND, AgentEditLock, FlowDashDeps, approval_policy

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
        explore_models: list | None = None,
        entries: t.Sequence[ModelEntry] = (),
        ledger: UsageLedger | None = None,
    ):
        self.app = app
        # The models' limits, fallbacks and settings, by id, and where their spend is counted.
        self.entries = {entry.id: entry for entry in entries}
        self.ledger = ledger
        self._choices = {
            choice.id: choice
            for choice in [*(models or ()), *(explore_models or ())]
            if isinstance(choice, ModelChoice)
        }
        self.ops = LiveDashboardOps(app)
        # With the Explore view's agent too, one request at a time edits the dashboard.
        self.edit_lock = AgentEditLock() if explore_agent is not None else None
        deps = FlowDashDeps(self.ops, edit_lock=self.edit_lock, on_edit_wait=self._on_drawer_wait)
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
        # The Explore view asks its own agent, whose runs the drawer does not show. It builds its
        # answers in an exploration beside the dashboard, so they stay off the canvas and out of
        # the edit history until the person adds one, which takes the edit lock like a request.
        self.explore_runner = None
        self.explorations = None
        if explore_agent is not None:
            self.explorations = ExplorationOps(self.ops)
            self.explore_runner = _ExploreRunner(
                self,
                explore_agent,
                RunState(),
                deps=FlowDashDeps(self.explorations),
                approval_policy=self._approval_policy(),
                run_kwargs=dict(settings),
            )
            self.explore_runner.state.subscribe(self._note_run)
        # Background checks of restored versions, kept so they are not collected while they run.
        self._checks: set[asyncio.Task] = set()
        # A single model is named in the chat, with panel-agents versions that show one; there is
        # no pick to remember, and a remembered one is kept for when there is a choice again.
        chat_params = {"models": list(models), "model_id": model} if models else {}
        if len(models or ()) > 1:
            chat_params["remember_model"] = MODEL_KEY
        preferences = editor._split_view.preferences
        self.drawer = AgentDrawer(
            self.runner,
            title="Assistant",
            variant="persistent",
            width=preferences.assistant_width,
            on_apply=self.apply_version,
            # Someone else's request is redacted for people who may not see what it touched.
            redact_runs=editor.run_redacted,
            chat_params=chat_params,
        )
        if self.explore_runner is not None:
            if explore_models is not None:
                # Explore is offered other models than the assistant, so it keeps its own pick.
                explore_params = {
                    "models": list(explore_models),
                    "model_id": explore_models[0].id if explore_models else None,
                }
                if len(explore_models) > 1:
                    explore_params["remember_model"] = f"{MODEL_KEY}-explore"
                editor.explore_feed.attach(
                    self.explore_runner,
                    chat_params=explore_params,
                    foreign=self._drawer_run,
                    explorations=self.explorations,
                    add=self.add_answer,
                    show=self.show_answer,
                )
            elif len(models or ()) > 1:
                # The Explore view's composer offers the same models and shares the drawer's pick.
                editor.explore_feed.attach(
                    self.explore_runner,
                    chat_params=chat_params,
                    model_chat=self.drawer.chat,
                    foreign=self._drawer_run,
                    explorations=self.explorations,
                    add=self.add_answer,
                    show=self.show_answer,
                )
            else:
                # With nothing to pick, the composer names the Explore agent's own model.
                only = explore_model or agent_model_choice(explore_agent)
                editor.explore_feed.attach(
                    self.explore_runner,
                    chat_params={"models": [only], "model_id": only.id} if only else {},
                    foreign=self._drawer_run,
                    explorations=self.explorations,
                    add=self.add_answer,
                    show=self.show_answer,
                )
        # The drawer opens at the width last dragged to in this browser.
        preferences.param.watch(lambda e: setattr(self.drawer, "width", e.new), "assistant_width")
        self.drawer.param.watch(lambda e: setattr(preferences, "assistant_width", e.new), "width")
        # Whether Ask was set aside for the edit an add from its docked feed started.
        self._ask_resumes = False
        editor.param.watch(self._on_explore, ["explore", "ask", "editable"])
        # In edit mode Ask is this drawer, opened and closed with the bar's Ask.
        editor.assistant = True
        self.drawer.param.watch(self._on_drawer_toggled, "open")
        self._on_explore()
        self.highlighter = ChangeHighlighter(self)
        self.chrome = SessionChrome(self)
        editor.run_pending = self.run_pending
        self.history_notice = pmui.Alert(
            text_typography("", variant="body2", margin=0),
            severity="warning",
            variant="outlined",
            visible=False,
            margin=(8, 12, 0, 12),
        )
        # Says that the assistant's request waits for the Explore view's to finish its edits.
        self.wait_notice = pmui.Alert(
            text_typography("", variant="body2", margin=0),
            severity="info",
            variant="outlined",
            visible=False,
            margin=(8, 12, 0, 12),
            css_classes=["flowdash-agent-waiting"],
        )
        self._place_notice(self.wait_notice)
        # Offered on a new dashboard until its first request, see FlowDashApp.agent_starters.
        self.starters = StarterQuestions(self)
        self._place_notice(self.starters.view)
        if editor.history:
            editor.edit_history.on_load = self._on_history_loaded
            self.drawer.param.watch(self._on_drawer_open, "open")
            self._place_notice(self.history_notice)

    @property
    def runners(self) -> list[_SessionRunner]:
        """The session's runners: the drawer's, then the Explore view's if it has an agent."""
        return [self.runner] + ([self.explore_runner] if self.explore_runner is not None else [])

    def _approval_policy(self):
        from panel_flowdash.permissions import approval_rules

        app = self.app
        permissions = getattr(app, "_permissions", None) or {}
        sensitive = list(app.agent_sensitive_components)
        sensitive += [
            p for p in permissions.get("sensitive_components") or () if p not in sensitive
        ]
        sensitive += [cid for cid in app._approval_components() if cid not in sensitive]
        return approval_policy(
            *app.agent_approval_rules,
            *approval_rules(permissions),
            sensitive_components=sensitive,
            ops=self.ops,
        )

    def model_id(self, target: t.Any) -> str | None:
        """Return the id of the offered model a runner's ``model`` is, or None."""
        if target is None:
            return None
        for choice in self._choices.values():
            if choice.target is target or choice.target == target or choice.id == target:
                return choice.id
        return target if isinstance(target, str) else None

    def plan_request(self, target: t.Any) -> tuple[str | None, dict[str, t.Any], str | None]:
        """Return the model id a request picked as *target* runs with, its run arguments and a note.

        A model over its monthly limit gives way to its fallback, noted for
        the person; the id is None when the request is refused.
        """
        picked = self.model_id(target)
        if picked is None or not self.entries:
            return picked, {}, None
        spent = self.ledger.spent if self.ledger is not None else (lambda _model: 0.0)
        chosen, note = select_model(self.entries, picked, spent)
        if chosen is None:
            return None, {}, note
        kwargs: dict[str, t.Any] = {}
        if chosen != picked:
            choice = self._choices.get(chosen)
            entry = self.entries.get(chosen)
            if choice is None and entry is not None:
                choice = model_choice(entry, self.app._project_section("ai"))
            kwargs["model"] = choice.target if choice is not None else chosen
        entry = self.entries.get(chosen)
        if entry is not None and entry.settings:
            kwargs["model_settings"] = {**(self.app.agent_model_settings or {}), **entry.settings}
        return chosen, kwargs, note

    def record_spend(self, model_id: str | None, runner: AgentRunner, run_id: str):
        """Count the cost of run *run_id* against *model_id*'s monthly spend."""
        if model_id is None or self.ledger is None:
            return
        run = runner.state.runs.get(run_id)
        if run is None:
            return
        self.ledger.add(model_id, run.cost_usd)

    def _on_explore(self, *events):
        """Open the drawer as Ask in edit mode, close it while the feed is Ask, and bring back a pending approval.

        An add from Ask's docked feed that starts edit mode sets Ask aside
        rather than opening the drawer, whose conversation is another one,
        and Ask comes back as edit mode ends, unless the person turned it on
        or off meanwhile.
        """
        editor = self.app._editor
        names = {event.name for event in events}
        if "editable" in names and not editor.editable and self._ask_resumes:
            self._ask_resumes = False
            editor.ask = True
        exploring = editor.explore or (editor.ask and not editor.editable)
        self.drawer.open_on_approval = not exploring
        pending = self.runner.state.pending_approvals
        if exploring:
            self.drawer.open = False
        elif editor.editable:
            if "ask" in names:
                self._ask_resumes = False
                self.drawer.open = editor.ask
            elif self.drawer.open:
                # Open as edit mode starts, e.g. as the assistant begins to edit: it is Ask now.
                editor.ask = True
            elif editor.ask and self._adding_from_feed() and not pending:
                editor.ask = False
                # Only now: turning Ask off runs this again, as a change that clears it.
                self._ask_resumes = True
            elif editor.ask or pending:
                self.drawer.open = True
        elif pending:
            self.drawer.open = True

    def _adding_from_feed(self) -> bool:
        feed = self.app._editor._explore_feed
        return feed is not None and feed.adding

    def _on_drawer_toggled(self, event):
        """Keep Ask in step with the drawer: it is Ask in edit mode, and takes the docked feed's place in view mode.

        In view mode the drawer and the docked feed would squeeze the output
        between them, so opening the drawer, e.g. to review an approval,
        turns Ask off, as turning Ask on closes the drawer.
        """
        editor = self.app._editor
        if editor.editable:
            if not editor.explore and editor.ask != event.new:
                editor.ask = event.new
        elif event.new and editor.ask:
            editor.ask = False

    async def add_answer(self, run: str, node: str) -> str:
        """Add the answer *node* of Explore run *run* to the dashboard and return its name there.

        It waits for a request of the assistant that is editing the
        dashboard, and is one turn of *run* and one version of the history.
        """
        name = await self.explorations.add_to_dashboard(
            run,
            node,
            question=self.explore_runner.questions.get(run),
            edit_lock=self.edit_lock,
            on_wait=self.app._editor.explore_feed.show_waiting,
        )
        self.publish_agent_layout(run)
        return name

    def run_pending(self, turn_id: str):
        """Ask the assistant the question of the pending turn *turn_id*, as the turn's request.

        The request is an ordinary one, shown in the drawer's chat, and its
        edits answer the turn, so the question's block in the report shows
        the nodes it builds. Only editors may run a question; one asked while
        the assistant is busy is sent once it is done. A question already
        asked and not yet answered is not asked again.
        """
        editor = self.app._editor
        if editor.read_only:
            raise PermissionError("Only editors can run the report's questions.")
        if turn_id in editor.running_pending:
            return
        turn = next((t for t in editor.turns if t.id == turn_id), None)
        if turn is None or turn.status != "pending" or not turn.question:
            raise ValueError(f"{turn_id!r} is not a question waiting to be run.")
        editor.running_pending = [*editor.running_pending, turn_id]
        self.chrome.ask(turn.question, turn=turn_id)

    def pending_done(self, turn_id: str):
        """Note that the request asking the pending turn *turn_id*'s question ended, or never started."""
        editor = self.app._editor
        if turn_id in editor.running_pending:
            editor.running_pending = [t for t in editor.running_pending if t != turn_id]

    def show_answer(self, name: str):
        """Show the tiles, leaving the Explore view or the report, and mark the dashboard node *name*, e.g. an answer just added."""
        editor = self.app._editor
        if editor.editable and editor.mode == "wiring":
            editor.mode = "dashboard"
        editor.surface = "workspace"
        self.highlighter.show({name: "add"})

    def _note_run(self, event):
        if isinstance(event, RunStarted) and event.parent_run_id is None:
            self.app._editor.note_own_run(event.run_id)

    def _drawer_run(self, run_id: str) -> bool:
        """Whether *run_id* is a run of the drawer's assistant, in this session or the stored history."""
        return run_id in self.runner.state.runs

    def _on_drawer_wait(self, waiting: str | None):
        if waiting is not None:
            self.wait_notice.objects[0].object = f"Waiting for {waiting} to finish…"
        self.wait_notice.visible = waiting is not None

    def _place_notice(self, notice):
        """Put a notice above the drawer's body, or leave it out if the drawer has changed.

        AgentDrawer has no slot for a host's notices yet, so this reaches into its layout.
        """
        content = getattr(self.drawer, "_content", None)
        if not isinstance(content, pn.layout.ListLike):
            return
        body = getattr(self.drawer, "_body", None)
        index = next((i for i, obj in enumerate(content.objects) if obj is body), None)
        if index is None:
            content.append(notice)
        else:
            content.insert(index, notice)

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
            self.app._notify(
                "error", f"v{version.version} was not restored: {error_text(exc)}", duration=8000
            )
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
        #: The id of the pending turn the next request answers, if any, see ``FlowDash.answer_pending``.
        self.answering: str | None = None

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
        app = self._session.app
        if not app._assistant_still_allowed():
            app._notify("error", "You no longer have access to the assistant.", duration=8000)
            return None
        # Approval rules and sensitive components changed in the admin view apply from this request.
        self.approval_policy = self._session._approval_policy()
        model_id, model_kwargs, note = self._session.plan_request(kwargs.get("model", self.model))
        if note:
            self._session.app._notify("warning" if model_id else "error", note, duration=8000)
        if model_id is None and note:
            return None
        # The limit's fallback wins over a model the caller passed.
        kwargs = {**kwargs, **model_kwargs}
        self._session.ops.editor.end_turn()
        self._session.publish_user_edits()
        self.deps.start_request(prompt if isinstance(prompt, str) else None, run_id)
        answering, self.answering = self.answering, None
        if answering is not None:
            try:
                self._session.ops.editor.answer_pending(answering, run_id)
            except ValueError:
                # Taken back or answered meanwhile: the request runs as an ordinary one.
                pass
        try:
            return await super().run(prompt, **kwargs)
        finally:
            self.deps.end_request()
            self._session.publish_agent_layout(run_id)
            self._session.record_spend(model_id, self, run_id)


class _ExploreRunner(_SessionRunner):
    """Runs the Explore view's agent, with run ids that mark its turns as the Explore view's.

    Each run works on the open dashboard's exploration (``deps.ops`` is an
    ``ExplorationOps``), brought up to date with the dashboard first.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: What was asked in each run, for the turn adding one of its answers to the dashboard.
        self.questions: dict[str, str] = {}

    async def run(self, prompt=None, **kwargs):
        run_id = kwargs["run_id"] = self._run_id(kwargs.get("run_id"))
        if isinstance(prompt, str):
            self.questions[run_id] = prompt
        exploration = self.deps.ops.exploration()
        exploration.sync()
        try:
            return await super().run(prompt, **kwargs)
        finally:
            self.deps.ops.record_answers(exploration, run_id, self.questions.get(run_id))
            exploration.note_run(run_id, keep=self.deps.ops.MAX_RUNS)

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
