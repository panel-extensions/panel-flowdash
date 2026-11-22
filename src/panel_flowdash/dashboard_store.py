"""Persistence for dashboard graphs, backed by SQLite or an in-memory dict."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panel_flowdash.auth import Identity, Permission, can_administer, can_edit, is_authorized
from panel_flowdash.groups import DashboardGroup, decode_groups
from panel_flowdash.parameters import DashboardParameter, decode_parameters
from panel_flowdash.report import decode_report
from panel_flowdash.turns import DashboardTurn, decode_turns

logger = logging.getLogger(__name__)

# The ids `BaseDashboardStore.create_dashboard` makes.
_RANDOM_ID = re.compile(r"[0-9a-f]{12}")

# Titles already warned about as ambiguous, so a home page visit does not log each time.
_AMBIGUOUS_TITLES: set[str] = set()

# The version of :class:`DashboardModel` this release writes. Version 3 is the
# last released one; version 4 adds turns, node origins, parameters, groups and
# the report, which older models load without. Changes made before a release
# that ships them do not bump it: development builds wrote some version 4
# models as 5.
MODEL_VERSION = 4

# What decoding a stored dashboard raises when its data is malformed. Deeply
# nested JSON overflows the decoder with a RecursionError.
MALFORMED_ERRORS = (KeyError, TypeError, ValueError, AttributeError, RecursionError)


def decode_settings(value: Any) -> dict[str, Any]:
    """Return a private copy of stored dashboard settings, which may be missing (None).

    Raises TypeError unless they are a mapping with string keys. Settings are
    optional, so the stores catch this and drop just the settings, with a
    warning, rather than skipping the whole dashboard.
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"settings must be a mapping, not {type(value).__name__}")
    return copy.deepcopy(value)


class DashboardStoreError(Exception):
    """A store refused an operation for a reason the user should see.

    Raise it from a :class:`BaseDashboardStore` method to reject a request, for
    example a duplicate title, a name the backend cannot store or a missing
    permission. The app shows the message to the user as written and leaves
    its state unchanged, so phrase it for an end user. Any other exception is
    treated as a bug or an infrastructure failure and is logged with its
    traceback.
    """


class DashboardConflictError(DashboardStoreError):
    """A save was refused because the dashboard changed since it was loaded.

    Raised by :meth:`BaseDashboardStore.save_dashboard` when the stored
    revision differs from the revision the saved model was loaded at, that is
    when another session saved the dashboard in the meantime.

    Attributes
    ----------
    dashboard_id
        The dashboard that was being saved.
    expected
        The revision the rejected model was based on.
    current
        The revision now stored.
    """

    def __init__(self, dashboard_id: str, expected: int | None, current: int | None):
        self.dashboard_id = dashboard_id
        self.expected = expected
        self.current = current
        super().__init__("This dashboard was changed in another session since you opened it.")


def check_revision(dashboard: DashboardModel, stored: int | None) -> int:
    """Return the revision to store *dashboard* at, given the *stored* one.

    Raises :class:`DashboardConflictError` if *dashboard* was loaded at a
    revision other than the stored one. A model with no revision
    (``None``) is saved unconditionally, and *stored* is ``None`` when the
    dashboard is not stored (new, or deleted since it was loaded).
    """
    expected = dashboard.revision
    if expected is not None and stored is not None and stored != expected:
        raise DashboardConflictError(dashboard.dashboard_id, expected, stored)
    return max(stored or 0, expected or 0) + 1


@dataclass
class DashboardItem:
    """A component instance on the dashboard.

    x, y store the ReactFlow node canvas position.
    Grid layout (widths, heights, visibility) lives in DashboardModel.tile_layout.
    origin is the id of the turn that created the item, see DashboardModel.turns.
    inputs holds literal values of unconnected inputs, by port; a connection wins over them.
    """

    instance_id: str
    component_id: str
    x: float = 0
    y: float = 0
    config: dict[str, Any] = field(default_factory=dict)
    origin: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    preview_output: str | None = None
    collapsed: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "instance_id": self.instance_id,
            "component_id": self.component_id,
            "x": self.x,
            "y": self.y,
            "config": self.config,
        }
        if self.origin is not None:
            data["origin"] = self.origin
        if self.inputs:
            data["inputs"] = copy.deepcopy(self.inputs)
        if self.preview_output is not None:
            data["preview_output"] = self.preview_output
        if self.collapsed:
            data["collapsed"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardItem:
        origin = data.get("origin")
        inputs = data.get("inputs")
        if inputs is not None and (
            not isinstance(inputs, dict) or not all(isinstance(k, str) for k in inputs)
        ):
            raise TypeError(f"inputs must be a mapping, not {type(inputs).__name__}")
        preview_output = data.get("preview_output")
        collapsed = data.get("collapsed", False)
        return cls(
            instance_id=data["instance_id"],
            component_id=data["component_id"],
            x=data.get("x", 0),
            y=data.get("y", 0),
            config=data.get("config", {}),
            origin=origin if isinstance(origin, str) else None,
            inputs=copy.deepcopy(inputs or {}),
            preview_output=(
                preview_output if isinstance(preview_output, str) and preview_output else None
            ),
            collapsed=collapsed if isinstance(collapsed, bool) else False,
        )


@dataclass
class DashboardEdge:
    """A connection between two component ports, with the transform steps it applies."""

    source: str
    source_port: str
    target: str
    target_port: str
    transform: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "source_port": self.source_port,
            "target": self.target,
            "target_port": self.target_port,
        }
        if self.transform:
            data["transform"] = copy.deepcopy(self.transform)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardEdge:
        return cls(
            source=data["source"],
            source_port=data["source_port"],
            target=data["target"],
            target_port=data["target_port"],
            transform=copy.deepcopy(data.get("transform") or []),
        )


