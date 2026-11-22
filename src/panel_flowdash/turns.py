"""Turns: who changed a dashboard, in which request, and which nodes each change created.

A turn is one request to an agent, or one batch of a person's edits made
between requests. A dashboard keeps its turns in the order they were opened,
and each node records the turn that created it as its ``origin``. Which later
turns changed a node is not stored on the node: it comes from the edit history.

The editor opens and fills turns as edits happen, see
:meth:`~panel_flowdash.editor.FlowDash.end_turn` and :func:`agent_edits`.
An edit belongs to the agent when it is made inside a tool call of a
panel-agents ``AgentRunner``, or inside :func:`agent_edits`, and to the person
using the editor otherwise.
"""

from __future__ import annotations

import copy
import re
import typing as t
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, fields
from datetime import UTC, datetime

from panel_agents import current_run_id, current_tool_call_id

# Who makes the edits of a turn.
TURN_ACTORS = ("agent", "user")

# The statuses a turn can have besides done, which is written as no status at all.
TURN_STATUSES = ("pending",)

# The ids a turn can have. The editor makes ``t`` followed by 8 hex digits.
TURN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# How many turns a dashboard keeps. Past it the oldest turns that created no
# node still on the dashboard are dropped, so the file stays well within the size
# the YAML reader accepts.
MAX_TURNS = 200

# The longest question a turn records; a longer one is cut short with an ellipsis.
MAX_QUESTION_LENGTH = 2000

# The labels a summary lists before saying how many more there are.
_SUMMARY_LABELS = 8


class TurnError(ValueError):
    """A turn's data is malformed. ``key`` names the offending field."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@dataclass
class DashboardTurn:
    """One request to an agent, or one batch of a person's edits, that changed a dashboard.

    Attributes
    ----------
    id
        Unique within the dashboard and never reused, so the nodes'
        ``origin`` and the edit history can refer to it.
    actor
        ``"agent"`` or ``"user"``. A planned turn may leave it out.
    at
        When the turn's first edit was made, as ISO 8601 UTC, e.g.
        ``"2026-11-17T20:14:03Z"``.
    run
        The panel-agents run id of the agent's request.
    question
        What the person asked the agent. Recorded by default; exports can
        leave it out, see :func:`~panel_flowdash.editor.export_model`.
    summary
        What the turn changed, a few words per change.
    status
        ``None`` for a turn that ran, ``"pending"`` for a planned one that has not.
    """

    id: str
    actor: str | None = None
    at: str | None = None
    run: str | None = None
    question: str | None = None
    summary: str | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        """Return the turn as JSON data, leaving out fields that are not set."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }

    @classmethod
    def from_dict(cls, data: t.Any) -> DashboardTurn:
        """Read a turn written by :meth:`to_dict`, ignoring keys it does not know.

        Raises
        ------
        TurnError
            If a field has the wrong type or value, or one that is required is missing.
        """
        if not isinstance(data, dict):
            raise TurnError("turn", f"must be a mapping, not {type(data).__name__}.")
        values = {}
        for f in fields(cls):
            value = data.get(f.name)
            if value is not None and not isinstance(value, str):
                raise TurnError(f.name, f"must be a string, got {value!r}.")
            values[f.name] = value
        turn = cls(**values)
        if turn.id is None:
            raise TurnError("id", "is required.")
        if not TURN_ID_PATTERN.fullmatch(turn.id):
            raise TurnError(
                "id",
                f"must be 1 to 64 letters, digits, '_' and '-', starting with a letter or "
                f"digit, got {turn.id!r}.",
            )
        if turn.status is not None and turn.status not in TURN_STATUSES:
            raise TurnError("status", f"must be 'pending' or left out, got {turn.status!r}.")
        pending = turn.status == "pending"
        if turn.actor is None and not pending:
            raise TurnError("actor", "is required.")
        if turn.actor is not None and turn.actor not in TURN_ACTORS:
            raise TurnError("actor", f"must be 'agent' or 'user', got {turn.actor!r}.")
        if turn.at is None and not pending:
            raise TurnError("at", "is required.")
        if turn.at is not None:
            try:
                datetime.fromisoformat(turn.at)
            except ValueError:
                raise TurnError(
                    "at", f"must be a date and time like '2026-11-17T20:14:03Z', got {turn.at!r}."
                ) from None
        if pending and turn.question is None:
            raise TurnError("question", "is required for a pending turn.")
        return turn


