"""Summaries of a running dashboard: the health of each node and samples of its outputs."""

from __future__ import annotations

import math
import typing as t

from panel_flowdash.dataflow_engine import failure_text
from panel_flowdash.util import is_failure, missing_inputs

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

_MAX_TEXT = 200
_MAX_ITEMS = 10


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
    text = repr(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


def sample_value(value: t.Any, rows: int = 5) -> dict[str, t.Any]:
    """Describe *value* in a few lines of JSON: its type, size and the first few entries.

    A DataFrame gives its shape, column dtypes and first *rows* rows as
    records; a sequence its length and first items; anything else its value,
    or its truncated ``repr``.
    """
    if value is None:
        return {"type": None, "value": None}
    if is_failure(value):
        return {"type": "failure", "error": failure_text(value)}
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None and isinstance(value, pd.DataFrame):
        head = value.head(rows)
        return {
            "type": "DataFrame",
            "rows": int(value.shape[0]),
            "columns": {str(c): str(dtype) for c, dtype in value.dtypes.items()},
            "head": [
                {str(k): _scalar(v) for k, v in record.items()}
                for record in head.to_dict(orient="records")
            ],
        }
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
    return {"type": type(value).__name__, "value": _scalar(value)}


def preview_editor(
    editor: FlowDash,
    names: t.Mapping[str, str],
    *,
    samples: bool = True,
    rows: int = 5,
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
        How many rows or items each sample shows.

    Returns
    -------
    dict
        ``status`` (the worst node status) and ``nodes``, keyed by node name,
        each with a ``status`` of ``ok``, ``waiting``, ``failed``,
        ``restricted``, ``missing`` or ``error``, a ``message`` explaining any
        other status than ``ok`` and, with *samples*, ``outputs`` by port.
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
            nodes[name] = {"status": status, "message": placeholder.message}
            continue
        spec = editor._component_specs.get(item["component_id"])
        state = graph.get_state(instance_id)
        entry: dict[str, t.Any] = {"component": item["component_id"], "status": "ok"}
        failures = graph.get_failures(instance_id)
        held = failures.failed if failures is not None else {}
        waiting = missing_inputs(state, spec.inputs) if state is not None and spec else []
        if held:
            entry["status"] = "failed"
            entry["message"] = "; ".join(
                f"input '{port}' failed upstream: {failure.text}" for port, failure in held.items()
            )
        elif waiting:
            entry["status"] = "waiting"
            entry["message"] = f"Waiting for {', '.join(waiting)}."
        if samples and spec is not None and state is not None:
            entry["outputs"] = {
                port.name: sample_value(getattr(state, port.name, None), rows)
                for port in spec.outputs
            }
        nodes[name] = entry
    order = ["ok", "waiting", "missing", "restricted", "failed", "error"]
    status = max((n["status"] for n in nodes.values()), key=order.index, default="ok")
    return {"status": status, "nodes": nodes}
