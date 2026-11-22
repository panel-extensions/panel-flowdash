"""Global filters: a dashboard-level filter bar applied to every view that has the column.

A global filter names a column. While it has a value, every view the
dashboard shows (a tile on the grid, or a component in the top bar or the
sidebar) whose data has a column of that name keeps only the matching rows,
without any wiring. A view gets the global filters and whatever its own
inputs already filtered, e.g. by a Filter node feeding it; a view can opt out
of a filter, and a view the filter cannot apply to says so ("Not filtered by
State"). Sources and transforms, which have no tile, are never filtered, so a
Filter node upstream of a view still sees all rows.

In the dashboard YAML the filters are a top-level list, views named as in
``nodes`` (the stored model keeps instance ids, like groups do)::

    filters:
    - column: t_state
      label: State
      widget: select
      value: [TX, OK]
    - column: p_year
      widget: range
      value: [2010, 2020]
      exclude: [fleet_total]

``widget`` is ``select`` (keep rows whose value is one of ``value``) or
``range`` (keep rows between the two bounds of ``value``, either of which may
be null); left out, it follows the column's type. ``value`` is the filter's
starting value, and ``exclude`` lists the views that opt out.

Taking part
-----------

A filter value is a :class:`~panel_flowdash.selection.Predicate`, so a
component sees ``{column: predicate}``. There are two ways to take part:

- Do nothing. FlowDash filters the pandas DataFrames arriving at the inputs
  of a ``Viewer`` component itself, with :func:`filter_frame`, so every view
  reading a DataFrame takes part. Its columns are the columns of those frames.
- Implement the protocol of :class:`FilterTarget`, e.g. to push the
  predicates into a SQL query instead of filtering rows in memory:
  ``__flowdash_columns__()`` returns the columns the component can filter on
  (None while it does not know yet), and ``__flowdash_apply_filters__(filters)``
  receives the predicates that apply to it, by column, whenever they change
  (an empty mapping clears them). Its inputs are then left as they are. An
  optional ``__flowdash_column_values__(column)`` returns the values a
  ``select`` filter offers, or the ``(min, max)`` a ``range`` filter spans,
  for data FlowDash never sees in memory.

Only the filters a component declares a column for reach it, and none it opted
out of. Function components are not filtered: they read their inputs from the
node directly.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import typing as t
from collections.abc import Collection, Iterable, Mapping

import pandas as pd

from panel_flowdash.selection import Predicate

FILTER_KEYS = ("column", "label", "widget", "value", "exclude")
WIDGETS = ("select", "range")
MAX_LABEL_LENGTH = 200
# Keys starting with this are accepted, and ignored, in a filter.
EXTENSION_PREFIX = "x-"
# The most values a select filter offers.
MAX_OPTIONS = 1000


class FilterError(ValueError):
    """A filter's data is malformed. ``key`` names the offending field, e.g. ``filters[1].value``."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@t.runtime_checkable
class FilterTarget(t.Protocol):
    """A component that applies global filters itself, e.g. by pushing them into a query."""

    def __flowdash_columns__(self) -> Iterable[str] | None:
        """Return the columns the component can filter on, or None while it does not know."""

    def __flowdash_apply_filters__(self, filters: Mapping[str, Predicate]) -> None:
        """Apply *filters*, the predicates that apply to the component by column; empty clears them."""


def accepts_filters(component: t.Any) -> bool:
    """Whether *component* applies global filters itself, see :class:`FilterTarget`."""
    return callable(getattr(component, "__flowdash_apply_filters__", None))


# --------------------------------------------------------------------------
# Checking filters
# --------------------------------------------------------------------------


