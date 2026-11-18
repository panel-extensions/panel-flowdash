"""The embeddable dataflow editor.

:class:`FlowDash` is the reusable half of the framework: a ReactFlow wiring
canvas plus a tile-grid layout editor over a set of components. It knows nothing
about routing, pages, navigation or identity, so it can be dropped into any Panel
app, notebook or template. :class:`~panel_flowdash.app.FlowDashApp` builds the
full multi-page application on top of it.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import dataclasses
import inspect
import io
import itertools
import json
import logging
import pathlib
import re
import threading
import time
import typing as t
import uuid
import weakref
from contextlib import contextmanager, nullcontext
from functools import cache, partial
from types import MappingProxyType

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
from panel.viewable import Children, Viewer
from panel_tiles import TileGrid

from panel_flowdash.auth import Permission
from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_spec import _flowdash_metadata, build_component_specs
from panel_flowdash.dashboard_store import (
    BaseDashboardStore,
    DashboardConflictError,
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    DashboardStoreError,
)
from panel_flowdash.dataflow_engine import (
    DataflowGraph,
    InputFailure,
    _release_refs,
    failure_text,
)
from panel_flowdash.failure_overlay import FailureMessage, FailureOverlay, with_failure_overlay
from panel_flowdash.history import BURST_SECONDS, EditHistory, label_for
from panel_flowdash.placeholders import (
    PLACEHOLDER_NODE_TYPE,
    Placeholder,
    UnresolvedEdge,
    broken_edges_panel,
    placeholder_view,
    unresolved_summary,
)
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.transform_editor import TransformEditor
from panel_flowdash.transforms import parse_transform, summarize_transform
from panel_flowdash.turns import DashboardTurn, TurnLog, change_labels, strip_questions
from panel_flowdash.ui.controls_bar import ControlsBar
from panel_flowdash.ui.dashboard_bar import DashboardBar
from panel_flowdash.ui.fullscreen import (
    PRESENTATION_CSS,
    PRESENTING_CLASS,
    TOGGLE_CLASS,
    FullscreenController,
    PresentationOverlay,
)
from panel_flowdash.ui.inspect_sheet import (
    dataframe_preview,
    describe_value,
    heading,
    is_dataframe,
    make_inspect_sheet,
    output_section,
    reserve_space,
    sheet_header,
    value_status,
)
from panel_flowdash.ui.inspector import Inspector
from panel_flowdash.ui.layout_panel import GRID_EDIT_CSS, LayoutPanel
from panel_flowdash.ui.node_card import (
    CARD_WIDTH,
    NodeCard,
    node_card,
    placeholder_card,
    port_tops,
)
from panel_flowdash.ui.palette import ComponentPalette, palette_entry, palette_items
from panel_flowdash.ui.settings_dialog import SettingsDialog
from panel_flowdash.ui.split_view import SplitView
from panel_flowdash.ui.states import StartCard
from panel_flowdash.ui.status_bar import StatusBar, select_on_canvas
from panel_flowdash.ui.text import (
    code_block,
    escape_markdown,
    markdown_pane,
    text_alert,
    text_typography,
)
from panel_flowdash.ui.tile import TILE_KEY_PREFIX, Tile
from panel_flowdash.ui.tile_bands import base_band, reference_width
from panel_flowdash.util import (
    close_dialog,
    gate_view,
    is_async,
    is_async_gen,
    is_failure,
    missing_inputs,
    notify,
    open_dialog,
    page_dialog,
    panel_call,
    panel_viewer,
    stepped,
)
from panel_flowdash.widgets import BUILTIN_COMPONENTS

try:
    from panel_reactflow.base import _validate_data as _validate_node_data
except ImportError:
    _validate_node_data = None


@cache
def _warn_validation_unavailable():
    logger.warning(
        "panel_reactflow no longer provides _validate_data, so node data added "
        "while loading a dashboard is not validated against its schema."
    )


if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.component_spec import ComponentSpec, InputPort, OutputPort

logger = logging.getLogger("panel_flowdash")


def _no_editor(data, schema, *, id, type, on_patch):
    return None


def _add_all(add_many, add_one, items: list[dict]):
    """Add *items* in one call, or one by one, skipping the invalid ones, if that fails."""
    try:
        add_many(items)
    except ValueError:
        for item in items:
            try:
                add_one(item)
            except ValueError:
                logger.exception("Skipping invalid canvas item '%s'", item.get("id"))


class _ReadOnlyEditorError(DashboardStoreError, RuntimeError):
    pass


# The longest node name add_component takes. Names read from a file are not limited.
MAX_NAME_LENGTH = 64

_EMPTY_GRID_CONFIG = {"breakpoints": [], "responsive_layouts": {}, "reference_width": None}

# Set on the tile grid while it previews a band without TileGrid's frame around it.
_UNFRAMED_BAND = "fd-unframed-band"


# TileGrid aligns its layout entries with its objects by position. The editor
# stores and persists them keyed by the tile's instance id instead, so removing
# a tile, or skipping one that no longer loads, cannot hand its slot to the next.


def _key_layout(layout: list, instance_ids: list[str]) -> list[dict]:
    """Tag the entries of a TileGrid layout with the instance id of their tile."""
    return [
        {**spec, "instance_id": instance_id}
        for spec, instance_id in zip(layout, instance_ids, strict=False)
        if isinstance(spec, dict)
    ]


def _key_saved_layout(layout: list | None, legacy_ids: list[str]) -> list[dict]:
    """Key a persisted layout, mapping untagged entries saved by older versions by position."""
    keyed = []
    for i, spec in enumerate(layout or []):
        if not isinstance(spec, dict):
            continue
        if "instance_id" in spec:
            keyed.append(dict(spec))
        elif i < len(legacy_ids):
            keyed.append({**spec, "instance_id": legacy_ids[i]})
    return keyed


def _align_layout(keyed: list[dict], instance_ids: list[str]) -> list[dict]:
    """Build the positional TileGrid layout for tiles *instance_ids* from keyed entries.

    Tiles without an entry get an empty one, and the visual ``index`` values are
    renumbered so that entries of removed tiles leave no gaps in the order.
    """
    by_id = {spec["instance_id"]: spec for spec in keyed}
    specs = [by_id.get(instance_id) for instance_id in instance_ids]
    ordered = sorted(
        (i for i, spec in enumerate(specs) if spec is not None and spec.get("index") is not None),
        key=lambda i: (specs[i]["index"], i),
    )
    rank = {i: r for r, i in enumerate(ordered)}
    aligned = []
    for i, spec in enumerate(specs):
        entry = {k: v for k, v in (spec or {}).items() if k != "instance_id"}
        if i in rank:
            entry["index"] = rank[i]
        aligned.append(entry)
    while aligned and not aligned[-1]:
        aligned.pop()
    return aligned


def _bounded(value: float, low: float | None, high: float | None) -> float:
    if high is not None:
        value = min(value, high)
    if low is not None:
        value = max(value, low)
    return value


def _size_layout(layout: list[dict], specs: list, reference_width: int) -> list[dict]:
    """Apply the size hints of the tiles' *specs* to a positional TileGrid layout.

    A tile without an entry starts at its ``default_size``, and every tile's size
    is kept within its ``min_size`` and ``max_size``. Hints are pixels; layout
    widths are percentages of *reference_width*, heights are pixels.
    """

    def percent(pixels):
        return None if pixels is None else round(min(100.0, pixels * 100 / reference_width), 2)

    sized = []
    entries = [*layout, *([{}] * (len(specs) - len(layout)))]
    for i, entry in enumerate(entries):
        spec = specs[i] if i < len(specs) else None
        entry = dict(entry)
        if spec is not None:
            if not entry and spec.default_size:
                default = spec.default_size
                if "width" in default:
                    entry["width"] = percent(default["width"])
                if "height" in default:
                    entry["height"] = default["height"]
            low, high = spec.min_size or {}, spec.max_size or {}
            if isinstance(entry.get("width"), (int, float)):
                entry["width"] = _bounded(
                    entry["width"], percent(low.get("width")), percent(high.get("width"))
                )
            if isinstance(entry.get("height"), (int, float)):
                entry["height"] = _bounded(entry["height"], low.get("height"), high.get("height"))
        sized.append(entry)
    while sized and not sized[-1]:
        sized.pop()
    return sized


# Screen pixels between the visible canvas edge and an auto-placed node.
_PLACEMENT_MARGIN = 40
# Flow units each repeated auto-placement shifts, so nodes don't stack exactly.
_PLACEMENT_CASCADE = 40
# Horizontal gap, in flow units, between a node dropped on a port and the node it wires to.
_DROP_WIRE_OFFSET = 360
# The canvas accepts only this drag type, so drags from unrelated lists are ignored.
_PALETTE_DRAG_TYPE = "application/x-flowdash-component"
# The DataTransfer type of files dragged in from the OS.
_FILES_DROP_TYPE = "Files"
# Left out of exports: they tie a dashboard to one store, owner and audience, and
# importing a file must not let it claim another dashboard's id, sharing rules or
# the revision a save is checked against.
_IDENTITY_FIELDS = ("dashboard_id", "user_id", "permission", "revision")
# What `ReactFlow.add_node` fills in, applied when nodes are added in bulk instead.
_NODE_DEFAULTS = {
    "type": "panel",
    "data": {},
    "position": {"x": 0.0, "y": 0.0},
    "selected": False,
    "draggable": True,
    "connectable": True,
    "deletable": True,
}
_SIDE_PANEL_WIDTH = 250

# Narrower windows show the Split tab with the inspector folded until something is selected.
_SPLIT_FOLD_WIDTH = 1600

_FLOW_STYLESHEET = """\
.react-flow__node {
  padding: 0;
  border-radius: 10px;
  border: 1px solid var(--mui-palette-divider, var(--panel-border-color));
  background-color: var(--mui-palette-background-paper, var(--panel-background-color));
  box-shadow: 0 1px 2px var(--panel-shadow-color);
  color: var(--mui-palette-text-primary, var(--panel-on-background-color));
  font-size: 13px;
  min-width: 140px;
}
.react-flow__node.selected {
  border-color: var(--mui-palette-primary-main, var(--panel-primary-color));
  box-shadow: 0 0 0 2px var(--mui-palette-primary-main, var(--panel-primary-color));
}
/* Nodes show a compact card (see ui/node_card.py) that fills the node, so the
   card's port labels line up with the handles ReactFlow spreads over its height. */
