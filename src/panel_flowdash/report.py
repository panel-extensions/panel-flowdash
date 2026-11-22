"""Reports: a dashboard read as a document, an ordered list of blocks over the same graph.

A report is a title and a list of blocks. Blocks hold prose and point at
nodes, groups and turns; they never hold data, so the report always shows
the nodes' current outputs. A dashboard has at most one report. In the
dashboard YAML it is a top-level ``report`` mapping, with nodes named as in
``nodes`` (the stored model keeps instance ids, like groups do)::

    report:
      title: Q3 revenue review
      blocks:
      - heading: Summary
      - text: Revenue is {{ total.value:,.0f }} this quarter.
      - node: revenue_kpi
      - group: regional
      - node: churn
        show: table
        caption: Accounts lost, by month
      - turn: t9f8e7d6c

A node may be in the report without having a tile. Removing a node removes
the blocks that show it, and turns the report refers to are kept when old
turns are trimmed. A dashboard without a report reads as the one
:func:`report_blocks` derives from its groups and tile layout.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
import fractions
import re
import typing as t
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from panel_flowdash.groups import GroupError, check_group_id
from panel_flowdash.turns import TURN_ID_PATTERN

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

REPORT_KEYS = ("title", "blocks")
# The key that gives a block its kind, and the keys each kind may add.
BLOCK_KINDS = ("heading", "text", "node", "group", "turn")
BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "heading": ("level",),
    "text": (),
    "node": ("show", "caption", "height"),
    "group": (),
    "turn": (),
}
# Keys every block may have: the turn that wrote it, and a page break before it.
COMMON_BLOCK_KEYS = ("origin", "page_break")
# How a node block shows its node: as its tile does, or its first output as a table.
SHOW_CHOICES = ("view", "table")
HEADING_LEVELS = (1, 2, 3)
MAX_TITLE_LENGTH = 200
MAX_CAPTION_LENGTH = 500
# Keys starting with this are accepted, and ignored, in a report and its blocks.
EXTENSION_PREFIX = "x-"


class ReportError(ValueError):
    """A report's data is malformed. ``key`` names the offending field, e.g. ``blocks[2].node``."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


def block_kind(block: Mapping) -> str:
    """Return the kind of a checked block, one of :data:`BLOCK_KINDS`."""
    return next(kind for kind in BLOCK_KINDS if kind in block)