def _scalar(value: t.Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


def _check_value(value: t.Any, widget: str | None, key: str) -> t.Any:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise FilterError(key, f"must be a list, got {value!r}.")
    value = list(value)
    if not all(_scalar(v) for v in value):
        raise FilterError(key, "must hold strings, numbers, booleans or null.")
    if widget == "range":
        if len(value) != 2:
            raise FilterError(key, f"a range takes [start, end], got {value!r}.")
        if any(isinstance(v, bool) for v in value):
            raise FilterError(key, "a range's bounds are numbers, dates or null.")
    return value


def check_filter(value: t.Any, key: str = "filter") -> dict[str, t.Any]:
    """Return a copy of a filter without extension keys, raising :class:`FilterError` if malformed.

    The views in ``exclude`` are checked for their form only: whether they
    exist is up to the caller.
    """
    if not isinstance(value, Mapping):
        raise FilterError(key, f"must be a mapping, not {type(value).__name__}.")
    unknown = [
        k for k in value if k not in FILTER_KEYS and not str(k).startswith(EXTENSION_PREFIX)
    ]
    if unknown:
        raise FilterError(
            key,
            f"unknown key {', '.join(repr(k) for k in unknown)}; expected {', '.join(FILTER_KEYS)}.",
        )
    column = value.get("column")
    if not isinstance(column, str) or not column.strip():
        raise FilterError(f"{key}.column", f"must name a column, got {column!r}.")
    checked: dict[str, t.Any] = {"column": column}
    label = value.get("label")
    if label is not None:
        if not isinstance(label, str):
            raise FilterError(f"{key}.label", f"must be a string, got {label!r}.")
        if len(label) > MAX_LABEL_LENGTH:
            raise FilterError(
                f"{key}.label", f"must be at most {MAX_LABEL_LENGTH} characters long."
            )
        if label.strip():
            checked["label"] = label
    widget = value.get("widget")
    if widget is not None:
        if widget not in WIDGETS:
            raise FilterError(f"{key}.widget", f"must be select or range, got {widget!r}.")
        checked["widget"] = widget
    filter_value = _check_value(value.get("value"), widget, f"{key}.value")
    if filter_value is not None:
        checked["value"] = filter_value
    exclude = value.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, (list, tuple)) or not all(
            isinstance(v, str) and v for v in exclude
        ):
            raise FilterError(f"{key}.exclude", f"must be a list of node names, got {exclude!r}.")
        if exclude:
            checked["exclude"] = list(dict.fromkeys(exclude))
    return checked


def check_filters(value: t.Any) -> list[dict[str, t.Any]]:
    """Return a checked copy of a dashboard's filters, raising :class:`FilterError` if malformed."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise FilterError("filters", f"must be a list, not {type(value).__name__}.")
    filters = [check_filter(f, f"filters[{i}]") for i, f in enumerate(value)]
    seen: set[str] = set()
    for i, entry in enumerate(filters):
        if entry["column"] in seen:
            raise FilterError(
                f"filters[{i}].column", f"filters {entry['column']!r} a second time."
            )
        seen.add(entry["column"])
    return filters


def decode_filters(value: t.Any) -> list[dict[str, t.Any]]:
    """Return the filters a stored dashboard holds, empty if none; ValueError when malformed."""
    return check_filters(value)


def map_filters(filters: Iterable[Mapping] | None, nodes: Mapping[str, str]) -> list[dict]:
    """Return *filters* with the views they exclude renamed by *nodes*, dropping views not in it."""
    mapped = []
    for entry in filters or ():
        entry = copy.deepcopy(dict(entry))
        exclude = [nodes[n] for n in entry.pop("exclude", ()) if n in nodes]
        if exclude:
            entry["exclude"] = exclude
        mapped.append(entry)
    return mapped


def without_nodes(filters: list[dict], instance_ids: Collection[str]) -> list[dict]:
    """Return *filters* no longer excluding the nodes *instance_ids*."""
    return map_filters(
        filters,
        {n: n for entry in filters for n in entry.get("exclude", ()) if n not in instance_ids},
    )


def filter_label(entry: Mapping) -> str:
    """Return the name a filter is shown by: its label, else its column."""
    return entry.get("label") or entry["column"]


# --------------------------------------------------------------------------
# Values and predicates
# --------------------------------------------------------------------------


def widget_for(entry: Mapping, sample: pd.Series | None = None) -> str:
    """Return a filter's widget: its own, else ``range`` for numbers and dates, else ``select``."""
    if entry.get("widget"):
        return entry["widget"]
    if sample is not None and (
        pd.api.types.is_datetime64_any_dtype(sample.dtype)
        or (
            pd.api.types.is_numeric_dtype(sample.dtype)
            and not pd.api.types.is_bool_dtype(sample.dtype)
        )
    ):
        return "range"
    return "select"


