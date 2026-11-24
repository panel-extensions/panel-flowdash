"""The Explore view: questions about a dashboard's data as a feed, with each answer's outputs shown live.

With an agent attached, the Explore view asks its own agent runner, apart
from the assistant in the drawer, and the feed holds the requests asked in
the Explore view only: the runs of this session and, from the dashboard
file, the turns of earlier ones (their run ids start with
``EXPLORE_RUN_PREFIX``). Without one, every turn of the dashboard is shown,
as a record of how it was made.

Each turn becomes a run of the feed's own panel-agents ``RunState``, shown
by an ``AgentChat`` in its document layout. A run asked in this session is
the runner's run itself, forwarded event by event, so the feed shows its
steps, answer and approvals as they stream. Any other turn is projected from
the dashboard file: the question, what the turn changed, and no model calls.

Each node a turn created is an inline artifact of that run whose value refers
to the node, never its data, so the card always shows the node's current
output and never disagrees with its tile: a view's card shows the view its
tile shows, which can only be on screen once, so the Explore view and the
editor's workspace are never shown together. Nodes made before the first
recorded turn are listed, collapsed, in a leading cell.

The answers of the runs asked in this session are not on the dashboard: they
are nodes of the session's exploration of it (see
:mod:`panel_flowdash.agent.exploration`), and each card shows one live from
there, with "Add to dashboard" copying it onto the dashboard.
"""

from __future__ import annotations

import logging
import typing as t
from datetime import datetime

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewable, Viewer
from panel_agents import ActionRegistry, AgentChat, ArtifactAction, RunState, events as ev
from panel_agents.components.approval import ApprovalCard

from panel_flowdash.port_types import port_types_compatible
from panel_flowdash.ui import tile_layout
from panel_flowdash.ui.text import escape_markdown, text_typography

if t.TYPE_CHECKING:
    from panel_agents import Artifact

    from panel_flowdash.editor import FlowDash
    from panel_flowdash.turns import DashboardTurn

logger = logging.getLogger(__name__)

#: The artifact kinds of a node's card, by the node's role.
NODE_KINDS = {
    "source": "data",
    "transform": "data",
    "view": "view",
    "widget": "control",
    "other": "output",
}

#: The artifact kind of the leading cell listing nodes made before the first recorded turn.
EARLIER_KIND = "outputs"

EARLIER_TITLE = "Before the recorded requests"

#: The title of the leading cell that holds, collapsed, the turns made outside Explore.
HISTORY_TITLE = "Earlier requests and edits"

# Who the leading entry is from, with a dashboard for an avatar.
EARLIER_AGENT = "Dashboard"

#: The title of a card whose node uses a component the person may not use.
RESTRICTED_TITLE = "Restricted output"

# The width of the shell's navigation rail, and the window width below which the
# shell hides it (``panel_flowdash.ui.shell.NARROW_BREAKPOINT``, which imports the app).
RAIL_WIDTH = 80
NARROW_BREAKPOINT = 600

#: How many entries around the visible ones the chat feed renders; more than the pages shown hold.
FEED_LOAD_BUFFER = 100_000

#: The height of a card showing a component's view, when its size hints give none.
CARD_HEIGHT = 320

_EARLIER_RUN = "dashboard-earlier"

#: How the run ids of the Explore view's own requests start, so their turns are known after a reload.
EXPLORE_RUN_PREFIX = "explore-"


def is_explore_run(run_id: str | None) -> bool:
    """Whether *run_id* is a request asked in the Explore view."""
    return bool(run_id) and run_id.startswith(EXPLORE_RUN_PREFIX)


_ARTIFACT_EVENTS = (ev.ArtifactPublished, ev.ArtifactApplied, ev.ArtifactVersionsDropped)

# The chat feed's frame in Ask's dock, which draws its own edge.
_DOCKED_FRAME = {"sx": {"boxShadow": "none", "borderRadius": 0}, "margin": 0}

# How long forwarded events are gathered before the feed catches up, in milliseconds.
_COALESCE_MS = 50


def _narrow_css() -> str:
    """Let the feed take the window's width on a narrow window.

    The page's containers keep the minimum width of the editor's canvas and
    its panels, so the feed is limited to the window, less the navigation
    rail, which the shell hides below its narrow breakpoint.
    """
    return f"""
@media (max-width: {NARROW_BREAKPOINT - 0.05}px) {{ :host {{ max-width: 100vw; }} }}
@media (min-width: {NARROW_BREAKPOINT}px) and (max-width: 799.95px) {{
  :host {{ max-width: calc(100vw - {RAIL_WIDTH}px); }}
}}
"""


