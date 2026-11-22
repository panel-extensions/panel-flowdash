"""The editor's global filters: which views they reach, the filter bar and the notes on tiles.

:class:`GlobalFilters` keeps the dashboard's filters (see
:mod:`panel_flowdash.global_filters`) and their current values, and hands
every view the dashboard shows the predicates that reach it: a component
implementing :class:`~panel_flowdash.global_filters.FilterTarget` gets them
through ``__flowdash_apply_filters__``, any other ``Viewer`` has the
DataFrames at its inputs filtered. :class:`FilterBar` shows one widget per
filter above the tiles and, in edit mode, adds and removes filters. Each
tile shows the Filter nodes feeding it as chips, and the active filters that
do not reach it as a "Not filtered by ..." hint.

The values are what the person viewing the dashboard chose; only a filter's
starting ``value`` is saved with the dashboard.
"""

from __future__ import annotations

import datetime as dt
import logging
import typing as t
import weakref

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.global_filters import (
    FilterMismatch,
    accepts_filters,
    check_filters,
    check_value,
    coerce_predicate,
    column_options,
    column_span,
    comparable_span,
    filter_frame_checked,
    filter_label,
    frame_columns,
    hint_detail,
    hint_text,
    plain_value,
    predicate,
    resolve_view,
    widget_for,
)
from panel_flowdash.selection import Predicate
from panel_flowdash.ui.controls_bar import compact_inputs

logger = logging.getLogger(__name__)

_FILTER_WIDTH = 240

# The remove button sits on the filter's top-right corner, so it reads as the filter's own.
_REMOVE_CSS = """
:host {
  position: absolute;
  right: 0;
  top: 0;
  z-index: 2;
  background: var(--mui-palette-background-default, #fff);
  border-radius: 50%;
}
"""