def _bound(value: t.Any) -> t.Any:
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def predicate(entry: Mapping, value: t.Any, widget: str | None = None) -> Predicate | None:
    """Return the predicate a filter with *value* applies, or None while it keeps every row.

    A select keeps the rows whose column is one of the values; an empty
    selection keeps them all. A range keeps the rows between its bounds; ISO
    date strings are read as datetimes, and a range without either bound
    keeps every row.
    """
    widget = widget or entry.get("widget") or ("range" if _looks_like_range(value) else "select")
    if value is None:
        return None
    values = list(value)
    if widget == "range":
        if len(values) != 2 or all(v is None for v in values):
            return None
        return Predicate(entry["column"], "range", tuple(_bound(v) for v in values))
    if not values:
        return None
    return Predicate(entry["column"], "in", tuple(values))


def _looks_like_range(value: t.Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(
            v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in value
        )
    )


def frame_columns(values: Iterable[t.Any]) -> set[str]:
    """Return the columns of the DataFrames among *values*."""
    columns: set[str] = set()
    for value in values:
        if isinstance(value, pd.DataFrame):
            columns.update(str(c) for c in value.columns)
    return columns


def filter_frame(frame: t.Any, predicates: Mapping[str, Predicate]) -> t.Any:
    """Return the rows of *frame* matching every predicate on a column it has.

    The reference implementation of global filters, applied to the DataFrame
    inputs of components that do not filter themselves. Anything that is not
    a DataFrame, and a frame without any of the columns, is returned as is.
    """
    if not isinstance(frame, pd.DataFrame) or not predicates:
        return frame
    applicable = [p for column, p in predicates.items() if column in frame.columns]
    if not applicable:
        return frame
    keep = pd.Series(True, index=frame.index)
    for p in applicable:
        keep &= p.mask(frame).fillna(False).astype(bool)
    return frame[keep]


def column_options(frames: Iterable[pd.DataFrame], column: str) -> list[t.Any]:
    """Return the distinct values of *column* across *frames*, sorted, as plain values."""
    values: dict[t.Any, None] = {}
    for frame in frames:
        if column not in frame.columns:
            continue
        for value in frame[column].dropna().unique():
            values[value.item() if hasattr(value, "item") else value] = None
            if len(values) >= MAX_OPTIONS:
                break
    try:
        return sorted(values)
    except TypeError:
        return sorted(values, key=str)


def column_span(frames: Iterable[pd.DataFrame], column: str) -> tuple[t.Any, t.Any] | None:
    """Return the smallest and largest value of *column* across *frames*, or None if it has none."""
    low = high = None
    for frame in frames:
        if column not in frame.columns:
            continue
        series = frame[column].dropna()
        if series.empty:
            continue
        lo, hi = series.min(), series.max()
        low = lo if low is None else min(low, lo)
        high = hi if high is None else max(high, hi)
    if low is None:
        return None
    return low, high


# --------------------------------------------------------------------------
# Which filters reach a view
# --------------------------------------------------------------------------


# Why a global filter does not reach a view.
MISSING = "missing"
EXCLUDED = "excluded"


def resolve_view(
    filters: Iterable[Mapping],
    active: Mapping[str, Predicate],
    node: str,
    columns: Collection[str] | None,
) -> tuple[dict[str, Predicate], dict[str, str]]:
    """Return the predicates that reach the view *node*, and why each other active filter does not.

    *active* are the predicates of the filters with a value, by column, and
    *columns* the view's columns, None while unknown. A filter the view opts
    out of is ``excluded``, and one on a column it lacks is ``missing``.
    """
    applied: dict[str, Predicate] = {}
    skipped: dict[str, str] = {}
    for entry in filters:
        column = entry["column"]
        if column not in active:
            continue
        if node in entry.get("exclude", ()):
            skipped[column] = EXCLUDED
        elif columns is None or column not in columns:
            skipped[column] = MISSING
        else:
            applied[column] = active[column]
    return applied, skipped


def hint_text(filters: Iterable[Mapping], skipped: Mapping[str, str]) -> str:
    """Return the line a view shows for the active filters that do not reach it, e.g. ``Not filtered by State``."""
    labels = [filter_label(f) for f in filters if f["column"] in skipped]
    return f"Not filtered by {', '.join(labels)}" if labels else ""


def hint_detail(filters: Iterable[Mapping], skipped: Mapping[str, str]) -> str:
    """Return why each filter in *skipped* does not reach a view, for its tooltip."""
    reasons = []
    for entry in filters:
        reason = skipped.get(entry["column"])
        if reason == EXCLUDED:
            reasons.append(f"{filter_label(entry)}: this view opts out")
        elif reason == MISSING:
            reasons.append(f"{filter_label(entry)}: its data has no column {entry['column']!r}")
    return "; ".join(reasons)