@dataclass
class DashboardModel:
    """A persisted dashboard: nodes + edges + tile layout, and the turns that made them."""

    dashboard_id: str
    user_id: str
    title: str
    version: int = MODEL_VERSION
    items: list[DashboardItem] = field(default_factory=list)
    edges: list[DashboardEdge] = field(default_factory=list)
    tile_layout: list[dict[str, Any]] = field(default_factory=list)
    breakpoints: list[int] = field(default_factory=list)
    responsive_layouts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Container width the tile layout was authored at; narrower screens derive from it.
    reference_width: int | None = None
    permission: Permission = field(default_factory=Permission)
    # Dashboard-wide values, keyed by the parameter names of the editor's settings object.
    settings: dict[str, Any] = field(default_factory=dict)
    # The requests to the agent and batches of manual edits that changed the
    # dashboard, oldest first; each item's `origin` names one of them.
    turns: list[DashboardTurn] = field(default_factory=list)
    # The config fields the dashboard can be run with, by name; each names an
    # item by instance id, see panel_flowdash.parameters.
    parameters: dict[str, DashboardParameter] = field(default_factory=dict)
    # Nodes framed together on the canvas, see panel_flowdash.groups.
    groups: list[DashboardGroup] = field(default_factory=list)
    # The dashboard read as a document, nodes by instance id, see
    # panel_flowdash.report; None reads as the report derived from the layout.
    report: dict[str, Any] | None = None
    # Bumped by the store on every save; None saves without a conflict check.
    revision: int | None = None
    # When the store last wrote the dashboard (UTC), filled in by the store on
    # load. Not part of the dashboard's content, so not serialized or compared.
    updated_at: datetime | None = field(default=None, compare=False)

    @property
    def owner(self) -> str:
        """The immutable owner principal (the creating user)."""
        return self.user_id

    def to_dict(self) -> dict[str, Any]:
        data = {
            "version": self.version,
            "dashboard_id": self.dashboard_id,
            "user_id": self.user_id,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
            "edges": [edge.to_dict() for edge in self.edges],
            "tile_layout": self.tile_layout,
            "breakpoints": self.breakpoints,
            "responsive_layouts": self.responsive_layouts,
            "reference_width": self.reference_width,
            "permission": self.permission.to_dict(),
            "settings": copy.deepcopy(self.settings),
            "turns": [turn.to_dict() for turn in self.turns],
            "parameters": {name: p.to_dict() for name, p in self.parameters.items()},
            "groups": [group.to_dict() for group in self.groups],
            "revision": self.revision,
        }
        # Left out without one, so dashboards without a report are written as before.
        if self.report is not None:
            data["report"] = copy.deepcopy(self.report)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardModel:
        # These are compared and hashed by every access check, so a wrong type
        # must fail here rather than in the middle of a listing.
        for key in ("dashboard_id", "user_id", "title"):
            if not isinstance(data[key], str):
                raise TypeError(f"{key} must be a string, not {type(data[key]).__name__}")
        return cls(
            dashboard_id=data["dashboard_id"],
            user_id=data["user_id"],
            title=data["title"],
            version=_migrated(data.get("version", 1)),
            items=[DashboardItem.from_dict(i) for i in data.get("items", [])],
            edges=[DashboardEdge.from_dict(e) for e in data.get("edges", [])],
            tile_layout=data.get("tile_layout", []),
            breakpoints=data.get("breakpoints", []),
            responsive_layouts=data.get("responsive_layouts", {}),
            reference_width=data.get("reference_width"),
            permission=Permission.from_dict(data.get("permission")),
            settings=decode_settings(data.get("settings")),
            turns=decode_turns(data.get("turns")),
            parameters=decode_parameters(data.get("parameters")),
            groups=decode_groups(data.get("groups")),
            report=decode_report(data.get("report")),
            revision=_decode_revision(data.get("revision")),
        )


