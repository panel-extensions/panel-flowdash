"""Reading and changing the tile layout of a dashboard from its editor.

Shared by the inspector's Tile tab and the layout panel. Every helper works
whether or not the tile grid is on screen, since the editor keeps the layout
of a hidden grid until it is shown again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import panel_material_ui as pmui

from panel_flowdash.ui.tile import resolve_framed

GRID_COLUMNS = 12

HIDDEN = "hidden"
# The roles named in the reason a tile is hidden, as the editor hides theirs itself.
_ROLE_NAMES = {"source": "data source", "transform": "transform"}

# The frame choice of a tile without a frame option of its own.
FRAME_DEFAULT = "default"

# Where a placed component is shown: as a tile on the grid, or beside it in the
# controls row above the grid or the sidebar left of it.
GRID, TOP, SIDEBAR = "grid", "top", "sidebar"
AREAS = (GRID, TOP, SIDEBAR)
AREA_NAMES = {GRID: "grid", TOP: "top bar", SIDEBAR: "sidebar"}
AREA_ICONS = {GRID: "dashboard", TOP: "vertical_align_top", SIDEBAR: "view_sidebar"}

# The tile kinds that stay legible beside the grid, in a 240 px slot of the top
# bar or the sidebar's column. Charts, tables and data previews need the room of
# a tile, so they stay on the grid.
COMPACT_KINDS = frozenset({"control", "kpi", "text"})


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
    # Showing or hiding a tile is not laying it out, so the editor may still arrange the rest.
    if set(patch) - {"visible"}:
        editor._auto_tiles.clear()
    if "visible" in patch:
        # Shown or hidden on purpose, so the editor does not hide it again.
        editor._chosen_visibility.add(instance_id)
    editor._pending_tile_layout = layout
    editor._rebuild_tile_grid()
    automatic = editor._auto_tiles or editor._deferred_tiles is not None
    if automatic and not set(patch) - {"visible"} and editor._all_tiles_automatic([instance_id]):
        # The rest is laid out by the editor, so a tile shown again joins its arrangement.
        if patch.get("visible") is not False:
            editor._auto_tiles.add(instance_id)
        if editor._deferred_tiles is not None:
            editor._deferred_rearrange = True
        else:
            editor._arrange_tiles()
    editor._record_edit(
        "Changed the tile layout",
        op="layout",
        args={"name": editor._node_name(instance_id), "keys": sorted(patch)},
    )


def move_tile(editor, instance_id: str, index: int):
    """Move a component's tile to position *index* among the tiles, 0 for the first."""
    layout = [dict(e) for e in _layout(editor)]
    entries = _entries(editor)
    tiles = [
        item["instance_id"]
        for item in editor._tile_items
        if area_of(editor, item, entries) == GRID
    ]
    ranked = {e["instance_id"]: e.get("index") for e in layout if "instance_id" in e}
    order = sorted(
        tiles, key=lambda iid: (ranked.get(iid) is None, ranked.get(iid) or 0, tiles.index(iid))
    )
    order.remove(instance_id)
    order.insert(max(0, min(index, len(order))), instance_id)
    by_id = {e["instance_id"]: e for e in layout if "instance_id" in e}
    for position, iid in enumerate(order):
        by_id.setdefault(iid, {"instance_id": iid})["index"] = position
    editor._auto_tiles.clear()
    editor._pending_tile_layout = [*by_id.values(), *(e for e in layout if "instance_id" not in e)]
    editor._rebuild_tile_grid()
    editor._record_edit(
        "Changed the tile layout", op="layout", args={"name": editor._node_name(instance_id)}
    )


def frame_choices(editor, instance_id: str) -> dict[str, bool | str]:
    """Return a tile's frame choices by label, naming what its default resolves to."""
    item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
    spec = editor._tile_spec(item) if item is not None else None
    framed = resolve_framed(None, spec and spec.framed, editor.settings.tile_cards)
    return {
        f"Default ({'framed' if framed else 'unframed'})": FRAME_DEFAULT,
        "Framed": True,
        "Unframed": False,
    }


