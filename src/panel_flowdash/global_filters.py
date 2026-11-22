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

``__flowdash_apply_filters__`` may return the filters it could not apply, as
``{column: reason}``, e.g. a column its query has but whose type the filter
does not suit; the tile then says "Not filtered by ..." with the reason, as
it does when it raises. A component whose columns change after it was
placed, e.g. when a new query arrives at an input that is not a DataFrame,
calls :func:`request_filters` to have them read again and its filters handed
over anew.

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

import numpy as np
import pandas as pd

from panel_flowdash.selection import Predicate

FILTER_KEYS = ("column", "label", "widget", "value", "exclude")
WIDGETS = ("select", "range")
MAX_LABEL_LENGTH = 200
# Keys starting with this are accepted, and ignored, in a filter.
EXTENSION_PREFIX = "x-"
# The most values a select filter offers, and keeps: every view re-runs with
# the whole list on each change, so a larger one would cost seconds per view.
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

    def __flowdash_apply_filters__(
        self, filters: Mapping[str, Predicate]
    ) -> Mapping[str, str] | None:
        """Apply *filters*, the predicates that apply to the component by column; empty clears them.

        Returns the filters the component could not apply, as ``{column: reason}``, or None.
        """


def accepts_filters(component: t.Any) -> bool:
    """Whether *component* applies global filters itself, see :class:`FilterTarget`."""
    return callable(getattr(component, "__flowdash_apply_filters__", None))


def request_filters(component: t.Any) -> bool:
    """Have the dashboards showing *component* read its columns again and hand it its filters anew.

    A :class:`FilterTarget` calls this when ``__flowdash_columns__`` would
    now answer differently, e.g. once a query arriving at one of its inputs
    has run. Returns whether a dashboard shows *component*.
    """
    from panel_flowdash.ui.global_filters import GlobalFilters

    return GlobalFilters.request(component)


def skipped_filters(result: t.Any, filters: Collection[str]) -> dict[str, str]:
    """Return what ``__flowdash_apply_filters__`` reported it skipped, by column, as text.

    *result* is a mapping ``{column: reason}`` or an iterable of
    ``(column, reason)`` pairs; columns not among *filters*, the ones handed
    over, are dropped.
    """
    if result is None:
        return {}
    if isinstance(result, Mapping):
        items = result.items()
    elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        items = []
        for item in result:
            if isinstance(item, str):
                items.append((item, ""))
            else:
                column, reason = item
                items.append((column, reason))
    else:
        raise TypeError(
            "__flowdash_apply_filters__ returns {column: reason} or None, "
            f"got {type(result).__name__}."
        )
    return {
        str(column): str(reason or "could not be applied")
        for column, reason in items
        if str(column) in filters
    }


# --------------------------------------------------------------------------
# Checking filters
# --------------------------------------------------------------------------


def _scalar(value: t.Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


def check_value(value: t.Any, widget: str | None, key: str = "value") -> t.Any:
    """Return a filter value as a list, raising :class:`FilterError` if it is not one *widget* takes."""
    return _check_value(value, widget, key)


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
    elif len(value) > MAX_OPTIONS:
        raise FilterError(
            key,
            f"a selection keeps at most {MAX_OPTIONS} values, got {len(value)}; "
            "use a range, or a Filter node, to keep more.",
        )
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


class FilterMismatch(ValueError):
    """A filter's values cannot be compared with a column of the same name, e.g. numbers with text."""


def _kind(series: pd.Series) -> str:
    """Return what *series* holds, as far as comparing it with a filter's values goes."""
    dtype = series.dtype
    if isinstance(dtype, pd.CategoricalDtype):
        return "category"
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    sample = series.dropna()
    if sample.empty:
        return "empty"
    first = sample.iloc[0]
    if isinstance(first, dt.datetime):
        return "pydatetime"
    if isinstance(first, dt.date):
        return "date"
    if isinstance(first, str):
        return "text"
    return "other"


_HOLDS = {
    "number": "numbers",
    "datetime": "dates",
    "pydatetime": "dates",
    "date": "dates",
    "text": "text",
}


def _is_number(value: t.Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_number(value: t.Any) -> t.Any:
    if value is None or _is_number(value):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            pass
        else:
            return int(number) if number.is_integer() else number
    raise FilterMismatch(f"holds numbers, and {value!r} is not one")


def _to_moment(value: t.Any, kind: str) -> t.Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            raise FilterMismatch(f"holds dates, and {value!r} is not one") from None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if not isinstance(value, dt.date):
        raise FilterMismatch(f"holds dates, and {value!r} is not one")
    if kind == "date":
        return value.date() if isinstance(value, dt.datetime) else value
    if kind == "pydatetime" and not isinstance(value, dt.datetime):
        return dt.datetime.combine(value, dt.time())
    if kind == "datetime":
        return pd.Timestamp(value)
    return value


def _to_text(value: t.Any) -> t.Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.date, pd.Timestamp)):
        return value.isoformat()
    return str(value)