def _turn_time(turn: DashboardTurn) -> float:
    try:
        return datetime.fromisoformat((turn.at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _change_list(summary: str) -> str:
    """Return a turn's summary as a Markdown list, one change per item, read literally."""
    labels = [label.strip() for label in summary.rstrip(".").split(". ") if label.strip()]
    return "\n".join(f"- {escape_markdown(label)}" for label in labels)


def _message(run_id: str, key: str, role: str, text: str, at: float) -> list[ev.Event]:
    message_id = f"{run_id}-{key}"
    return [
        ev.TextMessageStart(message_id=message_id, role=role, run_id=run_id, timestamp=at),
        ev.TextMessageContent(message_id=message_id, delta=text, run_id=run_id, timestamp=at),
        ev.TextMessageEnd(message_id=message_id, run_id=run_id, timestamp=at),
    ]


class ExploreFeed(Viewer):
    """The Explore view of a :class:`~panel_flowdash.editor.FlowDash` editor.

    Without an agent it shows every turn, projected. :meth:`attach` connects
    the Explore view's runner: the feed then shows the requests asked in the
    Explore view, its runs in full as they happen, and gets a composer for
    people who may edit the dashboard.
    """

    editor = param.Parameter(doc="The FlowDash editor whose dashboard the feed shows.")

    shown = param.Boolean(
        default=False,
        doc="""
        Whether the feed is on screen. While hidden it only records the
        agent's events, and catches up when shown.""",
    )

    docked = param.Boolean(
        default=False,
        doc="""
        Whether the feed is docked in Ask beside the dashboard or the report,
        which show the dashboard's views. A view is on screen only once, so
        the card of a dashboard node then says where its view is instead.""",
    )

    page_size = param.Integer(
        default=20,
        bounds=(1, None),
        doc="""
        How many of the latest entries the feed shows at first, and how many
        older ones each "Show earlier requests" adds. Bounding the feed keeps
        switching to Explore quick on a dashboard with a long history.""",
    )

    def __init__(self, editor: FlowDash, **params):
        super().__init__(editor=editor, **params)
        self.state = RunState(flush_interval=0)
        self.runner = None
        self.actions = ActionRegistry()
        self.actions.add(self._add_to_dashboard, name="add_to_dashboard")
        self.actions.add(self._show_on_dashboard, name="show_on_dashboard")
        # The session's explorations, where the Explore view's answers are built, and how to add one.
        self.explorations = None
        self._add: t.Callable[[str, str], t.Awaitable[str]] | None = None
        self._show: t.Callable[[str], None] | None = None
        self._adding = 0
        self._recorded: dict[str, list[ev.Event]] = {}
        self._roots: dict[str, str] = {}
        # The dashboard open when each recorded run started; a run shows in that dashboard's feed only.
        self._run_dashboards: dict[str, str | None] = {}
        self._applied: list[ev.Event] = []
        # The projected change lists, which may grow while their turn is open.
        self._summaries: set[str] = set()
        # The watchers of the data cards' values, by node.
        self._value_watchers: dict[str, tuple[t.Any, str, t.Callable, t.Any]] = {}
        self._published: dict[str, ev.ArtifactPublished] = {}
        self._notes: dict[str, tuple[pmui.Typography, t.Callable[[], dict]]] = {}
        self._unsubscribe = None
        self._pending = False
        self._chat: AgentChat | None = None
        self._chat_params: dict = {}
        self._model_chat: AgentChat | None = None
        # The chat feed's own frame, a card, restored when it leaves the dock.
        self._card_frame: dict[str, t.Any] | None = None
        self._model_watchers: list = []
        self._chat_key: tuple | None = None
        self._foreign: t.Callable[[str], bool] | None = None
        # Says that a question waits for the assistant's request to finish its edits.
        self._waiting = pmui.Typography(
            "",
            variant="body2",
            visible=False,
            margin=(4, 10),
            sx={"color": "text.secondary", "textAlign": "center"},
            css_classes=["explore-waiting"],
        )
        self._empty = pmui.Typography(
            "Ask a question about this dashboard to start. Each answer and the outputs it adds show here.",
            variant="body2",
            margin=(24, 10),
            sx={"color": "text.secondary", "textAlign": "center"},
            css_classes=["explore-empty"],
        )
        self._layout = pn.Column(
            sizing_mode="stretch_both",
            margin=0,
            css_classes=["flowdash-explore"],
            stylesheets=[_narrow_css()],
        )
        # The index of the first entry shown; None shows the latest page at the next sync.
        self._first: int | None = None
        self._hidden = 0
        self._earlier = pmui.Button(
            label="Show earlier requests",
            icon="expand_less",
            variant="text",
            size="small",
            visible=False,
            margin=(4, 10),
            sx={"textTransform": "none", "alignSelf": "center"},
            css_classes=["explore-earlier-requests"],
        )
        self._earlier.on_click(lambda _event: self.show_earlier())
        editor.param.watch(self._on_dashboard, "dashboard")

    def _on_dashboard(self, *_events):
        self._first = None
        self._schedule()

    def show_earlier(self):
        """Add the next page of older entries above those shown.

        The entries shown stay as they are and the older ones go in before
        them, so the feed keeps the reader at the same entry.
        """
        if not self._first or not self.shown:
            return
        self._first = max(0, self._first - self.page_size)
        timeline, cells = self.project()
        applied = self._applied
        added = len(timeline) - len(applied)
        if added < 0 or [self._key(e) for e in timeline[added:]] != [
            self._key(e) for e in applied
        ]:
            self.sync()
            return
        state = self.state
        shown = list(state.items)
        # The older runs are applied without telling the chat, then put in front of
        # the shown ones, so the chat inserts them rather than appending them.
        with param.parameterized.discard_events(state):
            state.apply_many(timeline[:added])
        known = {id(item) for item in shown}
        state.items = [*(i for i in state.items if id(i) not in known), *shown]
        state.param.trigger("runs")
        self._applied = list(timeline)
        for aid, event in cells.items():
            if aid not in self._published:
                state.apply(event)
                self._published[aid] = event
        self.sync()

    def __panel__(self):
        """Render the feed."""
        return self._layout

    @property
    def chat(self) -> AgentChat:
        """The chat showing the feed, built for the person's rights and the attached agent."""
        key = (self.runner is not None and not self.editor.read_only, self.runner)
        if self._chat is None or self._chat_key != key:
            self._chat_key = key
            # Shown by the next sync, once it holds the feed's runs and cards: added to a chat
            # already on the page, each card is a patch of its own, and under load the
            # browser can miss some of them.
            self._chat = self._build_chat(composer=key[0])
            self._frame_chat()
        return self._chat

    def _frame_chat(self):
        """Lay the feed flat in Ask's dock, which frames it, and as a card in the Explore view."""
        if self._chat is None:
            return
        feed = self._chat.feed
        if self._card_frame is None:
            self._card_frame = {"sx": feed.sx, "margin": feed.margin}
        frame = _DOCKED_FRAME if self.docked else self._card_frame
        feed.param.update(**frame)

    def _build_chat(self, composer: bool) -> AgentChat:
        params: dict[str, t.Any] = {}
        if composer:
            params.update(
                runner=self.runner,
                approval_factory=self._approval,
                placeholder="Ask a question about this dashboard…",
                **self._chat_params,
            )
        if "user_placement" in AgentChat.param:
            # As in the assistant's chat: questions as bubbles on the right, no avatars or names.
            params.update(
                user_placement="right", message_params={"show_avatar": False, "show_user": False}
            )
        from panel_flowdash.agent.chrome import TOOL_LABELS

        chat = AgentChat(
            self.state,
            layout="document",
            readonly=not composer,
            user="You",
            tool_labels=TOOL_LABELS,
            # The feed renders every entry it holds, at most a few pages: a window of
            # rendered entries that shifts as one is added re-renders the feed and
            # loses the reader's place.
            feed_params={"load_buffer": FEED_LOAD_BUFFER},
            **({"avatars": {EARLIER_AGENT: "📊"}} if "avatars" in AgentChat.param else {}),
            inline_artifacts=lambda artifact: (
                artifact.kind in NODE_KINDS.values() or artifact.kind == EARLIER_KIND
            ),
            cell_renderer=self._render,
            action_registry=self.actions,
            artifact_actions=[
                ArtifactAction(
                    "add_to_dashboard",
                    "Add to dashboard",
                    icon="add",
                    arguments=self._placement,
                    done=self._placed,
                ),
                ArtifactAction(
                    "show_on_dashboard",
                    "Show",
                    icon="visibility",
                    arguments=self._showable,
                ),
            ],
            **params,
        )
        if composer:
            self._follow_model(chat)
        return chat

    def _follow_model(self, chat: AgentChat):
        """Keep the composer's picked model and the drawer chat's the same, so both ask the same model."""
        for obj, watcher in self._model_watchers:
            obj.param.unwatch(watcher)
        self._model_watchers = []
        other = self._model_chat
        if other is None or "model_id" not in chat.param:
            return
        if other.model_id is not None:
            chat.model_id = other.model_id

        def follow(source, target):
            def sync(event):
                if target.model_id != event.new:
                    target.model_id = event.new

            return source, source.param.watch(sync, "model_id")

        self._model_watchers = [follow(chat, other), follow(other, chat)]

    def show_waiting(self, waiting: str | None):
        """Say that the question asked waits for *waiting*, e.g. "the assistant's edit", or stop saying it."""
        if waiting is not None:
            self._waiting.object = f"Waiting for {waiting} to finish…"
        self._waiting.visible = waiting is not None

    def _approval(self, request, state):
        """Decide a forwarded approval on the runner's own request, so the runner hears of it."""
        source = self.runner.state if self.runner is not None else None
        if source is not None and request.tool_call_id in source.approvals:
            return ApprovalCard(request=source.approvals[request.tool_call_id], state=source)
        return ApprovalCard(request=request, state=state)

    def attach(
        self,
        runner,
        chat_params: dict | None = None,
        model_chat: AgentChat | None = None,
        foreign: t.Callable[[str], bool] | None = None,
        explorations: t.Any = None,
        add: t.Callable[[str, str], t.Awaitable[str]] | None = None,
        show: t.Callable[[str], None] | None = None,
    ):
        """Ask the Explore view's agent *runner* (a panel-agents ``AgentRunner``) and show its runs.

        *chat_params* are passed to the composer's chat, e.g. the models a
        person may pick from, and *model_chat* is another chat, such as the
        drawer's, whose picked model the composer shares. *foreign* tells
        whether a run id is another runner's, which the feed then never
        counts as asked in Explore, whatever the id looks like.

        *explorations* is the session's
        :class:`~panel_flowdash.agent.exploration.ExplorationOps`, where the
        runner builds its answers; their cards show the exploration's nodes.
        *add* is awaited with a run and one of its answer nodes to add the
        answer to the dashboard, returning the node's name there, and *show*
        shows a dashboard node, e.g. an answer once added.
        """
        self._foreign = foreign
        self.explorations = explorations
        self._add = add
        self._show = show
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._chat_params = dict(chat_params or {})
        self._model_chat = model_chat
        self.runner = runner
        self._unsubscribe = runner.state.subscribe(self._on_event)
        if self.shown:
            self.sync()

    @param.depends("docked", watch=True)
    def _on_docked(self):
        self._frame_chat()
        # The cards rendered for the other placement show or leave out the dashboard's views.
        if self.shown and self._applied:
            self._rebuild(*self.project())

    @param.depends("shown", watch=True)
    def _on_shown(self):
        # Hidden cards stop following their nodes' values and catch up when shown again.
        for instance_id in list(self._value_watchers):
            if self.shown:
                self._watch_value(instance_id)
            else:
                self._unwatch_value(instance_id, forget=False)
        if self.shown:
            self.chat  # noqa: B018
            self.sync()

    def _dashboard_id(self) -> str | None:
        dashboard = self.editor.dashboard
        return dashboard.dashboard_id if dashboard is not None else None

    # ------------------------------------------------------------------
    # The agent session's events
    # ------------------------------------------------------------------

    def _on_event(self, event: ev.Event):
        if isinstance(event, _ARTIFACT_EVENTS) or event.run_id is None:
            return
        run_id = event.run_id
        if isinstance(event, ev.RunStarted) and event.parent_run_id is not None:
            self._roots[run_id] = self._roots.get(event.parent_run_id, event.parent_run_id)
        root = self._roots.get(run_id, run_id)
        if root not in self._recorded:
            self._run_dashboards[root] = self._dashboard_id()
        self._recorded.setdefault(root, []).append(event)
        self._schedule()

    def _schedule(self, *_events):
        if not self.shown or self._pending:
            return
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self.sync()
            return
        self._pending = True

        def run():
            self._pending = False
            self.sync()

        doc.add_timeout_callback(run, _COALESCE_MS)

    # ------------------------------------------------------------------
    # The projection
    # ------------------------------------------------------------------

    def _items(self, editor: FlowDash | None = None) -> dict[str, dict]:
        return {item["instance_id"]: item for item in (editor or self.editor)._tile_items}

    def _names(self, editor: FlowDash | None = None) -> dict[str, str]:
        from panel_flowdash.agent.yaml_format import node_names

        return node_names((editor or self.editor)._tile_items)

    def _shown_names(self, editor: FlowDash | None = None) -> dict[str, str]:
        """Return the node names cards show, leaving unnamed the nodes the person may not use."""
        editor = editor or self.editor
        return {
            instance_id: RESTRICTED_TITLE if editor._node_restricted(instance_id) else name
            for instance_id, name in self._names(editor).items()
        }

    def _exploration(self):
        """Return the open dashboard's exploration, if the session has made one."""
        if self.explorations is None:
            return None
        return self.explorations.explorations().get(self._dashboard_id())

    def _asked_here(self, turn: DashboardTurn) -> bool:
        """Whether *turn* is a request asked in the Explore view, in this session or earlier."""
        run = turn.run
        if run is None:
            return False
        if run in self._recorded or run in self._roots:
            return True
        if self._foreign is not None and self._foreign(run):
            return False
        return is_explore_run(run)

    def project(self) -> tuple[list[ev.Event], dict[str, ev.ArtifactPublished]]:
        """Return the feed's runs as events, oldest first, and its cards by artifact id.

        With an Explore agent attached the feed holds the Explore view's own
        requests only; without one it is the record of every turn.
        """
        editor = self.editor
        turns = [turn for turn in editor.turns if turn.status != "pending"]
        asking = self.runner is not None
        others: list[DashboardTurn] = []
        if asking:
            # The dashboard's other turns, from the assistant, by hand or asked here before
            # Explore had its own agent, are kept in a collapsed entry at the top.
            others = [turn for turn in turns if not self._asked_here(turn)]
            turns = [turn for turn in turns if self._asked_here(turn)]
        self._summaries = set()
        names = self._shown_names()
        items = self._items()
        made: dict[str, list[str]] = {}
        earlier = []
        known = {turn.id for turn in turns}
        for instance_id in items:
            origin = editor.node_origin(instance_id)
            if origin in known:
                made.setdefault(origin, []).append(instance_id)
            elif not asking or origin not in {turn.id for turn in others}:
                earlier.append(instance_id)
        entries: list[tuple[float, list[ev.Event]]] = []
        cells: dict[str, ev.ArtifactPublished] = {}
        if asking and (others or earlier):
            entries.append((float("-inf"), self._history_run(others, earlier)))
            cells[f"{_EARLIER_RUN}:nodes"] = ev.ArtifactPublished(
                artifact_id=f"{_EARLIER_RUN}:nodes",
                value={"nodes": earlier, "turns": [turn.id for turn in others]},
                kind=EARLIER_KIND,
                title=HISTORY_TITLE,
                run_id=_EARLIER_RUN,
                timestamp=0.0,
            )
        elif earlier:
            entries.append((float("-inf"), self._earlier_run(earlier, names)))
            cells[f"{_EARLIER_RUN}:nodes"] = ev.ArtifactPublished(
                artifact_id=f"{_EARLIER_RUN}:nodes",
                value={"nodes": earlier},
                kind=EARLIER_KIND,
                title=EARLIER_TITLE,
                run_id=_EARLIER_RUN,
                timestamp=0.0,
            )
        dashboard_id = self._dashboard_id()
        recorded = {
            run_id: events
            for run_id, events in self._recorded.items()
            if self._run_dashboards.get(run_id) == dashboard_id
        }
        exploration = self._exploration()
        placed = set()
        for turn in turns:
            if self.explorations is not None and turn.run in recorded:
                # This session's answers are cards of its exploration; a dashboard turn of one
                # of its runs is an answer added to the dashboard, which its card says.
                continue
            if turn.run is not None and turn.run in recorded and turn.run not in placed:
                run_id = turn.run
                placed.add(run_id)
                events = recorded[run_id]
                entries.append((events[0].timestamp, list(events)))
            else:
                run_id = f"turn-{turn.id}"
                entries.append((_turn_time(turn), self._projected_run(turn, run_id)))
            for instance_id in made.get(turn.id, ()):
                item = items[instance_id]
                kind = self._kind(item)
                cells[f"node:{instance_id}"] = ev.ArtifactPublished(
                    artifact_id=f"node:{instance_id}",
                    value={"node": instance_id},
                    kind=kind,
                    title=names.get(instance_id, instance_id),
                    metadata={"turn": turn.id},
                    run_id=run_id,
                    timestamp=_turn_time(turn),
                )
        for run_id, events in recorded.items():
            if run_id not in placed:
                entries.append((events[0].timestamp, list(events)))
            if self.explorations is not None:
                cells.update(self._answer_cells(exploration, run_id, events))
        entries.sort(key=lambda entry: entry[0])
        # The collapsed entry of the turns made outside Explore stays at the top whatever is paged.
        pinned = entries[:1] if asking and entries and entries[0][0] == float("-inf") else []
        entries = entries[len(pinned) :]
        # Only the latest page and the pages asked for since are shown.
        if self._first is None:
            self._first = max(0, len(entries) - self.page_size)
        self._first = min(self._first, max(0, len(entries) - 1))
        self._hidden = self._first
        entries = pinned + entries[self._first :]
        shown = {
            event.run_id
            for _, events in entries
            for event in events
            if isinstance(event, ev.RunStarted)
        }
        cells = {aid: event for aid, event in cells.items() if event.run_id in shown}
        return [event for _, events in entries for event in events], cells

    def _answer_cells(
        self, exploration, run_id: str, events: list[ev.Event]
    ) -> dict[str, ev.ArtifactPublished]:
        """Return a card for each answer of a finished run: each node it made that none of its others read.

        The answers are those the session recorded when the run ended, so a
        card stays when its nodes are gone, e.g. pruned or after opening
        another dashboard.
        """
        if not any(
            isinstance(e, (ev.RunFinished, ev.RunError)) and e.run_id == run_id for e in events
        ):
            return {}
        live = self._items(exploration.editor) if exploration is not None else {}
        cells = {}
        for answer in self.explorations.answers(self._dashboard_id(), run_id):
            node = answer.node
            artifact_id = f"explore:{run_id}:{node}"
            item = live.get(node) or {"instance_id": node, "component_id": answer.component}
            editor = exploration.editor if node in live else self.editor
            cells[artifact_id] = ev.ArtifactPublished(
                artifact_id=artifact_id,
                value={"exploration": run_id, "node": node},
                kind=self._kind(item, editor),
                title=answer.title,
                run_id=run_id,
                timestamp=events[0].timestamp,
            )
        return cells

    def _earlier_run(self, earlier: list[str], names: dict[str, str]) -> list[ev.Event]:
        count = len(earlier)
        text = (
            f"This dashboard has {count} output{'s' * (count != 1)} made before its requests "
            "were recorded. Open one to see it here."
        )
        return [
            ev.RunStarted(run_id=_EARLIER_RUN, agent_name=EARLIER_AGENT, timestamp=0.0),
            *_message(_EARLIER_RUN, "a", "assistant", text, 0.0),
            ev.RunFinished(run_id=_EARLIER_RUN, timestamp=0.0),
        ]

    def _history_run(self, others: list[DashboardTurn], nodes: list[str]) -> list[ev.Event]:
        parts = []
        if others:
            n = len(others)
            parts.append(
                f"{n} earlier request{'s' * (n != 1)} and edit{'s' * (n != 1)} made outside Explore"
            )
        if nodes:
            n = len(nodes)
            parts.append(f"{n} output{'s' * (n != 1)} made before requests were recorded")
        text = f"This dashboard has {' and '.join(parts)}. Show them to see each one here."
        self._summaries.add(f"{_EARLIER_RUN}-a")
        return [
            ev.RunStarted(run_id=_EARLIER_RUN, agent_name=EARLIER_AGENT, timestamp=0.0),
            *_message(_EARLIER_RUN, "a", "assistant", text, 0.0),
            ev.RunFinished(run_id=_EARLIER_RUN, timestamp=0.0),
        ]

    def _turn_text(self, turn: DashboardTurn) -> str:
        """Describe an earlier turn for the collapsed history: who, what was asked, what changed."""
        redacted = (
            self.editor.run_redacted(turn.run) if turn.actor == "agent" else self.editor.read_only
        )
        who = "Assistant" if turn.actor == "agent" else "Edited by hand"
        lines = [f"**{who}**"]
        if turn.actor == "agent" and turn.question and not redacted:
            lines[0] += f": {escape_markdown(turn.question)}"
        lines.append(
            _change_list(turn.summary)
            if turn.summary and not redacted
            else "- Changed the dashboard."
        )
        return "\n\n".join(lines)

    def _projected_run(self, turn: DashboardTurn, run_id: str) -> list[ev.Event]:
        at = _turn_time(turn)
        redacted = (
            self.editor.run_redacted(turn.run) if turn.actor == "agent" else self.editor.read_only
        )
        events: list[ev.Event] = []
        if turn.actor == "agent":
            events.append(ev.RunStarted(run_id=run_id, agent_name="Assistant", timestamp=at))
            if turn.question and not redacted:
                events += _message(run_id, "q", "user", turn.question, at)
        else:
            events.append(ev.RunStarted(run_id=run_id, agent_name="Edited by hand", timestamp=at))
        answer = (
            _change_list(turn.summary)
            if turn.summary and not redacted
            else "Changed the dashboard."
        )
        self._summaries.add(f"{run_id}-a")
        events += _message(run_id, "a", "assistant", answer, at)
        events.append(ev.RunFinished(run_id=run_id, timestamp=at))
        return events

    def sync(self):
        """Bring the feed up to date with the dashboard's turns and the agent's runs.

        New runs and cards are appended, a turn's change list that grew is
        updated in place, and a card whose node changed kind gets a new
        version. Only when what was shown no longer holds, e.g. after an undo
        removed turns or nodes, is the feed rebuilt.
        """
        if not self.shown:
            return
        exploration = self._exploration()
        if exploration is not None:
            exploration.sync()
        timeline, cells = self.project()
        applied = self._applied
        prefix = timeline[: len(applied)]
        if [self._key(e) for e in prefix] != [self._key(e) for e in applied] or any(
            aid not in cells for aid in self._published
        ):
            self._rebuild(timeline, cells)
        else:
            for i, (old, new) in enumerate(zip(applied, prefix, strict=True)):
                if old != new:
                    # A projected change list grew; its message is ours, so it is changed in place.
                    self.state.messages[new.message_id].content = new.delta
                    applied[i] = new
            tail = timeline[len(applied) :]
            self.state.apply_many(tail)
            applied.extend(tail)
            for aid, event in cells.items():
                if self._published.get(aid) != event:
                    self.state.apply(event)
                    self._published[aid] = event
        hidden = self._hidden
        self._earlier.param.update(
            visible=bool(hidden),
            label=f"Show earlier requests ({hidden} more)" if hidden else "Show earlier requests",
        )
        content = (
            [self._earlier, self.chat, self._waiting]
            if (timeline or self._chat_key[0])
            else [self._empty]
        )
        if list(self._layout.objects) != content:
            self._layout.objects = content
        self.refresh()

    def _key(self, event: ev.Event):
        """Return what must match for an applied event to stand; a projected change list may grow."""
        if isinstance(event, ev.TextMessageContent) and event.message_id in self._summaries:
            return ("summary", event.message_id)
        return event

    def _rebuild(self, timeline: list[ev.Event], cells: dict[str, ev.ArtifactPublished]):
        self._notes.clear()
        self._release_values()
        self.state.reset()
        self.state.apply_many(timeline)
        self.state.apply_many(cells.values())
        self._applied = list(timeline)
        self._published = dict(cells)

    def refresh(self):
        """Update what the cards say about their nodes: changed later, on the dashboard."""
        for note, describe in list(self._notes.values()):
            note.param.update(**describe())
        if self._chat is not None:
            self._chat.refresh_actions()

    # ------------------------------------------------------------------
    # The cards
    # ------------------------------------------------------------------

    def _note(self, instance_id: str) -> dict[str, t.Any]:
        editor = self.editor
        if instance_id not in self._items():
            return {"object": "Removed from the dashboard.", "visible": True}
        later = editor.later_turns(instance_id)
        if not later:
            return {"object": "", "visible": False}
        from panel_flowdash.ui.provenance import later_summary

        text = later_summary(later, editor._later_turns_may_be_more(instance_id))
        return {"object": f"{text}.", "visible": True}

    def _card_note(self, key: str, describe: t.Callable[[], dict[str, t.Any]]) -> pmui.Typography:
        note = pmui.Typography(
            "",
            variant="caption",
            margin=(4, 12, 0, 12),
            sx={"color": "text.secondary"},
            css_classes=["explore-card-note"],
        )
        note.param.update(**describe())
        self._notes[key] = (note, describe)
        return note

    def _render(self, artifact: Artifact) -> Viewable:
        value = artifact.value or {}
        if artifact.kind == EARLIER_KIND and "turns" in value:
            return self._render_history(value.get("turns", []), value.get("nodes", []))
        if artifact.kind == EARLIER_KIND:
            return self._render_earlier(value.get("nodes", []))
        if "exploration" in value:
            return self._render_answer(value["exploration"], value.get("node"))
        instance_id = value.get("node")
        note = self._card_note(instance_id, lambda: self._note(instance_id))
        return pn.Column(note, self._output(instance_id), sizing_mode="stretch_width", margin=0)

    def _render_answer(self, run_id: str, node: str | None) -> Viewable:
        """Render an answer of the exploration live, with the nodes it is built from collapsed."""
        exploration = self._exploration()
        if exploration is None or node not in exploration._items():
            answer = self.explorations.answer(self._dashboard_id(), run_id, node)
            question = answer.question if answer is not None else None
            text = "This answer is no longer live; ask the question again to rebuild it"
            return self._caption(f"{text}: {question}" if question else f"{text}.")
        editor = exploration.editor
        note = self._card_note(
            f"explore:{run_id}:{node}", lambda: self._answer_note(exploration, run_id, node)
        )
        objects = [note, self._output(node, editor, key=f"explore:{node}")]
        built = [iid for iid in exploration.upstream(node) if not exploration.is_mirror(iid)]
        if built:
            objects.append(self._caption("Built from:"))
            objects.append(self._render_earlier(list(reversed(built)), editor))
        return pn.Column(*objects, sizing_mode="stretch_width", margin=0)

    def _answer_note(self, exploration, run_id: str, node: str) -> dict[str, t.Any]:
        parts = []
        for name, how in exploration.changed_since(run_id, node):
            if how == "changed":
                parts.append(
                    f"{name} changed on the dashboard since; this shows the current {name}."
                )
            else:
                parts.append(
                    f"{name} was {'removed from' if how == 'removed' else 'replaced on'} the "
                    "dashboard; this shows its last output."
                )
        return {"object": " ".join(parts), "visible": bool(parts)}

    def _render_history(self, turn_ids: list[str], nodes: list[str]) -> Viewable:
        """Render the dashboard's turns from outside Explore, collapsed until shown."""
        body = pn.Column(sizing_mode="stretch_width", margin=0, visible=False)
        button = pmui.Button(
            label="Show earlier requests and edits",
            icon="expand_more",
            variant="text",
            size="small",
            margin=(0, 4),
            sx={"textTransform": "none", "justifyContent": "flex-start"},
            css_classes=["explore-history-toggle"],
        )

        def build():
            by_id = {turn.id: turn for turn in self.editor.turns}
            made: dict[str, list[str]] = {}
            for instance_id in self._items():
                made.setdefault(self.editor.node_origin(instance_id), []).append(instance_id)
            rows = []
            for turn_id in turn_ids:
                turn = by_id.get(turn_id)
                if turn is None:
                    continue
                rows.append(
                    pmui.Typography(
                        self._turn_text(turn),
                        variant="body2",
                        margin=(8, 12, 0, 12),
                        css_classes=["explore-history-turn"],
                    )
                )
                if made.get(turn_id):
                    rows.append(self._render_earlier(made[turn_id]))
            if nodes:
                rows.append(self._caption("Made before requests were recorded:"))
                rows.append(self._render_earlier(nodes))
            return rows

        def toggle(_event):
            body.visible = not body.visible
            button.param.update(
                icon="expand_less" if body.visible else "expand_more",
                label="Hide earlier requests and edits"
                if body.visible
                else "Show earlier requests and edits",
            )
            if body.visible and not body.objects:
                body.objects = build()

        button.on_click(toggle)
        return pn.Column(button, body, sizing_mode="stretch_width", margin=(4, 0))

    def _render_earlier(self, nodes: list[str], editor: FlowDash | None = None) -> Viewable:
        names = self._shown_names(editor)
        rows = []
        for instance_id in nodes:
            body = pn.Column(sizing_mode="stretch_width", margin=0, visible=False)
            button = pmui.Button(
                label=names.get(instance_id, instance_id),
                icon="expand_more",
                variant="text",
                size="small",
                margin=(0, 4),
                sx={"textTransform": "none", "justifyContent": "flex-start"},
                css_classes=["explore-earlier-toggle"],
            )

            def toggle(_event, instance_id=instance_id, body=body, button=button):
                body.visible = not body.visible
                button.icon = "expand_less" if body.visible else "expand_more"
                if body.visible and not body.objects:
                    body.objects = [self._output(instance_id, editor)]

            button.on_click(toggle)
            rows.append(pn.Column(button, body, sizing_mode="stretch_width", margin=0))
        return pn.Column(*rows, sizing_mode="stretch_width", margin=(4, 0))

    def _output(
        self, instance_id: str | None, editor: FlowDash | None = None, key: str | None = None
    ) -> Viewable:
        """Return a node's live output: its view, or its first output's value for data.

        *editor* holds the node, by default the dashboard's, e.g. the
        exploration's for an answer; *key* names the card's value watcher.
        """
        dashboard = editor is None or editor is self.editor
        editor = editor or self.editor
        items = list(editor._tile_items)
        index = next(
            (i for i, item in enumerate(items) if item["instance_id"] == instance_id), None
        )
        if index is None:
            return pn.Spacer(height=0)
        item = items[index]
        if "placeholder" in item or editor._node_restricted(instance_id):
            return self._caption("This output uses a component you may not see.")
        spec = editor._component_specs.get(item["component_id"])
        if not self._shows_view(item, editor):
            return self._value(instance_id, spec.outputs[0].name, editor, key)
        if dashboard and tile_layout.area_of(editor, item) == tile_layout.SIDEBAR:
            return self._caption("Shown in the sidebar.")
        if dashboard and self.docked:
            return self._caption(
                "Shown on the dashboard."
                if self._placed_item(item)
                else "Add it to the dashboard to see it beside this conversation."
            )
        view = editor._tile_objects[index] if index < len(editor._tile_objects) else None
        if view is None:
            return self._caption("This output has nothing to show yet.")
        if editor._node_role(item["component_id"]) == "widget":
            # A control takes the height of its widgets.
            return pn.Column(view, sizing_mode="stretch_width", margin=(4, 8))
        size = (spec.default_size if spec is not None else None) or {}
        entry = tile_layout.tile_entry(editor, instance_id).get("height") if dashboard else None
        height = max(int(entry or 0), int(size.get("height") or 0), CARD_HEIGHT)
        return pn.Column(view, height=height, sizing_mode="stretch_width", margin=(4, 8))

    def _kind(self, item: dict, editor: FlowDash | None = None) -> str:
        """Return the kind a node's card is labelled with, the same whether it shows the view or the value."""
        editor = editor or self.editor
        role = editor._node_role(item["component_id"])
        if role in ("view", "widget"):
            return NODE_KINDS[role]
        spec = editor._component_specs.get(item["component_id"])
        if (
            spec is not None
            and spec.outputs
            and port_types_compatible(spec.outputs[0].type, "DataFrame")
        ):
            return NODE_KINDS["source"]
        return NODE_KINDS["other"]

    def _shows_view(self, item: dict, editor: FlowDash | None = None) -> bool:
        """Whether a node's card shows its view rather than its first output's value.

        A table is what a data node is asked for, so a table output is shown as
        one; any other output only when the node's tile is not on the dashboard,
        as for a source of numbers, while a chart that reports a selection
        shows the chart. An answer's node is on no dashboard, so its value shows.
        """
        dashboard = editor is None or editor is self.editor
        editor = editor or self.editor
        spec = editor._component_specs.get(item["component_id"])
        if spec is None or not spec.outputs:
            return True
        if editor._node_role(item["component_id"]) in ("view", "widget"):
            return True
        if port_types_compatible(spec.outputs[0].type, "DataFrame"):
            return False
        return dashboard and self._placed_item(item)

    def _value(
        self, instance_id: str, port: str, editor: FlowDash | None = None, key: str | None = None
    ) -> Viewable:
        from panel_agents.components.renderers import render

        key = key or instance_id
        state = (editor or self.editor)._dataflow_graph.get_state(instance_id)
        body = pn.Column(sizing_mode="stretch_width", margin=(0, 4))

        def update(*_):
            value = getattr(state, port, None)
            if value is None:
                body.objects = [self._caption("No value yet.")]
                return
            try:
                body.objects = [render(value, page_size=10)]
            except Exception:
                logger.exception("Could not show the output of %s", instance_id)
                body.objects = [self._caption("This output could not be shown.")]

        update()
        self._unwatch_value(key)
        if state is not None and port in state.param:
            self._value_watchers[key] = (state, port, update, None)
            if self.shown:
                self._watch_value(key)
        return body

    def _watch_value(self, instance_id: str):
        state, port, update, watcher = self._value_watchers[instance_id]
        if watcher is None:
            self._value_watchers[instance_id] = (
                state,
                port,
                update,
                state.param.watch(update, port),
            )
            update()

    def _unwatch_value(self, instance_id: str, forget: bool = True):
        entry = (
            self._value_watchers.pop(instance_id, None)
            if forget
            else self._value_watchers.get(instance_id)
        )
        if entry is None:
            return
        state, port, update, watcher = entry
        if watcher is not None:
            state.param.unwatch(watcher)
        if not forget:
            self._value_watchers[instance_id] = (state, port, update, None)

    def _release_values(self):
        """Stop following the values of the data cards, e.g. when they are rendered anew."""
        for instance_id in list(self._value_watchers):
            self._unwatch_value(instance_id)

    def _caption(self, text: str) -> Viewable:
        return text_typography(
            text, variant="body2", margin=(8, 12), sx={"color": "text.secondary"}
        )

    # ------------------------------------------------------------------
    # Add to dashboard
    # ------------------------------------------------------------------

    def _node_of(self, artifact: Artifact) -> str | None:
        value = artifact.value
        if not isinstance(value, dict) or "exploration" in value:
            return None
        return value.get("node")

    @staticmethod
    def _answer_of(artifact: Artifact) -> tuple[str, str] | None:
        value = artifact.value
        if isinstance(value, dict) and "exploration" in value:
            return value["exploration"], value.get("node")
        return None

    def _added_as(self, answer: tuple[str, str]) -> str | None:
        """Return the dashboard node an answer was added as, while the dashboard still has it."""
        record = self._answer_record(answer)
        name = record.added if record is not None else None
        return name if name is not None and name in self._names().values() else None

    def _answer_record(self, answer: tuple[str, str]):
        if self.explorations is None:
            return None
        return self.explorations.answer(self._dashboard_id(), *answer)

    def _placement(self, artifact: Artifact) -> dict[str, t.Any] | None:
        answer = self._answer_of(artifact)
        if answer is not None:
            if self._add is None or self.editor.read_only or self._added_as(answer):
                return None
            if not self.explorations.live(self._dashboard_id(), answer[1]):
                return None
            return {"exploration": answer[0], "node": answer[1]}
        instance_id = self._node_of(artifact)
        editor = self.editor
        if instance_id is None or editor.read_only or instance_id not in self._items():
            return None
        if "placeholder" in self._items()[instance_id]:
            return None
        return {"name": self._names().get(instance_id, instance_id)}

    def _placed_item(self, item: dict) -> bool:
        if tile_layout.area_of(self.editor, item) in (tile_layout.TOP, tile_layout.SIDEBAR):
            return True
        return any(
            i["instance_id"] == item["instance_id"] for i in tile_layout.grid_tiles(self.editor)
        )

    def _showable(self, artifact: Artifact) -> dict[str, t.Any] | None:
        answer = self._answer_of(artifact)
        name = self._added_as(answer) if answer is not None else None
        return {"name": name} if name is not None and self._show is not None else None

    def _placed(self, artifact: Artifact) -> str | None:
        answer = self._answer_of(artifact)
        if answer is not None:
            name = self._added_as(answer)
            if name is None:
                return None
            record = self._answer_record(answer)
            already = record is not None and record.already
            return f"{'Already on' if already else 'On'} dashboard as {name}"
        instance_id = self._node_of(artifact)
        item = self._items().get(instance_id) if instance_id is not None else None
        return "On dashboard" if item is not None and self._placed_item(item) else None

    def _ops(self):
        session = getattr(self.runner, "_session", None)
        if session is not None:
            return session.ops
        from panel_flowdash.agent.ops import DashboardOps

        return DashboardOps(self.editor)

    @property
    def adding(self) -> bool:
        """Whether an add from one of the feed's cards is under way."""
        return self._adding > 0

    async def _add_to_dashboard(
        self, name: str | None = None, exploration: str | None = None, node: str | None = None
    ) -> str:
        """Add an answer of the exploration to the dashboard, or show a dashboard node's output as a tile.

        An add from the dock that starts edit mode shows the tile there, as Show does.
        """
        docked = self.docked
        self._adding += 1
        try:
            if exploration is not None and node is not None:
                if self._add is None:
                    raise ValueError("Answers cannot be added to this dashboard.")
                name = await self._add(exploration, node)
            elif name is not None:
                self._ops().place_tile(name, visible=True)
            else:
                raise ValueError("Name a node, or an answer by its exploration and node.")
        finally:
            self._adding -= 1
        self.refresh()
        if docked and self.editor.editable and self._show is not None:
            self._show(name)
        return f"Added {name} to the dashboard."

    def _show_on_dashboard(self, name: str) -> str:
        """Show the tiles and point out the dashboard node *name*."""
        if self._show is not None:
            self._show(name)
        return f"Showing {name}."


__all__ = ["EARLIER_KIND", "EXPLORE_RUN_PREFIX", "NODE_KINDS", "ExploreFeed", "is_explore_run"]
