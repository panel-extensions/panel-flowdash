"""Plot selections as one value, carried by ports of type ``Selection``.

A brush or click on a chart becomes a :class:`Selection`: predicates on fields
of the chart's data, such as ``category in [A, B]`` or ``10 <= price <= 20``,
and the node that made it. A filter takes it and keeps the rows matching every
predicate; an empty selection keeps every row.

The chart a selection is made on should read the unfiltered data and show its
own selection, as Vega-Lite and HoloViews do, so only other views are filtered
by it. Feeding the chart from the filter its selection drives is a cycle,
which the editor refuses.

:func:`from_vega` and :func:`from_holoviews` turn what those libraries report
into a ``Selection``, and :func:`link_selection` gives a component a debounced
``selection`` output from a Vega pane or a HoloViews stream.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
import typing as t
import weakref
from dataclasses import dataclass

import pandas as pd
import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash.port_types import register_port_type
from panel_flowdash.registry import register

SELECTION_TYPE = register_port_type("Selection")

# Milliseconds a chart waits after the last change of a selection before reporting it,
# so a drag reports once it pauses rather than on every mouse move.
DEBOUNCE = 150

# Milliseconds a link waits before reporting a selection, so selections that arrive
# together are reported once. After a report that took longer, e.g. while the filters
# it drove ran their queries, it waits half that long, up to COALESCE_MAX, so the
# selections a drag sends meanwhile replace each other rather than queue.
COALESCE = 10
COALESCE_MAX = 200


@dataclass(frozen=True)
class Predicate:
    """Rows whose ``field`` lies in a range or is one of a set of values.

    ``op`` is ``"range"``, with ``values`` the ``(start, end)`` bounds, either
    of which may be None, or ``"in"``, with ``values`` the values to keep.
    ``closed`` says which bounds of a range are included: ``"both"``, or
    ``"left"`` for ``start <= x < end``.
    """

    field: str
    op: t.Literal["range", "in"]
    values: tuple
    closed: t.Literal["both", "left"] = "both"

    def __post_init__(self):
        """Store the values as a tuple and check the predicate."""
        object.__setattr__(self, "values", tuple(self.values))
        if self.op not in ("range", "in"):
            raise ValueError(f"A predicate's op is 'range' or 'in', not {self.op!r}.")
        if self.closed not in ("both", "left"):
            raise ValueError(
                f"A range is closed on 'both' ends or the 'left', not {self.closed!r}."
            )
        if self.op == "range" and len(self.values) != 2:
            raise ValueError(f"A range predicate takes (start, end), got {self.values!r}.")

    def mask(self, frame: pd.DataFrame) -> pd.Series:
        """Return which rows of *frame* match."""
        column = frame[self.field]
        if self.op == "in":
            return column.isin(self.values)
        start, end = self.values
        keep = pd.Series(True, index=frame.index)
        if start is not None:
            keep &= column >= start
        if end is not None:
            keep &= (column < end) if self.closed == "left" else (column <= end)
        return keep

    def to_dict(self) -> dict[str, t.Any]:
        """Return the predicate as a dict of its fields."""
        return {
            "field": self.field,
            "op": self.op,
            "values": list(self.values),
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, data: t.Mapping[str, t.Any]) -> Predicate:
        """Return the predicate :meth:`to_dict` described."""
        return cls(data["field"], data["op"], tuple(data["values"]), data.get("closed", "both"))

    def __str__(self) -> str:
        """Describe the predicate, e.g. ``category in [A, B]`` or ``10 <= price < 20``."""
        if self.op == "in":
            return f"{self.field} in [{', '.join(map(str, self.values))}]"
        start, end = self.values
        upper = "<" if self.closed == "left" else "<="
        if start is None:
            return f"{self.field} {upper} {end}"
        if end is None:
            return f"{self.field} >= {start}"
        return f"{start} <= {self.field} {upper} {end}"


@dataclass(frozen=True)
class Selection:
    """A plot selection: rows matching every predicate, made on the node ``source``.

    A selection with no predicates is empty and selects every row.

    Some selections cannot be turned into predicates on fields, and filters
    refuse them: ``index`` holds rows named by their position in the chart's
    data, such as a Vega-Lite point selection without fields, and
    ``unsupported`` says why another kind of selection cannot filter data,
    such as a lasso or a brush on a repeating time unit.
    """

    predicates: tuple[Predicate, ...] = ()
    source: str | None = None
    index: tuple[int, ...] | None = None
    unsupported: str | None = None

    def __post_init__(self):
        """Store the predicates and the index as tuples."""
        object.__setattr__(self, "predicates", tuple(self.predicates))
        if self.index is not None:
            object.__setattr__(self, "index", tuple(self.index))

    @property
    def empty(self) -> bool:
        """Whether the selection keeps every row."""
        return not self.predicates and self.index is None and self.unsupported is None

    @property
    def fields(self) -> list[str]:
        """The fields the predicates are on, in order."""
        return [p.field for p in self.predicates]

    def check_filterable(self) -> None:
        """Raise ``ValueError`` if the selection cannot be applied to data as predicates on fields."""
        if self.unsupported is not None:
            raise ValueError(self.unsupported)
        if self.index is not None:
            raise ValueError(
                "The selection names rows by their position in the chart, so it cannot filter "
                "other data. Select on fields instead, e.g. a Vega-Lite point selection with "
                "'fields' or a HoloViews element with a key column."
            )

    def filter(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return the rows of *frame* matching every predicate; an empty selection keeps them all."""
        self.check_filterable()
        missing = [f for f in self.fields if f not in frame.columns]
        if missing:
            raise ValueError(
                f"The data has no column {missing[0]!r} to apply the selection to. "
                f"Its columns are: {', '.join(map(str, frame.columns))}."
            )
        if not self.predicates:
            return frame
        keep = pd.Series(True, index=frame.index)
        for predicate in self.predicates:
            keep &= predicate.mask(frame)
        return frame[keep]

    def to_dict(self) -> dict[str, t.Any]:
        """Return the selection as a dict of plain values, e.g. to store it."""
        return {
            "predicates": [p.to_dict() for p in self.predicates],
            "source": self.source,
            "index": None if self.index is None else list(self.index),
            "unsupported": self.unsupported,
        }

    @classmethod
    def from_dict(cls, data: t.Mapping[str, t.Any]) -> Selection:
        """Return the selection :meth:`to_dict` described."""
        return cls(
            tuple(Predicate.from_dict(p) for p in data.get("predicates") or ()),
            data.get("source"),
            data.get("index"),
            data.get("unsupported"),
        )

    def __str__(self) -> str:
        """Describe the selection, e.g. ``category in [A, B]; 10 <= price <= 20``."""
        if self.unsupported is not None:
            return f"unsupported: {self.unsupported}"
        if self.index is not None:
            return f"{len(self.index)} rows by position"
        return "; ".join(map(str, self.predicates)) or "nothing selected"


