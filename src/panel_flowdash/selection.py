"""Plot selections as one value, carried by ports of type ``Selection``.

A brush, click or lasso on a chart becomes a :class:`Selection`: predicates on
fields of the chart's data, such as ``category in [A, B]`` or ``price in
[10, 20]``, and the node that made it. A filter takes it and keeps the rows
matching every predicate; an empty selection keeps every row.

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
import typing as t
import weakref
from dataclasses import dataclass

import pandas as pd
import panel as pn
import param

from panel_flowdash.port_types import register_port_type

SELECTION_TYPE = register_port_type("Selection")

# Milliseconds a chart waits after the last change of a selection before reporting it,
# so a drag reports once it pauses rather than on every mouse move.
DEBOUNCE = 150


@dataclass(frozen=True)
class Predicate:
    """Rows whose ``field`` lies in a range or is one of a set of values.

    ``op`` is ``"range"``, with ``values`` the ``(start, end)`` bounds, or
    ``"in"``, with ``values`` the values to keep. ``closed`` says which bounds
    of a range are included: ``"both"``, or ``"left"`` for ``start <= x < end``.
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

    def __str__(self) -> str:
        """Describe the predicate, e.g. ``price in [10, 20)``."""
        if self.op == "in":
            return f"{self.field} in [{', '.join(map(str, self.values))}]"
        right = ")" if self.closed == "left" else "]"
        return f"{self.field} in [{self.values[0]}, {self.values[1]}{right}"


@dataclass(frozen=True)
class Selection:
    """A plot selection: rows matching every predicate, made on the node ``source``.

    A selection with no predicates is empty and selects every row.
    ``index`` holds a selection that names rows by their position in the
    chart's data, such as a Vega-Lite point selection without fields; it
    cannot be turned into a predicate on fields, and filters refuse it.
    """

    predicates: tuple[Predicate, ...] = ()
    source: str | None = None
    index: tuple[int, ...] | None = None

    def __post_init__(self):
        """Store the predicates and the index as tuples."""
        object.__setattr__(self, "predicates", tuple(self.predicates))
        if self.index is not None:
            object.__setattr__(self, "index", tuple(self.index))

    @property
    def empty(self) -> bool:
        """Whether the selection keeps every row."""
        return not self.predicates and self.index is None

    @property
    def fields(self) -> list[str]:
        """The fields the predicates are on, in order."""
        return [p.field for p in self.predicates]

    def check_filterable(self) -> None:
        """Raise ``ValueError`` if the selection names rows by position instead of by fields."""
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

    def __str__(self) -> str:
        """Describe the selection, e.g. ``category in [A, B]; price in [10, 20]``."""
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


def _vega_channels(spec: t.Any) -> dict[str, dict]:
    """Return the encoding channels by the name a selection reports their field under.

    A field with a ``timeUnit`` is reported as ``{timeUnit}_{field}``.
    Layers and concatenated views are searched too.
    """
    found: dict[str, dict] = {}
    if not isinstance(spec, dict):
        return found
    for channel in (spec.get("encoding") or {}).values():
        if isinstance(channel, dict) and isinstance(channel.get("field"), str):
            name = _unescape(channel["field"])
            found.setdefault(name, channel)
            if unit := _time_unit(channel):
                found.setdefault(f"{unit}_{name}", channel)
    for key in ("layer", "concat", "hconcat", "vconcat"):
        for view in spec.get(key) or []:
            for name, channel in _vega_channels(view).items():
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


_UNIT_FREQUENCIES = {
    "year": "Y",
    "yearquarter": "Q",
    "yearmonth": "M",
    "yearweek": "W",
    "yearmonthdate": "D",
    "yearmonthdatehours": "h",
    "yearmonthdatehoursminutes": "min",
}


def _snap_time_unit(start: dt.datetime, end: dt.datetime, unit: str):
    """Return the range of the units Vega highlights: those whose start lies within the brush.

    Returns None for a unit that does not cut time into consecutive periods,
    e.g. ``month`` alone, which folds every year onto the same twelve months.
    """
    freq = _UNIT_FREQUENCIES.get(unit)
    if freq is None:
        return None
    first = pd.Period(start, freq)
    if first.start_time < pd.Timestamp(start):
        first += 1
    last = pd.Period(end, freq)
    if last < first:
        return None
    return first.start_time.to_pydatetime(), (last + 1).start_time.to_pydatetime()


def _snap_bins(start: float, end: float, bin_spec: dict):
    """Return the range of the bins whose midpoint lies within the brush, or None if no bin's does.

    A brush covering no midpoint keeps its own range.

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


def _interval_predicate(name: str, values: list, channel: dict) -> Predicate:
    field = _unescape(channel.get("field", name))
    kind = channel.get("type")
    if kind in ("nominal", "ordinal"):
        return Predicate(field, "in", tuple(values))
    start, end = values[0], values[-1]
    unit = _time_unit(channel)
    if kind == "temporal" or unit:
        start, end = sorted((_from_epoch_ms(start), _from_epoch_ms(end)))
        if unit:
            snapped = _snap_time_unit(start, end, unit)
            if snapped is None:
                return Predicate(field, "range", (start, end))
            return Predicate(field, "range", snapped, closed="left")
        return Predicate(field, "range", (start, end))
    start, end = sorted((start, end))
    if isinstance(channel.get("bin"), dict):
        snapped = _snap_bins(start, end, channel["bin"])
        if snapped is not None:
            return Predicate(field, "range", snapped, closed="left")
    return Predicate(field, "range", (start, end))


def from_vega(
    value: t.Any, kind: str | None, spec: t.Any = None, source: str | None = None
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

    Temporal values arrive as epoch milliseconds. A brush on a field with a
    ``timeUnit`` keeps the whole units whose start it covers, which are the
    ones Vega highlights. A brush on a binned field keeps the whole bins whose
    midpoint it covers when the spec fixes ``bin.step``, and the brushed range
    as it is otherwise.
    """
    if not value:
        return Selection(source=source)
    channels = _vega_channels(spec)
    if kind == "interval" and isinstance(value, dict):
        predicates = []
        for name, values in value.items():
            if not values:
                continue
            predicates.append(
                _interval_predicate(name, list(values), channels.get(_unescape(name), {}))
            )
        return Selection(tuple(predicates), source)
    items = value if isinstance(value, list) else [value]
    if any(not isinstance(item, dict) for item in items):
        return Selection(source=source, index=tuple(int(v) - 1 for v in items))
    by_field: dict[str, list] = {}
    for item in items:
        for name, v in item.items():
            channel = channels.get(_unescape(name), {})
            field = _unescape(channel.get("field", name))
            if channel.get("type") == "temporal" or _time_unit(channel):
                v = _from_epoch_ms(v)
            values = by_field.setdefault(field, [])
            if v not in values:
                values.append(v)
    return Selection(tuple(Predicate(f, "in", tuple(vs)) for f, vs in by_field.items()), source)


