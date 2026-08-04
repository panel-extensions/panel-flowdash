"""Persistence for dashboard graphs, backed by SQLite or an in-memory dict."""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panel_flowdash.auth import Identity, Permission, can_administer, is_authorized


@dataclass
class DashboardItem:
    """A component instance on the dashboard.

    x, y store the ReactFlow node canvas position.
    Grid layout (widths, heights, visibility) lives in DashboardModel.tile_layout.
    """

    instance_id: str
    component_id: str
    x: float = 0
    y: float = 0
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "component_id": self.component_id,
            "x": self.x,
            "y": self.y,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardItem:
        return cls(
            instance_id=data["instance_id"],
            component_id=data["component_id"],
            x=data.get("x", 0),
            y=data.get("y", 0),
            config=data.get("config", {}),
        )


@dataclass
class DashboardEdge:
    """A connection between two component ports."""

    source: str
    source_port: str
    target: str
    target_port: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "source_port": self.source_port,
            "target": self.target,
            "target_port": self.target_port,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> DashboardEdge:
        return cls(
            source=data["source"],
            source_port=data["source_port"],
            target=data["target"],
            target_port=data["target_port"],
        )


@dataclass
class DashboardModel:
    """A persisted dashboard: nodes + edges + tile layout."""

    dashboard_id: str
    user_id: str
    title: str
    version: int = 3
    items: list[DashboardItem] = field(default_factory=list)
    edges: list[DashboardEdge] = field(default_factory=list)
    tile_layout: list[dict[str, Any]] = field(default_factory=list)
    breakpoints: list[int] = field(default_factory=list)
    responsive_layouts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    permission: Permission = field(default_factory=Permission)

    @property
    def owner(self) -> str:
        """The immutable owner principal (the creating user)."""
        return self.user_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "dashboard_id": self.dashboard_id,
            "user_id": self.user_id,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
            "edges": [edge.to_dict() for edge in self.edges],
            "tile_layout": self.tile_layout,
            "breakpoints": self.breakpoints,
            "responsive_layouts": self.responsive_layouts,
            "permission": self.permission.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DashboardModel:
        return cls(
            dashboard_id=data["dashboard_id"],
            user_id=data["user_id"],
            title=data["title"],
            version=data.get("version", 1),
            items=[DashboardItem.from_dict(i) for i in data.get("items", [])],
            edges=[DashboardEdge.from_dict(e) for e in data.get("edges", [])],
            tile_layout=data.get("tile_layout", []),
            breakpoints=data.get("breakpoints", []),
            responsive_layouts=data.get("responsive_layouts", {}),
            permission=Permission.from_dict(data.get("permission")),
        )


class BaseDashboardStore(ABC):
    """The persistence interface the editor and app shell depend on.

    Subclasses implement the six storage primitives below; the access-control
    and lookup helpers are backend-independent and inherited. Implement this to
    back dashboards with something other than SQLite.
    """

    @abstractmethod
    def save_dashboard(self, dashboard: DashboardModel) -> None:
        """Insert or update a dashboard."""

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

    def find_by_id_or_title(self, ref: str) -> DashboardModel | None:
        """Resolve a dashboard by its id first, then by title.

        Titles are only unique per user, so a title match returns the most
        recently updated dashboard. Used to resolve the operator-configured
        home dashboard, which may be given as either an id or a title.
        """
        model = self._load_any(ref)
        if model is not None:
            return model
        return next((m for m in self._all_dashboards() if m.title == ref), None)

    def get_owner(self, dashboard_id: str) -> str | None:
        """Return the owner (user_id) of a dashboard, or ``None`` if missing."""
        model = self._load_any(dashboard_id)
        return model.owner if model else None

    def can_administer(
        self, identity: Identity, dashboard_id: str, admin_groups: frozenset[str] = frozenset()
    ) -> bool:
        """Whether *identity* may administer (edit/delete/share) the dashboard."""
        model = self._load_any(dashboard_id)
        if model is None:
            return False
        return can_administer(identity, model.owner, admin_groups)


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
        for dashboard in (dashboards or {}).values():
            self.save_dashboard(dashboard)

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        self._dashboards[dashboard.dashboard_id] = copy.deepcopy(dashboard)
        # Re-inserting moves the dashboard to the front of the recency order.
        if dashboard.dashboard_id in self._order:
            self._order.remove(dashboard.dashboard_id)
        self._order.insert(0, dashboard.dashboard_id)

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        model = self._dashboards.get(dashboard_id)
        return copy.deepcopy(model) if model is not None else None

    def _all_dashboards(self) -> list[DashboardModel]:
        return [copy.deepcopy(self._dashboards[did]) for did in self._order]

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        model = self._dashboards.get(dashboard_id)
        if model is None or model.user_id != user_id:
            return False
        del self._dashboards[dashboard_id]
        self._order.remove(dashboard_id)
        return True

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        model = self._dashboards.get(dashboard_id)
        if model is None or model.user_id != user_id:
            return False
        model.title = new_title
        return True

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        model = self._dashboards.get(dashboard_id)
        if model is None:
            return False
        model.permission = copy.deepcopy(permission)
        return True


