"""Summaries of a running dashboard: the health of each node and samples of its outputs."""

from __future__ import annotations

import json
import math
import typing as t

from panel_flowdash.dataflow_engine import exception_text, failure_text
from panel_flowdash.util import is_failure, missing_inputs
from panel_flowdash.value_text import held_table_names, safe_repr

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

_MAX_TEXT = 200
_MAX_ITEMS = 10
_MAX_COLUMNS = 50
_MAX_ROWS = 20
# Characters of JSON a whole report may take; samples are dropped past it.
_MAX_REPORT = 20_000


def _scalar(value: t.Any) -> t.Any:
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", None) == 0:
        value = item()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            return value[:_MAX_TEXT] + "…"
        return value
    text = safe_repr(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


def sample_value(value: t.Any, rows: int = 5) -> dict[str, t.Any]:
    """Describe *value* in a few lines of JSON: its type, size and the first few entries.

    A DataFrame gives its shape, column dtypes and first *rows* rows as
    records; a sequence its length and first items; anything else its value,
    or, for an object that is not plain data, its type and name.
    """
    if value is None:
        return {"type": None, "value": None}
    if is_failure(value):
        return {"type": "failure", "error": failure_text(value)}
    try:
        import pandas as pd
    except ImportError:
        pd = None
    rows = max(0, min(rows, _MAX_ROWS))
    if pd is not None and isinstance(value, pd.DataFrame):
        shown = value.iloc[:rows, :_MAX_COLUMNS]
        sample = {
            "type": "DataFrame",
            "rows": int(value.shape[0]),
            "columns": {str(c): str(dtype) for c, dtype in shown.dtypes.items()},
            "head": [
                {str(k): _scalar(v) for k, v in record.items()}
                for record in shown.to_dict(orient="records")
            ],
        }
        if value.shape[1] > _MAX_COLUMNS:
            sample["columns_not_shown"] = int(value.shape[1] - _MAX_COLUMNS)
        return sample
    if pd is not None and isinstance(value, pd.Series):
        return {
            "type": "Series",
            "length": len(value),
            "dtype": str(value.dtype),
            "head": [_scalar(v) for v in value.head(rows).tolist()],
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return {
            "type": type(value).__name__,
            "length": len(items),
            "head": [_scalar(v) for v in items[:_MAX_ITEMS]],
        }
    if isinstance(value, dict):
        keys = list(value)
        return {
            "type": "dict",
            "length": len(keys),
            "head": {str(k): _scalar(value[k]) for k in keys[:_MAX_ITEMS]},
        }
    sample = {"type": type(value).__name__, "value": _scalar(value)}
    if tables := held_table_names(value):
        sample["tables"] = tables[:_MAX_ITEMS]
    return sample


def output_failures(editor: FlowDash, instance_id: str) -> dict[str, str]:
    """Return why each output of a placed node failed, by port, for those whose latest run failed.

    That covers an output holding a failure and one whose run raised while
    the port kept its last value: without ``propagate_failures``, and on the
    first run, which happens before the node's inputs are wired.
    """
    item = next(i for i in editor._tile_items if i["instance_id"] == instance_id)
    spec = editor._component_specs.get(item["component_id"])
    state = editor.graph.get_state(instance_id)
    if spec is None or state is None:
        return {}
    raised = editor._output_failures.get(instance_id, {})
    found = {}
    for port in spec.outputs:
        value = getattr(state, port.name, None)
        if is_failure(value):
            found[port.name] = failure_text(value)
        elif port.name in raised:
            found[port.name] = exception_text(raised[port.name])
    return found


def node_health(editor: FlowDash, instance_id: str) -> tuple[str, str | None]:
    """Return ``("ok", None)``, or ``("failed" | "waiting", message)`` for a placed node.

    A node is failed when an input failed upstream and waiting while a
    blocking input is still missing, as :func:`preview_editor` reports it.
    """
    graph = editor.graph
    item = next(i for i in editor._tile_items if i["instance_id"] == instance_id)
    spec = editor._component_specs.get(item["component_id"])
    state = graph.get_state(instance_id)
    failures = graph.get_failures(instance_id)
    held = failures.failed if failures is not None else {}
    if held:
        return "failed", "; ".join(
            f"input '{port}' failed upstream: {failure.text}" for port, failure in held.items()
        )
    waiting = missing_inputs(state, spec.inputs) if state is not None and spec else []
    if waiting:
        return "waiting", f"Waiting for {', '.join(waiting)}."
    return "ok", None


def preview_editor(
    editor: FlowDash,
    names: t.Mapping[str, str],
    *,
    samples: bool = True,
    rows: int = 5,
    invalid: t.Mapping[str, t.Sequence[str]] | None = None,
) -> dict[str, t.Any]:
    """Report the health and output samples of every node placed in *editor*.

    Parameters
    ----------
    editor
        The editor whose dataflow has run.
    names
        Node names by instance id, as in the dashboard's YAML.
    samples
        Whether to include the current value of each output. Leave it off
        for someone the dashboard hides its outputs from.
    rows
        How many rows or items each sample shows, at most 20. A DataFrame
        shows at most 50 columns, and samples are left out once the report
        grows past about 20,000 characters.
    invalid
        Problems the config checks of the components found, by instance id.
        Such a node keeps running and is reported ``invalid``.

    Returns
    -------
    dict
        ``status`` (the worst node status) and ``nodes``, keyed by node name,
        each with a ``status`` of ``ok``, ``waiting``, ``invalid``,
        ``failed``, ``restricted``, ``missing`` or ``error``, a ``message``
        explaining any other status than ``ok`` and, with *samples*,
        ``outputs`` by port. A node is ``failed`` when an input failed
        upstream or one of its own outputs failed, and ``invalid`` when its
        component's config check refuses its config.
    """
    graph = editor.graph
    nodes: dict[str, dict[str, t.Any]] = {}
    for item in editor._tile_items:
        instance_id = item["instance_id"]
        name = names.get(instance_id, instance_id)
        placeholder = item.get("placeholder")
        if placeholder is not None:
            status = {"denied": "restricted", "missing": "missing"}.get(
                placeholder.reason, "error"
            )
            nodes[name] = {"status": status, "message": _scalar(placeholder.message)}
            continue
        spec = editor._component_specs.get(item["component_id"])
        state = graph.get_state(instance_id)
        entry: dict[str, t.Any] = {"component": item["component_id"], "status": "ok"}
        status, message = node_health(editor, instance_id)
        if status == "ok" and (failed := output_failures(editor, instance_id)):
            status = "failed"
            message = "; ".join(f"output '{port}' failed: {text}" for port, text in failed.items())
        problems = (invalid or {}).get(instance_id)
        if problems:
            status, message = "invalid", "; ".join([*problems, *([message] if message else [])])
        if status != "ok":
            entry["status"] = status
            entry["message"] = _scalar(message)
        if samples and spec is not None and state is not None:
            entry["outputs"] = {
                port.name: sample_value(getattr(state, port.name, None), rows)
                for port in spec.outputs
            }
        nodes[name] = entry
    order = ["ok", "waiting", "missing", "restricted", "invalid", "failed", "error"]
    status = max((n["status"] for n in nodes.values()), key=order.index, default="ok")
    size = len(json.dumps(nodes, default=str))
    for entry in reversed(list(nodes.values())):
        if size <= _MAX_REPORT:
            break
        if "outputs" in entry:
            size -= len(json.dumps(entry["outputs"], default=str))
            entry["outputs"] = "omitted, the report is too long; preview fewer nodes"
    return {"status": status, "nodes": nodes}
