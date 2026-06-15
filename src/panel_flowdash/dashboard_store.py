"""SQLite-backed persistence for dashboard graphs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    version: int = 2
    items: list[DashboardItem] = field(default_factory=list)
    edges: list[DashboardEdge] = field(default_factory=list)
    tile_layout: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "dashboard_id": self.dashboard_id,
            "user_id": self.user_id,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
            "edges": [edge.to_dict() for edge in self.edges],
            "tile_layout": self.tile_layout,
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
        )


class DashboardStore:
    """SQLite-backed store for dashboard models."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

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
            for col, default in [("edges_json", "'[]'"), ("tile_layout_json", "'[]'")]:
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

    def load_dashboard(self, user_id: str, dashboard_id: str) -> DashboardModel | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM dashboards WHERE dashboard_id = ? AND user_id = ?",
                (dashboard_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        items_json = json.dumps([item.to_dict() for item in dashboard.items])
        edges_json = json.dumps([edge.to_dict() for edge in dashboard.edges])
        tile_layout_json = json.dumps(dashboard.tile_layout)
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO dashboards (dashboard_id, user_id, title, version, items_json, edges_json, tile_layout_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(dashboard_id) DO UPDATE SET
                    title = excluded.title,
                    version = excluded.version,
                    items_json = excluded.items_json,
                    edges_json = excluded.edges_json,
                    tile_layout_json = excluded.tile_layout_json,
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
                ),
            )

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM dashboards WHERE dashboard_id = ? AND user_id = ?",
                (dashboard_id, user_id),
            )
        return cursor.rowcount > 0

    def create_dashboard(self, user_id: str, title: str) -> DashboardModel:
        dashboard = DashboardModel(
            dashboard_id=uuid.uuid4().hex[:12],
            user_id=user_id,
            title=title,
        )
        self.save_dashboard(dashboard)
        return dashboard

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
        edges = json.loads(edges_raw)
        tile_layout = json.loads(tile_layout_raw)
        return DashboardModel(
            dashboard_id=row["dashboard_id"],
            user_id=row["user_id"],
            title=row["title"],
            version=row["version"],
            items=[DashboardItem.from_dict(i) for i in items],
            edges=[DashboardEdge.from_dict(e) for e in edges],
            tile_layout=tile_layout,
        )