def frame_value(entry: dict) -> bool | str:
    """Return the frame choice of a tile's layout *entry*."""
    framed = entry.get("framed")
    return framed if isinstance(framed, bool) else FRAME_DEFAULT


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
    hidden: bool = False
    area: str = GRID

    @property
    def restorable(self) -> bool:
        """Whether showing it again is a layout change, i.e. it was hidden."""
        return self.hidden


def tile_title(editor, item: dict) -> str:
    """Return the name a placed component goes by on the dashboard."""
    if "placeholder" in item:
        return item["placeholder"].title
    title = (item.get("config") or {}).get("title")
    spec = editor._component_specs.get(item["component_id"])
    return title or (spec.title if spec else item["component_id"])


def _entries(editor) -> dict[str, dict]:
    return {e["instance_id"]: e for e in _layout(editor) if "instance_id" in e}


def default_area(editor, item: dict) -> str | None:
    """Return where a component goes unless it was moved: controls beside the grid, the rest on it.

    Controls are the components registered with ``sidebar=True``, and go where
    the dashboard's ``controls_position`` setting says. None while the
    component is not loaded yet, which is shown nowhere until it is.
    """
    if "placeholder" in item:
        return GRID
    entry = editor._component_entries.get(item["component_id"])
    if entry is None:
        return None
    if entry.metadata.sidebar:
        return SIDEBAR if editor.settings.controls_position == "left" else TOP
    return GRID


def fits_beside(editor, item: dict) -> bool:
    """Whether a component may move off the grid: a control, a KPI or text, not a chart or table."""
    if "placeholder" in item:
        return False
    entry = editor._component_entries.get(item["component_id"])
    if entry is None:
        return False
    return entry.metadata.sidebar or editor._tile_box(item).kind in COMPACT_KINDS


def refusal(editor, item: dict, area: str) -> str:
    """Explain why a component that does not fit beside the grid cannot go to *area*."""
    if "placeholder" in item:
        return (
            f"{tile_title(editor, item)} is not available to you, so it stays where it is "
            "for those who can use it."
        )
    return (
        f"{tile_title(editor, item)} needs the room of a tile; only controls, KPIs "
        f"and text fit in the {AREA_NAMES[area]}."
    )


def area_of(editor, item: dict, entries: dict[str, dict] | None = None) -> str | None:
    """Return the area a placed component is shown in, one of :data:`AREAS`, or None if nowhere yet."""
    default = default_area(editor, item)
    if default is None:
        return None
    entries = _entries(editor) if entries is None else entries
    area = entries.get(item["instance_id"], {}).get("area")
    if area == GRID or (area in AREAS and fits_beside(editor, item)):
        return area
    return default


def _ordered(items: list[dict], entries: dict[str, dict]) -> list[dict]:
    def key(pair):
        position, item = pair
        index = entries.get(item["instance_id"], {}).get("index")
        return (index is None, index or 0, position)

    return [item for _, item in sorted(enumerate(items), key=key)]


def area_items(editor, area: str, entries: dict[str, dict] | None = None) -> list[dict]:
    """Return the placed items shown in *area*, in their order there; hidden tiles included."""
    entries = _entries(editor) if entries is None else entries
    items = [item for item in editor._tile_items if area_of(editor, item, entries) == area]
    return _ordered(items, entries)