class GlobalFilters(param.Parameterized):
    """The global filters of a :class:`~panel_flowdash.editor.FlowDash` editor and the views they reach."""

    filters = param.List(
        default=[], doc="The dashboard's filters, views they exclude by instance id."
    )

    values = param.Dict(default={}, doc="The current value of each filter, by column.")

    revision = param.Integer(
        default=0, doc="Bumped when the data of a view changes, so filter choices are read again."
    )

    shared = param.List(
        default=[],
        doc="""
        The Filter nodes feeding every view, as (instance id, title). The
        filter bar shows them once instead of a chip on each tile.""",
    )

    def __init__(self, editor, **params):
        super().__init__(**params)
        self._editor = weakref.ref(editor)
        # The predicates handed to each view, by instance id.
        self._applied: dict[str, dict[str, Predicate]] = {}
        self._notes: dict[str, tuple] = {}
        # Why a filter could not be applied to an input of a view, by view, input and column.
        self._mismatched: dict[str, dict[str, dict[str, str]]] = {}
        # What apply() last worked out, which the notes of a single view reuse.
        self._active: dict[str, Predicate] = {}
        self._targets: set[str] = set()
        self._chips: dict[str, list[tuple[str, str]]] = {}
        self.param.watch(lambda *_: self.apply(), ["filters", "values"])

    @property
    def editor(self):
        return self._editor()

    # ------------------------------------------------------------------
    # Filters and values
    # ------------------------------------------------------------------

    def load(self, filters: list[dict]):
        """Show *filters*, keeping the current value of each filter whose starting value is the same."""
        filters = check_filters(filters)
        before = {f["column"]: f for f in self.filters}
        values = {}
        for entry in filters:
            column = entry["column"]
            old = before.get(column)
            if (
                old is not None
                and old.get("value") == entry.get("value")
                and column in self.values
            ):
                values[column] = self.values[column]
            else:
                values[column] = entry.get("value")
        with param.parameterized.batch_call_watchers(self):
            self.filters = filters
            self.values = values

    def set_value(self, column: str, value: t.Any):
        """Change the current value of the filter on *column*, e.g. as the viewer chose it.

        Raises
        ------
        KeyError
            If the dashboard has no filter on *column*.
        FilterError
            If *value* is not one the filter's widget takes.
        """
        entry = self.entry(column)
        if entry is None:
            raise KeyError(f"The dashboard has no filter on {column!r}.")
        value = check_value(value, self.widget(entry), f"filters.{column}.value")
        if self.values.get(column) != value:
            self.values = {**self.values, column: value}

    def entry(self, column: str) -> dict | None:
        return next((f for f in self.filters if f["column"] == column), None)

    def widget(self, entry: dict) -> str:
        """Return the widget of *entry*: its own, or the one the type of its column gives."""
        if entry.get("widget"):
            return entry["widget"]
        sample = next(
            (f[entry["column"]] for f in self.frames() if entry["column"] in f.columns), None
        )
        return widget_for(entry, sample)

    def active(self) -> dict[str, Predicate]:
        """Return the predicates of the filters with a value, by column."""
        active = {}
        for entry in self.filters:
            try:
                p = predicate(entry, self.values.get(entry["column"]), self.widget(entry))
            except (TypeError, ValueError) as exc:
                logger.warning("Ignoring the filter on %r: %s", entry["column"], exc)
                continue
            if p is not None:
                active[entry["column"]] = p
        return active

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def targets(self) -> list[str]:
        """Return the instance ids of the views the dashboard shows, which global filters reach."""
        from panel_flowdash.ui import tile_layout

        editor = self.editor
        if editor is None:
            return []
        ids = [item["instance_id"] for item in tile_layout.grid_tiles(editor)]
        for area in (tile_layout.TOP, tile_layout.SIDEBAR):
            ids += [item["instance_id"] for item in tile_layout.area_items(editor, area)]
        return list(dict.fromkeys(ids))

    def _inputs(self, instance_id: str) -> dict[str, t.Any]:
        """Return the values at the inputs of a node, as they arrive, before any global filter."""
        editor = self.editor
        state = editor._dataflow_graph.get_state(instance_id)
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        spec = editor._component_specs.get(item["component_id"]) if item else None
        if state is None or spec is None:
            return {}
        return {p.name: getattr(state, p.name, None) for p in spec.inputs if p.name in state.param}

    def columns(self, instance_id: str) -> set[str] | None:
        """Return the columns a view can be filtered on, None while it does not know them."""
        instance = self.editor._component_instances.get(instance_id)
        declare = getattr(instance, "__flowdash_columns__", None)
        if callable(declare):
            try:
                declared = declare()
            except Exception as exc:
                logger.warning("__flowdash_columns__ of %r failed: %s", instance_id, exc)
                return None
            return None if declared is None else {str(c) for c in declared}
        frames = self._inputs(instance_id).values()
        if not any(isinstance(v, pd.DataFrame) for v in frames):
            return None
        return frame_columns(frames)

    def frames(self) -> list[pd.DataFrame]:
        """Return the DataFrames arriving at the views, unfiltered, to offer filter choices from."""
        frames = []
        for iid in self.targets():
            frames += [v for v in self._inputs(iid).values() if isinstance(v, pd.DataFrame)]
        return frames

    def _declared_values(self, column: str) -> list:
        """Return what the views filtering themselves report for *column*, see FilterTarget."""
        reported = []
        for iid in self.targets():
            instance = self.editor._component_instances.get(iid)
            values = getattr(instance, "__flowdash_column_values__", None)
            if not callable(values):
                continue
            try:
                result = values(column)
            except Exception as exc:
                logger.warning("__flowdash_column_values__ of %r failed: %s", iid, exc)
                continue
            if result is not None:
                reported.append(result)
        return reported

    def options(self, column: str) -> list:
        """Return the values a select filter on *column* offers."""
        options = dict.fromkeys(column_options(self.frames(), column))
        for values in self._declared_values(column):
            options.update(dict.fromkeys(values))
        try:
            return sorted(options)
        except TypeError:
            return sorted(options, key=str)

    def span(self, column: str) -> tuple | None:
        """Return the smallest and largest value a range filter on *column* spans.

        Views whose column cannot be compared with the others', e.g. text
        where most hold numbers, are left out.
        """
        spans = [column_span([frame], column) for frame in self.frames()]
        spans += [tuple(v) for v in self._declared_values(column) if len(v) == 2]
        return comparable_span(spans)

    def available_columns(self) -> list[str]:
        """Return the columns of the views' data that have no filter yet, for adding one."""
        taken = {f["column"] for f in self.filters}
        columns: set[str] = set()
        for iid in self.targets():
            columns |= self.columns(iid) or set()
        return sorted(c for c in columns if c not in taken)

    def predicates_for(self, instance_id: str) -> dict[str, Predicate]:
        """Return the predicates a view gets now: the active filters it has not opted out of."""
        if instance_id not in self._applied:
            return {}
        return self._applied[instance_id]

    def _filtered(
        self, instance_id: str, values: dict[str, t.Any], predicates: dict[str, Predicate]
    ) -> dict[str, t.Any]:
        """Filter the DataFrames among *values*, noting the filters a view's column cannot take."""
        mismatched = self._mismatched.setdefault(instance_id, {})
        filtered = {}
        for name, value in values.items():
            try:
                filtered[name], problems = filter_frame_checked(value, predicates)
            except Exception as exc:
                logger.error("Filtering input %r of %r failed: %s", name, instance_id, exc)
                filtered[name], problems = value, {}
            if problems:
                mismatched[name] = problems
            else:
                mismatched.pop(name, None)
        if not mismatched:
            self._mismatched.pop(instance_id, None)
        return filtered

    def mismatched(self, instance_id: str) -> dict[str, str]:
        """Return why the filters a view's data cannot be compared with were not applied, by column."""
        reasons: dict[str, str] = {}
        for problems in self._mismatched.get(instance_id, {}).values():
            for column, reason in problems.items():
                reasons.setdefault(column, reason)
        return reasons

    def filter_inputs(self, instance_id: str, values: dict[str, t.Any]) -> dict[str, t.Any]:
        """Return the input *values* of a view with its global filters applied to its DataFrames.

        A filter a column cannot be compared with, e.g. a range of numbers on
        text, is left out for that view, whose tile then says so.
        """
        predicates = self._applied.get(instance_id)
        if not predicates:
            return values
        instance = self.editor._component_instances.get(instance_id)
        if accepts_filters(instance):
            return values
        return self._filtered(instance_id, values, predicates)

    def inputs_changed(self, instance_id: str, names: t.Iterable[str]):
        """Note that new values arrived at a view's inputs, which may change its columns and notes."""
        if not self.filters:
            return
        state = self.editor._dataflow_graph.get_state(instance_id)
        if state is None or not any(
            isinstance(getattr(state, n, None), pd.DataFrame) for n in names
        ):
            return
        self.revision += 1
        if instance_id in self._targets:
            self._sync_notes(instance_id)

    def apply(self):
        """Hand every view the predicates that reach it now, and update the notes on its tile."""
        editor = self.editor
        if editor is None:
            return
        active = self._active = self.active()
        shown = self.targets()
        targets = set(shown) if self.filters else set()
        for iid, instance in list(editor._component_instances.items()):
            reaching = {}
            if iid in targets:
                reaching = {
                    c: p
                    for c, p in active.items()
                    if iid not in (self.entry(c) or {}).get("exclude", ())
                }
            if accepts_filters(instance):
                columns = self.columns(iid) or set()
                reaching = {c: p for c, p in reaching.items() if c in columns}
            if reaching != self._applied.get(iid, {}):
                if reaching:
                    self._applied[iid] = reaching
                else:
                    self._applied.pop(iid, None)
                self._hand_over(iid, instance, reaching)
        for iid in list(self._applied):
            if iid not in editor._component_instances:
                del self._applied[iid]
        self._targets = set(shown)
        self._sync_chips(shown)
        for iid in set(self._notes) | self._targets:
            self._sync_notes(iid)

    def _hand_over(self, instance_id: str, instance, predicates: dict[str, Predicate]):
        editor = self.editor
        if accepts_filters(instance):
            try:
                instance.__flowdash_apply_filters__(dict(predicates))
            except Exception as exc:
                logger.error("Applying the global filters to %r failed: %s", instance_id, exc)
            return
        inputs = {
            name: value
            for name, value in self._inputs(instance_id).items()
            if isinstance(value, pd.DataFrame) and name in instance.param
        }
        if not inputs:
            self._mismatched.pop(instance_id, None)
            return
        values = self._filtered(instance_id, inputs, predicates)
        try:
            instance.param.update(values)
        except Exception as exc:
            name = next(iter(values))
            editor._report_input_error(instance_id, name, exc)

    def forget(self, instance_id: str):
        """Drop what is kept about a node that went away."""
        self._applied.pop(instance_id, None)
        self._notes.pop(instance_id, None)
        self._mismatched.pop(instance_id, None)
        self._chips.pop(instance_id, None)

    # ------------------------------------------------------------------
    # Notes on tiles
    # ------------------------------------------------------------------

    def _sources(self) -> dict[str, list[str]]:
        sources: dict[str, list[str]] = {}
        for edge in self.editor._dataflow_graph.edges:
            sources.setdefault(edge["target"], []).append(edge["source"])
        return sources

    def local_filters(
        self, instance_id: str, sources: dict[str, list[str]] | None = None
    ) -> list[str]:
        """Return the instance ids of the Filter nodes upstream of a view, nearest first.

        A Filter node is a transform tagged ``filter``, such as the built-in filter by selection.
        """
        editor = self.editor
        items = {i["instance_id"]: i for i in editor._tile_items}
        sources = self._sources() if sources is None else sources
        found, seen, queue = [], {instance_id}, list(sources.get(instance_id, ()))
        while queue:
            iid = queue.pop(0)
            if iid in seen:
                continue
            seen.add(iid)
            queue += sources.get(iid, ())
            item = items.get(iid)
            spec = editor._component_specs.get(item["component_id"]) if item else None
            if spec is None or editor._node_role(item["component_id"]) != "transform":
                continue
            if "filter" in (spec.tags or ()):
                found.append(iid)
        return found

    def _sync_chips(self, shown: list[str]):
        """Work out the Filter nodes of each view, and those every view with data shares.

        A Filter node feeding every view would put the same chip on each
        tile, so the filter bar shows it once instead.
        """
        editor = self.editor
        sources = self._sources()
        chips = {}
        for iid in shown:
            chips[iid] = [(n, editor._instance_title(n)) for n in self.local_filters(iid, sources)]
        data_views = [iid for iid in shown if self.columns(iid) is not None]
        shared: list[tuple[str, str]] = []
        if len(data_views) > 1:
            first = chips[data_views[0]]
            shared = [c for c in first if all(c in chips[iid] for iid in data_views[1:])]
        self._chips = {iid: [c for c in found if c not in shared] for iid, found in chips.items()}
        if shared != self.shared:
            self.shared = shared

    def notes(self, instance_id: str) -> tuple[list[tuple[str, str]], str, str]:
        """Return the chips of a view's own Filter nodes (id, title), its hint and the hint's detail.

        Filter nodes every view shares are left out, see :attr:`shared`.
        Reuses what :meth:`apply` last worked out, so a tile costs no more than its own view.
        """
        chips = self._chips.get(instance_id, [])
        hint = detail = ""
        if instance_id in self._targets:
            columns = self.columns(instance_id)
            instance = self.editor._component_instances.get(instance_id)
            if columns is not None or accepts_filters(instance):
                mismatched = self.mismatched(instance_id)
                _, skipped = resolve_view(
                    self.filters, self._active, instance_id, columns, mismatched
                )
                hint = hint_text(self.filters, skipped)
                detail = hint_detail(self.filters, skipped, mismatched)
        return chips, hint, detail

    def _sync_notes(self, instance_id: str):
        editor = self.editor
        tile = editor._inspect_tiles.get(instance_id)
        notes = self.notes(instance_id)
        if not any(notes[:2]):
            self._notes.pop(instance_id, None)
        else:
            self._notes[instance_id] = notes
        if tile is not None:
            tile.set_filters(*notes)


