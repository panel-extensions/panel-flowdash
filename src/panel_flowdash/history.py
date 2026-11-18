"""The edit history of a dashboard: a version for every edit, undo and redo, and its storage.

Every change made through the editor, by the person using it or by an agent,
becomes a version of the dashboard's document artifact in a panel-agents
``RunState``: the dashboard YAML as the editor's user sees it, with metadata
saying which operation made it. A gesture (dragging nodes, typing into a
config field, resizing tiles) becomes one version, not one per event.

The versions of the dashboard in the editor are written to its store with a
panel-agents ``HistoryRecorder`` as they are made, and loaded again when the
dashboard is next opened, so undo reaches edits of earlier sessions. The
store keeps a limited number of versions per dashboard and drops a
dashboard's history with the dashboard.

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
from dataclasses import dataclass
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
from panel_agents.events import ArtifactPublished

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
MAX_EVENTS = 2000

#: Operations that a quick succession of the same edit merges into one version.
COALESCED_OPS = frozenset({"move_nodes", "layout", "set_config", "settings", "set_transform"})

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


def _target(op: str, args: dict[str, t.Any]) -> t.Any:
    if op == "set_config":
        return args.get("name")
    if op == "set_transform":
        return (args.get("source"), args.get("target"))
    return None


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

    def extend(self, value: str, revision: str, args: dict[str, t.Any], now: float):
        self.value, self.revision, self.last = value, revision, now
        self.metadata["revision"] = revision
        keys = args.get("keys")
        if keys:
            merged = self.metadata["args"].setdefault("keys", [])
            merged.extend(k for k in keys if k not in merged)


def compact(
    records: list[dict[str, t.Any]],
    limit: int | None,
    max_age: float | None,
    now: float | None = None,
) -> list[dict[str, t.Any]]:
    """Return stored history *records* without the versions retention drops.

    The newest *limit* versions no older than *max_age* seconds are kept,
    with the applied records that name them and the runs they were made in.
    Returns *records* itself when nothing is dropped.
    """
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
        if kind == "ARTIFACT_PUBLISHED":
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
        self._stored: dict[str, int] = {}
        self._timer = False

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
    def busy(self) -> bool:
        """Whether a request to an agent is running, during which undo and redo wait."""
        return self.state.running

    @property
    def can_undo(self) -> bool:
        """Whether :meth:`undo` would undo something now."""
        if not self.active or self.busy:
            return False
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

    def _undo_value(self, whole_runs: bool = True) -> str | None:
        """Return the version :meth:`undo` would apply, without publishing anything."""
        self._ensure_loaded()
        artifact = self.artifact
        if artifact is None:
            return None
        applied = artifact.applied_version
        if self._pending is not None or self._baseline_differs(artifact):
            return applied.value if applied is not None else None
        number = None
        if whole_runs and applied is not None and applied.actor == "agent" and applied.run_id:
            try:
                number = artifact.undo_run_target(applied.run_id, self.state.runs)
            except ValueError:
                number = None
        number = number or artifact.undo_version
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
        store.append_history(
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
        value, revision = self.snapshot()
        agent = _agent_source()
        actor = "user" if agent is None else "agent"
        run_id = current_run_id() if agent is None else agent.run
        tool_call_id = None if agent is None else current_tool_call_id()
        user = self._user() if agent is None else None
        turn_id = turn.id if turn is not None else None
        key = (actor, user, run_id, tool_call_id, turn_id, op, _target(op, args))
        now = self._clock()
        window = self.editor.history_coalesce or 0
        pending = self._pending
        if (
            pending is not None
            and op in COALESCED_OPS
            and pending.key == key
            and now - pending.last <= window
        ):
            pending.extend(value, revision, args, now)
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
        )
        self._head = revision
        if op in COALESCED_OPS and window > 0:
            self._schedule()
        else:
            self.flush()

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
        doc.add_timeout_callback(partial(self._on_timer, doc), int(delay * 1000) + 20)

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
        applied = artifact.applied_version
        user = self._user()
        if whole_runs and applied is not None and applied.actor == "agent" and applied.run_id:
            try:
                target = artifact.undo_run_target(applied.run_id, self.state.runs)
            except ValueError:
                target = None
            if target is not None:
                return (
                    self.state.undo_run(
                        self.artifact_id, applied.run_id, user=user, on_apply=self._on_apply
                    )
                    is not None
                )
        return self.state.undo(self.artifact_id, user=user, on_apply=self._on_apply) is not None

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
        if self.busy:
            raise HistoryBusy(
                "The assistant is still working on a request. Wait for it to finish, or "
                "stop it, before undoing or going back to another version."
            )

    def _on_apply(self, artifact: Artifact, version: ArtifactVersion) -> bool:
        if artifact.artifact_id != self.artifact_id:
            raise ValueError("That version belongs to a dashboard that is not open.")
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
            if artifact_id not in self.state.artifacts:
                head = self._baseline[1] if self._baseline is not None else self._head
                load_history(
                    decode(kept, artifact_id, head, self._redact, self._opened_revision),
                    self.state,
                )
        except Exception:
            logger.exception("Could not load the edit history of dashboard %r", dashboard_id)
            kept = []
        self._stored[dashboard_id] = sum(r.get("type") == "ARTIFACT_PUBLISHED" for r in kept)
        self._recorders[artifact_id] = HistoryRecorder(
            self.state, partial(self._write, dashboard_id, artifact_id), artifact_ids=[artifact_id]
        )

    def _retain(self, dashboard_id: str) -> list[dict]:
        """Drop what retention no longer keeps from the stored history, and return the rest."""
        editor = self.editor
        max_age = editor.history_max_days * 86400 if editor.history_max_days else None
        return editor.store.rewrite_history(
            dashboard_id, lambda records: compact(records, editor.history_limit, max_age)
        )

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
        store = self.editor.store
        store.append_history(dashboard_id, record)
        if kind != "ARTIFACT_PUBLISHED":
            return
        count = self._stored[dashboard_id] = self._stored.get(dashboard_id, 0) + 1
        limit = self.editor.history_limit
        if limit and count > limit + max(limit // 10, 10):
            kept = self._retain(dashboard_id)
            self._stored[dashboard_id] = sum(r.get("type") == "ARTIFACT_PUBLISHED" for r in kept)

    def _redact(self, record: dict[str, t.Any]) -> dict[str, t.Any]:
        """Return a stored version as the editor's user may see it.

        A version stored by someone who may use a component this user may not
        holds that node's config and ports; they are replaced by the stand-in
        ``DashboardOps.get_dashboard`` writes for this user.
        """
        value = record.get("value")
        if not isinstance(value, str) or not self._hides_any(value):
            return record
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
        metadata = dict(record.get("metadata") or {})
        args = dict(metadata.get("args") or {})
        for key in ("source", "target"):
            ref = args.get(key)
            if isinstance(ref, str) and ref.partition(".")[0] in restricted:
                args[key] = ref.partition(".")[0]
        if args.get("name") in restricted:
            args.pop("keys", None)
        metadata.update(args=args, revision=revision)
        return {**record, "value": f"# revision: {revision}\n{text}", "metadata": metadata}

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


def label_for(labels: t.Sequence[str]) -> str | None:
    """Return the history label for an edit's turn labels."""
    if not labels:
        return None
    return labels[0] if len(labels) == 1 else summarize(labels)


__all__ = [
    "ARTIFACT_KIND",
    "COALESCED_OPS",
    "MAX_EVENTS",
    "EditHistory",
    "artifact_id_for",
    "revision_of",
]