def _string(value: t.Any, key: str, limit: int | None = None, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ReportError(key, f"must be a string, got {value!r}.")
    if not empty and not value.strip():
        raise ReportError(key, "must not be empty.")
    if limit is not None and len(value) > limit:
        raise ReportError(key, f"must be at most {limit} characters long.")
    return value


def _turn_id(value: t.Any, key: str) -> str:
    if not isinstance(value, str) or not TURN_ID_PATTERN.fullmatch(value):
        raise ReportError(key, f"must be a turn id, got {value!r}.")
    return value


def check_block(block: t.Any, key: str = "block") -> dict[str, t.Any]:
    """Return a copy of *block* without extension keys, raising :class:`ReportError` if malformed.

    References are checked for their form only: whether a node, group or
    turn of that name exists is up to the caller.
    """
    if not isinstance(block, Mapping):
        raise ReportError(key, f"must be a mapping, not {type(block).__name__}.")
    kinds = [kind for kind in BLOCK_KINDS if kind in block]
    if len(kinds) != 1:
        found = f"got {', '.join(kinds)}" if kinds else "got none"
        raise ReportError(key, f"must have exactly one of {', '.join(BLOCK_KINDS)}; {found}.")
    kind = kinds[0]
    allowed = (kind, *BLOCK_KEYS[kind], *COMMON_BLOCK_KEYS)
    unknown = [k for k in block if k not in allowed and not str(k).startswith(EXTENSION_PREFIX)]
    if unknown:
        raise ReportError(
            key,
            f"unknown key {', '.join(repr(k) for k in unknown)} for a {kind} block; "
            f"expected {', '.join(allowed)}.",
        )
    value = block[kind]
    if kind in ("heading", "text"):
        _string(value, f"{key}.{kind}", empty=kind == "text")
    elif kind == "node":
        if not isinstance(value, str) or not value:
            raise ReportError(f"{key}.node", f"must name a node, got {value!r}.")
    elif kind == "group":
        try:
            check_group_id(value)
        except GroupError as exc:
            raise ReportError(f"{key}.group", exc.reason) from None
    else:
        _turn_id(value, f"{key}.turn")
    checked: dict[str, t.Any] = {kind: value}
    level = block.get("level")
    if level is not None:
        if isinstance(level, bool) or level not in HEADING_LEVELS:
            raise ReportError(f"{key}.level", f"must be 1, 2 or 3, got {level!r}.")
        checked["level"] = level
    show = block.get("show")
    if show is not None:
        if show not in SHOW_CHOICES:
            raise ReportError(f"{key}.show", f"must be view or table, got {show!r}.")
        checked["show"] = show
    caption = block.get("caption")
    if caption is not None:
        checked["caption"] = _string(caption, f"{key}.caption", MAX_CAPTION_LENGTH)
    height = block.get("height")
    if height is not None:
        if (
            isinstance(height, bool)
            or not isinstance(height, (int, float))
            or not height > 0
            or height == float("inf")
        ):
            raise ReportError(
                f"{key}.height", f"must be a positive number of pixels, got {height!r}."
            )
        checked["height"] = height
    origin = block.get("origin")
    if origin is not None:
        checked["origin"] = _turn_id(origin, f"{key}.origin")
    page_break = block.get("page_break")
    if page_break is not None:
        if not isinstance(page_break, bool):
            raise ReportError(f"{key}.page_break", f"must be true or false, got {page_break!r}.")
        if page_break:
            checked["page_break"] = True
    return checked


def check_report(value: t.Any) -> dict[str, t.Any]:
    """Return a checked copy of a report, raising :class:`ReportError` if malformed."""
    if not isinstance(value, Mapping):
        raise ReportError("report", f"must be a mapping, not {type(value).__name__}.")
    unknown = [
        k for k in value if k not in REPORT_KEYS and not str(k).startswith(EXTENSION_PREFIX)
    ]
    if unknown:
        raise ReportError(
            "report",
            f"unknown key {', '.join(repr(k) for k in unknown)}; expected {', '.join(REPORT_KEYS)}.",
        )
    report: dict[str, t.Any] = {}
    title = value.get("title")
    if title is not None:
        report["title"] = _string(title, "title", MAX_TITLE_LENGTH)
    blocks = value.get("blocks")
    if blocks is None:
        blocks = []
    if not isinstance(blocks, list):
        raise ReportError("blocks", f"must be a list, not {type(blocks).__name__}.")
    report["blocks"] = [check_block(block, f"blocks[{i}]") for i, block in enumerate(blocks)]
    return report


def decode_report(value: t.Any) -> dict[str, t.Any] | None:
    """Return the report a stored dashboard holds, None if it has none; ValueError when malformed."""
    if value is None:
        return None
    return check_report(value)


def map_report(
    report: Mapping | None,
    nodes: Mapping[str, str],
    groups: Collection[str],
    turns: Collection[str],
) -> dict[str, t.Any] | None:
    """Return *report* with its node references renamed by *nodes*, dropping what it cannot keep.

    Blocks showing a node not in *nodes*, a group not in *groups* or a turn
    not in *turns* are left out, and so is an ``origin`` naming no turn in
    *turns*. Passing an identity mapping prunes a report to what a dashboard holds.
    """
    if report is None:
        return None
    blocks = []
    for block in report.get("blocks") or []:
        kind = block_kind(block)
        if kind == "node" and block["node"] not in nodes:
            continue
        if (kind == "group" and block["group"] not in groups) or (
            kind == "turn" and block["turn"] not in turns
        ):
            continue
        entry = copy.deepcopy(dict(block))
        if kind == "node":
            entry["node"] = nodes[block["node"]]
        if entry.get("origin") is not None and entry["origin"] not in turns:
            del entry["origin"]
        blocks.append(entry)
    mapped = {key: copy.deepcopy(report[key]) for key in ("title",) if report.get(key) is not None}
    mapped["blocks"] = blocks
    return mapped


def without_nodes(report: Mapping | None, instance_ids: Collection[str]) -> dict | None:
    """Return *report* without the blocks showing the nodes *instance_ids*, or *report* if none do."""
    if report is None or not any(
        block.get("node") in instance_ids for block in report.get("blocks") or []
    ):
        return report  # type: ignore[return-value]
    return {
        **copy.deepcopy(dict(report)),
        "blocks": [
            copy.deepcopy(block)
            for block in report.get("blocks") or []
            if block.get("node") not in instance_ids
        ],
    }


def report_turns(report: Mapping | None) -> set[str]:
    """Return the ids of the turns a report refers to, by turn blocks and origins."""
    turns: set[str] = set()
    for block in (report or {}).get("blocks") or []:
        turns.update(v for v in (block.get("turn"), block.get("origin")) if v is not None)
    return turns


# --------------------------------------------------------------------------
# Changing a report's blocks
# --------------------------------------------------------------------------


def _blocks(report: Mapping | None) -> list[dict[str, t.Any]]:
    return copy.deepcopy(list((report or {}).get("blocks") or []))


def _with_blocks(report: Mapping | None, blocks: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    return {**copy.deepcopy(dict(report or {})), "blocks": blocks}


def _index(blocks: list, index: int, key: str = "index") -> int:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(blocks):
        raise ReportError(
            key, f"must be the index of a block, 0 to {len(blocks) - 1}, got {index!r}."
        )
    return index


def insert_block(report: Mapping | None, index: int | None, block: Mapping) -> dict[str, t.Any]:
    """Return a copy of *report* with *block* inserted before the block at *index*, or last for None."""
    blocks = _blocks(report)
    if index is None:
        index = len(blocks)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(blocks):
        raise ReportError("index", f"must be 0 to {len(blocks)}, got {index!r}.")
    blocks.insert(index, check_block(block))
    return _with_blocks(report, blocks)


def move_block(report: Mapping | None, index: int, to: int) -> dict[str, t.Any]:
    """Return a copy of *report* with the block at *index* moved so that it ends up at index *to*."""
    blocks = _blocks(report)
    _index(blocks, index)
    _index(blocks, to, "to")
    blocks.insert(to, blocks.pop(index))
    return _with_blocks(report, blocks)


def remove_block(report: Mapping | None, index: int) -> dict[str, t.Any]:
    """Return a copy of *report* without the block at *index*."""
    blocks = _blocks(report)
    del blocks[_index(blocks, index)]
    return _with_blocks(report, blocks)


def update_block(report: Mapping | None, index: int, **changes: t.Any) -> dict[str, t.Any]:
    """Return a copy of *report* with keys of the block at *index* changed; a None value removes the key."""
    blocks = _blocks(report)
    block = blocks[_index(blocks, index)]
    for key, value in changes.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    blocks[index] = check_block(block, f"blocks[{index}]")
    return _with_blocks(report, blocks)


def set_title(report: Mapping | None, title: str | None) -> dict[str, t.Any]:
    """Return a copy of *report* titled *title*; an empty or None title leaves it the dashboard's."""
    updated = _with_blocks(report, _blocks(report))
    if title is None or not str(title).strip():
        updated.pop("title", None)
    else:
        updated["title"] = _string(str(title).strip(), "title", MAX_TITLE_LENGTH)
    return updated


# --------------------------------------------------------------------------
# Reading a report from an editor
# --------------------------------------------------------------------------


@dataclass
class ResolvedReport:
    """The report an editor shows: the authored one, or the one derived from the dashboard.

    Attributes
    ----------
    title
        The report's title, the dashboard's when the report gives none.
    blocks
        The blocks, with nodes by instance id, as :func:`check_block` returns them.
    filters
        The instance ids of the controls the report's values depend on, in
        their order beside the grid, listed as the filters applied.
    derived
        Whether the dashboard has no report of its own and *blocks* were derived.
    """

    title: str
    blocks: list[dict[str, t.Any]] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    derived: bool = False


def _ordered_items(editor: FlowDash) -> list[dict]:
    """Return the placed items shown on the dashboard, top bar and sidebar first, then the grid."""
    from panel_flowdash.ui import tile_layout

    entries = tile_layout._entries(editor)
    shown = []
    for area in (tile_layout.TOP, tile_layout.SIDEBAR, tile_layout.GRID):
        shown += [
            item
            for item in tile_layout.area_items(editor, area, entries)
            if entries.get(item["instance_id"], {}).get("visible") is not False
        ]
    return shown


def _is_control(editor: FlowDash, item: dict) -> bool:
    return "placeholder" not in item and editor._tile_box(item).kind == "control"


def group_members(editor: FlowDash, group_id: str) -> list[str]:
    """Return the shown members of a group in the order the dashboard shows them, controls left out."""
    group = editor._groups.get(group_id)
    if group is None:
        return []
    members = set(group.nodes)
    return [
        item["instance_id"]
        for item in _ordered_items(editor)
        if item["instance_id"] in members and not _is_control(editor, item)
    ]


def dashboard_nodes(editor: FlowDash) -> set[str]:
    """Return the instance ids of the nodes the dashboard shows, controls included."""
    return {item["instance_id"] for item in _ordered_items(editor)}


def turn_outputs(editor: FlowDash, turn_id: str) -> list[str]:
    """Return the nodes a turn made that no other node it made reads from, in dashboard order.

    They are the turn's answer, which its block in the report shows.
    """
    from panel_flowdash.ui import tile_layout

    made = [
        item["instance_id"]
        for item in editor._tile_items
        if editor.node_origin(item["instance_id"]) == turn_id
    ]
    feeding = {
        edge["source"]
        for edge in editor._dataflow_graph.edges
        if edge["source"] in made and edge["target"] in made
    }
    leaves = [iid for iid in made if iid not in feeding]
    order = {
        item["instance_id"]: i
        for i, item in enumerate(tile_layout.area_items(editor, tile_layout.GRID))
    }
    return sorted(leaves, key=lambda iid: order.get(iid, len(order)))


def report_blocks(editor: FlowDash) -> ResolvedReport:
    """Return the report *editor*'s dashboard reads as, authored or derived.

    A dashboard with a report gives its blocks, without those referring to
    nodes, groups or turns it no longer has. One without derives them from
    the dashboard: a group block for each group with a shown node, where its
    first tile is, and a node block for every other shown tile, heading and
    Markdown tiles included, in the order of the top bar, the sidebar and
    the grid. Controls are not blocks; they are the report's filters.
    """
    current = editor.dashboard
    title = current.title if current is not None else "Untitled"
    shown = _ordered_items(editor)
    controls = [item for item in editor._tile_items if _is_control(editor, item)]
    order = {item["instance_id"]: i for i, item in enumerate(shown)}
    filters = [
        item["instance_id"]
        for item in sorted(controls, key=lambda item: order.get(item["instance_id"], len(order)))
    ]
    report = editor.report
    if report is not None:
        return ResolvedReport(report.get("title") or title, report["blocks"], filters)
    membership = {iid: g.id for g in editor._groups.values() for iid in g.nodes}
    blocks: list[dict[str, t.Any]] = []
    placed_groups: set[str] = set()
    for item in shown:
        if _is_control(editor, item):
            continue
        group_id = membership.get(item["instance_id"])
        if group_id is None:
            blocks.append({"node": item["instance_id"]})
        elif group_id not in placed_groups:
            placed_groups.add(group_id)
            blocks.append({"group": group_id})
    return ResolvedReport(title, blocks, filters, derived=True)


# --------------------------------------------------------------------------
# Live values in text blocks
# --------------------------------------------------------------------------

# ``{{ <node>.<port> }}``, ``{{ <node>.<port>.<key> }}`` and a format spec after a colon.
PLACEHOLDER = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][\w-]*)(?:\.([\w-]+))?\s*(?::([^{}]*?))?\s*\}\}"
)

