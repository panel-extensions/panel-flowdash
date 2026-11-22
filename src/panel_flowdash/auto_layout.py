"""Automatic placement of nodes on the canvas and of tiles on the dashboard grid.

Models are poor at choosing coordinates, so FlowDash places what is added
without a position itself and offers to rearrange a whole dashboard on request.
Both algorithms are deterministic and work on plain data, so the editor, the
YAML reader and tests share them. The spacing and the rules that decide where
a tile goes are the constants below, kept here so they are easy to tweak.

Canvas
    Nodes go in columns by their depth in the dataflow, sources on the left
    and views on the right; groups of nodes not wired to each other are laid
    out on their own and packed side by side. A node without inputs moves right up to the
    column before the first node it feeds, so a control sits beside the
    transform it filters rather than with the data sources. Rows within a
    column are ordered by the barycenter heuristic to reduce edge crossings,
    and each node is then aligned with the nodes it is wired to.

Tiles
    Tiles are grouped by kind in this order: controls, KPIs, charts, tables
    and last the tiles of data sources and transforms, which start hidden
    when the editor places them. Controls, KPIs and data tiles keep the
    width of their ``default_size`` in whole columns and are packed into
    rows; controls and KPIs share a row only when they all fit in it.
    Charts fill a grid of two columns, an odd one out taking at most twice
    its default width, and tables take the full width. Text tiles
    (headings, notes) stay in front of the tile they preceded, so a heading
    keeps titling its section. The grid moves a tile up into any gap it
    fits, so the room a row of packed tiles leaves is filled on purpose: by
    the leading heading in the first row, else by the chart or table that
    follows, else by widening the row's tiles to at most twice their width.
    Tiles in a row share one height. The grid generates the layouts of
    narrower breakpoint bands from the arranged one; a band with a layout of
    its own gets new tiles inserted at the band's width.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------
# Canvas
# ----------------------------------------------------------------------------

#: Horizontal gap, in flow units, between the cards of neighbouring columns.
COLUMN_GAP = 120
#: Vertical gap between cards stacked in one column.
ROW_GAP = 40
#: Gap between a newly laid out group of nodes and the nodes already on the canvas.
BLOCK_GAP = 120
#: How much wider than tall the shelves of unconnected groups of nodes are packed.
SHELF_ASPECT = 1.6
#: Barycenter sweeps run to order the rows of the columns.
ORDER_SWEEPS = 6
#: Alignment passes run after ordering, each one down and up the columns.
ALIGN_PASSES = 3

#: The column a node without edges goes to, by role, as a fraction of the
#: graph's depth: sources on the left, views on the right.
ROLE_COLUMN = {"source": 0.0, "widget": 0.0, "transform": 0.5, "view": 1.0, "other": 1.0}
#: Columns of a graph without edges, by role.
ROLE_COLUMN_UNWIRED = {"source": 0, "widget": 1, "transform": 2, "view": 3, "other": 3}


@dataclass(frozen=True)
class NodeBox:
    """A node to place: its id, its card's size in flow units and its role."""

    id: str
    width: float
    height: float
    role: str = "other"


Point = tuple[float, float]


def _graph(ids: Sequence[str], edges: Iterable[tuple[str, str]]):
    known = set(ids)
    preds: dict[str, list[str]] = {i: [] for i in ids}
    succs: dict[str, list[str]] = {i: [] for i in ids}
    for source, target in edges:
        if source in known and target in known and source != target:
            if source not in preds[target]:
                preds[target].append(source)
                succs[source].append(target)
    return preds, succs


def _topological(ids: Sequence[str], preds, succs) -> list[str]:
    """Order *ids* so every node follows its inputs, breaking any cycle at the earliest node."""
    pending = {i: len(preds[i]) for i in ids}
    ready = [i for i in ids if not pending[i]]
    order: list[str] = []
    seen: set[str] = set()
    while len(order) < len(ids):
        if not ready:
            # A cycle, which the dataflow refuses, but a file may hold one.
            ready = [next(i for i in ids if i not in seen)]
        node = ready.pop(0)
        if node in seen:
            continue
        seen.add(node)
        order.append(node)
        for target in succs[node]:
            pending[target] -= 1
            if pending[target] <= 0 and target not in seen:
                ready.append(target)
    return order