def coerce_predicate(p: Predicate, series: pd.Series) -> Predicate:
    """Return *p* with its values made comparable with *series*, or raise :class:`FilterMismatch`.

    Columns are matched by name only, so two views can hold a column of the
    same name with different types. Values are converted where that is safe:
    ISO strings to dates for a column of dates, numeric strings to numbers
    for a column of numbers, numbers to strings for a column of text. A range
    on text or on unordered categories has nothing to compare and is refused.
    """
    kind = _kind(series)
    values = p.values
    if kind == "category":
        if p.op == "range" and not series.dtype.ordered:
            raise FilterMismatch("holds unordered categories, which have no range")
        categories = series.dtype.categories
        if p.op == "in" and len(categories) and isinstance(categories[0], str):
            values = tuple(_to_text(v) for v in values)
    elif kind == "number":
        values = tuple(_to_number(v) for v in values)
    elif kind in ("datetime", "pydatetime", "date"):
        values = tuple(_to_moment(v, kind) for v in values)
    elif kind == "text":
        if p.op == "range":
            if not all(v is None or isinstance(v, str) for v in values):
                raise FilterMismatch(
                    "holds text, which a range of numbers or dates cannot compare"
                )
        else:
            values = tuple(_to_text(v) for v in values)
    return Predicate(p.field, p.op, values, p.closed)


def filter_frame_checked(
    frame: t.Any, predicates: Mapping[str, Predicate]
) -> tuple[t.Any, dict[str, str]]:
    """Return the rows of *frame* matching every predicate it can apply, and why it could not apply the others.

    Like :func:`filter_frame`, and the reasons are by column, e.g.
    ``{"year": "holds numbers, and 'n/a' is not one"}``. A predicate that
    cannot be compared with its column is left out rather than failing the
    whole frame.
    """
    if not isinstance(frame, pd.DataFrame) or not predicates:
        return frame, {}
    keep = None
    mismatched: dict[str, str] = {}
    for column, p in predicates.items():
        if column not in frame.columns:
            continue
        try:
            coerced = coerce_predicate(p, frame[column])
            mask = coerced.mask(frame).fillna(False).astype(bool)
        except FilterMismatch as exc:
            mismatched[column] = str(exc)
            continue
        except (TypeError, ValueError) as exc:
            mismatched[column] = f"holds values the filter's cannot be compared with ({exc})"
            continue
        keep = mask if keep is None else keep & mask
    return (frame if keep is None else frame[keep]), mismatched


def filter_frame(frame: t.Any, predicates: Mapping[str, Predicate]) -> t.Any:
    """Return the rows of *frame* matching every predicate on a column it has.

    The reference implementation of global filters, applied to the DataFrame
    inputs of components that do not filter themselves. Anything that is not
    a DataFrame, and a frame without any of the columns, is returned as is,
    and a predicate whose values cannot be compared with its column is
    skipped, see :func:`filter_frame_checked`.
    """
    return filter_frame_checked(frame, predicates)[0]


def plain_value(value: t.Any) -> t.Any:
    """Return a column value as JSON can hold it: dates and datetimes as ISO strings."""
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def column_options(frames: Iterable[pd.DataFrame], column: str) -> list[t.Any]:
    """Return the distinct values of *column* across *frames*, sorted, as plain values."""
    values: dict[t.Any, None] = {}
    for frame in frames:
        if column not in frame.columns:
            continue
        for value in frame[column].dropna().unique():
            values[plain_value(value)] = None
            if len(values) >= MAX_OPTIONS:
                break
    try:
        return sorted(values)
    except TypeError:
        return sorted(values, key=str)


