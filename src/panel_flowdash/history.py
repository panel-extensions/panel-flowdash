"""The edit history of a dashboard: a version for every edit, undo and redo, and its storage.

Every change made through the editor, by the person using it or by an agent,
becomes a version of the dashboard's document artifact in a panel-agents
``RunState``: the dashboard YAML as the editor's user sees it, with metadata
saying which operation made it. A gesture (dragging nodes, typing into a
config field, resizing tiles) becomes one version, not one per event.

The versions of the dashboard in the editor are written to its store with a
panel-agents ``HistoryRecorder`` as they are made, and loaded again when the
dashboard is next opened, so undo reaches edits of earlier sessions; while
the assistant's drawer is open it is loaded as soon as a dashboard opens, so
the drawer lists them. The stored history starts with a format record
(:data:`HEADER_KEY`). The store keeps a limited number and size of versions
per dashboard and drops a dashboard's history with the dashboard.

An agent session shares the editor's ``RunState``
(:attr:`EditHistory.state`), so its drawer lists the same versions,
grouped by the agent's runs, and its Undo, Redo and Revert buttons go
through the same checks as :meth:`~panel_flowdash.editor.FlowDash.undo`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import typing as t
import uuid
import weakref
from dataclasses import dataclass, field
from functools import partial

import panel as pn
import yaml
from panel_agents import (
    HistoryRecorder,
    RunState,
    current_run_id,
    current_tool_call_id,
    load_history,
)
from panel_agents.events import ArtifactPublished, RunError, RunFinished

from panel_flowdash.turns import _agent_source, summarize

if t.TYPE_CHECKING:
    from panel_agents import Artifact, ArtifactVersion

    from panel_flowdash.editor import FlowDash
    from panel_flowdash.turns import DashboardTurn

logger = logging.getLogger(__name__)

#: The artifact kind the dashboard YAML is published under.
ARTIFACT_KIND = "flowdash-dashboard"

#: How many events the history's ``RunState`` keeps in memory. The versions
#: themselves are kept whatever the bound; it only caps ``state.events``,
#: which also collects the agent's chat when an agent session shares the state.
MAX_EVENTS = 400

# How the history batches edits into versions; the editor's parameters default to these.

#: Seconds without an edit that end a person's batch of edits. Within it, a
#: drag, typing into a field or a run of quick changes is one version.
BURST_SECONDS = 3.0

#: How many operations a version lists in its metadata; the count is always kept.
MAX_BATCH_OPS = 50

#: The key of the record that starts a dashboard's stored history, holding its format version.
HEADER_KEY = "flowdash_history"

#: The format version of the stored history this FlowDash writes and reads.
#: Histories without a header record were written before it and are version 1.
FORMAT_VERSION = 1

#: Shown instead of a stored history of a newer format version.
NEWER_FORMAT = (
    "History was written by a newer FlowDash and isn't shown; changes in this session "
    "won't be recorded."
)

#: About how many bytes of YAML a dashboard's stored history keeps by default.
HISTORY_MAX_BYTES = 16 * 2**20

#: Bytes a version's record is counted with besides its YAML, for its metadata.
_RECORD_OVERHEAD = 512

#: The type of the record a save adds to the stored history, naming the
#: version it saved and the dashboard's revision in the store.
SAVED_RECORD = "FLOWDASH_SAVED"

_REVISION_LINE = re.compile(r"# revision: (?P<token>[0-9a-f]+)\n")
_COMPONENT_LINE = re.compile(r"^\s+component: (.+)$", re.MULTILINE)


def revision_of(text: str) -> str:
    """Return the content hash of dashboard YAML, the token ``DashboardOps.revision`` gives."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def artifact_id_for(dashboard_id: str | None) -> str:
    """Return the id of the document artifact a dashboard's versions are published under."""
    return f"dashboard:{dashboard_id}" if dashboard_id else "dashboard:unsaved"


def _value_revision(value: t.Any) -> str | None:
    match = _REVISION_LINE.match(value) if isinstance(value, str) else None
    return match["token"] if match else None


@dataclass
class _Pending:
    """A version waiting for the rest of its gesture before it is published."""

    key: tuple
    value: str
    revision: str
    base: str | None
    metadata: dict[str, t.Any]
    title: str | None
    actor: str
    user: str | None
    run_id: str | None
    tool_call_id: str | None
    last: float

    labels: list[str]
    # The labelled edits, which the label counts; unlabelled ones such as arranging the layout are left out.
    counted: list[dict[str, t.Any]] = field(default_factory=list)

    def extend(
        self,
        value: str,
        revision: str,
        op: str,
        args: dict[str, t.Any],
        label: str | None,
        now: float,
    ):
        self.value, self.revision, self.last = value, revision, now
        metadata = self.metadata
        metadata["revision"] = revision
        metadata["edits"] = metadata.get("edits", 1) + 1
        ops = metadata.setdefault("ops", [])
        if len(ops) < MAX_BATCH_OPS:
            ops.append({"op": op, "args": dict(args)})
        if label:
            self.counted.append({"op": op, "args": dict(args)})
            if label not in self.labels:
                self.labels.append(label)
            metadata.update(describe_edits(self.counted, self.labels))


@dataclass
class _Request:
    """An agent request's edits since its last version, published together.

    ``value`` and ``revision`` hold the canvas as the agent's latest edit
    left it, so a person's edit made since does not end up in this version.
    """

    artifact_id: str
    dashboard_id: str | None
    run_id: str | None
    turn_id: str | None
    start: str | None
    title: str | None
    ops: list[dict[str, t.Any]]
    tool_call_ids: list[str]
    labels: list[str]
    value: str | None = None
    revision: str | None = None
    counted: list[dict[str, t.Any]] = field(default_factory=list)
    # The order of the request's latest edit among all requests' edits.
    seq: int = 0


def format_version(records: t.Iterable[dict[str, t.Any]]) -> int:
    """Return the format version of stored history *records*; 1 without a header."""
    for record in records:
        if HEADER_KEY in record:
            try:
                return int(record[HEADER_KEY])
            except (TypeError, ValueError):
                return FORMAT_VERSION + 1
    return 1