def _migrated(version: Any) -> int:
    """Return the model version a stored one is read as: older ones are upgraded on load."""
    if isinstance(version, bool) or not isinstance(version, int) or version <= 5:
        return MODEL_VERSION
    return version


def _parse_sqlite_time(value: Any) -> datetime | None:
    """Parse a SQLite ``datetime('now')`` value, which is UTC without an offset."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _decode_revision(value: Any) -> int | None:
    """Return a stored revision, or None (no conflict check) if it is not an integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class BaseDashboardStore(ABC):
    """The persistence interface the editor and app shell depend on.

    Subclasses implement the six storage primitives below; the access-control
    and lookup helpers are backend-independent and inherited. Implement this to
    back dashboards with something other than SQLite.

    To refuse a create, save, rename, delete or permission change, raise
    :class:`DashboardStoreError` with a message for the user.

    Stores that detect concurrent edits keep a revision per dashboard: every
    save stores ``dashboard.revision + 1`` (see :func:`check_revision`), and a
    save based on an older revision than the stored one raises
    :class:`DashboardConflictError`. A store that does not support revisions
    simply ignores the field and saves last-writer-wins.

    Models returned by listings may be shared with a backend cache, so callers
    treat them as read-only; single-dashboard lookups return private copies.

    Set :attr:`random_ids` to False in a store whose ids can be guessed.
    """

    random_ids: bool = True
    """Whether dashboard ids are random, so only someone given a link knows one.

    The default :meth:`create_dashboard` makes random ids. A store whose ids
    can be guessed (derived from titles, say) sets this to False, and the app
    then answers a dashboard someone may not open exactly like a missing one,
    rather than saying it exists and who owns it. Even with random ids, an id
    a caller chose when saving (see :meth:`guessable_id`) is treated so."""

    def guessable_id(self, dashboard_id: str) -> bool:
        """Whether *dashboard_id* could be guessed rather than only learned from a link.

        Only ids of the form :meth:`create_dashboard` makes count as random in
        a store with :attr:`random_ids`. Any store saves whatever id a model
        carries, so one chosen by the caller, like the slug an example seeds
        its dashboards under, can be guessed. A store whose own ids look
        different overrides this.
        """
        return not self.random_ids or _RANDOM_ID.fullmatch(dashboard_id) is None

    @abstractmethod
    def save_dashboard(self, dashboard: DashboardModel) -> None:
        """Insert or update a dashboard.

        Stores that support revisions refuse a *dashboard* whose ``revision``
        is not the stored one with :class:`DashboardConflictError`, as one
        atomic check and write, and on success set ``dashboard.revision`` to
        the stored revision. Renaming a dashboard or changing its permission
        does not change its revision.
        """

    @abstractmethod
    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard by id regardless of owner (for access checks)."""

    @abstractmethod
    def _all_dashboards(self) -> list[DashboardModel]:
        """Every stored dashboard, most recently updated first."""

    @abstractmethod
    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        """Delete a dashboard owned by *user_id*. Returns whether one was removed."""

    @abstractmethod
    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        """Retitle a dashboard owned by *user_id*. Returns whether one was updated."""

    @abstractmethod
    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        """Persist a new permission set on a dashboard. Returns success."""

    # ------------------------------------------------------------------
    # Edit history
    # ------------------------------------------------------------------
    #
    # The editor keeps every version of a dashboard as a list of JSON records
    # (see :mod:`panel_flowdash.history`). The defaults keep none, so a store
    # without them still offers undo within a session. A store that keeps the
    # history drops it in ``delete_dashboard``.

    def load_history(self, dashboard_id: str) -> list[dict[str, Any]]:
        """Return the edit history records of a dashboard, oldest first."""
        return []

    def append_history(self, dashboard_id: str, record: dict[str, Any]) -> None:
        """Add a record to the end of a dashboard's edit history.

        Does nothing when the dashboard is not stored, e.g. because it was
        deleted while an editor still had it open.
        """
        return None

    def replace_history(self, dashboard_id: str, records: list[dict[str, Any]]) -> None:
        """Replace a dashboard's edit history, e.g. with what retention keeps of it."""
        return None

    def delete_history(self, dashboard_id: str) -> None:
        """Drop a dashboard's edit history."""
        return None

    def rewrite_history(
        self,
        dashboard_id: str,
        rewrite: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Replace a dashboard's history with what *rewrite* returns for it, and return that.

        *rewrite* returns its argument itself when nothing changes, which
        writes nothing. Stores used by several sessions at once override this
        to read and write in one transaction, so no record appended meanwhile
        is lost.
        """
        records = self.load_history(dashboard_id)
        kept = rewrite(records)
        if kept is not records:
            self.replace_history(dashboard_id, kept)
        return kept

    def load_dashboard(self, user_id: str, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard, but only if *user_id* owns it."""
        model = self._load_any(dashboard_id)
        if model is None or model.user_id != user_id:
            return None
        return model

    def list_dashboards(self, user_id: str) -> list[DashboardModel]:
        """Dashboards owned by *user_id*, most recently updated first."""
        return [m for m in self._all_dashboards() if m.user_id == user_id]

    def title_exists(self, user_id: str, title: str, exclude_id: str | None = None) -> bool:
        """Check if a dashboard with the given title already exists for this user."""
        return any(
            m.title == title and m.dashboard_id != exclude_id
            for m in self.list_dashboards(user_id)
        )

    def create_dashboard(self, user_id: str, title: str) -> DashboardModel:
        """Create, persist and return a new empty dashboard."""
        dashboard = DashboardModel(
            dashboard_id=uuid.uuid4().hex[:12],
            user_id=user_id,
            title=title,
        )
        self.save_dashboard(dashboard)
        return dashboard

    def list_accessible(
        self, identity: Identity, *, default_allow: bool = True
    ) -> list[DashboardModel]:
        """List dashboards the *identity* owns or has been granted access to.

        Owned dashboards sort first (both groups by recency), so a user's own
        dashboards stay visually grouped ahead of ones shared with them.
        """
        owned: list[DashboardModel] = []
        shared: list[DashboardModel] = []
        for model in self._all_dashboards():
            if model.owner in identity.user_names:
                owned.append(model)
            elif is_authorized(
                model.permission,
                identity,
                default_allow=default_allow,
                owner=model.owner,
            ):
                shared.append(model)
        return owned + shared

    def load_for_access(
        self, identity: Identity, dashboard_id: str, *, default_allow: bool = True
    ) -> DashboardModel | None:
        """Load a dashboard if *identity* is authorized, else ``None``.

        Returns ``None`` both when the dashboard does not exist and when access
        is denied, so callers render a single "not found / denied" view.
        """
        model = self._load_any(dashboard_id)
        if model is None:
            return None
        if is_authorized(
            model.permission,
            identity,
            default_allow=default_allow,
            owner=model.owner,
        ):
            return model
        return None

    def find_by_id_or_title(
        self, ref: str, identity: Identity | None = None, *, default_allow: bool = True
    ) -> DashboardModel | None:
        """Resolve a dashboard by its id first, then by title.

        Titles are only unique per user, so a title can match several
        dashboards. Without *identity* (trusted code, such as
        :meth:`FlowDash.load`) the most recently updated match is returned.

        With *identity*, as when resolving the operator-configured home
        dashboard, a dashboard the identity may not open is never returned.
        The identity's own dashboard with that title wins. Otherwise a title
        owned by a single user resolves to that user's most recently updated
        dashboard, while a title owned by several users is ambiguous and
        resolves to ``None`` (the app then shows the launcher) with an error
        logged asking for the dashboard id. Picking one of them instead would
        let any user take over everyone's home page by creating a dashboard
        with the same title.
        """
        model = self._load_any(ref)
        if model is not None:
            return model
        match = self._pick_titled(
            [m for m in self._all_dashboards() if m.title == ref], identity, default_allow
        )
        # Listings may hand out shared, cached models; return a private copy.
        return None if match is None else self._load_any(match.dashboard_id)

    @staticmethod
    def _pick_titled(
        matches: list[DashboardModel], identity: Identity | None, default_allow: bool
    ) -> DashboardModel | None:
        """Choose among same-titled dashboards, ordered most recently updated first."""
        if identity is None:
            return matches[0] if matches else None

        def allowed(model: DashboardModel) -> bool:
            return is_authorized(
                model.permission, identity, default_allow=default_allow, owner=model.owner
            )

        own = [m for m in matches if m.owner in identity.user_names]
        if own:
            return next((m for m in own if allowed(m)), None)
        if len({m.owner for m in matches}) > 1:
            title = matches[0].title
            if title not in _AMBIGUOUS_TITLES:
                _AMBIGUOUS_TITLES.add(title)
                logger.error(
                    "Several users own a dashboard titled %r, so it cannot be used as "
                    "the home dashboard; configure it by id instead.",
                    title,
                )
            return None
        return next((m for m in matches if allowed(m)), None)

    def get_owner(self, dashboard_id: str) -> str | None:
        """Return the owner (user_id) of a dashboard, or ``None`` if missing."""
        model = self._load_any(dashboard_id)
        return model.owner if model else None

    def can_administer(
        self, identity: Identity, dashboard_id: str, admin_groups: frozenset[str] = frozenset()
    ) -> bool:
        """Whether *identity* may administer (rename/delete/share) the dashboard.

        Administering includes editing, see :meth:`can_edit`.
        """
        model = self._load_any(dashboard_id)
        if model is None:
            return False
        return self.can_administer_model(identity, model, admin_groups)

    def can_administer_model(
        self,
        identity: Identity,
        dashboard: DashboardModel,
        admin_groups: frozenset[str] = frozenset(),
    ) -> bool:
        """Like :meth:`can_administer`, for a dashboard that is already loaded.

        Listings use it to avoid loading every dashboard a second time.
        Override this method, not :meth:`can_administer`, to change who may
        administer a dashboard.
        """
        return can_administer(identity, dashboard.owner, admin_groups)

    def can_edit(
        self, identity: Identity, dashboard_id: str, admin_groups: frozenset[str] = frozenset()
    ) -> bool:
        """Whether *identity* may edit and save the dashboard's contents and settings."""
        model = self._load_any(dashboard_id)
        if model is None:
            return False
        return self.can_edit_model(identity, model, admin_groups)

    def can_edit_model(
        self,
        identity: Identity,
        dashboard: DashboardModel,
        admin_groups: frozenset[str] = frozenset(),
    ) -> bool:
        """Like :meth:`can_edit`, for a dashboard that is already loaded.

        Administrators (see :meth:`can_administer_model`) may always edit;
        anyone else needs an ``allow_edit_users``/``allow_edit_groups`` grant
        on the dashboard's permission. Override this method, not
        :meth:`can_edit`, to change who may edit a dashboard.
        """
        if self.can_administer_model(identity, dashboard, admin_groups):
            return True
        return can_edit(dashboard.permission, identity, dashboard.owner, frozenset())


class MemoryDashboardStore(BaseDashboardStore):
    """Dict-backed store for notebooks, scripts and tests.

    Dashboards live for as long as the store does and are never written to disk.
    Models are deep-copied in and out so a caller mutating a dashboard it saved
    (or loaded) cannot retroactively change what is stored, matching how the
    SQLite store behaves.
    """

    def __init__(self, dashboards: dict[str, DashboardModel] | None = None):
        self._dashboards: dict[str, DashboardModel] = {}
        self._order: list[str] = []
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        for dashboard in (dashboards or {}).values():
            self.save_dashboard(copy.deepcopy(dashboard))

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        with self._lock:
            stored = self._dashboards.get(dashboard.dashboard_id)
            revision = check_revision(dashboard, None if stored is None else stored.revision)
            self._dashboards[dashboard.dashboard_id] = copy.deepcopy(dashboard)
            self._dashboards[dashboard.dashboard_id].revision = revision
            self._dashboards[dashboard.dashboard_id].updated_at = datetime.now(UTC)
            # Re-inserting moves the dashboard to the front of the recency order.
            if dashboard.dashboard_id in self._order:
                self._order.remove(dashboard.dashboard_id)
            self._order.insert(0, dashboard.dashboard_id)
        dashboard.revision = revision

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        model = self._dashboards.get(dashboard_id)
        return copy.deepcopy(model) if model is not None else None

    def _all_dashboards(self) -> list[DashboardModel]:
        return [copy.deepcopy(self._dashboards[did]) for did in self._order]

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        with self._lock:
            model = self._dashboards.get(dashboard_id)
            if model is None or model.user_id != user_id:
                return False
            del self._dashboards[dashboard_id]
            self._order.remove(dashboard_id)
            self._history.pop(dashboard_id, None)
        return True

    def load_history(self, dashboard_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._history.get(dashboard_id, []))

    def append_history(self, dashboard_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            if dashboard_id in self._dashboards:
                self._history.setdefault(dashboard_id, []).append(copy.deepcopy(record))

    def replace_history(self, dashboard_id: str, records: list[dict[str, Any]]) -> None:
        with self._lock:
            if dashboard_id in self._dashboards:
                self._history[dashboard_id] = copy.deepcopy(records)

    def delete_history(self, dashboard_id: str) -> None:
        with self._lock:
            self._history.pop(dashboard_id, None)

    def rewrite_history(self, dashboard_id, rewrite):
        with self._lock:
            records = copy.deepcopy(self._history.get(dashboard_id, []))
            kept = rewrite(records)
            if kept is not records and dashboard_id in self._dashboards:
                self._history[dashboard_id] = copy.deepcopy(kept)
        return kept

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        model = self._dashboards.get(dashboard_id)
        if model is None or model.user_id != user_id:
            return False
        model.title = new_title
        model.updated_at = datetime.now(UTC)
        return True

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        model = self._dashboards.get(dashboard_id)
        if model is None:
            return False
        model.permission = copy.deepcopy(permission)
        model.updated_at = datetime.now(UTC)
        return True


# Connections a forked child inherited from its parent. The child must not close
# them (SQLite forbids touching a connection across fork(), and closing the last
# one may checkpoint and delete the parent's WAL), and dropping the last reference
# would close them, so they are kept alive here for the life of the process.
_INHERITED: list[sqlite3.Connection] = []


@dataclass(frozen=True)
class _OpenConnection:
    pid: int
    thread: threading.Thread
    conn: sqlite3.Connection


def _close_all(connections: list[_OpenConnection], lock: threading.Lock) -> None:
    pid = os.getpid()
    with lock:
        for entry in connections:
            if entry.pid == pid:
                entry.conn.close()
            else:
                _INHERITED.append(entry.conn)
        connections.clear()


class DashboardStore(BaseDashboardStore):
    """SQLite-backed store for dashboard models.

    Each thread reuses one connection, so a ``_get_conn()`` block nested in
    another on the same thread (for example in a subclass) shares its
    transaction: the inner block's commit or rollback ends the outer one too.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        # One connection per thread (and process, in case the server forks),
        # since opening one costs far more than the queries a listing runs.
        self._local = threading.local()
        self._connections: list[_OpenConnection] = []
        self._lock = threading.Lock()
        self._warned: set[tuple[Any, Any]] = set()
        weakref.finalize(self, _close_all, self._connections, self._lock)
        self._init_db()

    def close(self) -> None:
        """Close the connections this process opened; later calls reopen them."""
        _close_all(self._connections, self._lock)
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        pid = os.getpid()
        if conn is None or self._local.pid != pid:
            # Each connection is only used by the thread that opened it; the
            # flag lets close(), pruning and the finalizer close it elsewhere.
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            with self._lock:
                self._prune(pid)
                self._connections.append(_OpenConnection(pid, threading.current_thread(), conn))
            self._local.conn, self._local.pid = conn, pid
        return conn

    def _prune(self, pid: int) -> None:
        """Close connections of threads in this process that have exited."""
        keep = []
        for entry in self._connections:
            if entry.pid == pid and not entry.thread.is_alive():
                entry.conn.close()
            else:
                keep.append(entry)
        self._connections[:] = keep

    @contextmanager
    def _get_conn(self):
        conn = self._connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        with self._get_conn() as conn:
            # WAL mode is a property of the database file, so setting it once is enough.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dashboards (
                    dashboard_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    items_json TEXT NOT NULL DEFAULT '[]',
                    edges_json TEXT NOT NULL DEFAULT '[]',
                    tile_layout_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_dashboards_user
                ON dashboards (user_id)
            """)
            migrations = [
                ("edges_json", "'[]'"),
                ("tile_layout_json", "'[]'"),
                ("breakpoints_json", "'[]'"),
                ("responsive_layouts_json", "'{}'"),
                ("permission_json", "'{}'"),
                ("reference_width_json", "'null'"),
                ("settings_json", "'{}'"),
                ("turns_json", "'[]'"),
                ("parameters_json", "'{}'"),
                ("groups_json", "'[]'"),
                ("report_json", "'null'"),
            ]
            for col, default in migrations:
                try:
                    conn.execute(
                        f"ALTER TABLE dashboards ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(
                    "ALTER TABLE dashboards ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            # Added with the edit history; databases written before it gain the table here.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dashboard_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    dashboard_id TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_dashboard_history
                ON dashboard_history (dashboard_id, seq)
            """)

    def load_history(self, dashboard_id: str) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT record_json FROM dashboard_history WHERE dashboard_id = ? ORDER BY seq",
                (dashboard_id,),
            ).fetchall()
        records = []
        for row in rows:
            try:
                record = json.loads(row["record_json"])
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def append_history(self, dashboard_id: str, record: dict[str, Any]) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO dashboard_history (dashboard_id, record_json) SELECT ?, ? "
                "WHERE EXISTS (SELECT 1 FROM dashboards WHERE dashboard_id = ?)",
                (dashboard_id, json.dumps(record), dashboard_id),
            )

    def replace_history(self, dashboard_id: str, records: list[dict[str, Any]]) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM dashboard_history WHERE dashboard_id = ?", (dashboard_id,))
            conn.executemany(
                "INSERT INTO dashboard_history (dashboard_id, record_json) VALUES (?, ?)",
                [(dashboard_id, json.dumps(record)) for record in records],
            )

    def delete_history(self, dashboard_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM dashboard_history WHERE dashboard_id = ?", (dashboard_id,))

    def rewrite_history(self, dashboard_id, rewrite):
        # Most loads drop nothing, so they read without taking the write lock.
        records = self.load_history(dashboard_id)
        kept = rewrite(records)
        if kept is records:
            return kept
        with self._get_conn() as conn:
            if not conn.in_transaction:
                # Taken before reading, so no other connection appends in between.
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT record_json FROM dashboard_history WHERE dashboard_id = ? ORDER BY seq",
                (dashboard_id,),
            ).fetchall()
            records = []
            for row in rows:
                try:
                    record = json.loads(row["record_json"])
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            kept = rewrite(records)
            if kept is not records:
                conn.execute(
                    "DELETE FROM dashboard_history WHERE dashboard_id = ?", (dashboard_id,)
                )
                conn.executemany(
                    "INSERT INTO dashboard_history (dashboard_id, record_json) VALUES (?, ?)",
                    [(dashboard_id, json.dumps(record)) for record in kept],
                )
        return kept

    def list_dashboards(self, user_id: str) -> list[DashboardModel]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dashboards WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return self._rows_to_models(rows)

    def title_exists(self, user_id: str, title: str, exclude_id: str | None = None) -> bool:
        """Check if a dashboard with the given title already exists for this user."""
        with self._get_conn() as conn:
            if exclude_id:
                row = conn.execute(
                    "SELECT 1 FROM dashboards WHERE user_id = ? AND title = ? AND dashboard_id != ? LIMIT 1",
                    (user_id, title, exclude_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM dashboards WHERE user_id = ? AND title = ? LIMIT 1",
                    (user_id, title),
                ).fetchone()
        return row is not None

    def load_dashboard(self, user_id: str, dashboard_id: str) -> DashboardModel | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM dashboards WHERE dashboard_id = ? AND user_id = ?",
                (dashboard_id, user_id),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard by id regardless of owner (for access checks)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM dashboards WHERE dashboard_id = ?",
                (dashboard_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def _all_dashboards(self) -> list[DashboardModel]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dashboards ORDER BY updated_at DESC",
            ).fetchall()
        return self._rows_to_models(rows)

    def find_by_id_or_title(
        self, ref: str, identity: Identity | None = None, *, default_allow: bool = True
    ) -> DashboardModel | None:
        model = self._load_any(ref)
        if model is not None:
            return model
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dashboards WHERE title = ? ORDER BY updated_at DESC",
                (ref,),
            ).fetchall()
        return self._pick_titled(self._rows_to_models(rows), identity, default_allow)

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        """Persist a new permission set on a dashboard. Returns success."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE dashboards SET permission_json = ?, updated_at = datetime('now') WHERE dashboard_id = ?",
                (json.dumps(permission.to_dict()), dashboard_id),
            )
        return cursor.rowcount > 0

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        columns = {
            "title": dashboard.title,
            "version": dashboard.version,
            "items_json": json.dumps([item.to_dict() for item in dashboard.items]),
            "edges_json": json.dumps([edge.to_dict() for edge in dashboard.edges]),
            "tile_layout_json": json.dumps(dashboard.tile_layout),
            "breakpoints_json": json.dumps(dashboard.breakpoints),
            "responsive_layouts_json": json.dumps(dashboard.responsive_layouts),
            "reference_width_json": json.dumps(dashboard.reference_width),
            "permission_json": json.dumps(dashboard.permission.to_dict()),
            "settings_json": json.dumps(dashboard.settings),
            "turns_json": json.dumps([turn.to_dict() for turn in dashboard.turns]),
            "parameters_json": json.dumps(
                {name: p.to_dict() for name, p in dashboard.parameters.items()}
            ),
            "groups_json": json.dumps([group.to_dict() for group in dashboard.groups]),
            "report_json": json.dumps(dashboard.report),
        }
        with self._get_conn() as conn:
            # Writing first takes the database's write lock, so the revision
            # read below cannot change before this transaction commits.
            conn.execute(
                "UPDATE dashboards SET revision = revision WHERE dashboard_id = ?",
                (dashboard.dashboard_id,),
            )
            row = conn.execute(
                "SELECT revision FROM dashboards WHERE dashboard_id = ?",
                (dashboard.dashboard_id,),
            ).fetchone()
            revision = check_revision(dashboard, None if row is None else row["revision"])
            if row is None:
                names = ["dashboard_id", "user_id", *columns, "revision"]
                conn.execute(
                    f"INSERT INTO dashboards ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' * len(names))})",
                    (dashboard.dashboard_id, dashboard.user_id, *columns.values(), revision),
                )
            else:
                assignments = ", ".join(f"{name} = ?" for name in columns)
                conn.execute(
                    f"UPDATE dashboards SET {assignments}, revision = ?, "
                    "updated_at = datetime('now') WHERE dashboard_id = ?",
                    (*columns.values(), revision, dashboard.dashboard_id),
                )
        dashboard.revision = revision

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM dashboards WHERE dashboard_id = ? AND user_id = ?",
                (dashboard_id, user_id),
            )
            if cursor.rowcount > 0:
                conn.execute(
                    "DELETE FROM dashboard_history WHERE dashboard_id = ?", (dashboard_id,)
                )
        return cursor.rowcount > 0

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE dashboards SET title = ?, updated_at = datetime('now') WHERE dashboard_id = ? AND user_id = ?",
                (new_title, dashboard_id, user_id),
            )
        return cursor.rowcount > 0

    def _rows_to_models(self, rows: list[sqlite3.Row]) -> list[DashboardModel]:
        return [model for model in map(self._decode, rows) if model is not None]

    def _decode(self, row: sqlite3.Row) -> DashboardModel | None:
        """Decode one row, or log (once per revision) and return None if it is malformed."""
        try:
            return self._row_to_model(row)
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"])
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Skipping malformed dashboard row %r (%r)", row["dashboard_id"], exc
                )
            return None

    def _row_to_model(self, row: sqlite3.Row) -> DashboardModel:
        items = json.loads(row["items_json"])
        keys = row.keys()
        edges_raw = row["edges_json"] if "edges_json" in keys else "[]"
        tile_layout_raw = row["tile_layout_json"] if "tile_layout_json" in keys else "[]"
        breakpoints_raw = row["breakpoints_json"] if "breakpoints_json" in keys else "[]"
        responsive_raw = (
            row["responsive_layouts_json"] if "responsive_layouts_json" in keys else "{}"
        )
        permission_raw = row["permission_json"] if "permission_json" in keys else "{}"
        reference_raw = row["reference_width_json"] if "reference_width_json" in keys else "null"
        edges = json.loads(edges_raw)
        tile_layout = json.loads(tile_layout_raw)
        breakpoints = json.loads(breakpoints_raw)
        responsive_layouts = json.loads(responsive_raw)
        permission = Permission.from_dict(json.loads(permission_raw))
        for key in ("dashboard_id", "user_id", "title"):
            if not isinstance(row[key], str):
                raise TypeError(f"{key} must be a string, not {type(row[key]).__name__}")
        return DashboardModel(
            dashboard_id=row["dashboard_id"],
            user_id=row["user_id"],
            title=row["title"],
            version=_migrated(row["version"]),
            items=[DashboardItem.from_dict(i) for i in items],
            edges=[DashboardEdge.from_dict(e) for e in edges],
            tile_layout=tile_layout,
            breakpoints=breakpoints,
            responsive_layouts=responsive_layouts,
            reference_width=json.loads(reference_raw),
            permission=permission,
            settings=self._row_settings(row) if "settings_json" in keys else {},
            turns=self._row_turns(row) if "turns_json" in keys else [],
            parameters=self._row_parameters(row) if "parameters_json" in keys else {},
            groups=self._row_groups(row) if "groups_json" in keys else [],
            report=self._row_report(row) if "report_json" in keys else None,
            revision=row["revision"] if "revision" in keys else 0,
            updated_at=_parse_sqlite_time(row["updated_at"]),
        )

    def _row_settings(self, row: sqlite3.Row) -> dict[str, Any]:
        """Decode a row's settings, or log (once per revision) and drop them if malformed."""
        try:
            return decode_settings(json.loads(row["settings_json"]))
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"], "settings")
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Ignoring malformed settings of dashboard %r (%r)", row["dashboard_id"], exc
                )
            return {}

    def _row_turns(self, row: sqlite3.Row) -> list[DashboardTurn]:
        """Decode a row's turns, or log (once per revision) and drop them if malformed."""
        try:
            return decode_turns(json.loads(row["turns_json"]))
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"], "turns")
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Ignoring malformed turns of dashboard %r (%r)", row["dashboard_id"], exc
                )
            return []

    def _row_parameters(self, row: sqlite3.Row) -> dict[str, DashboardParameter]:
        """Decode a row's parameters, or log (once per revision) and drop them if malformed."""
        try:
            return decode_parameters(json.loads(row["parameters_json"]))
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"], "parameters")
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Ignoring malformed parameters of dashboard %r (%r)", row["dashboard_id"], exc
                )
            return {}

    def _row_groups(self, row: sqlite3.Row) -> list[DashboardGroup]:
        """Decode a row's groups, or log (once per revision) and drop them if malformed."""
        try:
            return decode_groups(json.loads(row["groups_json"]))
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"], "groups")
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Ignoring malformed groups of dashboard %r (%r)", row["dashboard_id"], exc
                )
            return []

    def _row_report(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """Decode a row's report, or log (once per revision) and drop it if malformed."""
        try:
            return decode_report(json.loads(row["report_json"]))
        except MALFORMED_ERRORS as exc:
            key = (row["dashboard_id"], row["updated_at"], "report")
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "Ignoring malformed report of dashboard %r (%r)", row["dashboard_id"], exc
                )
            return None
