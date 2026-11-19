"""The Explore view: a dashboard's turns read as a feed, with each turn's outputs shown live.

Every turn of the open dashboard becomes a run of the feed's own panel-agents
``RunState``, shown by an ``AgentChat`` in its document layout. A run asked in
this session is the agent session's run itself, forwarded event by event, so
the feed shows its steps, answer and approvals as they stream. Any other turn,
one made before a reload, by someone else or by hand, is projected from the
dashboard file: the question, what the turn changed, and no model calls.

Each node a turn created is an inline artifact of that run whose value refers
to the node, never its data, so the card always shows the node's current
output and never disagrees with its tile: a view's card shows the view its
tile shows, which can only be on screen once, so the Explore view and the
editor's workspace are never shown together. Nodes made before the first
recorded turn are listed, collapsed, in a leading cell.
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
from panel_flowdash.ui.text import text_typography

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

#: The height of a card showing a component's view, when its size hints give none.
CARD_HEIGHT = 320

_EARLIER_RUN = "explore-earlier"

_ARTIFACT_EVENTS = (ev.ArtifactPublished, ev.ArtifactApplied, ev.ArtifactVersionsDropped)

# How long forwarded events are gathered before the feed catches up, in milliseconds.
_COALESCE_MS = 50


def _turn_time(turn: DashboardTurn) -> float:
    try:
        return datetime.fromisoformat((turn.at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _message(run_id: str, key: str, role: str, text: str, at: float) -> list[ev.Event]:
    message_id = f"{run_id}-{key}"
    return [
        ev.TextMessageStart(message_id=message_id, role=role, run_id=run_id, timestamp=at),
        ev.TextMessageContent(message_id=message_id, delta=text, run_id=run_id, timestamp=at),
        ev.TextMessageEnd(message_id=message_id, run_id=run_id, timestamp=at),
    ]


class ExploreFeed(Viewer):
    """The Explore view of a :class:`~panel_flowdash.editor.FlowDash` editor.

    Without an agent it shows the projected turns. :meth:`attach` connects
    an agent session's runner: its runs then show in full as they happen,
    and the feed gets a composer for people who may edit the dashboard.
    """

    editor = param.Parameter(doc="The FlowDash editor whose dashboard the feed shows.")

    shown = param.Boolean(
        default=False,
        doc="""
        Whether the feed is on screen. While hidden it only records the
        agent's events, and catches up when shown.""",
    )

    def __init__(self, editor: FlowDash, **params):
        super().__init__(editor=editor, **params)
        self.state = RunState(flush_interval=0)
        self.runner = None
        self.actions = ActionRegistry()
        self.actions.add(self._add_to_dashboard, name="add_to_dashboard")
        self._recorded: dict[str, list[ev.Event]] = {}
        self._roots: dict[str, str] = {}
        # The dashboard open when each recorded run started; a run shows in that dashboard's feed only.
        self._run_dashboards: dict[str, str | None] = {}
        self._applied: list[ev.Event] = []
        self._published: dict[str, ev.ArtifactPublished] = {}
        self._notes: dict[str, pmui.Typography] = {}
        self._unsubscribe = None
        self._pending = False
        self._chat: AgentChat | None = None
        self._chat_key: tuple | None = None
        self._empty = pmui.Typography(
            "Ask a question about this dashboard to start. Each answer and the outputs it adds show here.",
            variant="body2",
            margin=(24, 10),
            sx={"color": "text.secondary", "textAlign": "center"},
            css_classes=["explore-empty"],
        )
        self._layout = pn.Column(
            sizing_mode="stretch_both", margin=0, css_classes=["flowdash-explore"]
        )
        editor.param.watch(self._schedule, "dashboard")

    def __panel__(self):
        """Render the feed."""
        return self._layout

    @property
    def chat(self) -> AgentChat:
        """The chat showing the feed, built for the person's rights and the attached agent."""
        key = (self.runner is not None and not self.editor.read_only, self.runner)
        if self._chat is None or self._chat_key != key:
            self._chat_key = key
            self._chat = self._build_chat(composer=key[0])
            self._layout.objects = [self._chat]
        return self._chat

    def _build_chat(self, composer: bool) -> AgentChat:
        params: dict[str, t.Any] = {}
        if composer:
            params.update(
                runner=self.runner,
                approval_factory=self._approval,
                placeholder="Ask a question about this dashboard…",
            )
        from panel_flowdash.agent.chrome import TOOL_LABELS

        return AgentChat(
            self.state,
            layout="document",
            readonly=not composer,
            user="You",
            tool_labels=TOOL_LABELS,
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
                )
            ],
            **params,
        )

    def _approval(self, request, state):
        """Decide a forwarded approval on the agent session's own request, so its runner hears of it."""
        source = self.runner.state if self.runner is not None else None
        if source is not None and request.tool_call_id in source.approvals:
            return ApprovalCard(request=source.approvals[request.tool_call_id], state=source)
        return ApprovalCard(request=request, state=state)

    def attach(self, runner):
        """Follow the runs of an agent *runner* (a panel-agents ``AgentRunner``) from now on."""
        if self._unsubscribe is not None:
            self._unsubscribe()
        self.runner = runner
        self._unsubscribe = runner.state.subscribe(self._on_event)
        if self.shown:
            self.chat  # noqa: B018

    @param.depends("shown", watch=True)
    def _on_shown(self):
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

    def _items(self) -> dict[str, dict]:
        return {item["instance_id"]: item for item in self.editor._tile_items}

    def _names(self) -> dict[str, str]:
        from panel_flowdash.agent.yaml_format import node_names

        return node_names(self.editor._tile_items)

    def project(self) -> tuple[list[ev.Event], dict[str, ev.ArtifactPublished]]:
        """Return the feed's runs as events, oldest first, and its cards by artifact id."""
        editor = self.editor
        turns = [turn for turn in editor.turns if turn.status != "pending"]
        names = self._names()
        items = self._items()
        made: dict[str, list[str]] = {}
        earlier = []
        known = {turn.id for turn in turns}
        for instance_id in items:
            origin = editor.node_origin(instance_id)
            if origin in known:
                made.setdefault(origin, []).append(instance_id)
            else:
                earlier.append(instance_id)
        entries: list[tuple[float, list[ev.Event]]] = []
        cells: dict[str, ev.ArtifactPublished] = {}
        if earlier:
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
        placed = set()
        for turn in turns:
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
                role = editor._node_role(item["component_id"])
                if role in ("view", "widget"):
                    kind = NODE_KINDS[role]
                elif self._shows_view(item):
                    kind = NODE_KINDS["view"]
                else:
                    kind = NODE_KINDS.get(role, NODE_KINDS["other"])
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
        entries.sort(key=lambda entry: entry[0])
        return [event for _, events in entries for event in events], cells

    def _earlier_run(self, earlier: list[str], names: dict[str, str]) -> list[ev.Event]:
        count = len(earlier)
        text = (
            f"This dashboard has {count} output{'s' * (count != 1)} made before its requests "
            "were recorded. Open one to see it here."
        )
        return [
            ev.RunStarted(run_id=_EARLIER_RUN, agent_name="Dashboard", timestamp=0.0),
            *_message(_EARLIER_RUN, "a", "assistant", text, 0.0),
            ev.RunFinished(run_id=_EARLIER_RUN, timestamp=0.0),
        ]

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
            answer = turn.summary if turn.summary and not redacted else "Changed the dashboard."
        else:
            events.append(ev.RunStarted(run_id=run_id, agent_name="Edited by hand", timestamp=at))
            answer = turn.summary if turn.summary and not redacted else "Changed the dashboard."
        events += _message(run_id, "a", "assistant", answer, at)
        events.append(ev.RunFinished(run_id=run_id, timestamp=at))
        return events

    def sync(self):
        """Bring the feed up to date with the dashboard's turns and the agent's runs.

        New runs and cards are added; when what was shown changed instead, e.g.
        after an undo or because a turn's summary grew, the feed is rebuilt.
        """
        if not self.shown:
            return
        timeline, cells = self.project()
        applied = self._applied
        if timeline[: len(applied)] != applied or any(
            cells.get(aid) != event for aid, event in self._published.items()
        ):
            self._rebuild(timeline, cells)
        else:
            tail = timeline[len(applied) :]
            self.state.apply_many(tail)
            applied.extend(tail)
            for aid, event in cells.items():
                if aid not in self._published:
                    self.state.apply(event)
                    self._published[aid] = event
        shown = self.chat if (timeline or self._chat_key[0]) else self._empty
        if list(self._layout.objects) != [shown]:
            self._layout.objects = [shown]
        self.refresh()

    def _rebuild(self, timeline: list[ev.Event], cells: dict[str, ev.ArtifactPublished]):
        self._notes.clear()
        self.state.reset()
        self.state.apply_many(timeline)
        self.state.apply_many(cells.values())
        self._applied = list(timeline)
        self._published = dict(cells)

    def refresh(self):
        """Update what the cards say about their nodes: changed later, on the dashboard."""
        for instance_id, note in list(self._notes.items()):
            note.param.update(**self._note(instance_id))
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

    def _render(self, artifact: Artifact) -> Viewable:
        value = artifact.value or {}
        if artifact.kind == EARLIER_KIND:
            return self._render_earlier(value.get("nodes", []))
        instance_id = value.get("node")
        note = pmui.Typography(
            "",
            variant="caption",
            margin=(4, 12, 0, 12),
            sx={"color": "text.secondary"},
            css_classes=["explore-card-note"],
        )
        note.param.update(**self._note(instance_id))
        self._notes[instance_id] = note
        return pn.Column(note, self._output(instance_id), sizing_mode="stretch_width", margin=0)

    def _render_earlier(self, nodes: list[str]) -> Viewable:
        names = self._names()
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
                    body.objects = [self._output(instance_id)]

            button.on_click(toggle)
            rows.append(pn.Column(button, body, sizing_mode="stretch_width", margin=0))
        return pn.Column(*rows, sizing_mode="stretch_width", margin=(4, 0))

    def _output(self, instance_id: str | None) -> Viewable:
        """Return a node's live output: its view, or its first output's value for data."""
        editor = self.editor
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
        if not self._shows_view(item):
            return self._value(instance_id, spec.outputs[0].name)
        if tile_layout.area_of(editor, item) == tile_layout.SIDEBAR:
            return self._caption("Shown in the sidebar.")
        view = editor._tile_objects[index] if index < len(editor._tile_objects) else None
        if view is None:
            return self._caption("This output has nothing to show yet.")
        size = (spec.default_size if spec is not None else None) or {}
        height = tile_layout.tile_entry(editor, instance_id).get("height") or size.get("height")
        return pn.Column(
            view, height=int(height or CARD_HEIGHT), sizing_mode="stretch_width", margin=(4, 8)
        )

    def _shows_view(self, item: dict) -> bool:
        """Whether a node's card shows its view rather than its first output's value.

        A table is what a data node is asked for, so a table output is shown as
        one; any other output only when the node's tile is not on the dashboard,
        as for a source of numbers, while a chart that reports a selection
        shows the chart.
        """
        editor = self.editor
        spec = editor._component_specs.get(item["component_id"])
        if spec is None or not spec.outputs:
            return True
        if editor._node_role(item["component_id"]) in ("view", "widget"):
            return True
        if port_types_compatible(spec.outputs[0].type, "DataFrame"):
            return False
        return self._placed_item(item)

    def _value(self, instance_id: str, port: str) -> Viewable:
        from panel_agents.components.renderers import render

        state = self.editor._dataflow_graph.get_state(instance_id)
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
        if state is not None and port in state.param:
            state.param.watch(update, port)
        return body

    def _caption(self, text: str) -> Viewable:
        return text_typography(
            text, variant="body2", margin=(8, 12), sx={"color": "text.secondary"}
        )

    # ------------------------------------------------------------------
    # Add to dashboard
    # ------------------------------------------------------------------

    def _node_of(self, artifact: Artifact) -> str | None:
        value = artifact.value
        return value.get("node") if isinstance(value, dict) else None

    def _placement(self, artifact: Artifact) -> dict[str, t.Any] | None:
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

    def _placed(self, artifact: Artifact) -> str | None:
        instance_id = self._node_of(artifact)
        item = self._items().get(instance_id) if instance_id is not None else None
        return "On dashboard" if item is not None and self._placed_item(item) else None

    def _ops(self):
        session = getattr(self.runner, "_session", None)
        if session is not None:
            return session.ops
        from panel_flowdash.agent.ops import DashboardOps

        return DashboardOps(self.editor)

    def _add_to_dashboard(self, name: str) -> str:
        """Show a node's output as a tile on the dashboard."""
        self._ops().place_tile(name, visible=True, area="grid")
        self.refresh()
        return f"Added {name} to the dashboard."


__all__ = ["EARLIER_KIND", "NODE_KINDS", "ExploreFeed"]
