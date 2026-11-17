"""Reading and changing the tile layout of a dashboard from its editor.

Shared by the inspector's Tile tab and the layout panel. Every helper works
whether or not the tile grid is on screen, since the editor keeps the layout
of a hidden grid until it is shown again.
"""

from __future__ import annotations

from dataclasses import dataclass

GRID_COLUMNS = 12

HIDDEN = "hidden by you"


def _layout(editor) -> list[dict]:
    return editor._pending_tile_layout or editor.layout


def tile_entry(editor, instance_id: str) -> dict:
    """Return the tile layout entry of a component, empty if it has none yet."""
    return next((dict(e) for e in _layout(editor) if e.get("instance_id") == instance_id), {})


def update_tile(editor, instance_id: str, **patch):
    """Change how a component's tile is laid out, e.g. ``visible``, ``width`` or ``height``.

    Works whether or not the tile grid is on screen: the grid is rebuilt from
    the updated layout, which is what is saved.
    """
    layout = [dict(e) for e in _layout(editor)]
    entry = next((e for e in layout if e.get("instance_id") == instance_id), None)
    if entry is None:
        entry = {"instance_id": instance_id}
        layout.append(entry)
    entry.update(patch)
    for key in [k for k, v in patch.items() if v is None]:
        del entry[key]
    editor._pending_tile_layout = layout
    editor._rebuild_tile_grid()
    editor.dirty = True


def move_tile(editor, instance_id: str, index: int):
    """Move a component's tile to position *index* among the tiles, 0 for the first."""
    layout = [dict(e) for e in _layout(editor)]
    tiles = [item["instance_id"] for item in editor._tile_items if not _in_sidebar(editor, item)]
    ranked = {e["instance_id"]: e.get("index") for e in layout if "instance_id" in e}
    order = sorted(
        tiles, key=lambda iid: (ranked.get(iid) is None, ranked.get(iid) or 0, tiles.index(iid))
    )
    order.remove(instance_id)
    order.insert(max(0, min(index, len(order))), instance_id)
    by_id = {e["instance_id"]: e for e in layout if "instance_id" in e}
    for position, iid in enumerate(order):
        by_id.setdefault(iid, {"instance_id": iid})["index"] = position
    editor._pending_tile_layout = [*by_id.values(), *(e for e in layout if "instance_id" not in e)]
    editor._rebuild_tile_grid()
    editor.dirty = True


def to_columns(width: float | None) -> int:
    """Return how many of the grid's columns a tile *width* in percent of the row spans."""
    if width is None:
        return GRID_COLUMNS
    return min(GRID_COLUMNS, max(1, round(width * GRID_COLUMNS / 100)))


def to_width(columns: int) -> float:
    """Return the width in percent of the row that spans *columns* of the grid."""
    return round(columns * 100 / GRID_COLUMNS, 2)


@dataclass(frozen=True)
class OffGrid:
    """A placed component without a tile on the dashboard, and why."""

    instance_id: str
    title: str
    reason: str

    @property
    def restorable(self) -> bool:
        """Whether showing it again is a layout change, i.e. it was hidden."""
        return self.reason == HIDDEN


def tile_title(editor, item: dict) -> str:
    """Return the name a placed component goes by on the dashboard."""
    if "placeholder" in item:
        return item["placeholder"].title
    title = (item.get("config") or {}).get("title")
    spec = editor._component_specs.get(item["component_id"])
    return title or (spec.title if spec else item["component_id"])


def _in_sidebar(editor, item: dict) -> bool:
    entry = editor._component_entries.get(item["component_id"])
    return "placeholder" not in item and (entry is None or entry.metadata.sidebar)


def _sidebar_reason(editor) -> str:
    return "in controls bar" if getattr(editor, "controls_bar", False) else "in sidebar"


def grid_tiles(editor) -> list[dict]:
    """Return the placed items shown as tiles, in placement order."""
    hidden = {e.get("instance_id") for e in _layout(editor) if e.get("visible") is False}
    return [
        item
        for item in editor._tile_items
        if not _in_sidebar(editor, item) and item["instance_id"] not in hidden
    ]


def off_grid(editor) -> list[OffGrid]:
    """List the placed components that have no tile on the dashboard, with the reason."""
    hidden = {e.get("instance_id") for e in _layout(editor) if e.get("visible") is False}
    listed = []
    for item in editor._tile_items:
        if _in_sidebar(editor, item):
            reason = _sidebar_reason(editor)
        elif item["instance_id"] in hidden:
            reason = HIDDEN
        else:
            continue
        listed.append(OffGrid(item["instance_id"], tile_title(editor, item), reason))
    return listed


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def layout_summary(editor) -> str:
    """Summarize the grid, e.g. ``"4 tiles, 2 components hidden"``."""
    summary = _count(len(grid_tiles(editor)), "tile")
    if hidden := len(off_grid(editor)):
        summary += f", {_count(hidden, 'component')} hidden"
    return summary