def _size(record: dict[str, t.Any]) -> int:
    value = record.get("value")
    return (len(value) if isinstance(value, str) else 0) + _RECORD_OVERHEAD


def compact(
    records: list[dict[str, t.Any]],
    limit: int | None,
    max_age: float | None,
    now: float | None = None,
    max_bytes: int | None = None,
) -> list[dict[str, t.Any]]:
    """Return stored history *records* without the versions retention drops.

    The newest *limit* versions no older than *max_age* seconds are kept,
    as many of them as fit in about *max_bytes* (always the newest one),
    with the applied records that name them and the runs they were made in.
    Returns *records* itself when nothing is dropped, and for a history of
    a newer format, which this FlowDash does not change.
    """
    if format_version(records) > FORMAT_VERSION:
        return records
    now = time.time() if now is None else now
    published = [r for r in records if r.get("type") == "ARTIFACT_PUBLISHED"]
    first = max(0, len(published) - limit) if limit else 0
    cutoff = now - max_age if max_age else None
    kept = [
        r
        for r in published[first:]
        if cutoff is None
        or not isinstance(r.get("timestamp"), (int, float))
        or r["timestamp"] >= cutoff
    ]
    if max_bytes is not None and kept:
        # The version the latest save holds stays, so reopening finds the line to the saved dashboard.
        saved = next(
            (r.get("version_id") for r in reversed(records) if r.get("type") == SAVED_RECORD), None
        )
        pin = (
            None
            if saved is None
            else _latest(kept, lambda r: (r.get("metadata") or {}).get("version_id") == saved)
        )
        total = _size(kept[pin]) if pin is not None else 0
        start = len(kept)
        while start > 0 and (
            start == len(kept) or start - 1 == pin or total + _size(kept[start - 1]) <= max_bytes
        ):
            start -= 1
            if start != pin:
                total += _size(kept[start])
        kept = ([kept[pin]] if pin is not None and pin < start else []) + kept[start:]
    if len(kept) == len(published):
        return records
    kept_ids = {id(r) for r in kept}
    versions = {(r.get("metadata") or {}).get("version_id") for r in kept} - {None}
    parents = {
        r.get("run_id"): r.get("parent_run_id") for r in records if r.get("type") == "RUN_STARTED"
    }
    runs: set[str] = set()
    for r in kept:
        run_id = r.get("run_id")
        while run_id is not None and run_id not in runs:
            runs.add(run_id)
            run_id = parents.get(run_id)
    out = []
    for r in records:
        kind = r.get("type")
        if kind is None:
            keep = HEADER_KEY in r
        elif kind == "ARTIFACT_PUBLISHED":
            keep = id(r) in kept_ids
        elif kind == "ARTIFACT_APPLIED":
            keep = r.get("version_id") in versions
        elif kind in ("RUN_STARTED", "RUN_FINISHED", "RUN_ERROR"):
            keep = r.get("run_id") in runs
        elif kind == SAVED_RECORD:
            keep = r.get("version_id") in versions
        else:
            keep = False
        if keep:
            out.append(r)
    return out


def _lineage(published: t.Sequence[tuple[str | None, str | None]], start: int | None) -> list[int]:
    """Return the positions of the versions that lead to version *start*, oldest first.

    *published* holds each version's ``(revision, base)`` from its metadata,
    in the order they were made. The line follows each version's ``base``
    back to the latest earlier version with that revision, so edits that
    were undone, and edits other sessions made but never saved, are not on it.
    """
    if start is None:
        return []
    line = [start]
    base = published[start][1]
    while base is not None:
        earlier = next((k for k in range(line[-1] - 1, -1, -1) if published[k][0] == base), None)
        if earlier is None:
            break
        line.append(earlier)
        base = published[earlier][1]
    return line[::-1]


def _latest(items: t.Sequence[t.Any], match: t.Callable[[t.Any], bool]) -> int | None:
    return next((i for i in range(len(items) - 1, -1, -1) if match(items[i])), None)


def decode(
    records: t.Iterable[dict[str, t.Any]],
    artifact_id: str,
    head: str | None,
    redact: t.Callable[[dict[str, t.Any]], dict[str, t.Any]] | None = None,
    saved: int | None = None,
) -> list[dict[str, t.Any]]:
    """Turn stored history records into the event records ``load_history`` takes.

    Only the versions on the line to the dashboard as it was opened are
    kept (see :func:`_lineage`), with the runs they were made in, so undo
    steps back along the edits that made the saved dashboard and never into
    edits another session undid or did not save. The line ends at the
    version the save of store revision *saved* recorded, or failing that at
    the latest version whose YAML has revision *head*, the latest saved
    version, or the latest version, so a dashboard whose YAML reads
    differently since, e.g. after an upgrade, keeps its history. Applied
    records are left out for the same reason. *redact* rewrites each
    version's record for the person loading it.
    """
    records = list(records)
    raw = [
        r
        for r in records
        if r.get("type") == "ARTIFACT_PUBLISHED" and r.get("artifact_id") == artifact_id
    ]
    shown = [redact(r) if redact is not None else r for r in raw]
    ids = [(r.get("metadata") or {}).get("version_id") for r in raw]
    marks = [r for r in records if r.get("type") == SAVED_RECORD and r.get("version_id") in ids]

    def marked(mark: dict | None) -> int | None:
        return None if mark is None else _latest(ids, lambda vid: vid == mark["version_id"])

    start = None
    if saved is not None:
        start = marked(next((m for m in reversed(marks) if m.get("revision") == saved), None))
    if start is None and head is not None:
        start = _latest(shown, lambda r: _value_revision(r.get("value")) == head)
    if start is None and marks:
        start = marked(marks[-1])
    if start is None and raw:
        start = len(raw) - 1
    line = _lineage(
        [
            ((r.get("metadata") or {}).get("revision"), (r.get("metadata") or {}).get("base"))
            for r in raw
        ],
        start,
    )
    kept = {id(raw[k]): shown[k] for k in line}
    parents = {
        r.get("run_id"): r.get("parent_run_id") for r in records if r.get("type") == "RUN_STARTED"
    }
    runs: set[str] = set()
    for r in kept.values():
        run_id = r.get("run_id")
        while run_id is not None and run_id not in runs:
            runs.add(run_id)
            run_id = parents.get(run_id)
    out = []
    for record in records:
        kind = record.get("type")
        if kind == "ARTIFACT_PUBLISHED" and id(record) in kept:
            out.append(kept[id(record)])
        elif kind in ("RUN_STARTED", "RUN_FINISHED", "RUN_ERROR") and record.get("run_id") in runs:
            out.append(record)
    return out