.rf-node-content {
  padding: 0;
}
.rf-node-label {
  display: none;
}
/* The card has no controls, so pointer events reach the node to select and drag it. */
.rf-node-view-wrapper,
.rf-node-view-wrapper * {
  pointer-events: none;
}
.react-flow__handle {
  width: 10px;
  height: 10px;
  border: 2px solid var(--mui-palette-primary-main, var(--panel-primary-color));
  background: var(--mui-palette-background-paper, var(--panel-background-color));
}
.react-flow__handle-right {
  background: var(--mui-palette-primary-main, var(--panel-primary-color));
}
/* The popups stay on the canvas, never over the panels beside it. */
:host {
  overflow: hidden;
}
.rf-value-popup:has(.fd-popup-leftward) {
  transform: translateX(-100%);
}
.react-flow__edge-textbg {
  fill: var(--mui-palette-background-paper, var(--panel-background-color, #fff));
  stroke: var(--mui-palette-primary-main, var(--panel-primary-color, #0072b5));
  stroke-width: 1px;
  rx: 9px;
  ry: 9px;
}
.react-flow__edge-text {
  fill: var(--mui-palette-text-primary, var(--panel-on-background-color, #222));
  font-size: 12px;
  font-weight: 500;
}
/* A failed edge's transform chip is filled in the error colour, so it reads as an issue. */
.react-flow__edge:has(.react-flow__edge-path[style*="danger"]) .react-flow__edge-textbg {
  fill: var(--mui-palette-error-main, #d32f2f);
  stroke: none;
}
.react-flow__edge:has(.react-flow__edge-path[style*="danger"]) .react-flow__edge-text {
  fill: var(--mui-palette-error-contrastText, #fff);
}"""

# A transformed edge is dashed in the accent color, so it reads as derived at a glance.
_TRANSFORMED_EDGE_STYLE = {
    "stroke": "var(--panel-primary-color, #0072b5)",
    "strokeDasharray": "6 3",
    "strokeWidth": 2,
}
# A failed edge or node is red, over any transform styling, so a failure is the first thing seen.
_FAILED_EDGE_STYLE = {"stroke": "var(--panel-danger-color, #d32f2f)", "strokeWidth": 2}
_FAILED_NODE_STYLE = {
    "borderColor": "var(--panel-danger-color, #d32f2f)",
    "boxShadow": "0 0 0 1px var(--panel-danger-color, #d32f2f)",
}


# Stands in for the component of an exported item the exporting user may not
# use. Importing it back restores the stored item, or drops it.
RESTRICTED_COMPONENT_ID = "flowdash:restricted"

_MISSING = object()


def export_model(
    model: DashboardModel, restricted: t.Collection[str] = (), *, questions: bool = True
) -> dict:
    """Serialize *model* as a dashboard file, without its identity fields.

    The file keeps the dashboard's turns and each item's origin, so a
    dashboard that is imported elsewhere still shows how it was built.

    The items whose instance ids are in *restricted* keep only their id and
    position, and the edges touching them are left out, so the file reveals
    neither their component, config nor ports.

    With *questions* False the turns leave out what was asked of the agent,
    for a file shared beyond the people who asked.
    """
    data = model.to_dict()
    for key in _IDENTITY_FIELDS:
        del data[key]
    if not questions:
        data["turns"] = strip_questions(data["turns"])
    if restricted:
        data["items"] = [
            {**item, "component_id": RESTRICTED_COMPONENT_ID, "config": {}}
            if item["instance_id"] in restricted
            else item
            for item in data["items"]
        ]
        data["edges"] = [
            edge
            for edge in data["edges"]
            if edge["source"] not in restricted and edge["target"] not in restricted
        ]
    return data


# The file extensions a dashboard file may have, JSON first.
DASHBOARD_FILE_EXTENSIONS = (".json", ".yaml", ".yml")


def export_filename(title: str, extension: str = "json") -> str:
    """File name a downloaded dashboard titled *title* is saved under, as JSON or YAML."""
    stem = re.sub(r"[^\w-]+", "_", title).strip("_") or "dashboard"
    return f"{stem}.{extension}"


def export_text(data: dict, extension: str = "json", restricted_edges=()) -> str:
    """Return a dashboard export as the text of a JSON or YAML file."""
    if extension == "yaml":
        from panel_flowdash.agent.yaml_format import dashboard_to_yaml

        return dashboard_to_yaml(data, restricted_edges)
    return json.dumps(data, indent=2)


# The editor whose component is being created, for `current_dashboard`.
_INSTANTIATING: contextvars.ContextVar[FlowDash | None] = contextvars.ContextVar(
    "panel_flowdash_instantiating", default=None
)
# The latest editor of each served session; weak on both sides so a session can end.
_SESSION_EDITORS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def current_dashboard() -> FlowDash | None:
    """Return the editor that is showing the component calling this.

    Call it from a component's ``__init__`` or function body. It gives access
    to the editor's ``settings`` and its ``refresh`` event without importing
    anything from the application. Later, for example from a callback, it falls
    back to the latest editor created in the current session. Outside a session
    and outside component creation there is no editor, and it returns ``None``,
    as it does for a component that is rendered on its own.
    """
    editor = _INSTANTIATING.get()
    if editor is None and pn.state.curdoc is not None:
        ref = _SESSION_EDITORS.get(pn.state.curdoc)
        editor = ref() if ref is not None else None
    return editor


def copy_context(context: t.Mapping[str, t.Any]) -> dict[str, t.Any]:
    """Copy an editor's context one level down, so dicts, lists and sets in it are not shared."""
    return {
        key: copy.copy(value) if isinstance(value, (dict, list, set)) else value
        for key, value in context.items()
    }


def dashboard_context() -> t.Mapping[str, t.Any]:
    """Return a read-only view of the resources the host gave the editor showing this component.

    Hosts hand resources to components through ``FlowDash(context=...)`` or
    by adding to ``editor.context``, e.g. Lumen keeps its source bindings
    under ``editor.context["lumen.sources"]``. Call it where
    :func:`current_dashboard` works: while a component is created and while
    one of its outputs runs; without an editor the view is empty. The view
    follows later additions to the editor's dict, so code that runs in
    threads of its own should take the view when the component is created.
    """
    editor = current_dashboard()
    return MappingProxyType(editor.context if editor is not None else {})


def _drop_repeated_ids(model: DashboardModel) -> int:
    """Keep only the first item of *model* with each instance id, returning how many were dropped."""
    seen: set[str] = set()
    unique = []
    for item in model.items:
        if item.instance_id not in seen:
            seen.add(item.instance_id)
            unique.append(item)
    dropped = len(model.items) - len(unique)
    if dropped:
        model.items = unique
    return dropped


@contextmanager
def _creating(editor_ref: weakref.ref[FlowDash], instance_id: str, wiring: list):
    """Run one step of an async component body as part of creating *instance_id*.

    Like synchronous creation, `current_dashboard` returns the editor and every
    subscription the step makes is released with the component.
    """
    editor = editor_ref()
    if editor is None:
        yield
        return
    before = editor._subscriptions()
    token = _INSTANTIATING.set(editor)
    try:
        yield
    finally:
        _INSTANTIATING.reset(token)
        editor._track_subscriptions(instance_id, before, wiring)


@contextmanager
def _current(editor_ref: weakref.ref[FlowDash]):
    """Make `current_dashboard` return the editor while one of its components' outputs runs."""
    token = _INSTANTIATING.set(editor_ref())
    try:
        yield
    finally:
        _INSTANTIATING.reset(token)


class _ViewRuns:
    """The steps of the runs of an async ``__panel__``, keeping the latest run's subscriptions.

    A ``__panel__`` that declares ``param.depends`` runs again whenever its
    dependencies change, and its new view replaces the old one. Starting a run
    releases what the previous run subscribed to, and a run that was superseded
    while still awaiting releases its later subscriptions right away.
    """

    def __init__(self, editor_ref: weakref.ref[FlowDash], instance_id: str, wiring: list):
        self._editor_ref = editor_ref
        self._instance_id = instance_id
        self._wiring = wiring
        self._latest: list | None = None

    def __call__(self) -> t.Callable[[], t.ContextManager]:
        if self._latest is not None:
            self._release(self._latest)
        run = self._latest = []
        return partial(self._step, run)

    def _release(self, releases: list):
        for release in releases:
            # Releasing the component's wiring has run (and dropped) it already.
            if release in self._wiring:
                self._wiring.remove(release)
                release()
        releases.clear()

    @contextmanager
    def _step(self, run: list):
        before = {id(release) for release in self._wiring}
        try:
            with _creating(self._editor_ref, self._instance_id, self._wiring):
                yield
        finally:
            run.extend(release for release in self._wiring if id(release) not in before)
            if run is not self._latest:
                self._release(run)


def _dispose_on_session_end(editor_ref: weakref.ref[FlowDash], session_context):
    editor = editor_ref()
    if editor is not None:
        editor._stop_refresh_timer()
        editor._dispose_components()


class _Outlet(param.Parameterized):
    """Holds a single async output reference of a component, apart from its node state."""

    # Param re-runs every async reference on an object whenever any of that
    # object's references fires, so an async output resolved on the node state
    # would also start each time one of the node's inputs changes, on top of
    # the run its own dependencies trigger (fixed upstream by holoviz/param#1233).
    value = param.Parameter(allow_refs=True)


class FlowDash(Viewer):
    """A dataflow wiring canvas and dashboard layout editor over a set of components.

    The editor pairs a ReactFlow canvas, where components are placed and their
    typed ports wired together, with a tile grid that lays the same components
    out as a dashboard. Everything to do with routing, pages and identity lives
    in :class:`~panel_flowdash.app.FlowDashApp` instead, so this can be embedded
    anywhere.

    Parameters
    ----------
    components
        The components to offer. Accepts a decorated function, a ``Viewer``
        subclass, a mapping of explicit component ids, a project directory to
        scan, or a list mixing any of those. See
        :func:`~panel_flowdash.component_library.normalize_components`.
    settings_class
        A subclass of :class:`~panel_flowdash.settings.DashboardSettings` adding
        dashboard-wide parameters of your own, persisted with each dashboard.

    Examples
    --------
    >>> editor = FlowDash(components=[ticker_select, price_chart])
    >>> src = editor.add_component("Components/ticker_select")
    >>> dst = editor.add_component("Components/price_chart")
    >>> editor.connect(src, "ticker", dst, "ticker")
    True
    """

    authorize_component = param.Callable(
        default=None,
        doc="""
        Called with a component's `RegistryEntry` to decide whether this
        session may use it. Components it refuses are left out of the palette
        and `add_component` raises `PermissionError` for them; a loaded
        dashboard shows them as locked placeholders that are never created
        and are saved back unchanged. `FlowDashApp`
        supplies one that evaluates each component's ``allow_*``/``deny_*``
        rules and ``authorize`` callback against the signed-in user. None
        allows every component.""",
    )

    authorize_dashboard = param.Callable(
        default=None,
        doc="""
        Called with the stored `DashboardModel` and the access it needs,
        ``"view"`` or ``"edit"``, to decide whether this session may still
        read or overwrite it. `save` asks for ``"edit"`` against the
        dashboard's current sharing rules, so edit rights revoked from another
        session take effect without reloading; reloading and discarding
        changes ask for ``"view"``. `FlowDashApp` supplies one that checks the
        signed-in user. None allows both.""",
    )

    breakpoints = param.List(default=[768, 1200], doc="Responsive breakpoints for the tile grid.")

    import_unknown_components = param.Boolean(
        default=True,
        doc="""
        Whether importing a file may add or change items whose component this
        editor does not offer. They are imported as they are, shown as
        unavailable and saved back unchanged, so a dashboard keeps them until
        the component is offered again. Set it to False when users are
        restricted by giving each a smaller set of components rather than
        through `authorize_component`: a component one user lacks may be one
        another has, so such an item is then treated like a restricted one,
        keeping its stored config and wiring or being left out of the import.
        `authorize_component` is the better mechanism, since it also shows a
        known but denied component as restricted rather than unavailable.""",
    )

    components = param.Parameter(
        default=None,
        doc="""
        The components to offer in the editor. A decorated function, a Viewer
        subclass, a mapping of explicit component ids, a project directory, or a
        list mixing any of those. Read at construction time.""",
    )

    context = param.Dict(
        default={},
        doc="""
        Resources the host hands to components, such as connections or data
        sources, by key. Components read them with `dashboard_context()`.
        Prefix keys with your package's name, e.g. "lumen.sources". The
        editor copies the dict it is given, and each dict, list or set in it,
        so editors built from one dict, and previews built from an editor,
        can replace or add entries without affecting each other; other values
        are shared objects. Add to the dict rather than reassigning it, since
        views components already hold do not follow a new dict.""",
    )

    controls_bar = param.Boolean(
        default=True,
        doc="""
        Whether to show the views of components registered with
        ``sidebar=True`` beside the tiles (see `controls_position`), while
        the dashboard is viewed or laid out. Turn it off to render the
        `sidebar` views in a layout of your own instead. In full screen the
        editor shows them either way, since nothing around it is visible, so
        leave them out of your layout while `fullscreen` is on.""",
    )

    controls_position = param.Selector(
        default="top",
        objects=["top", "left"],
        doc="""
        Default of the ``controls_position`` dashboard setting for dashboards
        that do not store one: where the views of components registered with
        ``sidebar=True`` go. "top" puts them in one slim row above the tiles,
        which suits the few filters most dashboards have; "left" stacks them
        in a collapsible panel left of the tiles, which suits many controls.""",
    )

    header_layout = param.Selector(
        default="compact",
        objects=["compact", "full"],
        doc="""
        Default of the ``header_layout`` dashboard setting for dashboards that
        do not store one: how the dashboard bar is laid out. "compact" keeps
        it to one line, which a FlowDashApp with the default navigation folds
        into its top bar; "full" gives it a row of its own with a large title
        over the description, the health chip and when the dashboard was last
        refreshed.""",
    )

    include_builtin_components = param.Boolean(
        default=True, doc="Include Select, MultiChoice and Slider in the component palette."
    )

    inspect_outputs = param.Boolean(
        default=False,
        doc="""
        Whether each tile in the dashboard grid gets an Inspect action that
        opens a side sheet with the current value of every output the
        component publishes. The action is shown in the dashboard view and in
        preview, not while arranging tiles. It reveals outputs the tile itself
        may not display, so while the editor is ``read_only`` (in a
        ``FlowDashApp``, for anyone who may not edit the dashboard) it is only
        shown if the dashboard's ``viewers_can_inspect`` setting allows it. A
        component can add its own explanation per output by defining
        ``__flowdash_inspect__(self, port)``.""",
    )

    include_page_dialog = param.Boolean(
        default=True,
        doc="""
        Whether the editor lays out the session's page dialog, the one
        `open_dialog` and `confirm` use. Set it to False when the surrounding
        page already places `page_dialog()` itself, as `FlowDashApp` does; the
        dialog must appear in only one layout. Read when the workspace is built,
        so it needs a served session to take effect.""",
    )

    dashboard = param.ClassSelector(
        class_=DashboardModel,
        default=None,
        doc="""
        The dashboard currently loaded. Updated by `load`, `load_model`,
        `new_dashboard` and `save`. May be passed at construction as either a
        DashboardModel or, when a store is configured, a dashboard id or title.""",
    )

    dirty = param.Boolean(
        default=False,
        doc="""
        Whether the canvas has unsaved changes. Managed by the editor; watch it
        to prompt before discarding work.""",
    )

    editable = param.Boolean(
        default=True,
        doc="""
        Whether the dashboard can be edited. When False the side panel is hidden
        and the tile grid is shown locked, giving a pure view of the dashboard.""",
    )

    fullscreen = param.Boolean(
        default=False,
        doc="""
        Whether the dashboard is presented on its own: the tiles and the
        controls bar fill the screen, without the dashboard bar or the page
        around the editor, and a small overlay shows the title, the health and
        an exit button while the pointer moves. Turning it on from a click
        also puts the editor into the browser's full screen; otherwise, e.g.
        when set at load time, the editor covers the browser window. Leaving
        the browser's full screen turns it off. Only available in view mode:
        switching to edit mode turns it off.""",
    )

    history = param.Boolean(
        default=True,
        doc="""
        Whether every edit becomes a version of the dashboard's edit history,
        which `undo` and `redo` move through and the store keeps across
        sessions (see `history_limit`). Read at each edit.""",
    )

    history_coalesce = param.Number(
        default=BURST_SECONDS,
        bounds=(0, None),
        doc="""
        Seconds without an edit that end a batch of the person's edits in
        the edit history, so a burst of edits, such as a drag, typing into
        a config field or a few quick changes, is one version. An edit of the
        agent, a save, undo and redo end a batch too, and each request to the
        agent is one version. Zero records each of the person's edits on its own.""",
    )

    history_limit = param.Integer(
        default=500,
        bounds=(1, None),
        allow_None=True,
        doc="""
        The most versions a dashboard's stored history keeps; older ones are
        dropped. None keeps them all.""",
    )

    history_max_days = param.Number(
        default=90,
        bounds=(0, None),
        allow_None=True,
        doc="""
        How many days the store keeps a version of a dashboard's history.
        None keeps versions however old they are.""",
    )

    mode = param.Selector(
        default="wiring",
        objects=["wiring", "dashboard", "split"],
        doc="""
        Which workspace is shown: 'wiring' for the ReactFlow canvas, 'dashboard'
        for the tile grid and 'split' for both side by side, with the canvas
        on the left. Only meaningful while `editable`.""",
    )

    node_previews = param.Boolean(
        default=False,
        doc="""
        Default of the ``node_previews`` dashboard setting for dashboards that
        do not store one: whether each card on the wiring canvas previews its
        current value below its ports, e.g. the first rows of a table or a
        sparkline of a series, rather than summarizing it in one line.""",
    )

    notifications = param.Boolean(
        default=True,
        doc="""
        Whether to surface user-facing messages as Panel notifications. When
        disabled (or when no notification area exists) messages are logged.""",
    )

    preview = param.Boolean(
        default=False,
        doc="""
        Lock the tile grid as an end user sees it without leaving edit mode.
        Only meaningful while `editable` and in 'dashboard' mode. The
        dashboard bar offers no control for it, since view mode is the preview.""",
    )

    propagate_failures = param.Boolean(
        default=False,
        doc="""
        Carry a failing output or edge transform downstream instead of leaving
        the connected inputs on their last value. An input declared in
        `handles_failures` receives the failure as a `param.ReactiveError`; the
        tile of a component with any other failed input is covered by a message
        naming the failure, and its outputs pass the failure on. Read at
        construction time.""",
    )

    read_only = param.Boolean(
        default=False,
        doc="""
        Whether saving is forbidden. The canvas can still be rearranged but
        `save` refuses. Set this from your own authorization logic.""",
    )

    refresh = param.Event(
        doc="""
        Triggered every ``settings.refresh_period`` seconds while a dashboard is
        shown, and whenever the user clicks the refresh button. Components that
        show live data subscribe to it, see `current_dashboard`.""",
    )

    saved = param.Event(doc="Triggered after a dashboard is successfully saved.")

    settings = param.ClassSelector(
        class_=DashboardSettings,
        constant=True,
        doc="""
        The dashboard-wide settings, an instance of `settings_class`. Filled
        from the loaded dashboard and written back on save. Edited in edit mode
        through the dashboard bar's Settings form.""",
    )

    settings_class = param.ClassSelector(
        class_=DashboardSettings,
        default=DashboardSettings,
        is_instance=False,
        doc="""
        The class of `settings`. Subclass `DashboardSettings` to add your own
        dashboard-wide parameters. Read at construction time.""",
    )

    show_in_editor = param.Callable(
        default=None,
        allow_None=True,
        doc="""
        Called with a tile's instance id when someone viewing the dashboard
        asks to see that component in the editor, from the tile's More menu or
        its failure message. The action is offered in view mode while this is
        set and `read_only` is False; the caller decides what showing a
        component in the editor means, e.g. switching to edit mode.""",
    )

    sidebar = Children(
        default=[],
        doc="""
        Views of placed components that declare `sidebar=True`, which are kept
        out of the tile grid. Managed by the editor; render these wherever your
        layout wants them.""",
    )

    store = param.ClassSelector(
        class_=BaseDashboardStore,
        default=None,
        doc="""
        Dashboard persistence backend. Accepts a store instance or a path to a
        SQLite file. When None the editor is ephemeral and `save` merely returns
        the model for the caller to persist.""",
    )

    tile_cards = param.Boolean(
        default=True,
        doc="""
        Default of the ``tile_cards`` dashboard setting for dashboards that do
        not store one: whether tiles are drawn as framed cards. Each dashboard
        can change it in its settings.""",
    )

    tile_titles = param.Boolean(
        default=False,
        doc="""
        Default of the ``tile_titles`` dashboard setting for dashboards that do
        not store one: whether a tile's card shows its title.""",
    )

    toolbar = param.Boolean(
        default=True,
        doc="""
        Whether to render the dashboard bar above the workspace. In view mode
        it holds the title, Refresh and the View/Edit control; in edit mode
        the Dataflow/Layout tabs and the Settings, Discard, Save and Done
        actions. The component palette is shown regardless.""",
    )

    popup_trigger = param.ObjectSelector(
        default="click",
        objects=["click", "hover", "none"],
        doc="""
        Whether live port values open on click, hover, or not automatically.
        On hover the popups cover the nodes the pointer passes over, so they
        open on click by default.""",
    )

    popup_hover_delay = param.Integer(
        default=500,
        bounds=(0, None),
        doc="Delay in milliseconds before a hover value preview opens.",
    )

    value_repr = param.Callable(
        default=None,
        allow_None=True,
        doc="Optional callable converting a live port value to a Panel viewable or displayable value.",
    )

    toolbar_extra = Children(
        default=[],
        doc="""
        Additional items placed in the dashboard bar's actions, before the
        View/Edit control in view mode and after Settings in edit mode.""",
    )

    user = param.String(
        default="local", doc="Principal recorded as the owner of dashboards created here."
    )

    def __init__(self, components=None, **params):
        if components is not None:
            params["components"] = components
        if isinstance(params.get("store"), (str, pathlib.Path)):
            params["store"] = DashboardStore(params["store"])
        # Held back until the canvas exists, and resolved from an id if needed.
        dashboard = params.pop("dashboard", None)
        params["settings"] = params.get("settings_class", type(self).settings_class)()
        # A tab the host asked for wins over the one remembered by the browser.
        self._restore_split = "mode" not in params

        super().__init__(**params)
        self.context = copy_context(self.context)

        self._registry: dict[str, RegistryEntry] = normalize_components(self.components)
        if self.include_builtin_components:
            for app_id, entry in normalize_components(BUILTIN_COMPONENTS).items():
                self._registry.setdefault(app_id, entry)
        self._component_entries = {k: v for k, v in self._registry.items() if v.metadata.component}
        self._component_specs: dict[str, ComponentSpec] = {}
        self._components_loaded = False
        self._failed_components: set[str] = set()

        # The format the Download action writes, "json" or "yaml".
        self._download_format = "json"
        self._muted = False
        self._canvas_batch: tuple[list[dict], list[dict]] | None = None
        self._grid_populated = False
        self._edge_count = 0
        self._edge_id_map: dict[str, tuple[str, str, str, str]] = {}
        self._tile_items: list[dict] = []
        self._tile_objects: list[Viewable] = []
        # Saved edges that could not be wired, kept to be saved unchanged, and the
        # notice about them waiting for an editor to see it.
        self._unresolved_edges: list[UnresolvedEdge] = []
        self._unresolved_notice: str | None = None
        # Layout entries keyed by instance id, waiting for the grid to be rendered.
        self._pending_tile_layout: list[dict] = []
        # Instance ids of the tiles currently in the grid, in grid order; None
        # marks the slot of a tile removed while the grid was not rebuilt.
        self._grid_ids: list[str | None] = []
        # The framed tiles of the grid, by instance id, kept so a grid rebuild
        # does not re-render them.
        self._inspect_tiles: dict[str, Tile] = {}
        # Each tile carries a unique key as a CSS class, so it can be found on the page.
        self._tile_keys = itertools.count()
        # The tab Split returns to when it is toggled off.
        self._mode_before_split = "wiring"
        self._split_shown = False
        # Whether the inspector was folded to make room for the split, rather than by the user.
        self._inspector_folded = False
        self._folding = False
        self._pending_reveal: str | None = None
        self._tile_selection_follows = True
        self._rebuilding_grid = False
        self._inspected_id: str | None = None
        # Responsive grid settings waiting to be applied; empty values must also be
        # applied so a loaded dashboard doesn't inherit the previous one's overrides.
        self._pending_grid_config: dict | None = None
        self._inspection_key: tuple[str, str] | None = None

        # The turns of the dashboard and the turn each item was created in.
        self._turn_log = TurnLog()
        self._origins: dict[str, str] = {}
        self._hydrating = False
        self._recording_edit = False
        self.param.watch(self._on_dirty, "dirty", onlychanged=False)
        self._history = EditHistory(self)
        self._restricted_seen: dict[str, DashboardItem] = {}
        self._restricted_seen_edges: list[DashboardEdge] = []
        # Set while the canvas is loaded as part of an edit or an undo, which opens no new history.
        self._history_quiet = False

        self._settings_names = [name for name in self.settings.param if name != "name"]
        self._loading_settings = False
        self._refresh_timer = None
        self._refresh_ms = 0
        self.settings.param.watch(self._on_setting_changed, self._settings_names)
        if pn.state.curdoc is not None:
            _SESSION_EDITORS[pn.state.curdoc] = weakref.ref(self)

        self._dataflow_graph = DataflowGraph(
            {},
            on_error=self._on_wiring_error,
            propagate_failures=self.propagate_failures,
            hold=pn.io.hold,
        )
        self._component_instances: dict[str, Viewer] = {}
        self._component_wiring: dict[str, list[t.Callable[[], None]]] = {}
        # The exception of each output whose latest run failed, by instance id and port.
        self._output_failures: dict[str, dict[str, Exception]] = {}
        self._transform_editors: dict[str, TransformEditor] = {}
        self._load_component_extensions()
        self._palette = self._make_palette()
        self._flow = self._build_flow_canvas()
        self.param.watch(
            self._update_value_popup_config,
            ["popup_trigger", "popup_hover_delay"],
        )
        self._view = self._build_component_view()

        # Components handed over as live objects need no import, so their specs
        # can be built eagerly and the editor is usable the moment it is
        # constructed. Directory-scanned entries are imported lazily instead,
        # off the event loop, by `ensure_components_loaded_async`.
        if all(entry.app is not None for entry in self._component_entries.values()):
            self.ensure_components_loaded()

        self._apply_mode_state()
        # A watcher only runs on changes, so `fullscreen=True` given here is applied by hand.
        self._apply_fullscreen()
        if dashboard is not None:
            self._init_dashboard(dashboard)
        else:
            self._history.opened()

        # Without a session, Panel would register the callback for every future
        # session instead. An editor built inside a session is tied to it, so one
        # shared from there (e.g. through ``pn.state.as_cached``) is disposed when
        # that session ends. The weakref lets an app discard and rebuild editors
        # within a session without keeping the old ones alive until it ends.
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None:
            pn.state.on_session_destroyed(partial(_dispose_on_session_end, weakref.ref(self)))

    def dispose(self):
        """Tear down the editor, disposing every placed component.

        Empties the dataflow graph, the canvas and the tile grid, then calls the
        ``dispose`` hook of each component that was placed, once. The editor is
        left empty and consistent, so it can still be used afterwards.

        An editor created inside a server session disposes its components
        automatically when that session ends. In a notebook, under pyodide or
        for an editor created outside a session, call this yourself.
        """
        self._history.flush()
        self._stop_refresh_timer()
        self._reset_canvas()

    def _dispose_components(self):
        """Detach and dispose every placed component without touching the UI.

        Used once the session's Document is already destroyed, when pushing
        model updates would be pointless.
        """
        # Unwired first: removing a node resets the inputs downstream of it,
        # which would re-render components on the destroyed Document.
        for instance_id in list(self._component_wiring):
            self._release_wiring(instance_id)
        for node_id in list(self._dataflow_graph.node_ids):
            self._dataflow_graph.remove_node(node_id)
        self._inspect_tiles.clear()
        for instance_id in [*self._component_instances, *self._component_wiring]:
            self._dispose_component(instance_id)

    def _dispose_component(self, instance_id: str):
        """Call the ``dispose`` hook of the component placed as *instance_id*, if it has one.

        The watchers and output references wiring the component to its node
        are released first, so nothing flows into or out of it any more.
        Popping the instance first makes this a no-op on any later call, so a
        component is disposed at most once however it leaves the editor.
        """
        self._release_wiring(instance_id)
        instance = self._component_instances.pop(instance_id, None)
        hook = getattr(instance, "dispose", None)
        if not callable(hook):
            return
        try:
            result = hook()
        except Exception:
            logger.exception("dispose() of component '%s' failed", instance_id)
            return
        if inspect.isawaitable(result):
            pn.state.execute(partial(self._await_dispose, instance_id, result), schedule=True)

    def _release_wiring(self, instance_id: str):
        releases = self._component_wiring.pop(instance_id, [])
        for release in releases:
            try:
                release()
            except Exception:
                logger.exception("Releasing the wiring of component '%s' failed", instance_id)
        # A pending async body still holds the list; emptied, it holds no watchers.
        releases.clear()

    @staticmethod
    async def _await_dispose(instance_id: str, awaitable):
        try:
            await awaitable
        except Exception:
            logger.exception("dispose() of component '%s' failed", instance_id)

    def _init_dashboard(self, dashboard):
        """Resolve the ``dashboard`` constructor argument to a loaded model."""
        if isinstance(dashboard, DashboardModel):
            self.load_model(dashboard)
        elif self.store is not None:
            self.load(dashboard)
        else:
            raise ValueError(
                f"Cannot load dashboard {dashboard!r} without a store; pass a "
                "DashboardModel or configure store=."
            )

    # ------------------------------------------------------------------
    # Component loading
    # ------------------------------------------------------------------

    def _wanted_ids(self, component_ids: t.Iterable[str] | None) -> list[str]:
        """Resolve a requested id set against the registry, in registry order.

        Ids naming no registered component are dropped; reporting them is the
        caller's job, since a saved dashboard referencing a component the editor
        does not offer is a warning rather than a load failure.
        """
        if component_ids is None:
            return list(self._component_entries)
        aliases = self._component_aliases()
        wanted = {self._resolve_component_id(cid, aliases) for cid in component_ids}
        return [cid for cid in self._component_entries if cid in wanted]

    def _component_aliases(self) -> dict[str, str]:
        """Map the former ids components declare with ``aliases`` to their current ids.

        A registered id always wins over an alias, and of two components
        claiming one alias the first in registry order wins.
        """
        aliases: dict[str, str] = {}
        for app_id, entry in self._component_entries.items():
            for alias in entry.metadata.aliases:
                if alias in self._component_entries:
                    continue
                owner = aliases.setdefault(alias, app_id)
                if owner != app_id:
                    logger.warning(
                        "Component alias '%s' is claimed by both '%s' and '%s'; using '%s'.",
                        alias,
                        owner,
                        app_id,
                        owner,
                    )
        return aliases

    def _resolve_component_id(
        self, component_id: str, aliases: dict[str, str] | None = None
    ) -> str:
        """Return the current id of a component a saved dashboard may name by a former id."""
        if component_id in self._component_entries:
            return component_id
        if aliases is None:
            aliases = self._component_aliases()
        return aliases.get(component_id, component_id)

    def _unimported_ids(self, wanted: list[str]) -> list[str]:
        """Of *wanted*, the ids whose module has not been imported yet.

        Components that already failed to import are excluded, so a broken module
        is not retried (and re-reported) on every load.
        """
        return [
            cid
            for cid in wanted
            if self._component_entries[cid].app is None and cid not in self._failed_components
        ]

    def _load_entries(self, component_ids: list[str]) -> list[str]:
        """Import the named component modules, collecting failures rather than raising."""
        errors: list[str] = []
        for component_id in component_ids:
            try:
                self._component_entries[component_id].load()
            except Exception as exc:
                self._failed_components.add(component_id)
                errors.append(f"{component_id}: {exc}")
        return errors

    def _register_specs(self, wanted: list[str], errors: list[str] | None = None):
        """Introspect the loaded components in *wanted* and register their specs.

        Specs are added to the graph incrementally, so this is safe to call after
        nodes have been placed: an existing graph keeps its nodes and edges.
        """
        for msg in errors or []:
            logger.warning("Failed to load component: %s", msg)
            self._notify("warning", f"Component load failed: {msg}", duration=6000)
        new = [
            cid
            for cid in wanted
            if cid not in self._component_specs and self._component_entries[cid].app is not None
        ]
        if not new:
            return
        specs = build_component_specs(self._registry, component_ids=new)
        self._component_specs.update(specs)
        self._dataflow_graph.register_specs(specs)
        self._rebuild_flow_canvas()

    def ensure_components_loaded(self, component_ids: t.Iterable[str] | None = None):
        """Import component modules and build their specs, if not done already.

        Called automatically whenever specs are needed. On a live server prefer
        :meth:`ensure_components_loaded_async`, which imports off the event loop.

        Parameters
        ----------
        component_ids
            Import only these components. Defaults to the whole catalog, which is
            what the editor palette needs; viewing a dashboard passes just the
            components it places, so an unrelated component doing work at import
            time cannot slow it down.
        """
        wanted = self._wanted_ids(component_ids)
        errors = self._load_entries(self._unimported_ids(wanted))
        self._register_specs(wanted, errors)
        if component_ids is None:
            self._components_loaded = True

    async def ensure_components_loaded_async(self, component_ids: t.Iterable[str] | None = None):
        """Async :meth:`ensure_components_loaded`, importing off the event loop.

        If the awaiting task is cancelled (e.g. by a newer navigation) the
        imports already running finish in their thread, but nothing is marked
        loaded and their import errors are not reported; the next call
        retries whatever is still unimported and reports its errors then.
        """
        wanted = self._wanted_ids(component_ids)
        unimported = self._unimported_ids(wanted)
        errors = await asyncio.to_thread(self._load_entries, unimported) if unimported else []
        self._register_specs(wanted, errors)
        if component_ids is None:
            self._components_loaded = True

    @property
    def component_specs(self) -> dict[str, ComponentSpec]:
        """Specs for the available components, keyed by component id."""
        self.ensure_components_loaded()
        return self._component_specs

    @property
    def graph(self) -> DataflowGraph:
        """The live dataflow graph wiring the placed components together."""
        return self._dataflow_graph

    # ------------------------------------------------------------------
    # Notifications and error reporting
    # ------------------------------------------------------------------

    def _notify(self, severity: str, message: str, duration: int = 3000):
        """Surface a message to the user, or log it when notifications are unavailable."""
        notify(severity, message, duration=duration, enabled=self.notifications)

    def _on_wiring_error(self, source_id, source_port, target_id, target_port, exc):
        logger.error(
            "Runtime wiring error (%s.%s -> %s.%s): %s",
            source_id,
            source_port,
            target_id,
            target_port,
            exc,
            exc_info=exc,
        )
        self._notify(
            "error",
            f"Runtime wiring error ({source_port} → {target_port}): {exc}",
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Canvas construction
    # ------------------------------------------------------------------

    def _load_component_extensions(self):
        """Load the Panel extensions every offered component declares.

        Components are imported lazily, after the page has fetched its
        JavaScript, so an extension first loaded by a dropped component would
        never reach the browser and its view would break the canvas.
        """
        extensions = sorted(
            {
                ext
                for entry in self._component_entries.values()
                for ext in entry.metadata.extensions
            }
        )
        if extensions:
            pn.extension(*extensions)

    def _make_palette(self):
        self._palette_panel = ComponentPalette(
            drag_type=_PALETTE_DRAG_TYPE,
            items=self._palette_items,
            on_click=self._on_palette_clicked,
        )
        return self._palette_panel.menu

    def _palette_items(self) -> list[dict]:
        placed = {item["component_id"] for item in self._tile_items}
        entries = []
        for app_id, entry in self._component_entries.items():
            if not self._component_authorized(app_id):
                continue
            spec = self._component_specs.get(app_id)
            entries.append(palette_entry(entry, spec, placed=app_id in placed))
        return palette_items(entries, self._palette_panel.query)

    def _component_authorized(self, component_id: str) -> bool:
        """Whether `authorize_component` lets this session use *component_id*."""
        entry = self._component_entries.get(component_id)
        if entry is None:
            return False
        if self.authorize_component is None:
            return True
        try:
            return bool(self.authorize_component(entry))
        except Exception:
            logger.exception("authorize_component failed for '%s'; denying it", component_id)
            return False

    @param.depends("authorize_component", watch=True)
    def _sync_authorized_palette(self):
        self._sync_toolbar_state()

    def _node_types_from_specs(self):
        node_types = {}
        node_editors = {}
        for comp_id, spec in self._component_specs.items():
            type_key = comp_id.replace("/", "__")
            tops_in, tops_out = port_tops(len(spec.inputs), len(spec.outputs))
            node_types[type_key] = pr.NodeType(
                type=type_key,
                label=spec.title,
                schema=spec.config_state_class,
                inputs=[
                    {
                        "id": port.name,
                        "label": port.label or port.name,
                        "type": port.type,
                        "top": round(top, 2),
                        **(
                            {"maxConnections": 1}
                            if port.multiple is False
                            or (port.multiple is None and (port.type or "").lower() != "list")
                            else {}
                        ),
                    }
                    for port, top in zip(spec.inputs, tops_in, strict=True)
                ],
                outputs=[
                    {
                        "id": port.name,
                        "label": port.label or port.name,
                        "type": port.type,
                        "top": round(top, 2),
                    }
                    for port, top in zip(spec.outputs, tops_out, strict=True)
                ],
            )
        node_types[PLACEHOLDER_NODE_TYPE] = pr.NodeType(
            type=PLACEHOLDER_NODE_TYPE, label="Unavailable component", inputs=[], outputs=[]
        )
        return node_types, node_editors

    def _rebuild_flow_canvas(self):
        """Update node_types on the live ReactFlow canvas after component load."""
        node_types, node_editors = self._node_types_from_specs()
        self._flow.param.update(node_types=node_types, node_editors=node_editors)

    def _update_value_popup_config(self, event):
        self._flow.param.update(
            popup_trigger=self.popup_trigger,
            popup_hover_delay=self.popup_hover_delay,
        )
        if self.popup_trigger != "hover":
            self._inspection_key = None
            self._flow.close_popup()

    def _build_flow_canvas(self):
        node_types, node_editors = self._node_types_from_specs()

        flow = pr.ReactFlow(
            nodes=[],
            edges=[],
            node_types=node_types,
            node_editors=node_editors,
            # Config and transforms are edited in the inspector, so ReactFlow
            # builds no node or edge editors.
            default_node_editor=_no_editor,
            default_edge_editor=_no_editor,
            editable=True,
            editor_mode="side",
            enable_connect=True,
            # Fitting the view uses the whole pane, so a corner minimap would cover
            # the nodes there; the status bar offers it for large graphs instead.
            show_minimap=False,
            sizing_mode="stretch_both",
            min_height=600,
            stylesheets=[_FLOW_STYLESHEET],
            popup_trigger=self.popup_trigger,
            popup_hover_delay=self.popup_hover_delay,
            drop_types=[_PALETTE_DRAG_TYPE, _FILES_DROP_TYPE],
            connection_validation={
                "direction": True,
                "cycles": True,
                "duplicates": True,
                "capacity": True,
            },
        )
        flow.on("drop", self._on_component_dropped)
        flow.add_connection_validator(
            lambda payload, _flow: self._validate_connection(
                payload["source"],
                payload["sourceHandle"],
                payload["target"],
                payload["targetHandle"],
            )
        )

        def _on_edge_added(event):
            if self._muted:
                return
            edge = event.get("edge", event) if isinstance(event, dict) else {}
            src_id = edge.get("source", "")
            tgt_id = edge.get("target", "")
            src_handle = edge.get("sourceHandle", "")
            tgt_handle = edge.get("targetHandle", "")
            if src_id and tgt_id and src_handle and tgt_handle:
                result = self._restricted_input_error(tgt_id, tgt_handle) or (
                    self._dataflow_graph.add_edge(src_id, src_handle, tgt_id, tgt_handle)
                )
            else:
                result = "Connection requires source and target ports."
            if result is True:
                edge_id = edge.get("id", "")
                if edge_id:
                    mapping = (src_id, src_handle, tgt_id, tgt_handle)
                    self._edge_id_map[edge_id] = mapping
                    if self._edge_failed(mapping):
                        flow.patch_edge_props(edge_id, self._edge_chip(mapping))
                self._record_edit(
                    self._connect_label("Connected", src_id, src_handle, tgt_id, tgt_handle),
                    op="connect",
                    args=self._edge_args(src_id, src_handle, tgt_id, tgt_handle),
                )
                self._notify("success", f"Wired: {src_handle} → {tgt_handle}", duration=3000)
            else:
                logger.warning("Edge rejected: %s", result)
                self._notify("error", result, duration=5000)
                with self._muted_canvas():
                    flow.remove_edge(edge.get("id", ""))

        def _on_edge_deleted(event):
            if self._muted:
                return
            edge_id = event.get("edge_id", "") if isinstance(event, dict) else ""
            if not edge_id:
                return
            mapping = self._edge_id_map.pop(edge_id, None)
            self._drop_edge_editor(edge_id)
            if mapping:
                self._dataflow_graph.remove_edge(*mapping)
                self._record_edit(
                    self._connect_label("Disconnected", *mapping),
                    op="disconnect",
                    args=self._edge_args(*mapping),
                )

        def _on_node_data_changed(event):
            if self._muted:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            patch = event.get("patch", {}) if isinstance(event, dict) else {}
            if not node_id or not patch:
                return
            self._apply_config_patch(node_id, patch)

        def _on_node_deleted(event):
            if self._muted:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            if node_id:
                self._forget_node(node_id)
                self._dispose_component(node_id)
                name = self._node_name(node_id)
                self._record_edit(f"Removed {name}", op="remove_node", args={"name": name})
                self._sync_placed()

        flow.param.watch(self._on_selection_changed, "selection")
        flow.on("edge_added", _on_edge_added)
        flow.on("edge_deleted", _on_edge_deleted)
        flow.on("node_data_changed", _on_node_data_changed)
        flow.on("node_deleted", _on_node_deleted)
        flow.on("handle_clicked", self._on_handle_clicked)
        flow.on("edge_clicked", self._on_edge_clicked)
        flow.on("handle_hovered", self._on_handle_hovered)
        flow.on("edge_hovered", self._on_edge_hovered)
        flow.on("handle_unhovered", self._on_inspection_unhovered)
        flow.on("edge_unhovered", self._on_inspection_unhovered)

        return flow

    def _restricted_input_error(self, target_id: str, target_port: str) -> str | None:
        """Return why *target_port* takes no edge because a restricted component feeds it, or None.

        The stored connection of a component this session may not use is kept
        as stored and never wired, so the graph does not see it; an input it
        feeds is still taken unless it accepts a list.
        """
        if self._dataflow_graph._is_list_input(target_id, target_port):
            return None
        private = {iid for iid, p in self._placeholders().items() if p.private}
        for unresolved in self._unresolved_edges:
            edge = unresolved.edge
            if (
                edge.source in private
                and edge.target == target_id
                and self._resolve_port_name(target_id, edge.target_port) == target_port
            ):
                return (
                    f"Input '{target_port}' is already fed by a component you do not have "
                    "access to, so it takes no other connection."
                )
        return None

    def _validate_connection(self, source_id, source_port, target_id, target_port, transform=None):
        """Return why a connection would be refused, or None, including restricted inputs."""
        return self._restricted_input_error(
            target_id, target_port
        ) or self._dataflow_graph.validate_connection(
            source_id, source_port, target_id, target_port, transform
        )

    @contextmanager
    def _muted_canvas(self):
        """Suppress the ReactFlow event handlers for the duration of the block.

        ``ReactFlow.add_edge`` and friends emit the same events the frontend
        does, so a programmatic mutation would otherwise re-enter the handler
        that is already performing it: the handler would see the edge as a
        second connection to an occupied input, reject it and remove it again.
        """
        previous, self._muted = self._muted, True
        try:
            yield
        finally:
            self._muted = previous

    def _add_canvas_node(self, node: dict):
        if self._canvas_batch is None:
            with self._muted_canvas():
                self._flow.add_node(node)
            return
        for key, default in _NODE_DEFAULTS.items():
            node.setdefault(key, dict(default) if isinstance(default, dict) else default)
        self._check_node_data(node["type"], node["data"])
        self._canvas_batch[0].append(node)

    def _check_node_data(self, node_type: str, data: dict):
        """Raise ValueError if *data* does not match the schema of *node_type* on the canvas."""
        if not self._flow.validate_on_add:
            return
        if _validate_node_data is None:
            _warn_validation_unavailable()
            return
        _validate_node_data(data, (self._flow.node_types.get(node_type) or {}).get("schema"))

    def _add_canvas_edge(self, edge: dict):
        if self._canvas_batch is None:
            with self._muted_canvas():
                self._flow.add_edge(edge)
            return
        edge.setdefault("data", {})
        self._canvas_batch[1].append(edge)

    @contextmanager
    def _batched_canvas(self):
        """Collect the nodes and edges added in the block and put them on the canvas at once.

        ``ReactFlow.add_node`` and ``add_edge`` rebuild the whole node list and
        its views on every call, which makes adding N items one by one O(N^2)
        once the canvas is rendered (18 s for a 500 node dashboard).
        """
        nodes: list[dict] = []
        edges: list[dict] = []
        self._canvas_batch = (nodes, edges)
        try:
            yield
        finally:
            self._canvas_batch = None
            flow = self._flow
            with self._muted_canvas():
                _add_all(flow.add_nodes, flow.add_node, nodes)
                _add_all(flow.add_edges, flow.add_edge, edges)

    def _forget_node(self, node_id: str):
        """Drop a node from the graph, the tile bookkeeping and the edge map.

        Its layout entries go too, so a node placed later under the same id
        starts with a fresh tile rather than the removed one's.
        """
        self._dataflow_graph.remove_node(node_id)
        self._forget_layout(node_id)
        self._inspect_tiles.pop(node_id, None)
        if self._inspected_id == node_id:
            self._close_inspection_dialog()
        idx = next(
            (i for i, item in enumerate(self._tile_items) if item["instance_id"] == node_id),
            None,
        )
        if idx is not None:
            self._tile_items.pop(idx)
            self._tile_objects.pop(idx)
        for edge_id, mapping in list(self._edge_id_map.items()):
            if node_id in (mapping[0], mapping[2]):
                del self._edge_id_map[edge_id]
                self._drop_edge_editor(edge_id)
        self._unresolved_edges = [
            u for u in self._unresolved_edges if node_id not in (u.edge.source, u.edge.target)
        ]
        self._sync_toolbar_state()

    def _forget_layout(self, node_id: str):
        self._grid_ids = [None if i == node_id else i for i in self._grid_ids]
        self._pending_tile_layout = [
            spec for spec in self._pending_tile_layout if spec.get("instance_id") != node_id
        ]
        config = self._pending_grid_config
        if config is not None:
            config["responsive_layouts"] = {
                band: [spec for spec in layout if spec.get("instance_id") != node_id]
                for band, layout in config["responsive_layouts"].items()
            }

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    def _describe_failures(
        self, instance_id: str, failed: dict[str, InputFailure]
    ) -> FailureMessage:
        """Name the components that stopped a node, and explain each failed input in its ports."""
        lines, sources = [], []
        for port_name, failure in failed.items():
            source_title = self._instance_title(failure.source_id)
            if source_title not in sources:
                sources.append(source_title)
            port = self._port_spec(instance_id, port_name, output=False)
            source = self._port_spec(failure.source_id, failure.source_port, output=True)
            source_id = self._component_id_for_instance(failure.source_id)
            source_spec = self._component_specs.get(source_id) if source_id else None
            lines.append(
                f"Input '{(port.label if port else None) or port_name}' is stale: "
                f"'{(source.label if source else None) or failure.source_port}' of "
                f"'{source_spec.title if source_spec else failure.source_id}' "
                f"failed ({failure.text})"
            )
        return FailureMessage(sources=sources, details="\n".join(lines))

    def _sync_failure_marks(self, instance_id: str, event):
        """Outline a node and its failed edges in red while its inputs hold a failure."""
        self._patch_canvas_props(
            "node", instance_id, {"style": _FAILED_NODE_STYLE if event.new else None}
        )
        for edge_id, mapping in self._edge_id_map.items():
            if mapping[2] == instance_id:
                self._patch_canvas_props("edge", edge_id, self._edge_chip(mapping))

    def _patch_canvas_props(self, kind: str, item_id: str, patch: dict[str, t.Any]):
        """Patch a node or edge, also while it is still waiting in a batched canvas update."""
        if self._canvas_batch is not None:
            pending = self._canvas_batch[0 if kind == "node" else 1]
            item = next((item for item in pending if item["id"] == item_id), None)
            if item is not None:
                for key, value in patch.items():
                    if value is None:
                        item.pop(key, None)
                    else:
                        item[key] = value
                return
        if kind == "node":
            self._flow.patch_node_props(item_id, patch)
        else:
            self._flow.patch_edge_props(item_id, patch)

    # ------------------------------------------------------------------
    # Value inspection
    # ------------------------------------------------------------------

    def _component_id_for_instance(self, instance_id: str) -> str | None:
        return next(
            (
                item["component_id"]
                for item in self._tile_items
                if item["instance_id"] == instance_id
            ),
            None,
        )

    def _port_spec(
        self, instance_id: str, port_name: str, *, output: bool
    ) -> InputPort | OutputPort | None:
        """Look up the OutputPort/InputPort declaration for a placed node's port."""
        component_id = self._component_id_for_instance(instance_id)
        spec = self._component_specs.get(component_id) if component_id else None
        if spec is None:
            return None
        ports = spec.outputs if output else spec.inputs
        return next((p for p in ports if p.name == port_name), None)

    def _value_preview(self, value: t.Any) -> Viewable:
        """Render a value for the popup, using custom or default formatting."""
        if self.value_repr is not None:
            try:
                rendered = self.value_repr(value)
                return pn.panel(rendered)
            except Exception:
                logger.exception("Value popup representation failed")
        if value is None:
            return markdown_pane("*No value yet*", margin=0)
        if is_failure(value):
            return pmui.Alert(
                text_typography(failure_text(value), variant="body2", margin=0),
                severity="error",
                variant="outlined",
                margin=0,
            )
        try:
            import pandas as pd

            if isinstance(value, pd.DataFrame):
                return pn.Column(
                    markdown_pane(
                        f"DataFrame · {value.shape[0]} rows x {value.shape[1]} cols",
                        margin=(0, 0, 6, 0),
                    ),
                    pn.pane.DataFrame(value.head(10), sizing_mode="stretch_width"),
                    sizing_mode="stretch_width",
                )
        except ImportError:
            pass
        if isinstance(value, (pn.viewable.Viewable, Viewer)):
            return value
        try:
            return pn.pane.JSON(value, depth=2, sizing_mode="stretch_width")
        except Exception:
            text = repr(value)
            if len(text) > 2000:
                text = text[:2000] + "…"
            return markdown_pane(code_block(text))

    def _build_value_popup(self, title: str, subtitle: str | None, value: t.Any) -> Viewable:
        header = f"**{escape_markdown(title)}**"
        if subtitle:
            header += f"  \n*{escape_markdown(subtitle)}*"
        return pn.Column(
            markdown_pane(header, margin=(0, 0, 6, 0)),
            self._value_preview(value),
            sizing_mode="stretch_width",
        )

    def _inspect_hook_content(self, instance_id: str, port_name: str) -> Viewable | None:
        """Ask the live component to explain an output, or None to keep the default alone."""
        hook = getattr(self._component_instances.get(instance_id), "__flowdash_inspect__", None)
        if hook is None:
            return None
        try:
            content = hook(port_name)
            return None if content is None else pn.panel(content)
        except Exception as exc:
            logger.exception("Inspecting output '%s' of %s failed", port_name, instance_id)
            return text_alert(
                f"Could not describe this output: {exc}",
                severity="error",
                margin=(6, 0, 0, 0),
            )

    def _instance_title(self, instance_id: str) -> str:
        """Return the title of a placed component: its configured title, else its component's."""
        placeholder = self._placeholders().get(instance_id)
        if placeholder is not None:
            return placeholder.title
        config = self._dataflow_graph.get_config_state(instance_id)
        title = getattr(config, "title", None) if config is not None else None
        if isinstance(title, str) and title:
            return title
        component_id = self._component_id_for_instance(instance_id)
        spec = self._component_specs.get(component_id) if component_id else None
        return spec.title if spec is not None else instance_id

    def _output_lineage(self, instance_id: str, port_name: str) -> tuple[list[str], list[str]]:
        """Return the components an output feeds, and where the node's inputs come from."""
        feeds, comes_from = [], []
        for mapping in self._edge_id_map.values():
            source_id, source_port, target_id, _ = mapping
            if source_id == instance_id and source_port == port_name:
                feeds.append(self._instance_title(target_id))
            if target_id == instance_id:
                steps = parse_transform(self._dataflow_graph.get_edge_transform(*mapping))
                summary = summarize_transform(steps)
                source = self._instance_title(source_id)
                comes_from.append(f"{source}, {summary}" if summary else source)
        return list(dict.fromkeys(feeds)), list(dict.fromkeys(comes_from))

    def _inspection_sections(
        self, instance_id: str, spec: ComponentSpec
    ) -> list[tuple[str, Viewable]]:
        """One titled section per output, reading each current value now rather than at build time."""
        state = self._dataflow_graph.get_state(instance_id)
        sections = []
        for port in spec.outputs:
            value = getattr(state, port.name, None)
            type_name, shape = describe_value(value)
            feeds, comes_from = self._output_lineage(instance_id, port.name)
            rows = [
                ("Type", type_name or port.type),
                ("Shape", shape),
                ("Feeds", ", ".join(feeds)),
                ("Comes from", ", ".join(comes_from)),
            ]
            if value is None:
                preview = None
            elif self.value_repr is None and is_dataframe(value):
                preview = dataframe_preview(value, port.name)
            else:
                preview = pn.Column(
                    heading("Value"),
                    self._value_preview(value),
                    margin=0,
                    sizing_mode="stretch_width",
                )
            section = output_section(
                status=value_status(value, is_failure(value)),
                rows=rows,
                preview=preview,
                extra=self._inspect_hook_content(instance_id, port.name),
            )
            sections.append((port.label or port.name, section))
        return sections

    def _on_inspect_clicked(self, instance_id: str, tile: Tile, _event=None):
        # A click can arrive after the tile left the grid, e.g. after navigating
        # to another dashboard that reuses the instance id; it belongs to no tile now.
        if self._inspect_tiles.get(instance_id) is not tile or not self._can_inspect():
            return
        spec = self._component_specs[self._component_id_for_instance(instance_id)]
        objects = [sheet_header(self._instance_title(instance_id), self._close_inspection_dialog)]
        # A gated tile shows its placeholder, so say why its outputs may be empty.
        missing = missing_inputs(self._dataflow_graph.get_state(instance_id), spec.inputs)
        if missing:
            notice = f"Waiting for {', '.join(missing)}; the outputs may not be computed yet."
            objects.append(text_alert(notice, severity="info", margin=(0, 0, 6, 0)))
        objects.append(
            pmui.Tabs(
                *self._inspection_sections(instance_id, spec),
                margin=0,
                sizing_mode="stretch_width",
                sx={"& .MuiTab-root": {"textTransform": "none"}},
            )
        )
        self._inspected_id = instance_id
        self._inspect_dialog.param.update(objects=objects, open=True)
        reserve_space(self._workspace_area, True)

    def _close_inspection_dialog(self):
        """Close the sheet and let go of the values it shows, whose tile may be gone."""
        self._inspected_id = None
        if self._inspect_dialog.open or self._inspect_dialog.objects:
            self._inspect_dialog.param.update(open=False, objects=[])
            reserve_space(self._workspace_area, False)

    def _can_inspect_tile(self, spec: ComponentSpec | None) -> bool:
        return bool(self._can_inspect() and spec and spec.outputs)

    def _can_show_in_editor(self) -> bool:
        return self.show_in_editor is not None and not self.read_only and not self.editable

    def _on_show_in_editor(self, instance_id: str):
        if self._can_show_in_editor():
            self.show_in_editor(instance_id)

    def _make_tile(self, item: dict, spec: ComponentSpec | None, view: Viewable, bounds: dict):
        """Frame *view* with a title bar whose status follows the node's inputs."""
        instance_id = item["instance_id"]
        name = self._instance_title(instance_id)
        config = self._dataflow_graph.get_config_state(instance_id)
        title = name
        if config is not None and "title" in config.param:
            default = spec.title if spec is not None else name
            title = config.param.title.rx.pipe(lambda value: value or default)
        description = (spec.description or "").strip() if spec is not None else ""
        tile = Tile(
            view,
            title=title,
            label=name,
            subtitle=" ".join(description.split("\n\n")[0].split()) or None,
            on_show_in_editor=partial(self._on_show_in_editor, instance_id),
            css_classes=["flowdash-tile", f"{TILE_KEY_PREFIX}{next(self._tile_keys)}"],
            **bounds,
        )
        tile.on_inspect = partial(self._on_inspect_clicked, instance_id, tile)
        if isinstance(view, FailureOverlay):
            button = pmui.Button(
                label="Show in editor",
                icon="account_tree",
                size="small",
                variant="text",
                margin=(0, 4),
            )
            button.on_click(lambda _event: self._on_show_in_editor(instance_id))
            view.actions[:] = [button]

        state = self._dataflow_graph.get_state(instance_id)
        failures = (
            self._dataflow_graph.get_failures(instance_id) if self.propagate_failures else None
        )
        inputs = [p.name for p in (spec.inputs if spec else []) if state and p.name in state.param]

        def update_status(*_):
            if failures is not None and failures.failed:
                tile.set_status("failed")
            elif spec is not None and state is not None and missing_inputs(state, spec.inputs):
                tile.set_status("waiting")
            else:
                tile.set_status(None)

        update_status()
        wiring = self._component_wiring.setdefault(instance_id, [])
        if inputs:
            watcher = state.param.watch(update_status, inputs)
            wiring.append(partial(state.param.unwatch, watcher))
        if failures is not None:
            watcher = failures.param.watch(update_status, "failed")
            wiring.append(partial(failures.param.unwatch, watcher))
        return tile

    def _sync_tile(self, tile: Tile, spec: ComponentSpec | None):
        """Offer a tile's actions to whoever is viewing it now."""
        show_in_editor = self._can_show_in_editor()
        tile.set_actions(inspect=self._can_inspect_tile(spec), show_in_editor=show_in_editor)
        if isinstance(tile.view, FailureOverlay):
            for button in tile.view.actions:
                button.visible = show_in_editor

    @param.depends("editable", "show_in_editor", watch=True)
    def _sync_tile_actions(self):
        for item in self._tile_items:
            tile = self._inspect_tiles.get(item["instance_id"])
            if tile is not None:
                self._sync_tile(tile, self._tile_spec(item))

    def _tile_spec(self, item: dict) -> ComponentSpec | None:
        """Return the spec whose hints apply to a tile; a placeholder keeps its saved size."""
        if "placeholder" in item:
            return None
        return self._component_specs.get(item["component_id"])

    def _grid_view(self, item: dict, view: Viewable) -> Viewable:
        """Return the tile's view in a frame with a title bar and its actions.

        A tile with width bounds carries them on the frame, since TileGrid reads
        a tile's ``min_width`` and ``max_width`` to bound resizing and
        responsive widths. A placeholder has neither, even if its component is
        offered.
        """
        instance_id = item["instance_id"]
        spec = self._tile_spec(item)
        bounds = {
            f"{key}_width": size["width"]
            for key, size in (("min", spec and spec.min_size), ("max", spec and spec.max_size))
            if size and "width" in size
        }
        tile = self._inspect_tiles.get(instance_id)
        if tile is None or tile.view is not view:
            tile = self._inspect_tiles[instance_id] = self._make_tile(item, spec, view, bounds)
        tile.set_chrome(not self._tile_grid.editable)
        tile.set_style(framed=self.settings.tile_cards, titled=self.settings.tile_titles)
        self._sync_tile(tile, spec)
        return tile

    @param.depends(
        "controls_position",
        "header_layout",
        "node_previews",
        "tile_cards",
        "tile_titles",
        watch=True,
        on_init=True,
    )
    def _seed_tile_settings(self):
        """Apply the editor's layout defaults to the dashboard settings shown now."""
        loading = getattr(self, "_loading_settings", False)
        self._loading_settings = True
        try:
            self.settings.param.update(**self._layout_defaults())
        finally:
            self._loading_settings = loading

    def _layout_defaults(self) -> dict[str, t.Any]:
        return {
            "controls_position": self.controls_position,
            "header_layout": self.header_layout,
            "node_previews": self.node_previews,
            "tile_cards": self.tile_cards,
            "tile_titles": self.tile_titles,
        }

    @param.depends("settings.header_layout", watch=True)
    def _sync_header_layout(self):
        bar = getattr(self, "_dashboard_bar", None)
        if bar is not None:
            # A host header and the bar's own row trade the same parts, which
            # one message must move so neither holds them while the other does.
            with pn.io.hold():
                bar.layout = self.settings.header_layout

    @param.depends("settings.controls_position", watch=True)
    def _sync_controls_position(self):
        controls = getattr(self, "_view_controls", None)
        if controls is None or controls.position == self.settings.controls_position:
            return
        controls.position = self.settings.controls_position
        if self._grid_on_screen:
            self._apply_mode_state()

    @param.depends("settings.node_previews", watch=True)
    def _sync_node_previews(self):
        self._show_node_previews(self.settings.node_previews)

    def _show_node_previews(self, shown: bool):
        """Show or hide the previews on the canvas for this session, without changing the setting."""
        self._node_previews_shown = shown
        flow = getattr(self, "_flow", None)
        if flow is None:
            return
        with pn.io.hold():
            for node in flow.nodes:
                if isinstance(card := node.get("view"), NodeCard):
                    card.show_preview = shown

    @param.depends("settings.tile_cards", "settings.tile_titles", watch=True)
    def _sync_tile_style(self):
        # Also fires while the editor is being constructed, before there are tiles.
        for tile in getattr(self, "_inspect_tiles", {}).values():
            tile.set_style(framed=self.settings.tile_cards, titled=self.settings.tile_titles)

    def _can_inspect(self) -> bool:
        """Whether this session may inspect outputs: editors always, viewers if the dashboard allows."""
        return self.inspect_outputs and (not self.read_only or self.settings.viewers_can_inspect)

    @param.depends("read_only", "settings.viewers_can_inspect", watch=True)
    def _sync_inspect_buttons(self):
        self._sync_tile_actions()
        if not self._can_inspect():
            self._close_inspection_dialog()

    def _show_inspection(self, key, title, value, position, flow, leftward=False):
        self._inspection_key = key
        popup = self._build_value_popup(title, None, value)
        if leftward:
            popup.css_classes = [*popup.css_classes, "fd-popup-leftward"]
        flow.show_popup(popup, position)

    def _on_handle_clicked(self, payload, flow):
        node_id = payload.get("node_id", "") if isinstance(payload, dict) else ""
        handle_id = payload.get("handle_id") if isinstance(payload, dict) else None
        direction = payload.get("direction", "") if isinstance(payload, dict) else ""
        position = payload.get("position") if isinstance(payload, dict) else None
        if not node_id or not handle_id or position is None:
            return
        port = self._port_spec(node_id, handle_id, output=direction == "output")
        label = (port.label if port else None) or handle_id
        subtitle = f"{direction} · {port.type}" if port and port.type else direction
        state = self._dataflow_graph.get_state(node_id)
        value = getattr(state, handle_id, None) if state is not None else None
        failures = self._dataflow_graph.get_failures(node_id)
        if direction == "input" and failures is not None and handle_id in failures.failed:
            value = failures.failed[handle_id].error
        self._show_inspection(
            ("handle", node_id + ":" + str(handle_id)),
            f"{label} ({subtitle})",
            value,
            position,
            flow,
            # An output's value opens over its own node, so a node at the right
            # edge of the canvas does not push the popup over the inspector.
            leftward=direction == "output",
        )

    def _on_edge_clicked(self, payload, flow):
        # A click selects the edge, and the open inspector then describes it.
        if self._inspector.visible and not self._inspector.collapsed:
            return
        self._show_edge_value(payload, flow)

    def _show_edge_value(self, payload, flow):
        edge_id = payload.get("edge_id", "") if isinstance(payload, dict) else ""
        position = payload.get("position") if isinstance(payload, dict) else None
        mapping = self._edge_id_map.get(edge_id)
        if not mapping or position is None:
            return
        source_id, source_port, _target_id, _target_port = mapping
        port = self._port_spec(source_id, source_port, output=True)
        label = (port.label if port else None) or source_port
        if self._dataflow_graph.get_edge_transform(*mapping):
            subtitle = "edge · transformed"
        else:
            subtitle = f"edge · {port.type}" if port and port.type else "edge"
        expr = self._dataflow_graph.get_edge_expression(*mapping)
        value = expr.rx.value if expr is not None else None
        self._show_inspection(("edge", edge_id), f"{label} ({subtitle})", value, position, flow)

    def _on_handle_hovered(self, payload, flow):
        self._on_handle_clicked(payload, flow)

    def _on_edge_hovered(self, payload, flow):
        self._show_edge_value(payload, flow)

    def _on_inspection_unhovered(self, payload, flow):
        if self.popup_trigger != "hover":
            return
        if not isinstance(payload, dict):
            return
        if "node_id" in payload:
            key = ("handle", payload.get("node_id", "") + ":" + str(payload.get("handle_id")))
        else:
            key = ("edge", payload.get("edge_id", ""))
        if key != self._inspection_key:
            return
        flow.close_popup()
        self._inspection_key = None

    # ------------------------------------------------------------------
    # Config state
    # ------------------------------------------------------------------

    def _apply_config_patch(self, node_id, patch):
        """Apply an editor patch to a node's config state and persist it."""
        config_state = self._dataflow_graph.get_config_state(node_id)
        applied = {}
        for key, value in patch.items():
            if config_state is not None and hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, value)
                except Exception as exc:
                    logger.warning("Config '%s' rejected on %s: %s", key, node_id, exc)
                    continue
            applied[key] = value
        if not applied:
            return
        for item in self._tile_items:
            if item["instance_id"] == node_id:
                item.setdefault("config", {}).update(applied)
                break
        name = self._node_name(node_id)
        self._record_edit(
            f"Changed the config of {name}",
            op="set_config",
            args={"name": name, "keys": sorted(applied)},
        )

    def _seed_config_state(self, instance_id, config):
        """Overlay saved config onto a node's config state and return node data seed."""
        config_state = self._dataflow_graph.get_config_state(instance_id)
        if config_state is None:
            return {}
        for key, value in (config or {}).items():
            if hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, value)
                except Exception as exc:
                    logger.warning("Saved config '%s' rejected on %s: %s", key, instance_id, exc)
        return {name: getattr(config_state, name) for name in config_state.param if name != "name"}

    def _bind_config_to_viewer(self, node_id, instance, config_state, wiring):
        """Sync config-state params onto a Viewer instance, live.

        A config param the component declares ``two_way`` in its ``"flowdash"``
        metadata is synced back too, so a value the component edits itself, such
        as text typed into its tile, is saved with the dashboard.
        """
        for name in config_state.param:
            if name == "name" or not hasattr(instance.param, name):
                continue
            try:
                setattr(instance, name, getattr(config_state, name))
            except Exception as exc:
                logger.warning("Config '%s' could not be set: %s", name, exc)
                continue

            def _propagate(event, _name=name):
                try:
                    setattr(instance, _name, event.new)
                except Exception as exc:
                    logger.warning("Config '%s' update failed: %s", _name, exc)

            watcher = config_state.param.watch(_propagate, name)
            wiring.append(partial(config_state.param.unwatch, watcher))
            if _flowdash_metadata(instance.param[name]).get("two_way"):
                watcher = instance.param.watch(
                    partial(self._config_edited_by_component, weakref.ref(self), node_id),
                    name,
                )
                wiring.append(partial(instance.param.unwatch, watcher))

    @staticmethod
    def _config_edited_by_component(editor_ref, node_id, event):
        editor = editor_ref()
        if editor is None:
            return
        if editor.editable and not editor.read_only and not editor.preview:
            editor._apply_config_patch(node_id, {event.name: event.new})
            return
        # Viewers and a preview cannot change the dashboard, so the component goes back to the saved value.
        config_state = editor._dataflow_graph.get_config_state(node_id)
        if config_state is not None:
            setattr(event.obj, event.name, getattr(config_state, event.name))

    # ------------------------------------------------------------------
    # Component instantiation
    # ------------------------------------------------------------------

    def _instantiate_for_node(self, entry, node_state, config_state=None):
        """Create a live component view wired to the node_state."""
        app_fn = entry.load()

        if not callable(app_fn):
            return pn.panel(app_fn)

        if inspect.isclass(app_fn) and issubclass(app_fn, pn.viewable.Viewer):
            return self._instantiate_viewer_for_node(app_fn, entry, node_state, config_state)

        sig = inspect.signature(app_fn)
        kwargs = {}
        if "config" in sig.parameters:
            kwargs["config"] = node_state
        if "instance_config" in sig.parameters and config_state is not None:
            kwargs["instance_config"] = config_state
        if "context" in sig.parameters:
            kwargs["context"] = "component"

        # Unlike pages, sync components are not offloaded to a thread: placement
        # is synchronous, the outputs they publish must exist before saved edges
        # are reconnected, and the dataflow graph is not thread-safe. Slow
        # components should be async (see docs/how-to/register-components.md).
        # An async body, or an awaitable a sync component returns, runs at
        # render time, after `_place` has finished tracking.
        node_id = node_state.name
        wiring = self._component_wiring.setdefault(node_id, [])
        app_fn = stepped(app_fn, partial(_creating, weakref.ref(self), node_id, wiring))
        return panel_call(app_fn, **kwargs)

    def _instantiate_viewer_for_node(self, viewer_cls, entry, node_state, config_state=None):
        """Instantiate a Viewer and wire its params to the node_state."""
        spec = self._component_specs.get(entry.app_id)
        instance = viewer_cls()
        node_id = node_state.name
        self._dispose_component(node_id)
        self._component_instances[node_id] = instance
        wiring = self._component_wiring[node_id] = []

        if config_state is not None:
            self._bind_config_to_viewer(node_id, instance, config_state, wiring)

        # Weak, so a component kept by the app does not keep a discarded editor alive.
        editor = weakref.ref(self)
        input_names = [p.name for p in spec.inputs] if spec else []
        for name in input_names:
            if not hasattr(instance.param, name):
                continue

            def _propagate_input(event, _name=name):
                # A component rejecting a value, or a watcher of its own raising,
                # must not abort the dispatch to the node's other consumers.
                try:
                    setattr(instance, _name, event.new)
                except Exception as exc:
                    if (owner := editor()) is not None:
                        owner._report_input_error(node_id, _name, exc)

            watcher = node_state.param.watch(_propagate_input, name)
            wiring.append(partial(node_state.param.unwatch, watcher))

        failures = self._output_failures[node_id] = {}
        wiring.append(partial(self._output_failures.pop, node_id, None))
        methods = instance.param.outputs()
        for name, (_, method, _) in methods.items():
            if not hasattr(node_state.param, name):
                continue
            ref = self._output_reference(
                instance, name, method, self.propagate_failures, editor, failures
            )
            if is_async(method if callable(method) else getattr(instance, method)):
                outlet = _Outlet(name=f"{node_id}_{name}")
                outlet.value = ref
                ref = outlet.param.value
                # Overwriting the reference cancels a pending or still yielding output.
                wiring.append(partial(_release_refs, outlet))
            setattr(node_state, name, ref)

        # An output named after a Parameter is that Parameter, with no method to wrap.
        for port in spec.outputs if spec else ():
            if (
                port.name not in methods
                and port.name in instance.param
                and hasattr(node_state.param, port.name)
            ):
                setattr(node_state, port.name, instance.param[port.name])

        return panel_viewer(instance, runs=_ViewRuns(editor, node_id, wiring))

    def _report_input_error(self, node_id: str, port: str, exc: Exception):
        source = next(
            (
                (edge["source"], edge["source_port"])
                for edge in self._dataflow_graph.edges
                if edge["target"] == node_id and edge["target_port"] == port
            ),
            ("", ""),
        )
        self._on_wiring_error(*source, node_id, port, exc)

    @staticmethod
    def _output_reference(
        instance, name, method, propagate_failures=False, editor_ref=None, failures=None
    ):
        """Bind an output method to its dependencies, as a reference for a node port.

        Param resolves the reference whenever a dependency changes, awaiting
        async outputs on the event loop and publishing every value an async
        generator yields. A failing output is skipped, so the port keeps its
        last value, unless *propagate_failures* publishes the failure on the
        port instead. The first evaluation runs before any input is wired, so a
        sync output failing there is skipped either way.

        While the output runs, `current_dashboard` returns the editor of
        *editor_ref*. *failures* records the exception of each output whose
        latest run failed, including that first one, for the agent's reports.
        """
        fn = method if callable(method) else getattr(instance, method)
        deps = [
            (dep.inst or instance).param[dep.name]
            for dep in instance.param.method_dependencies(fn.__name__)
            if dep.name != "name"
        ]
        # A bind without arguments is not a reference; `name` never changes, so
        # depending on it evaluates the output once and keeps it bound.
        deps = deps or [instance.param.name]
        failures = {} if failures is None else failures

        def current():
            return _current(editor_ref) if editor_ref is not None else nullcontext()

        def _failed(exc):
            failures[name] = exc
            logger.error("Output '%s' failed: %s", name, exc, exc_info=exc)
            if propagate_failures:
                return param.ReactiveError(exc)
            raise param.Skip from exc

        if is_async_gen(fn):

            async def _output(*_):
                values = fn()
                while True:
                    try:
                        with current():
                            value = await values.__anext__()
                    except StopAsyncIteration:
                        return
                    except param.Skip:
                        raise
                    except Exception as exc:
                        yield _failed(exc)
                        return
                    failures.pop(name, None)
                    yield value

        elif is_async(fn):

            async def _output(*_):
                try:
                    with current():
                        value = await fn()
                except param.Skip:
                    raise
                except Exception as exc:
                    return _failed(exc)
                failures.pop(name, None)
                return value

        else:
            initial = [True]

            def _output(*_):
                quiet = bool(initial) and initial.pop()
                try:
                    with current():
                        value = fn()
                except param.Skip:
                    raise
                except Exception as exc:
                    if quiet:
                        failures[name] = exc
                        raise param.Skip from exc
                    return _failed(exc)
                failures.pop(name, None)
                return value

        return param.bind(_output, *deps)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_component_view(self):
        self._dashboard_bar = bar = DashboardBar(editor=self, layout=self.settings.header_layout)
        self._view_controls = ControlsBar(editor=self, position=self.settings.controls_position)
        self._save_button = bar.save_button
        self._download_button = bar.download_button
        self._refresh_button = bar.refresh_button
        self._settings_button = bar.settings_button
        self._save_button.on_click(lambda _event: self._on_save_clicked())
        self._clear_dialog = self._build_clear_dialog()
        self._pending_import: DashboardModel | None = None
        self._import_message = markdown_pane()
        self._import_dialog = self._build_import_dialog()
        self._inspect_dialog = make_inspect_sheet()
        # Saving is only offered with unsaved changes, and rearranging is a change.
        self._flow.on(
            "node_moved",
            lambda _payload: self._mark_rearranged("Moved nodes on the canvas", "move_nodes"),
        )
        self._tile_grid.param.watch(self._on_tile_layout_edited, ["layout", "responsive_layouts"])
        self._settings_dialog = self._build_settings_dialog()
        self._settings_button.on_click(lambda _event: self._settings_view.show("general"))
        self._palette_section = self._palette_panel
        # Specs registered after a lazy import replace the node types, and give
        # the palette real ports in place of the ones declared on `@register`.
        self._flow.param.watch(lambda _event: self._palette_panel.refresh(), "node_types")
        self._side_panel = pmui.Column(
            self._palette_section,
            width=_SIDE_PANEL_WIDTH,
            sizing_mode="stretch_height",
            margin=0,
            sx={"borderRight": 1, "borderColor": "divider"},
        )
        self._start_card = StartCard(editor=self)
        self._empty_hint = self._start_card.__panel__()
        self._workspace_area = pn.Column(sizing_mode="stretch_both", scroll="y-auto")
        self._inspector = Inspector(editor=self)
        self._layout_panel = LayoutPanel(editor=self)
        self._status_bar = StatusBar(editor=self)
        # The Full screen button may sit in an app's top bar, outside the editor.
        toggle_class = f"{TOGGLE_CLASS}-{id(self)}"
        bar.fullscreen_button.css_classes = [*bar.fullscreen_button.css_classes, toggle_class]
        self._fullscreen_controller = FullscreenController(
            enabled=not self.editable, margin=0, height=0, toggle_class=toggle_class
        )
        self._fullscreen_controller.param.watch(self._on_fullscreen_changed, "active")
        self._fullscreen_overlay = PresentationOverlay(bar.health_chip)
        bar.title.param.watch(self._name_fullscreen_overlay, "object")
        self._name_fullscreen_overlay()
        self._split_view = SplitView(
            on_collapse=self._on_split_collapsed,
            on_load=self._on_split_preferences_loaded,
            on_resize=self._fit_canvas,
        )
        self._flow.param.watch(self._sync_tile_selection, "selection")
        self._inspector.param.watch(self._on_inspector_toggled, "collapsed")
        self._split_view.events.param.watch(self._on_pane_resized, "pane_size")

        # One slot: a row counts the width of hidden children, and only one of
        # the two panels is shown at a time. The slot is hidden with both,
        # since an empty Column still takes the width of its scroll button.
        self._panel_slot = pn.Column(
            self._inspector, self._layout_panel, margin=0, sizing_mode="stretch_height"
        )

        self._sync_toolbar_extra()
        self._sync_toolbar_state()
        self._stamp_refresh()
        return pn.Column(
            bar,
            pn.Row(
                self._side_panel,
                self._workspace_area,
                self._panel_slot,
                sizing_mode="stretch_both",
            ),
            self._status_bar,
            self._fullscreen_overlay,
            self._fullscreen_controller,
            self._split_view.storage,
            sizing_mode="stretch_both",
            stylesheets=[PRESENTATION_CSS],
        )

    def _on_fullscreen_changed(self, event):
        self.fullscreen = event.new

    def _name_fullscreen_overlay(self, *_):
        title = self.dashboard.title if self.dashboard is not None else "Untitled dashboard"
        self._fullscreen_controller.overlay_label = f"{title}, full screen controls"

    @pn.io.hold()
    @param.depends("fullscreen", "editable", watch=True)
    def _apply_fullscreen(self):
        """Show the presentation layout, or the normal one, in view mode only."""
        if self.fullscreen and self.editable:
            self.fullscreen = False
            return
        presenting = self.fullscreen
        self._fullscreen_controller.param.update(active=presenting, enabled=not self.editable)
        self._fullscreen_overlay.show(presenting)
        self._dashboard_bar.visible = self.toolbar and not presenting
        view = self._view
        classes = [c for c in view.css_classes if c != PRESENTING_CLASS]
        view.css_classes = [*classes, PRESENTING_CLASS] if presenting else classes

    def _mark_rearranged(self, label: str = "Changed the tile layout", op: str = "layout"):
        if self.editable:
            self._record_edit(label, op=op)

    def _on_tile_layout_edited(self, event):
        # Only a layout the browser sent was edited by the user; the editor sets
        # it itself whenever the grid is rebuilt or sized.
        from_browser = self._tile_grid._in_process__events.get(pn.state.curdoc, {})
        if event.name in from_browser:
            self._mark_rearranged()

    def _build_clear_dialog(self):
        cancel = pmui.Button(label="Cancel", variant="text")
        confirm = pmui.Button(label="Clear canvas", color="danger")

        def _on_confirm(_event):
            self._clear_dialog.open = False
            self.clear()

        cancel.on_click(lambda _event: self._clear_dialog.param.update(open=False))
        confirm.on_click(_on_confirm)
        return pmui.Dialog(
            objects=[
                markdown_pane(
                    "Remove every component and connection from the canvas? "
                    "Unsaved work will be lost."
                ),
                pn.Row(pn.layout.HSpacer(), cancel, confirm, sizing_mode="stretch_width"),
            ],
            title="Clear canvas",
            open=False,
            min_width=400,
        )

    def _build_import_dialog(self):
        cancel = pmui.Button(label="Cancel", variant="text")
        confirm = pmui.Button(label="Replace canvas", color="danger")

        def _on_confirm(_event):
            self._import_dialog.open = False
            model, self._pending_import = self._pending_import, None
            if model is not None:
                self._apply_import(model)

        def _on_cancel(_event):
            self._import_dialog.open = False
            self._pending_import = None

        cancel.on_click(_on_cancel)
        confirm.on_click(_on_confirm)
        return pmui.Dialog(
            objects=[
                self._import_message,
                pn.Row(pn.layout.HSpacer(), cancel, confirm, sizing_mode="stretch_width"),
            ],
            title="Load dashboard file",
            open=False,
            min_width=400,
        )

    def _build_settings_dialog(self):
        self._settings_view = SettingsDialog(self)
        return self._settings_view.dialog

    @param.depends("dirty", "read_only", watch=True)
    def _sync_toolbar_state(self):
        """Reflect canvas contents and save state in the dashboard bar and palette."""
        self._dashboard_bar.sync()
        hint = [] if self._tile_items else [self._empty_hint]
        if self._unresolved_edges:
            hint.append(self._broken_edges_listing())
        if self._flow.bottom_panel != hint:
            self._flow.bottom_panel = hint

        items = self._palette_items()
        if items != self._palette.items:
            # MenuList remaps `expanded` onto new items, which would override an
            # `expanded` set in the same update.
            self._palette.items = items
            self._palette.expanded = [(i,) for i in range(len(items))]

    @property
    def _tile_grid(self):
        if not hasattr(self, "_tile__grid"):
            self._tile__grid = TileGrid(
                breakpoints=list(self.breakpoints),
                card=False,
                close_action="hide",
                editable=False,
                local_save=False,
                min_height=320,
                show_breakpoint_toolbar=False,
                sizing_mode="stretch_both",
                stylesheets=[GRID_EDIT_CSS],
            )
            self._tile__grid.param.watch(self._on_tile_layout, "layout")
            self._tile__grid.param.watch(
                self._sync_preview_band, ["reference_width", "breakpoints", "preview_band"]
            )
            self._tile__grid.param.watch(self._on_tile_selected, "selected")
        return self._tile__grid

    def _preview_band(self) -> str | None:
        """Return the band the grid is laid out and edited for, or None to follow its width.

        Beside the canvas the pane is narrower than the dashboard's breakpoints,
        so a grid following its width would show, and let the user edit, a
        narrow-screen layout. The Split tab therefore always arranges the base
        band, i.e. ``layout`` itself, and the Layout tab the band picked in
        its layout panel.
        """
        if not self.editable or self.preview or self.mode == "wiring":
            return None
        grid = self._tile_grid
        base = base_band(grid.breakpoints, grid.reference_width)
        if self.mode == "dashboard":
            band = self._layout_panel.band
            if band is not None and band in grid.param.preview_band.objects:
                return band
        return base

    def _sync_preview_band(self, *_events):
        layout_panel = getattr(self, "_layout_panel", None)
        if layout_panel is None:
            return
        grid = self._tile_grid
        band = self._preview_band()
        # The frame TileGrid draws around a previewed band marks a screen width;
        # the widest band and the Split tab's base band are not one.
        labels = [label for label in grid.param.preview_band.objects if label is not None]
        framed = band is not None and self.mode == "dashboard" and band != labels[-1]
        classes = [c for c in grid.css_classes if c != _UNFRAMED_BAND]
        if band is not None and not framed:
            classes.append(_UNFRAMED_BAND)
        grid.param.update(preview_band=band, css_classes=classes)
        layout_panel.sync_bands()

    # ------------------------------------------------------------------
    # The selected tile and the selected node
    # ------------------------------------------------------------------

    @property
    def _tiles_selectable(self) -> bool:
        return self.editable and not self.preview

    def _selected_tile_id(self) -> str | None:
        index = self._tile_grid.selected
        if index is None or index >= len(self._grid_ids):
            return None
        return self._grid_ids[index]

    def _sync_tile_selection(self, *_events):
        """Select the tile of the component selected on the canvas, or no tile.

        Of several selected components the tile already selected stays so,
        else the first one with a tile is selected.
        """
        selection = self._flow.selection or {}
        nodes = selection.get("nodes") or []
        if self.editable and self.mode == "split":
            if self._inspector_folded and (nodes or selection.get("edges")):
                self._fold_inspector(False)
        target = None
        if self._tiles_selectable:
            current = self._selected_tile_id()
            target = (
                current
                if current in nodes
                else next((n for n in nodes if n in self._grid_ids), None)
            )
        self._set_selected_tile(target, follow=False)

    def _set_selected_tile(self, instance_id: str | None, follow: bool = True):
        """Select the tile of *instance_id*; with *follow* its component is selected too."""
        index = self._grid_ids.index(instance_id) if instance_id in self._grid_ids else None
        grid = self._tile_grid
        if grid.selected == index:
            return
        following, self._tile_selection_follows = self._tile_selection_follows, follow
        try:
            grid.selected = index
        finally:
            self._tile_selection_follows = following

    def _on_tile_selected(self, _event):
        """Select the component of a tile clicked, focused, dragged or resized on the grid.

        A tile that is closed, or whose component is removed, is no longer
        selected on the grid, but its component stays selected on the canvas.
        """
        if self._rebuilding_grid or not self._tiles_selectable:
            return
        instance_id = self._selected_tile_id()
        if instance_id is None:
            return
        layout_panel = getattr(self, "_layout_panel", None)
        if layout_panel is not None:
            layout_panel.select(instance_id)
        if self._tile_selection_follows:
            self._select_node(instance_id)

    def _select_node(self, instance_id: str):
        """Select the node of *instance_id* on the canvas, bringing it into view in the Split tab."""
        if not any(node["id"] == instance_id for node in self._flow.nodes):
            return
        selection = self._flow.selection or {}
        if selection.get("nodes") == [instance_id] and not selection.get("edges"):
            return
        if self.mode == "split":
            if self._inspector_folded:
                # The inspector opens for the selection and narrows the pane, so the
                # node is revealed once the pane reports its new size.
                self._pending_reveal = instance_id
                self._fold_inspector(False)
            else:
                self._reveal_node(instance_id)
        select_on_canvas(self._flow, node_id=instance_id)

    def _sized_layout(self, layout: list[dict]) -> list[dict]:
        """Apply the size hints of the tiles on the grid to its positional *layout*."""
        items = {item["instance_id"]: item for item in self._tile_items}
        specs = [self._tile_spec(items[i]) if i in items else None for i in self._grid_ids]
        grid = self._tile_grid
        return _size_layout(layout, specs, reference_width(grid.reference_width, grid.breakpoints))

    def _on_tile_layout(self, event):
        # Resizing in the browser bounds only a tile's width, so heights are kept in bounds here.
        sized = self._sized_layout(event.new)
        if sized != event.new:
            self._tile_grid.layout = sized

    def _grid_config(self) -> dict:
        """Return the responsive settings to persist, whether or not the grid is on screen."""
        if self._grid_populated:
            grid = self._tile_grid
            return {
                "breakpoints": list(grid.breakpoints),
                "responsive_layouts": {
                    band: self._placed_layout(_key_layout(layout, self._grid_ids))
                    for band, layout in grid.responsive_layouts.items()
                },
                "reference_width": grid.reference_width,
            }
        config = dict(self._pending_grid_config or _EMPTY_GRID_CONFIG)
        config["responsive_layouts"] = {
            band: self._placed_layout(layout)
            for band, layout in config["responsive_layouts"].items()
        }
        return config

    def _apply_grid_config(self, config: dict):
        self._tile_grid.param.update(
            breakpoints=list(config["breakpoints"] or self.breakpoints),
            responsive_layouts={
                band: _align_layout(layout, self._grid_ids)
                for band, layout in config["responsive_layouts"].items()
            },
            reference_width=config["reference_width"],
        )

    @param.depends("toolbar_extra", watch=True)
    def _sync_toolbar_extra(self):
        """Re-seat caller-supplied toolbar items in the dashboard bar."""
        self._dashboard_bar.sync()

    @pn.io.hold()
    @param.depends(
        "editable",
        "include_page_dialog",
        "inspect_outputs",
        "mode",
        "preview",
        "toolbar",
        watch=True,
    )
    def _apply_mode_state(self):
        """Reconcile the dashboard bar, side panel and workspace with the display params."""
        interactive = self.editable and not self.preview
        split = self.editable and self.mode == "split"
        showing_grid = split or self.mode == "dashboard" or not self.editable
        showing_canvas = split or not showing_grid
        self._dashboard_bar.visible = self.toolbar and not self.fullscreen
        self._dashboard_bar.sync()
        self._side_panel.visible = showing_canvas
        self._inspector.visible = showing_canvas
        self._layout_panel.visible = interactive and showing_grid and not split
        self._panel_slot.visible = self._inspector.visible or self._layout_panel.visible
        self._status_bar.visible = self.editable
        self._split_view.events.active = split
        self._view_controls.beside_canvas = split
        if split != self._split_shown:
            self._split_shown = split
            self._fold_inspector(split)
        self._tile_grid.param.update(editable=interactive, card=interactive)
        self._sync_preview_band()
        # Column children rather than Row children, so the dialogs take no flex space.
        dialogs = [self._clear_dialog, self._import_dialog, self._settings_dialog]
        if self.inspect_outputs:
            dialogs.insert(0, self._inspect_dialog)
        shared_dialog = page_dialog() if self.include_page_dialog else None
        if shared_dialog is not None:
            dialogs.append(shared_dialog)
        if split:
            self._split_view.show([self._flow], self._view_controls.layout(self._tile_grid))
            self._workspace_area[:] = [self._split_view, *dialogs]
            self._rebuild_tile_grid()
        elif showing_grid:
            self._workspace_area[:] = [*self._view_controls.layout(self._tile_grid), *dialogs]
            self._rebuild_tile_grid()
        else:
            self._stash_tile_layout()
            self._workspace_area[:] = [self._flow, *dialogs]
            self._rebuild_sidebar()
        self._sync_tile_selection()

    @param.depends("mode", watch=True)
    def _remember_mode(self):
        # A tab chosen before the browser's preferences arrived is not overridden by them.
        self._restore_split = False
        if self.mode != "split":
            self._mode_before_split = self.mode
        self._split_view.preferences.split = self.mode == "split"

    def toggle_split(self):
        """Show the canvas and the tile grid side by side, or return to the previous tab."""
        self.mode = self._mode_before_split if self.mode == "split" else "split"

    @property
    def _grid_on_screen(self) -> bool:
        return self.mode in ("dashboard", "split") or not self.editable

    def _on_split_collapsed(self, pane: int):
        """Fall back to the tab of the pane left over, keeping the split's sizes for next time."""
        if self.mode == "split":
            self.mode = "dashboard" if pane == 0 else "wiring"
        self._split_view.restore()

    def _on_split_preferences_loaded(self):
        self._restore_split = self._restore_split and self._split_view.preferences.split
        self._apply_stored_split()

    @param.depends("editable", watch=True)
    def _apply_stored_split(self):
        """Reopen the Split tab remembered by the browser, once, when the editor is editable."""
        if not (self.editable and self._restore_split and self._split_view.loaded):
            return
        self._restore_split = False
        if self.mode != "split":
            self.mode = "split"

    def _fit_canvas(self):
        self._flow.fit_view()

    def _reveal_node(self, instance_id: str):
        """Pan the canvas, at its current zoom, so the node of *instance_id* is in view.

        The server knows the viewport and the node positions but not the
        rendered node sizes, so the node's center is estimated from its card.
        """
        pane = self._split_view.events.pane_size
        node = next((n for n in self._flow.nodes if n["id"] == instance_id), None)
        if node is None or not pane:
            return
        viewport = self._flow.viewport or {}
        zoom = viewport.get("zoom") or 1
        card = node.get("view")
        height = card.height if isinstance(card, NodeCard) else 0
        x = (node["position"]["x"] + CARD_WIDTH / 2) * zoom + viewport.get("x", 0)
        y = (node["position"]["y"] + height / 2) * zoom + viewport.get("y", 0)
        if 0 <= x <= pane[0] and 0 <= y <= pane[1]:
            return
        self._flow.fit_view(instance_id, min_zoom=zoom, max_zoom=zoom, duration=300)

    def _fold_inspector(self, split: bool):
        """Fold the inspector while the split is shown in a narrow window, until it is needed.

        Beside the palette and the open inspector, the canvas and the grid would
        get about 300 px each at 1280 px. The inspector opens again with the
        first selection, or when the split is left.
        """
        width = self._split_view.storage.window_width
        selection = self._flow.selection or {}
        if split:
            fold = (
                width is not None
                and width < _SPLIT_FOLD_WIDTH
                and not self._inspector.collapsed
                and not (selection.get("nodes") or selection.get("edges"))
            )
        else:
            fold = False
        if fold or self._inspector_folded:
            self._folding = True
            try:
                self._inspector.collapsed = fold
            finally:
                self._folding = False
            self._inspector_folded = fold

    def _on_inspector_toggled(self, _event):
        if not self._folding:
            self._inspector_folded = False

    def _on_pane_resized(self, _event):
        instance_id, self._pending_reveal = self._pending_reveal, None
        if instance_id is not None and self.mode == "split":
            self._reveal_node(instance_id)

    def _sync_placed(self):
        """Show a component added or removed on whichever of the canvas and grid is on screen."""
        if self._grid_on_screen:
            self._rebuild_tile_grid()
        else:
            self._rebuild_sidebar()

    def _stash_tile_layout(self):
        """Remember the grid's layout before the grid leaves the workspace."""
        if not self._grid_populated:
            return
        self._pending_tile_layout = self.layout
        self._pending_grid_config = self._grid_config()

    def _rebuild_sidebar(self):
        """Publish views of the placed components that opted into sidebar placement.

        Independent of the wiring/dashboard toggle, so it runs whenever the
        canvas changes rather than only when the tile grid is visible.
        """
        sidebar_views = []
        for i, item in enumerate(self._tile_items):
            entry = self._component_entries.get(item["component_id"])
            if entry is None or not entry.metadata.sidebar:
                continue
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = markdown_pane(f"*{escape_markdown(entry.title)}*")
            sidebar_views.append(view)
        self.sidebar = sidebar_views
        self._view_controls.sync()
        bar = self._dashboard_bar
        bar.health.track()
        bar.health_popover.sync()

    @pn.io.hold()
    def _rebuild_tile_grid(self):
        # Capture the layout keyed by instance id before the objects change, so it
        # can be realigned with the tiles that remain.
        layout = self._pending_tile_layout or self.layout
        config = self._pending_grid_config
        if config is None and self._grid_populated:
            config = self._grid_config()
        grid_views, grid_ids = [], []
        for i, item in enumerate(self._tile_items):
            entry = self._component_entries.get(item["component_id"])
            if "placeholder" not in item and (entry is None or entry.metadata.sidebar):
                continue
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = markdown_pane(f"*{escape_markdown(entry.title)}*")
            grid_views.append(self._grid_view(item, view))
            grid_ids.append(item["instance_id"])
        # TileGrid keeps the selection on its tile while the objects change, but
        # the instance ids only follow afterwards.
        self._rebuilding_grid = True
        try:
            self._tile_grid[:] = grid_views
        finally:
            self._rebuilding_grid = False
        self._grid_ids = grid_ids
        self._grid_populated = True
        self._rebuild_sidebar()
        # Size hints in pixels convert to the layout's percentages at the restored reference width.
        if config is not None:
            self._apply_grid_config(config)
            self._pending_grid_config = None
        self._tile_grid.layout = self._sized_layout(_align_layout(layout, grid_ids))
        self._pending_tile_layout = []
        self._sync_tile_selection()

    def _placed_layout(self, keyed: list[dict]) -> list[dict]:
        """Drop the layout entries of tiles that are no longer placed."""
        placed = {item["instance_id"] for item in self._tile_items}
        return [spec for spec in keyed if spec.get("instance_id") in placed]

    @property
    def layout(self) -> list[dict]:
        """The current tile layout, whether or not the grid is on screen.

        One entry per tile, keyed by the tile's ``instance_id``.
        """
        if self._grid_populated:
            return self._placed_layout(_key_layout(self._tile_grid.layout, self._grid_ids))
        return self._placed_layout(self._pending_tile_layout)

    # ------------------------------------------------------------------
    # Settings and refresh
    # ------------------------------------------------------------------

    def _settings_values(self) -> dict[str, t.Any]:
        return {name: getattr(self.settings, name) for name in self._settings_names}

    def _apply_settings(self, stored: dict[str, t.Any]):
        """Load a dashboard's stored settings, falling back to defaults for the rest."""
        # A fresh instance doubles as the validator, so a bad stored value falls back
        # to its default instead of keeping the previous dashboard's value.
        fresh = self.settings_class(**self._layout_defaults())
        for name, value in (stored or {}).items():
            if name not in self._settings_names:
                logger.debug("Ignoring unknown dashboard setting '%s'", name)
                continue
            try:
                setattr(fresh, name, value)
            except (TypeError, ValueError) as exc:
                logger.warning("Saved setting '%s' rejected: %s", name, exc)
        self._loading_settings = True
        try:
            self.settings.param.update(
                **{name: getattr(fresh, name) for name in self._settings_names}
            )
        finally:
            self._loading_settings = False

    def _subscriptions(self) -> dict[int, tuple[param.Parameterized, param.parameterized.Watcher]]:
        """Every watcher on the editor and its settings, which components subscribe to."""
        return {
            id(watcher): (owner, watcher)
            for owner in (self, self.settings)
            for by_mode in owner.param.watchers.values()
            for watchers in by_mode.values()
            for watcher in watchers
        }

    def _track_subscriptions(self, instance_id: str, before: dict, wiring: list | None = None):
        """Release what a component subscribed to while it was created along with its wiring.

        `_dispose_component` runs the releases however the component leaves the
        editor, so a removed component stops reacting to `refresh` and can be freed.
        With *wiring*, the list the component was placed with, subscriptions made
        after that placement was released (an async body finishing late) are
        released right away instead of attaching to a later placement.
        """
        current = self._component_wiring.get(instance_id)
        if wiring is None:
            wiring = current = self._component_wiring.setdefault(instance_id, [])
        for key, (owner, watcher) in self._subscriptions().items():
            if key in before:
                continue
            release = partial(owner.param.unwatch, watcher)
            if current is wiring:
                wiring.append(release)
            else:
                release()

    def _on_setting_changed(self, *events):
        if not self._loading_settings:
            self._record_edit(
                "Changed the settings",
                op="settings",
                args={"keys": sorted({event.name for event in events})},
            )

    @param.depends("dashboard", "settings.refresh_period", watch=True)
    def _schedule_refresh_timer(self, *_events):
        # The period can change before the page has loaded, and a callback added
        # then would start ahead of the session being ready.
        pn.state.onload(self._sync_refresh_timer)

    def _sync_refresh_timer(self):
        """Run one periodic callback while a dashboard is shown and a period is set."""
        period = self.settings.refresh_period if self.dashboard is not None else 0
        period_ms = int(period * 1000)
        if period_ms == self._refresh_ms:
            return
        self._stop_refresh_timer()
        if period_ms:
            self._refresh_timer = pn.state.add_periodic_callback(
                partial(self.param.trigger, "refresh"), period=period_ms
            )
            self._refresh_ms = period_ms
            self._refresh_started = time.time()

    def _stop_refresh_timer(self):
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
        self._refresh_ms = 0

    @param.depends("refresh", watch=True)
    def _stamp_refresh(self):
        self._dashboard_bar.stamp_refresh()

    # ------------------------------------------------------------------
    # Public canvas API
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    @property
    def turns(self) -> list[DashboardTurn]:
        """The turns of the open dashboard, oldest first, as copies.

        A turn is one request to the agent, or one batch of the person's edits
        made between requests; each node records the turn that created it as
        its origin. The editor opens a turn at the first edit that needs one:
        an edit made in an agent's tool call joins the turn of that agent run,
        any other edit the person's open turn. See :mod:`panel_flowdash.turns`.
        """
        return self._turn_log.snapshot()

    def node_origin(self, instance_id: str) -> str | None:
        """Return the id of the turn that created a node, or None if no turn is recorded."""
        return self._origins.get(instance_id)

    def end_turn(self):
        """End the turn of the person's edits, so their next edit starts a new turn.

        Call it when the person sends a request to an agent, which the
        FlowDash assistant does, so the edits made before and after the
        request form separate turns.
        """
        self._turn_log.end_user_turn()

    def end_agent_turns(self):
        """End the turns of the agent's requests, so the next request's edits start a new turn.

        The dashboard tools call it when a request ends, whether it finished,
        failed or was cancelled. Each request's edits then become one version
        of the edit history.
        """
        self._turn_log.end_agent_turns()
        self._history.end_requests()

    def _record_edit(
        self,
        *labels: str,
        op: str = "edit",
        args: dict[str, t.Any] | None = None,
        created: t.Iterable[str] = (),
    ) -> DashboardTurn | None:
        """Mark the canvas changed, note the change in its turn and record it in the history.

        *op* and *args* name the edit in its history version (see
        :mod:`panel_flowdash.history`), and the turn becomes the origin of
        the nodes in *created*. Returns the turn. While a dashboard is being
        loaded nothing is noted, and None is returned.
        """
        turn = None if self._hydrating else self._note_turn(*labels)
        if turn is not None:
            for instance_id in created:
                self._origins[instance_id] = turn.id
        self._recording_edit = True
        try:
            self.dirty = True
        finally:
            self._recording_edit = False
        if turn is not None:
            self._history.record(op, args or {}, label_for(labels), turn)
        return turn

    def _note_turn(self, *labels: str) -> DashboardTurn:
        turn = self._turn_log.record(*labels)
        self._turn_log.prune(set(self._origins.values()))
        return turn

    def _on_dirty(self, event):
        # Code that marks the canvas changed without `_record_edit` still opens a turn.
        if event.new and not self._recording_edit and not self._hydrating:
            self._history.record("edit", {}, None, self._note_turn())

    def _port_ref(self, instance_id: str, port: str) -> str:
        """Name a port as the YAML does; a restricted node's port is left out, as its YAML hides it."""
        placeholder = self._placeholders().get(instance_id)
        name = self._node_name(instance_id)
        return name if placeholder is not None and placeholder.private else f"{name}.{port}"

    def _edge_args(self, source_id, source_port, target_id, target_port) -> dict[str, str]:
        return {
            "source": self._port_ref(source_id, source_port),
            "target": self._port_ref(target_id, target_port),
        }

    def _node_name(self, instance_id: str) -> str:
        """Return the name a node has in the dashboard YAML, for a turn's summary."""
        from panel_flowdash.agent.yaml_format import node_names

        items = [{"instance_id": item["instance_id"]} for item in self._tile_items]
        if not any(item["instance_id"] == instance_id for item in items):
            items.append({"instance_id": instance_id})
        return node_names(items)[instance_id]

    def _connect_label(self, verb, source_id, source_port, target_id, target_port) -> str:
        # Only the nodes: a summary is shown to everyone who may read the
        # dashboard, including people from whom a restricted node's ports are hidden.
        joiner = "from" if verb == "Disconnected" else "to"
        return f"{verb} {self._node_name(source_id)} {joiner} {self._node_name(target_id)}"

    def add_component(
        self,
        component_id: str,
        config: dict | None = None,
        position: dict | tuple | None = None,
        instance_id: str | None = None,
    ) -> str:
        """Place a component on the canvas and return its instance id.

        Parameters
        ----------
        component_id
            Id of a registered component.
        config
            Design-time configuration overrides for this instance.
        position
            Canvas position as ``{"x": ..., "y": ...}`` or ``(x, y)``. Defaults
            to the next free slot in a three-column grid.
        instance_id
            Id for the new instance, which is also its node's name in the
            dashboard YAML, as a node a YAML file names gets it. It must be a
            valid node name without ``__``, at most ``MAX_NAME_LENGTH`` (64)
            characters, that no node on the canvas has.
            Defaults to a generated ``<component>_<6 hex>`` id.

        Returns
        -------
        str
            The new instance's id, for use with `connect` and `remove_component`.

        Raises
        ------
        KeyError
            If *component_id* is not a registered component.
        ValueError
            If *instance_id* is not a valid node name or is taken.
        PermissionError
            If `authorize_component` refuses it.
        """
        if component_id not in self._component_entries:
            raise KeyError(
                f"Unknown component '{component_id}'. Available: {sorted(self._component_entries)}"
            )
        if component_id in self._placed_singletons():
            title = self._component_entries[component_id].title
            raise ValueError(f"'{title}' can only be placed once per dashboard.")
        self.ensure_components_loaded([component_id])
        if component_id not in self._component_specs:
            raise KeyError(f"Component '{component_id}' failed to load.")
        # Checked after loading, which resolves rules the source scan could not read.
        if not self._component_authorized(component_id):
            title = self._component_entries[component_id].title
            raise PermissionError(f"You are not authorized to use '{title}'.")
        if instance_id is not None:
            self._check_instance_id(instance_id)
        instance_id = self._place(
            component_id,
            instance_id or self._new_instance_id(component_id),
            config or {},
            position,
        )
        name = self._node_name(instance_id)
        self._record_edit(
            f"Added {name}",
            op="add_node",
            args={"component": component_id, "name": name},
            created=[instance_id],
        )
        self._sync_placed()
        return instance_id

    def _taken_names(self) -> tuple[list[dict], set[str]]:
        """Return the canvas's items as node-name input, and every name and id they hold."""
        from panel_flowdash.agent.yaml_format import node_names

        items = [{"instance_id": item["instance_id"]} for item in self._tile_items]
        return items, set(node_names(items).values()) | {item["instance_id"] for item in items}

    def _check_instance_id(self, instance_id: str):
        """Refuse an instance id that is not a node name a YAML file could give, or is taken."""
        from panel_flowdash.agent.yaml_format import NAME_PATTERN

        if not isinstance(instance_id, str) or not NAME_PATTERN.match(instance_id):
            raise ValueError(
                f"The node name {instance_id!r} is not valid: names start with a letter or "
                "underscore and contain only letters, digits, '_' and '-'."
            )
        if len(instance_id) > MAX_NAME_LENGTH:
            raise ValueError(
                f"The node name {instance_id[:20]!r}... is not valid: names are at most "
                f"{MAX_NAME_LENGTH} characters, this one has {len(instance_id)}."
            )
        if "__" in instance_id:
            raise ValueError(
                f"The node name {instance_id!r} is not valid: names may not contain '__', "
                "which marks ids the editor generates."
            )
        _, taken = self._taken_names()
        if instance_id in taken:
            stem, n = instance_id[: MAX_NAME_LENGTH - 4].rstrip("_"), 2
            while f"{stem}_{n}" in taken:
                n += 1
            raise ValueError(
                f"A node named '{instance_id}' is already on the dashboard; "
                f"choose another name, e.g. '{stem}_{n}'."
            )

    def _new_instance_id(self, component_id: str) -> str:
        """Return a new instance id whose node name no node on the canvas has.

        The name the dashboard YAML gives a node comes from its id, so a new
        node must not take the name of an existing one, which would then
        have to be renamed.
        """
        from panel_flowdash.agent.yaml_format import node_names

        items, taken = self._taken_names()
        type_key = component_id.replace("/", "__")
        while True:
            instance_id = f"{type_key}_{uuid.uuid4().hex[:6]}"
            name = node_names([*items, {"instance_id": instance_id}])[instance_id]
            if instance_id not in taken and name not in taken:
                return instance_id

    def _place(self, component_id, instance_id, config, position=None) -> str:
        """Instantiate a component and add it to the graph, canvas and tile list.

        A component already placed under *instance_id* is removed first, so
        the new one replaces it rather than sitting next to it under the same id.
        """
        entry = self._component_entries[component_id]
        spec = self._component_specs[component_id]
        if any(item["instance_id"] == instance_id for item in self._tile_items):
            self._unplace(instance_id)

        node_state = self._dataflow_graph.add_node(instance_id, component_id)
        config_state = self._dataflow_graph.get_config_state(instance_id)
        config_data = self._seed_config_state(instance_id, config)
        # Checked before the component is created, so data the canvas would
        # reject leaves nothing behind for a placeholder to share the id with.
        try:
            self._check_node_data(component_id.replace("/", "__"), config_data)
        except ValueError:
            self._dataflow_graph.remove_node(instance_id)
            raise
        before = self._subscriptions()
        token = _INSTANTIATING.set(self)
        try:
            view = self._instantiate_for_node(entry, node_state, config_state)
        except Exception:
            self._dataflow_graph.remove_node(instance_id)
            self._track_subscriptions(instance_id, before)
            self._dispose_component(instance_id)
            raise
        finally:
            _INSTANTIATING.reset(token)
        self._track_subscriptions(instance_id, before)
        # The component is instantiated even while blocked, so it can subscribe
        # and publish outputs; only what the tile displays is gated.
        view, release = gate_view(view, node_state, spec.inputs, entry.metadata.placeholder)
        if release is not None:
            self._component_wiring.setdefault(instance_id, []).append(release)
        # Over the placeholder too: a failure upstream is what keeps an input unset.
        failures = self._dataflow_graph.get_failures(instance_id)
        if self.propagate_failures:
            view = with_failure_overlay(
                view, failures, partial(self._describe_failures, instance_id)
            )

        if position is None:
            position = self._default_position()
        elif isinstance(position, tuple):
            position = {"x": position[0], "y": position[1]}

        node = pr.NodeSpec(
            id=instance_id,
            type=component_id.replace("/", "__"),
            position=position,
            label=spec.title,
            data=config_data,
        )
        node_dict = node.to_dict()
        node_dict["view"] = node_card(
            spec,
            node_state,
            failures,
            self._component_wiring.setdefault(instance_id, []),
            instance=self._component_instances.get(instance_id),
            show_preview=getattr(self, "_node_previews_shown", self.settings.node_previews),
        )
        self._add_canvas_node(node_dict)

        self._tile_items.append(
            {
                "instance_id": instance_id,
                "component_id": component_id,
                "config": dict(config),
            }
        )
        self._tile_objects.append(view)
        if self.propagate_failures:
            watcher = failures.param.watch(
                partial(self._sync_failure_marks, instance_id), "failed"
            )
            self._component_wiring.setdefault(instance_id, []).append(
                partial(failures.param.unwatch, watcher)
            )
        self._sync_toolbar_state()
        return instance_id

    def _default_position(self) -> dict:
        """Pick a free spot near the top-left of the visible canvas."""
        viewport = self._flow.viewport
        if viewport:
            # The server never learns the canvas size, so anchor to the visible
            # top-left corner rather than the centre.
            zoom = viewport.get("zoom") or 1
            x = (_PLACEMENT_MARGIN - viewport.get("x", 0)) / zoom
            y = (_PLACEMENT_MARGIN - viewport.get("y", 0)) / zoom
        else:
            count = len(self._tile_items)
            x, y = (count % 3) * 350, (count // 3) * 250
        taken = {(round(n["position"]["x"]), round(n["position"]["y"])) for n in self._flow.nodes}
        while (round(x), round(y)) in taken:
            x += _PLACEMENT_CASCADE
            y += _PLACEMENT_CASCADE
        return {"x": x, "y": y}

    def _placed_singletons(self) -> set[str]:
        return {
            item["component_id"]
            for item in self._tile_items
            if "placeholder" not in item
            and self._component_entries[item["component_id"]].metadata.singleton
        }

    def remove_component(self, instance_id: str):
        """Remove a placed component along with its edges and its tile."""
        self._unplace(instance_id)
        name = self._node_name(instance_id)
        self._record_edit(f"Removed {name}", op="remove_node", args={"name": name})
        self._sync_placed()

    def _unplace(self, instance_id: str):
        """Take a component off the graph, canvas and tile list, and dispose it."""
        self._forget_node(instance_id)
        if self._canvas_batch is not None:
            nodes, edges = self._canvas_batch
            nodes[:] = [n for n in nodes if n["id"] != instance_id]
            edges[:] = [e for e in edges if instance_id not in (e["source"], e["target"])]
        if any(node["id"] == instance_id for node in self._flow.nodes):
            with self._muted_canvas():
                self._flow.remove_node(instance_id)
        self._dispose_component(instance_id)

    def connect(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, t.Any]] | None = None,
    ) -> bool | str:
        """Wire an output port to an input port.

        Parameters
        ----------
        transform
            Steps applied to the values carried along the edge, see
            :meth:`set_edge_transform`.

        Returns
        -------
        bool or str
            ``True`` on success, or a message explaining the rejection (unknown
            port, type mismatch, cycle, an input that is already connected, or
            an invalid transform). An input fed by a component this session may
            not use counts as connected.
        """
        restricted = self._restricted_input_error(target_id, target_port)
        if restricted is not None:
            return restricted
        result = self._dataflow_graph.add_edge(
            source_id, source_port, target_id, target_port, transform
        )
        if result is not True:
            return result
        self._edge_count += 1
        edge_id = f"e{self._edge_count}"
        self._edge_id_map[edge_id] = (source_id, source_port, target_id, target_port)
        mapping = self._edge_id_map[edge_id]
        self._add_canvas_edge(
            {
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "sourceHandle": source_port,
                "targetHandle": target_port,
                "markerEnd": {"type": "arrowclosed"},
                **(self._edge_chip(mapping) if transform or self._edge_failed(mapping) else {}),
            }
        )
        self._record_edit(
            self._connect_label("Connected", *mapping),
            op="connect",
            args=self._edge_args(*mapping),
        )
        return True

    def set_edge_transform(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, t.Any]] | None,
    ) -> bool | str:
        """Replace the transform applied along an edge.

        Each step is a dict naming an ``op`` from
        :data:`panel_flowdash.transforms.TRANSFORMS` plus its arguments, for
        example ``[{"op": "column", "column": "capacity"}, {"op": "aggregate",
        "how": "sum"}]``. Pass an empty list to remove the transform.

        Returns
        -------
        bool or str
            ``True`` on success, or a message explaining the rejection. A
            rejected transform leaves the edge on its previous one.
        """
        mapping = (source_id, source_port, target_id, target_port)
        result = self._dataflow_graph.set_edge_transform(*mapping, transform)
        if result is not True:
            return result
        self._record_edit(
            self._connect_label("Changed the transform from", *mapping),
            op="set_transform",
            args=self._edge_args(*mapping),
        )
        chip = self._edge_chip(mapping)
        for edge_id, existing in self._edge_id_map.items():
            if existing == mapping:
                self._flow.patch_edge_props(edge_id, chip)
        return True

    def _edge_chip(self, mapping) -> dict[str, t.Any]:
        """Edge props summarizing an edge's transform and failure, clearing them when it has none.

        The label names a single step, e.g. "fx first 500", or counts them,
        e.g. "fx 3 steps". An edge whose transform failed, or whose edited
        transform cannot be applied, is drawn in the error colour.
        """
        steps = parse_transform(self._dataflow_graph.get_edge_transform(*mapping))
        invalid = any(
            editor.issue
            for edge_id, editor in self._transform_editors.items()
            if self._edge_id_map.get(edge_id) == mapping
        )
        if len(steps) == 1:
            label = f"fx {summarize_transform(steps, max_length=24) or steps[0].op}"
        elif steps:
            label = f"fx {len(steps)} steps"
        else:
            label = "fx not applied" if invalid else None
        style = _TRANSFORMED_EDGE_STYLE if label else None
        if invalid or self._edge_failed(mapping):
            style = {**(style or {}), **_FAILED_EDGE_STYLE}
        return {"label": label, "style": style}

    def _edge_failed(self, mapping) -> bool:
        """Whether the failure held by the edge's target input arrived over this edge."""
        failures = self._dataflow_graph.get_failures(mapping[2])
        failure = failures.failed.get(mapping[3]) if failures is not None else None
        return failure is not None and (failure.source_id, failure.source_port) == mapping[:2]

    def _transform_editor(self, edge_id: str) -> TransformEditor:
        """Return the transform editor of an edge, which the inspector shows while it is selected.

        It is kept until the edge is removed, so a draft that cannot be applied
        yet stays flagged on the canvas and in the status bar after the edge is
        deselected.
        """
        editor = self._transform_editors.get(edge_id)
        if editor is None:
            editor = self._transform_editors[edge_id] = TransformEditor(self, edge_id)
            editor.param.watch(lambda _event: self._on_transform_issue(edge_id), "issue")
        return editor

    def _on_transform_issue(self, edge_id: str):
        mapping = self._edge_id_map.get(edge_id)
        if mapping is None:
            return
        self._patch_canvas_props("edge", edge_id, self._edge_chip(mapping))
        self._status_bar.sync()
        self._inspector.refresh_summary()

    def _on_selection_changed(self, event):
        """Reload an edge's transform editor when the edge is newly selected, keeping drafts."""
        previous = set((event.old or {}).get("edges", []))
        for edge_id in (event.new or {}).get("edges", []):
            editor = self._transform_editors.get(edge_id)
            if edge_id not in previous and editor is not None:
                editor.sync()

    def _drop_edge_editor(self, edge_id: str):
        editor = self._transform_editors.pop(edge_id, None)
        if editor is not None:
            editor.dispose()

    def disconnect(self, source_id: str, source_port: str, target_id: str, target_port: str):
        """Remove the edge between two ports."""
        mapping = (source_id, source_port, target_id, target_port)
        self._dataflow_graph.remove_edge(*mapping)
        for edge_id, existing in list(self._edge_id_map.items()):
            if existing == mapping:
                del self._edge_id_map[edge_id]
                self._drop_edge_editor(edge_id)
                with self._muted_canvas():
                    self._flow.remove_edge(edge_id)
        self._record_edit(
            self._connect_label("Disconnected", *mapping),
            op="disconnect",
            args=self._edge_args(*mapping),
        )

    @pn.io.hold()
    def clear(self):
        """Remove every component and edge from the canvas."""
        had_items = bool(self._tile_items)
        self._reset_canvas()
        if had_items:
            self._record_edit("Cleared the dashboard", op="clear")
        self._notify("info", "Cleared all component tiles.", duration=3000)

    def _reset_canvas(self):
        """Tear down all node/edge state, clear the ReactFlow canvas and dispose the components."""
        for node_id in list(self._dataflow_graph.node_ids):
            self._dataflow_graph.remove_node(node_id)
        self._tile_items = []
        self._tile_objects = []
        self._inspect_tiles.clear()
        self._close_inspection_dialog()
        self._edge_id_map.clear()
        for edge_id in list(self._transform_editors):
            self._drop_edge_editor(edge_id)
        self._edge_count = 0
        self._unresolved_edges = []
        self._unresolved_notice = None
        self._origins = {}
        self._rebuild_sidebar()
        with self._muted_canvas():
            self._flow.param.update(nodes=[], edges=[])
        if self._grid_populated:
            self._tile_grid[:] = []
        self._grid_ids = []
        self._sync_toolbar_state()
        for instance_id in [*self._component_instances, *self._component_wiring]:
            self._dispose_component(instance_id)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_model(self, title: str | None = None) -> DashboardModel:
        """Serialize the current canvas into a :class:`DashboardModel`.

        The returned model is detached from the editor, so this is the seam to
        use when persisting to something other than the configured store.
        """
        current = self.dashboard
        positions = {
            node.get("id", ""): (
                node.get("position", {}).get("x", 0),
                node.get("position", {}).get("y", 0),
            )
            for node in self._flow.nodes
        }
        model = DashboardModel(
            dashboard_id=current.dashboard_id if current else uuid.uuid4().hex[:12],
            user_id=current.user_id if current else self.user,
            title=title or (current.title if current else "Untitled"),
        )
        if current is not None:
            model.version = current.version
            model.permission = current.permission
            model.revision = current.revision
        # Keys this editor has no parameter for stay, so saving does not erase
        # settings an application with a richer `settings_class` stored.
        stored = current.settings if current else {}
        model.settings = {
            **{k: v for k, v in stored.items() if k not in self._settings_names},
            **self._settings_values(),
        }
        model.items = [
            DashboardItem(
                instance_id=item["instance_id"],
                component_id=item["component_id"],
                x=positions.get(item["instance_id"], (0, 0))[0],
                y=positions.get(item["instance_id"], (0, 0))[1],
                config=item.get("config", {}),
                origin=self._origins.get(item["instance_id"]),
            )
            for item in self._tile_items
        ]
        model.turns = self._turn_log.snapshot()
        model.edges = [
            DashboardEdge(
                source=edge["source"],
                source_port=edge["source_port"],
                target=edge["target"],
                target_port=edge["target_port"],
                transform=edge["transform"],
            )
            for edge in self._dataflow_graph.edges
        ] + [copy.deepcopy(unresolved.edge) for unresolved in self._unresolved_edges]
        # Read the layout through `layout` and the pending-config fields so that
        # saving from wiring mode, where the grid is off screen, cannot clobber a
        # layout that was loaded from storage but never rendered.
        model.tile_layout = self.layout
        config = self._grid_config()
        model.breakpoints = config["breakpoints"]
        model.responsive_layouts = config["responsive_layouts"]
        model.reference_width = config["reference_width"]
        return model

    def load_model(self, model: DashboardModel):
        """Hydrate the canvas from a :class:`DashboardModel`.

        Items and edges that cannot be restored, such as a component this editor
        does not offer or an edge whose port no longer exists, do not abort the
        load. They are shown as placeholders and saved back unchanged.

        Only the components the model places are imported, so a component that
        does work at import time cannot slow down dashboards that do not use it.
        On a live server prefer :meth:`load_model_async`, which imports off the
        event loop.
        """
        self.ensure_components_loaded(self._model_component_ids(model))
        self._hydrate_model(model)

    async def load_model_async(self, model: DashboardModel):
        """Async :meth:`load_model`, importing the model's components off the event loop."""
        await self.ensure_components_loaded_async(self._model_component_ids(model))
        self._hydrate_model(model)

    @staticmethod
    def _model_component_ids(model: DashboardModel) -> set[str]:
        """Return the ids of the components a dashboard model places."""
        return {item.component_id for item in model.items}

    def _hydrate_model(self, model: DashboardModel, keep_open_turns: bool = False):
        """Populate the canvas from *model*, assuming its components are loaded.

        The model's turns replace the editor's. With *keep_open_turns* the
        turns open to more edits stay open, for a change made by loading a model.
        """
        self._hydrating = True
        try:
            self._hydrate(model, keep_open_turns)
        finally:
            self._hydrating = False
        if not self._history_quiet:
            self._note_restricted()
            self._history.opened()

    @contextmanager
    def _quiet_history(self):
        """Load models inside the block as part of an edit, not as opening a dashboard."""
        quiet, self._history_quiet = self._history_quiet, True
        try:
            yield
        finally:
            self._history_quiet = quiet

    @pn.io.hold()
    def _hydrate(self, model: DashboardModel, keep_open_turns: bool):
        # Every placement path, placeholders included, then sees one item per id,
        # so the canvas and the tiles agree and a save does not write repeats back.
        if duplicates := _drop_repeated_ids(model):
            logger.warning(
                "Dashboard '%s' has %d item(s) repeating the id of an earlier item; "
                "only the first item with each id is loaded.",
                model.dashboard_id,
                duplicates,
            )
        with self._muted_canvas():
            self._reset_canvas()
        self._turn_log.reset(model.turns, keep_open=keep_open_turns)
        known = {turn.id for turn in model.turns}
        self._origins = {
            item.instance_id: item.origin for item in model.items if item.origin in known
        }
        # After the previous components are disposed, so none of them reacts to the
        # new values, and before these are placed, so they see this dashboard's.
        self._apply_settings(model.settings)
        self.dashboard = model
        # Unlike palette drops (see `_on_component_dropped`) this replaces every
        # node view at once, so no view already on the canvas can be blanked.
        aliases = self._component_aliases()
        with self._muted_canvas(), self._batched_canvas(), self._dataflow_graph.batch():
            for item in model.items:
                component_id = self._resolve_component_id(item.component_id, aliases)
                placeholder = self._item_placeholder(item, component_id)
                if placeholder is None:
                    spec = self._component_specs[component_id]
                    try:
                        self._place(
                            component_id,
                            item.instance_id,
                            {spec.resolve_name(key): value for key, value in item.config.items()},
                            {"x": item.x, "y": item.y},
                        )
                        continue
                    except Exception:
                        logger.exception(
                            "Error loading component '%s' (%s)",
                            item.component_id,
                            item.instance_id,
                        )
                        placeholder = Placeholder(
                            "failed", spec.title, f"'{spec.title}' failed to load."
                        )
                self._place_placeholder(item, placeholder)
            placeholders = self._placeholders()
            for edge in model.edges:
                if edge.source in placeholders or edge.target in placeholders:
                    result = "One of its components could not be restored."
                else:
                    result = self.connect(
                        edge.source,
                        self._resolve_port_name(edge.source, edge.source_port),
                        edge.target,
                        self._resolve_port_name(edge.target, edge.target_port),
                        edge.transform,
                    )
                if result is not True:
                    logger.warning(
                        "Keeping unresolved saved edge %s.%s -> %s.%s: %s",
                        edge.source,
                        edge.source_port,
                        edge.target,
                        edge.target_port,
                        result,
                    )
                    self._unresolved_edges.append(UnresolvedEdge(edge, result))
        self._unresolved_notice = unresolved_summary(
            len(placeholders), len(self._unresolved_edges)
        )

        legacy_ids = self._legacy_layout_ids(model)
        self._grid_populated = False
        self._pending_tile_layout = _key_saved_layout(model.tile_layout, legacy_ids)
        self._pending_grid_config = {
            "breakpoints": list(model.breakpoints or []),
            "responsive_layouts": {
                band: _key_saved_layout(layout, legacy_ids)
                for band, layout in (model.responsive_layouts or {}).items()
            },
            "reference_width": model.reference_width,
        }
        self.dirty = False
        self._stamp_refresh()
        self._layout_panel.band = None
        self._apply_mode_state()
        self._sync_toolbar_state()
        self._notify_unresolved()

    def _resolve_port_name(self, instance_id: str, port: str) -> str:
        """Return the current name of a port a saved edge may name by a former name."""
        spec = self._component_specs.get(self._component_id_for_instance(instance_id) or "")
        return spec.resolve_name(port) if spec is not None else port

    def _item_placeholder(self, item: DashboardItem, component_id: str) -> Placeholder | None:
        """Return the placeholder to show instead of placing a saved item, or None to place it.

        Parameters
        ----------
        item
            The saved item, untouched.
        component_id
            Its component id after resolving aliases.
        """
        if component_id in self._component_entries and not self._component_authorized(
            component_id
        ):
            return Placeholder(
                "denied",
                "Restricted component",
                "You do not have access to this component.",
                private=True,
            )
        if component_id in self._component_specs:
            return None
        entry = self._component_entries.get(component_id)
        if entry is not None:
            return Placeholder("failed", entry.title, f"'{entry.title}' could not be imported.")
        return Placeholder(
            "missing",
            item.component_id,
            f"Component '{item.component_id}' is not available. It is kept on the "
            "dashboard and comes back once the component is offered again.",
        )

    def _place_placeholder(self, item: DashboardItem, placeholder: Placeholder) -> str:
        """Show a saved item that cannot be placed as a placeholder node and tile.

        The item's component id and config are kept as saved, so the next save
        writes it back unchanged; none of it is sent to the page.
        """
        view = placeholder_view(placeholder)
        node = pr.NodeSpec(
            id=item.instance_id,
            type=PLACEHOLDER_NODE_TYPE,
            position={"x": item.x, "y": item.y},
            label=placeholder.title,
            data={},
        ).to_dict()
        node["view"] = placeholder_card(placeholder)
        self._add_canvas_node(node)
        self._tile_items.append(
            {
                "instance_id": item.instance_id,
                "component_id": item.component_id,
                "config": copy.deepcopy(item.config),
                "placeholder": placeholder,
            }
        )
        self._tile_objects.append(view)
        self._sync_toolbar_state()
        return item.instance_id

    def _placeholders(self) -> dict[str, Placeholder]:
        """Return the placeholders on the canvas, by instance id."""
        return {
            item["instance_id"]: item["placeholder"]
            for item in self._tile_items
            if "placeholder" in item
        }

    def _broken_edges_listing(self) -> Viewable:
        """List the saved edges that could not be wired, leaving out private endpoints."""
        placeholders = self._placeholders()
        lines, hidden = [], 0
        for unresolved in self._unresolved_edges:
            edge = unresolved.edge
            ends = []
            for instance_id, port in (
                (edge.source, edge.source_port),
                (edge.target, edge.target_port),
            ):
                placeholder = placeholders.get(instance_id)
                if placeholder is not None and placeholder.private:
                    break
                if placeholder is not None:
                    title = placeholder.title
                else:
                    spec = self._component_specs.get(
                        self._component_id_for_instance(instance_id) or ""
                    )
                    title = spec.title if spec else instance_id
                ends.append(f"{title}.{port}")
            else:
                lines.append(f"{ends[0]} → {ends[1]}: {unresolved.reason}")
                continue
            hidden += 1
        return broken_edges_panel(lines, hidden, self._discard_unresolved_edges)

    def _discard_unresolved_edges(self):
        """Drop the saved edges that could not be wired, so the next save leaves them out."""
        if not self._unresolved_edges:
            return
        self._unresolved_edges = []
        self._record_edit(
            "Discarded the connections that could not be restored", op="discard_unresolved"
        )
        self._sync_toolbar_state()

    @param.depends("editable", "read_only", watch=True)
    def _notify_unresolved(self):
        """Tell an editor once that a loaded dashboard holds items that could not be restored."""
        if self._unresolved_notice is None or not self.editable or self.read_only:
            return
        message, self._unresolved_notice = self._unresolved_notice, None
        self._notify("warning", message, duration=8000)

    def _legacy_layout_ids(self, model: DashboardModel) -> list[str]:
        """Return the tiles an untagged layout saved with *model* lines up with, in grid order.

        Older versions saved entries by grid position, over every item except
        sidebar components. Items whose component is no longer offered still
        held a slot when the layout was saved, so they keep theirs here.
        """
        ids = []
        aliases = self._component_aliases()
        for item in model.items:
            entry = self._component_entries.get(
                self._resolve_component_id(item.component_id, aliases)
            )
            # A sidebar component that is no longer in the registry cannot be
            # recognised as one, so it is counted as a tile and shifts the
            # legacy entries after it by one slot. Layouts saved since tiles
            # were keyed by instance id are unaffected.
            if entry is not None and entry.metadata.sidebar:
                continue
            ids.append(item.instance_id)
        return ids

    def load(self, dashboard_id: str):
        """Load a dashboard from the configured store, by id or title."""
        if self.store is None:
            raise ValueError("Cannot load a dashboard without a store.")
        model = self.store.find_by_id_or_title(dashboard_id)
        if model is None:
            raise KeyError(f"Dashboard not found: {dashboard_id}")
        self.load_model(model)

    def new_dashboard(self, title: str) -> DashboardModel:
        """Start a new empty dashboard, persisting it if a store is configured."""
        if self.store is not None:
            model = self.store.create_dashboard(self.user, title)
        else:
            model = DashboardModel(
                dashboard_id=uuid.uuid4().hex[:12], user_id=self.user, title=title
            )
        self._start_dashboard(model)
        return model

    def _start_dashboard(self, model: DashboardModel):
        """Show an empty canvas for a newly created *model*."""
        self._reset_canvas()
        self._pending_tile_layout = []
        self._pending_grid_config = dict(_EMPTY_GRID_CONFIG)
        self._layout_panel.band = None
        self._apply_settings(model.settings)
        self._turn_log.reset([])
        self.dashboard = model
        self.dirty = False
        self._note_restricted()
        self._history.opened()

    def save(self, title: str | None = None) -> DashboardModel:
        """Persist the current canvas and return the saved model.

        With no store configured the model is still built and returned, so the
        caller can persist it themselves.

        The owner, sharing rules and (unless *title* is given) title are taken
        from the stored dashboard rather than from the copy loaded into the
        editor, so saving never reverts a rename or sharing change made from
        another session since the dashboard was opened. If the dashboard is no
        longer stored (deleted from another session, or loaded from elsewhere),
        saving creates it with this editor's :attr:`user` as the owner, so it
        is never recreated under the previous owner's name.

        Raises
        ------
        RuntimeError
            If :attr:`read_only` is set, or :attr:`authorize_dashboard` no
            longer grants edit access to the stored dashboard (which also sets
            :attr:`read_only`).
        DashboardConflictError
            If the store holds a newer revision of the dashboard than the one
            loaded into the editor, because another session saved it since.
            Nothing is written; reload the dashboard or use :meth:`save_as`.
        """
        if self.read_only:
            raise _ReadOnlyEditorError("This dashboard is read-only.")
        model = self.to_model(title=title)
        if self.store is not None:
            stored = self.store._load_any(model.dashboard_id)
            if stored is not None and not self._dashboard_authorized(stored, "edit"):
                self.read_only = True
                raise _ReadOnlyEditorError(
                    "You no longer have edit access to this dashboard, so it was not saved."
                )
            if stored is not None:
                model.user_id = stored.user_id
                model.permission = stored.permission
                if title is None:
                    model.title = stored.title
            else:
                model.user_id = self.user
            self.store.save_dashboard(model)
        self.dashboard = model
        self.dirty = False
        self._history.saved()
        self.param.trigger("saved")
        return model

    def save_as(self, title: str) -> DashboardModel:
        """Persist the current canvas as a new dashboard titled *title* and switch to it.

        The new dashboard is owned by this editor's :attr:`user` and not
        shared with anyone. With no store configured the model is still built
        and returned.

        Raises
        ------
        RuntimeError
            If :attr:`read_only` is set.
        """
        if self.read_only:
            raise _ReadOnlyEditorError("This dashboard is read-only.")
        model = self.to_model(title=title)
        model.permission = Permission()
        if self.store is not None:
            created = self.store.create_dashboard(self.user, title)
            model.dashboard_id, model.user_id = created.dashboard_id, created.user_id
            model.title, model.revision = created.title, created.revision
            self.store.save_dashboard(model)
        else:
            model.dashboard_id, model.user_id = uuid.uuid4().hex[:12], self.user
            model.revision = None
        self.dashboard = model
        self.dirty = False
        self._history.opened()
        self.param.trigger("saved")
        return model

    # ------------------------------------------------------------------
    # Edit history
    # ------------------------------------------------------------------

    @property
    def edit_history(self) -> EditHistory:
        """The versions of the open dashboard, one per edit; see :mod:`panel_flowdash.history`.

        Its ``state`` is the panel-agents ``RunState`` they are published in,
        to show them in an ``ArtifactView`` or share with an ``AgentRunner``.
        """
        return self._history

    @property
    def can_undo(self) -> bool:
        """Whether :meth:`undo` has an edit to undo."""
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        """Whether :meth:`redo` has an undone edit to redo."""
        return self._history.can_redo

    def undo(self, *, whole_runs: bool = True) -> bool:
        """Undo the last edit to the dashboard and return whether anything was undone.

        A person's edits are undone one at a time, a gesture such as a drag
        as one edit. The edits an agent made for one request are undone
        together, back to the dashboard as it was before the request, unless
        *whole_runs* is False. Undo reaches back into earlier sessions, as far
        as the store keeps the dashboard's history (see :attr:`history_limit`).

        Undoing restores the dashboard's turns and node origins as they were,
        and never brings back a node whose component the user may not use or
        that this editor does not offer.

        Raises
        ------
        PermissionError
            If undoing would bring back such a node; nothing changes.
        """
        return self._history.undo(whole_runs=whole_runs)

    def redo(self) -> bool:
        """Redo what the last :meth:`undo` undid, all of it, and return whether anything was redone.

        Redo is available until the next edit.

        Raises
        ------
        PermissionError
            As for :meth:`undo`.
        """
        return self._history.redo()

    def _restore_version(self, text: str):
        """Make the canvas match a version of its history, as undo, redo and reverting do.

        The version's turns and node origins come back with it. A node whose
        component the user may not use, or that this editor does not offer,
        comes back only as the canvas held it, or as it was when the
        dashboard was opened, and never with what the version says of it.
        """
        model = self._restore_model(text)
        self.ensure_components_loaded(self._model_component_ids(model))
        with self._quiet_history():
            self._hydrating = True
            try:
                patched = self._patch_canvas(model)
            finally:
                self._hydrating = False
            if not patched:
                self._hydrate_model(model, keep_open_turns=True)

    @pn.io.hold()
    def _patch_canvas(self, model: DashboardModel) -> bool:
        """Change only what differs between the canvas and *model*, and return whether it could.

        Nodes that stay keep their component and its outputs, so restoring a
        version that changed one node does not rebuild the others. Anything
        this does not handle, such as placeholders or connections that could
        not be restored, returns False before changing the canvas, and the
        caller loads the whole model instead.
        """
        current = self.to_model()
        if _drop_repeated_ids(model) or self._unresolved_edges:
            return False
        placeholders = self._placeholders()
        aliases = self._component_aliases()
        before = {item.instance_id: item for item in current.items}
        after = {item.instance_id: item for item in model.items}

        def resolved(item):
            return self._resolve_component_id(item.component_id, aliases)

        removed = [iid for iid in before if iid not in after]
        added = [iid for iid in after if iid not in before]
        replaced = [
            iid for iid in after if iid in before and resolved(after[iid]) != resolved(before[iid])
        ]
        kept = [iid for iid in after if iid in before and iid not in replaced]
        for iid in [*removed, *replaced]:
            if iid in placeholders:
                return False
        for iid in [*added, *replaced]:
            if self._item_placeholder(after[iid], resolved(after[iid])) is not None:
                return False
        for iid in kept:
            if iid in placeholders and (
                before[iid].config != after[iid].config
                or before[iid].component_id != after[iid].component_id
            ):
                return False

        self._turn_log.reset(model.turns, keep_open=True)
        known = {turn.id for turn in model.turns}
        self._origins = {
            item.instance_id: item.origin for item in model.items if item.origin in known
        }
        if model.settings != current.settings:
            self._apply_settings(model.settings)
        self.dashboard = model
        edge_key = lambda e: (e.source, e.source_port, e.target, e.target_port)
        old_edges = {edge_key(e): e for e in current.edges}
        new_edges = {edge_key(e): e for e in model.edges}
        with self._muted_canvas(), self._batched_canvas(), self._dataflow_graph.batch():
            for key, edge in old_edges.items():
                if key not in new_edges and edge.source in before and edge.target in before:
                    if (
                        edge.source not in removed + replaced
                        and edge.target not in removed + replaced
                    ):
                        self.disconnect(*key)
            for iid in [*removed, *replaced]:
                self._unplace(iid)
            for iid in [*replaced, *added]:
                item = after[iid]
                component_id = resolved(item)
                spec = self._component_specs[component_id]
                self._place(
                    component_id,
                    iid,
                    {spec.resolve_name(k): v for k, v in item.config.items()},
                    {"x": item.x, "y": item.y},
                )
            for iid in kept:
                if iid not in placeholders:
                    self._set_config(iid, after[iid].config)
            fresh = set(replaced) | set(added)
            for key, edge in new_edges.items():
                old = old_edges.get(key)
                endpoints_new = edge.source in fresh or edge.target in fresh
                if old is None or endpoints_new:
                    result = self.connect(*key, edge.transform)
                elif old.transform != edge.transform:
                    result = self.set_edge_transform(*key, edge.transform or [])
                else:
                    continue
                if result is not True:
                    logger.warning("Restoring a version could not wire %s: %s", key, result)
                    self._unresolved_edges.append(UnresolvedEdge(edge, result))
        self._match_order(model)
        moved = {
            iid: (after[iid].x, after[iid].y)
            for iid in kept
            if (after[iid].x, after[iid].y) != (before[iid].x, before[iid].y)
        }
        if moved:
            with self._muted_canvas():
                self._flow.nodes = [
                    {**node, "position": {"x": moved[node["id"]][0], "y": moved[node["id"]][1]}}
                    if node.get("id") in moved
                    else node
                    for node in self._flow.nodes
                ]
        layout = (
            model.tile_layout,
            model.breakpoints,
            model.responsive_layouts,
            model.reference_width,
        )
        if (
            layout
            != (
                current.tile_layout,
                current.breakpoints,
                current.responsive_layouts,
                current.reference_width,
            )
            or added
            or removed
            or replaced
        ):
            legacy_ids = self._legacy_layout_ids(model)
            self._grid_populated = False
            self._pending_tile_layout = _key_saved_layout(model.tile_layout, legacy_ids)
            self._pending_grid_config = {
                "breakpoints": list(model.breakpoints or []),
                "responsive_layouts": {
                    band: _key_saved_layout(entries, legacy_ids)
                    for band, entries in (model.responsive_layouts or {}).items()
                },
                "reference_width": model.reference_width,
            }
            self._apply_mode_state()
        self._sync_placed()
        self._sync_toolbar_state()
        return True

    def _match_order(self, model: DashboardModel):
        """Put the canvas's nodes and edges in *model*'s order, which its YAML, and so its revision, follows."""
        rank = {item.instance_id: i for i, item in enumerate(model.items)}
        order = sorted(
            range(len(self._tile_items)),
            key=lambda i: rank.get(self._tile_items[i]["instance_id"], len(rank) + i),
        )
        if order != list(range(len(order))):
            objects = self._tile_objects
            self._tile_items = [self._tile_items[i] for i in order]
            if len(objects) == len(order):
                self._tile_objects = [objects[i] for i in order]
        edge_rank = {
            (e.source, e.source_port, e.target, e.target_port): i
            for i, e in enumerate(model.edges)
        }
        edges = self._dataflow_graph._edges
        keys = sorted(edges, key=lambda key: edge_rank.get(key, len(edge_rank)))
        if keys != list(edges):
            reordered = {key: edges[key] for key in keys}
            edges.clear()
            edges.update(reordered)

    def _set_config(self, instance_id: str, config: dict[str, t.Any]):
        """Give a placed node exactly *config*, resetting fields it leaves out to their defaults."""
        item = next(i for i in self._tile_items if i["instance_id"] == instance_id)
        spec = self._component_specs.get(item["component_id"])
        target = {spec.resolve_name(k): v for k, v in config.items()} if spec else dict(config)
        current = item.get("config", {})
        if target == current:
            return
        config_state = self._dataflow_graph.get_config_state(instance_id)
        for key in [k for k in current if k not in target]:
            if config_state is not None and key in config_state.param:
                setattr(config_state, key, config_state.param[key].default)
        patch = {k: v for k, v in target.items() if current.get(k, _MISSING) != v}
        for key, value in patch.items():
            if config_state is not None and hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, value)
                except Exception as exc:
                    logger.warning("Config '%s' rejected on %s: %s", key, instance_id, exc)
        item["config"] = copy.deepcopy(target)

    def _restore_refusal(self, text: str) -> str | None:
        """Return why restoring a version of the history is refused, or None if it is allowed."""
        try:
            self._restore_model(text, notify=False)
        except PermissionError as exc:
            return str(exc)
        except (ValueError, KeyError, TypeError) as exc:
            return f"That version cannot be read: {exc}"
        return None

    def _restore_model(self, text: str, notify: bool = True) -> DashboardModel:
        from panel_flowdash.agent.yaml_format import (
            dashboard_from_dict,
            node_names,
            read_dashboard_text,
        )

        if self.read_only:
            raise PermissionError("You may only view this dashboard, so it cannot be changed.")
        current = self.to_model()
        data = dashboard_from_dict(read_dashboard_text(text), base=self.export_dashboard())
        model = DashboardModel.from_dict(
            {
                **data,
                "dashboard_id": current.dashboard_id,
                "user_id": current.user_id,
                "title": current.title,
                "permission": None,
            }
        )
        model.permission, model.revision = current.permission, current.revision
        reference = self._restricted_reference(current)
        known = {item.instance_id for item in reference.items}
        refused = [
            iid
            for iid in self._restricted_ids(model, unknown=not self.import_unknown_components)
            if iid not in known
        ]
        if refused:
            names = node_names([item.to_dict() for item in model.items])
            listed = ", ".join(repr(names.get(iid, iid)) for iid in refused)
            raise PermissionError(
                f"That would bring back {listed}, which you may not use here, so nothing was changed."
            )
        origins = {item.instance_id: item.origin for item in model.items}
        notifications, self.notifications = self.notifications, self.notifications and notify
        try:
            self._guard_restricted(model, reference)
        finally:
            self.notifications = notifications
        for item in model.items:
            item.origin = origins.get(item.instance_id)
        return model

    def _restricted_reference(self, current: DashboardModel) -> DashboardModel:
        """Return the canvas with the restricted nodes it held when the dashboard was opened.

        The user cannot place such a node, so these are the only ones a
        version may bring back, as they were then.
        """
        on_canvas = {item.instance_id for item in current.items}
        extra = [
            copy.deepcopy(item)
            for iid, item in self._restricted_seen.items()
            if iid not in on_canvas
        ]
        edges = list(current.edges) + [
            copy.deepcopy(edge)
            for edge in self._restricted_seen_edges
            if edge not in current.edges
        ]
        return dataclasses.replace(current, items=[*current.items, *extra], edges=edges)

    def _note_restricted(self):
        """Remember the restricted nodes on the canvas as opened, for undo to bring back."""
        private = {iid for iid, p in self._placeholders().items() if p.private}
        model = self.to_model() if private else None
        self._restricted_seen = {
            item.instance_id: copy.deepcopy(item)
            for item in (model.items if model else [])
            if item.instance_id in private
        }
        self._restricted_seen_edges = [
            copy.deepcopy(edge)
            for edge in (model.edges if model else [])
            if {edge.source, edge.target} & private
        ]

    def export_dashboard(self, *, questions: bool = True) -> dict:
        """Return the canvas as a JSON-serializable dict, as the Download button writes it.

        The dashboard id, owner and sharing rules are omitted, so the file can
        be loaded into any dashboard with :meth:`import_dashboard`. With
        *questions* False its turns leave out what was asked of the agent,
        see :func:`export_model`.
        """
        private = {iid for iid, placeholder in self._placeholders().items() if placeholder.private}
        return export_model(self.to_model(), restricted=private, questions=questions)

    def import_dashboard(
        self, data: dict | str | bytes, *, keep_turns: bool = False
    ) -> DashboardModel:
        """Replace the canvas with an exported dashboard, as dropping its file does.

        *data* is a downloaded dashboard file, as JSON text or a dict, or a
        dashboard in the YAML format (see :mod:`panel_flowdash.agent`). The
        loaded dashboard keeps its id, owner, title and sharing rules, so
        saving writes the imported contents to it. Without a loaded dashboard
        the file's title is used and saving creates a new one. The canvas is
        marked dirty.

        The import is an edit of its own, in the turn of whoever makes it (see
        :attr:`turns`). By default the file's turns and node origins replace
        the canvas's, and nodes the file gives no origin get the import's
        turn. With *keep_turns* the file's turns and origins are ignored
        instead: the canvas keeps its turns, nodes already on it keep their
        origin and new nodes get the import's turn. That is how the agent's
        changes are loaded, so it cannot rewrite who made what. Nodes are
        matched by name: a node given another component under the same name
        keeps its origin, and a renamed node is a new node with the import's
        turn as its origin. A file that changes nothing opens no turn.

        Raises
        ------
        ValueError
            If *data* is neither a dashboard export nor a dashboard in the YAML
            format.
        """
        from panel_flowdash.agent.yaml_format import dashboard_to_dict

        model = self._imported_model(data)
        before = dashboard_to_dict(self)
        if not keep_turns:
            present = {item["instance_id"] for item in self._tile_items}
            with self._quiet_history():
                self.load_model(model)
            self._record_import(model, before, present)
            return model
        model.turns = self._turn_log.snapshot()
        present = {item["instance_id"] for item in self._tile_items}
        for item in model.items:
            item.origin = self._origins.get(item.instance_id)
        self.ensure_components_loaded(self._model_component_ids(model))
        with self._quiet_history():
            self._hydrate_model(model, keep_open_turns=True)
        labels = change_labels(before, dashboard_to_dict(self))
        if not labels:
            self._mark_dirty()
            return model
        self._record_edit(
            *labels,
            op="replace",
            created=[item.instance_id for item in model.items if item.instance_id not in present],
        )
        return model

    def _mark_dirty(self):
        """Mark the canvas changed without noting an edit in a turn."""
        self._recording_edit = True
        try:
            self.dirty = True
        finally:
            self._recording_edit = False

    def _record_import(self, model: DashboardModel, before: dict, present: set[str]):
        """Note an imported file that changed the canvas *before* in the turn of whoever imported it.

        The turn becomes the origin of the nodes that were not on the canvas
        and that the file gives no origin. A file that changes nothing opens
        no turn, though the canvas is still marked changed.
        """
        from panel_flowdash.agent.yaml_format import dashboard_to_dict

        if not change_labels(before, dashboard_to_dict(self)):
            self._mark_dirty()
            return
        self._record_edit(
            "Loaded a dashboard file",
            op="import",
            created=[
                item.instance_id
                for item in model.items
                if item.instance_id not in present and item.instance_id not in self._origins
            ],
        )

    def _imported_model(self, data: dict | str | bytes) -> DashboardModel:
        """Parse an export into a model that takes the loaded dashboard's identity."""
        from panel_flowdash.agent.yaml_format import (
            dashboard_from_dict,
            is_dashboard_yaml,
            read_dashboard_text,
        )

        current = self.dashboard
        if isinstance(data, (str, bytes)):
            data = read_dashboard_text(data)
        if is_dashboard_yaml(data):
            # Names map back to the ids of the canvas they were exported from.
            data = dashboard_from_dict(data, base=self.export_dashboard())
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Not a FlowDash dashboard export.")
        try:
            model = DashboardModel.from_dict(
                {
                    **data,
                    "dashboard_id": current.dashboard_id if current else uuid.uuid4().hex[:12],
                    "user_id": current.user_id if current else self.user,
                    "title": current.title if current else data.get("title") or "Untitled",
                    "permission": None,
                }
            )
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError(f"Malformed dashboard export: {exc!r}") from exc
        if current is not None:
            model.permission = current.permission
            model.revision = current.revision
        stored = self.store._load_any(current.dashboard_id) if current and self.store else None
        self._guard_restricted(model, stored or current)
        return model

    def _restricted_ids(self, model: DashboardModel, unknown: bool = False) -> set[str]:
        """Return the ids of the items of *model* whose component this session may not use.

        With *unknown*, items whose component this session does not offer at
        all count too, since nothing here can tell whether their users may.
        """
        aliases = self._component_aliases()
        restricted = set()
        for item in model.items:
            component_id = self._resolve_component_id(item.component_id, aliases)
            offered = component_id in self._component_entries
            if (
                item.component_id == RESTRICTED_COMPONENT_ID
                or (offered and not self._component_authorized(component_id))
                or (unknown and not offered)
            ):
                restricted.add(item.instance_id)
        return restricted

    def _guard_restricted(self, model: DashboardModel, reference: DashboardModel | None):
        """Keep an imported *model* from adding or changing items this session may not use.

        Without `import_unknown_components`, items whose component this
        session does not offer count as ones it may not use. Each is replaced
        by the item with its id in *reference*, the dashboard as stored, when
        that one is restricted (or unknown) too, or the file holds only the stand-in
        :func:`export_model` writes for it, and dropped otherwise. Imported
        edges touching these items are dropped and the stored edges between
        the kept ones and the rest restored, so the file decides neither their
        config nor their wiring. Of several items sharing an id only the first
        is kept, since placing the next would replace it.
        """
        duplicates = _drop_repeated_ids(model)
        if duplicates:
            self._notify(
                "warning",
                f"Left out {duplicates} item{'s' * (duplicates != 1)} of the file that "
                "repeat the id of another item.",
                duration=5000,
            )
        unknown = not self.import_unknown_components
        restricted = self._restricted_ids(model, unknown=unknown)
        if not restricted:
            return
        stored = {item.instance_id: item for item in reference.items} if reference else {}
        stored_restricted = (
            self._restricted_ids(reference, unknown=unknown) if reference else set()
        )
        items, kept = [], set()
        for item in model.items:
            iid = item.instance_id
            original = stored.get(iid)
            if iid not in restricted:
                items.append(item)
            elif original is not None and (
                iid in stored_restricted or item.component_id == RESTRICTED_COMPONENT_ID
            ):
                items.append(dataclasses.replace(copy.deepcopy(original), x=item.x, y=item.y))
                kept.add(iid)
        present = {item.instance_id for item in items}
        edges = [e for e in model.edges if not {e.source, e.target} & restricted]
        if reference is not None:
            edges += [
                copy.deepcopy(edge)
                for edge in reference.edges
                if {edge.source, edge.target} & kept and {edge.source, edge.target} <= present
            ]
        model.items, model.edges = items, edges
        dropped = len(restricted) - len(kept)
        if dropped:
            self._notify(
                "warning",
                f"Left out {dropped} component{'s' * (dropped != 1)} of the file that you "
                "do not have access to or that this app does not offer.",
                duration=5000,
            )

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------

    def _add_from_ui(self, component_id, position=None) -> str | None:
        """Add *component_id*, reporting failures as notifications instead of raising."""
        if component_id in self._placed_singletons():
            title = self._component_entries[component_id].title
            self._notify("warning", f"'{title}' is already on the canvas.", duration=3000)
            return None
        try:
            instance_id = self.add_component(component_id, position=position)
        except KeyError:
            self._notify("warning", "Select a valid component first.", duration=3000)
        except PermissionError as exc:
            self._notify("error", str(exc), duration=5000)
        except Exception as exc:
            logger.exception("Failed to add component '%s'", component_id)
            self._notify("error", f"Failed to add component: {exc}", duration=5000)
        else:
            entry = self._component_entries[component_id]
            self._notify("success", f"Added component: {entry.title}", duration=3000)
            return instance_id
        return None

    def _on_palette_clicked(self, item):
        component_id = item.get("component_id") if isinstance(item, dict) else None
        if component_id is not None:
            self._add_from_ui(component_id)

    def _on_component_dropped(self, payload):
        if payload.get("drop_type") == _FILES_DROP_TYPE:
            self._on_files_dropped(payload.get("data") or [])
            return
        # panel-reactflow handles frontend messages inside `hold()`. On Panel
        # 1.10 a held patch that adds node views blanks the views already on
        # the canvas, so apply the drop on the next tick like a button click.
        pn.state.execute(partial(self._add_dropped, payload), schedule=True)

    def _download_callback(self):
        data = self.export_dashboard()
        extension = self._download_format
        self._download_button.filename = export_filename(data["title"], extension)
        if extension == "yaml":
            from panel_flowdash.agent.yaml_format import editor_restricted_edges

            return io.StringIO(export_text(data, "yaml", editor_restricted_edges(self)))
        return io.StringIO(export_text(data))

    def _on_files_dropped(self, files: list[dict]):
        file = next(
            (f for f in files if f.get("name", "").lower().endswith(DASHBOARD_FILE_EXTENSIONS)),
            None,
        )
        if file is None:
            self._notify("warning", "Drop a downloaded dashboard .json or .yaml file to load it.")
            return
        try:
            model = self._imported_model(file.get("content", ""))
        except ValueError as exc:
            self._notify("error", f"Could not load {file['name']}: {exc}", duration=5000)
            return
        if not self._tile_items:
            self._apply_import(model)
            return
        self._pending_import = model
        self._import_message.object = (
            f"Replace the canvas with the {len(model.items)} components in "
            f"**{escape_markdown(file['name'])}**? Unsaved work will be lost."
        )
        self._import_dialog.open = True

    def _apply_import(self, model: DashboardModel):
        # Scheduled for the same reason as palette drops (see `_on_component_dropped`).
        pn.state.execute(partial(self._import_from_ui, model), schedule=True)

    async def _import_from_ui(self, model: DashboardModel):
        from panel_flowdash.agent.yaml_format import dashboard_to_dict

        before = dashboard_to_dict(self)
        present = {item["instance_id"] for item in self._tile_items}
        try:
            with self._quiet_history():
                await self.load_model_async(model)
        except Exception as exc:
            logger.exception("Failed to load dropped dashboard")
            self._notify("error", f"Could not load the dashboard: {exc}", duration=5000)
            return
        self._record_import(model, before, present)
        if not self._placeholders() and not self._unresolved_edges:
            self._notify("success", f"Loaded {len(model.items)} components.", duration=3000)

    def _add_dropped(self, payload):
        """Add a component dropped from the palette, wiring it to the port it landed on."""
        item = {"items": self._palette.items}
        try:
            for index in payload["data"]["path"]:
                item = item["items"][index]
        except (KeyError, IndexError, TypeError):
            return
        component_id = item.get("component_id")
        if component_id is None:
            return
        target = payload.get("target") or {}
        wire = target.get("direction") in ("input", "output") and target.get("handle_id")
        position = payload["position"]
        if wire:
            anchor = next(n for n in self._flow.nodes if n["id"] == target["node_id"])["position"]
            offset = -_DROP_WIRE_OFFSET if target["direction"] == "input" else _DROP_WIRE_OFFSET
            position = {"x": anchor["x"] + offset, "y": anchor["y"]}
        instance_id = self._add_from_ui(component_id, position=position)
        if instance_id is not None and wire:
            self._wire_dropped(instance_id, component_id, target)

    def _wire_dropped(self, instance_id, component_id, target):
        """Connect a dropped node to the port it was dropped on, via its first fitting port."""
        spec = self.component_specs[component_id]
        node_id, handle_id = target["node_id"], target["handle_id"]
        if target["direction"] == "input":
            candidates = [(instance_id, port.name, node_id, handle_id) for port in spec.outputs]
        else:
            candidates = [(node_id, handle_id, instance_id, port.name) for port in spec.inputs]
        reasons = []
        for candidate in candidates:
            reason = self._validate_connection(*candidate)
            if reason is None:
                result = self.connect(*candidate)
                if result is True:
                    return
                reason = result
            reasons.append(reason)
        title = self._component_entries[component_id].title
        detail = f": {reasons[0]}" if reasons else ""
        self._notify("warning", f"Added '{title}' but could not connect it{detail}", duration=5000)

    def _on_save_clicked(self) -> bool:
        try:
            model = self.save()
        except DashboardConflictError as exc:
            logger.info("Store refused to save dashboard: %s", exc)
            self._notify("error", str(exc), duration=5000)
            self._show_save_conflict()
        except DashboardStoreError as exc:
            logger.info("Store refused to save dashboard: %s", exc)
            self._notify("error", str(exc), duration=5000)
        except Exception:
            logger.exception("Failed to save dashboard")
            self._notify(
                "error", "Could not save the dashboard. The error has been logged.", duration=5000
            )
        else:
            # The dashboard bar shows the save state; without it nothing else would.
            if not self._dashboard_bar.visible:
                self._notify("success", f'Dashboard "{model.title}" saved.', duration=3000)
            return True
        return False

    def _show_save_conflict(self):
        """Offer to reload the stored dashboard or save the canvas as a copy."""
        keep = pmui.Button(label="Keep editing", variant="text")
        reload = pmui.Button(label="Reload", color="danger", variant="outlined")
        copy = pmui.Button(label="Save as copy", color="primary")
        # Under threaded Panel two clicks can be dispatched concurrently, and a
        # second "Save as copy" must not create a second copy.
        answered = threading.Lock()

        def answer_with(handler):
            def answer(_event):
                if answered.acquire(blocking=False):
                    close_dialog()
                    handler()

            return answer

        keep.on_click(answer_with(lambda: None))
        reload.on_click(answer_with(self._reload_from_store))
        copy.on_click(answer_with(self._save_copy_from_ui))
        open_dialog(
            "Dashboard changed since you opened it",
            pn.Column(
                markdown_pane(
                    "Someone else saved this dashboard after you opened it, so saving "
                    "would overwrite their changes. **Reload** discards your changes "
                    "and shows the latest version. **Save as copy** keeps your "
                    "changes in a new dashboard."
                ),
                pn.Row(pn.layout.HSpacer(), keep, reload, copy, sizing_mode="stretch_width"),
                sizing_mode="stretch_width",
            ),
        )

    def _dashboard_authorized(self, model: DashboardModel, access: str) -> bool:
        if self.authorize_dashboard is None:
            return True
        try:
            return bool(self.authorize_dashboard(model, access))
        except Exception:
            logger.exception(
                "authorize_dashboard failed for '%s'; denying %s access",
                model.dashboard_id,
                access,
            )
            return False

    def _load_stored_dashboard(self) -> DashboardModel | None:
        """Return the stored version of the loaded dashboard if this session may still view it.

        Notifies and returns None when it was deleted or is no longer shared
        with this session.
        """
        current = self.dashboard
        model = None
        if self.store is not None and current is not None:
            model = self.store._load_any(current.dashboard_id)
        if model is None:
            self._notify("error", "The dashboard no longer exists.", duration=5000)
            return None
        if not self._dashboard_authorized(model, "view"):
            self._notify("error", "You no longer have access to this dashboard.", duration=5000)
            return None
        self.read_only = self.read_only or not self._dashboard_authorized(model, "edit")
        return model

    def _reload_from_store(self):
        model = self._load_stored_dashboard()
        if model is None:
            return
        self.load_model(model)
        self._notify("info", f'Reloaded the latest version of "{model.title}".', duration=3000)

    def _copy_title(self) -> str:
        base = f"{self.dashboard.title if self.dashboard else 'Untitled'} (copy"
        title, n = f"{base})", 2
        while self.store is not None and self.store.title_exists(self.user, title):
            title, n = f"{base} {n})", n + 1
        return title

    def _save_copy_from_ui(self):
        try:
            model = self.save_as(self._copy_title())
        except DashboardStoreError as exc:
            logger.info("Store refused to save a copy of the dashboard: %s", exc)
            self._notify("error", str(exc), duration=5000)
        except Exception:
            logger.exception("Failed to save a copy of the dashboard")
            self._notify(
                "error", "Could not save a copy. The error has been logged.", duration=5000
            )
        else:
            self._notify("success", f'Saved your changes as "{model.title}".', duration=3000)

    def __panel__(self):
        """Render the editor."""
        return self._view
