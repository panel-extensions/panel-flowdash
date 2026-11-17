"""What changed between two versions of a dashboard, in the dashboard's own terms."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

import yaml

_Doc = dict[str, t.Any]


@dataclass(frozen=True)
class DashboardChange:
    """One difference between two versions of a dashboard.

    Attributes
    ----------
    op
        ``"add"``, ``"remove"`` or ``"change"``.
    path
        Where, as keys into the YAML document, e.g. ``("nodes", "kpi", "config", "metric")``.
    label
        The change in a few words, e.g. ``"kpi: metric turbines → capacity"``.
    old, new
        The values before and after, None for an addition or removal.
    """

    op: str
    path: tuple[str, ...]
    label: str
    old: t.Any = None
    new: t.Any = None


def _load(value: str | _Doc | None) -> _Doc:
    if value is None:
        return {}
    if isinstance(value, (str, bytes)):
        value = yaml.safe_load(value) or {}
    return value if isinstance(value, dict) else {}


def _short(value: t.Any) -> str:
    text = value if isinstance(value, str) else yaml.safe_dump(value, default_flow_style=True)
    text = " ".join(str(text).split()).removesuffix("...").strip()
    return text if len(text) <= 40 else text[:39] + "…"


def _edge_key(edge: dict) -> str:
    return f"{edge.get('from')} → {edge.get('to')}"


def _mapping_changes(
    old: dict, new: dict, path: tuple[str, ...], describe: t.Callable[[str], str]
) -> list[DashboardChange]:
    changes = []
    for key in [*old, *(k for k in new if k not in old)]:
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        if key not in new:
            op, label = "remove", f"{describe(key)} removed"
        elif key not in old:
            op, label = "add", f"{describe(key)} set to {_short(after)}"
        else:
            op, label = "change", f"{describe(key)}: {_short(before)} → {_short(after)}"
        changes.append(DashboardChange(op, (*path, str(key)), label, before, after))
    return changes


def dashboard_changes(old: str | _Doc | None, new: str | _Doc | None) -> list[DashboardChange]:
    """List what changed from one version of a dashboard to the next.

    Parameters
    ----------
    old, new
        Two versions in the YAML format, as text or parsed.

    Returns
    -------
    list of DashboardChange
        Nodes added, removed or given another component, config fields
        changed, edges added or removed, transforms changed, tiles changed and
        settings changed, in that order.
    """
    before, after = _load(old), _load(new)
    changes: list[DashboardChange] = []
    if before.get("title") != after.get("title"):
        changes.append(
            DashboardChange(
                "change",
                ("title",),
                f"Renamed to {_short(after.get('title'))}",
                before.get("title"),
                after.get("title"),
            )
        )
    old_nodes, new_nodes = before.get("nodes") or {}, after.get("nodes") or {}
    for name, node in new_nodes.items():
        if name not in old_nodes:
            changes.append(
                DashboardChange(
                    "add", ("nodes", name), f"Added {name} ({node.get('component')})", None, node
                )
            )
    for name, node in old_nodes.items():
        if name not in new_nodes:
            changes.append(
                DashboardChange(
                    "remove",
                    ("nodes", name),
                    f"Removed {name} ({node.get('component')})",
                    node,
                    None,
                )
            )
    for name, node in new_nodes.items():
        previous = old_nodes.get(name)
        if previous is None:
            continue
        if previous.get("component") != node.get("component"):
            changes.append(
                DashboardChange(
                    "change",
                    ("nodes", name, "component"),
                    f"{name}: {previous.get('component')} → {node.get('component')}",
                    previous.get("component"),
                    node.get("component"),
                )
            )
        changes += _mapping_changes(
            previous.get("config") or {},
            node.get("config") or {},
            ("nodes", name, "config"),
            lambda key, name=name: f"{name}: {key}",
        )
        if previous.get("position") != node.get("position"):
            changes.append(
                DashboardChange(
                    "change",
                    ("nodes", name, "position"),
                    f"{name} moved on the canvas",
                    previous.get("position"),
                    node.get("position"),
                )
            )
    old_edges = {_edge_key(e): e for e in before.get("edges") or []}
    new_edges = {_edge_key(e): e for e in after.get("edges") or []}
    for key, edge in new_edges.items():
        if key not in old_edges:
            changes.append(DashboardChange("add", ("edges",), f"Connected {key}", None, edge))
    for key, edge in old_edges.items():
        if key not in new_edges:
            changes.append(
                DashboardChange("remove", ("edges",), f"Disconnected {key}", edge, None)
            )
    for key, edge in new_edges.items():
        previous = old_edges.get(key)
        if previous is not None and previous.get("transform") != edge.get("transform"):
            steps = len(edge.get("transform") or [])
            label = f"{key}: transform " + (
                f"set to {steps} step{'s' * (steps != 1)}" if steps else "removed"
            )
            changes.append(
                DashboardChange(
                    "change", ("edges",), label, previous.get("transform"), edge.get("transform")
                )
            )
    old_layout, new_layout = before.get("layout") or {}, after.get("layout") or {}
    old_tiles = {t["node"]: t for t in old_layout.get("tiles") or [] if isinstance(t, dict)}
    new_tiles = {t["node"]: t for t in new_layout.get("tiles") or [] if isinstance(t, dict)}
    for name in [*new_tiles, *(n for n in old_tiles if n not in new_tiles)]:
        if old_tiles.get(name) != new_tiles.get(name):
            changes.append(
                DashboardChange(
                    "change",
                    ("layout", "tiles", name),
                    f"Tile {name} laid out differently",
                    old_tiles.get(name),
                    new_tiles.get(name),
                )
            )
    if [t for t in old_tiles] != [t for t in new_tiles if t in old_tiles] and set(
        old_tiles
    ) == set(new_tiles):
        changes.append(
            DashboardChange(
                "change", ("layout", "tiles"), "Tiles reordered", list(old_tiles), list(new_tiles)
            )
        )
    changes += _mapping_changes(
        before.get("settings") or {},
        after.get("settings") or {},
        ("settings",),
        lambda key: f"Setting {key}",
    )
    return changes