EMPTY = Selection()


# ---------------------------------------------------------------------------
# Vega-Lite
# ---------------------------------------------------------------------------


def _time_unit(channel: dict) -> str | None:
    unit = channel.get("timeUnit")
    if isinstance(unit, dict):
        unit = unit.get("unit")
    return unit if isinstance(unit, str) else None


def _unescape(name: str) -> str:
    return name.replace("\\", "")


def _aggregates(view: dict) -> bool:
    encoding = view.get("encoding") or {}
    if any(isinstance(c, dict) and c.get("aggregate") for c in encoding.values()):
        return True
    return any(isinstance(s, dict) and "aggregate" in s for s in view.get("transform") or ())


@dataclass(frozen=True)
class _Channel:
    """An encoding channel, and whether the view it is in aggregates its rows."""

    spec: dict
    aggregated: bool


def _vega_channels(spec: t.Any, aggregated: bool = False) -> dict[str, _Channel]:
    """Return the encoding channels by the name a selection reports their field under.

    A field with a ``timeUnit`` is reported as ``{timeUnit}_{field}``.
    Layers and concatenated views are searched too.
    """
    found: dict[str, _Channel] = {}
    if not isinstance(spec, dict):
        return found
    aggregated = aggregated or _aggregates(spec)
    for channel in (spec.get("encoding") or {}).values():
        if isinstance(channel, dict) and isinstance(channel.get("field"), str):
            name = _unescape(channel["field"])
            found.setdefault(name, _Channel(channel, aggregated))
            if unit := _time_unit(channel):
                found.setdefault(f"{unit}_{name}", _Channel(channel, aggregated))
    for key in ("layer", "concat", "hconcat", "vconcat"):
        for view in spec.get(key) or []:
            for name, channel in _vega_channels(
                view, aggregated if key == "layer" else False
            ).items():
                found.setdefault(name, channel)
    if isinstance(spec.get("spec"), dict):
        for name, channel in _vega_channels(spec["spec"]).items():
            found.setdefault(name, channel)
    return found