# ---------------------------------------------------------------------------
# HoloViews
# ---------------------------------------------------------------------------


def _dims(element) -> list[str]:
    return [d.name for d in (*element.kdims, *element.vdims)]


def _range(field: str, start, end) -> Predicate:
    start, end = sorted((start, end))
    return Predicate(field, "range", (start, end))


def from_holoviews(stream, element=None, *, key: str | None = None, source: str | None = None):
    """Return the selection a HoloViews stream reports.

    ``BoundsXY``, ``BoundsX`` and ``BoundsY`` give ranges on the element's
    first two dimensions. ``Selection1D``, which Bokeh's box, lasso and tap
    tools fill, gives the selected rows' values of *key*: by default the
    element's first key dimension when its values are unique, such as the
    categories of ``Bars``, and the rows' positions otherwise, which filters
    refuse.
    """
    name = type(stream).__name__
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
            return Selection((_range(dims[0], x0, x1), _range(dims[1], y0, y1)), source)
        dim = dims[0] if name == "BoundsX" else dims[1]
        return Selection((_range(dim, *bounds),), source)
    if name == "Selection1D":
        if not stream.index:
            return Selection(source=source)
        if element is None:
            return Selection(source=source, index=tuple(stream.index))
        frame = element.dframe()
        if key is None and element.kdims:
            first = element.kdims[0].name
            key = first if frame[first].is_unique else None
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

    Equal selections in a row are reported once. While one is being
    reported, for example while the filters it drives run their queries,
    newer selections are held and only the latest is reported next.
    """

    value = param.ClassSelector(class_=Selection, default=EMPTY, doc="The latest selection.")

    debounce = param.Integer(
        default=DEBOUNCE,
        bounds=(0, None),
        doc="""
        Milliseconds a Vega pane waits after the last change of a selection
        before sending it. Bokeh's selection tools report on release already.""",
    )

    def __init__(self, source: str | t.Callable[[], str | None] | None = None, **params):
        super().__init__(**params)
        self._source = source
        self._pending: Selection | None = None
        self._reporting = False
        self._scheduled = False
        self._unwatch: list[t.Callable[[], None]] = []

    @property
    def source(self) -> str | None:
        """The node the selection is made on."""
        return self._source() if callable(self._source) else self._source

    def push(self, selection: Selection) -> None:
        """Report *selection*, unless it equals the one reported last."""
        self._pending = selection
        if self._reporting or self._scheduled:
            return
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None:
            self._scheduled = True
            doc.add_next_tick_callback(self._flush)
        else:
            self._flush()

    def _flush(self) -> None:
        self._scheduled = False
        self._reporting = True
        try:
            while self._pending is not None:
                selection, self._pending = self._pending, None
                if selection != self.value:
                    self.value = selection
        finally:
            self._reporting = False

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

        The pane's ``debounce`` is set to this link's. *spec* defaults to the
        pane's object. Replacing a linked pane or stream clears the selection.
        """
        relinked = bool(self._unwatch)
        self.unlink()
        pane.debounce = self.debounce

        def current_spec():
            if spec is not None:
                return spec
            obj = pane.object
            return obj if isinstance(obj, dict) else getattr(obj, "to_dict", dict)()

        def changed(event):
            kinds = pane._selections
            self.push(from_vega(event.new, kinds.get(event.name), current_spec(), self.source))

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
) -> SelectionLink:
    """Give *component* a ``selection`` output that follows the selections of *target*.

    *target* is a ``pn.pane.Vega`` or a HoloViews stream (with the *element*
    it selects on). The selection is written to the parameter *output* of
    *component*, which the component declares, e.g.
    ``selection = param.ClassSelector(class_=Selection, default=EMPTY)``,
    and lists in ``provides`` with type ``SELECTION_TYPE``. Its ``source`` is
    the component's node id.

    Pass the *link* returned before when the component replaces its pane,
    e.g. after its data changed, so the output keeps one link; the new pane
    starts with nothing selected.
    """
    if link is None:
        ref = weakref.ref(component)
        from panel_flowdash.editor import current_dashboard, current_node_id

        created_for = current_node_id()
        editor = current_dashboard()
        editor_ref = weakref.ref(editor) if editor is not None else None

        def source():
            if created_for is not None:
                return created_for
            owner, owner_editor = ref(), editor_ref() if editor_ref is not None else None
            if owner is None or owner_editor is None:
                return None
            return owner_editor.node_id(owner)

        link = SelectionLink(source=source, debounce=debounce)
        link.param.watch(lambda event: _set(ref, output, event.new), "value")
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