class FilterBar(Viewer):
    """A widget for each global filter of a :class:`GlobalFilters`, and in edit mode a way to add and remove them."""

    filters = param.ClassSelector(class_=GlobalFilters, doc="The filters the bar shows.")

    editable = param.Boolean(default=False, doc="Whether filters can be added and removed.")

    has_content = param.Boolean(default=False, doc="Whether the bar shows anything.")

    def __init__(self, **params):
        super().__init__(**params)
        self._widgets: dict[str, tuple[t.Any, pn.Column]] = {}
        self._syncing = False
        self._pending = False
        self._chips: dict[tuple[str, str], pmui.Chip] = {}
        self._add = pmui.MenuButton(
            label="Add filter",
            icon="filter_alt",
            items=[],
            size="small",
            variant="text",
            margin=(10, 10),
            align="center",
            sx={"textTransform": "none"},
            css_classes=["fd-filter-add"],
        )
        self._add.on_click(self._on_add)
        self._row = pn.FlexBox(
            align_items="center",
            flex_wrap="wrap",
            gap="0px",
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-filter-bar"],
        )
        self.filters.param.watch(self._on_filters, ["filters", "revision", "shared"])
        self.filters.param.watch(self._on_values, "values")
        self.sync()

    def __panel__(self):
        """Render the bar."""
        return self._row

    @param.depends("editable", watch=True)
    def _on_editable(self):
        self.sync()

    def _on_filters(self, *events):
        # New data arrives at every view in turn, so the choices are read again once for all.
        if any(e.name in ("filters", "shared") for e in events):
            self.sync()
            return
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self.sync()
        elif not self._pending:
            self._pending = True
            doc.add_next_tick_callback(self._sync_pending)

    def _sync_pending(self):
        self._pending = False
        self.sync()

    def _on_values(self, _event):
        self._syncing = True
        try:
            for column, (widget, _) in self._widgets.items():
                try:
                    self._show_value(widget, self.filters.values.get(column))
                except (TypeError, ValueError) as exc:
                    logger.warning("Cannot show the filter on %r: %s", column, exc)
        finally:
            self._syncing = False

    @pn.io.hold()
    def sync(self):
        """Build a widget for each filter, reusing those whose filter is unchanged."""
        state = self.filters
        objects = []
        live = set()
        for entry in state.filters:
            column = entry["column"]
            live.add(column)
            kind = state.widget(entry)
            key = (kind, filter_label(entry))
            cached = self._widgets.get(column)
            if cached is None or getattr(cached[0], "_fd_key", None) != key:
                widget = self._make_widget(entry, kind)
                widget._fd_key = key
                remove = pmui.IconButton(
                    icon="close",
                    description=f"Remove the {filter_label(entry)} filter",
                    size="small",
                    margin=0,
                    css_classes=["fd-filter-remove"],
                    stylesheets=[_REMOVE_CSS],
                )
                remove.on_click(lambda _event, c=column: self._remove(c))
                wrapper = pn.Column(
                    widget,
                    remove,
                    margin=0,
                    width=_FILTER_WIDTH + 20,
                    styles={"position": "relative"},
                    css_classes=["fd-filter"],
                )
                cached = self._widgets[column] = (widget, wrapper)
            widget, wrapper = cached
            wrapper[1].visible = self.editable
            self._syncing = True
            try:
                self._update_choices(widget, entry, kind)
                self._show_value(widget, state.values.get(column))
            except (TypeError, ValueError) as exc:
                # One filter whose values do not suit its widget must not take the bar down.
                logger.warning("Cannot show the filter on %r: %s", column, exc)
            finally:
                self._syncing = False
            objects.append(wrapper)
        for column in [c for c in self._widgets if c not in live]:
            del self._widgets[column]
        available = state.available_columns() if self.editable else []
        self._add.items = [{"label": c, "column": c} for c in available]
        self._add.visible = bool(available)
        if self._add.visible:
            objects.append(self._add)
        objects += self._shared_chips(state.shared)
        if list(self._row) != objects:
            self._row[:] = objects
        self.has_content = bool(objects)

    def _shared_chips(self, shared: list) -> list:
        """Return a chip for each Filter node feeding every view, reusing those already made."""
        chips = []
        for iid, title in shared:
            chip = self._chips.get((iid, title))
            if chip is None:
                chip = self._chips[(iid, title)] = pmui.Chip(
                    label=f"{title} · all views",
                    icon="filter_alt",
                    color="default",
                    size="small",
                    variant="outlined",
                    description=f"Every view is filtered by {title}",
                    margin=(10, 10),
                    align="center",
                    css_classes=["fd-filter-shared"],
                )
            chips.append(chip)
        self._chips = {key: c for key, c in self._chips.items() if key in set(map(tuple, shared))}
        return chips

    def _make_widget(self, entry: dict, kind: str):
        label = filter_label(entry)
        column = entry["column"]
        if kind == "range":
            span = self.filters.span(column)
            dates = span is not None and isinstance(span[0], (dt.date, pd.Timestamp))
            cls = pmui.DateRangeSlider if dates else pmui.RangeSlider
            # A date slider needs its bounds from the start.
            bounds = {"start": span[0], "end": span[1], "value": span} if dates else {}
            widget = cls(label=label, width=_FILTER_WIDTH, margin=(10, 10), size="small", **bounds)
            widget.param.watch(lambda event: self._on_widget(column, event), "value_throttled")
        else:
            widget = pmui.MultiChoice(
                label=label,
                placeholder="All",
                width=_FILTER_WIDTH,
                margin=(10, 10),
                visual_size="small",
            )
            widget.param.watch(lambda event: self._on_widget(column, event), "value")
        widget.css_classes = [*widget.css_classes, "fd-filter-widget"]
        compact_inputs(widget)
        return widget

    def _update_choices(self, widget, entry: dict, kind: str):
        column = entry["column"]
        if kind == "range":
            span = self.filters.span(column)
            if span is None:
                widget.disabled = True
                return
            start, end = span
            if isinstance(start, pd.Timestamp):
                start, end = start.to_pydatetime(), end.to_pydatetime()
            elif hasattr(start, "item"):
                start, end = start.item(), end.item()
            if start == end:
                end = start + (dt.timedelta(days=1) if isinstance(start, dt.date) else 1)
            update = {"start": start, "end": end, "disabled": False}
            if isinstance(start, int) and isinstance(end, int):
                update["step"] = 1
            widget.param.update(update)
        else:
            options = self.filters.options(column)
            current = self.filters.values.get(column) or []
            widget.options = list(dict.fromkeys([*options, *current]))

    def _show_value(self, widget, value):
        if isinstance(widget, pmui.MultiChoice):
            value = list(value or [])
            if widget.value != value:
                widget.value = value
            return
        start, end = widget.start, widget.end
        if widget.disabled or start is None or end is None:
            return
        target = (start, end)
        if value is not None:
            kind = "pydatetime" if isinstance(widget, pmui.DateRangeSlider) else "number"
            try:
                bounds = coerce_predicate(
                    Predicate("value", "range", tuple(value)),
                    pd.Series([start], dtype=object)
                    if kind == "pydatetime"
                    else pd.Series([start]),
                ).values
            except (FilterMismatch, TypeError, ValueError):
                bounds = (None, None)
            target = tuple(
                limit if bound is None else min(max(bound, start), end)
                for bound, limit in zip(bounds, (start, end), strict=True)
            )
        if tuple(widget.value or ()) != target:
            widget.value = target

    def _on_widget(self, column: str, event):
        if self._syncing:
            return
        value = event.new
        if isinstance(event.obj, pmui.MultiChoice):
            value = list(value or []) or None
        elif value is not None:
            lo, hi = value
            start, end = event.obj.start, event.obj.end
            value = None if (lo, hi) == (start, end) else [plain_value(lo), plain_value(hi)]
        self.filters.set_value(column, value)

    def _on_add(self, item):
        column = item.get("column") if isinstance(item, dict) else None
        editor = self.filters.editor
        if column is None or editor is None:
            return
        entry = {"column": column}
        entry["widget"] = self.filters.widget(entry)
        editor.set_filters([*editor.filters, entry], label=f"Added the {column} filter")

    def _remove(self, column: str):
        editor = self.filters.editor
        if editor is None:
            return
        editor.set_filters(
            [f for f in editor.filters if f["column"] != column],
            label=f"Removed the {column} filter",
        )