def decode_turns(value: t.Any) -> list[DashboardTurn]:
    """Return stored turns, which may be missing (None), as a list of :class:`DashboardTurn`.

    Raises TypeError unless they are a list, and :class:`TurnError` for a
    malformed turn or two turns with the same id. Turns are optional, so the
    stores catch this and drop just the turns, with a warning, rather than
    skipping the whole dashboard.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"turns must be a list, not {type(value).__name__}")
    turns = [DashboardTurn.from_dict(item) for item in value]
    ids = [turn.id for turn in turns]
    if len(set(ids)) != len(ids):
        duplicate = next(i for i in ids if ids.count(i) > 1)
        raise TurnError("id", f"{duplicate!r} is the id of two turns.")
    return turns


def strip_questions(turns: t.Iterable[dict[str, t.Any]]) -> list[dict[str, t.Any]]:
    """Return copies of exported turns without the questions asked in them."""
    return [{k: copy.deepcopy(v) for k, v in turn.items() if k != "question"} for turn in turns]


@dataclass(frozen=True)
class _AgentEdits:
    run: str | None
    question: str | None


_AGENT_EDITS: ContextVar[_AgentEdits | None] = ContextVar(
    "panel_flowdash_agent_edits", default=None
)


@contextmanager
def agent_edits(run: str | None = None, question: str | None = None) -> Iterator[None]:
    """Count the edits made inside the block as the agent's, in the turn of request *run*.

    The dashboard tools wrap each call in it. Tools of your own that change an
    editor need it only when they do not run in a panel-agents ``AgentRunner``,
    whose tool calls count as the agent's anyway.

    Parameters
    ----------
    run
        The run id of the agent's request; edits of the same run share a
        turn. Defaults to the panel-agents run the block executes in.
    question
        What the person asked, recorded on the turn when it opens.
    """
    token = _AGENT_EDITS.set(_AgentEdits(run or current_run_id(), question))
    try:
        yield
    finally:
        _AGENT_EDITS.reset(token)


def _clear_agent():
    _AGENT_EDITS.set(None)
    # panel-agents' run scope also marks edits as a run's; it has no public way
    # to leave it, so the person's context drops it directly.
    from panel_agents import context as agent_context

    if (scope := getattr(agent_context, "_SCOPE", None)) is not None:
        scope.set(None)


def person_context() -> Context:
    """Return a copy of the current context in which edits count as the person's.

    For work the agent's edits set off that acts for the person, e.g. a held
    back inspector draft checked again after the agent rewired its node: run
    in it, the draft's edits land in the person's own history batch, not in
    the agent's turn.
    """
    context = copy_context()
    context.run(_clear_agent)
    return context


def _agent_source() -> _AgentEdits | None:
    """Return the agent request the calling code edits for, or None for the person's own edits."""
    edits = _AGENT_EDITS.get()
    if edits is not None:
        return edits
    if current_tool_call_id() is not None:
        return _AgentEdits(current_run_id(), None)
    return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _question(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if len(text) > MAX_QUESTION_LENGTH:
        text = text[: MAX_QUESTION_LENGTH - 1].rstrip() + "…"
    return text


def summarize(labels: t.Sequence[str]) -> str | None:
    """Join change labels such as ``"Added kpi"`` into a summary, naming at most 8 of them."""
    if not labels:
        return None
    shown = list(labels[:_SUMMARY_LABELS])
    if len(labels) > _SUMMARY_LABELS:
        more = len(labels) - _SUMMARY_LABELS
        shown.append(f"And {more} more change{'s' * (more != 1)}")
    return " ".join(label if label.endswith(".") else f"{label}." for label in shown)


_MORE_LABELS = re.compile(r"And \d+ more changes?")


def _summary_labels(summary: str | None) -> list[str]:
    """Return the labels :func:`summarize` joined into *summary*, without its count of the rest."""
    if not summary:
        return []
    labels = [label.strip() for label in summary.rstrip(".").split(". ")]
    return [label for label in labels if label and not _MORE_LABELS.fullmatch(label)]


class TurnLog:
    """The turns of the dashboard in an editor, and those still open to more edits.

    A person's turn stays open until :meth:`end_user_turn` or an edit of the
    agent; an agent's turn stays open for the rest of its run, so an edit the
    person makes while the agent works becomes a turn of its own between two
    edits of the agent's turn.
    """

    def __init__(self):
        self.turns: list[DashboardTurn] = []
        self._open: dict[tuple, DashboardTurn] = {}
        self._labels: dict[str, list[str]] = {}
        # The pending turn each agent request answers, by run, see claim_pending.
        self._claims: dict[str | None, str] = {}
        # Pending turns a request took over, as they were, to give back if its edits are taken back.
        self._claimed: dict[str, DashboardTurn] = {}

    def reset(self, turns: t.Iterable[DashboardTurn], *, keep_open: bool = False):
        """Replace the turns, e.g. with a loaded dashboard's; *keep_open* keeps the open ones open."""
        self.turns = [copy.deepcopy(turn) for turn in turns]
        ids = {turn.id for turn in self.turns}
        if keep_open:
            by_id = {turn.id: turn for turn in self.turns}
            self._open = {k: by_id[turn.id] for k, turn in self._open.items() if turn.id in ids}
            # Undo can take a claimed turn back to pending; the request's next edit takes it again.
            for key, turn in list(self._open.items()):
                if key[0] == "agent" and turn.status == "pending":
                    del self._open[key]
                    self._claims[key[1]] = turn.id
        else:
            self._open = {}
        labels = {}
        for turn in self._open.values():
            noted = self._labels.get(turn.id, [])
            # Undo can take an open turn back to an earlier summary, which it then goes on from.
            labels[turn.id] = (
                noted if summarize(noted) == turn.summary else _summary_labels(turn.summary)
            )
        self._labels = labels

    def snapshot(self) -> list[DashboardTurn]:
        """Return copies of the turns, for a model to keep."""
        return copy.deepcopy(self.turns)

    def end_user_turn(self):
        """Close the person's open turn, so their next edit opens a new one."""
        self._open.pop(("user",), None)

    def end_agent_turns(self, run: str | None = None):
        """Close the agent's open turns, or only request *run*'s, so the next request's edits open a new one.

        A pending turn the request claimed but made no edit for stays pending.
        """
        closed = [
            key for key in self._open if key[0] == "agent" and (run is None or key[1] == run)
        ]
        for key in closed:
            self._claimed.pop(self._open.pop(key).id, None)
        if run is None:
            self._claims.clear()
        else:
            self._claims.pop(run, None)

    def discard(self, run: str | None):
        """Drop the open turn of request *run*, e.g. once its edits were all taken back.

        A pending turn the request took over is pending again, as it was.
        """
        turn = self._open.pop(("agent", run), None)
        if turn is None:
            return
        self._labels.pop(turn.id, None)
        original = self._claimed.pop(turn.id, None)
        if original is not None:
            self.turns = [original if t.id == turn.id else t for t in self.turns]
            self._claims[run] = turn.id
        else:
            self.turns = [t for t in self.turns if t.id != turn.id]

    def claim_pending(self, turn_id: str, run: str | None) -> DashboardTurn:
        """Make the pending turn *turn_id* the turn of request *run*'s next edits, and return it.

        The request's turn so far is closed. The pending turn stays pending
        until the request's first edit, which makes it the request's turn:
        the nodes it creates get it as their origin, so its question in the
        report shows them as its answer.

        Raises
        ------
        TurnError
            If no pending turn has the id *turn_id*.
        """
        turn = next((t for t in self.turns if t.id == turn_id), None)
        if turn is None or turn.status != "pending":
            raise TurnError("turn", f"{turn_id!r} is not a question waiting to be run.")
        self._open.pop(("agent", run), None)
        self._claims[run] = turn_id
        return turn

    def current(self) -> DashboardTurn:
        """Return the turn an edit made now belongs to, opening it if needed."""
        agent = _agent_source()
        if agent is None:
            key: tuple = ("user",)
        else:
            # An edit of the agent ends the person's turn.
            self.end_user_turn()
            key = ("agent", agent.run)
        turn = self._open.get(key)
        if turn is None and agent is not None:
            turn = self._take_claim(agent)
        if turn is None:
            turn = DashboardTurn(
                id=self._new_id(),
                actor="user" if agent is None else "agent",
                at=_now(),
                run=None if agent is None else agent.run,
                question=None if agent is None else _question(agent.question),
            )
            self.turns.append(turn)
            self._open[key] = turn
            self._labels[turn.id] = []
        elif agent is not None and turn.question is None:
            turn.question = _question(agent.question)
        return turn

    def _take_claim(self, agent: _AgentEdits) -> DashboardTurn | None:
        """Open the pending turn request *agent* answers for its edits, if it claimed one."""
        turn_id = self._claims.pop(agent.run, None)
        turn = next((t for t in self.turns if t.id == turn_id), None)
        if turn is None or turn.status != "pending":
            return None
        self._claimed[turn.id] = copy.deepcopy(turn)
        turn.status = None
        turn.actor = "agent"
        turn.at = _now()
        turn.run = agent.run
        turn.summary = None
        self._open[("agent", agent.run)] = turn
        self._labels[turn.id] = []
        return turn

    def record(self, *labels: str) -> DashboardTurn:
        """Note an edit, described by *labels*, in the turn it belongs to, and return that turn."""
        turn = self.current()
        noted = self._labels.setdefault(turn.id, [])
        for label in labels:
            if label and label not in noted:
                noted.append(label)
        if noted:
            turn.summary = summarize(noted)
        return turn

    def add_pending(self, question: str) -> DashboardTurn:
        """Add a planned turn asking *question*, not run yet, and return it."""
        text = _question(question)
        if text is None:
            raise TurnError("question", "is required for a pending turn.")
        turn = DashboardTurn(id=self._new_id(), question=text, status="pending")
        self.turns.append(turn)
        return turn

    def drop_pending(self, ids: t.Collection[str]):
        """Drop the pending turns *ids*; turns that ran are kept."""
        self.turns = [
            turn for turn in self.turns if not (turn.status == "pending" and turn.id in ids)
        ]

    def prune(self, keep: t.Collection[str]):
        """Drop the oldest closed turns not in *keep* until at most ``MAX_TURNS`` are left."""
        excess = len(self.turns) - MAX_TURNS
        if excess <= 0:
            return
        protected = set(keep) | {turn.id for turn in self._open.values()}
        dropped = set()
        for turn in self.turns:
            if len(dropped) == excess:
                break
            if turn.id not in protected:
                dropped.add(turn.id)
        self.turns = [turn for turn in self.turns if turn.id not in dropped]

    def _new_id(self) -> str:
        taken = {turn.id for turn in self.turns}
        while True:
            turn_id = f"t{uuid.uuid4().hex[:8]}"
            if turn_id not in taken:
                return turn_id


def changes_node(turn: DashboardTurn, name: str) -> bool:
    """Whether *turn*'s summary says it changed the node called *name*, beyond adding it.

    A summary names at most a few changes, so a turn that changed many nodes
    may leave this one out.
    """
    pattern = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
    return any(
        pattern.search(label) and label != f"Added {name}"
        for label in _summary_labels(turn.summary)
    )


def summary_cut_short(turn: DashboardTurn) -> bool:
    """Whether *turn*'s summary left changes out ("And 3 more changes"), so it may not name every node it changed."""
    return any(
        _MORE_LABELS.fullmatch(label.strip())
        for label in (turn.summary or "").rstrip(".").split(". ")
    )


def _node(ref: t.Any) -> str:
    # Summaries name only the nodes of an edge: a port may be hidden from people
    # who cannot use the node's component, and summaries are shown to everyone.
    return str(ref).partition(".")[0]


def _without_removed(report: t.Any, removed: set[str]) -> t.Any:
    if not isinstance(report, dict):
        return report
    blocks = [
        block
        for block in report.get("blocks") or []
        if not (isinstance(block, dict) and block.get("node") in removed)
    ]
    return {**report, "blocks": blocks}


def change_labels(old: t.Any, new: t.Any) -> list[str]:
    """Describe the change between two dashboard YAML documents as turn summary labels.

    Unlike the diff shown to the person, the labels name what changed but not
    the values, so a summary never repeats a config value such as a query,
    and edges by their nodes alone, so it never names the hidden port of a
    node someone may not use.
    """
    from panel_flowdash.agent.diff import dashboard_changes

    labels: list[str] = []
    changes = dashboard_changes(old, new)
    # The tile of a node added or removed comes and goes with it.
    placed = {c.path[1] for c in changes if c.path[0] == "nodes" and len(c.path) == 2}
    for change in changes:
        path = change.path
        if path[0] == "nodes" and len(path) == 2:
            label = f"{'Added' if change.op == 'add' else 'Removed'} {path[1]}"
        elif path[0] == "nodes" and path[2] == "position":
            label = "Moved nodes on the canvas"
        elif path[0] == "nodes" and path[2] == "component":
            label = f"Changed the component of {path[1]}"
        elif path[0] == "nodes":
            label = f"Changed the config of {path[1]}"
        elif path[0] == "edges" and change.op in ("add", "remove"):
            edge = change.new if change.op == "add" else change.old
            verb = "Connected {} to {}" if change.op == "add" else "Disconnected {} from {}"
            label = verb.format(_node(edge.get("from")), _node(edge.get("to")))
        elif path[0] == "edges":
            source, _, target = change.label.partition(": transform")[0].partition(" → ")
            label = f"Changed the transform from {_node(source)} to {_node(target)}"

        elif path[0] == "layout" and path[1:2] == ("tiles",) and path[2:] and path[2] in placed:
            continue
        elif path[0] == "layout":
            label = "Changed the tile layout"
        elif path[0] == "settings":
            label = "Changed the settings"
        elif path[0] == "report":
            # The blocks of a removed node go with it.
            if _without_removed(change.old, placed) == change.new:
                continue
            label = "Changed the report"
        else:
            continue
        if label not in labels:
            labels.append(label)
    return labels


__all__ = [
    "MAX_QUESTION_LENGTH",
    "MAX_TURNS",
    "TURN_ACTORS",
    "TURN_ID_PATTERN",
    "TURN_STATUSES",
    "DashboardTurn",
    "TurnError",
    "agent_edits",
    "changes_node",
    "decode_turns",
    "strip_questions",
    "summary_cut_short",
]