def _from_epoch_ms(value: t.Any) -> t.Any:
    """Return Vega's temporal values, epoch milliseconds, as naive datetimes in UTC as Panel sends them."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).replace(tzinfo=None)
    return value


# Vega-Lite's time unit parts, longest spelling first where one starts another.
_UNIT_PARTS = (
    "year",
    "quarter",
    "month",
    "week",
    "dayofyear",
    "day",
    "date",
    "hours",
    "minutes",
    "seconds",
    "milliseconds",
)
# The pandas period of a time unit, by its finest part. Vega's weeks start on Sunday.
_PART_FREQUENCIES = {
    "year": "Y",
    "quarter": "Q",
    "month": "M",
    "week": "W-SAT",
    "dayofyear": "D",
    "day": "D",
    "date": "D",
    "hours": "h",
    "minutes": "min",
    "seconds": "s",
    "milliseconds": "ms",
}


def _unit_parts(unit: str) -> list[str] | None:
    parts, rest = [], unit
    while rest:
        part = next((p for p in _UNIT_PARTS if rest.startswith(p)), None)
        if part is None:
            return None
        parts.append(part)
        rest = rest[len(part) :]
    return parts


def _snap_time_unit(start_ms: float, end_ms: float, unit: str, timezone: str | None):
    """Return the range of the units Vega highlights, those whose start lies within the brush.

    A unit is cut in UTC when it starts with ``utc`` and otherwise in the
    browser's *timezone*, as Vega does; the bounds are returned as naive
    datetimes in UTC, as Panel sends naive datetimes. Returns None when the
    brush covers no unit's start.
    """
    base, utc = _base_unit(unit)
    freq = _PART_FREQUENCIES[_unit_parts(base)[-1]]
    zone = "UTC" if utc or not timezone else timezone

    def local(ms):
        return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(zone).tz_localize(None)

    def to_utc(stamp):
        aware = stamp.tz_localize(zone, ambiguous=False, nonexistent="shift_forward")
        return aware.tz_convert("UTC").tz_localize(None).to_pydatetime()

    start, end = sorted((local(start_ms), local(end_ms)))
    first = pd.Period(start, freq)
    if first.start_time < start:
        first += 1
    last = pd.Period(end, freq)
    if last < first:
        return None
    return to_utc(first.start_time), to_utc((last + 1).start_time)


def _base_unit(unit: str) -> tuple[str, bool]:
    """Return *unit* without its ``binned`` and ``utc`` prefixes, and whether it is in UTC."""
    unit = unit.removeprefix("binned")
    return unit.removeprefix("utc"), unit.startswith("utc")


def _repeating_unit(unit: str) -> bool:
    """Whether *unit* folds time onto a cycle, e.g. ``month`` puts every January together."""
    parts = _unit_parts(_base_unit(unit)[0])
    return parts is None or parts[0] != "year"


def _snap_bins(start: float, end: float, bin_spec: dict):
    """Return the range of the bins whose midpoint lies within the brush, or None if no bin's does.

    Bins are only known here when the spec fixes their ``step``; Vega derives
    them from ``maxbins`` and the data otherwise. Edges lie at ``anchor``,
    else at the start of an ``extent`` that is not made nice, else at
    multiples of the step.
    """
    step = bin_spec.get("step")
    if not isinstance(step, (int, float)) or isinstance(step, bool) or step <= 0:
        return None
    extent = bin_spec.get("extent")
    if isinstance(bin_spec.get("anchor"), (int, float)):
        base = bin_spec["anchor"]
    elif isinstance(extent, (list, tuple)) and len(extent) == 2 and bin_spec.get("nice") is False:
        base = extent[0]
    else:
        base = 0
    first = math.ceil((start - base) / step - 0.5)
    last = math.floor((end - base) / step - 0.5)
    if last < first:
        return None
    return base + first * step, base + (last + 1) * step


def _interval_predicate(
    name: str, values: list, channel: _Channel | None, timezone: str | None
) -> Predicate | str:
    """Return the predicate of an interval on one field, or why it cannot be one."""
    spec = channel.spec if channel is not None else {}
    field = _unescape(spec.get("field", name))
    kind = spec.get("type")
    if kind in ("nominal", "ordinal"):
        return Predicate(field, "in", tuple(values))
    start, end = values[0], values[-1]
    unit = _time_unit(spec)
    if unit:
        if _repeating_unit(unit):
            return (
                f"The brush on {field!r} by the time unit {unit!r} selects the same {unit} in "
                "every year, which a range of dates cannot express. Brush a time unit that "
                "starts with the year, e.g. 'yearmonth', to filter by it."
            )
        snapped = _snap_time_unit(start, end, unit, timezone)
        if snapped is None:
            # The brush covers no unit's start, so Vega highlights nothing: an empty range.
            first = _from_epoch_ms(min(start, end))
            return Predicate(field, "range", (first, first), closed="left")
        return Predicate(field, "range", snapped, closed="left")
    if kind == "temporal":
        start, end = sorted((_from_epoch_ms(start), _from_epoch_ms(end)))
        return Predicate(field, "range", (start, end))
    start, end = sorted((start, end))
    if channel is not None and channel.aggregated and isinstance(spec.get("bin"), dict):
        snapped = _snap_bins(start, end, spec["bin"])
        if snapped is not None:
            return Predicate(field, "range", snapped, closed="left")
    return Predicate(field, "range", (start, end))


def from_vega(
    value: t.Any,
    kind: str | None,
    spec: t.Any = None,
    source: str | None = None,
    timezone: str | None = None,
) -> Selection:
    """Return the selection a Vega pane reports for one selection parameter.

    Parameters
    ----------
    value
        The value of the parameter on ``pn.pane.Vega.selection``. An interval
        gives ``{field: [start, end]}`` per encoded field on a continuous
        scale and ``{field: [v1, v2, ...]}`` on a discrete one; a point gives a
        list of ``{field: value}`` items, or ``_vgsid_`` row ids when it names
        no fields. Empty or None means nothing is selected.
    kind
        ``"interval"`` or ``"point"``, as ``Vega._selections`` reports it.
    spec
        The Vega-Lite spec, whose encodings give each field's type, time
        unit and bins.
    source
        The node the selection is made on.
    timezone
        The browser's IANA timezone, in which Vega cuts local time units;
        UTC when not given.

    Temporal values arrive as epoch milliseconds. A brush on a field with a
    ``timeUnit`` starting with the year keeps the whole units whose start it
    covers, which are the ones Vega highlights; a repeating time unit such as
    ``month`` or ``hours`` cannot be expressed as a range and is unsupported.
    A brush on a binned field of an aggregating chart keeps the whole bins
    whose midpoint it covers when the spec fixes ``bin.step``, and the
    brushed range as it is otherwise. Point selections on temporal fields do
    not reach Python: Panel cannot read them.
    """
    if not value:
        return Selection(source=source)
    channels = _vega_channels(spec)
    if kind == "interval" and isinstance(value, dict):
        predicates = []
        for name, values in value.items():
            if not values:
                continue
            channel = channels.get(_unescape(name))
            predicate = _interval_predicate(name, list(values), channel, timezone)
            if isinstance(predicate, str):
                return Selection(source=source, unsupported=predicate)
            predicates.append(predicate)
        return Selection(tuple(predicates), source)
    items = value if isinstance(value, list) else [value]
    if any(not isinstance(item, dict) for item in items):
        return Selection(source=source, index=tuple(int(v) - 1 for v in items))
    by_field: dict[str, list] = {}
    for item in items:
        for name, v in item.items():
            channel = channels.get(_unescape(name))
            field = _unescape(channel.spec.get("field", name) if channel else name)
            values = by_field.setdefault(field, [])
            if v not in values:
                values.append(v)
    return Selection(tuple(Predicate(f, "in", tuple(vs)) for f, vs in by_field.items()), source)


# ---------------------------------------------------------------------------
# HoloViews
# ---------------------------------------------------------------------------


def _element(element):
    """Return the element a DynamicMap shows last, or *element* itself."""
    if type(element).__name__ == "DynamicMap":
        return element.last
    return element


def _dims(element) -> list[str]:
    return [d.name for d in (*element.kdims, *element.vdims)]


def _categorical(frame: pd.DataFrame, dim: str) -> bool:
    column = frame[dim]
    return not (
        pd.api.types.is_numeric_dtype(column) or pd.api.types.is_datetime64_any_dtype(column)
    ) or pd.api.types.is_bool_dtype(column)


def _range(field: str, start, end) -> Predicate:
    start, end = sorted((start, end))
    return Predicate(field, "range", (start, end))


def from_holoviews(stream, element=None, *, key: str | None = None, source: str | None = None):
    """Return the selection a HoloViews stream reports.

    ``BoundsXY``, ``BoundsX`` and ``BoundsY`` give ranges on the element's
    first two dimensions; a box on a categorical axis is unsupported, since
    its bounds are screen positions rather than values. ``Selection1D``,
    which Bokeh's box, lasso and tap tools fill, gives the selected rows'
    values of *key*: by default the element's first key dimension when it is
    categorical and its values are unique, such as the categories of
    ``Bars``, and the rows' positions otherwise, which filters refuse. A
    ``Lasso`` stream's polygon cannot be a filter and is unsupported. For a
    ``DynamicMap``, the element it shows last is used.
    """
    name = type(stream).__name__
    element = _element(element)
    if name == "Lasso":
        return Selection(
            source=source,
            unsupported=(
                "A lasso selects a polygon, which cannot filter other data. Use a box "
                "selection, or Bokeh's lasso tool with a Selection1D stream and a key column."
            ),
        )
    if name in ("BoundsXY", "BoundsX", "BoundsY"):
        if element is None:
            raise ValueError(f"A {name} selection needs the element, to name its dimensions.")
        bounds = (
            stream.bounds
            if name == "BoundsXY"
            else stream.boundsx
            if name == "BoundsX"
            else stream.boundsy
        )
        if bounds is None:
            return Selection(source=source)
        dims = _dims(element)
        if name == "BoundsXY":
            x0, y0, x1, y1 = bounds
            pairs = [(dims[0], x0, x1), (dims[1], y0, y1)]
        else:
            pairs = [(dims[0] if name == "BoundsX" else dims[1], *bounds)]
        frame = element.dframe()
        for dim, _, _ in pairs:
            if dim in frame and _categorical(frame, dim):
                return Selection(
                    source=source,
                    unsupported=(
                        f"A box on the categorical axis {dim!r} gives screen positions, not "
                        "values. Select the categories with a Selection1D stream instead."
                    ),
                )
        return Selection(tuple(_range(dim, start, end) for dim, start, end in pairs), source)
    if name == "Selection1D":
        if not stream.index:
            return Selection(source=source)
        if element is None:
            return Selection(source=source, index=tuple(stream.index))
        frame = element.dframe()
        if key is None and element.kdims:
            first = element.kdims[0].name
            unique_category = _categorical(frame, first) and frame[first].is_unique
            key = first if unique_category else None
        if key is None:
            return Selection(source=source, index=tuple(stream.index))
        values = frame.iloc[list(stream.index)][key].tolist()
        return Selection((Predicate(key, "in", tuple(dict.fromkeys(values))),), source)
    raise TypeError(f"Cannot read a selection from a {name} stream.")


# ---------------------------------------------------------------------------
# Linking a component's output
# ---------------------------------------------------------------------------


class SelectionLink(param.Parameterized):
    """Reports the selection made on a Vega pane or HoloViews stream as ``value``.

    Equal selections in a row are reported once. Selections that arrive
    while one is being reported, for example while the filters it drives
    run their queries, replace each other, and only the newest is reported
    next, so at most one is in flight. After a slow report the link waits
    half as long, up to ``COALESCE_MAX`` milliseconds, before the next, so
    a drag's selections collect rather than each running its own report.
    """

    value = param.ClassSelector(class_=Selection, default=EMPTY, doc="The latest selection.")

    debounce = param.Integer(
        default=DEBOUNCE,
        bounds=(0, None),
        doc="""
        Milliseconds a Vega pane waits after the last change of a selection
        before sending it, unless the pane's own debounce was set. Bokeh's
        selection tools report on release already.""",
    )

    def __init__(self, source: str | t.Callable[[], str | None] | None = None, **params):
        super().__init__(**params)
        self._source = source
        self._lock = threading.Lock()
        self._pending: Selection | None = None
        self._scheduled = False
        self._reporting = False
        self._last_report = 0.0
        self._unwatch: list[t.Callable[[], None]] = []

    @property
    def source(self) -> str | None:
        """The node the selection is made on."""
        return self._source() if callable(self._source) else self._source

    def push(self, selection: Selection) -> None:
        """Report *selection*, unless it equals the one reported last.

        In a served session it is reported shortly after, together with any
        that follow it; outside one, at once.
        """
        with self._lock:
            self._pending = selection
            if self._scheduled or self._reporting:
                return
            self._scheduled = True
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None:
            wait = min(max(COALESCE, self._last_report * 500), COALESCE_MAX)
            doc.add_timeout_callback(self._flush, wait)
        else:
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            self._scheduled = False
            if self._reporting:
                return
            self._reporting = True
        try:
            while True:
                with self._lock:
                    selection, self._pending = self._pending, None
                    if selection is None:
                        self._reporting = False
                        return
                if selection != self.value:
                    started = time.monotonic()
                    self.value = selection
                    self._last_report = time.monotonic() - started
        except BaseException:
            with self._lock:
                self._reporting = False
            raise

    def clear(self) -> None:
        """Report an empty selection."""
        self.push(Selection(source=self.source))

    def unlink(self) -> None:
        """Stop following the pane or stream linked last."""
        for unwatch in self._unwatch:
            unwatch()
        self._unwatch = []

    def link_vega(self, pane: pn.pane.Vega, spec: t.Any = None) -> SelectionLink:
        """Follow the selections of *pane*, replacing what was linked before.

        The pane's ``debounce`` is set to this link's unless it was set
        before. *spec* defaults to the pane's object. Replacing a linked pane
        or stream clears the selection.
        """
        relinked = bool(self._unwatch)
        self.unlink()
        if pane.debounce == pane.param.debounce.default:
            pane.debounce = self.debounce
        cache: dict[str, t.Any] = {}

        def current_spec():
            if spec is not None:
                return spec
            obj = pane.object
            if cache.get("object") is not obj:
                cache["object"] = obj
                cache["spec"] = obj if isinstance(obj, dict) else getattr(obj, "to_dict", dict)()
            return cache["spec"]

        def changed(event):
            browser = pn.state.browser_info
            timezone = getattr(browser, "timezone", None) if browser is not None else None
            kinds = pane._selections
            self.push(
                from_vega(event.new, kinds.get(event.name), current_spec(), self.source, timezone)
            )

        def watch(selection):
            names = [n for n in selection.param if n != "name"]
            if names:
                watcher = selection.param.watch(changed, names)
                return lambda: selection.param.unwatch(watcher)
            return lambda: None

        state = {"unwatch": watch(pane.selection)}

        def replaced(event):
            state["unwatch"]()
            state["unwatch"] = watch(event.new)
            self.clear()

        pane_watcher = pane.param.watch(replaced, "selection")
        self._unwatch = [
            lambda: state["unwatch"](),
            lambda: pane.param.unwatch(pane_watcher),
        ]
        if relinked:
            self.clear()
        return self

    def link_stream(self, stream, element=None, *, key: str | None = None) -> SelectionLink:
        """Follow a HoloViews ``BoundsXY``, ``BoundsX``, ``BoundsY`` or ``Selection1D`` stream.

        Replacing a linked pane or stream clears the selection.
        """
        relinked = bool(self._unwatch)
        self.unlink()

        def changed(**kwargs):
            self.push(from_holoviews(stream, element, key=key, source=self.source))

        stream.add_subscriber(changed)

        def remove():
            stream._subscribers = [(p, s) for p, s in stream._subscribers if s is not changed]

        self._unwatch = [remove]
        if relinked:
            self.clear()
        return self


# The links of each component, released when the editor disposes of the component.
_LINKS: weakref.WeakKeyDictionary[t.Any, list[SelectionLink]] = weakref.WeakKeyDictionary()


def release_links(component: t.Any) -> None:
    """Unlink every link :func:`link_selection` made for *component*."""
    try:
        links = _LINKS.pop(component, [])
    except TypeError:
        return
    for link in links:
        link.unlink()


def link_selection(
    component: param.Parameterized,
    target,
    *,
    output: str = "selection",
    spec: t.Any = None,
    element=None,
    key: str | None = None,
    debounce: int = DEBOUNCE,
    link: SelectionLink | None = None,
    source: str | t.Callable[[], str | None] | None = None,
) -> SelectionLink:
    """Give *component* a ``selection`` output that follows the selections of *target*.

    *target* is a ``pn.pane.Vega`` or a HoloViews stream (with the *element*
    it selects on). The selection is written to the parameter *output* of
    *component*, which the component declares, e.g.
    ``selection = param.ClassSelector(class_=Selection, default=EMPTY)``,
    and lists in ``provides`` with type ``SELECTION_TYPE``. Its ``source`` is
    the component's node id. Call it from ``__init__`` or ``__panel__``; the
    editor unlinks it when the component is removed.

    Pass the *link* returned before when the component replaces its pane,
    e.g. after its data changed, so the output keeps one link; the new pane
    starts with nothing selected. Pass *source*, the node id or a function
    returning it, when linking outside the component's creation and outside
    a served session, where the node cannot be looked up.
    """
    if link is None:
        from panel_flowdash.editor import current_dashboard, current_node_id

        ref = weakref.ref(component)
        created_for = current_node_id()
        editor = current_dashboard()
        editor_ref = weakref.ref(editor) if editor is not None else None

        def find_source():
            if source is not None:
                return source() if callable(source) else source
            if created_for is not None:
                return created_for
            owner = ref()
            owner_editor = editor_ref() if editor_ref is not None else None
            if owner is None or owner_editor is None:
                return None
            return owner_editor.node_id(owner)

        link = SelectionLink(source=find_source, debounce=debounce)
        link.param.watch(lambda event: _set(ref, output, event.new), "value")
        _LINKS.setdefault(component, []).append(link)
    else:
        link.debounce = debounce
    if isinstance(target, pn.pane.Vega):
        link.link_vega(target, spec)
    else:
        link.link_stream(target, element, key=key)
    return link


def _set(ref, name, value):
    owner = ref()
    if owner is not None:
        setattr(owner, name, value)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@register(
    page=False,
    component=True,
    title="Filter by selection",
    icon="filter_alt",
    description=(
        "Keeps the rows of a DataFrame that match a chart's selection, wired from the chart's "
        "selection output: within each brushed range and among the clicked values. An empty "
        "selection keeps every row. Feed the chart itself the unfiltered data, so it keeps "
        "showing everything with its selection highlighted."
    ),
    tags=["transform"],
    requires=[
        {"key": "data", "type": "DataFrame", "required": True},
        {"key": "selection", "type": SELECTION_TYPE, "required": False, "blocking": False},
    ],
    provides=[{"key": "filtered", "type": "DataFrame"}],
    default_size={"width": 280, "height": 90},
    min_size={"width": 160, "height": 60},
)
class SelectionFilter(Viewer):
    """The rows of a DataFrame that match a selection."""

    data = param.DataFrame(default=None, allow_None=True, doc="The rows to filter.")

    selection = param.ClassSelector(
        class_=Selection, default=None, allow_None=True, doc="The selection rows must match."
    )

    @param.output(param.DataFrame)
    @param.depends("data", "selection")
    def filtered(self):
        """Return the rows of ``data`` matching ``selection``, all of them without one."""
        if self.data is None or self.selection is None:
            return self.data
        return self.selection.filter(self.data)

    def _summary(self):
        if self.data is None:
            return "Waiting for data"
        total = len(self.data)
        if self.selection is None or self.selection.empty:
            return f"All {total:,} rows"
        try:
            kept = len(self.selection.filter(self.data))
        except ValueError as exc:
            return str(exc)
        return f"{kept:,} of {total:,} rows: {self.selection}"

    def __panel__(self):
        """Return a line saying how many rows the selection keeps."""
        from panel_flowdash.ui.text import text_typography

        summary = param.rx(param.bind(lambda data, selection: self._summary(), self.param.data, self.param.selection))
        return text_typography(summary, sizing_mode="stretch_width", margin=(5, 10))