# Exactly these types: a subclass may format itself however it likes.
_PLAIN_TYPES = (
    str,
    bool,
    int,
    float,
    decimal.Decimal,
    fractions.Fraction,
    dt.date,
    dt.datetime,
    dt.time,
    dt.timedelta,
)
_PANDAS_SCALARS = ("Timestamp", "Timedelta")
_MAX_PLAIN_DEPTH = 3


def _unwrap(value: t.Any) -> t.Any:
    """Return a NumPy scalar as the Python value it holds; anything else as it is."""
    if type(value).__module__ == "numpy" and getattr(value, "ndim", None) == 0:
        try:
            return value.item()
        except Exception:
            return value
    return value


def _plain(value: t.Any, depth: int = 0) -> bool:
    """Whether *value* is data a placeholder may write out: text, numbers, dates and lists or mappings of them.

    Any other object, one only its own ``str``, ``repr`` or ``format`` could
    describe, is never written out, since those can show whatever it holds.
    """
    value = _unwrap(value)
    kind = type(value)
    if value is None or kind in _PLAIN_TYPES:
        return True
    if kind.__module__.startswith("pandas") and kind.__name__ in _PANDAS_SCALARS:
        return True
    if depth >= _MAX_PLAIN_DEPTH:
        return False
    if kind in (list, tuple):
        return all(_plain(v, depth + 1) for v in value)
    if kind is dict:
        return all(_plain(k, depth + 1) and _plain(v, depth + 1) for k, v in value.items())
    return False