def _scalar_value(value: t.Any) -> t.Any:
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def _span_kind(value: t.Any) -> str | None:
    if _is_number(value):
        return "number"
    if isinstance(value, (dt.date, pd.Timestamp)):
        return "moment"
    return None


def _moment(value: t.Any) -> dt.datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    return value.replace(tzinfo=None)


def comparable_span(spans: Iterable[tuple[t.Any, t.Any] | None]) -> tuple[t.Any, t.Any] | None:
    """Return the overall span of *spans*, keeping those of the kind most of them have.

    Spans of numbers and of dates cannot be compared with each other, nor
    those of text with anything a range slider shows, so the others are dropped.
    """
    by_kind: dict[str, list[tuple]] = {}
    for span in spans:
        if span is None:
            continue
        lo, hi = (_scalar_value(v) for v in span)
        kind = _span_kind(lo)
        if kind is None or _span_kind(hi) != kind:
            continue
        if kind == "moment":
            lo, hi = _moment(lo), _moment(hi)
        by_kind.setdefault(kind, []).append((lo, hi))
    if not by_kind:
        return None
    chosen = max(by_kind.values(), key=len)
    return min(s[0] for s in chosen), max(s[1] for s in chosen)


def column_span(frames: Iterable[pd.DataFrame], column: str) -> tuple[t.Any, t.Any] | None:
    """Return the smallest and largest value of *column* across *frames*, or None if it has none.

    Frames whose column cannot be compared with the others' are left out, see
    :func:`comparable_span`.
    """
    spans = []
    for frame in frames:
        if column not in frame.columns:
            continue
        series = frame[column].dropna()
        if series.empty:
            continue
        try:
            spans.append((series.min(), series.max()))
        except TypeError:
            continue
    return comparable_span(spans)


# --------------------------------------------------------------------------
# Which filters reach a view
# --------------------------------------------------------------------------


# Why a global filter does not reach a view.
MISSING = "missing"
EXCLUDED = "excluded"
MISMATCHED = "mismatched"
# The view itself reported it could not apply the filter, see FilterTarget.
SKIPPED = "skipped"


def resolve_view(
    filters: Iterable[Mapping],
    active: Mapping[str, Predicate],
    node: str,
    columns: Collection[str] | None,
    mismatched: Mapping[str, str] | None = None,
    reported: Mapping[str, str] | None = None,
) -> tuple[dict[str, Predicate], dict[str, str]]:
    """Return the predicates that reach the view *node*, and why each other active filter does not.

    *active* are the predicates of the filters with a value, by column, and
    *columns* the view's columns, None while unknown. A filter the view opts
    out of is ``excluded``, one on a column it lacks is ``missing``, and one
    whose values its column cannot be compared with, as *mismatched* says by
    column, is ``mismatched``. One the view itself could not apply, as
    *reported* says by column, is ``skipped``.
    """
    mismatched = mismatched or {}
    reported = reported or {}
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
        elif column in mismatched:
            skipped[column] = MISMATCHED
        elif column in reported:
            skipped[column] = SKIPPED
        else:
            applied[column] = active[column]
    return applied, skipped


def hint_text(filters: Iterable[Mapping], skipped: Mapping[str, str]) -> str:
    """Return the line a view shows for the active filters that do not reach it, e.g. ``Not filtered by State``."""
    labels = [filter_label(f) for f in filters if f["column"] in skipped]
    return f"Not filtered by {', '.join(labels)}" if labels else ""


def hint_detail(
    filters: Iterable[Mapping],
    skipped: Mapping[str, str],
    mismatched: Mapping[str, str] | None = None,
    reported: Mapping[str, str] | None = None,
) -> str:
    """Return why each filter in *skipped* does not reach a view, for its tooltip."""
    reasons = []
    for entry in filters:
        reason = skipped.get(entry["column"])
        if reason == EXCLUDED:
            reasons.append(f"{filter_label(entry)}: this view opts out")
        elif reason == MISSING:
            reasons.append(f"{filter_label(entry)}: its data has no column {entry['column']!r}")
        elif reason == MISMATCHED:
            why = (mismatched or {}).get(entry["column"], "cannot be compared with the filter")
            reasons.append(f"{filter_label(entry)}: its column {entry['column']!r} {why}")
        elif reason == SKIPPED:
            why = (reported or {}).get(entry["column"], "could not be applied")
            reasons.append(f"{filter_label(entry)}: {why}")
    return "; ".join(reasons)