class HistoryBusy(RuntimeError):
    """Undo, redo or restoring a version was refused because an agent's request is running."""


class EditHistory:
    """The versions of the dashboards an editor has open, and undo and redo over them.

    Created by :class:`~panel_flowdash.editor.FlowDash`, which calls
    :meth:`record` after every edit and :meth:`opened` whenever another
    dashboard (or a new one) is shown. Use the editor's
    :meth:`~panel_flowdash.editor.FlowDash.undo` and
    :meth:`~panel_flowdash.editor.FlowDash.redo` rather than this object;
    :attr:`state` is the ``RunState`` holding the versions, to show them in
    a panel-agents ``ArtifactView`` or to share with an ``AgentRunner``.
    """

    def __init__(self, editor: FlowDash, *, clock: t.Callable[[], float] = time.monotonic):
        self.editor = editor
        self.state = RunState(max_events=MAX_EVENTS)
        self._clock = clock
        self._dashboard_id: str | None = None
        self._baseline: tuple[str, str] | None = None
        self._head: str | None = None
        self._saved: str | None = None
        self._opened_revision: int | None = None
        self._pending: _Pending | None = None
        self._applying = False
        self._loaded: set[str] = set()
        self._recorders: dict[str, HistoryRecorder] = {}
        # Per dashboard, how many versions its stored history holds and about how many bytes.
        self._stored: dict[str, tuple[int, int]] = {}
        self._unheaded: dict[str, bool] = {}
        self._timer = False
        self._requests: dict[tuple[str, str | None], _Request] = {}
        self._edit_seq = 0
        # The order of the latest edit a published request version holds.
        self._published_seq = 0
        self._refused: dict[str, str] = {}
        #: Called after :meth:`load`, e.g. to show :attr:`refused`.
        self.on_load: t.Callable[[], None] | None = None
        #: Whether the versions are on screen, e.g. in the assistant's drawer,
        #: so a dashboard opened meanwhile loads its stored history at once.
        self.shown = False
        # Ahead of any recorder, so a request's version is stored before its run ends.
        self.state.subscribe(self._on_event)
        # Other states whose runs edit the dashboard, e.g. the Explore view's, see follow().
        self._followed: list[RunState] = []

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether edits are recorded: history is on and the user may edit the dashboard."""
        return bool(self.editor.history) and not self.editor.read_only

    @property
    def artifact_id(self) -> str:
        """The id of the open dashboard's document artifact in :attr:`state`."""
        return artifact_id_for(self._dashboard_id)

    @property
    def artifact(self) -> Artifact | None:
        """The open dashboard's document artifact, once it has a version."""
        return self.state.artifacts.artifacts.get(self.artifact_id)

    @property
    def versions(self) -> list[ArtifactVersion]:
        """The open dashboard's versions, oldest first, including the edit in progress."""
        self.flush()
        self._ensure_loaded()
        artifact = self.artifact
        return list(artifact.versions) if artifact is not None else []

    @property
    def refused(self) -> str | None:
        """Why the open dashboard's stored history is not shown, if it was loaded and is not."""
        return self._refused.get(self.artifact_id)

    def follow(self, state: RunState):
        """Treat the runs of another agent's *state* like those in :attr:`state`.

        For an agent whose runs are shown apart from the history's, such as
        the Explore view's: undo and redo wait while one of its runs is
        going, and each request's edits become a version when its run ends.
        """
        if state is not self.state and state not in self._followed:
            self._followed.append(state)
            state.subscribe(self._on_event)

    @property
    def busy(self) -> bool:
        """Whether a request to an agent is running, during which undo and redo wait."""
        return self.state.running or any(state.running for state in self._followed)

    @property
    def can_undo(self) -> bool:
        """Whether :meth:`undo` would undo something now."""
        if not self.active or self.busy:
            return False
        if self._requests:
            return True
        value = self._undo_value()
        return value is not None and self.editor._restore_refusal(value) is None

    @property
    def can_redo(self) -> bool:
        """Whether :meth:`redo` would redo something now."""
        if not self.active or self.busy or self._pending is not None:
            return False
        self._ensure_loaded()
        artifact = self.artifact
        if artifact is None or self._baseline_differs(artifact):
            return False
        number = artifact.redo_run_version
        if number is None:
            return False
        return self.editor._restore_refusal(artifact.versions[number - 1].value) is None

    @property
    def undo_label(self) -> str:
        """What :meth:`undo` would take back, e.g. ``"Added kpi"``, for menus."""
        if self._pending is not None:
            return self._pending.metadata.get("label") or ""
        artifact = self.artifact
        applied = artifact.applied_version if artifact is not None else None
        return _describe(applied)

    @property
    def redo_label(self) -> str:
        """What :meth:`redo` would bring back, for menus."""
        artifact = self.artifact
        number = artifact.redo_run_version if artifact is not None else None
        return _describe(artifact.versions[number - 1]) if number else ""

    def step_value(self, action: str) -> str | None:
        """Return the version :meth:`undo` (*action* ``"undo"``) or :meth:`redo` would apply, if any."""
        if action == "undo":
            return self._undo_value()
        artifact = self.artifact
        number = artifact.redo_run_version if artifact is not None else None
        return artifact.versions[number - 1].value if number else None

    def _undo_value(self, whole_runs: bool = True) -> str | None:
        """Return the version :meth:`undo` would apply, without publishing anything."""
        self._ensure_loaded()
        artifact = self.artifact
        if artifact is None:
            return None
        applied = artifact.applied_version
        if self._pending is not None or self._baseline_differs(artifact):
            return applied.value if applied is not None else None
        number = (self._request_start(artifact) if whole_runs else None) or artifact.undo_version
        return artifact.versions[number - 1].value if number else None

    def _baseline_differs(self, artifact: Artifact) -> bool:
        if self._baseline is None:
            return False
        applied = artifact.applied_version
        return applied is not None and _value_revision(applied.value) != self._baseline[1]

    def snapshot(self) -> tuple[str, str]:
        """Return the dashboard YAML as the editor's user sees it, and its revision.

        The text starts with the ``# revision:`` line ``DashboardOps.get_dashboard`` writes.
        """
        from panel_flowdash.agent.yaml_format import dashboard_to_yaml

        text = dashboard_to_yaml(self.editor)
        revision = revision_of(text)
        return f"# revision: {revision}\n{text}", revision

    def _user(self) -> str | None:
        return self.editor.user or pn.state.user

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def opened(self):
        """Start recording the dashboard now in the editor, as it is now.

        The stored history is loaded at the first edit, undo or look at the
        versions, so opening a dashboard to view it costs one YAML dump. The
        dashboard as opened becomes a version just before the first edit or
        undo, unless the stored history ends with it.
        """
        self.flush()
        current = self.editor.dashboard
        self._dashboard_id = current.dashboard_id if current is not None else None
        self._baseline = None
        self._head = self._saved = None
        self._opened_revision = current.revision if current is not None else None
        if self.active:
            self._baseline = self.snapshot()
            self._head = self._saved = self._baseline[1]
        if self.shown:
            self.load()

    def load(self):
        """Load the open dashboard's stored history now, to show it before any edit.

        Loading writes nothing to the store: the dashboard as opened becomes
        a version at the first edit, undo or restore, as without loading. In
        view mode retention is applied to what is shown, not to the store.
        """
        if not self.active:
            return
        self.flush()
        self._ensure_loaded()
        if self.on_load is not None:
            self.on_load()

    def saved(self):
        """Note that the canvas as it is now was saved, under the editor's dashboard id.

        When the canvas had no dashboard before, its versions so far move to
        the saved dashboard's history, so they are stored and undo keeps them.
        """
        self.flush()
        current = self.editor.dashboard
        dashboard_id = current.dashboard_id if current is not None else None
        if dashboard_id != self._dashboard_id:
            self._move_to(dashboard_id)
        self._close_requests()
        if self.active and self.artifact is not None:
            # Saving can rename the dashboard, which its YAML holds.
            value, revision = self.snapshot()
            if revision != self._head:
                self._publish(
                    value,
                    {
                        "op": "save",
                        "args": {},
                        "label": "Saved the dashboard",
                        "turn": None,
                        "base": self._head,
                        "revision": revision,
                        "version_id": uuid.uuid4().hex,
                    },
                    title=current.title if current is not None else None,
                    actor="user",
                    user=self._user(),
                    run_id=None,
                    tool_call_id=None,
                )
                self._head = revision
        self._saved = self._head
        self._mark_saved()

    def _mark_saved(self):
        """Note in the stored history which version the store now holds, at which revision."""
        artifact, store, current = self.artifact, self.editor.store, self.editor.dashboard
        if (
            not self.active
            or artifact is None
            or artifact.applied_version is None
            or store is None
            or current is None
            or self.artifact_id not in self._recorders
        ):
            return
        self._append(
            current.dashboard_id,
            {
                "type": SAVED_RECORD,
                "version_id": artifact.applied_version.metadata.get("version_id"),
                "revision": current.revision,
                "timestamp": time.time(),
            },
        )

    def _move_to(self, dashboard_id: str | None):
        old = self.artifact
        self._dashboard_id = dashboard_id
        self._baseline = None
        if old is None or not self.active:
            return
        self._ensure_loaded()
        if self.artifact is not None or old.applied_version is None:
            return
        old_id = old.artifact_id
        line = _lineage(
            [(v.metadata.get("revision"), v.metadata.get("base")) for v in old.versions],
            old.applied - 1,
        )
        current = self.editor.dashboard
        for k in line:
            version = old.versions[k]
            self._publish(
                version.value,
                dict(version.metadata),
                title=current.title if current is not None else None,
                actor=version.actor,
                user=version.user,
                run_id=version.run_id,
                tool_call_id=version.tool_call_id,
            )
        logger.debug("Moved %d versions from %s to %s", len(line), old_id, self.artifact_id)

    def record(
        self, op: str, args: dict[str, t.Any], label: str | None, turn: DashboardTurn | None
    ):
        """Record an edit the editor just made, as a new version or as part of the gesture in progress.

        Parameters
        ----------
        op
            The operation, e.g. ``"add_node"`` or ``"set_config"``.
        args
            What it was applied to, e.g. ``{"name": "kpi"}``. Never config
            values or data, which the version's YAML holds anyway.
        label
            A sentence for people, e.g. ``"Added kpi"``.
        turn
            The turn the edit belongs to.
        """
        if not self.active or self._applying:
            return
        agent = _agent_source()
        if agent is not None:
            self._record_request(agent.run, op, args, label, turn)
            return
        self._close_requests()
        value, revision = self.snapshot()
        actor = "user" if agent is None else "agent"
        run_id = current_run_id() if agent is None else agent.run
        tool_call_id = None if agent is None else current_tool_call_id()
        user = self._user() if agent is None else None
        turn_id = turn.id if turn is not None else None
        key = (actor, user, run_id, turn_id)
        now = self._clock()
        window = self.editor.history_coalesce or 0
        pending = self._pending
        if pending is not None and pending.key == key and now - pending.last <= window:
            pending.extend(value, revision, op, args, label, now)
            self._head = revision
            self._schedule()
            return
        self.flush()
        if revision == self._head:
            return
        self._ensure_base()
        current = self.editor.dashboard
        self._pending = _Pending(
            key=key,
            value=value,
            revision=revision,
            base=self._head,
            metadata={
                "op": op,
                "args": dict(args),
                "edits": 1,
                "ops": [{"op": op, "args": dict(args)}],
                "label": label,
                "turn": turn_id,
                "base": self._head,
                "revision": revision,
                "version_id": uuid.uuid4().hex,
            },
            title=current.title if current is not None else None,
            actor=actor,
            user=user,
            run_id=run_id,
            tool_call_id=tool_call_id,
            last=now,
            labels=[label] if label else [],
            counted=[{"op": op, "args": dict(args)}] if label else [],
        )
        self._head = revision
        if window > 0:
            self._schedule()
        else:
            self.flush()

    def _record_request(
        self,
        run_id: str | None,
        op: str,
        args: dict[str, t.Any],
        label: str | None,
        turn: DashboardTurn | None,
    ):
        """Note an agent's edit in its request, whose edits become one version when it ends.

        A person's edit or a save in between publishes the request's edits so
        far as a version of their own, see :meth:`_close_requests`.
        """
        self.flush()
        turn_id = turn.id if turn is not None else None
        key = (self.artifact_id, turn_id or run_id)
        request = self._requests.get(key)
        if request is None:
            self._ensure_base()
            artifact = self.artifact
            applied = artifact.applied_version if artifact is not None else None
            current = self.editor.dashboard
            request = self._requests[key] = _Request(
                artifact_id=self.artifact_id,
                dashboard_id=self._dashboard_id,
                run_id=run_id,
                turn_id=turn_id,
                start=applied.metadata.get("version_id") if applied is not None else None,
                title=current.title if current is not None else None,
                ops=[],
                tool_call_ids=[],
                labels=[],
            )
        request.ops.append({"op": op, "args": dict(args)})
        if label:
            request.counted.append({"op": op, "args": dict(args)})
            if label not in request.labels:
                request.labels.append(label)
        tool_call_id = current_tool_call_id()
        if tool_call_id and tool_call_id not in request.tool_call_ids:
            request.tool_call_ids.append(tool_call_id)
        request.value, request.revision = self.snapshot()
        self._edit_seq += 1
        request.seq = self._edit_seq
        self._head = request.revision

    def _close_requests(self):
        """Publish the edits the open dashboard's requests made so far, keeping the requests open.

        Called before a person's edit and on a save, so the history keeps the
        agent's and the person's edits apart, each version with its real base.
        """
        for request in sorted(self._requests.values(), key=lambda r: r.seq):
            if request.artifact_id == self.artifact_id and request.ops:
                self._publish_request(request)

    def request_run(self) -> str | None:
        """Return the run of the open dashboard's one agent request still open, or None."""
        runs = {r.run_id for r in self._requests.values() if r.artifact_id == self.artifact_id}
        return next(iter(runs)) if len(runs) == 1 else None

    @property
    def requests_pending(self) -> bool:
        """Whether an agent request has edits not yet published as its version."""
        return bool(self._requests)

    def end_requests(self, run_id: str | None = None):
        """Publish the version of each agent request that ended: all of them, or those of *run_id*.

        The editor calls it when the agent's turns end, and the history when
        a run in its state finishes or fails, so a request is one version
        whether it finished, failed or was cancelled. The tiles the request
        added are laid out first, as part of its version.
        """
        self.editor._release_tile_layout(run_id)
        for key, request in list(self._requests.items()):
            if run_id is None or request.run_id == run_id:
                del self._requests[key]
                if request.ops:
                    self._publish_request(request)

    def _on_event(self, event):
        if isinstance(event, (RunFinished, RunError)) and self._requests:
            self.end_requests(event.run_id)

    def _publish_request(self, request: _Request):
        if request.artifact_id != self.artifact_id:
            # The agent moved on to another dashboard; the canvas no longer shows this one.
            logger.debug(
                "Dropping the edits of a request on %s, which is not open", request.artifact_id
            )
            return
        self.flush()
        value, revision, ops, labels = request.value, request.revision, request.ops, request.labels
        tool_call_ids, counted = request.tool_call_ids, request.counted
        request.ops, request.tool_call_ids, request.labels, request.counted = [], [], [], []
        if ops and request.seq < self._published_seq:
            # Two agents' requests ran at once and the other's version, published since,
            # already holds these edits: publishing this older canvas would undo its later ones.
            return
        self._published_seq = max(self._published_seq, request.seq)
        artifact = self.artifact
        applied = artifact.applied_version if artifact is not None else None
        base = _value_revision(applied.value) if applied is not None else None
        if value is None or revision == base:
            return
        self._publish(
            value,
            {
                "op": "request",
                "args": {},
                "edits": len(ops),
                "ops": ops[:MAX_BATCH_OPS],
                **describe_edits(counted, labels),
                "turn": request.turn_id,
                "start": request.start,
                "tool_call_ids": tool_call_ids,
                "base": base,
                "revision": revision,
                "version_id": uuid.uuid4().hex,
            },
            title=request.title,
            actor="agent",
            user=None,
            run_id=request.run_id,
            tool_call_id=None,
        )

    def flush(self):
        """Publish the edit in progress, if any, as a version."""
        pending, self._pending = self._pending, None
        if pending is None or pending.revision == pending.base:
            return
        self._publish(
            pending.value,
            pending.metadata,
            title=pending.title,
            actor=pending.actor,
            user=pending.user,
            run_id=pending.run_id,
            tool_call_id=pending.tool_call_id,
        )
        self._bound()

    def _bound(self):
        """Keep the values of at most about ``history_limit`` versions of the open dashboard in memory.

        Past the limit and a margin, older versions keep their number and
        author but lose their value, so open views stay in step.
        """
        limit, artifact = self.editor.history_limit, self.artifact
        if not limit or artifact is None or self.busy:
            return
        kept = sum(not v.dropped for v in artifact.versions)
        if kept > limit + max(limit // 10, 10):
            self.state.drop_artifact_versions(
                self.artifact_id, before=len(artifact.versions) - limit + 1
            )

    def _publish(self, value: str, metadata: dict[str, t.Any], *, title: str | None, **fields):
        # The event is built here rather than with `publish_artifact`, which
        # would fill fields left empty from the run the flushing code is in.
        self.state.apply(
            ArtifactPublished(
                artifact_id=self.artifact_id,
                value=value,
                kind=ARTIFACT_KIND,
                title=title,
                metadata=metadata,
                applied=True,
                **fields,
            )
        )

    def _ensure_base(self):
        """Publish the dashboard as it was opened, unless the latest version already holds it."""
        self._ensure_loaded()
        baseline, self._baseline = self._baseline, None
        if baseline is None:
            return
        value, revision = baseline
        artifact = self.artifact
        applied = artifact.applied_version if artifact is not None else None
        base = _value_revision(applied.value) if applied is not None else None
        if base == revision:
            return
        current = self.editor.dashboard
        self._publish(
            value,
            {
                "op": "open",
                "args": {},
                "label": "Opened the dashboard",
                "turn": None,
                "base": base,
                "revision": revision,
                "version_id": uuid.uuid4().hex,
            },
            title=current.title if current is not None else None,
            actor="user",
            user=self._user(),
            run_id=None,
            tool_call_id=None,
        )

    def _schedule(self):
        """Publish the gesture in progress once it pauses, in a served session."""
        doc = pn.state.curdoc
        if self._timer or doc is None or doc.session_context is None or self._pending is None:
            return
        delay = max(self._pending.last + self.editor.history_coalesce - self._clock(), 0)
        self._timer = True
        # Weakly, so a pending timer does not keep a discarded editor alive.
        history = weakref.ref(self)

        def fire():
            if (alive := history()) is not None:
                alive._on_timer(doc)

        doc.add_timeout_callback(fire, int(delay * 1000) + 20)

    def _on_timer(self, doc):
        from panel.io.state import set_curdoc

        self._timer = False
        pending = self._pending
        if pending is None:
            return
        with set_curdoc(doc):
            if self._clock() - pending.last < self.editor.history_coalesce:
                self._schedule()
            else:
                self.flush()

    # ------------------------------------------------------------------
    # Undo and redo
    # ------------------------------------------------------------------

    def undo(self, *, whole_runs: bool = True) -> bool:
        """Undo the last change, or the agent's whole last request. See :meth:`FlowDash.undo`."""
        if not self.active:
            return False
        self._refuse_while_busy()
        self.flush()
        self._ensure_base()
        artifact = self.artifact
        if artifact is None:
            return False
        user = self._user()
        number = self._request_start(artifact) if whole_runs else None
        if number is not None:
            version = artifact.versions[number - 1]
            self._on_apply(artifact, version)
            self.state.apply_artifact_version(self.artifact_id, number, user=user, action="undo")
            return True
        return self.state.undo(self.artifact_id, user=user, on_apply=self._on_apply) is not None

    def _request_start(self, artifact: Artifact) -> int | None:
        """Return the version applied before the agent request that made the applied one, if it did.

        Undoing a request goes back there in one step, also past the
        versions a person made while the request ran.
        """
        applied = artifact.applied_version
        if applied is None or applied.actor != "agent" or not applied.metadata.get("start"):
            return None
        start = applied.metadata["start"]
        number = next(
            (
                v.version
                for v in reversed(artifact.versions[: applied.version - 1])
                if v.metadata.get("version_id") == start and not getattr(v, "dropped", False)
            ),
            None,
        )
        if number is None:
            return None
        between = artifact.versions[number : applied.version - 1]
        # With a person's edit in between, undo steps back one version, so it keeps theirs.
        if any(v.actor != "agent" or v.run_id != applied.run_id for v in between):
            return None
        return number

    def redo(self) -> bool:
        """Redo what the last undo undid, in one step. See :meth:`FlowDash.redo`."""
        if not self.active:
            return False
        self._refuse_while_busy()
        self.flush()
        self._ensure_base()
        if self.artifact is None:
            return False
        return (
            self.state.redo_run(self.artifact_id, user=self._user(), on_apply=self._on_apply)
            is not None
        )

    def restore(self, artifact: Artifact, version: ArtifactVersion) -> bool:
        """Make the canvas match *version*, for an ``ArtifactView``'s ``on_apply``.

        The view records the step itself. Raises ``PermissionError`` if the
        version holds nodes the user may not bring back.
        """
        if not self.active:
            raise PermissionError("You may only view this dashboard, so it cannot be changed.")
        self._refuse_while_busy()
        self.flush()
        self._ensure_base()
        return self._on_apply(artifact, version)

    def _refuse_while_busy(self):
        if not self.busy:
            # Requests made outside a run of the state end when someone steps through the history.
            self.end_requests()
            return
        if self.busy:
            raise HistoryBusy(
                "The assistant is still working on a request. Wait for it to finish, or "
                "stop it, before undoing or going back to another version."
            )

    def _on_apply(self, artifact: Artifact, version: ArtifactVersion) -> bool:
        if artifact.artifact_id != self.artifact_id:
            raise ValueError("That version belongs to a dashboard that is not open.")
        if _splits_a_request(artifact, version):
            raise PermissionError(
                "Someone else edited while the assistant worked on this request, so going back "
                "past part of it would also take back their edit. Step back with Undo instead."
            )
        self._applying = True
        try:
            self.editor._restore_version(version.value)
        finally:
            self._applying = False
        self._head = self.snapshot()[1]
        if self._head == self._saved:
            self.editor.dirty = False
        else:
            self.editor._mark_dirty()
        return True

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _ensure_loaded(self):
        """Load the open dashboard's stored versions and start writing new ones to the store."""
        artifact_id = self.artifact_id
        if artifact_id in self._loaded:
            return
        self._loaded.add(artifact_id)
        store, dashboard_id = self.editor.store, self._dashboard_id
        if store is None or dashboard_id is None:
            return
        try:
            kept = self._retain(dashboard_id)
            if (version := format_version(kept)) > FORMAT_VERSION:
                logger.warning(
                    "The edit history of dashboard %r has format version %s; this FlowDash "
                    "reads up to version %s, so it neither shows nor extends it.",
                    dashboard_id,
                    version,
                    FORMAT_VERSION,
                )
                self._refused[artifact_id] = NEWER_FORMAT
                return
            if artifact_id not in self.state.artifacts:
                head = self._baseline[1] if self._baseline is not None else self._head
                load_history(
                    decode(
                        kept,
                        artifact_id,
                        head,
                        partial(self._redact, restricts=self._restricts_any()),
                        self._opened_revision,
                    ),
                    self.state,
                )
                self._adopt_saved_version(kept, artifact_id)
        except Exception:
            logger.exception("Could not load the edit history of dashboard %r", dashboard_id)
            kept = []
        else:
            # A history stored before the header existed stays without one: it reads as version 1.
            self._unheaded[dashboard_id] = not kept
        self._count_stored(dashboard_id, kept)
        self._recorders[artifact_id] = HistoryRecorder(
            self.state, partial(self._write, dashboard_id, artifact_id), artifact_ids=[artifact_id]
        )

    def _adopt_saved_version(self, records: list[dict], artifact_id: str):
        """Take the loaded version that the opened dashboard was saved as for the canvas.

        Its YAML can read differently from the canvas's, e.g. after an
        upgrade added a setting, but it is the same dashboard, so it is not
        published again and the first undo goes to the version before it.
        """
        artifact = self.state.artifacts.artifacts.get(artifact_id)
        applied = artifact.applied_version if artifact is not None else None
        if applied is None or self._opened_revision is None:
            return
        vid = applied.metadata.get("version_id")
        if any(
            r.get("type") == SAVED_RECORD
            and r.get("version_id") == vid
            and r.get("revision") == self._opened_revision
            for r in records
        ):
            self._baseline = None

    def _retain(self, dashboard_id: str) -> list[dict]:
        """Drop what retention no longer keeps from the stored history, and return the rest."""
        editor = self.editor

        def retain(records):
            return compact(
                records, editor.history_limit, max_age, max_bytes=editor.history_max_bytes
            )

        max_age = editor.history_max_days * 86400 if editor.history_max_days else None
        if not editor.editable:
            # Viewing a dashboard never writes to its store.
            return retain(editor.store.load_history(dashboard_id))
        return editor.store.rewrite_history(dashboard_id, retain)

    def _append(self, dashboard_id: str, record: dict[str, t.Any]):
        store = self.editor.store
        if self._unheaded.pop(dashboard_id, False):
            store.append_history(dashboard_id, {HEADER_KEY: FORMAT_VERSION})
        store.append_history(dashboard_id, record)

    def _write(self, dashboard_id: str, artifact_id: str, record: dict[str, t.Any]):
        """Store *record* in the dashboard's history; the sink of the dashboard's recorder."""
        kind = record.get("type")
        if kind is None:
            return
        if kind == "ARTIFACT_APPLIED":
            artifact = self.state.artifacts.artifacts.get(artifact_id)
            number = record.get("version")
            if artifact is None or not isinstance(number, int) or number > len(artifact.versions):
                return
            record["version_id"] = artifact.versions[number - 1].metadata.get("version_id")
        self._append(dashboard_id, record)
        if kind != "ARTIFACT_PUBLISHED":
            return
        count, size = self._stored.get(dashboard_id, (0, 0))
        count, size = self._stored[dashboard_id] = (count + 1, size + _size(record))
        limit, max_bytes = self.editor.history_limit, self.editor.history_max_bytes
        if (limit and count > limit + max(limit // 10, 10)) or (
            max_bytes is not None and size > max_bytes + max_bytes // 10
        ):
            self._count_stored(dashboard_id, self._retain(dashboard_id))

    def _count_stored(self, dashboard_id: str, records: list[dict[str, t.Any]]):
        published = [r for r in records if r.get("type") == "ARTIFACT_PUBLISHED"]
        self._stored[dashboard_id] = (len(published), sum(map(_size, published)))

    def _redact(self, record: dict[str, t.Any], restricts: bool | None = None) -> dict[str, t.Any]:
        """Return a stored version as the editor's user may see it.

        A version stored by someone who may use a component this user may not
        holds that node's config and ports; they are replaced by the stand-in
        ``DashboardOps.get_dashboard`` writes for this user.
        """
        value = record.get("value")
        if not isinstance(value, str):
            return record
        if not self._hides_any(value):
            if not (self._restricts_any() if restricts is None else restricts):
                return record
            # A node the edits added and removed again is not in the version, but may be one
            # this user may not use, so its ports and config keys are left out of the edits.
            metadata = _redact_metadata(record.get("metadata") or {}, _node_names(value))
            return {**record, "metadata": metadata}
        from panel_flowdash.agent.yaml_format import (
            dashboard_from_dict,
            dashboard_to_yaml,
            parse_yaml,
        )
        from panel_flowdash.dashboard_store import DashboardModel
        from panel_flowdash.editor import export_model

        editor = self.editor
        try:
            data = dashboard_from_dict(parse_yaml(value))
            model = DashboardModel.from_dict(
                {
                    **data,
                    "dashboard_id": self._dashboard_id or "",
                    "user_id": "",
                    "title": data.get("title") or "",
                }
            )
        except Exception:
            # A version this user's editor cannot read is shown to nobody.
            return {**record, "value": "", "metadata": {}}
        restricted = editor._restricted_ids(model)
        hidden = [e.to_dict() for e in model.edges if {e.source, e.target} & restricted]
        text = dashboard_to_yaml(export_model(model, restricted=restricted), hidden)
        revision = revision_of(text)
        # A node a batch removed again is not in the version, so it is hidden like a restricted one.
        known = {item.instance_id for item in model.items} - restricted
        metadata = _redact_metadata(record.get("metadata") or {}, known)
        metadata["revision"] = revision
        return {**record, "value": f"# revision: {revision}\n{text}", "metadata": metadata}

    def _restricts_any(self) -> bool:
        """Whether the editor's user may not use some component, so stored edits may name what they may not see."""
        editor = self.editor
        return any(
            not editor._component_authorized(component) for component in editor._component_entries
        )

    def _hides_any(self, text: str) -> bool:
        editor = self.editor
        for match in _COMPONENT_LINE.finditer(text):
            try:
                component = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                return True
            if not isinstance(component, str):
                continue
            resolved = editor._resolve_component_id(component)
            if resolved in editor._component_entries and not editor._component_authorized(
                resolved
            ):
                return True
        return False

    def close(self):
        """Publish the edit in progress and stop writing to the store."""
        self.flush()
        for recorder in self._recorders.values():
            recorder.close()
        self._recorders.clear()


def _splits_a_request(artifact: Artifact, target: ArtifactVersion) -> bool:
    """Whether going back to *target* lands inside an agent request that someone else's edit split.

    That is a request with versions both up to *target* and after it, with
    another actor's version among those after *target*, so the canvas would
    keep part of the request but lose the other actor's edit made during
    it. Going back to before the whole request, or across whole requests
    and bursts, is a deliberate revert and allowed; so is going forward.
    """
    applied = artifact.applied
    if applied is None or target.version >= applied:
        return False
    after = [(v.actor, v.run_id) for v in artifact.versions[target.version : applied]]
    for version in artifact.versions[: target.version]:
        key = (version.actor, version.run_id)
        if version.actor != "agent" or version.run_id is None or key not in after:
            continue
        last = len(after) - 1 - after[::-1].index(key)
        if any(other != key for other in after[:last]):
            return True
    return False


def components_of(text: t.Any) -> set[str]:
    """Return the component ids a version's YAML places, e.g. to import them before restoring it."""
    try:
        doc = yaml.safe_load(text) if isinstance(text, str) else None
    except yaml.YAMLError:
        return set()
    nodes = doc.get("nodes") if isinstance(doc, dict) else None
    if not isinstance(nodes, dict):
        return set()
    return {
        node["component"]
        for node in nodes.values()
        if isinstance(node, dict) and isinstance(node.get("component"), str)
    }


def _describe(version: ArtifactVersion | None) -> str:
    if version is None:
        return ""
    label = version.metadata.get("label") or ""
    if version.actor == "agent":
        return f"The assistant's request: {label}" if label else "The assistant's request"
    return label


_NODES_SECTION = re.compile(r"^nodes:\n((?:[ \t].*\n?|\n)*)", re.MULTILINE)
_NODE_KEY = re.compile(r"^  ([A-Za-z_][\w-]*):", re.MULTILINE)


def _node_names(text: str) -> set[str]:
    """Return the names of the nodes in a version's YAML, without parsing it."""
    match = _NODES_SECTION.search(text)
    return set(_NODE_KEY.findall(match.group(1))) if match else set()


def _redact_metadata(metadata: dict[str, t.Any], known: set[str]) -> dict[str, t.Any]:
    """Leave the ports and config keys of nodes not in *known* out of a version's edits."""
    metadata = dict(metadata)
    metadata["args"] = _redact_args(metadata.get("args") or {}, known)
    if isinstance(metadata.get("ops"), list):
        metadata["ops"] = [
            {**op, "args": _redact_args(op.get("args") or {}, known)}
            for op in metadata["ops"]
            if isinstance(op, dict)
        ]
    return metadata


def _redact_args(args: dict[str, t.Any], known: set[str]) -> dict[str, t.Any]:
    """Leave the ports and config keys of nodes not in *known* out of an edit's arguments."""
    args = dict(args)
    for key in ("source", "target"):
        ref = args.get(key)
        if isinstance(ref, str) and ref.partition(".")[0] not in known:
            args[key] = ref.partition(".")[0]
    if args.get("name") not in known:
        args.pop("keys", None)
    return args


#: How many of an edit's labels its history label lists before it counts them instead.
LISTED_LABELS = 3

#: The verb and noun an edit operation is counted under, e.g. "Added 8 components".
_OP_COUNTS = {
    "add_node": ("Added", "component"),
    "connect": ("Added", "connection"),
    "remove_node": ("Removed", "component"),
    "disconnect": ("Removed", "connection"),
    "set_config": ("Configured", "component"),
    "set_input": ("Configured", "component"),
    "set_transform": ("Changed", "transform"),
}


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' * (n != 1)}"


def describe_edits(
    ops: t.Sequence[dict[str, t.Any]], labels: t.Sequence[str]
) -> dict[str, str | None]:
    """Return the ``label`` of a version made of several edits, and the ``detail`` listing them.

    A few edits are listed in the label, e.g. ``"Added a. Connected a to b."``;
    more are counted, e.g. ``"Added 8 components and 7 connections"``, with
    every label in the detail. *ops* are the labelled edits, so bookkeeping
    such as arranging the layout at the end of a request is not counted.
    """
    if len(labels) <= LISTED_LABELS:
        return {"label": summarize(labels), "detail": None}
    counts: dict[str, dict[str, set]] = {}
    other = 0
    for op in ops:
        verb, noun = _OP_COUNTS.get(op.get("op"), (None, None))
        if verb is None:
            other += 1
            continue
        args = op.get("args") or {}
        key = args.get("name") or (args.get("source"), args.get("target"))
        counts.setdefault(verb, {}).setdefault(noun, set()).add(str(key))
    parts = [
        f"{verb if i == 0 else verb.lower()} "
        + " and ".join(_plural(len(keys), noun) for noun, keys in nouns.items())
        for i, (verb, nouns) in enumerate(counts.items())
    ]
    if other:
        parts.append(
            f"{'made' if parts else 'Made'} {_plural(other, 'other change' if parts else 'change')}"
        )
    label = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    detail = " ".join(text if text.endswith(".") else f"{text}." for text in labels)
    return {"label": label, "detail": detail}


def label_for(labels: t.Sequence[str]) -> str | None:
    """Return the history label for an edit's turn labels."""
    if not labels:
        return None
    return labels[0] if len(labels) == 1 else summarize(labels)


__all__ = [
    "ARTIFACT_KIND",
    "BURST_SECONDS",
    "MAX_EVENTS",
    "EditHistory",
    "artifact_id_for",
    "revision_of",
]
