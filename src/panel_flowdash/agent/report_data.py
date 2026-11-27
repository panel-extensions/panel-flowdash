"""What the report's placeholders can read: the data catalogue the agent's get_report adds.

Whether an output is data is judged by its current value, as placeholders
judge it: text, numbers, dates and lists or mappings of them, or a table. An
output holding any other object, such as a data source or a pipeline, is
never offered, so the catalogue needs nothing declared per port. A node with
no data of its own that reads another node, such as a chart, is mapped to
the nearest node upstream that has data, following the input its
:class:`~panel_flowdash.report.PlaceholderHint` names first.
"""

from __future__ import annotations

import re
import typing as t
from collections import deque

from panel_flowdash.report import _plain, output_value, placeholder_text
from panel_flowdash.text_fill import _frame

if t.TYPE_CHECKING:
    from .ops import DashboardOps

#: The most characters the catalogue takes; nodes past it are counted, not described.
MAX_CATALOGUE = 6000
# Sample rows and columns shown per table, and the longest cell text.
_SAMPLE_ROWS = 3
_SAMPLE_COLUMNS = 6
_CELL = 40
# How far upstream a view is followed to its data.
_MAX_DEPTH = 12
# A column a placeholder can name with dot syntax, as the placeholder pattern reads it.
_ADDRESSABLE = re.compile(r"[\w-]+")


def _visible(ops: DashboardOps) -> dict[str, str]:
    """Return the names of the nodes the user may read, by instance id."""
    editor = ops.editor
    return {iid: name for iid, name in ops._names().items() if not editor._node_restricted(iid)}


def data_outputs(
    ops: DashboardOps, name: str, names: t.Mapping[str, str] | None = None
) -> list[tuple[str, t.Any]]:
    """Return ``(port, value)`` for each output of node *name* holding data a placeholder can show.

    *names* maps the instance ids of the nodes the user may read to their
    names, to look at many nodes without working it out each time.
    """
    editor = ops.editor
    ids = {n: iid for iid, n in (_visible(ops) if names is None else names).items()}
    instance_id = ids.get(name)
    if instance_id is None:
        return []
    item = ops._item(instance_id)
    spec = editor._component_specs.get(item["component_id"])
    if spec is None:
        return []
    found = []
    for port in spec.outputs:
        value = output_value(editor, name, port.name, ids)
        if value is None:
            continue
        if _frame(value) or _plain(value):
            found.append((port.name, value))
    return found


def data_upstream(ops: DashboardOps, name: str, first: str | None = None) -> str | None:
    """Return the nearest node upstream of *name* with data outputs, None without one.

    With *first*, e.g. a hint's ``upstream``, the nodes feeding that input
    are searched before the node's other inputs. Restricted nodes are
    neither returned nor walked through.
    """
    names = _visible(ops)
    ids = {n: iid for iid, n in names.items()}
    if name not in ids:
        return None
    edges = ops.editor._dataflow_graph.edges

    def walk(port: str | None) -> str | None:
        seen = {ids[name]}
        queue: deque[tuple[str, int]] = deque([(ids[name], 0)])
        while queue:
            target, depth = queue.popleft()
            if depth >= _MAX_DEPTH:
                continue
            for edge in edges:
                source = edge["source"]
                if edge["target"] != target or source not in names or source in seen:
                    continue
                if depth == 0 and port is not None and edge["target_port"] != port:
                    continue
                seen.add(source)
                if data_outputs(ops, names[source], names):
                    return names[source]
                queue.append((source, depth + 1))
        return None

    return (walk(first) if first is not None else None) or walk(None)


def _columns(frame: t.Any) -> tuple[list[str], list[str]]:
    """Return the columns of *frame* placeholders can name, and those they cannot."""
    columns = [str(c) for c in frame.columns]
    addressable = [c for c in columns if _ADDRESSABLE.fullmatch(c)]
    return addressable, [c for c in columns if c not in addressable]