def _lookup(value: t.Any, key: str | None) -> t.Any:
    if key is None:
        return value
    if type(value) is dict:
        return value.get(key)
    if type(value).__module__.startswith("pandas") and type(value).__name__ == "Series":
        try:
            return value.get(key)
        except Exception:
            return None
    return None


def placeholder_text(value: t.Any, spec: str = "") -> str:
    """Return *value* as a placeholder writes it in a report: plain data formatted, else its type's name.

    Text, numbers, dates and lists and mappings of them are formatted as the
    Markdown component formats them, by *spec* if it applies. Any other
    object gives only its type's name, so a value that holds a credential
    never shows it. Prose reads two values as a range ("1981 to 2022"), and a
    whole number from 1000 to 9999 as a year, without a thousands separator,
    unless *spec* says otherwise.
    """
    from panel_flowdash.text_components import format_value
    from panel_flowdash.util import is_failure

    value = _unwrap(value)
    if value is None or is_failure(value):
        return ""
    if not _plain(value):
        return type(value).__name__
    if type(value) in (list, tuple) and len(value) == 2:
        ends = [_unwrap(v) for v in value]
        if all(_scalar(v) for v in ends):
            return " to ".join(placeholder_text(v, spec) for v in ends)
    if not spec and type(value) is int and 1000 <= value <= 9999:
        return str(value)
    return format_value(value, spec)