def assign_columns(nodes: Sequence[NodeBox], edges: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Return the column of each node, 0 for the leftmost."""
    ids = [n.id for n in nodes]
    roles = {n.id: n.role for n in nodes}
    preds, succs = _graph(ids, edges)
    order = _topological(ids, preds, succs)
    rank = {node: i for i, node in enumerate(order)}
    column: dict[str, int] = {}
    for node in order:
        column[node] = 1 + max(
            (column[p] for p in preds[node] if rank[p] < rank[node]), default=-1
        )
    # Nodes without inputs sit just before the first node they feed.
    for node in reversed(order):
        if not preds[node] and succs[node]:
            column[node] = max(0, min(column[s] for s in succs[node]) - 1)
    wired = [i for i in ids if preds[i] or succs[i]]
    unwired = [i for i in ids if not (preds[i] or succs[i])]
    if wired:
        depth = max(column[i] for i in wired)
        for node in unwired:
            column[node] = round(ROLE_COLUMN.get(roles[node], 1.0) * depth)
    else:
        for node in unwired:
            column[node] = ROLE_COLUMN_UNWIRED.get(roles[node], 3)
    used = sorted(set(column.values()))
    compact = {c: i for i, c in enumerate(used)}
    return {node: compact[column[node]] for node in ids}


def _crossings(layers: list[list[str]], column: dict[str, int], preds) -> int:
    """Count the crossings between edges that join the same two columns."""
    position = {node: i for layer in layers for i, node in enumerate(layer)}
    total = 0
    for c, layer in enumerate(layers[1:], start=1):
        pairs = [
            (position[p], position[node])
            for node in layer
            for p in preds[node]
            if column[p] == c - 1
        ]
        for i, (a, b) in enumerate(pairs):
            for c2, d in pairs[i + 1 :]:
                if (a - c2) * (b - d) < 0:
                    total += 1
    return total


def order_rows(
    nodes: Sequence[NodeBox],
    edges: Iterable[tuple[str, str]],
    column: Mapping[str, int],
    initial: Mapping[str, float] | None = None,
) -> list[list[str]]:
    """Order the nodes of each column to reduce edge crossings, top to bottom.

    *initial* gives a starting key per node, e.g. its current y, ties and
    nodes without one keeping the order of *nodes*.
    """
    ids = [n.id for n in nodes]
    preds, succs = _graph(ids, edges)
    initial = initial or {}
    index = {node: i for i, node in enumerate(ids)}
    layers: list[list[str]] = [[] for _ in range(max(column.values(), default=-1) + 1)]
    for node in sorted(ids, key=lambda n: (initial.get(n, math.inf), index[n])):
        layers[column[node]].append(node)

    def place(layer):
        return {node: (i + 0.5) / len(layer) for i, node in enumerate(layer)}

    def sweep(layers, neighbours, indices):
        for c in indices:
            fixed = {}
            for other in layers:
                fixed.update(place(other))
            current = place(layers[c])

            def key(node, current=current, fixed=fixed):
                linked = [fixed[n] for n in neighbours[node]]
                centre = sum(linked) / len(linked) if linked else current[node]
                return (centre, current[node])

            layers[c] = sorted(layers[c], key=key)

    best = [list(layer) for layer in layers]
    best_count = _crossings(best, dict(column), preds)
    for i in range(ORDER_SWEEPS):
        if best_count == 0:
            break
        if i % 2 == 0:
            sweep(layers, preds, range(1, len(layers)))
        else:
            sweep(layers, succs, range(len(layers) - 2, -1, -1))
        count = _crossings(layers, dict(column), preds)
        if count < best_count:
            best, best_count = [list(layer) for layer in layers], count
    return best


def _pack(
    layer: list[str], desired: dict[str, float], height: dict[str, float]
) -> dict[str, float]:
    """Return tops for *layer*, in its order and ROW_GAP apart, as close to *desired* as allowed."""
    tops: dict[str, float] = {}
    bottom = -math.inf
    for node in layer:
        top = max(desired[node], bottom + ROW_GAP)
        tops[node] = top
        bottom = top + height[node]
    # Spread the push-down evenly: shift the whole column back up by the mean overshoot.
    shift = sum(tops[n] - desired[n] for n in layer) / len(layer) if layer else 0
    return {node: top - shift for node, top in tops.items()}


def arrange_graph(
    nodes: Sequence[NodeBox],
    edges: Iterable[tuple[str, str]],
    origin: Point = (0.0, 0.0),
    initial: Mapping[str, float] | None = None,
) -> dict[str, Point]:
    """Lay out every node by its dataflow and return its top-left position.

    The layout's top-left corner is at *origin*. *initial* gives a starting
    row order per node, e.g. its current y on the canvas. Nodes without any
    edge, e.g. headings, go in a row below the wired ones, unless no node
    is wired, when they go in columns by role.
    """
    if not nodes:
        return {}
    edges = list(edges)
    preds, succs = _graph([n.id for n in nodes], edges)
    wired = [n for n in nodes if preds[n.id] or succs[n.id]]
    unwired = [n for n in nodes if not (preds[n.id] or succs[n.id])]
    if not wired:
        return _arrange_layers(unwired, [], origin, initial)
    positions = _arrange_groups(wired, edges, origin, initial)
    left = min(x for x, _ in positions.values())
    y = max(positions[n.id][1] + n.height for n in wired) + BLOCK_GAP
    step = max(n.width for n in nodes) + COLUMN_GAP
    per_row = max(1, len({x for x, _ in positions.values()}))
    for i in range(0, len(unwired), per_row):
        row = unwired[i : i + per_row]
        for j, node in enumerate(row):
            positions[node.id] = (left + j * step, y)
        y += max(n.height for n in row) + ROW_GAP
    return positions


def _arrange_groups(
    nodes: Sequence[NodeBox],
    edges: list[tuple[str, str]],
    origin: Point,
    initial: Mapping[str, float] | None,
) -> dict[str, Point]:
    """Lay out each connected group of nodes on its own and pack the groups in shelves.

    Groups not wired to each other go side by side, left to right, starting
    a new shelf below once a shelf is about :data:`SHELF_ASPECT` times as
    wide as all the groups would be tall stacked into a square.
    """
    boxes = {n.id: n for n in nodes}
    preds, succs = _graph(list(boxes), edges)
    groups = _components(list(boxes), preds, succs)
    if len(groups) == 1:
        return _arrange_layers(nodes, edges, origin, initial)
    blocks = []
    for group in groups:
        layout = _arrange_layers([boxes[n] for n in group], edges, (0.0, 0.0), initial)
        width = max(x + boxes[n].width for n, (x, _) in layout.items())
        height = max(y + boxes[n].height for n, (_, y) in layout.items())
        blocks.append((layout, width, height))
    area = sum((w + BLOCK_GAP) * (h + BLOCK_GAP) for _, w, h in blocks)
    limit = max(max(w for _, w, _ in blocks), math.sqrt(area) * SHELF_ASPECT)
    positions: dict[str, Point] = {}
    x = y = shelf = 0.0
    for layout, width, height in blocks:
        if x and x + width > limit:
            x, y, shelf = 0.0, y + shelf + BLOCK_GAP, 0.0
        for node, (nx, ny) in layout.items():
            positions[node] = (origin[0] + x + nx, origin[1] + y + ny)
        x += width + BLOCK_GAP
        shelf = max(shelf, height)
    return positions


def _arrange_layers(
    nodes: Sequence[NodeBox],
    edges: list[tuple[str, str]],
    origin: Point,
    initial: Mapping[str, float] | None,
) -> dict[str, Point]:
    boxes = {n.id: n for n in nodes}
    column = assign_columns(nodes, edges)
    layers = order_rows(nodes, edges, column, initial)
    preds, succs = _graph([n.id for n in nodes], edges)
    height = {n.id: n.height for n in nodes}
    width = max(n.width for n in nodes)
    step = width + COLUMN_GAP

    tops: dict[str, float] = {}
    for layer in layers:
        total = sum(height[n] for n in layer) + ROW_GAP * (len(layer) - 1)
        y = -total / 2
        for node in layer:
            tops[node] = y
            y += height[node] + ROW_GAP

    def centre(node):
        return tops[node] + height[node] / 2

    def align(indices, neighbours):
        for c in indices:
            layer = layers[c]
            desired = {}
            for node in layer:
                linked = [centre(n) for n in neighbours[node]]
                desired[node] = (
                    sum(linked) / len(linked) - height[node] / 2 if linked else tops[node]
                )
            tops.update(_pack(layer, desired, height))

    for _ in range(ALIGN_PASSES):
        align(range(1, len(layers)), preds)
        align(range(len(layers) - 2, -1, -1), succs)

    top = min(tops.values())
    return {
        node: (
            origin[0] + column[node] * step + (width - boxes[node].width) / 2,
            origin[1] + tops[node] - top,
        )
        for node in boxes
    }


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Whether two (x, y, width, height) boxes come closer than the row gap."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    gap = ROW_GAP / 2
    return ax < bx + bw + gap and bx < ax + aw + gap and ay < by + bh + gap and by < ay + ah + gap


def _free_below(x: float, y: float, box: NodeBox, placed: list[tuple]) -> float:
    """Return the first y from *y* down at which *box* at *x* overlaps none of *placed*."""
    while True:
        hits = [p for p in placed if _overlaps((x, y, box.width, box.height), p)]
        if not hits:
            return y
        y = max(py + ph for _, py, _, ph in hits) + ROW_GAP


def _components(ids: Sequence[str], preds, succs) -> list[list[str]]:
    seen: set[str] = set()
    groups = []
    for start in ids:
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            group.append(node)
            for other in (*preds[node], *succs[node]):
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        order = {node: i for i, node in enumerate(ids)}
        groups.append(sorted(group, key=order.__getitem__))
    return groups


def place_nodes(
    nodes: Sequence[NodeBox],
    edges: Iterable[tuple[str, str]],
    fixed: Mapping[str, Point],
) -> dict[str, Point]:
    """Return positions for the nodes not in *fixed*, leaving the fixed ones where they are.

    A group of new nodes that is not wired to any fixed node is laid out on
    its own, below everything on the canvas. A new node wired to fixed nodes
    goes in the column its depth gives it, measured from the fixed nodes
    around it, at the height of the nodes it is wired to, and moves up or
    down only as far as it must to overlap nothing.
    """
    edges = list(edges)
    boxes = {n.id: n for n in nodes}
    ids = [n.id for n in nodes]
    fixed = {k: v for k, v in fixed.items() if k in boxes}
    if not fixed:
        return arrange_graph(nodes, edges)
    preds, succs = _graph(ids, edges)
    placed = {node: fixed[node] for node in fixed}
    occupied = [(*fixed[n], boxes[n].width, boxes[n].height) for n in fixed]
    width = max(n.width for n in nodes)
    step = width + COLUMN_GAP
    result: dict[str, Point] = {}

    for group in _components(ids, preds, succs):
        new = [n for n in group if n not in fixed]
        if not new:
            continue
        anchors = [n for n in group if n in fixed]
        if not anchors:
            left = min(x for x, _, _, _ in occupied)
            bottom = max(y + h for _, y, _, h in occupied)
            layout = arrange_graph([boxes[n] for n in group], edges, (left, bottom + BLOCK_GAP))
            for node, point in layout.items():
                result[node] = placed[node] = point
                occupied.append((*point, boxes[node].width, boxes[node].height))
            continue
        column = assign_columns([boxes[n] for n in group], edges)
        known: dict[int, list[float]] = defaultdict(list)
        for node in anchors:
            known[column[node]].append(fixed[node][0])
        xs = {c: sorted(v)[len(v) // 2] for c, v in known.items()}

        def column_x(c, xs=xs):
            if c in xs:
                return xs[c]
            lower = [k for k in xs if k < c]
            upper = [k for k in xs if k > c]
            if lower and upper:
                lo, hi = max(lower), min(upper)
                return xs[lo] + (xs[hi] - xs[lo]) * (c - lo) / (hi - lo)
            if lower:
                return xs[max(lower)] + (c - max(lower)) * step
            return xs[min(upper)] - (min(upper) - c) * step

        def linked(node):
            return [n for n in (*preds[node], *succs[node]) if n in placed]

        remaining = list(new)
        while remaining:
            # New nodes grow out from the fixed ones a wave at a time: those
            # wired to placed nodes go next, stacked per column in the order
            # of the nodes they are wired to, centred on them.
            wave = [n for n in remaining if linked(n)] or remaining[:1]
            for c in sorted({column[n] for n in wave}):

                def desired(node):
                    centres = [placed[n][1] + boxes[n].height / 2 for n in linked(node)]
                    return sum(centres) / len(centres) if centres else None

                group = sorted(
                    (n for n in wave if column[n] == c),
                    key=lambda n: (
                        desired(n) if desired(n) is not None else -math.inf,
                        ids.index(n),
                    ),
                )
                wanted = [d for n in group if (d := desired(n)) is not None]
                total = sum(boxes[n].height for n in group) + ROW_GAP * (len(group) - 1)
                if wanted:
                    y = sum(wanted) / len(wanted) - total / 2
                else:
                    y = min(oy for _, oy, _, _ in occupied)
                for node in group:
                    box = boxes[node]
                    x = column_x(c) + (width - box.width) / 2
                    y = _free_below(x, y, box, occupied)
                    result[node] = placed[node] = (x, y)
                    occupied.append((x, y, box.width, box.height))
                    remaining.remove(node)
                    y += box.height + ROW_GAP
    return result


# ----------------------------------------------------------------------------
# Tiles
# ----------------------------------------------------------------------------

#: The order the kinds of tiles go in, top to bottom.
TILE_KINDS = ("control", "kpi", "chart", "table", "data")
#: The columns of the grid tile widths snap to.
GRID_COLUMNS = 12
#: Kinds sized by their content and packed into rows, by the family that shares rows.
PACKED = {"control": "top", "kpi": "top", "data": "data"}
#: The width, in pixels, a packed tile gets when its component gives none.
TILE_WIDTH = {"control": 360, "kpi": 280, "data": 360}
#: The narrowest a tile of each kind is laid out, in pixels, unless its min_size asks for more.
TILE_MIN_WIDTH = {"control": 240, "kpi": 200, "chart": 420, "data": 280}
#: The fewest columns a leading heading takes when it shares the first row of packed tiles.
HEADING_MIN_COLUMNS = 4
#: The fewest columns a chart or table takes in the room a row of packed tiles leaves.
FILLER_MIN_COLUMNS = 6
#: How many times their own width the tiles of a row may widen to close a gap.
MAX_WIDENING = 2
#: Kinds whose tiles start hidden when the editor places them: the dashboard's plumbing.
HIDDEN_KINDS = frozenset({"data"})
#: A view whose default width is at most this many pixels is laid out as a KPI.
KPI_MAX_WIDTH = 320
#: Tags that make a view a KPI or a table, or a tile text.
KPI_TAGS = frozenset({"kpi", "metric", "indicator"})
TABLE_TAGS = frozenset({"table"})
TEXT_TAGS = frozenset({"text", "heading", "markdown", "notes", "divider"})
#: The width the narrowest breakpoint band is laid out for, e.g. a phone.
NARROWEST_WIDTH = 360
#: The keys of a layout entry the arrangement sets; any other key a tile has is kept.
ARRANGED_KEYS = frozenset({"instance_id", "index", "width", "height"})


@dataclass(frozen=True)
class TileBox:
    """A tile to lay out: its id, kind and the size hints of its component, in pixels."""

    id: str
    kind: str
    default_size: Mapping[str, float] = field(default_factory=dict)
    min_size: Mapping[str, float] = field(default_factory=dict)
    max_size: Mapping[str, float] = field(default_factory=dict)


def tile_kind(
    role: str, tags: Iterable[str] = (), default_size: Mapping | None = None, text: bool = False
) -> str:
    """Return how a tile is laid out, one of :data:`TILE_KINDS` or ``"text"``.

    *role* is the component's role in the palette (see
    :func:`panel_flowdash.ui.roles.component_role`); *text* marks the
    built-in text components.
    """
    tags = {str(tag).strip().lower() for tag in tags}
    if text or tags & TEXT_TAGS:
        return "text"
    if role == "widget":
        return "control"
    if role in ("source", "transform"):
        return "data"
    if tags & TABLE_TAGS:
        return "table"
    if tags & KPI_TAGS:
        return "kpi"
    width = (default_size or {}).get("width")
    if isinstance(width, (int, float)) and width <= KPI_MAX_WIDTH:
        return "kpi"
    return "chart"


def _percent(pixels: float, width: float) -> float:
    return round(min(100.0, pixels * 100 / width), 2)


def _width(columns: int) -> float:
    return round(columns * 100 / GRID_COLUMNS, 2)


def _bounded_height(tile: TileBox, height: float | None) -> float | None:
    if height is None:
        return None
    low, high = tile.min_size.get("height"), tile.max_size.get("height")
    if high is not None:
        height = min(height, high)
    if low is not None:
        height = max(height, low)
    return height


def _column_bounds(tile: TileBox, width: float) -> tuple[int, int]:
    low = max(TILE_MIN_WIDTH.get(tile.kind, 0), tile.min_size.get("width") or 0)
    high = tile.max_size.get("width")
    least = max(1, min(GRID_COLUMNS, math.ceil(low * GRID_COLUMNS / width - 1e-9)))
    most = GRID_COLUMNS if high is None else int(high * GRID_COLUMNS // width)
    return least, max(1, min(GRID_COLUMNS, most))


def _columns(tile: TileBox, width: float) -> int:
    """Return the columns a tile takes at a grid *width*: its default width, within its bounds."""
    least, most = _column_bounds(tile, width)
    pixels = tile.default_size.get("width") or TILE_WIDTH.get(tile.kind, width)
    return max(least, min(most, round(pixels * GRID_COLUMNS / width)))


@dataclass
class _Row:
    tiles: list[TileBox]
    columns: list[int]
    # A row whose tiles keep their own heights, e.g. KPIs beside a chart.
    ragged: bool = False
    # A row whose room is left empty on purpose.
    keep: bool = False

    @property
    def free(self) -> int:
        return GRID_COLUMNS - sum(self.columns)


def _pack_tiles(tiles: list[TileBox], width: float) -> list[_Row]:
    """Fill rows left to right with tiles at their own width."""
    rows: list[_Row] = []
    for tile in tiles:
        columns = _columns(tile, width)
        if not rows or rows[-1].free < columns:
            rows.append(_Row([], []))
        rows[-1].tiles.append(tile)
        rows[-1].columns.append(columns)
    return rows


def _chart_rows(tiles: list[TileBox], width: float) -> list[_Row]:
    """Put charts two to a row where two fit, the odd one out on a row of its own.

    A chart on its own takes at most twice its default width.
    """
    least = max(_column_bounds(t, width)[0] for t in tiles)
    per_row = 2 if least <= GRID_COLUMNS // 2 else 1
    rows = []
    for i in range(0, len(tiles), per_row):
        chunk = tiles[i : i + per_row]
        share = GRID_COLUMNS // len(chunk)
        rows.append(_Row(chunk, [min(share, _widest(t, width)) for t in chunk]))
    return rows


def _widest(tile: TileBox, width: float) -> int:
    """Return the most columns a chart or table takes: twice its default width, within its bounds."""
    least, most = _column_bounds(tile, width)
    pixels = tile.default_size.get("width")
    if pixels:
        most = min(most, max(least, MAX_WIDENING * round(pixels * GRID_COLUMNS / width)))
    return most


def _spread(row: _Row, width: float):
    """Widen the tiles of *row* a column at a time, to at most twice their own width."""
    most = [
        min(_column_bounds(t, width)[1], MAX_WIDENING * c)
        for t, c in zip(row.tiles, row.columns, strict=True)
    ]
    while row.free:
        grown = False
        for i in range(len(row.tiles)):
            if row.free and row.columns[i] < most[i]:
                row.columns[i] += 1
                grown = True
        if not grown:
            return


def _widen_to(row: _Row, total: int):
    """Widen every tile of *row* by a column at a time, alike, until they take *total* columns."""
    while sum(row.columns) < total and sum(row.columns) + len(row.columns) <= GRID_COLUMNS:
        row.columns = [c + 1 for c in row.columns]


def _family(tile: TileBox) -> str:
    return PACKED.get(tile.kind, tile.kind)


def _ordered(tiles: Sequence[TileBox]) -> tuple[list[TileBox], list[TileBox]]:
    """Return the leading text tiles and the rest in display order.

    Kinds go in :data:`TILE_KINDS` order; a text tile stays in front of the
    tile that followed it, and text after the last other tile stays last.
    """
    rank = {kind: i for i, kind in enumerate(TILE_KINDS)}
    sections, text = [], []
    for tile in tiles:
        if tile.kind == "text":
            text.append(tile)
        else:
            sections.append((text, tile))
            text = []
    lead = []
    if sections:
        lead, first = sections[0]
        sections[0] = ([], first)
    else:
        lead, text = text, []
    sections.sort(key=lambda s: rank.get(s[1].kind, len(rank)))
    rest = [t for heading, tile in sections for t in (*heading, tile)]
    return lead, rest + text


def _finish(row: _Row, tile: TileBox | None, width: float) -> bool:
    """Put *tile*, a chart or table, into the room left in a row of packed tiles, if it fits."""
    if tile is None or tile.kind not in ("chart", "table"):
        return False
    least = _column_bounds(tile, width)[0]
    columns = min(row.free, _widest(tile, width))
    if columns < max(least, FILLER_MIN_COLUMNS):
        return False
    row.tiles.append(tile)
    row.columns.append(columns)
    row.ragged = True
    return True


def _pairs_charts(tiles: list[TileBox], j: int) -> bool:
    """Whether the charts from *tiles[j]* on start with a pair, in an even run."""
    k = j
    while k < len(tiles) and tiles[k].kind == "chart":
        k += 1
    return k - j >= 2 and (k - j) % 2 == 0


def _below(row: _Row, tiles: list[TileBox], j: int, width: float) -> bool:
    """Whether no tile after *tiles[j]* fits in the room under the packed tiles of *row*.

    With ``tiles[j]`` beside them in a taller row, the grid would move such a
    tile up under them.
    """
    room = sum(row.columns)
    rest = tiles[j + 1 :]
    k = 0
    while k < len(rest) and rest[k].kind == tiles[j].kind == "chart":
        k += 1
    charts, others = rest[:k], rest[k:]
    widths = (
        [GRID_COLUMNS // 2 if len(charts) > 1 else _widest(charts[0], width)] if charts else []
    )
    widths += [_columns(t, width) if t.kind in PACKED else GRID_COLUMNS for t in others]
    return all(w > room for w in widths)


def _rows_of(
    lead: list[TileBox], tiles: list[TileBox], width: float
) -> tuple[list[TileBox], list[_Row]]:
    """Group tiles in display order into the rows they are shown in.

    Returns the leading text that still needs rows of its own, and the rows.
    A single leading heading finishes the first row of packed tiles; the
    last row of a run of packed tiles takes the chart or table after it into
    the room it leaves.
    """
    rows: list[_Row] = []
    i = 0
    while i < len(tiles):
        tile = tiles[i]
        if tile.kind == "text":
            rows.append(_Row([tile], [GRID_COLUMNS]))
            i += 1
            continue
        j = i
        while j < len(tiles) and tiles[j].kind != "text" and _family(tiles[j]) == _family(tile):
            j += 1
        run = tiles[i:j]
        if tile.kind in PACKED:
            # Controls and KPIs share a row only when they all fit in it, so a kind stays together.
            if sum(_columns(t, width) for t in run) <= GRID_COLUMNS:
                packed = _pack_tiles(run, width)
            else:
                packed = [
                    row
                    for kind in TILE_KINDS
                    if (part := [t for t in run if t.kind == kind])
                    for row in _pack_tiles(part, width)
                ]
            first = packed[0]
            if not rows and len(lead) == 1 and first.free >= HEADING_MIN_COLUMNS:
                first.tiles.insert(0, lead[0])
                first.columns.insert(0, first.free)
                lead = []
            if packed[-1].free and _pairs_charts(tiles, j):
                # Pulling the first chart up would split the pair, so the row
                # widens only until the pair's first chart no longer fits in
                # the room, which stays empty.
                pair = min(GRID_COLUMNS // 2, _widest(tiles[j], width))
                _widen_to(packed[-1], GRID_COLUMNS - pair + 1)
                packed[-1].keep = True
            elif (
                packed[-1].free
                and j < len(tiles)
                and _below(packed[-1], tiles, j, width)
                and _finish(packed[-1], tiles[j], width)
            ):
                j += 1
            rows.extend(packed)
        elif tile.kind == "chart":
            rows.extend(_chart_rows(run, width))
        else:
            rows.extend(_Row([t], [GRID_COLUMNS]) for t in run)
        i = j
    return lead, rows


def _close_gaps(rows: list[_Row], width: float):
    """Widen the tiles of a row of packed tiles that a later tile would otherwise move into.

    The grid moves a tile up into any gap it fits.
    """
    for k, row in enumerate(rows):
        if not row.free or row.ragged or row.keep or row.tiles[0].kind not in PACKED:
            continue
        later = [c for other in rows[k + 1 :] for c in other.columns]
        if later and min(later) <= row.free:
            _spread(row, width)


def arrange_tiles(
    tiles: Sequence[TileBox],
    width: float,
    previous: Mapping[str, Mapping] | None = None,
) -> list[dict]:
    """Lay out *tiles*, given in their current order, for a grid *width* pixels wide.

    Returns one layout entry per tile, in display order, with the tile's
    ``instance_id``, its ``index``, its ``width`` in percent of the row and
    the ``height`` in pixels of the tallest tile of its row; a row without
    height hints gets none, so the grid measures its content. Controls, KPIs
    and the tiles of sources and transforms get the width of their
    ``default_size`` in whole columns, and a chart or table after them fills
    the room left in their last row at its own height. *previous* gives each
    tile's current entry, whose keys other than :data:`ARRANGED_KEYS` are kept.
    """
    lead, rest = _ordered(tiles)
    lead, rows = _rows_of(lead, rest, width)
    _close_gaps(rows, width)
    rows = [_Row([t], [GRID_COLUMNS]) for t in lead] + rows
    previous = previous or {}
    entries: list[dict] = []
    for row in rows:
        heights = [
            _bounded_height(t, t.default_size.get("height")) for t in row.tiles if t.kind != "text"
        ]
        tallest = max((h for h in heights if h is not None), default=None)
        for tile, columns in zip(row.tiles, row.columns, strict=True):
            kept = {
                k: v for k, v in (previous.get(tile.id) or {}).items() if k not in ARRANGED_KEYS
            }
            entry = {**kept, "instance_id": tile.id, "width": _width(columns)}
            if row.ragged or (tile.kind == "text" and len(row.tiles) == 1):
                height = _bounded_height(tile, tile.default_size.get("height"))
            else:
                height = _bounded_height(tile, tallest)
            if height is not None:
                entry["height"] = height
            entry["index"] = len(entries)
            entries.append(entry)
    return entries


def _entry(tile: TileBox, width: float) -> dict:
    """Return the entry of a tile laid out on its own."""
    if tile.kind in PACKED:
        columns = _columns(tile, width)
    else:
        columns = GRID_COLUMNS
    entry: dict = {"instance_id": tile.id, "width": _width(columns)}
    height = _bounded_height(tile, tile.default_size.get("height"))
    if height is not None:
        entry["height"] = height
    return entry


def _pairs_with(tile: TileBox, before: TileBox | None, entry: Mapping, width: float) -> bool:
    """Whether a new chart *tile* can share the row of the chart *before* it, both half wide.

    Only a chart alone in its row, at full width, that the editor laid out
    (*before* is its box) makes room, and only when both fit in half the row.
    """
    if tile.kind != "chart" or before is None or before.kind != "chart":
        return False
    if (entry.get("width") or 100) < 99.99:
        return False
    half = GRID_COLUMNS // 2
    return all(_column_bounds(t, width)[0] <= half for t in (tile, before))


def stack_tiles(
    tiles: Sequence[TileBox], previous: Mapping[str, Mapping] | None = None
) -> list[dict]:
    """Lay out *tiles* one to a row at full width, in the order given, each at its default height."""
    previous = previous or {}
    entries = []
    for index, tile in enumerate(tiles):
        kept = {k: v for k, v in (previous.get(tile.id) or {}).items() if k not in ARRANGED_KEYS}
        entry = {**kept, "instance_id": tile.id, "width": 100.0, "index": index}
        height = _bounded_height(tile, tile.default_size.get("height"))
        if height is not None:
            entry["height"] = height
        entries.append(entry)
    return entries


def band_widths(breakpoints: Sequence[float], labels: Sequence[str]) -> dict[str, float]:
    """Return the width each breakpoint band is laid out for: the narrowest it gets."""
    edges = [0, *sorted(breakpoints)]
    return {label: max(NARROWEST_WIDTH, edge) for label, edge in zip(labels, edges, strict=False)}


def insert_tiles(
    layout: Sequence[dict],
    order: Sequence[str],
    kinds: Mapping[str, str],
    new: Sequence[TileBox],
    width: float,
    previous: Mapping[str, Mapping] | None = None,
    resizable: Mapping[str, TileBox] | None = None,
) -> list[dict]:
    """Add entries for the *new* tiles to a *layout*, leaving the other tiles' sizes alone.

    *order* lists the shown tiles in display order and *kinds* gives the
    kind of each. A new tile goes after the last tile of its kind, or else
    before the first tile of a later kind, at the size of the tile of its
    kind before it when their row has room (a packed tile keeps its own
    width), else in a row of its own. A new chart after a chart alone in
    its row shares that row, both half wide, when the chart before it is
    one of *resizable*, the tiles the editor laid out itself, by id. The
    other entries change only when a tile goes in among them, and then only
    their ``index``, or when such a chart makes room. *previous* gives
    entries the new tiles had, whose keys other than :data:`ARRANGED_KEYS`
    are kept.
    """
    rank = {kind: i for i, kind in enumerate(TILE_KINDS)}
    by_id = {e["instance_id"]: dict(e) for e in layout if "instance_id" in e}
    fresh = {tile.id for tile in new}
    ordered = [
        iid for iid in order if iid not in fresh and by_id.get(iid, {}).get("visible") is not False
    ]
    appended = True
    resized: set[str] = set()
    for tile in new:
        kept = (previous or {}).get(tile.id) or {}
        entry = {k: v for k, v in kept.items() if k not in ARRANGED_KEYS} | _entry(tile, width)
        same = [i for i, iid in enumerate(ordered) if kinds.get(iid) == tile.kind]
        if tile.kind == "text" or not ordered:
            position = len(ordered)
        elif same:
            position = same[-1] + 1
            last = by_id.get(ordered[same[-1]], {})
            row = sum(by_id.get(ordered[i], {}).get("width") or 100 for i in same) % 100
            if tile.kind in PACKED:
                fits = row and row + entry["width"] <= 100.01
            else:
                fits = tile.kind == "chart" and row and row + (last.get("width") or 100) <= 100.01
                if fits:
                    entry["width"] = last.get("width")
                elif _pairs_with(tile, (resizable or {}).get(ordered[same[-1]]), last, width):
                    last = by_id.setdefault(ordered[same[-1]], {"instance_id": ordered[same[-1]]})
                    half = _width(GRID_COLUMNS // 2)
                    heights = [
                        h for h in (last.get("height"), entry.get("height")) if h is not None
                    ]
                    last["width"] = entry["width"] = half
                    if heights:
                        last["height"] = max(heights)
                    resized.add(ordered[same[-1]])
                    fits = True
            if fits and last.get("height") is not None:
                entry["height"] = last["height"]
        else:
            later = [
                i
                for i, iid in enumerate(ordered)
                if rank.get(kinds.get(iid), len(rank)) > rank.get(tile.kind, len(rank))
            ]
            position = later[0] if later else len(ordered)
        appended = appended and position == len(ordered)
        ordered.insert(position, tile.id)
        by_id[tile.id] = entry
        kinds = {**kinds, tile.id: tile.kind}
    indexed = [e["index"] for e in by_id.values() if e.get("index") is not None]
    if appended and not resized:
        start = max(indexed, default=-1) + 1
        added = []
        for offset, tile in enumerate(new):
            entry = by_id[tile.id]
            if indexed:
                entry["index"] = start + offset
            elif entry.keys() <= {"instance_id", "width"} and entry.get("width") == 100:
                # What the grid does for a tile without an entry anyway.
                continue
            added.append(entry)
        return [*(dict(e) for e in layout), *added]
    for index, iid in enumerate(ordered):
        by_id.setdefault(iid, {"instance_id": iid})["index"] = index
    hidden = sorted(
        (e for iid, e in by_id.items() if iid not in ordered),
        key=lambda e: e.get("index", math.inf),
    )
    for index, entry in enumerate(hidden, start=len(ordered)):
        entry["index"] = index
    return [by_id[iid] for iid in ordered] + hidden