class DashboardStore(BaseDashboardStore):
    """SQLite-backed store for dashboard models."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._get_conn() as conn:
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
            ]
            for col, default in migrations:
                try:
                    conn.execute(
                        f"ALTER TABLE dashboards ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass

    def list_dashboards(self, user_id: str) -> list[DashboardModel]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dashboards WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

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
        if row is None:
            return None
        return self._row_to_model(row)

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard by id regardless of owner (for access checks)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM dashboards WHERE dashboard_id = ?",
                (dashboard_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def _all_dashboards(self) -> list[DashboardModel]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dashboards ORDER BY updated_at DESC",
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def find_by_id_or_title(self, ref: str) -> DashboardModel | None:
        """Resolve a dashboard by its id first, then by title.

        Titles are only unique per user, so a title match returns the most
        recently updated dashboard. Used to resolve the operator-configured
        home dashboard, which may be given as either an id or a title.
        """
        model = self._load_any(ref)
        if model is not None:
            return model
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM dashboards WHERE title = ? ORDER BY updated_at DESC LIMIT 1",
                (ref,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        """Persist a new permission set on a dashboard. Returns success."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE dashboards SET permission_json = ?, updated_at = datetime('now') WHERE dashboard_id = ?",
                (json.dumps(permission.to_dict()), dashboard_id),
            )
        return cursor.rowcount > 0

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        items_json = json.dumps([item.to_dict() for item in dashboard.items])
        edges_json = json.dumps([edge.to_dict() for edge in dashboard.edges])
        tile_layout_json = json.dumps(dashboard.tile_layout)
        breakpoints_json = json.dumps(dashboard.breakpoints)
        responsive_layouts_json = json.dumps(dashboard.responsive_layouts)
        permission_json = json.dumps(dashboard.permission.to_dict())
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO dashboards (dashboard_id, user_id, title, version, items_json, edges_json, tile_layout_json, breakpoints_json, responsive_layouts_json, permission_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(dashboard_id) DO UPDATE SET
                    title = excluded.title,
                    version = excluded.version,
                    items_json = excluded.items_json,
                    edges_json = excluded.edges_json,
                    tile_layout_json = excluded.tile_layout_json,
                    breakpoints_json = excluded.breakpoints_json,
                    responsive_layouts_json = excluded.responsive_layouts_json,
                    permission_json = excluded.permission_json,
                    updated_at = datetime('now')
                """,
                (
                    dashboard.dashboard_id,
                    dashboard.user_id,
                    dashboard.title,
                    dashboard.version,
                    items_json,
                    edges_json,
                    tile_layout_json,
                    breakpoints_json,
                    responsive_layouts_json,
                    permission_json,
                ),
            )

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM dashboards WHERE dashboard_id = ? AND user_id = ?",
                (dashboard_id, user_id),
            )
        return cursor.rowcount > 0

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE dashboards SET title = ?, updated_at = datetime('now') WHERE dashboard_id = ? AND user_id = ?",
                (new_title, dashboard_id, user_id),
            )
        return cursor.rowcount > 0

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
        edges = json.loads(edges_raw)
        tile_layout = json.loads(tile_layout_raw)
        breakpoints = json.loads(breakpoints_raw)
        responsive_layouts = json.loads(responsive_raw)
        permission = Permission.from_dict(json.loads(permission_raw))
        return DashboardModel(
            dashboard_id=row["dashboard_id"],
            user_id=row["user_id"],
            title=row["title"],
            version=row["version"],
            items=[DashboardItem.from_dict(i) for i in items],
            edges=[DashboardEdge.from_dict(e) for e in edges],
            tile_layout=tile_layout,
            breakpoints=breakpoints,
            responsive_layouts=responsive_layouts,
            permission=permission,
        )