def example(ops: DashboardOps, name: str) -> str | None:
    """Return a placeholder reading node *name*'s data, e.g. ``{{ sales.data.region }}``."""
    outputs = data_outputs(ops, name)
    for port, value in outputs:
        if _frame(value):
            addressable, _ = _columns(value)
            if addressable:
                return f"{{{{ {name}.{port}.{addressable[0]} }}}}"
    for port, value in outputs:
        if not _frame(value):
            return f"{{{{ {name}.{port} }}}}"
    return None


def _cell(value: t.Any) -> str:
    text = " ".join(placeholder_text(value).split())
    return text if len(text) <= _CELL else text[: _CELL - 1] + "…"


def _data_entry(ops: DashboardOps, name: str, title: str) -> str:
    lines = [f"- {name} ({title})"]
    for port, value in data_outputs(ops, name):
        if not _frame(value):
            lines.append(f"  {{{{ {name}.{port} }}}} is now {_cell(value)}")
            continue
        addressable, others = _columns(value)
        shown = addressable[:_SAMPLE_COLUMNS]
        lines.append(
            f"  {name}.{port}: a table of {len(value)} rows; "
            + (
                "e.g. "
                + ", ".join(f"{{{{ {name}.{port}.{c} }}}}" for c in shown[:3])
                + (
                    f", {{{{#each {name}.{port} limit=5}}}}...{{{{/each}}}}"
                    if len(value) > 1
                    else ""
                )
                if shown
                else "no column a placeholder can name"
            )
        )
        if others:
            lines.append(
                "  columns placeholders cannot name (rename them in the query): "
                + ", ".join(repr(c) for c in others[:_SAMPLE_COLUMNS])
            )
        if shown and len(value):
            lines.append("    " + " | ".join(shown))
            for i in range(min(len(value), _SAMPLE_ROWS)):
                row = value.iloc[i]
                lines.append("    " + " | ".join(_cell(row[c]) for c in shown))
    return "\n".join(lines)


def report_catalogue(ops: DashboardOps) -> str | None:
    """Return what the report's placeholders can read on the open dashboard, None if nothing.

    Lists each node that shows another node's data with the node holding it,
    then each node with data outputs: its current values, or a table's rows,
    columns and first rows, with placeholders that read them. Restricted nodes
    are left out, and the text stops near :data:`MAX_CATALOGUE` characters.
    """
    from panel_flowdash.ui.tile_layout import tile_title

    editor = ops.editor
    names = _visible(ops)
    items = {item["instance_id"]: item for item in editor._tile_items}
    with_data = [name for name in names.values() if data_outputs(ops, name, names)]
    fed = {e["target"] for e in editor._dataflow_graph.edges}
    views = []
    for iid, name in names.items():
        if name in with_data or iid not in fed:
            continue
        hint = ops.node_placeholder_hint(name)
        upstream = data_upstream(ops, name, hint.upstream if hint is not None else None)
        if upstream is None:
            continue
        line = f"- {name} ({tile_title(editor, items[iid])}) reads its data from {upstream}"
        if hint is not None and hint.note:
            line += f"; {hint.note}"
        views.append(line)
    entries = [
        _data_entry(ops, name, tile_title(editor, items[iid]))
        for iid, name in names.items()
        if name in with_data
    ]
    sections = []
    budget = MAX_CATALOGUE
    for heading, lines in (
        ("Nodes without data of their own; write placeholders for the node they read", views),
        ("Data placeholders can read", entries),
    ):
        kept = []
        for i, line in enumerate(lines):
            if kept and len(line) > budget:
                kept.append(f"- ... and {len(lines) - i} more")
                break
            kept.append(line)
            budget -= len(line)
        if kept:
            sections.append(f"{heading}:\n" + "\n".join(kept))
    return "\n\n".join(sections) or None


__all__ = ["MAX_CATALOGUE", "data_outputs", "data_upstream", "example", "report_catalogue"]