def move_to_area(
    editor, instance_id: str, area: str, before: str | None = None, index: int | None = None
) -> bool:
    """Show a component in *area*, in front of the component *before* there or else last.

    *index*, the position among the others in *area* with 0 for the first, may
    be given instead of *before*.

    One edit, and so one version in the history. Moving within an area
    reorders it. A tile keeps its size on the grid while it is beside it, and
    a hidden tile moved anywhere is shown. Returns whether anything moved.

    Raises
    ------
    ValueError
        If the component is a chart, table or data preview and *area* is not
        the grid, or *area* is unknown.
    """
    if area not in AREAS:
        raise ValueError(f"area is one of {', '.join(AREAS)}, got {area!r}.")
    item = next(i for i in editor._tile_items if i["instance_id"] == instance_id)
    if area != GRID and not fits_beside(editor, item):
        raise ValueError(refusal(editor, item, area))
    layout = [dict(e) for e in _layout(editor)]
    by_id = {e["instance_id"]: e for e in layout if "instance_id" in e}
    order = [
        i["instance_id"]
        for i in area_items(editor, area, by_id)
        if i["instance_id"] != instance_id
    ]
    if before is None and index is not None:
        before = order[index] if 0 <= index < len(order) else None
    order.insert(order.index(before) if before in order else len(order), instance_id)
    current = [i["instance_id"] for i in area_items(editor, area, by_id)]
    shown = by_id.get(instance_id, {}).get("visible") is not False
    if area_of(editor, item, by_id) == area and order == current and shown:
        return False
    entry = by_id.setdefault(instance_id, {"instance_id": instance_id})
    if area == default_area(editor, item) == GRID:
        entry.pop("area", None)
    else:
        entry["area"] = area
    entry.pop("visible", None)
    for position, iid in enumerate(order):
        by_id.setdefault(iid, {"instance_id": iid})["index"] = position
    editor._auto_tiles.clear()
    editor._pending_tile_layout = [*by_id.values(), *(e for e in layout if "instance_id" not in e)]
    editor._rebuild_tile_grid()
    name = editor._node_name(instance_id)
    editor._record_edit(
        f"Moved {name} to the {AREA_NAMES[area]}", op="layout", args={"name": name, "area": area}
    )
    return True


def move_targets(editor, item: dict) -> list[str]:
    """Return the areas a placed component can be moved to from where it is now."""
    area = area_of(editor, item)
    if area is None or (area == GRID and not fits_beside(editor, item)):
        return []
    return [a for a in AREAS if a != area]


def move_buttons(
    editor,
    item: dict,
    areas: Iterable[str] | None = None,
    then: Callable[[], None] | None = None,
    **params,
) -> list[pmui.Button]:
    """Return a "Move to ..." button for each area the component can be moved to, of *areas* if given.

    They do what dragging the component there does, for keyboards and screen
    readers. *then* is called after a move, e.g. to put the focus back on the
    rebuilt panel, since the button pressed goes away with the move.
    """
    buttons = []
    for area in move_targets(editor, item):
        if areas is not None and area not in areas:
            continue
        button = pmui.Button(
            label=f"Move to {AREA_NAMES[area]}",
            description=f"Move {tile_title(editor, item)} to the {AREA_NAMES[area]}",
            icon=AREA_ICONS[area],
            variant="text",
            size="small",
            sx={"textTransform": "none"},
            **params,
        )

        def move(_event, area=area):
            move_to_area(editor, item["instance_id"], area)
            if then is not None:
                then()

        button.on_click(move)
        buttons.append(button)
    return buttons


def grid_tiles(editor) -> list[dict]:
    """Return the placed items shown as tiles, in placement order."""
    entries = _entries(editor)
    return [
        item
        for item in editor._tile_items
        if area_of(editor, item, entries) == GRID
        and entries.get(item["instance_id"], {}).get("visible") is not False
    ]


def off_grid(editor) -> list[OffGrid]:
    """List the placed components that have no tile on the dashboard, with the reason."""
    entries = _entries(editor)
    hidden = {iid for iid, e in entries.items() if e.get("visible") is False}
    listed = []
    for item in editor._tile_items:
        area = area_of(editor, item, entries)
        if area is None:
            continue
        if area != GRID:
            # Without a controls bar, the host shows the components beside the grid itself.
            reason = (
                f"in {AREA_NAMES[area] if getattr(editor, 'controls_bar', False) else 'sidebar'}"
            )
        elif item["instance_id"] in hidden:
            role = _ROLE_NAMES.get(editor._node_role(item["component_id"]))
            reason = f"{role} · {HIDDEN}" if role else HIDDEN
        else:
            continue
        listed.append(
            OffGrid(
                item["instance_id"],
                tile_title(editor, item),
                reason,
                hidden=item["instance_id"] in hidden,
                area=area,
            )
        )
    return listed


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def layout_summary(editor) -> str:
    """Summarize the grid, e.g. ``"4 tiles, 2 components hidden"``."""
    summary = _count(len(grid_tiles(editor)), "tile")
    if hidden := len(off_grid(editor)):
        summary += f", {_count(hidden, 'component')} hidden"
    return summary