def _scalar(value: t.Any) -> bool:
    return value is not None and type(value) not in (list, tuple, dict)


def output_value(
    editor: FlowDash, name: str, port: str, ids: t.Mapping[str, str] | None = None
) -> t.Any:
    """Return the current value of output *port* of the node called *name*, None if it has none to show.

    None too when there is no such node or output, when the node is one the
    editor's user may not use, or when the output failed. *ids* maps node
    names to instance ids, to look up many outputs without naming the nodes each time.
    """
    from panel_flowdash.agent.preview import output_failures

    instance_id = (_node_ids(editor) if ids is None else ids).get(name)
    if instance_id is None or editor._node_restricted(instance_id):
        return None
    item = next(i for i in editor._tile_items if i["instance_id"] == instance_id)
    spec = editor._component_specs.get(item["component_id"])
    if spec is None:
        return None
    resolved = spec.resolve_name(port)
    if not any(p.name == resolved for p in spec.outputs):
        return None
    if resolved in output_failures(editor, instance_id):
        return None
    state = editor.graph.get_state(instance_id)
    return getattr(state, resolved, None) if state is not None else None


def _node_ids(editor: FlowDash) -> dict[str, str]:
    from panel_flowdash.agent.yaml_format import node_names

    return {name: iid for iid, name in node_names(editor._tile_items).items()}


def _filler(editor: FlowDash) -> Callable[[re.Match], str]:
    ids = _node_ids(editor)

    def fill(match: re.Match) -> str:
        name, port, key, spec = match.groups()
        value = _lookup(output_value(editor, name, port, ids), key)
        return placeholder_text(value, (spec or "").strip())

    return fill


def render_report_text(editor: FlowDash, text: str, value_class: str | None = None) -> str:
    """Render a text block as HTML, filling its ``{{ node.port }}`` placeholders from current outputs.

    Placeholders are filled as the Markdown component fills its own: after
    the Markdown is parsed, so a value is literal text, and never in a link or
    image URL. A placeholder naming no node or output, a restricted node or a
    failed output is left empty. With *value_class* each value in the text is
    a ``<span>`` of that class, which the Report view shows as a chip.
    """
    from panel_flowdash.text_components import markdown_with_placeholders

    return markdown_with_placeholders(text, PLACEHOLDER, _filler(editor), value_class)


def fill_report_text(editor: FlowDash, text: str) -> str:
    """Return a text block's Markdown with its placeholders filled, each value escaped as literal text."""
    from panel_flowdash.text_components import fill_placeholders
    from panel_flowdash.ui.text import escape_markdown

    return fill_placeholders(text, PLACEHOLDER, _filler(editor), escape_markdown)


def placeholder_refs(text: str) -> Iterable[tuple[str, str]]:
    """Yield the ``(node, port)`` each placeholder of a text block reads."""
    for match in PLACEHOLDER.finditer(text or ""):
        yield match.group(1), match.group(2)


__all__ = [
    "BLOCK_KEYS",
    "BLOCK_KINDS",
    "COMMON_BLOCK_KEYS",
    "REPORT_KEYS",
    "ReportError",
    "ResolvedReport",
    "check_block",
    "check_report",
    "dashboard_nodes",
    "decode_report",
    "fill_report_text",
    "group_members",
    "insert_block",
    "map_report",
    "move_block",
    "placeholder_text",
    "remove_block",
    "render_report_text",
    "report_blocks",
    "report_turns",
    "set_title",
    "turn_outputs",
    "update_block",
    "without_nodes",
]
