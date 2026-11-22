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
import html
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
from panel.layout.base import ListLike
from panel.layout.spacer import Spacer
from panel.pane.base import ReplacementPane
from panel.pane.markup import HTMLBasePane
from panel.viewable import Children, Layoutable, Viewer
from panel_agents import RunState
from panel_tiles import TileGrid

from panel_flowdash import auto_layout
from panel_flowdash.auth import Permission
from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_spec import (
    _flowdash_metadata,
    build_component_specs,
    config_json_schema,
    config_value,
)
from panel_flowdash.config_choices import spec_fields
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
    connection_type,
    failure_text,
)
from panel_flowdash.failure_overlay import FailureMessage, FailureOverlay, with_failure_overlay
from panel_flowdash.global_filters import (
    check_filters,
    map_filters,
    without_nodes as without_filtered_nodes,
)
from panel_flowdash.groups import (
    GROUP_HEADER_HEIGHT,
    GROUP_PORT_ROW_HEIGHT,
    MAX_LABEL_LENGTH,
    DashboardGroup,
    check_group_id,
    copy_groups,
    group_class,
    group_id_of,
    group_node_data,
    group_node_id,
    group_ports,
    is_group_node,
    unique_group_id,
)
from panel_flowdash.history import BURST_SECONDS, HISTORY_MAX_BYTES, EditHistory, label_for
from panel_flowdash.input_literals import check_literal, stored_literal
from panel_flowdash.parameters import (
    ContractError,
    ContractProblem,
    DashboardParameter,
    ParameterError,
    bind_document,
    check_contracts,
    check_name,
    compute_expects,
    failures,
    field_path,
    get_field,
    parameters_schema,
    suggest_name,
)
from panel_flowdash.placeholders import (
    PLACEHOLDER_NODE_TYPE,
    Placeholder,
    UnresolvedEdge,
    broken_edges_panel,
    placeholder_view,
    unresolved_summary,
)
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.report import (
    check_report,
    insert_block,
    map_report,
    report_blocks,
    report_turns,
    without_nodes,
)
from panel_flowdash.selection import release_links
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.transform_editor import TransformEditor
from panel_flowdash.transforms import parse_transform, summarize_transform
from panel_flowdash.turns import (
    DashboardTurn,
    TurnLog,
    _agent_source,
    agent_edits,
    change_labels,
    changes_node,
    strip_questions,
    summary_cut_short,
)
from panel_flowdash.ui import tile_layout
from panel_flowdash.ui.code_panel import HEADER_HEIGHT, CodePanel
from panel_flowdash.ui.controls_bar import ControlsBar
from panel_flowdash.ui.dashboard_bar import HIDE_ON_PHONE, PHONE_BREAKPOINT, DashboardBar
from panel_flowdash.ui.dock_handle import DockHandle
from panel_flowdash.ui.fullscreen import (
    PRESENTATION_CSS,
    PRESENTING_CLASS,
    TOGGLE_CLASS,
    FullscreenController,
    PresentationOverlay,
)
from panel_flowdash.ui.global_filters import GlobalFilters
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
from panel_flowdash.ui.inspector import DATA, HISTORY, SPEC, TILE, Inspector
from panel_flowdash.ui.layout_panel import GRID_EDIT_CSS, LayoutPanel
from panel_flowdash.ui.node_card import (
    CARD_WIDTH,
    CardBridge,
    NodeCard,
    card_height,
    node_card,
    node_preview_value,
    placeholder_card,
    port_label,
    port_tops,
)
from panel_flowdash.ui.node_preview import (
    CHART_COLOR,
    PREVIEW_KIND_KEY,
    _is_table,
    run_off_loop,
    sanitize_svg,
    summarize_value,
)
from panel_flowdash.ui.palette import (
    ComponentPalette,
    editor_presets,
    palette_entry,
    palette_items,
    preset_entries,
)
from panel_flowdash.ui.provenance import provenance_objects
from panel_flowdash.ui.report_view import ReportView
from panel_flowdash.ui.roles import component_role
from panel_flowdash.ui.settings_dialog import SettingsDialog
from panel_flowdash.ui.sheet import (
    ABOVE_SHEET_CLASS,
    BEHIND_SHEET_CLASS,
    INSPECTOR_SHEET_HEIGHT,
    INSPECTOR_SHEET_TALL_HEIGHT,
    SheetLabel,
    bottom_sheet,
    inspector_sheet,
    sheet_button,
)
from panel_flowdash.ui.sidecar import SidecarFold
from panel_flowdash.ui.split_view import SplitView
from panel_flowdash.ui.states import StartCard
from panel_flowdash.ui.status_bar import StatusBar, select_on_canvas
from panel_flowdash.ui.text import (
    escape_markdown,
    markdown_pane,
    text_alert,
    text_typography,
)
from panel_flowdash.ui.tile import TILE_KEY_PREFIX, Tile, resolve_framed
from panel_flowdash.ui.tile_bands import band_labels, base_band, reference_width
from panel_flowdash.ui.tile_drag import TileDrag
from panel_flowdash.ui.transform_offer import TransformOffer, bridge_kind
from panel_flowdash.util import (
    close_dialog,
    escape_closes,
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
from panel_flowdash.value_text import (
    error_text,
    held_table_names,
    is_plain,
    object_label,
    safe_frame,
    safe_repr,
)
from panel_flowdash.widgets import BUILTIN_COMPONENTS, TEXT_COMPONENTS

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

PlacementReason = t.Literal["new", "load", "restore"]


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
# Set on the tile grid in the Split tab, which shows the dashboard at the pane's width.
_SPLIT_PREVIEW = "fd-split-preview"


# TileGrid aligns its layout entries with its objects by position. The editor
# stores and persists them keyed by the tile's instance id instead, so removing
# a tile, or skipping one that no longer loads, cannot hand its slot to the next.

# Keys of a persisted layout entry the editor applies itself rather than TileGrid,
# whose layout the browser rewrites with only the keys it knows.
_TILE_OPTIONS = ("framed", "area")


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


def _display_rank(entries: dict[str, dict], item: dict, position: int) -> tuple:
    """Order a placed item by its layout index, then those without one by placement."""
    index = entries.get(item["instance_id"], {}).get("index")
    return (index is None, index or 0, position)


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
        entry = {
            k: v for k, v in (spec or {}).items() if k != "instance_id" and k not in _TILE_OPTIONS
        }
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


# The height a tile whose view takes its height from the tile starts at.
FILL_HEIGHT = 360

_HEIGHT_SIZING = ("stretch_both", "stretch_height", "scale_both", "scale_height")


def _fills_tile(view: t.Any, lazy: list | None = None) -> bool:
    """Whether *view* takes its height from its tile rather than giving the tile one.

    The grid sizes a tile without a height to its content, so such a view, e.g.
    a Vega-Lite chart sized to its container, would leave the tile a header
    tall and draw nothing. A layout fills its tile if one of its views does,
    and a pane that renders another object, e.g. a ``ParamMethod``, if what it
    rendered does. Those panes are appended to *lazy*, since what they render
    can change. Text and spacers have a height of their own.
    """
    if not isinstance(view, Layoutable) or view.height is not None or view.min_height is not None:
        return False
    if isinstance(view, ReplacementPane):
        if lazy is not None:
            lazy.append(view)
        return any(_fills_tile(child, lazy) for child in view._inner_layout)
    if isinstance(view, ListLike):
        return any(_fills_tile(child, lazy) for child in view)
    if isinstance(view, (HTMLBasePane, Spacer)):
        return False
    if view.sizing_mode in _HEIGHT_SIZING:
        return True
    if isinstance(view, pn.pane.Vega):
        spec = view.object
        height = spec.get("height") if isinstance(spec, dict) else getattr(spec, "height", None)
        return height == "container"
    return False


def _size_layout(
    layout: list[dict], specs: list, reference_width: int, fills: t.Sequence[bool] = ()
) -> list[dict]:
    """Apply the size hints of the tiles' *specs* to a positional TileGrid layout.

    A tile without an entry starts at its ``default_size``, and every tile's size
    is kept within its ``min_size`` and ``max_size``. Hints are pixels; layout
    widths are percentages of *reference_width*, heights are pixels. A tile
    whose view *fills* it and that has no height yet starts at :data:`FILL_HEIGHT`.
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
        if "height" not in entry and i < len(fills) and fills[i]:
            entry["height"] = FILL_HEIGHT
        if spec is not None:
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
# The lowest zoom of the canvas, low enough to fit a dashboard the agent built
# into a canvas narrowed by the palette, the inspector and the agent's drawer.
_CANVAS_MIN_ZOOM = 0.2
# Horizontal gap, in flow units, between a node dropped on a port and the node it wires to.
_DROP_WIRE_OFFSET = 360
# The canvas accepts only this drag type, so drags from unrelated lists are ignored.
_PALETTE_DRAG_TYPE = "application/x-flowdash-component"
# The DataTransfer type of files dragged in from the OS.
_FILES_DROP_TYPE = "Files"
# Seconds the drop of a tile released beside the grid is waited for; the page sends
# it at most about two seconds after the release, once the grid's drag has ended.
_TILE_DROP_WAIT = 3.0
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
# A node added on a phone is shown at least this large, this far below the canvas's top.
_PHONE_REVEAL_ZOOM = 0.75
_PHONE_REVEAL_TOP = 24

# How long after the canvas moves to another tab an empty selection from its page is
# taken as the old view letting go of it rather than the person clearing it.
_CANVAS_REMOUNT_GRACE = 3.0
_CANVAS_MIN_HEIGHT = 600
_CANVAS_MIN_WIDTH = 160
# With the code panel under it, the canvas keeps this much and the panel starts this high.
_CANVAS_MIN_HEIGHT_DOCKED = 160
_CODE_PANEL_HEIGHT = 400
# Narrower windows show the code panel in the sidecar's Spec tab rather than under the canvas.
_CODE_PANEL_MIN_WIDTH = 960


def _has_selection(selection: dict | None) -> bool:
    selection = selection or {}
    return bool(selection.get("nodes") or selection.get("edges"))


# Touch targets on a phone.
_FLOW_PHONE_CSS = (
    f"@media (max-width: {PHONE_BREAKPOINT - 0.05}px) {{ "
    ".react-flow__controls-button { width: 44px; height: 44px; } }"
)

_FLOW_STYLESHEET = """\
/* The canvas shares its height with the code panel docked under it. */
:host(.fd-docked) .react-flow { min-height: 0 !important; height: 100% !important; }
/* React Flow's own dark palette draws edges at #3e3e3e on #141414, close to invisible. */
.react-flow.dark {
  --xy-edge-stroke: var(--mui-palette-text-disabled, #8a8a8a);
  --xy-edge-stroke-selected: var(--mui-palette-text-secondary, #b1b1b1);
  --xy-connectionline-stroke: var(--mui-palette-text-secondary, #b1b1b1);
}
.react-flow.dark .react-flow__attribution {
  background: var(--mui-palette-background-paper, var(--panel-background-color));
  border-top-left-radius: 4px;
}
.react-flow.dark .react-flow__attribution a {
  color: var(--mui-palette-text-secondary, #999);
}
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
/* Selection is an offset dashed ring, so it reads apart from the solid agent and failure
   outlines that sit on the border and can show at the same time. React Flow removes the
   outline of a focused node, which a clicked node is, so this needs its specificity. */
.react-flow__node.selected,
.react-flow__node.selectable.selected:is(:focus, :focus-visible) {
  outline: 2px dashed var(--mui-palette-primary-main, var(--panel-primary-color));
  outline-offset: 3px;
}
/* A node reached with the keyboard but not selected gets a quieter solid ring. */
.react-flow__node.selectable:focus-visible:not(.selected) {
  outline: 2px solid var(--mui-palette-text-secondary, currentColor);
  outline-offset: 3px;
}
.react-flow.dark .react-flow__node.selected,
.react-flow.dark .react-flow__node.selectable.selected:is(:focus, :focus-visible) {
  outline-color: var(--mui-palette-primary-light, var(--mui-palette-primary-main));
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
  border: 2px solid var(--fd-hc, var(--mui-palette-primary-main, var(--panel-primary-color)));
  background: var(--mui-palette-background-paper, var(--panel-background-color));
}
.react-flow.dark .react-flow__handle {
  border-color: var(--fd-hc-dark, var(--fd-hc, var(--mui-palette-primary-main, var(--panel-primary-color))));
}
.react-flow__handle-right {
  background: var(--fd-hc, var(--mui-palette-primary-main, var(--panel-primary-color)));
}
.react-flow.dark .react-flow__handle-right {
  background: var(--fd-hc-dark, var(--fd-hc, var(--mui-palette-primary-main, var(--panel-primary-color))));
}
.react-flow__handle.fd-handle-optional {
  border-style: dashed;
}
.react-flow__handle.fd-handle-missing {
  border-color: var(--mui-palette-error-main, #d32f2f) !important;
  box-shadow: 0 0 0 3px rgba(211, 47, 47, 0.22);
}
.react-flow.dark .react-flow__handle.fd-handle-missing {
  border-color: var(--mui-palette-error-main, #ef5350) !important;
  box-shadow: 0 0 0 3px rgba(239, 83, 80, 0.28);
}
.react-flow__handle.fd-handle-hidden {
  opacity: 0;
  pointer-events: none;
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
}/* A collapsed group shows each port's label and type beside its handle, as
   node cards do, rather than only in the handle's tooltip. */
.rf-group--collapsed {
  min-width: 260px;
}
.fd-group-rows-1 > .rf-group--collapsed { min-height: 66px !important; }
.fd-group-rows-2 > .rf-group--collapsed { min-height: 88px !important; }
.fd-group-rows-3 > .rf-group--collapsed { min-height: 110px !important; }
.fd-group-rows-4 > .rf-group--collapsed { min-height: 132px !important; }
.fd-group-rows-5 > .rf-group--collapsed { min-height: 154px !important; }
.fd-group-rows-6 > .rf-group--collapsed { min-height: 176px !important; }
.fd-group-rows-7 > .rf-group--collapsed { min-height: 198px !important; }
.fd-group-rows-8 > .rf-group--collapsed { min-height: 220px !important; }
.fd-group-rows-9 > .rf-group--collapsed { min-height: 242px !important; }
.fd-group-rows-10 > .rf-group--collapsed { min-height: 264px !important; }
.fd-group-rows-11 > .rf-group--collapsed { min-height: 286px !important; }
.fd-group-rows-12 > .rf-group--collapsed { min-height: 308px !important; }
.rf-group--collapsed > .react-flow__handle[data-tooltip]::after {
  opacity: 1;
  background: none;
  padding: 0;
  color: var(--mui-palette-text-secondary, var(--panel-on-background-color));
  font-size: 12px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rf-group--collapsed > .react-flow__handle-left[data-tooltip]::after {
  left: calc(100% + 6px);
  right: auto;
}
.rf-group--collapsed > .react-flow__handle-right[data-tooltip]::after {
  right: calc(100% + 6px);
  left: auto;
}
"""

# A transformed edge is dashed in the accent color, so it reads as derived at a glance.
_TRANSFORMED_EDGE_STYLE = {
    "stroke": "var(--panel-primary-color, #0072b5)",
    "strokeDasharray": "6 3",
    "strokeWidth": 2,
}
# A failed edge or node is red, over any transform styling, so a failure is the first thing seen.
_FAILED_EDGE_STYLE = {"stroke": "var(--panel-danger-color, #d32f2f)", "strokeWidth": 2}
# A narrow window still gets a pop-out wide enough to read a chart or a few columns.
_POPOUT_MIN_WIDTH = 560

# A value only its repr describes, as compact as the Data tab's captions.
_REPR_CSS = """
pre {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  font-size: 11.5px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--mui-palette-text-secondary, inherit);
}
"""

# Tabulator's sort arrow sits over the title unless the header leaves room for it.
_POPOUT_TABLE_CSS = """
.tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-title-holder {
  display: flex;
  align-items: center;
  gap: 4px;
}
.tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-sorter {
  position: static !important;
  margin-left: auto;
}
.tabulator .tabulator-tableholder {
  overflow: auto !important;
  scrollbar-width: thin;
  scrollbar-gutter: stable;
}
"""

_FAILED_NODE_STYLE = {
    "borderColor": "var(--panel-danger-color, #d32f2f)",
    "boxShadow": "0 0 0 1px var(--panel-danger-color, #d32f2f)",
}


# Stands in for the component of an exported item the exporting user may not
# use. Importing it back restores the stored item, or drops it.
RESTRICTED_COMPONENT_ID = "flowdash:restricted"

# What an editor can show: the canvas or the tile grid, the Explore view or the Report view.
SURFACES = ("workspace", "explore", "report")

# Space between a group's nodes and its frame, in px.
_GROUP_PADDING = 24
# The width of a collapsed group's card, as the canvas stylesheet sets it.
_COLLAPSED_GROUP_WIDTH = 260

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
        data["parameters"] = {
            name: parameter
            for name, parameter in data["parameters"].items()
            if parameter["node"] not in restricted
        }
        data["items"] = [
            {
                **{k: v for k, v in item.items() if k != "inputs"},
                "component_id": RESTRICTED_COMPONENT_ID,
                "config": {},
            }
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
# The node whose component is being created, for `current_node_id`.
_INSTANTIATING_NODE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "panel_flowdash_instantiating_node", default=None
)
# Why the component being created is placed, for `placement_context`.
_PLACEMENT: contextvars.ContextVar[PlacementReason | None] = contextvars.ContextVar(
    "panel_flowdash_placement", default=None
)
# Why the editor operation in progress places nodes; unset inside a component's creation.
_PLACING: contextvars.ContextVar[PlacementReason | None] = contextvars.ContextVar(
    "panel_flowdash_placing", default=None
)
# The latest editor of each served session; weak on both sides so a session can end.
_SESSION_EDITORS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _apply_each(instance: param.Parameterized, values: dict[str, t.Any]):
    """Set *values* on *instance* one by one after a joint update failed, yielding each error.

    The joint update stops dispatching at the first error, and may already
    have set the values that come before it, so setting those again would not
    notify their watchers. Those are triggered instead.
    """
    for name, value in values.items():
        try:
            if getattr(instance, name) is value:
                instance.param.trigger(name)
            else:
                setattr(instance, name, value)
        except Exception as exc:
            yield name, exc


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


def current_node_id() -> str | None:
    """Return the id of the node whose component is being created, or None.

    Call it from a component's ``__init__`` or function body, e.g. to stamp
    the values the component outputs with the node they come from. Later,
    use :meth:`FlowDash.node_id` with the component instead.
    """
    return _INSTANTIATING_NODE.get()


def _unset(config_state: param.Parameterized, key: str) -> bool:
    """Whether the config field *key* still holds its declared default."""
    value, default = getattr(config_state, key), config_state.param[key].default
    if value is default:
        return True
    try:
        return bool(value == default)
    except Exception:
        return False


def _next_tick(callback: t.Callable[[], t.Any]):
    """Call *callback* once the session's current event is handled; outside a session, never."""
    doc = pn.state.curdoc
    if doc is not None and doc.session_context is not None:
        doc.add_next_tick_callback(callback)


def placement_context() -> PlacementReason | None:
    """Return why the component calling this is being created, or None.

    Call it from a component's ``__init__`` or function body:

    - ``'new'``: someone added the node, from the palette, ``add_component``
      or the agent, so the component may fill in defaults of its own.
    - ``'load'``: a dashboard is being opened, imported or replaced, and its
      saved configuration is what the component should show.
    - ``'restore'``: undo, redo or restoring a version brings the node back as
      it was.

    Outside the creation of a FlowDash node it returns ``None``, as it does
    from a thread the component starts, so read it before handing work off.
    """
    return _PLACEMENT.get()


@contextmanager
def _placing(reason: PlacementReason):
    """Create the nodes placed inside the block for *reason*, unless an enclosing block set one."""
    if _PLACING.get() is not None:
        yield
        return
    token = _PLACING.set(reason)
    try:
        yield
    finally:
        _PLACING.reset(token)


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
def _creating(
    editor_ref: weakref.ref[FlowDash],
    instance_id: str,
    wiring: list,
    reason: PlacementReason | None = None,
):
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
    node_token = _INSTANTIATING_NODE.set(instance_id)
    reason_token = _PLACEMENT.set(reason)
    placing_token = _PLACING.set(None)
    try:
        yield
    finally:
        _PLACING.reset(placing_token)
        _PLACEMENT.reset(reason_token)
        _INSTANTIATING_NODE.reset(node_token)
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


class _EditorCall:
    """Call a method of an editor without keeping the editor alive.

    Cards and other views that live as long as the node state hold these
    instead of bound methods, so a closed session's editor can be freed.
    """

    __slots__ = ("_args", "_method", "_ref")

    def __init__(self, editor: FlowDash, method: str, *args: t.Any):
        self._ref = weakref.ref(editor)
        self._method = method
        self._args = args

    def __call__(self, *args: t.Any) -> t.Any:
        editor = self._ref()
        return None if editor is None else getattr(editor, self._method)(*self._args, *args)

    def __repr__(self) -> str:
        return f"_EditorCall({self._method!r})"


def _plain_integer_formatters(df) -> dict[str, dict[str, str]]:
    """Show integer columns below 10,000 without thousands separators, as cards do (years, ids)."""
    formatters = {}
    for column in df.columns:
        values = df[column]
        if getattr(values.dtype, "kind", None) in "iu" and len(values):
            if values.abs().max() < 10_000:
                formatters[str(column)] = {"type": "plaintext"}
    return formatters


def _popout_chart(payload: t.Mapping) -> Viewable:
    """Render a chart preview payload in full, from its Vega-Lite spec and rows."""
    raw_spec = payload.get("spec")
    if isinstance(raw_spec, t.Mapping):
        spec = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json"}
        spec.update(copy.deepcopy(dict(raw_spec)))
        data = payload.get("data")
        if data is not None and _is_table(data):
            spec["data"] = {"values": data.head(500).to_dict(orient="records")}
        elif isinstance(data, list):
            spec["data"] = {"values": data[:500]}
        # Without these Vega draws at its 200px step default, not across the dialog.
        spec.setdefault("width", "container")
        spec.setdefault("height", 300)
        spec.setdefault("autosize", {"type": "fit", "contains": "padding"})
        config = spec.setdefault("config", {})
        if isinstance(config, dict):
            config.setdefault("mark", {}).setdefault("color", CHART_COLOR)
        return pn.pane.Vega(spec, sizing_mode="stretch_width", height=340, margin=0)
    if payload.get("svg"):
        return pn.pane.HTML(
            sanitize_svg(str(payload["svg"])), sizing_mode="stretch_width", height=320, margin=0
        )
    return markdown_pane("*No chart data*", margin=0)


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

    code_drafts = param.Boolean(
        default=False,
        doc="""
        Whether the code panel holds edits that have not been run. Managed by
        the editor; like ``dirty``, watch it to prompt before discarding work.""",
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

    history_max_bytes = param.Integer(
        default=HISTORY_MAX_BYTES,
        bounds=(0, None),
        allow_None=True,
        doc="""
        About how many bytes of dashboard YAML a dashboard's stored history
        keeps; the oldest versions are dropped past it, the latest is always
        kept. None keeps versions however large they are.""",
    )

    mode = param.Selector(
        default="wiring",
        objects=["wiring", "dashboard", "split"],
        doc="""
        Which workspace is shown: 'wiring' for the ReactFlow canvas, 'dashboard'
        for the tile grid and 'split' for both side by side, with the canvas
        on the left. Only meaningful while `editable`.""",
    )

    surface = param.Selector(
        default="workspace",
        objects=list(SURFACES),
        doc="""
        What the editor shows, in view and in edit mode: 'workspace' for the
        canvas or the tile grid (as `mode` and `editable` say), 'explore' for
        the Explore view, the dashboard's turns as a feed with the outputs
        each made (see :mod:`panel_flowdash.explore`), or 'report' for the
        Report view, the dashboard read as a document (see
        :mod:`panel_flowdash.report`). The views of the tiles move between
        them, so only one is ever on screen.""",
    )

    explore = param.Boolean(
        default=False,
        doc="""
        Whether the Explore view is shown: the same as ``surface == 'explore'``.
        Setting it to False returns to the workspace.""",
    )

    edited = param.Event(doc="Triggered after each edit is recorded in its turn.")

    code_panel_node = param.String(
        default=None,
        allow_None=True,
        constant=True,
        doc="The id of the node the code panel under the canvas shows, or None while it is closed.",
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

    provenance = param.Boolean(
        default=True,
        doc="""
        Whether a node an agent's request created offers "How was this
        made?", in its tile's More menu and in the inspector, opening a side
        sheet with that request: its question, model calls, tool calls and
        the versions it published, from the run the node's origin turn
        names. People who may not edit the dashboard see the request's
        shape without its content.""",
    )

    agent_state = param.ClassSelector(
        class_=RunState,
        default=None,
        allow_None=True,
        doc="""
        The panel-agents ``RunState`` holding the agent's runs, which
        "How was this made?" shows. None uses the edit history's, which an
        assistant shares when history is on.""",
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
        # Outlines other code draws on nodes, e.g. the assistant's changes.
        self._node_outlines: dict[str, dict[str, str]] = {}
        self._popout_request: object | None = None
        self._tile_items: list[dict] = []
        self._tile_objects: list[Viewable] = []
        # Saved config values a node's config refused on load, by node, kept to tell the person.
        self._rejected_config: dict[str, dict[str, t.Any]] = {}
        # Saved edges that could not be wired, kept to be saved unchanged, and the
        # notice about them waiting for an editor to see it.
        self._unresolved_edges: list[UnresolvedEdge] = []
        self._unresolved_notice: str | None = None
        # Layout entries keyed by instance id, waiting for the grid to be rendered.
        self._pending_tile_layout: list[dict] = []
        # Instance ids of the tiles currently in the grid, in grid order; None
        # marks the slot of a tile removed while the grid was not rebuilt.
        self._grid_ids: list[str | None] = []
        # Each tile's own frame option, by instance id, kept beside the grid's layout.
        self._tile_frames: dict[str, bool] = {}
        # The area saved with each tile on the grid that has one, kept beside the
        # grid's layout like the frames: "grid" for a component that would go
        # beside it, or where a placeholder goes for those who can load it.
        self._grid_areas: dict[str, str] = {}
        # The area and layout entry of each component shown beside the grid, by instance id.
        self._area_entries: dict[str, tuple[str, dict]] = {}
        # The grid's layout while it waits for the browser to drop removed tiles.
        self._deferred_grid_layout: list[dict] | None = None
        # Heights the grid shows for tiles whose view fills them, until someone resizes them.
        self._fill_heights: dict[str, float] = {}
        self._fill_watched: weakref.WeakSet = weakref.WeakSet()
        # The views shown beside the grid, by area, as (instance id, view) in order.
        self._area_views: dict[str, list] = {}
        # The framed tiles of the grid, by instance id, kept so a grid rebuild
        # does not re-render them.
        self._inspect_tiles: dict[str, Tile] = {}
        # Each tile carries a unique key as a CSS class, so it can be found on the page.
        self._tile_keys = itertools.count()
        # The tab Split returns to when it is toggled off.
        self._mode_before_split = "wiring"
        self._split_shown = False
        self._phone = False
        self._phone_tile_tapped = False
        # Where the canvas is shown ('wiring', 'split' or None when hidden); moving it
        # renders it afresh, and the page then reports an empty selection.
        self._canvas_host: str | None = None
        self._remount_deadline = 0.0
        self._tile_selection_follows = True
        self._rebuilding_grid = False
        self._inspected_id: str | None = None
        self._span_view = None
        self._how_made_shown: tuple[str, str | None] | None = None
        self._how_made_caption: str | None = None
        self._own_runs: set[str] = set()
        # Responsive grid settings waiting to be applied; empty values must also be
        # applied so a loaded dashboard doesn't inherit the previous one's overrides.
        self._pending_grid_config: dict | None = None
        self._inspection_key: tuple[str, str] | None = None

        # The turns of the dashboard and the turn each item was created in.
        self._turn_log = TurnLog()
        self._origins: dict[str, str] = {}
        self._explore_feed = None
        self._report_view = None
        # The dashboard's parameters by name, each naming its node by instance id.
        self._parameters: dict[str, DashboardParameter] = {}
        # The component each parameter's node had when it was declared or loaded.
        self._parameter_components: dict[str, str] = {}
        # Nodes framed together on the canvas, by group id.
        self._groups: dict[str, DashboardGroup] = {}
        # The dashboard read as a document, nodes by instance id, or None for
        # the report derived from the layout; see panel_flowdash.report.
        self._report: dict[str, t.Any] | None = None
        # The dashboard's global filters and the views they reach, see
        # panel_flowdash.global_filters.
        self._global_filters = GlobalFilters(self)
        # The nodes expanding a group pushed aside, by group id: their position
        # after the push and before it, so collapsing can move them back.
        self._pushed: dict[str, dict[str, tuple[tuple[float, float], tuple[float, float]]]] = {}
        # Nodes whose canvas position and tiles whose layout the editor chose
        # rather than a person, so the editor may move them again, see
        # `_settle_nodes` and `_place_new_tiles`.
        self._floating_nodes: set[str] = set()
        self._auto_tiles: set[str] = set()
        # The tiles released over the top bar or the sidebar and not dropped there
        # yet, oldest first: when, and whether the grid has since sent the layout
        # the drag ended with, which the move's version then holds.
        self._tile_releases: dict[str, dict[str, t.Any]] = {}
        # Set when the editor moved nodes, so the canvas fits them once the agent's request ends.
        self._fit_pending = False
        # While an agent request runs, the tiles it adds wait here to be laid out once it ends.
        self._deferred_tiles: set[str] | None = None
        self._deferred_rearrange = False
        # The requests deferring the layout, and the tiles each added, so each is laid out as it ends.
        self._deferring: set[str | None] = set()
        self._deferred_by_run: dict[str | None, set[str]] = {}
        # Tiles shown or hidden on purpose, which the editor never hides as plumbing.
        self._chosen_visibility: set[str] = set()
        # Set when deferred tiles were laid out, for the agent session to publish the result.
        self._layout_released = False
        self._hydrating = False
        self._recording_edit = False
        # Counts recorded edits, so a default knows whether an edit took it in.
        self._edit_count = 0
        # Defaults set while an edit rewired an input, by node, keys and `_edit_count` then.
        self._pending_defaults: list[tuple[str, list[str], int]] = []
        # Defaults set off the session's event loop, applied on it, see `set_default`.
        self._queued_defaults: list[tuple[str, dict]] = []
        self._queued_lock = threading.Lock()
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
        self._session_doc = (
            weakref.ref(pn.state.curdoc)
            if pn.state.curdoc is not None and pn.state.curdoc.session_context is not None
            else None
        )

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
        self._sync_min_width()

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

        Selection links made for it with ``link_selection`` are unlinked.

        The watchers and output references wiring the component to its node
        are released first, so nothing flows into or out of it any more.
        Popping the instance first makes this a no-op on any later call, so a
        component is disposed at most once however it leaves the editor.
        """
        self._release_wiring(instance_id)
        panel = getattr(self, "_code_panel", None)
        if panel is not None:
            if panel.instance_id == instance_id:
                self.close_code_panel()
            panel.drop_drafts(instance_id)
        instance = self._component_instances.pop(instance_id, None)
        if instance is not None:
            release_links(instance)
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
                errors.append(f"{component_id}: {error_text(exc)}")
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
            f"Runtime wiring error ({source_port} → {target_port}): {error_text(exc)}",
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Canvas construction
    # ------------------------------------------------------------------

    def _load_component_extensions(self):
        """Load the Panel extensions every offered component declares, and the inspector's.

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
            # The inspector edits code fields in Panel's CodeEditor and card pop-outs use Tabulator.
            | {"codeeditor", "tabulator"}
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
            base = palette_entry(entry, spec, placed=app_id in placed)
            entries.append(base)
            entries.extend(preset_entries(base, editor_presets(self, entry)))
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
                schema=config_json_schema(spec.config_state_class),
                inputs=[
                    {
                        "id": port.name,
                        "label": port.label or port.name,
                        "type": port.type,
                        "top": f"var(--fd-port-in-{idx}, {round(top, 2)}px)",
                        **(
                            {"maxConnections": 1}
                            if port.multiple is False
                            or (port.multiple is None and (port.type or "").lower() != "list")
                            else {}
                        ),
                    }
                    for idx, (port, top) in enumerate(zip(spec.inputs, tops_in, strict=True))
                ],
                outputs=[
                    {
                        "id": port.name,
                        "label": port.label or port.name,
                        "type": port.type,
                        "top": f"var(--fd-port-out-{idx}, {round(top, 2)}px)",
                    }
                    for idx, (port, top) in enumerate(zip(spec.outputs, tops_out, strict=True))
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
            min_zoom=_CANVAS_MIN_ZOOM,
            sizing_mode="stretch_both",
            min_height=_CANVAS_MIN_HEIGHT,
            css_classes=[f"fd-canvas-{id(self)}"],
            stylesheets=[_FLOW_STYLESHEET, _FLOW_PHONE_CSS],
            popup_trigger=self.popup_trigger,
            popup_hover_delay=self.popup_hover_delay,
            drop_types=[_PALETTE_DRAG_TYPE, _FILES_DROP_TYPE],
            # Deleting a frame keeps its nodes, as grouping leaves the dataflow alone.
            group_delete="ungroup",
            connection_validation={
                "direction": True,
                "cycles": True,
                "duplicates": True,
                "capacity": True,
            },
        )
        flow.on("drop", self._on_component_dropped)
        process_msg = flow._process_msg
        dropped = _EditorCall(self, "_selection_dropped_by_remount")

        def _process_msg(msg: dict) -> None:
            if not dropped(msg):
                process_msg(msg)

        flow._process_msg = _process_msg
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
                self._sync_card_connections(tgt_id)
                self._record_edit(
                    self._connect_label("Connected", src_id, src_handle, tgt_id, tgt_handle),
                    op="connect",
                    args=self._edge_args(src_id, src_handle, tgt_id, tgt_handle),
                )
                self._notify("success", f"Wired: {src_handle} → {tgt_handle}", duration=3000)
            else:
                with self._muted_canvas():
                    flow.remove_edge(edge.get("id", ""))
                # Adding and removing the edge leaves `edges` as it was, so nothing
                # tells the browser to drop the edge it drew.
                flow._send_msg(
                    {"type": "edge_rejected", "edge_id": edge.get("id"), "reason": result}
                )
                bridge = result is not True and self._bridge(
                    src_id, src_handle, tgt_id, tgt_handle
                )
                if bridge:
                    self._transform_offer.offer((src_id, src_handle, tgt_id, tgt_handle), bridge)
                    return
                logger.warning("Edge rejected: %s", result)
                self._notify("error", result, duration=5000)

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
                self._sync_card_connections(mapping[2])
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
            if not node_id or not patch or group_id_of(node_id):
                return
            self._apply_config_patch(node_id, patch)

        def _on_node_deleted(event):
            if self._muted:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            if group_id := group_id_of(node_id):
                self._on_group_frame_deleted(group_id)
                return
            if node_id:
                self._forget_node(node_id)
                self._dispose_component(node_id)
                name = self._node_name(node_id)
                self._record_edit(f"Removed {name}", op="remove_node", args={"name": name})
                self._sync_placed()

        flow.param.watch(self._on_selection_changed, "selection")
        flow.on("edge_added", _on_edge_added)
        flow.on("edge_deleted", _on_edge_deleted)
        flow.on("connection_refused", self._on_connection_refused)
        flow.on("node_data_changed", _on_node_data_changed)
        flow.on("node_deleted", _on_node_deleted)
        flow.on("group_toggled", self._on_group_toggled)
        flow.on("nodes_ungrouped", self._on_nodes_ungrouped)
        flow.param.watch(self._sync_group_ports, "edges")
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
        """Return why a connection would be refused, or None, including restricted inputs.

        A connection without steps whose types only transform steps can
        bridge, e.g. a table to a number, is allowed, so dropping it offers
        those steps instead of refusing it.
        """
        reason = self._restricted_input_error(
            target_id, target_port
        ) or self._dataflow_graph.validate_connection(
            source_id, source_port, target_id, target_port, transform
        )
        if reason is not None and not transform:
            if self._bridge(source_id, source_port, target_id, target_port):
                return None
        return reason

    def _bridge(self, source_id, source_port, target_id, target_port) -> str | None:
        """Return the kind of steps that would let this connection through, or None.

        Only a connection that every rule but its types allows can be bridged;
        see :func:`~panel_flowdash.ui.transform_offer.bridge_kind`.
        """
        source = self._port_spec(source_id, source_port, output=True)
        target = self._port_spec(target_id, target_port, output=False)
        if source is None or target is None:
            return None
        kind = bridge_kind(source.type, connection_type(target))
        if kind is None:
            return None
        if self._restricted_input_error(target_id, target_port) is not None:
            return None
        if self._dataflow_graph.validate_connection(
            source_id, source_port, target_id, target_port, types=False
        ):
            return None
        return kind

    def _on_connection_refused(self, event):
        """Say why a connection dropped onto a port was refused, as the canvas draws nothing."""
        reason = event.get("reason") if isinstance(event, dict) else None
        self._notify(
            "warning", f"Not connected: {reason or 'the port refuses it.'}", duration=5000
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
            self._sync_card_bridge()

    def _forget_node(self, node_id: str):
        """Drop a node from the graph, the tile bookkeeping and the edge map.

        Its layout entries go too, so a node placed later under the same id
        starts with a fresh tile rather than the removed one's.
        """
        self._dataflow_graph.remove_node(node_id)
        self._node_outlines.pop(node_id, None)
        self._forget_layout(node_id)
        self._drop_from_groups(node_id)
        self._report = without_nodes(self._report, {node_id})
        self._global_filters.forget(node_id)
        if any(node_id in f.get("exclude", ()) for f in self._global_filters.filters):
            self._global_filters.filters = without_filtered_nodes(
                self._global_filters.filters, {node_id}
            )
        self._floating_nodes.discard(node_id)
        self._auto_tiles.discard(node_id)
        self._chosen_visibility.discard(node_id)
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
        affected_targets: set[str] = set()
        for edge_id, mapping in list(self._edge_id_map.items()):
            if node_id in (mapping[0], mapping[2]):
                del self._edge_id_map[edge_id]
                self._drop_edge_editor(edge_id)
                if mapping[2] != node_id:
                    affected_targets.add(mapping[2])
        for target_id in affected_targets:
            self._sync_card_connections(target_id)
        self._unresolved_edges = [
            u for u in self._unresolved_edges if node_id not in (u.edge.source, u.edge.target)
        ]
        self._sync_toolbar_state()

    def _forget_layout(self, node_id: str):
        self._grid_ids = [None if i == node_id else i for i in self._grid_ids]
        self._area_entries.pop(node_id, None)
        self._grid_areas.pop(node_id, None)
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
        self._sync_node_style(instance_id, failed=bool(event.new))
        for edge_id, mapping in self._edge_id_map.items():
            if mapping[2] == instance_id:
                self._patch_canvas_props("edge", edge_id, self._edge_chip(mapping))

    def _canvas_node(self, instance_id: str) -> dict | None:
        """Return a node of the canvas, also while it is still waiting in a batched update."""
        if self._canvas_batch is not None:
            for node in self._canvas_batch[0]:
                if node["id"] == instance_id:
                    return node
        for node in self._flow.nodes:
            if node["id"] == instance_id:
                return node
        return None

    def _find_node_card(self, instance_id: str) -> NodeCard | None:
        node = self._canvas_node(instance_id)
        card = node.get("view") if node is not None else None
        return card if isinstance(card, NodeCard) else None

    def _node_cards(self) -> dict[str, NodeCard]:
        return {
            node["id"]: card
            for node in self._flow.nodes
            if isinstance(card := node.get("view"), NodeCard)
        }

    def _sync_node_style(self, instance_id: str, *, failed: bool | None = None):
        """Set a node's style from its card's handle positions, its failure and an agent outline.

        Each of them owns some keys of the one style, so a change to one of
        them keeps the others. The node is only patched when its style changes.
        """
        node = self._canvas_node(instance_id)
        if node is None:
            return
        style: dict[str, t.Any] = {}
        if isinstance(card := node.get("view"), NodeCard):
            style.update(card.handle_layout()[1])
        if failed is None:
            failures = (
                self._dataflow_graph.get_failures(instance_id) if self.propagate_failures else None
            )
            failed = bool(failures.failed) if failures is not None else False
        if failed:
            style.update(_FAILED_NODE_STYLE)
        style.update(self._node_outlines.get(instance_id) or {})
        if (node.get("style") or None) != (style or None):
            self._patch_canvas_props("node", instance_id, {"style": style or None})

    def _set_node_outline(self, instance_id: str, style: dict[str, str] | None):
        """Outline a node, e.g. one the assistant changed, or remove its outline with None."""
        if style:
            self._node_outlines[instance_id] = dict(style)
        elif self._node_outlines.pop(instance_id, None) is None:
            return
        self._sync_node_style(instance_id)

    def _sync_card_connections(self, instance_id: str):
        """Update a card's inputs: what they are connected to and whether a required one is missing."""
        card = self._find_node_card(instance_id)
        if card is None or not card.inputs:
            return
        incoming: dict[str, list[str]] = {}
        for edge in self._dataflow_graph.edges:
            if edge["target"] == instance_id:
                incoming.setdefault(edge["target_port"], []).append(
                    f"{self._node_name(edge['source'])}.{edge['source_port']}"
                )
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        literals = (item.get("inputs") or {}) if item is not None else {}
        state = self._dataflow_graph.get_state(instance_id)
        updated = []
        for port in card.inputs:
            name = port["name"]
            sources = incoming.get(name, [])
            missing = bool(
                port.get("required") and state is not None and getattr(state, name, None) is None
            )
            updated.append(
                {
                    **port,
                    "connected": bool(sources) or name in literals,
                    "source": ", ".join(sources),
                    "missing": missing,
                }
            )
        if updated != card.inputs:
            card.inputs = updated
        self._sync_node_style(instance_id)

    def _on_card_layout(self, instance_id: str):
        """Follow a card whose handles moved or changed style."""
        self._sync_node_style(instance_id)
        self._sync_card_bridge()

    def _on_card_bridge_action(self, data: dict):
        """Act on a click on one of the actions drawn inside a card."""
        node_id = data.get("node_id")
        card = self._find_node_card(node_id) if node_id else None
        if card is None:
            return
        action = data.get("action")
        if action == "toggle_collapse":
            self._set_card_view(node_id, collapsed=not card.collapsed)
        elif action == "select_output":
            port = data.get("port")
            if any(p["name"] == port for p in card.outputs):
                self._set_card_view(node_id, preview_output=port)
        elif action == "toggle_optional":
            card.show_optional = not card.show_optional
        elif action == "popout":
            self._open_card_popout(node_id)

    def _set_card_view(self, instance_id: str, **changes: t.Any):
        """Collapse or expand a card, or pick the output it previews as one undoable edit.

        Collapsing only changes how the canvas looks, like panning, so it is
        neither an edit in the history nor an unsaved change; it is saved
        with the dashboard's next save.
        """
        card = self._find_node_card(instance_id)
        if card is None:
            return
        changes = {k: v for k, v in changes.items() if getattr(card, k) != v}
        if not changes:
            return
        with pn.io.hold():
            card.param.update(**changes)
            if (status_bar := getattr(self, "_status_bar", None)) is not None:
                status_bar.sync_collapse_toggle()
        if self.read_only or "preview_output" not in changes:
            return
        name = self._node_name(instance_id)
        output = changes["preview_output"]
        self._record_edit(
            f"Previewed {name}.{output}",
            op="preview_output",
            args={"node": name, "output": output},
        )

    def _sync_card_bridge(self):
        bridge = getattr(self, "_card_bridge", None)
        if bridge is None or self._canvas_batch is not None:
            return
        handles = {iid: card.handle_layout()[0] for iid, card in self._node_cards().items()}
        if bridge.handles != handles:
            bridge.handles = handles

    def _collapse_all_nodes(self, collapsed: bool):
        """Collapse or expand every card on the canvas, which like collapsing one is no edit."""
        with pn.io.hold():
            for card in self._node_cards().values():
                card.collapsed = collapsed

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
                    pn.pane.DataFrame(safe_frame(value.head(10)), sizing_mode="stretch_width"),
                    sizing_mode="stretch_width",
                )
        except ImportError:
            pass
        if isinstance(value, (pn.viewable.Viewable, Viewer)):
            return value
        try:
            # The pane only serializes the value once rendered, where a failure would blank it.
            json.dumps(value)
            return pn.pane.JSON(value, depth=2, sizing_mode="stretch_width")
        except Exception:
            pass
        if is_plain(value):
            text = str(value)
        elif isinstance(value, (list, tuple, set, frozenset, dict)):
            text = safe_repr(value)
        else:
            # An object's own repr may list secrets, e.g. a source's URI and token.
            label = escape_markdown(object_label(value))
            if tables := held_table_names(value):
                label += " · " + ", ".join(f"`{name}`" for name in tables[:20])
                if len(tables) > 20:
                    label += " …"
            return markdown_pane(label, margin=0)
        if len(text) > 2000:
            text = text[:2000] + "…"
        return pn.pane.HTML(
            f'<pre class="fd-value-repr">{html.escape(text)}</pre>',
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_REPR_CSS],
        )

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
                f"Could not describe this output: {error_text(exc)}",
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
        self._how_made_shown = None
        self._release_span()
        self._inspect_dialog.param.update(objects=objects, open=True)
        reserve_space(self._workspace_area, True)

    def _release_span(self):
        if self._span_view is not None:
            self._span_view.release()
            self._span_view = None

    def _close_inspection_dialog(self):
        """Close the sheet and let go of the values it shows, whose tile may be gone."""
        self._inspected_id = None
        self._how_made_shown = None
        self._release_span()
        if self._inspect_dialog.open or self._inspect_dialog.objects:
            self._inspect_dialog.param.update(open=False, objects=[])
            reserve_space(self._workspace_area, False)

    def origin_turn(self, instance_id: str) -> DashboardTurn | None:
        """Return a copy of the turn that created a node, or None if no turn is recorded."""
        origin = self._origins.get(instance_id)
        turn = next((t for t in self._turn_log.turns if t.id == origin), None)
        return copy.deepcopy(turn) if turn is not None else None

    def made_by_agent(self, instance_id: str) -> bool:
        """Whether "How was this made?" applies: an agent's request created the node."""
        if not self.provenance:
            return False
        turn = self.origin_turn(instance_id)
        return turn is not None and turn.actor == "agent"

    def later_turns(self, instance_id: str) -> list[DashboardTurn]:
        """Return copies of the turns after a node's origin whose summary says they changed it."""
        origin = self._origins.get(instance_id)
        turns = self._turn_log.turns
        start = next((i for i, turn in enumerate(turns) if turn.id == origin), None)
        if start is None:
            return []
        from panel_flowdash.agent.yaml_format import node_names

        name = node_names(self._tile_items).get(instance_id, instance_id)
        return [copy.deepcopy(t) for t in turns[start + 1 :] if changes_node(t, name)]

    def _later_turns_may_be_more(self, instance_id: str) -> bool:
        """Whether a turn after a node's origin left changes out of its summary, so more may have changed it."""
        origin = self._origins.get(instance_id)
        turns = self._turn_log.turns
        start = next((i for i, turn in enumerate(turns) if turn.id == origin), None)
        return start is not None and any(summary_cut_short(t) for t in turns[start + 1 :])

    def _node_restricted(self, instance_id: str) -> bool:
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        if item is None:
            return False
        if "placeholder" in item or item.get("component_id") == RESTRICTED_COMPONENT_ID:
            return True
        return not self._component_authorized(self._resolve_component_id(item["component_id"]))

    def note_own_run(self, run_id: str):
        """Record that the person using this editor asked for run *run_id*, so it is never redacted for them."""
        self._own_runs.add(run_id)

    def run_redacted(self, run_id: str | None) -> bool:
        """Whether a request is shown to the person viewing without its content.

        Their own requests are always shown in full. Anyone else's are
        redacted for people who may not edit the dashboard, and when the
        request created or changed a node whose component this person may
        not use.
        """
        if run_id is not None and run_id in self._own_runs:
            return False
        if self.read_only:
            return True
        turns = [turn for turn in self._turn_log.turns if run_id and turn.run == run_id]
        if not turns:
            return False
        from panel_flowdash.agent.yaml_format import node_names

        made = {turn.id for turn in turns}
        names = node_names(self._tile_items)
        return any(
            self._node_restricted(instance_id)
            for instance_id, name in names.items()
            if self._origins.get(instance_id) in made
            or any(changes_node(turn, name) for turn in turns)
        )

    def _turn_redacted(self, turn: DashboardTurn, instance_id: str) -> bool:
        if turn.run is not None and turn.run in self._own_runs:
            return False
        return self._node_restricted(instance_id) or self.run_redacted(turn.run)

    def show_how_made(self, instance_id: str, turn_id: str | None = None):
        """Show the agent request that created a node, or the later turn *turn_id* that changed it.

        It shows the request's span (question, model calls, tool calls and
        published versions) while :attr:`agent_state` holds the run's steps,
        and what the turn records otherwise, followed by the turns that
        changed the node later. In edit mode it opens the sidecar's History tab
        for the node; in view mode it opens the side sheet. Requests are
        redacted as :meth:`run_redacted` decides, and when the node's
        component is one the person may not use.
        """
        self._render_how_made(instance_id, turn_id, open_sheet=True)

    def _run_state(self, run_id: str | None) -> RunState:
        """Return the ``RunState`` holding the steps of run *run_id*: the Explore view's or :attr:`agent_state`."""
        runner = self.explore_feed.runner if self._explore_feed is not None else None
        if runner is not None and run_id is not None and run_id in runner.state.runs:
            return runner.state
        return self.agent_state if self.agent_state is not None else self._history.state

    def _how_made_parts(
        self, instance_id: str, turn_id: str | None
    ) -> tuple[str, list[Viewable], t.Any]:
        """Return the caption and content of how a node was made or changed, and its span view.

        The span view, if any, follows the request's steps until released.
        """
        origin = self.origin_turn(instance_id)
        turn = origin
        if turn_id is not None:
            turn = next((t for t in self.later_turns(instance_id) if t.id == turn_id), origin)
        state = self._run_state(turn.run if turn is not None else None)
        later = self.later_turns(instance_id) if turn is origin else []
        body, span = provenance_objects(
            turn,
            state,
            redacted=self._turn_redacted(turn, instance_id),
            later=later,
            more=turn is origin and self._later_turns_may_be_more(instance_id),
            on_open=lambda t: self.show_how_made(instance_id, t.id),
        )
        caption = "How this was made" if turn is origin else "How this was changed"
        return caption, body, span

    def _render_how_made(self, instance_id: str, turn_id: str | None, *, open_sheet: bool):
        if not self.made_by_agent(instance_id):
            return
        # Over the Report view the side sheet shows it, as in view mode.
        if self._inspector.visible and self.surface == "workspace":
            self._close_inspection_dialog()
            self._show_in_sidecar(instance_id, HISTORY, turn_id)
            return
        self._release_span()
        caption, body, self._span_view = self._how_made_parts(instance_id, turn_id)
        title = self._instance_title(instance_id)
        objects = [sheet_header(title, self._close_inspection_dialog, caption=caption), *body]
        self._how_made_shown = (instance_id, turn_id)
        self._inspected_id = instance_id
        if not open_sheet:
            # Re-rendering for new rights never reopens a sheet the person closed, and keeps
            # its header, so a click on Close already on its way still closes it.
            current = self._inspect_dialog.objects
            header = current[:1] if current and caption == self._how_made_caption else objects[:1]
            self._how_made_caption = caption
            self._inspect_dialog.objects = [*header, *body]
            return
        self._how_made_caption = caption
        self._inspect_dialog.param.update(objects=objects, open=True)
        if not self.editable:
            reserve_space(self._workspace_area, True)

    @param.depends("editable", watch=True)
    def _close_sheet_for_mode(self):
        # The sheet belongs to the mode it was opened in: in edit mode it would cover
        # the dashboard bar's Save and Done, so the sidecar takes over what it showed.
        shown, inspected = self._how_made_shown, self._inspected_id
        was_open = self._inspect_dialog.open
        self._close_inspection_dialog()
        if not (self.editable and was_open and inspected is not None):
            return
        if shown is not None and shown[0] == inspected:
            self._show_in_sidecar(inspected, HISTORY, shown[1])
        elif self._can_inspect():
            self._show_in_sidecar(inspected, DATA)

    def _show_in_sidecar(self, instance_id: str, tab: str, turn_id: str | None = None):
        """Select a node and open the sidecar on its *tab*, as the person asked for it."""
        if not any(node["id"] == instance_id for node in self._flow.nodes):
            return
        self._inspector.open_tab(instance_id, tab, turn_id)
        if self._inspector.visible and self._inspector.collapsed:
            self._sidecar.user_toggle(True)
        self._select_node(instance_id)

    @param.depends("read_only", watch=True)
    def _reshow_how_made(self):
        shown = self._how_made_shown
        if shown is not None and self._inspect_dialog.open and self._inspected_id == shown[0]:
            self._render_how_made(*shown, open_sheet=False)

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
            on_how_made=partial(self.show_how_made, instance_id),
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

    def _sync_tile(self, instance_id: str, tile: Tile, spec: ComponentSpec | None):
        """Offer a tile's actions to whoever is viewing it now."""
        show_in_editor = self._can_show_in_editor()
        tile.set_actions(
            inspect=self._can_inspect_tile(spec),
            show_in_editor=show_in_editor,
            how_made=self.made_by_agent(instance_id),
        )
        if isinstance(tile.view, FailureOverlay):
            for button in tile.view.actions:
                button.visible = show_in_editor

    @param.depends("editable", "show_in_editor", "provenance", watch=True)
    def _sync_tile_actions(self):
        for item in self._tile_items:
            tile = self._inspect_tiles.get(item["instance_id"])
            if tile is not None:
                self._sync_tile(item["instance_id"], tile, self._tile_spec(item))

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
        tile.set_style(framed=self._tile_framed(item), titled=self.settings.tile_titles)
        tile.set_stale(self._code_stale(instance_id))
        tile.set_filters(*self._global_filters.notes(instance_id))
        self._sync_tile(instance_id, tile, spec)
        return tile

    def _tile_framed(self, item: dict) -> bool:
        """Whether a tile is framed: its own option, its component's hint, then the dashboard's."""
        spec = self._tile_spec(item)
        return resolve_framed(
            self._tile_frames.get(item["instance_id"]),
            spec.framed if spec is not None else None,
            self.settings.tile_cards,
        )

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
        # Controls that were not moved follow the setting.
        if self._grid_populated:
            self._rebuild_tile_grid()
        else:
            self._rebuild_sidebar()

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
        tiles = getattr(self, "_inspect_tiles", {})
        for item in getattr(self, "_tile_items", []):
            if (tile := tiles.get(item["instance_id"])) is not None:
                tile.set_style(framed=self._tile_framed(item), titled=self.settings.tile_titles)

    def _can_inspect(self) -> bool:
        """Whether this session may inspect outputs: editors always, viewers if the dashboard allows."""
        return self.inspect_outputs and (not self.read_only or self.settings.viewers_can_inspect)

    @param.depends("read_only", "settings.viewers_can_inspect", watch=True)
    def _sync_inspect_buttons(self):
        self._sync_tile_actions()
        if not self._can_inspect() and self._how_made_shown is None:
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
        if group_id := group_id_of(node_id):
            # A collapsed group's port shows the value of the first inner port it stands for.
            group = self._groups.get(group_id)
            ends = (
                []
                if group is None
                else [
                    port.ends[0]
                    for port in itertools.chain(*self._group_ports(group))
                    if port.handle == handle_id
                ]
            )
            if not ends:
                return
            node_id, handle_id = ends[0]
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

    def _apply_config_patch(self, node_id, patch, inputs=None):
        """Apply an editor patch to a node's config state, and *inputs* literals, as one edit.

        The *inputs* are checked first, so a refused one changes nothing.
        """
        changed = self._set_input_values(node_id, inputs) if inputs else {}
        applied = self._set_config_state(node_id, patch) if patch else {}
        if not applied and not changed:
            return
        name = self._node_name(node_id)
        if not applied:
            labels = ", ".join(changed.values())
            label = f"Set {labels} of {name}"
        else:
            label = f"Changed the config of {name}"
        self._record_edit(
            label,
            op="set_config" if applied else "set_input",
            args={
                "name": name,
                "keys": sorted(applied) + [f"inputs.{p}" for p in sorted(changed)],
            },
        )

    def _set_config_state(self, node_id, patch) -> dict:
        """Apply a patch to a node's config state and item, and return what was applied."""
        config_state = self._dataflow_graph.get_config_state(node_id)
        applied = {}
        values = {}
        for key, value in patch.items():
            if config_state is not None and hasattr(config_state.param, key):
                rejected = self._rejected_config.get(node_id, {}).pop(key, _MISSING)
                try:
                    converted = config_value(config_state.param[key], value)
                    config_state.param[key]._validate(converted)
                except Exception as exc:
                    logger.warning("Config '%s' rejected on %s: %s", key, node_id, exc)
                    if rejected is not _MISSING:
                        self._rejected_config[node_id][key] = rejected
                    continue
                values[key] = converted
            applied[key] = value
        if values:
            # Set together, so the node and its component see one change, not one per field.
            try:
                config_state.param.update(values)
            except Exception:
                for key, value in values.items():
                    try:
                        setattr(config_state, key, value)
                    except Exception as exc:
                        logger.warning("Config '%s' rejected on %s: %s", key, node_id, exc)
                        applied.pop(key, None)
        if applied:
            for item in self._tile_items:
                if item["instance_id"] == node_id:
                    item.setdefault("config", {}).update(applied)
                    break
        return applied

    def _seed_config_state(self, instance_id, config):
        """Overlay saved config onto a node's config state and return node data seed."""
        config_state = self._dataflow_graph.get_config_state(instance_id)
        if config_state is None:
            return {}
        for key, value in (config or {}).items():
            if hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, config_value(config_state.param[key], value))
                except Exception as exc:
                    logger.warning("Saved config '%s' rejected on %s: %s", key, instance_id, exc)
                    self._rejected_config.setdefault(instance_id, {})[key] = value
        return {name: getattr(config_state, name) for name in config_state.param if name != "name"}

    def _bind_config_to_viewer(self, node_id, instance, config_state, wiring):
        """Sync config-state params onto a Viewer instance, live.

        A config param the component declares ``two_way`` in its ``"flowdash"``
        metadata is synced back too, so a value the component edits itself, such
        as text typed into its tile, is saved with the dashboard.
        """
        names = []
        for name in config_state.param:
            if name == "name" or not hasattr(instance.param, name):
                continue
            try:
                setattr(instance, name, getattr(config_state, name))
            except Exception as exc:
                logger.warning("Config '%s' could not be set: %s", name, exc)
                continue
            names.append(name)

        def _propagate(*events):
            # One update for a patch of several fields, so the component refreshes once.
            values = {event.name: event.new for event in events}
            try:
                instance.param.update(values)
                return
            except Exception:
                if len(values) == 1:
                    logger.warning("Config '%s' update failed", events[0].name, exc_info=True)
                    return
            for name, exc in _apply_each(instance, values):
                logger.warning("Config '%s' update failed: %s", name, exc)

        if names:
            watcher = config_state.param.watch(_propagate, names)
            wiring.append(partial(config_state.param.unwatch, watcher))
        for name in names:
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
        config_state = editor._dataflow_graph.get_config_state(node_id)
        # The config's own value coming back from the sync onto the component is no edit.
        if config_state is not None and getattr(config_state, event.name, _MISSING) is event.new:
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
        app_fn = stepped(
            app_fn, partial(_creating, weakref.ref(self), node_id, wiring, _PLACEMENT.get())
        )
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
        input_names = [
            p.name for p in (spec.inputs if spec else ()) if hasattr(instance.param, p.name)
        ]

        def _propagate_inputs(*events):
            # Inputs changed together arrive together, so the component runs once.
            values = {event.name: event.new for event in events}
            if (owner := editor()) is not None:
                values = owner._global_filters.filter_inputs(node_id, values)
            try:
                instance.param.update(values)
                if (owner := editor()) is not None:
                    owner._global_filters.inputs_changed(node_id, values)
                return
            except Exception as exc:
                if len(values) == 1:
                    if (owner := editor()) is not None:
                        owner._report_input_error(node_id, events[0].name, exc)
                    return
            # A component rejecting a value, or a watcher of its own raising,
            # must not abort the dispatch to the node's other consumers.
            for name, exc in _apply_each(instance, values):
                if (owner := editor()) is not None:
                    owner._report_input_error(node_id, name, exc)

        if input_names:
            watcher = node_state.param.watch(_propagate_inputs, input_names)
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
                        # Param does not catch a Skip raised by an async
                        # generator, so end the run instead of raising one.
                        try:
                            failure = _failed(exc)
                        except param.Skip:
                            return
                        yield failure
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
        self._tile_drag = TileDrag(
            grid=self._grid_class,
            uid=self._view_controls.uid,
            on_move=self._on_tile_dropped,
            on_refuse=self._on_tile_refused,
            on_release=self._on_tile_released,
        )
        self._save_button = bar.save_button
        self._download_button = bar.download_button
        self._refresh_button = bar.refresh_button
        self._settings_button = bar.settings_button
        self._save_button.on_click(lambda _event: self._save_from_button())
        self._clear_dialog = self._build_clear_dialog()
        self._pending_import: DashboardModel | None = None
        self._import_message = markdown_pane()
        self._import_dialog = self._build_import_dialog()
        self._transform_offer = TransformOffer(self)
        self._inspect_dialog = make_inspect_sheet()
        # Saving is only offered with unsaved changes, and rearranging is a change.
        self._flow.on("node_moved", self._on_node_moved)
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
            stylesheets=[HIDE_ON_PHONE],
        )
        self._start_card = StartCard(editor=self)
        self._empty_hint = self._start_card.__panel__()
        # Its own minimum, or the closed dialogs it holds (Clear canvas, a tile's full
        # screen view) would keep the canvas their width and push the inspector off screen.
        self._workspace_area = pn.Column(
            sizing_mode="stretch_both",
            scroll="y-auto",
            stylesheets=[f":host {{ min-width: {_CANVAS_MIN_WIDTH}px !important; }}"],
        )
        # The modal dialogs overlay the page, so they hold no room in it. Panel
        # adds a child's width to its layout's minimum even when that child is a
        # closed dialog, which would keep the dashboard wider than a phone.
        self._dialog_host = pn.Column(margin=0, width=0, height=0, sizing_mode="fixed")
        self._inspector = Inspector(editor=self)
        self._layout_panel = LayoutPanel(editor=self)
        self._sidecar = SidecarFold(self)
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
        self._flow.param.watch(self._on_selection_for_sidecar, "selection")
        self._inspector.param.watch(self._on_sidecar_collapsed, "collapsed")
        self._inspector.param.watch(self._on_sidecar_resized, "width")
        self._inspector.param.watch(lambda _event: self._sidecar.sync(), "visible")
        self._split_view.storage.param.watch(self._on_window_resized, "window_width")
        self._code_panel: CodePanel | None = None
        self._code_dock_height = _CODE_PANEL_HEIGHT
        self._code_panel_was_allowed = self._code_panel_allowed()
        self._code_dock = pn.Column(
            margin=0,
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-code-dock"],
            # Collapsed to its header, not the smallest height of its editor and result.
            stylesheets=[".fd-code-panel { min-height: 0 !important; }"],
        )
        # Set while the window or the tab moves the panel, which is not a choice to remember.
        self._moving_code_dock = False

        # The sidecar's slot, hidden with it, since an empty Column still takes
        # the width of its scroll button.
        self._panel_slot = pn.Column(
            self._inspector, margin=0, sizing_mode="stretch_height", stylesheets=[HIDE_ON_PHONE]
        )

        self._build_phone_sheets()

        self._sync_toolbar_extra()
        self._sync_toolbar_state()
        self._stamp_refresh()
        self._editor_row = pn.Row(
            self._side_panel,
            self._workspace_area,
            self._panel_slot,
            sizing_mode="stretch_both",
        )
        self._card_bridge = CardBridge(
            canvas_class=f"fd-canvas-{id(self)}",
            on_action=_EditorCall(self, "_on_card_bridge_action"),
            margin=0,
            height=0,
        )
        return pn.Column(
            bar,
            self._editor_row,
            # Under the palette, the canvas and the inspector, so it has the window's width.
            self._code_dock,
            self._status_bar,
            self._fullscreen_overlay,
            self._fullscreen_controller,
            self._sheet_host,
            self._tile_drag,
            self._split_view.storage,
            self._card_bridge,
            sizing_mode="stretch_both",
            stylesheets=[PRESENTATION_CSS],
        )

    def _build_phone_sheets(self):
        """Make the bottom sheets and buttons standing in for the side panels on a phone."""
        self._palette_sheet, self._palette_sheet_body = bottom_sheet(
            "Add component", "fd-palette-sheet", "85dvh"
        )
        self._layout_sheet, self._layout_sheet_body = bottom_sheet(
            "Arrange tiles", "fd-layout-sheet", "70dvh"
        )
        self._inspector_sheet = inspector_sheet()
        self._inspector_label = SheetLabel(label="Inspector", width=0, height=0, margin=0)
        self._add_button = sheet_button("add", "Add component", "fd-add-component")
        self._add_button.on_click(lambda _event: setattr(self._palette_sheet, "open", True))
        self._layout_button = sheet_button("tune", "Arrange tiles", "fd-arrange-tiles")
        self._layout_button.on_click(lambda _event: self._open_layout_sheet())
        # Like the dialogs, the sheets take no room in the layout. The inspector's
        # sheet, which is always rendered, joins them only on a phone.
        self._sheet_host = pn.Column(
            self._add_button,
            self._layout_button,
            self._palette_sheet,
            self._layout_sheet,
            margin=0,
            width=0,
            height=0,
            sizing_mode="fixed",
        )
        self._dashboard_bar._phone_query.param.watch(self._on_phone_changed, "phone")
        self._flow.param.watch(self._on_phone_selection, "selection")
        # A tap on the selected node or connection opens a dismissed sheet again.
        self._flow.on("node_clicked", self._on_phone_tap)
        self._flow.on("edge_clicked", self._on_phone_tap)
        self._inspector.param.watch(lambda _event: self._sync_inspector_sheet(), "collapsed")
        self._inspector.param.watch(self._on_editing_spec, "editing_spec")

    def _open_layout_sheet(self):
        self._layout_panel.refresh()
        self._layout_sheet.open = True

    @pn.io.hold()
    def _on_phone_changed(self, event):
        """Move the palette and the side panels into bottom sheets below the phone breakpoint, and back."""
        phone = self._phone = event.new
        self._palette_sheet.open = self._layout_sheet.open = False
        # Sized for where they go before they move, so the columns they leave
        # and join keep their own sizing.
        self._palette_panel.touch = phone
        self._inspector.sheet = phone
        if phone:
            self._side_panel[:] = []
            self._panel_slot[:] = []
            self._palette_sheet_body[:] = [self._palette_section]
            self._layout_sheet_body[:] = [self._layout_panel]
            self._inspector_sheet[:] = [self._inspector_label, self._inspector]
            self._sheet_host.append(self._inspector_sheet)
        else:
            self._sheet_host.remove(self._inspector_sheet)
            self._inspector_sheet[:] = []
            self._layout_sheet_body[:] = []
            self._palette_sheet_body[:] = []
            self._side_panel[:] = [self._palette_section]
            self._panel_slot[:] = [self._inspector]
            # A sheet dismissed on the phone is not a fold of the column.
            self._sidecar.left_phone()
        self._sync_min_width()
        self._apply_mode_state()
        self._sync_code_dock_side()
        if self._inspector.visible:
            # Fit to the canvas's new width once the browser has laid it out.
            pn.state.execute(self._fit_canvas, schedule=True)

    def _sync_phone_panels(self, showing_canvas: bool, layout: bool):
        """Show the side panels beside the workspace, or on a phone the buttons opening their sheets."""
        phone = self._phone
        self._side_panel.visible = showing_canvas and not phone
        self._panel_slot.visible = not phone and self._inspector.visible
        # Shown on phones only, by their stylesheet.
        self._add_button.visible = showing_canvas and self.editable and not self.preview
        self._layout_button.visible = layout
        if not showing_canvas:
            self._palette_sheet.open = False
        if not layout:
            self._layout_sheet.open = False
        self._sync_inspector_sheet()

    def _on_phone_tap(self, _payload):
        if self._phone:
            self._inspector.collapsed = False

    def _on_phone_selection(self, event):
        # A new selection opens the sheet again after it was dismissed.
        if self._phone and _has_selection(event.new) and event.new != event.old:
            self._inspector.collapsed = False
        self._sync_inspector_sheet()

    def _reveal_above_sheet(self, instance_id: str):
        """Pan the canvas so the node of *instance_id* sits at its top, above the inspector's sheet."""
        node = next((n for n in self._flow.nodes if n["id"] == instance_id), None)
        width = self._split_view.storage.window_width
        if node is None or not width:
            return
        zoom = max((self._flow.viewport or {}).get("zoom") or 1, _PHONE_REVEAL_ZOOM)
        x = width / 2 - (node["position"]["x"] + CARD_WIDTH / 2) * zoom
        y = _PHONE_REVEAL_TOP - node["position"]["y"] * zoom
        self._flow.viewport = {"x": x, "y": y, "zoom": zoom}

    def _on_editing_spec(self, event):
        height = INSPECTOR_SHEET_TALL_HEIGHT if event.new else INSPECTOR_SHEET_HEIGHT
        self._inspector_sheet.sx = {**self._inspector_sheet.sx, "height": height}
        self._sync_inspector_sheet()

    def _sync_inspector_sheet(self):
        """Open the inspector's sheet on a phone while something is selected and it is not dismissed."""
        inspector = self._inspector
        is_open = self._inspector_sheet.open = (
            self._phone
            and inspector.visible
            and not inspector.collapsed
            and _has_selection(self._flow.selection)
            # In the Layout tab it opens for a tile tapped there, not over the grid at once.
            and (self.mode != "dashboard" or self._phone_tile_tapped)
        )
        lifted = [ABOVE_SHEET_CLASS]
        if inspector.editing_spec:
            # The taller sheet leaves no room above it for the button.
            lifted.append(BEHIND_SHEET_CLASS)
        classes = [
            c
            for c in self._add_button.css_classes
            if c not in (ABOVE_SHEET_CLASS, BEHIND_SHEET_CLASS)
        ]
        self._add_button.css_classes = [*classes, *lifted] if is_open else classes

    @property
    def explore_feed(self):
        """The :class:`~panel_flowdash.explore.ExploreFeed` the Explore view shows, made on first use."""
        if self._explore_feed is None:
            from panel_flowdash.explore import ExploreFeed

            self._explore_feed = ExploreFeed(self)
        return self._explore_feed

    @property
    def report_view(self):
        """The :class:`~panel_flowdash.ui.report_view.ReportView` the Report view shows, made on first use."""
        if self._report_view is None:
            # ReportView is imported with the editor: its contents list is a JSComponent,
            # whose type must be known before the first page is served.
            self._report_view = ReportView(editor=self)
        return self._report_view

    @param.depends("explore", watch=True)
    def _explore_to_surface(self):
        if self.explore:
            self.surface = "explore"
        elif self.surface == "explore":
            self.surface = "workspace"

    @pn.io.hold()
    @param.depends("surface", watch=True)
    def _show_surface(self):
        """Show the Explore view, the Report view or the workspace; one at a time, as they share the tiles' views."""
        surface = self.surface
        if self.explore != (surface == "explore"):
            self.explore = surface == "explore"
        view = self._view
        self._sync_min_width()
        away = {"explore": self._explore_feed, "report": self._report_view}
        for name, shown in away.items():
            if shown is not None and name != surface:
                shown.shown = False
        if surface == "workspace":
            view[1] = self._editor_row
            self._status_bar.visible = self.editable
            self._sync_tile_drag()
            return
        self.fullscreen = False
        shown = self.explore_feed if surface == "explore" else self.report_view
        view[1] = shown
        self._status_bar.visible = False
        shown.shown = True

    @param.depends("editable", watch=True)
    def _sync_min_width(self):
        """Let the feed, the report and the dashboard narrow to the window; only the canvas and its panels need the room."""
        canvas = self.editable and self.surface == "workspace" and not self._phone
        self._view.min_width = None if canvas else 0

    @param.depends("edited", watch=True)
    def _sync_explore(self):
        if self._explore_feed is not None:
            self._explore_feed._schedule()
        if self._report_view is not None:
            self._report_view.schedule()

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

    def _on_node_moved(self, payload):
        node_id = payload.get("node_id") if isinstance(payload, dict) else None
        if node_id is None:
            self._floating_nodes.clear()
        else:
            self._floating_nodes.discard(node_id)
        self._mark_rearranged("Moved nodes on the canvas", "move_nodes")

    def _on_tile_layout_edited(self, event):
        # Only a layout the browser sent was edited by the user; the editor sets
        # it itself whenever the grid is rebuilt or sized.
        from_browser = self._tile_grid._in_process__events.get(pn.state.curdoc, {})
        if event.name in from_browser:
            if event.name == "layout" and len(event.new) != len(self._grid_ids):
                # Sent for tiles the grid showed before a change the browser has
                # not applied yet, so it would size the wrong tiles.
                self._tile_grid.layout = event.old
                return
            self._auto_tiles.clear()
            if event.name == "layout":
                for instance_id, entry in zip(self._grid_ids, event.new, strict=False):
                    height = self._fill_heights.get(instance_id)
                    if height is not None and entry.get("height") != height:
                        del self._fill_heights[instance_id]
            if (release := self._awaiting_tile_drop()) is not None:
                release["held"] = True
            else:
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
            **escape_closes(),
        )

    def _build_import_dialog(self):
        cancel = pmui.Button(label="Cancel", variant="text")
        confirm = pmui.Button(label="Replace canvas", color="danger")

        def _on_confirm(_event):
            model, self._pending_import = self._pending_import, None
            self._import_dialog.open = False
            if model is not None:
                self._apply_import(model)

        def _on_closed(event):
            # Cancel, Escape and a click outside all drop the file.
            if not event.new:
                self._pending_import = None

        cancel.on_click(lambda _event: self._import_dialog.param.update(open=False))
        confirm.on_click(_on_confirm)
        dialog = pmui.Dialog(
            objects=[
                self._import_message,
                pn.Row(pn.layout.HSpacer(), cancel, confirm, sizing_mode="stretch_width"),
            ],
            title="Load dashboard file",
            open=False,
            min_width=400,
            **escape_closes(),
        )
        dialog.param.watch(_on_closed, "open")
        return dialog

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
                css_classes=[self._grid_class],
            )
            self._tile__grid.param.watch(self._on_tile_layout, "layout")
            self._tile__grid.param.watch(
                self._sync_preview_band, ["reference_width", "breakpoints", "preview_band"]
            )
            self._tile__grid.param.watch(self._on_tile_selected, "selected")
        return self._tile__grid

    @property
    def _grid_class(self) -> str:
        return f"fd-grid-{id(self):x}"

    def _sync_tile_drag(self):
        """Tell the page which tiles and components can be dragged where, and whether now."""
        drag = getattr(self, "_tile_drag", None)
        if drag is None:
            return
        controls = self._view_controls
        split = self.mode == "split"
        enabled = (
            self.editable
            and not self.preview
            and self._grid_on_screen
            and self.controls_bar
            and not self.fullscreen
        )
        items = {item["instance_id"]: item for item in self._tile_items}
        tiles = {}
        for instance_id in self._grid_ids:
            tile = self._inspect_tiles.get(instance_id)
            if instance_id not in items or tile is None:
                continue
            key = next((c for c in tile.css_classes if c.startswith(TILE_KEY_PREFIX)), None)
            if key is not None:
                item = items[instance_id]
                tiles[key[len(TILE_KEY_PREFIX) :]] = {
                    "id": instance_id,
                    "fits": tile_layout.fits_beside(self, item),
                    # A placeholder stays put because it cannot be loaded, not for its size.
                    "refusal": "Unavailable components stay on the grid"
                    if "placeholder" in item
                    else "",
                }
        handles = {
            key: {
                "id": instance_id,
                "fits": True,
                "title": tile_layout.tile_title(self, items[instance_id]),
            }
            for instance_id, key in controls.keys.items()
            if instance_id in items
        }
        if not enabled:
            self._end_tile_release()
        drag.param.update(
            enabled=enabled,
            areas=[tile_layout.TOP] if split else [tile_layout.TOP, tile_layout.SIDEBAR],
            tiles=tiles,
            controls=handles,
        )

    def _on_tile_released(self, instance_id: str):
        self._end_tile_release(instance_id)
        at = time.monotonic()
        self._tile_releases[instance_id] = {"at": at, "held": False}
        ref = weakref.ref(self)

        def expire():
            editor = ref()
            if editor is not None and editor._tile_releases.get(instance_id, {}).get("at") == at:
                editor._end_tile_release(instance_id)

        self._call_later(_TILE_DROP_WAIT, expire)

    def _call_later(self, seconds: float, callback: t.Callable[[], None]):
        """Call *callback* once after *seconds*, in a served session; elsewhere never."""
        from panel.io.state import set_curdoc

        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            return

        def run():
            with set_curdoc(doc):
                callback()

        doc.add_timeout_callback(run, int(seconds * 1000))

    def _awaiting_tile_drop(self) -> dict[str, t.Any] | None:
        """Return the latest release of a tile beside the grid still waiting for its drop."""
        now = time.monotonic()
        for instance_id, release in list(self._tile_releases.items()):
            if now - release["at"] >= _TILE_DROP_WAIT:
                # The drop never came; the caller records the layout, held one included.
                del self._tile_releases[instance_id]
        return next(reversed(self._tile_releases.values()), None)

    def _end_tile_release(self, instance_id: str | None = None, recorded: bool = False):
        """Forget the release of *instance_id*, or every one, recording held layouts unless *recorded*."""
        ids = list(self._tile_releases) if instance_id is None else [instance_id]
        releases = [self._tile_releases.pop(i, None) for i in ids]
        if not recorded and any(r is not None and r["held"] for r in releases):
            # Not `_mark_rearranged`: the layout came while editing, even if that just ended.
            self._record_edit("Changed the tile layout", op="layout")

    def _on_tile_dropped(self, instance_id: str, area: str, before: str | None):
        moved = False
        try:
            if self.editable and any(i["instance_id"] == instance_id for i in self._tile_items):
                moved = tile_layout.move_to_area(self, instance_id, area, before)
        except ValueError as exc:
            self._notify("info", error_text(exc), duration=4000)
        finally:
            self._end_tile_release(instance_id, recorded=moved)

    def _on_tile_refused(self, instance_id: str, area: str):
        self._end_tile_release(instance_id)
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        if item is not None:
            self._notify("info", tile_layout.refusal(self, item, area), duration=4000)

    def _preview_band(self) -> str | None:
        """Return the band the grid is laid out and edited for, or None to follow its width.

        The Layout tab arranges the band picked in its layout panel, so its
        edits land in that band however narrow the grid is. The Split tab
        follows the pane's width and shows the dashboard as it would look at
        that width; its tiles can't be dragged or resized there, since an
        edit would otherwise go to whichever band the pane happens to fall in.
        """
        if not self.editable or self.preview or self.mode != "dashboard":
            return None
        grid = self._tile_grid
        band = self._layout_panel.band
        if band is not None and band in grid.param.preview_band.objects:
            return band
        return base_band(grid.breakpoints, grid.reference_width)

    def _sync_preview_band(self, *_events):
        layout_panel = getattr(self, "_layout_panel", None)
        if layout_panel is None:
            return
        grid = self._tile_grid
        band = self._preview_band()
        # The frame TileGrid draws around a previewed band marks a screen width;
        # the widest band is not one.
        labels = [label for label in grid.param.preview_band.objects if label is not None]
        framed = band is not None and band != labels[-1]
        classes = [c for c in grid.css_classes if c not in (_UNFRAMED_BAND, _SPLIT_PREVIEW)]
        if band is not None and not framed:
            classes.append(_UNFRAMED_BAND)
        if self.editable and self.mode == "split":
            classes.append(_SPLIT_PREVIEW)
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
        if instance_id is None or not self._tile_selection_follows:
            return
        if self.mode == "dashboard":
            # Entered from the Layout tab, the sidecar shows the tile's options first.
            self._inspector.open_tab(instance_id, TILE)
            self._phone_tile_tapped = True
        self._select_node(instance_id)
        if self._phone:
            self._sync_inspector_sheet()

    def _clear_selection(self) -> bool:
        """Clear the selection on the canvas and the tile grid; return whether there was one.

        In the Layout tab the canvas is hidden and its page reports nothing,
        so the selection is cleared here as well.
        """
        selection = self._flow.selection or {}
        tile = self._selected_tile_id()
        if not (selection.get("nodes") or selection.get("edges") or tile is not None):
            return False
        select_on_canvas(self._flow)
        if self._canvas_host is None:
            self._flow.selection = {"nodes": [], "edges": []}
        self._set_selected_tile(None, follow=False)
        return True

    def _select_node(self, instance_id: str):
        """Select the node of *instance_id* on the canvas, bringing it into view in the Split tab."""
        if not any(node["id"] == instance_id for node in self._flow.nodes):
            return
        selection = self._flow.selection or {}
        if selection.get("nodes") == [instance_id] and not selection.get("edges"):
            return
        if self.mode == "split":
            self._reveal_node(instance_id)
        select_on_canvas(self._flow, node_id=instance_id)
        if self._canvas_host is None:
            # No canvas is shown to report the selection, e.g. in the Layout tab.
            self._flow.selection = {"nodes": [instance_id], "edges": []}

    def _sized_layout(self, layout: list[dict]) -> list[dict]:
        """Apply the size hints of the tiles on the grid to its positional *layout*."""
        items = {item["instance_id"]: item for item in self._tile_items}
        specs = [self._tile_spec(items[i]) if i in items else None for i in self._grid_ids]
        lazy: list[ReplacementPane] = []
        fills = []
        for instance_id in self._grid_ids:
            tile = self._inspect_tiles.get(instance_id)
            fills.append(tile is not None and _fills_tile(tile.view, lazy))
        for pane in lazy:
            if pane not in self._fill_watched:
                self._fill_watched.add(pane)
                pane._inner_layout.param.watch(self._refill_tiles, "objects")
        grid = self._tile_grid
        width = reference_width(grid.reference_width, grid.breakpoints)
        hinted = _size_layout(layout, specs, width)
        sized = _size_layout(layout, specs, width, fills)
        # Heights given only for display, so a layout without them is saved without them.
        for i, (instance_id, entry) in enumerate(zip(self._grid_ids, sized, strict=False)):
            if "height" in entry and (i >= len(hinted) or "height" not in hinted[i]):
                self._fill_heights[instance_id] = entry["height"]
        return sized

    def _refill_tiles(self, *_events):
        """Size the tiles again once a pane in one of them rendered what it shows."""
        if not self._grid_populated:
            return
        layout = self._deferred_grid_layout
        if layout is None:
            layout = self._tile_grid.layout
        sized = self._sized_layout(layout)
        if sized != layout:
            self._set_grid_layout(sized)

    def _without_fill_heights(self, keyed: list[dict]) -> list[dict]:
        """Drop the heights the editor gave tiles that fill them and nobody resized."""
        stripped = []
        for entry in keyed:
            height = self._fill_heights.get(entry.get("instance_id"))
            if height is not None and entry.get("height") == height:
                entry = {k: v for k, v in entry.items() if k != "height"}
            stripped.append(entry)
        return stripped

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
                    band: self._without_fill_heights(
                        self._placed_layout(_key_layout(layout, self._grid_ids))
                    )
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
        # The sidecar is one panel in every tab that edits: Dataflow, Split and Layout.
        self._inspector.visible = showing_canvas or (interactive and showing_grid)
        self._layout_panel.visible = interactive and showing_grid and not split
        self._sync_phone_panels(showing_canvas, self._layout_panel.visible)
        self._status_bar.visible = self.editable and self.surface == "workspace"
        self._split_view.events.active = split
        self._view_controls.param.update(beside_canvas=split, editing=interactive and showing_grid)
        self._split_shown = split
        self._tile_grid.param.update(editable=interactive, card=interactive)
        self._sync_preview_band()
        # Column children rather than Row children, so the dialogs take no flex space.
        dialogs = [
            self._clear_dialog,
            self._import_dialog,
            self._settings_dialog,
            self._transform_offer.dialog,
        ]
        shared_dialog = page_dialog() if self.include_page_dialog else None
        if shared_dialog is not None:
            dialogs.append(shared_dialog)
        if list(self._dialog_host) != dialogs:
            self._dialog_host[:] = dialogs
        dialogs = [self._dialog_host]
        if self.inspect_outputs or self.provenance:
            dialogs.insert(0, self._inspect_dialog)
        canvas_host = "split" if split else "wiring" if showing_canvas else None
        if canvas_host != self._canvas_host:
            self._canvas_host = canvas_host
            self._expect_canvas_remount()
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
        if showing_canvas:
            self._fit_placed_nodes()
        self._sync_tile_selection()
        self._sync_tile_drag()
        self._sidecar.sync()
        # "Arrange for" is shown in the Layout tab only.
        self._inspector.refresh_summary()

    def _expect_canvas_remount(self):
        """Keep the selection while the canvas moves to another tab, or is hidden.

        The canvas's old view reports an empty selection as it goes, though the
        nodes stay selected on the page; the selection is the editor's, so it
        is kept for the inspector, the tiles and the code panel alike.
        """
        selection = self._flow.selection or {}
        if selection.get("nodes") or selection.get("edges"):
            self._remount_deadline = time.monotonic() + _CANVAS_REMOUNT_GRACE

    def _selection_dropped_by_remount(self, msg: dict) -> bool:
        if msg.get("type") != "selection_changed" or not self._remount_deadline:
            return False
        expected = time.monotonic() < self._remount_deadline
        self._remount_deadline = 0.0
        return expected and not (msg.get("nodes") or msg.get("edges"))

    @param.depends("mode", watch=True)
    def _remember_mode(self):
        self._phone_tile_tapped = False
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
        return self.surface == "workspace" and (
            self.mode in ("dashboard", "split") or not self.editable
        )

    def _on_split_collapsed(self, pane: int):
        """Fall back to the tab of the pane left over, keeping the split's sizes for next time."""
        if self.mode == "split":
            self.mode = "dashboard" if pane == 0 else "wiring"
        self._split_view.restore()

    def _on_split_preferences_loaded(self):
        self._restore_split = self._restore_split and self._split_view.preferences.split
        self._apply_stored_split()
        self._inspector.width = self._split_view.preferences.sidecar_width
        self._sync_code_dock_side()
        if self._code_panel is not None:
            self._sync_code_spec_view()

    @param.depends("editable", watch=True)
    def _apply_stored_split(self):
        """Reopen the Split tab remembered by the browser, once, when the editor is editable."""
        if not (self.editable and self._restore_split and self._split_view.loaded):
            return
        self._restore_split = False
        if self.mode != "split":
            self.mode = "split"

    # ------------------------------------------------------------------
    # The code panel docked under the canvas
    # ------------------------------------------------------------------

    @property
    def code_panel(self) -> CodePanel:
        """The :class:`~panel_flowdash.ui.code_panel.CodePanel` docked under the canvas, made on first use."""
        if self._code_panel is None:
            panel = self._code_panel = CodePanel(editor=self, on_close=self.close_code_panel)
            self._code_handle = DockHandle(sizing_mode="stretch_width", height=7, margin=0)
            self._code_handle.param.watch(self._on_code_dock_resized, "size")
            self._sync_code_dock_side()
            panel.param.watch(self._on_code_dock_moved, "dock")
            panel.param.watch(self._sync_code_dock, ["collapsed", "instance_id"])
            panel.param.watch(self._sync_code_panel_node, "instance_id")
            panel.param.watch(self._on_code_drafts, "dirty")
            self._sync_code_spec_view()
            panel.param.watch(self._on_code_spec_view, ["spec_view", "spec_text"])
            # The held drafts change with "dirty" too.
            panel.param.watch(self._sync_stale_marks, ["stale", "dirty"])
        return self._code_panel

    def _sync_code_spec_view(self):
        """Show specs in the code panel in the view the browser last had."""
        preferences = self._split_view.preferences
        self._code_panel.param.update(
            spec_view=preferences.spec_view, spec_text=preferences.spec_text
        )

    def _on_code_spec_view(self, *_events):
        panel = self._code_panel
        self._split_view.preferences.param.update(
            spec_view=panel.spec_view, spec_text=panel.spec_text
        )

    def _code_stale(self, instance_id: str) -> str | None:
        """Return whether the node's result does not match its spec in the code panel: ``"draft"``, ``"running"`` or None."""
        panel = getattr(self, "_code_panel", None)
        return panel.stale_states().get(instance_id) if panel is not None else None

    def _sync_stale_marks(self, *_events):
        """Mark the cards and tiles of the nodes whose result does not match their spec in the code panel."""
        states = self._code_panel.stale_states()
        for instance_id, card in self._node_cards().items():
            stale = states.get(instance_id)
            if card.stale != stale:
                card.stale = stale
        for instance_id, tile in self._inspect_tiles.items():
            tile.set_stale(states.get(instance_id))

    def _sync_code_panel_node(self, event):
        with param.edit_constant(self):
            self.code_panel_node = event.new

    @property
    def code_panel_open(self) -> bool:
        """Whether the code panel shows a node, under the canvas or in the sidecar's Spec tab."""
        return self._code_panel is not None and self._code_panel.instance_id is not None

    def _code_panel_allowed(self) -> bool:
        return self.editable and not self.preview

    def _code_bottom_allowed(self) -> bool:
        """Whether the window is wide enough for the code panel under the canvas."""
        width = self._split_view.storage.window_width
        return (
            self._code_panel_allowed()
            and not self._phone
            and (width is None or width >= _CODE_PANEL_MIN_WIDTH)
        )

    def _code_panel_fits(self) -> bool:
        """Whether the code panel can be shown under the canvas now."""
        showing_canvas = self.mode in ("wiring", "split") and self.surface == "workspace"
        return self._code_bottom_allowed() and showing_canvas

    def _code_placement(self) -> str:
        """Return where the code panel goes: ``"bottom"`` as the browser last chose and where it fits, else ``"sidecar"``."""
        wanted = self._split_view.preferences.code_dock
        return "bottom" if wanted == "bottom" and self._code_panel_fits() else "sidecar"

    def _primary_spec_field(self, instance_id: str) -> str | None:
        """Return the spec field of the node *instance_id* to edit: one holding a draft, else the first."""
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        spec = self._component_specs.get(item["component_id"]) if item else None
        fields = spec_fields(spec) if spec is not None else []
        if self._code_panel is not None:
            drafted = self._code_panel.drafted_fields(instance_id)
            fields = [f for f in fields if f in drafted] + fields
        return fields[0] if fields else None

    def open_code_panel(
        self, instance_id: str, field: str | None = None, *, focus: bool = True
    ) -> bool:
        """Edit the spec field *field* of the node *instance_id* in the code panel.

        *field* defaults to the node's first spec field, a code field or a
        spec dict; a node without one shows its result only. The panel opens
        under the canvas where it was last docked there and fits, otherwise
        in the sidecar's Spec tab, which then shows the node. Returns False,
        leaving the panel as it was, where it cannot be shown: to viewers, or
        for a node or field it cannot edit. Callers then fall back to the
        field's own editor.
        """
        if field is None:
            field = self._primary_spec_field(instance_id)
        if not self._code_panel_allowed() or not self.code_panel.can_edit(instance_id, field):
            return False
        docking = not self.code_panel_open
        self._sync_code_dock_side()
        self.code_panel.open(instance_id, field, focus=focus)
        if self._code_panel.dock == "sidecar":
            self._show_spec_tab(instance_id)
            self._place_code_panel()
        elif not self._code_dock.objects:
            # Added once it shows a field, so the browser builds the editor with its language
            # and text: a change arriving before the code/result split has rendered is lost.
            self._place_code_panel()
            if focus:
                self._code_panel._focus()
        if docking and self._code_panel.dock == "bottom":
            self._keep_in_view(instance_id)
        return True

    def _show_spec_tab(self, instance_id: str):
        """Select the node *instance_id* alone and show its Spec tab in the open sidecar."""
        selection = self._flow.selection or {}
        if selection.get("nodes") != [instance_id] or selection.get("edges"):
            self._select_node(instance_id)
        self._inspector.open_tab(instance_id, SPEC)
        if self._inspector.collapsed:
            self._inspector.fold(False)

    def open_spec_in_sidecar(self, instance_id: str) -> bool:
        """Move the code panel into the sidecar's Spec tab, remembered for the browser, showing *instance_id*."""
        self._split_view.preferences.code_dock = "sidecar"
        self._sync_code_dock_side()
        return self.open_code_panel(instance_id)

    def close_code_panel(self):
        """Close the code panel, holding a draft it shows until the field is opened again."""
        if self.code_panel_open:
            self._code_panel.release()

    def edit_spec(self, instance_id: str, field: str | None = None) -> bool:
        """Open the spec of the node *instance_id* in the code panel, showing the canvas if needed.

        Where the browser keeps the panel in the sidecar, or the window is too
        narrow for it under the canvas, the sidecar's Spec tab shows it in the
        tab already open. Returns False to viewers.
        """
        if not self._code_panel_allowed():
            return False
        if self._split_view.preferences.code_dock == "bottom" and self._code_bottom_allowed():
            self.surface = "workspace"
            if self.mode not in ("wiring", "split"):
                self.mode = "split"
        return self.open_code_panel(instance_id, field)

    def _follow_selection(self, selection: dict | None):
        """Show the node selected alone on the canvas in the open code panel."""
        panel = self._code_panel
        if panel is None or panel.instance_id is None:
            return
        nodes = (selection or {}).get("nodes") or []
        if len(nodes) != 1 or (selection or {}).get("edges") or group_id_of(nodes[0]):
            return
        node = nodes[0]
        if node == panel.instance_id or not panel.can_edit(node):
            return
        panel.open(node, self._primary_spec_field(node), focus=False)
        if panel.dock == "sidecar":
            self._place_code_panel()

    def _sync_code_dock_side(self):
        """Show the code panel where the browser last had it, in the sidecar where it does not fit under the canvas."""
        panel = self._code_panel
        if panel is None:
            return
        self._moving_code_dock = True
        try:
            panel.param.update(
                can_dock_bottom=self._code_bottom_allowed(), dock=self._code_placement()
            )
        finally:
            self._moving_code_dock = False

    def _on_code_dock_moved(self, event):
        if not self._moving_code_dock:
            # Moved with its button, as the person chose.
            self._split_view.preferences.code_dock = event.new
            if event.new == "bottom" and not self._code_panel_fits():
                self.surface = "workspace"
                self.mode = "split"
            elif event.new == "sidecar" and self.code_panel_open:
                self._show_spec_tab(self._code_panel.instance_id)
            self._sync_code_dock_side()
        if self._code_dock.objects or self.code_panel_open:
            self._place_code_panel()
        if self.code_panel_open and self._code_panel.dock == "bottom":
            # The canvas changes shape, so it may zoom out to keep the whole node in view.
            self._keep_in_view(self._code_panel.instance_id, keep_zoom=False)

    def _keep_in_view(self, instance_id: str, keep_zoom: bool = True):
        """Once the canvas has resized, e.g. for the code panel, keep the node in view.

        With *keep_zoom* it pans at the current zoom, otherwise it may zoom out
        to fit the node, never in.
        """
        if not self._flow._models or (doc := pn.state.curdoc) is None:
            return
        zoom = (self._flow.viewport or {}).get("zoom") or 1
        least = zoom if keep_zoom else None
        doc.add_timeout_callback(
            lambda: self._flow.fit_view(instance_id, min_zoom=least, max_zoom=zoom, duration=300),
            # A moved panel renders afresh first.
            200 if keep_zoom else 400,
        )

    @pn.io.hold()
    def _place_code_panel(self):
        """Put the code panel under the canvas or in the sidecar's Spec tab, as its :attr:`dock` says.

        It is in one place at a time, so a node's spec has one editor.
        """
        panel = self._code_panel
        if panel.dock == "sidecar":
            self._code_dock[:] = []
        elif not self._code_dock.objects:
            self._code_dock[:] = [self._code_handle, panel]
        self._inspector.sync_spec_tab()
        self._sync_code_dock()

    @pn.io.hold()
    def _sync_code_dock(self, *_events):
        """Show the code panel under the canvas, at its size for the session or as its header."""
        panel = self._code_panel
        if panel is None:
            return
        shown = self.code_panel_open and panel.dock == "bottom" and self._code_panel_fits()
        collapsed = shown and panel.collapsed
        self._code_handle.visible = shown and not collapsed
        height = HEADER_HEIGHT if collapsed else self._code_dock_height
        # Set as a style: the layout would read a height param as a share of the room.
        self._code_dock.param.update(
            visible=shown, styles={"flex": "none", "height": f"{height}px", "min-height": "0"}
        )
        # Their minimum heights were worked out from the canvas's own while it had the window.
        for layout in (
            self._view,
            self._editor_row,
            self._workspace_area,
            self._split_view.left,
            self._split_view.split,
        ):
            styles = {k: v for k, v in (layout.styles or {}).items() if k != "min-height"}
            if shown:
                styles["min-height"] = "0"
            if styles != (layout.styles or {}):
                layout.styles = styles
        docked = [c for c in self._flow.css_classes if c != "fd-docked"]
        self._flow.param.update(
            min_height=_CANVAS_MIN_HEIGHT_DOCKED if shown else _CANVAS_MIN_HEIGHT,
            css_classes=[*docked, "fd-docked"] if shown else docked,
        )

    def _on_code_dock_resized(self, event):
        if event.new:
            self._code_dock_height = int(event.new)
            if self.code_panel_open and not self._code_panel.collapsed:
                self._sync_code_dock()

    @param.depends("mode", "surface", watch=True)
    def _on_code_dock_tab(self):
        """Move the code panel into the sidecar outside the Dataflow and Split tabs, and back under the canvas there."""
        if self._code_panel is None:
            return
        self._sync_code_dock_side()
        self._sync_code_dock()

    @param.depends("editable", "preview", watch=True)
    def _close_code_panel_if_hidden(self):
        if self.code_panel_open and not self._code_panel_allowed():
            self.close_code_panel()
        allowed = self._code_panel_allowed()
        if allowed != getattr(self, "_code_panel_was_allowed", allowed):
            # The inspector edits specs itself where the panel cannot.
            self._code_panel_was_allowed = allowed
            if getattr(self, "_inspector", None) is not None:
                self._inspector.refresh_node()

    def _on_window_resized(self, _event):
        self._close_code_panel_if_hidden()
        panel = self._code_panel
        under = panel is not None and self.code_panel_open and self._code_dock.visible
        self._sync_code_dock_side()
        self._sidecar.window_resized()
        if under and panel.dock == "sidecar":
            # Narrowed out from under the canvas, the spec being edited moves to the Spec tab.
            self._show_spec_tab(panel.instance_id)
            self._place_code_panel()

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

    def _on_selection_for_sidecar(self, event):
        new = event.new or {}
        if (new.get("nodes") or new.get("edges")) and new != (event.old or {}):
            self._sidecar.selection_made()

    def _on_sidecar_collapsed(self, event):
        self._sidecar.on_collapsed(event.new)
        self._inspector._sync_badge()

    def _on_sidecar_resized(self, event):
        self._split_view.preferences.sidecar_width = event.new
        self._sidecar.sync()

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

    def _rebuild_sidebar(self, entries: dict[str, dict] | None = None):
        """Publish views of the placed components shown beside the grid, in the top bar or sidebar.

        Independent of the wiring/dashboard toggle, so it runs whenever the
        canvas changes rather than only when the tile grid is visible.
        *entries* are the layout entries by instance id, read from the layout
        unless given.
        """
        if entries is None:
            entries = {e["instance_id"]: e for e in self.layout if "instance_id" in e}
        positions = {item["instance_id"]: i for i, item in enumerate(self._tile_items)}
        areas = {}
        for area in (tile_layout.TOP, tile_layout.SIDEBAR):
            views = areas[area] = []
            for item in tile_layout.area_items(self, area, entries):
                i = positions[item["instance_id"]]
                view = self._tile_objects[i] if i < len(self._tile_objects) else None
                if view is None:
                    title = tile_layout.tile_title(self, item)
                    view = markdown_pane(f"*{escape_markdown(title)}*")
                views.append((item["instance_id"], view))
        self._area_views = areas
        self._view_controls.placed = {
            iid: area for area, views in areas.items() for iid, _ in views
        }
        self.sidebar = [view for views in areas.values() for _, view in views]
        if self._view_controls.sync():
            self._place_controls()
        self._sync_tile_drag()
        self._global_filters.apply()
        bar = self._dashboard_bar
        bar.health.track()
        bar.health_popover.sync()

    def _place_controls(self):
        """Place the top bar and sidebar around the grid again, after the sidebar came or went."""
        if self.mode == "split" or not self._grid_on_screen:
            return
        controls = self._view_controls
        placed = (controls._row, controls._left_row, self._tile_grid)
        rest = [o for o in self._workspace_area if not any(o is p for p in placed)]
        self._workspace_area[:] = [*controls.layout(self._tile_grid), *rest]

    @pn.io.hold()
    def _rebuild_tile_grid(self):
        # Capture the layout keyed by instance id before the objects change, so it
        # can be realigned with the tiles that remain.
        layout = self._pending_tile_layout or self.layout
        self._tile_frames = {
            spec["instance_id"]: spec["framed"]
            for spec in layout
            if isinstance(spec.get("framed"), bool)
        }
        entries = {spec["instance_id"]: spec for spec in layout if "instance_id" in spec}
        config = self._pending_grid_config
        if config is None and self._grid_populated:
            config = self._grid_config()
        grid_views, grid_ids = [], []
        self._grid_areas, self._area_entries = {}, {}
        # The grid's objects follow the tiles' display order. TileGrid sorts its
        # tiles by object order when the objects change and only moves them to
        # their layout index afterwards, so other orders leave tiles overlapping.
        positions = range(len(self._tile_items))
        for i in sorted(positions, key=lambda i: _display_rank(entries, self._tile_items[i], i)):
            item = self._tile_items[i]
            instance_id = item["instance_id"]
            area = tile_layout.area_of(self, item, entries)
            if area != tile_layout.GRID:
                if area is not None and instance_id in entries:
                    self._area_entries[instance_id] = (area, dict(entries[instance_id]))
                continue
            stored = entries.get(instance_id, {}).get("area")
            if stored == tile_layout.GRID or (
                "placeholder" in item and stored in tile_layout.AREAS
            ):
                self._grid_areas[instance_id] = stored
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = markdown_pane(f"*{escape_markdown(tile_layout.tile_title(self, item))}*")
            grid_views.append(self._grid_view(item, view))
            grid_ids.append(item["instance_id"])
        # TileGrid keeps the selection on its tile while the objects change, but
        # the instance ids only follow afterwards.
        changed = grid_ids != self._grid_ids
        self._rebuilding_grid = True
        try:
            self._tile_grid[:] = grid_views
        finally:
            self._rebuilding_grid = False
        self._grid_ids = grid_ids
        self._grid_populated = True
        self._rebuild_sidebar(entries)
        # Size hints in pixels convert to the layout's percentages at the restored reference width.
        if config is not None:
            self._apply_grid_config(config)
            self._pending_grid_config = None
        self._set_grid_layout(self._sized_layout(_align_layout(layout, grid_ids)), changed)
        self._pending_tile_layout = []
        self._sync_tile_selection()

    def _set_grid_layout(self, layout: list[dict], changed: bool = False):
        """Give the grid its positional *layout*, after its objects when they *changed*.

        TileGrid applies a layout that arrives with its objects to the tiles it
        showed before (its `objects` handler runs after its `layout` handler),
        so with a tile removed from the middle each later tile would take the
        size of the tile after it. A rendered grid therefore gets its objects
        in one message and the layout in the next; `layout` reads the waiting
        one meanwhile.
        """
        doc = pn.state.curdoc
        # A rebuild in the same message as one that changed the tiles waits as well.
        removed = changed or self._deferred_grid_layout is not None
        if not (removed and doc is not None and self._tile_grid._models):
            self._deferred_grid_layout = None
            self._tile_grid.layout = layout
            return
        self._deferred_grid_layout = layout

        def flush():
            if self._deferred_grid_layout is layout:
                self._deferred_grid_layout = None
                self._tile_grid.layout = layout

        doc.add_next_tick_callback(flush)

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
            grid_layout = self._deferred_grid_layout
            if grid_layout is None:
                grid_layout = self._tile_grid.layout
            keyed = self._without_fill_heights(_key_layout(grid_layout, self._grid_ids))
            ids = {spec["instance_id"] for spec in keyed}
            keyed += [
                {"instance_id": iid}
                for iid in self._grid_ids
                if (iid in self._tile_frames or iid in self._grid_areas) and iid not in ids
            ]
            for spec in keyed:
                if spec["instance_id"] in self._tile_frames:
                    spec["framed"] = self._tile_frames[spec["instance_id"]]
                if spec["instance_id"] in self._grid_areas:
                    spec["area"] = self._grid_areas[spec["instance_id"]]
            # Beside the grid, after its tiles, so each area keeps its order.
            beside = sorted(
                self._area_entries.values(),
                key=lambda pair: (
                    tile_layout.AREAS.index(pair[0]),
                    pair[1].get("index") is None,
                    pair[1].get("index") or 0,
                ),
            )
            keyed += [
                {**entry, "index": len(self._grid_ids) + rank}
                for rank, (_, entry) in enumerate(beside)
            ]
            return self._placed_layout(keyed)
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
                self.trigger_refresh, period=period_ms
            )
            self._refresh_ms = period_ms
            self._refresh_started = time.time()

    def _stop_refresh_timer(self):
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
        self._refresh_ms = 0

    def trigger_refresh(self):
        """Trigger `refresh` with every change it causes delivered as one propagation pass.

        A node fed by two sources that both run again then runs once, with
        both new values, instead of once per source.
        """
        with self._dataflow_graph.propagation():
            self.param.trigger("refresh")

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

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    @property
    def groups(self) -> dict[str, DashboardGroup]:
        """The groups on the canvas by id, as copies, nodes by instance id.

        See :mod:`panel_flowdash.groups`.
        """
        return copy_groups(self._groups)

    def group_of(self, instance_id: str) -> str | None:
        """Return the id of the group a node is in, or None."""
        return next((g.id for g in self._groups.values() if instance_id in g.nodes), None)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    @property
    def report(self) -> dict[str, t.Any] | None:
        """The dashboard's report as a copy, nodes by instance id, or None if it has none.

        Blocks referring to nodes, groups or turns the dashboard no longer has
        are left out. A dashboard without a report reads as the one
        :func:`~panel_flowdash.report.report_blocks` derives from its layout.
        See :mod:`panel_flowdash.report`.
        """
        return map_report(
            self._report,
            {item["instance_id"]: item["instance_id"] for item in self._tile_items},
            set(self._groups),
            {turn.id for turn in self._turn_log.turns},
        )

    def set_report(
        self,
        report: t.Mapping[str, t.Any] | None,
        *,
        label: str | None = None,
        coalesce: bool = True,
    ):
        """Replace the dashboard's report, nodes by instance id, as one undoable edit.

        None removes it, so the dashboard reads as its derived report again.
        *label* describes the edit in the history, "Changed the report" by
        default. A pending turn the report no longer refers to goes with its
        block, as it was only ever asked in the report. With *coalesce* False
        the edit is a version of its own, never batched with the quick edits
        before or after it, as for a structural change such as moving a block.

        Raises
        ------
        ValueError
            If *report* is malformed or refers to a node, group or turn the
            dashboard does not have.
        """
        checked = None if report is None else check_report(report)
        if checked is not None:
            known = map_report(
                checked,
                {item["instance_id"]: item["instance_id"] for item in self._tile_items},
                set(self._groups),
                {turn.id for turn in self._turn_log.turns},
            )
            if known != checked:
                missing = next(
                    (i, b) for i, b in enumerate(checked["blocks"]) if b not in known["blocks"]
                )
                raise ValueError(
                    f"blocks[{missing[0]}]: refers to something the dashboard does not have."
                )
        if checked == self.report:
            return
        dropped = report_turns(self._report) - report_turns(checked)
        self._report = checked
        if dropped:
            self._turn_log.drop_pending(dropped)
        if not coalesce:
            self._history.flush()
        self._record_edit(label or "Changed the report", op="set_report")
        if not coalesce:
            self._history.flush()

    def customize_report(self) -> bool:
        """Make the report derived from the dashboard its own, to be edited, and return whether it did.

        The derived blocks are written into the report as one undoable edit;
        the title stays the dashboard's until it is changed. A dashboard that
        already has a report keeps it.
        """
        if self._report is not None:
            return False
        self.set_report(
            {"blocks": report_blocks(self).blocks}, label="Customized the report", coalesce=False
        )
        return True

    def ask_in_report(self, question: str, index: int | None = None) -> str:
        """Add a question to the report, not run yet, before the block at *index* (last for None).

        The question becomes a pending turn and a turn block showing it, in
        one undoable edit, and its id is returned. A dashboard without a
        report of its own gets the derived one first.

        Raises
        ------
        ValueError
            If *question* is empty or *index* is not a place in the report.
        """
        turn = self._turn_log.add_pending(question)
        current = self.report
        if current is None:
            current = {"blocks": report_blocks(self).blocks}
        try:
            self.set_report(
                insert_block(current, index, {"turn": turn.id}),
                label="Asked a question in the report",
                coalesce=False,
            )
        except Exception:
            self._turn_log.drop_pending({turn.id})
            raise
        return turn.id

    def group_nodes(
        self,
        instance_ids: t.Iterable[str],
        label: str | None = None,
        *,
        group_id: str | None = None,
        collapsed: bool = False,
    ) -> str:
        """Frame nodes together on the canvas as one group, as one edit, and return its id.

        The nodes keep their place, config, connections and tiles. A group
        can be collapsed into one node showing the ports its connections
        cross, see :meth:`collapse_group`.

        Raises
        ------
        ValueError
            If there are no nodes, a node is not on the canvas or already in a
            group, or *group_id* is taken or not a valid name.
        """
        ids = list(dict.fromkeys(instance_ids))
        if not ids:
            raise ValueError("Select at least one component to group.")
        placed = {item["instance_id"] for item in self._tile_items}
        for instance_id in ids:
            if instance_id not in placed:
                raise ValueError(f"No component {instance_id!r} on the canvas.")
            if (current := self.group_of(instance_id)) is not None:
                raise ValueError(
                    f"{self._node_name(instance_id)!r} is already in group {current!r}; "
                    "ungroup it first."
                )
        label = (label or "").strip()[:MAX_LABEL_LENGTH] or self._next_group_label()
        if group_id is None:
            group_id = unique_group_id(label, self._groups)
        else:
            check_group_id(group_id)
            if group_id in self._groups:
                raise ValueError(f"A group named {group_id!r} already exists.")
        group = DashboardGroup(id=group_id, label=label, nodes=ids, collapsed=collapsed)
        self._groups[group_id] = group
        self._draw_group(group)
        self._record_edit(
            f"Grouped {len(ids)} component{'s' * (len(ids) != 1)} as {group.title}",
            op="group_nodes",
            args={"name": group_id, "nodes": [self._node_name(i) for i in ids]},
        )
        return group_id

    def ungroup(self, group_id: str) -> list[str]:
        """Remove a group's frame, keeping its nodes where they are, as one edit.

        Returns the instance ids of the nodes that were in it.
        """
        group = self._require_group(group_id)
        self._erase_group(group_id)
        self._record_edit(f"Ungrouped {group.title}", op="ungroup", args={"name": group_id})
        return list(group.nodes)

    def rename_group(self, group_id: str, label: str | None):
        """Change the label a group's header shows, as one edit."""
        group = self._require_group(group_id)
        label = (label or "").strip()[:MAX_LABEL_LENGTH] or None
        if label == group.label:
            return
        group.label = label
        with self._muted_canvas():
            self._flow.patch_node_props(group_node_id(group_id), {"label": group.title})
        self._record_edit(
            f"Renamed group {group_id} to {group.title}",
            op="rename_group",
            args={"name": group_id},
        )
        self._refresh_group_inspector(group_id)

    def collapse_group(self, group_id: str, collapsed: bool = True):
        """Draw a group as one node with its ports, or expand it again, as one edit.

        Expanding pushes the nodes to its right and below aside by the room the
        expanded group needs, and collapsing moves back the nodes it pushed
        that have not been moved since.
        """
        group = self._require_group(group_id)
        if group.collapsed == collapsed:
            return
        with self._muted_canvas():
            self._flow.collapse_group(group_node_id(group_id), collapsed)
        self._on_group_toggled_to(group, collapsed)

    def expand_group(self, group_id: str):
        """Expand a collapsed group, see :meth:`collapse_group`."""
        self.collapse_group(group_id, False)

    @staticmethod
    def _group_node_id(group_id: str) -> str:
        return group_node_id(group_id)

    def _next_group_label(self) -> str:
        titles = {group.title for group in self._groups.values()}
        n = 1
        while f"Group {n}" in titles:
            n += 1
        return f"Group {n}"

    def _require_group(self, group_id: str) -> DashboardGroup:
        group = self._groups.get(group_id)
        if group is None:
            raise KeyError(f"No group named {group_id!r}.")
        return group

    def _on_group_toggled(self, event):
        if self._muted:
            return
        group_id = group_id_of(event.get("node_id", "") if isinstance(event, dict) else "")
        group = self._groups.get(group_id) if group_id else None
        if group is not None and group.collapsed != bool(event.get("collapsed")):
            self._on_group_toggled_to(group, bool(event.get("collapsed")))

    def _on_group_toggled_to(self, group: DashboardGroup, collapsed: bool):
        group.collapsed = collapsed
        if collapsed:
            self._pull_back(group.id)
        else:
            self._push_aside(group)
        self._record_edit(
            f"{'Collapsed' if collapsed else 'Expanded'} {group.title}",
            op="collapse_group",
            args={"name": group.id, "collapsed": collapsed},
        )
        self._refresh_group_inspector(group.id)

    def _refresh_group_inspector(self, group_id: str):
        inspector = getattr(self, "_inspector", None)
        if inspector is not None and inspector._shown == ("group", group_id):
            inspector.refresh(force=True)

    def _group_ports(self, group: DashboardGroup):
        edges = [
            (e["source"], e.get("sourceHandle"), e["target"], e.get("targetHandle"))
            for e in self._flow.edges
            if group_id_of(e["source"]) is None and group_id_of(e["target"]) is None
        ]
        return group_ports(group.nodes, edges, self._port_info)

    def _port_info(self, instance_id: str, port: str, direction: str) -> tuple[str, str | None]:
        spec = self._component_specs.get(self._component_id_for_instance(instance_id) or "")
        ports = (spec.inputs if direction == "input" else spec.outputs) if spec else []
        match = next((p for p in ports if p.name == port), None)
        if match is None:
            return port, None
        return port_label(match), match.type

    def _draw_group(self, group: DashboardGroup):
        """Put *group*'s frame on the canvas around its nodes."""
        ports = self._group_ports(group)
        with self._muted_canvas():
            node_id = self._flow.group_nodes(
                group.nodes,
                group_id=group_node_id(group.id),
                label=group.title,
                padding=_GROUP_PADDING,
                data=group_node_data(group, *ports),
            )
            self._flow.patch_node_props(node_id, {"className": group_class(*ports)})

    def _erase_group(self, group_id: str):
        """Take *group_id*'s frame off the canvas and forget it, keeping its nodes."""
        self._groups.pop(group_id, None)
        self._pushed.pop(group_id, None)
        node_id = group_node_id(group_id)
        if any(node["id"] == node_id for node in self._flow.nodes):
            with self._muted_canvas():
                self._flow.ungroup(node_id)

    def _erase_canvas_groups(self):
        """Take every group frame off the canvas, so all nodes sit at the top level."""
        frames = [node["id"] for node in self._flow.nodes if is_group_node(node)]
        with self._muted_canvas():
            for node_id in frames:
                self._flow.ungroup(node_id)

    def _draw_groups(self, groups: t.Iterable[DashboardGroup]):
        """Make *groups* the canvas's groups, leaving out nodes that are not placed."""
        placed = {item["instance_id"] for item in self._tile_items}
        self._groups = {}
        for group in groups:
            group = copy.deepcopy(group)
            group.nodes = [n for n in group.nodes if n in placed and self.group_of(n) is None]
            if not group.nodes:
                continue
            self._groups[group.id] = group
            self._draw_group(group)
        self._pushed = {g: pushed for g, pushed in self._pushed.items() if g in self._groups}

    def _sync_group_ports(self, *_events):
        """Give each group the ports of the connections that now cross its border."""
        for group in self._groups.values():
            ports = self._group_ports(group)
            data = group_node_data(group, *ports)
            node_id = group_node_id(group.id)
            node = next((n for n in self._flow.nodes if n["id"] == node_id), None)
            if node is None:
                continue
            current = node.get("data") or {}
            patch = {k: v for k, v in data.items() if k != "collapsed" and current.get(k) != v}
            with self._muted_canvas():
                if patch:
                    self._flow.patch_node_data(node_id, patch)
                if node.get("className") != (name := group_class(*ports)):
                    self._flow.patch_node_props(node_id, {"className": name})

    def _drop_from_groups(self, instance_id: str):
        """Take a removed node out of its group, removing a group left empty."""
        for group in list(self._groups.values()):
            if instance_id in group.nodes:
                group.nodes.remove(instance_id)
                if not group.nodes:
                    self._erase_group(group.id)

    def _on_nodes_ungrouped(self, event):
        """Forget a group the page ungrouped, e.g. when its frame is deleted."""
        if self._muted:
            return
        group_id = group_id_of(event.get("group_id", "") if isinstance(event, dict) else "")
        group = self._groups.pop(group_id, None) if group_id else None
        if group is None:
            return
        self._pushed.pop(group_id, None)
        self._record_edit(f"Ungrouped {group.title}", op="ungroup", args={"name": group_id})

    def delete_group(self, group_id: str) -> list[str]:
        """Remove a group and its nodes from the dashboard; returns the removed instance ids."""
        nodes = list(self._require_group(group_id).nodes)
        # Forgotten first, so removing the frame is not taken for ungrouping it.
        self._groups.pop(group_id)
        self._pushed.pop(group_id, None)
        self._flow.remove_node([group_node_id(group_id), *nodes])
        return nodes

    def _on_group_frame_deleted(self, group_id: str):
        """Forget a group whose frame was deleted on the canvas, which deletes its nodes too."""
        group = self._groups.pop(group_id, None)
        self._pushed.pop(group_id, None)
        if group is not None:
            self._record_edit(
                f"Removed group {group.title}", op="remove_group", args={"name": group_id}
            )

    def _absolute_positions(self) -> dict[str, dict[str, float]]:
        """Return the canvas position of every component node, adding up group offsets."""
        nodes = {node["id"]: node for node in self._flow.nodes}
        positions = {}
        for node_id, node in nodes.items():
            if is_group_node(node):
                continue
            position = dict(node.get("position") or {"x": 0, "y": 0})
            parent = nodes.get(node.get("parentId")) if node.get("parentId") else None
            if parent is not None:
                origin = parent.get("position") or {}
                position = {
                    "x": position.get("x", 0) + origin.get("x", 0),
                    "y": position.get("y", 0) + origin.get("y", 0),
                }
            positions[node_id] = position
        return positions

    def _node_size(self, node_id: str) -> tuple[float, float]:
        node = next((n for n in self._flow.nodes if n["id"] == node_id), None)
        view = node.get("view") if node else None
        return CARD_WIDTH, view.height if isinstance(view, NodeCard) else card_height(1, 1)

    def _group_box(self, group: DashboardGroup, positions) -> tuple[float, float, float, float]:
        """Return the expanded frame of *group* as (x0, y0, x1, y1), from its nodes' cards."""
        boxes = []
        for instance_id in group.nodes:
            if instance_id in positions:
                w, h = self._node_size(instance_id)
                p = positions[instance_id]
                boxes.append((p["x"], p["y"], p["x"] + w, p["y"] + h))
        pad = _GROUP_PADDING
        return (
            min(b[0] for b in boxes) - pad,
            min(b[1] for b in boxes) - pad - pr.base.GROUP_HEADER_HEIGHT,
            max(b[2] for b in boxes) + pad,
            max(b[3] for b in boxes) + pad,
        )

    def _push_aside(self, group: DashboardGroup):
        """Move the top-level nodes to the right of and below an expanded group out of its way."""
        positions = self._absolute_positions()
        if not any(n in positions for n in group.nodes):
            return
        x0, y0, x1, y1 = self._group_box(group, positions)
        inputs, outputs = self._group_ports(group)
        collapsed_h = card_height(len(inputs), len(outputs)) + pr.base.GROUP_HEADER_HEIGHT
        members = set(group.nodes)
        frames = {group_node_id(g.id): g for g in self._groups.values() if g.id != group.id}
        candidates = []
        for node in self._flow.nodes:
            if (
                node.get("parentId")
                or node["id"] in members
                or node["id"] == group_node_id(group.id)
            ):
                continue
            position = node.get("position") or {}
            if node["id"] in frames:
                other = frames[node["id"]]
                ox0, oy0, ox1, oy1 = self._group_box(other, positions)
                box = (position.get("x", 0), position.get("y", 0), ox1 - ox0, oy1 - oy0)
            else:
                w, h = self._node_size(node["id"])
                box = (position.get("x", 0), position.get("y", 0), w, h)
            candidates.append((node["id"], box))
        # The nodes beside the group move right together, and the ones below
        # it move down together, by just enough to clear the expanded frame.
        gap = _GROUP_PADDING
        beside = [
            (node_id, box)
            for node_id, box in candidates
            if box[0] >= x0 + CARD_WIDTH and box[1] < y1 and box[1] + box[3] > y0
        ]
        below = [
            (node_id, box)
            for node_id, box in candidates
            if (node_id, box) not in beside
            and box[1] >= y0 + collapsed_h
            and box[0] < x1
            and box[0] + box[2] > x0
        ]
        dx = max([x1 + gap - box[0] for _, box in beside] + [0])
        dy = max([y1 + gap - box[1] for _, box in below] + [0])
        pushed: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
        for nodes, (sx, sy) in ((beside, (dx, 0)), (below, (0, dy))):
            if not (sx or sy):
                continue
            for node_id, (x, y, _w, _h) in nodes:
                pushed[node_id] = (
                    (round(x + sx, 1), round(y + sy, 1)),
                    (round(x, 1), round(y, 1)),
                )
        if not pushed:
            return
        with self._muted_canvas():
            for node_id, ((x, y), _) in pushed.items():
                self._flow.patch_node_props(node_id, {"position": {"x": x, "y": y}})
        self._pushed[group.id] = pushed

    def _pull_back(self, group_id: str):
        """Move back the nodes expanding *group_id* pushed aside, unless they were moved since."""
        pushed = self._pushed.pop(group_id, {})
        if not pushed:
            return
        current = {n["id"]: n.get("position") or {} for n in self._flow.nodes}
        with self._muted_canvas():
            for node_id, ((x, y), (ox, oy)) in pushed.items():
                position = current.get(node_id)
                if position is None or (
                    round(position.get("x", 0), 1),
                    round(position.get("y", 0), 1),
                ) != (x, y):
                    continue
                self._flow.patch_node_props(node_id, {"position": {"x": ox, "y": oy}})

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    @property
    def parameters(self) -> dict[str, DashboardParameter]:
        """The parameters of the open dashboard by name, as copies, nodes by instance id.

        Parameters on nodes the user may not use are left out. See
        :mod:`panel_flowdash.parameters`.
        """
        hidden = self._private_ids()
        placed = {item["instance_id"] for item in self._tile_items}
        return {
            name: copy.deepcopy(parameter)
            for name, parameter in self._parameters.items()
            if parameter.node in placed and parameter.node not in hidden
        }

    def _private_ids(self) -> set[str]:
        return {iid for iid, placeholder in self._placeholders().items() if placeholder.private}

    def _visible_parameter(self, name: str) -> DashboardParameter:
        """Return the parameter *name*, or raise ParameterError if the user cannot see it."""
        visible = self.parameters
        if name not in visible:
            raise ParameterError(
                f"parameters.{name}", "is not a parameter." + suggest_name(name, visible)
            )
        return self._parameters[name]

    def declare_parameter(
        self,
        name: str,
        node: str,
        field: str,
        type: str = "value",
        description: str | None = None,
    ) -> DashboardParameter:
        """Declare a parameter on a config field of a node, or redeclare one, and return it.

        The parameter's ``expects`` is computed by the contract of the node's
        component, if it has one (``register(contract=...)``).

        Parameters
        ----------
        name
            The parameter's name.
        node
            The instance id of the node.
        field
            The config field, or a dotted path into one, e.g. ``spec.tables.orders``.
        type
            One of :data:`~panel_flowdash.parameters.PARAMETER_TYPES`.
        description
            What the parameter is, for whoever binds it.

        Raises
        ------
        ParameterError
            If the name, field or type is malformed, the node does not exist,
            is one the user may not use, or the field is not one of its
            component's config fields, or the name is taken by a parameter
            the user may not see.
        """
        check_name(name)
        if name in self._parameters and name not in self.parameters:
            raise ParameterError("name", f"{name!r} is taken; choose another name.")
        parameter = DashboardParameter.from_dict(
            {"node": node, "field": field, "type": type, "description": description}
        )
        component_id, parameter.field = self._parameter_field(node, field)
        before = self._parameters.get(name), self._parameter_components.get(name)
        self._parameters[name] = parameter
        self._parameter_components[name] = component_id
        try:
            self._compute_expects([name])
        except BaseException:
            self._restore_parameter(name, *before)
            raise
        self._record_edit(
            f"Declared parameter {name} on {self._node_name(node)}",
            op="declare_parameter",
            args={"name": name},
        )
        return copy.deepcopy(self._parameters[name])

    def _restore_parameter(self, name: str, parameter, component_id):
        if parameter is None:
            self._parameters.pop(name, None)
            self._parameter_components.pop(name, None)
        else:
            self._parameters[name] = parameter
            self._parameter_components[name] = component_id

    def _parameter_field(self, instance_id: str, field: str) -> tuple[str, str]:
        """Return the component of a node a parameter may point at, and the field with its current name."""
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        if item is None or "placeholder" in item:
            raise ParameterError("node", f"names no node on the canvas: {instance_id!r}.")
        spec = self._component_specs.get(item["component_id"])
        if spec is None or not self._component_authorized(item["component_id"]):
            raise ParameterError("node", f"{instance_id!r} is not a component you may use.")
        keys = field_path(field)
        first = spec.resolve_name(str(keys[0]))
        names = [f.name for f in spec.config]
        if first not in names:
            raise ParameterError(
                "field",
                f"{spec.title} has no config field {keys[0]!r}."
                + (f" Its config fields are: {', '.join(names)}." if names else ""),
            )
        return item["component_id"], ".".join([first, *map(str, keys[1:])])

    def remove_parameter(self, name: str) -> bool:
        """Remove a parameter; the node keeps the value its field holds.

        Returns whether it existed; a parameter the user may not see counts as missing.
        """
        if name not in self.parameters:
            return False
        del self._parameters[name]
        self._parameter_components.pop(name, None)
        self._record_edit(f"Removed parameter {name}", op="remove_parameter", args={"name": name})
        return True

    def _note_parameter_nodes(self):
        """Record the component each parameter's node has now, so a replaced node drops it."""
        components = {item["instance_id"]: item["component_id"] for item in self._tile_items}
        self._parameter_components = {
            name: components[parameter.node]
            for name, parameter in self._parameters.items()
            if parameter.node in components
        }
        self._prune_parameters()

    def _prune_parameters(self):
        """Drop the parameters whose node was removed or replaced, or whose field no longer exists."""
        items = {item["instance_id"]: item for item in self._tile_items}
        for name, parameter in list(self._parameters.items()):
            item = items.get(parameter.node)
            if item is not None and "placeholder" in item:
                continue
            spec = self._component_specs.get(item["component_id"]) if item is not None else None
            if (
                item is None
                or spec is None
                or self._parameter_components.get(name) != item["component_id"]
                or str(field_path(parameter.field)[0]) not in {f.name for f in spec.config}
            ):
                del self._parameters[name]
                self._parameter_components.pop(name, None)

    def _contract(self, component_id: t.Any) -> t.Any:
        if not isinstance(component_id, str):
            return None
        component_id = self._resolve_component_id(component_id, self._component_aliases())
        spec = self._component_specs.get(component_id)
        return spec.contract if spec is not None else None

    def _compute_expects(self, names: t.Iterable[str] | None = None, missing_only: bool = False):
        """Record what the contracts of the parameters *names*, by default all, expect now.

        A parameter whose contract cannot tell keeps what it recorded. With
        *missing_only* only parameters that recorded nothing are computed.
        """
        from panel_flowdash.agent.yaml_format import dashboard_to_dict

        names = list(self._parameters if names is None else names)
        if missing_only:
            names = [n for n in names if self._parameters[n].expects is None]
        if not names:
            return
        doc = dashboard_to_dict(export_model(self.to_model()))
        names = [n for n in names if n in (doc.get("parameters") or {})]
        for name, expects in compute_expects(doc, names, self._contract, self.context).items():
            self._parameters[name].expects = expects

    def parameters_schema(self) -> dict[str, t.Any]:
        """Return the JSON Schema of a form binding the parameters, each defaulting to its field's value.

        See :func:`~panel_flowdash.parameters.parameters_schema`.
        """
        from panel_flowdash.config_check import node_config

        values = {}
        for name, parameter in self.parameters.items():
            item = next(i for i in self._tile_items if i["instance_id"] == parameter.node)
            spec = self._component_specs[item["component_id"]]
            try:
                values[name] = get_field(node_config(self, parameter.node, spec), parameter.field)
            except ParameterError:
                continue
        return parameters_schema(self.parameters, values)

    def check_parameters(self, values: t.Mapping[str, t.Any]) -> dict[str, list[ContractProblem]]:
        """Return what the contracts find wrong with binding *values*, without binding them.

        Includes warnings, see :class:`~panel_flowdash.parameters.ContractProblem`.

        Raises
        ------
        ParameterError
            If a name is not a parameter or a value is not of its type.
        """
        _, problems = self._bound(values)
        return problems

    def _bound(
        self, values: t.Mapping[str, t.Any]
    ) -> tuple[dict, dict[str, list[ContractProblem]]]:
        from panel_flowdash.agent.yaml_format import dashboard_to_dict

        for name in values:
            self._visible_parameter(name)
        doc = dashboard_to_dict(export_model(self.to_model()))
        bound = bind_document(doc, values)
        return bound, check_contracts(bound, list(values), self._contract, self.context)

    def bind_parameters(
        self, values: t.Mapping[str, t.Any], *, timeout: float | None = None
    ) -> dict[str, list[ContractProblem]]:
        """Write values into their parameters' fields, as one edit, after checking them.

        Each value is read as its parameter's type, so text such as ``"10"``
        for a number parameter is accepted. Before anything changes, the
        contract of each parameter's component checks the value against what
        the parameter recorded, the node's config takes the value, and the
        component's config check (``register(check_config=...)``) passes the
        result. Values that change nothing record no edit. Returns the
        warnings of the contracts.

        Raises
        ------
        ParameterError
            If a name is not a parameter the user can see, a value is not of
            its type or not one the field takes, or a config check refuses it;
            nothing is changed.
        ContractError
            If a contract refuses a value; nothing is changed. Its problems
            name the nodes that would fail.
        """
        patches, problems, _ = self._binding(values, timeout=timeout)
        self._apply_binding(values, patches)
        return problems

    def _binding(
        self, values: t.Mapping[str, t.Any], *, timeout: float | None = None
    ) -> tuple[dict[str, dict[str, t.Any]], dict[str, list[ContractProblem]], list[str]]:
        """Return the config patch binding *values* makes to each node, the contracts' and checks' warnings.

        Raises as :meth:`bind_parameters` does.
        """
        from panel_flowdash.agent.validation import _check_value
        from panel_flowdash.agent.yaml_format import node_names
        from panel_flowdash.config_check import CHECK_TIMEOUT, check_node_sync, node_config

        if not values:
            return {}, {}, []
        bound, problems = self._bound(values)
        if refused := failures(problems):
            raise ContractError(refused)
        names = node_names(self._tile_items)
        bound_nodes = bound.get("nodes") or {}
        patches: dict[str, dict[str, t.Any]] = {}
        owners: dict[tuple[str, str], str] = {}
        for name in values:
            parameter = self._parameters[name]
            key = str(field_path(parameter.field)[0])
            config = bound_nodes[names[parameter.node]]["config"]
            item = next(i for i in self._tile_items if i["instance_id"] == parameter.node)
            spec = self._component_specs[item["component_id"]]
            if node_config(self, parameter.node, spec).get(key) == config[key]:
                continue
            state = self._dataflow_graph.get_config_state(parameter.node)
            p = state.param.objects("existing").get(key) if state is not None else None
            if p is not None and (reason := _check_value(p, config[key])):
                raise ParameterError(
                    f"parameters.{name}", f"{names[parameter.node]}.{parameter.field}: {reason}"
                )
            patches.setdefault(parameter.node, {})[key] = copy.deepcopy(config[key])
            owners[(parameter.node, key)] = name
        warnings: list[str] = []
        for instance_id, patch in patches.items():
            result = check_node_sync(
                self, instance_id, patch, timeout=CHECK_TIMEOUT if timeout is None else timeout
            )
            if result:
                name = owners[(instance_id, next(iter(patch)))]
                raise ParameterError(
                    f"parameters.{name}",
                    f"The config of '{names[instance_id]}' was refused: " + "; ".join(result),
                )
            warnings.extend(result.warnings)
        return patches, problems, warnings

    def _apply_binding(self, values: t.Mapping[str, t.Any], patches: dict[str, dict[str, t.Any]]):
        if not patches:
            return
        from panel_flowdash.config_check import node_config

        before = {}
        for instance_id, patch in patches.items():
            item = next(i for i in self._tile_items if i["instance_id"] == instance_id)
            spec = self._component_specs[item["component_id"]]
            config = node_config(self, instance_id, spec)
            before[instance_id] = {key: copy.deepcopy(config.get(key)) for key in patch}
        applied = []
        for instance_id, patch in patches.items():
            if set(self._set_config_state(instance_id, patch)) != set(patch):
                for done in [*applied, instance_id]:
                    self._set_config_state(done, before[done])
                raise ParameterError(
                    "value", f"The config of '{self._node_name(instance_id)}' refused the values."
                )
            applied.append(instance_id)
        self._record_edit(
            f"Bound {', '.join(values)}",
            op="bind_parameters",
            args={"names": sorted(values)},
        )

    def _kept_parameters(
        self, parameters: dict[str, DashboardParameter]
    ) -> dict[str, DashboardParameter]:
        """Return the parameters of a change to load, with ``expects`` only where the canvas recorded it.

        A change cannot set what a contract expects; a parameter that points
        where it pointed before keeps what it recorded, any other is computed
        again once loaded. Parameters of nodes the user may not see stay as
        they are, whatever the change says under their names.
        """
        kept = {}
        for name, parameter in parameters.items():
            parameter = copy.deepcopy(parameter)
            current = self._parameters.get(name)
            same = current is not None and (current.node, current.field) == (
                parameter.node,
                parameter.field,
            )
            parameter.expects = copy.deepcopy(current.expects) if same else None
            kept[name] = parameter
        private = self._private_ids()
        for name, parameter in self._parameters.items():
            if parameter.node in private:
                kept[name] = copy.deepcopy(parameter)
        return kept

    def end_turn(self):
        """End the turn of the person's edits, so their next edit starts a new turn.

        Call it when the person sends a request to an agent, which the
        FlowDash assistant does, so the edits made before and after the
        request form separate turns.
        """
        self._turn_log.end_user_turn()

    def end_agent_turns(self, run_id: str | None = None):
        """End the turns of the agent's requests, so the next request's edits start a new turn.

        The dashboard tools call it when a request ends, whether it finished,
        failed or was cancelled. Each request's edits then become one version
        of the edit history. With *run_id* only that request's turn ends, so
        another agent's request running at the same time keeps its own.
        """
        self._release_tile_layout(run_id)
        self._turn_log.end_agent_turns(run_id)
        self._history.end_requests(run_id)
        self._fit_placed_nodes()

    def _discard_agent_turn(self, run_id: str | None):
        """Drop the open turn of request *run_id* and its pending version, once its edits were taken back."""
        self._turn_log.discard(run_id)
        self._history.drop_requests(run_id)

    def _defer_tile_layout(self, run_id: str | None = None):
        """Lay out the tiles added from now on once the agent's request *run_id* ends, all at once.

        Laying the grid out again for each tile the agent adds would rebuild
        every tile on screen once per tile.
        """
        if self._deferred_tiles is None:
            self._deferred_tiles = set()
        self._deferring.add(run_id)

    def _release_tile_layout(self, run_id: str | None = None):
        """Lay out the tiles added while the layout was deferred.

        The layout is an edit of the agent's request *run_id*, by default the
        one request with edits not yet in the history, so it joins that
        request's version. While another request still defers the layout,
        only the tiles *run_id* added are laid out.
        """
        if run_id is not None and self._deferring - {run_id}:
            if run_id not in self._deferring:
                return
            self._deferring.discard(run_id)
            pending = self._deferred_by_run.pop(run_id, set())
            rest = (self._deferred_tiles or set()) - pending
            self._deferred_tiles = None
            try:
                self._lay_out_released(pending, False, run_id)
            finally:
                self._deferred_tiles = rest
            return
        pending, self._deferred_tiles = self._deferred_tiles, None
        rearrange, self._deferred_rearrange = self._deferred_rearrange, False
        self._deferring.clear()
        self._deferred_by_run.clear()
        if pending is None:
            return
        self._lay_out_released(pending, rearrange, run_id)

    def _lay_out_released(self, pending: set[str], rearrange: bool, run_id: str | None):
        shown = {item["instance_id"] for item in tile_layout.grid_tiles(self)}
        pending &= shown
        if not (pending or rearrange):
            return
        if self._all_tiles_automatic(pending):
            self._arrange_tiles(self._plumbing(pending))
        else:
            self._place_new_tiles(pending)
        run_id = run_id or self._history.request_run()
        if run_id is None:
            self._mark_dirty()
        else:
            # No label: the edits that added the tiles describe the turn already.
            with agent_edits(run_id):
                self._record_edit(op="arrange_layout")
        self._layout_released = True

    def _fit_placed_nodes(self):
        """Fit the canvas to its nodes after the editor placed some, while the canvas is shown."""
        if self._fit_pending and (not self._grid_on_screen or self.mode == "split"):
            self._fit_pending = False
            self._flow.fit_view()

    # ------------------------------------------------------------------
    # Global filters
    # ------------------------------------------------------------------

    @property
    def filters(self) -> list[dict[str, t.Any]]:
        """The dashboard's global filters as a copy, the views they exclude by instance id.

        See :mod:`panel_flowdash.global_filters`.
        """
        return map_filters(
            self._global_filters.filters,
            {item["instance_id"]: item["instance_id"] for item in self._tile_items},
        )

    def set_filters(self, filters: t.Iterable[t.Mapping[str, t.Any]], *, label: str | None = None):
        """Replace the dashboard's global filters, views they exclude by instance id, as one undoable edit.

        Raises
        ------
        ValueError
            If *filters* are malformed or exclude a node the dashboard does not have.
        """
        checked = check_filters(list(filters))
        placed = {item["instance_id"] for item in self._tile_items}
        for i, entry in enumerate(checked):
            missing = [n for n in entry.get("exclude", ()) if n not in placed]
            if missing:
                raise ValueError(f"filters[{i}].exclude: names no node: {missing[0]!r}.")
        if checked == self.filters:
            return
        self._global_filters.load(checked)
        # Each change is a version of its own, so undo takes back one opt-out, not a burst.
        self._history.flush()
        self._record_edit(label or "Changed the filters", op="set_filters")
        self._history.flush()

    def set_filter_value(self, column: str, value: t.Any):
        """Change what the global filter on *column* keeps for whoever is viewing, without an edit.

        *value* is a list of values for a ``select`` filter, ``[start, end]``
        for a ``range``, or None to keep every row.
        """
        self._global_filters.set_value(column, value)

    def exclude_from_filter(self, column: str, instance_id: str, excluded: bool = True):
        """Opt the view *instance_id* out of the global filter on *column*, or back in."""
        filters = self.filters
        entry = next((f for f in filters if f["column"] == column), None)
        if entry is None:
            raise KeyError(f"The dashboard has no filter on {column!r}.")
        exclude = [n for n in entry.get("exclude", ()) if n != instance_id]
        if excluded:
            exclude.append(instance_id)
        entry.pop("exclude", None)
        if exclude:
            entry["exclude"] = exclude
        title = self._instance_title(instance_id)
        action = f"Excluded {title} from" if excluded else f"Included {title} in"
        self.set_filters(filters, label=f"{action} the {column} filter")

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
        if not self._hydrating:
            self._prune_parameters()
            self._edit_count += 1
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
            self.param.trigger("edited")
        return turn

    def _note_turn(self, *labels: str) -> DashboardTurn:
        turn = self._turn_log.record(*labels)
        self._turn_log.prune(set(self._origins.values()) | report_turns(self._report))
        return turn

    def _on_dirty(self, event):
        # Code that marks the canvas changed without `_record_edit` still opens a turn.
        if event.new and not self._recording_edit and not self._hydrating:
            self._edit_count += 1
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

    def node_id(self, component: t.Any) -> str | None:
        """Return the id of the node *component* was created for, or None if it is not one of this editor's.

        Only components that are ``Viewer`` classes are known by instance;
        a function component can call :func:`current_node_id` from its body.
        """
        return next(
            (nid for nid, inst in self._component_instances.items() if inst is component), None
        )

    def set_default(self, instance_id: str, config: t.Mapping[str, t.Any]) -> bool:
        """Give the node *instance_id* config values its component chose as a starting point.

        A component calls it for a node :func:`placement_context` reports as
        ``"new"``, from its ``__init__`` or function body, or later, e.g. once
        an input arrives. The values are saved with the dashboard like any
        config, and are part of the edit that brought them about: the
        placement, or the connection that delivered the input, so one undo
        takes back both. Values set outside any edit, e.g. by an input that
        arrives on its own, are recorded right away as a config edit of their
        own, so undoing a later edit keeps them. Called from a thread other
        than the session's event loop, they are set and recorded on the loop,
        as an edit of their own, and before a save.

        Only fields still at their declared default take a value, so a node
        that arrives configured, e.g. one the agent adds or a duplicate, keeps
        its config. Returns whether any field took one: False, changing
        nothing, also while a dashboard is loaded or a
        version restored, to viewers and in a preview, and for a node or a
        config field the node does not have.
        """
        config = self._unset_config(instance_id, config)
        if not config:
            return False
        if _INSTANTIATING.get() is self and _INSTANTIATING_NODE.get() == instance_id:
            # `_place` takes config written while the component is created into the node's config.
            config_state = self._dataflow_graph.get_config_state(instance_id)
            try:
                config_state.param.update(
                    {key: config_value(config_state.param[key], v) for key, v in config.items()}
                )
            except Exception as exc:
                logger.warning("Default config rejected on %s: %s", instance_id, exc)
                return False
            return True
        if (doc := self._off_loop_doc()) is not None:
            # The canvas and its history change on the session's event loop only.
            with self._queued_lock:
                self._queued_defaults.append((instance_id, config))
            doc.add_next_tick_callback(self._apply_queued_defaults)
            return True
        return self._apply_default(instance_id, config)

    def _unset_config(self, instance_id: str, config: t.Mapping[str, t.Any]) -> dict | None:
        """Return the fields of *config* a default may set on the node, or None if it may set none."""
        if self._hydrating or self.read_only or self.preview or not self.editable:
            return None
        config_state = self._dataflow_graph.get_config_state(instance_id)
        if config_state is None or any(
            key == "name" or key not in config_state.param for key in config
        ):
            return None
        return {key: value for key, value in config.items() if _unset(config_state, key)}

    def _off_loop_doc(self):
        """Return the session's document when called off its event loop, else None."""
        doc = self._session_doc() if self._session_doc is not None else None
        if doc is None or doc.session_context is None:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return doc
        return None

    def _apply_queued_defaults(self):
        """Set and record the defaults set off the event loop, each as its own edit."""
        with self._queued_lock:
            queued, self._queued_defaults = self._queued_defaults, []
        for instance_id, config in queued:
            if config := self._unset_config(instance_id, config):
                self._apply_default(instance_id, config)

    def _apply_default(self, instance_id: str, config: dict) -> bool:
        self._hydrating = True
        try:
            applied = self._set_config_state(instance_id, dict(config))
            if applied:
                self.dirty = True
        finally:
            self._hydrating = False
        if not applied:
            return False
        if self._dataflow_graph.wiring:
            # The edit rewiring the input records next and takes the default in.
            self._pending_defaults.append((instance_id, sorted(applied), self._edit_count))
            _next_tick(self._record_defaults)
        else:
            self._record_default(instance_id, sorted(applied))
        return True

    def _record_defaults(self):
        """Record the defaults no edit took in, each as its own edit."""
        pending, self._pending_defaults = self._pending_defaults, []
        current = self._edit_count
        for instance_id, keys, count in pending:
            if count == current:
                self._record_default(instance_id, keys)

    def _record_default(self, instance_id: str, keys: list[str]):
        if self._dataflow_graph.get_config_state(instance_id) is None:
            return
        name = self._node_name(instance_id)
        self._record_edit(
            f"Changed the config of {name}", op="set_config", args={"name": name, "keys": keys}
        )

    def add_component(
        self,
        component_id: str,
        config: dict | None = None,
        position: dict | tuple | None = None,
        instance_id: str | None = None,
        *,
        arrange: bool = False,
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
            to a free spot near the top-left of the visible canvas.
        instance_id
            Id for the new instance, which is also its node's name in the
            dashboard YAML, as a node a YAML file names gets it. It must be a
            valid node name without ``__``, at most ``MAX_NAME_LENGTH`` (64)
            characters, that no node on the canvas has.
            Defaults to a generated ``<component>_<6 hex>`` id.
        arrange
            Without a *position*, place the node by its dataflow, beside the
            nodes it is connected to, and move it again as it is connected
            until someone moves it by hand, and lay its tile out with the
            tiles of its kind (see :mod:`panel_flowdash.auto_layout`). The
            agent's ``add_node`` does this.

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
        automatic = arrange and position is None
        instance_id = self._place(
            component_id,
            instance_id or self._new_instance_id(component_id),
            config or {},
            position,
        )
        if automatic:
            self._floating_nodes.add(instance_id)
            self._settle_nodes()
            self._place_new_tiles([instance_id])
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

    def _open_card_popout(self, instance_id: str):
        """Show a card's previewed output in full in the page dialog: up to 100 rows, all columns.

        The value comes from the component's preview hook, which may query a
        database, so in a session it is fetched on a worker thread while the
        dialog says it is loading.
        """
        component_id = self._component_id_for_instance(instance_id)
        spec = self._component_specs.get(component_id) if component_id else None
        state = self._dataflow_graph.get_state(instance_id)
        if spec is None or state is None:
            return
        card = self._find_node_card(instance_id)
        fetch = partial(
            node_preview_value,
            spec,
            state,
            self._component_instances.get(instance_id),
            output=card.preview_output if card is not None else None,
        )
        show = partial(self._show_card_popout, instance_id, spec)
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            show(fetch())
            return
        self._popout_request = request = object()
        open_dialog(
            self._node_name(instance_id),
            pmui.LinearProgress(variant="indeterminate", sizing_mode="stretch_width"),
            width="lg",
            full_width=True,
        )

        def deliver(result):
            if self._popout_request is request:
                show(result)

        run_off_loop(fetch, deliver, doc)

    def _show_card_popout(self, instance_id: str, spec, result: tuple[str | None, t.Any]):
        port_name, value = result
        name = self._node_name(instance_id)
        title = f"{name}.{port_name}" if port_name else name
        caption = ""
        body: Viewable
        if value is None:
            body = markdown_pane("*No value yet*", margin=0)
        elif _is_table(value):
            rows, cols = value.shape
            head = safe_frame(value.head(100))
            caption = f"{rows:,} rows \u00d7 {cols:,}" + (
                f", first {len(head):,} shown" if rows > len(head) else ""
            )
            body = pn.widgets.Tabulator(
                head,
                layout="fit_data_table",
                stylesheets=[_POPOUT_TABLE_CSS],
                disabled=True,
                selectable=False,
                show_index=False,
                formatters=_plain_integer_formatters(head),
                sizing_mode="stretch_width",
                height=min(440, 48 + 36 * max(len(head), 1)),
                margin=0,
            )
        elif isinstance(value, t.Mapping) and value.get(PREVIEW_KIND_KEY) == "chart":
            caption = str(value.get("caption") or "")
            body = _popout_chart(value)
        elif isinstance(value, t.Mapping) and value.get(PREVIEW_KIND_KEY) == "kpi":
            caption = str(value.get("caption") or "")
            parts = [
                f"## {escape_markdown(str(value.get('value', '')))} "
                f"{escape_markdown(str(value.get('unit') or ''))}".strip()
            ]
            for key in ("label", "delta"):
                if value.get(key):
                    parts.append(escape_markdown(str(value[key])))
            body = markdown_pane("\n\n".join(parts), margin=(8, 0))
        else:
            caption = summarize_value(value)
            body = self._value_preview(value)
        header = pmui.Typography(
            caption, variant="caption", margin=(0, 0, 8, 0), sx={"color": "text.secondary"}
        )
        open_dialog(
            title,
            pn.Column(
                *([header] if caption else []),
                body,
                sizing_mode="stretch_width",
                min_width=_POPOUT_MIN_WIDTH,
                margin=0,
            ),
            width="lg",
            full_width=True,
        )

    def _place(
        self,
        component_id,
        instance_id,
        config,
        position=None,
        *,
        preview_output: str | None = None,
        collapsed: bool = False,
    ) -> str:
        """Instantiate a component and add it to the graph, canvas and tile list.

        A component already placed under *instance_id* is removed first, so
        the new one replaces it rather than sitting next to it under the same id.
        """
        entry = self._component_entries[component_id]
        spec = self._component_specs[component_id]
        if any(item["instance_id"] == instance_id for item in self._tile_items):
            # The component replacing it takes over the blocks showing it.
            report = self._report
            self._unplace(instance_id)
            self._report = report

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
        node_token = _INSTANTIATING_NODE.set(instance_id)
        # A node the component adds while it is created is new, whatever placed this one.
        reason_token = _PLACEMENT.set(_PLACING.get() or "new")
        placing_token = _PLACING.set(None)
        # Config a component writes while it is created, e.g. a default for a
        # new node, is the node's config, so it is saved with the dashboard.
        written: dict[str, t.Any] = {}
        config_watcher = None
        if config_state is not None:
            config_watcher = config_state.param.watch(
                lambda *events: written.update({e.name: e.new for e in events}),
                [name for name in config_state.param if name != "name"],
            )
        try:
            view = self._instantiate_for_node(entry, node_state, config_state)
        except Exception:
            self._dataflow_graph.remove_node(instance_id)
            self._track_subscriptions(instance_id, before)
            self._dispose_component(instance_id)
            raise
        finally:
            if config_watcher is not None:
                config_state.param.unwatch(config_watcher)
            _PLACING.reset(placing_token)
            _PLACEMENT.reset(reason_token)
            _INSTANTIATING_NODE.reset(node_token)
            _INSTANTIATING.reset(token)
        if written:
            config = {**config, **written}
            config_data.update(written)
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
        card = node_card(
            spec,
            node_state,
            failures,
            self._component_wiring.setdefault(instance_id, []),
            instance=self._component_instances.get(instance_id),
            show_preview=getattr(self, "_node_previews_shown", self.settings.node_previews),
            instance_id=instance_id,
            preview_output=preview_output,
            collapsed=collapsed,
            on_layout=_EditorCall(self, "_on_card_layout", instance_id),
        )
        card.stale = self._code_stale(instance_id)
        node_dict["view"] = card
        _, css_vars = card.handle_layout()
        if css_vars:
            node_dict["style"] = css_vars
        self._add_canvas_node(node_dict)
        self._sync_card_bridge()

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

    # ------------------------------------------------------------------
    # Automatic layout
    # ------------------------------------------------------------------

    def _node_role(self, component_id: str) -> str:
        entry = self._component_entries.get(component_id)
        spec = self._component_specs.get(component_id)
        if entry is None or spec is None:
            return "other"
        return component_role(entry, spec.inputs, spec.outputs)

    def _layout(self) -> tuple[list[auto_layout.NodeBox], list[tuple[str, str]], dict, dict]:
        """Return the boxes and edges to lay out, each group as one box of its frame.

        Also returns the canvas positions of the nodes and groups, by box id,
        and the offset of each group member from its group's box.
        """
        positions = self._absolute_positions()
        edges = self._node_edges()
        box_of: dict[str, str] = {}
        offsets: dict[str, dict[str, tuple[float, float]]] = {}
        frames: dict[str, auto_layout.NodeBox] = {}
        origins: dict[str, tuple[float, float]] = {}
        for group in self._groups.values():
            members = [n for n in group.nodes if n in positions]
            if not members:
                continue
            frame = group_node_id(group.id)
            x0, y0, x1, y1 = self._group_box(group, positions)
            if group.collapsed:
                inputs, outputs = self._group_ports(group)
                rows = max(len(inputs), len(outputs), 1)
                width, height = (
                    _COLLAPSED_GROUP_WIDTH,
                    GROUP_HEADER_HEIGHT + 12 + GROUP_PORT_ROW_HEIGHT * rows,
                )
            else:
                width, height = x1 - x0, y1 - y0
            frames[frame] = auto_layout.NodeBox(frame, width, height, "other")
            origins[frame] = (x0, y0)
            offsets[frame] = {n: (positions[n]["x"] - x0, positions[n]["y"] - y0) for n in members}
            box_of.update(dict.fromkeys(members, frame))
        # A group takes the place of its first member in the order.
        placed = set()
        ordered = []
        for box in self._node_boxes():
            target = frames.get(box_of.get(box.id)) if box.id in box_of else box
            if target is not None and target.id not in placed:
                placed.add(target.id)
                ordered.append(target)
        edges = [
            (box_of.get(a, a), box_of.get(b, b))
            for a, b in edges
            if box_of.get(a, a) != box_of.get(b, b)
        ]
        where = {k: (v["x"], v["y"]) for k, v in positions.items() if k not in box_of}
        where.update(origins)
        return ordered, list(dict.fromkeys(edges)), where, offsets

    @staticmethod
    def _unfold(
        placed: dict[str, tuple[float, float]], offsets: dict
    ) -> dict[str, tuple[float, float]]:
        """Return node positions from a layout of boxes, placing group members by their offsets."""
        positions = {}
        for box_id, (x, y) in placed.items():
            if box_id in offsets:
                for node_id, (dx, dy) in offsets[box_id].items():
                    positions[node_id] = (x + dx, y + dy)
            else:
                positions[box_id] = (x, y)
        return positions

    def _node_boxes(self) -> list[auto_layout.NodeBox]:
        """Return the nodes on the canvas with their cards' sizes and roles, in placement order."""
        components = {item["instance_id"]: item for item in self._tile_items}
        boxes = []
        for node in self._flow.nodes:
            if is_group_node(node):
                continue
            view = node.get("view")
            item = components.get(node["id"])
            role = "other"
            if item is not None and "placeholder" not in item:
                role = self._node_role(item["component_id"])
            height = view.height if isinstance(view, NodeCard) else card_height(1, 1)
            boxes.append(auto_layout.NodeBox(node["id"], CARD_WIDTH, height, role))
        return boxes

    def _node_edges(self) -> list[tuple[str, str]]:
        edges = [(e["source"], e["target"]) for e in self._dataflow_graph.edges]
        return edges + [(u.edge.source, u.edge.target) for u in self._unresolved_edges]

    def _move_nodes(self, positions: dict[str, tuple[float, float]]):
        """Move nodes to canvas *positions*, refitting the frames of the groups they are in.

        A node in a group is positioned relative to its frame on the canvas, so
        each frame is moved to fit its nodes first and its nodes are placed
        relative to it.
        """
        absolute = {
            node_id: (position["x"], position["y"])
            for node_id, position in self._absolute_positions().items()
        }
        absolute.update({k: (round(x, 1), round(y, 1)) for k, (x, y) in positions.items()})
        origins = {}
        for group in self._groups.values():
            members = [absolute[n] for n in group.nodes if n in absolute]
            if members:
                origins[group_node_id(group.id)] = (
                    round(min(x for x, _ in members) - _GROUP_PADDING, 1),
                    round(
                        min(y for _, y in members) - _GROUP_PADDING - pr.base.GROUP_HEADER_HEIGHT,
                        1,
                    ),
                )
        targets = dict(origins)
        for node in self._flow.nodes:
            node_id = node["id"]
            if node_id not in absolute or (
                node_id not in positions and node.get("parentId") not in origins
            ):
                continue
            x, y = absolute[node_id]
            ox, oy = origins.get(node.get("parentId"), (0, 0))
            targets[node_id] = (round(x - ox, 1), round(y - oy, 1))
        with self._muted_canvas():
            for node in self._flow.nodes:
                if node["id"] not in targets:
                    continue
                x, y = targets[node["id"]]
                position = {"x": x, "y": y}
                if (node.get("position") or {}) != position:
                    self._flow.patch_node_props(node["id"], {"position": position})
                    self._fit_pending = True

    def _settle_nodes(self):
        """Lay the nodes the editor placed out again, around the nodes placed by hand or a file."""
        if not self._floating_nodes or self._hydrating or self._canvas_batch is not None:
            return
        boxes, edges, where, offsets = self._layout()
        fixed = {box_id: xy for box_id, xy in where.items() if box_id not in self._floating_nodes}
        placed = auto_layout.place_nodes(boxes, edges, fixed)
        self._move_nodes(
            self._unfold({k: v for k, v in placed.items() if k not in fixed}, offsets)
        )

    def arrange_canvas(self):
        """Lay out every node on the canvas by its dataflow, as one edit.

        Sources go on the left and views on the right, in columns by their
        depth in the dataflow, ordered to reduce crossing edges. Positions
        set by hand are replaced too; that is what the request is for.
        """
        boxes, edges, where, offsets = self._layout()
        if not boxes:
            return
        initial = {box_id: y for box_id, (_x, y) in where.items()}
        self._move_nodes(
            self._unfold(auto_layout.arrange_graph(boxes, edges, initial=initial), offsets)
        )
        self._floating_nodes.clear()
        self._record_edit("Arranged the dataflow", op="arrange_canvas")
        self._fit_placed_nodes()

    def _tile_box(self, item: dict) -> auto_layout.TileBox:
        spec = self._tile_spec(item)
        entry = self._component_entries.get(item["component_id"])
        if "placeholder" in item or spec is None or entry is None:
            return auto_layout.TileBox(item["instance_id"], "chart")
        kind = auto_layout.tile_kind(
            self._node_role(item["component_id"]),
            entry.metadata.tags,
            spec.default_size,
            text=item["component_id"] in TEXT_COMPONENTS,
        )
        return auto_layout.TileBox(
            item["instance_id"],
            kind,
            dict(spec.default_size or {}),
            dict(spec.min_size or {}),
            dict(spec.max_size or {}),
        )

    def _tile_bands(self) -> tuple[int, dict[str, float]]:
        """Return the width the layout is arranged at and the width each band is laid out for."""
        config = self._grid_config()
        breakpoints = list(config["breakpoints"] or self.breakpoints)
        width = reference_width(config["reference_width"], breakpoints)
        return width, auto_layout.band_widths(breakpoints, band_labels(breakpoints))

    def _set_tile_layout(self, layout: list[dict], responsive: dict[str, list[dict]]):
        config = self._grid_config()
        config["responsive_layouts"] = responsive
        self._pending_tile_layout = layout
        self._pending_grid_config = config
        # A grid not built yet takes the layout when it is, so the tiles not
        # arranged keep having no entry of their own until then.
        if self._grid_populated:
            self._rebuild_tile_grid()

    def _all_tiles_automatic(self, new: t.Collection[str] = ()) -> bool:
        """Whether the editor laid out every tile, counting the *new* ones as its own."""
        deferred = self._deferred_tiles or ()
        return all(
            iid in self._auto_tiles or iid in new or iid in deferred
            for iid in (item["instance_id"] for item in tile_layout.grid_tiles(self))
        )

    def _tile_order(self) -> list[dict]:
        """Return the shown tiles in display order: by layout index, else in placement order."""
        tiles = tile_layout.grid_tiles(self)
        rank = {e["instance_id"]: e.get("index") for e in self.layout if "instance_id" in e}

        def key(i):
            index = rank.get(tiles[i]["instance_id"])
            return (index is None, index or 0, i)

        return [tiles[i] for i in sorted(range(len(tiles)), key=key)]

    def _arrange_tiles(self, hide: t.Collection[str] = ()):
        """Lay out every visible tile by its kind, hiding the tiles of *hide* first."""
        boxes = [
            self._tile_box(item) for item in self._tile_order() if item["instance_id"] not in hide
        ]
        width, _ = self._tile_bands()
        previous = {e["instance_id"]: e for e in self.layout}
        layout = auto_layout.arrange_tiles(boxes, width, previous)
        shown = {box.id for box in boxes}
        hidden = [dict(e) for e in self.layout if e.get("instance_id") not in shown | set(hide)]
        hidden += [{**previous.get(iid, {}), "instance_id": iid, "visible": False} for iid in hide]
        for index, entry in enumerate(hidden, start=len(layout)):
            entry["index"] = index
        # Narrower bands follow the arranged layout, rather than keep layouts made for the old one.
        self._set_tile_layout(layout + hidden, {})
        self._auto_tiles = shown

    def _plumbing(self, instance_ids: t.Iterable[str]) -> list[str]:
        """Return the shown tiles of *instance_ids* that are data sources and transforms.

        They are the dashboard's plumbing, so their new tiles start hidden; the
        person can show them from the components not on the dashboard.
        """
        ids = set(instance_ids) - self._chosen_visibility
        return [
            item["instance_id"]
            for item in tile_layout.grid_tiles(self)
            if item["instance_id"] in ids and self._tile_box(item).kind in auto_layout.HIDDEN_KINDS
        ]

    def _place_new_tiles(self, instance_ids: t.Iterable[str]):
        """Give the tiles of *instance_ids*, which nobody laid out yet, a place among the others.

        While no tile was laid out by a person every tile is arranged again;
        otherwise the new tiles join the tiles of their kind and the rest
        stay as they are.
        """
        if self._deferred_tiles is not None:
            self._deferred_tiles.update(instance_ids)
            source = _agent_source()
            run = source.run if source is not None else None
            self._deferred_by_run.setdefault(run, set()).update(instance_ids)
            return
        hide = self._plumbing(instance_ids)
        tiles = {
            item["instance_id"]: item
            for item in tile_layout.grid_tiles(self)
            if item["instance_id"] not in hide
        }
        new = [iid for iid in instance_ids if iid in tiles]
        if not (new or hide):
            return
        if self._all_tiles_automatic([*new, *hide]):
            self._arrange_tiles(hide)
            return
        if hide:
            layout = [e for e in self.layout if e.get("instance_id") not in hide]
            layout += [{"instance_id": iid, "visible": False} for iid in hide]
            self._set_tile_layout(layout, self._grid_config()["responsive_layouts"])
        if not new:
            return
        boxes = [self._tile_box(tiles[iid]) for iid in new]
        kinds = {iid: self._tile_box(item).kind for iid, item in tiles.items()}
        order = [item["instance_id"] for item in self._tile_order()]
        width, bands = self._tile_bands()

        def insert(entries, width):
            previous = {e["instance_id"]: e for e in entries if e.get("instance_id") in new}
            entries = [e for e in entries if e.get("instance_id") not in new]
            return auto_layout.insert_tiles(entries, order, kinds, boxes, width, previous)

        layout = insert(self.layout, width)
        responsive = {
            band: insert(entries, bands.get(band, width))
            for band, entries in self._grid_config()["responsive_layouts"].items()
        }
        self._set_tile_layout(layout, responsive)
        self._auto_tiles.update(new)

    def arrange_layout(self):
        """Lay out every visible tile by its kind, as one edit.

        Controls go first, then KPIs, both at their default width, charts in
        two columns, tables at full width and the tiles of data sources and
        transforms last; text tiles stay in front of the tile they preceded.
        Hidden tiles stay hidden. Layouts made for narrower breakpoint bands
        are dropped, so those bands follow the arranged layout again.
        """
        if not tile_layout.grid_tiles(self):
            return
        self._arrange_tiles()
        self._record_edit("Arranged the tile layout", op="arrange_layout")

    def _placed_singletons(self) -> set[str]:
        return {
            item["component_id"]
            for item in self._tile_items
            if "placeholder" not in item
            and self._component_entries[item["component_id"]].metadata.singleton
        }

    def remove_component(self, instance_id: str):
        """Remove a placed component along with its edges and its tile."""
        rearrange = instance_id in self._auto_tiles and self._all_tiles_automatic()
        self._unplace(instance_id)
        self._settle_nodes()
        if rearrange and self._deferred_tiles is not None:
            self._deferred_rearrange = True
        elif rearrange:
            self._arrange_tiles()
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
        self._sync_card_connections(target_id)
        self._settle_nodes()
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
        """Follow the selection in the code panel, and reload a newly selected edge's transform editor."""
        self._follow_selection(event.new)
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
        self._remove_edge(mapping)
        self._sync_card_connections(target_id)
        self._settle_nodes()
        self._record_edit(
            self._connect_label("Disconnected", *mapping),
            op="disconnect",
            args=self._edge_args(*mapping),
        )

    @pn.io.hold()
    def disconnect_input(self, instance_id: str, port: str):
        """Remove every edge into the input *port* of *instance_id*, as one edit."""
        mappings = [m for m in self._edge_id_map.values() if m[2] == instance_id and m[3] == port]
        if not mappings:
            return
        for mapping in mappings:
            self._remove_edge(mapping)
        self._sync_card_connections(instance_id)
        self._settle_nodes()
        self._record_edit(
            *(self._connect_label("Disconnected", *m) for m in mappings),
            op="disconnect",
            args=self._edge_args(*mappings[0]),
        )

    def _remove_edge(self, mapping: tuple[str, str, str, str]):
        self._dataflow_graph.remove_edge(*mapping)
        for edge_id, existing in list(self._edge_id_map.items()):
            if existing == mapping:
                del self._edge_id_map[edge_id]
                self._drop_edge_editor(edge_id)
                with self._muted_canvas():
                    self._flow.remove_edge(edge_id)

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
        self._floating_nodes = set()
        self._auto_tiles = set()
        self._chosen_visibility = set()
        self._tile_items = []
        self._tile_objects = []
        self._rejected_config = {}
        self._area_entries = {}
        self._grid_areas = {}
        self._inspect_tiles.clear()
        self._close_inspection_dialog()
        self._edge_id_map.clear()
        for edge_id in list(self._transform_editors):
            self._drop_edge_editor(edge_id)
        self._edge_count = 0
        self._unresolved_edges = []
        self._unresolved_notice = None
        self._origins = {}
        self._groups = {}
        self._report = None
        self._global_filters.load([])
        self._pushed = {}
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
            node_id: (position.get("x", 0), position.get("y", 0))
            for node_id, position in self._absolute_positions().items()
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
        cards = {
            node["id"]: card
            for node in self._flow.nodes
            if isinstance(card := node.get("view"), NodeCard)
        }
        model.items = [
            DashboardItem(
                instance_id=item["instance_id"],
                component_id=item["component_id"],
                x=positions.get(item["instance_id"], (0, 0))[0],
                y=positions.get(item["instance_id"], (0, 0))[1],
                config=item.get("config", {}),
                origin=self._origins.get(item["instance_id"]),
                inputs=copy.deepcopy(item.get("inputs") or {}),
                preview_output=(
                    cards[item["instance_id"]].preview_output
                    if item["instance_id"] in cards and "placeholder" not in item
                    else item.get("preview_output")
                ),
                collapsed=(
                    cards[item["instance_id"]].collapsed
                    if item["instance_id"] in cards and "placeholder" not in item
                    else bool(item.get("collapsed", False))
                ),
            )
            for item in self._tile_items
        ]
        model.turns = self._turn_log.snapshot()
        placed = {item.instance_id for item in model.items}
        model.groups = [
            dataclasses.replace(group, nodes=[n for n in group.nodes if n in placed])
            for group in self._groups.values()
            if any(n in placed for n in group.nodes)
        ]
        model.parameters = {
            name: copy.deepcopy(parameter)
            for name, parameter in self._parameters.items()
            if parameter.node in placed
        }
        model.report = map_report(
            self._report,
            {iid: iid for iid in placed},
            {group.id for group in model.groups},
            {turn.id for turn in model.turns},
        )
        model.filters = map_filters(self._global_filters.filters, {iid: iid for iid in placed})
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
        # A height the model stores is the model's, even where the editor showed the same one.
        self._fill_heights.clear()
        self._hydrating = True
        try:
            with _placing("load"):
                self._hydrate(model, keep_open_turns)
        finally:
            self._hydrating = False
        self._note_parameter_nodes()
        if not self._history_quiet:
            self._note_restricted()
            self._history.opened()
        if self._explore_feed is not None:
            self._explore_feed.sync()
        if self._report_view is not None:
            self._report_view.schedule()

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
        self._parameters = copy.deepcopy(model.parameters)
        known = {turn.id for turn in model.turns}
        self._origins = {
            item.instance_id: item.origin for item in model.items if item.origin in known
        }
        # After the previous components are disposed, so none of them reacts to the
        # new values, and before these are placed, so they see this dashboard's.
        self._apply_settings(model.settings)
        self.dashboard = model
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
                            preview_output=item.preview_output,
                            collapsed=item.collapsed,
                        )
                        self._set_inputs(item.instance_id, item.inputs)
                        self._sync_card_connections(item.instance_id)
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
        self._draw_groups(model.groups)
        self._report = copy.deepcopy(model.report)
        self._global_filters.load(model.filters)

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
                "inputs": copy.deepcopy(item.inputs),
                "preview_output": item.preview_output,
                "collapsed": item.collapsed,
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
        self._apply_queued_defaults()
        self._record_defaults()
        self._compute_expects()
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
        self._apply_queued_defaults()
        self._record_defaults()
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
        with self._quiet_history(), _placing("restore"):
            self._hydrating = True
            try:
                patched = self._patch_canvas(model)
            finally:
                self._hydrating = False
            if not patched:
                self._hydrate_model(model, keep_open_turns=True)
        if self._report_view is not None:
            # The report is restored after the dashboard, which the view follows.
            self._report_view.schedule()

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
                or before[iid].inputs != after[iid].inputs
                or before[iid].component_id != after[iid].component_id
            ):
                return False

        self._turn_log.reset(model.turns, keep_open=True)
        self._parameters = copy.deepcopy(model.parameters)
        known = {turn.id for turn in model.turns}
        self._origins = {
            item.instance_id: item.origin for item in model.items if item.origin in known
        }
        # Nodes move back to the top level, at the positions the model gives,
        # and the model's groups are drawn around them at the end.
        self._erase_canvas_groups()
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
                    preview_output=item.preview_output,
                    collapsed=item.collapsed,
                )
                self._set_inputs(iid, item.inputs)
            for iid in kept:
                if iid in placeholders:
                    tile = next(i for i in self._tile_items if i["instance_id"] == iid)
                    tile["preview_output"] = after[iid].preview_output
                else:
                    self._set_config(iid, after[iid].config)
                    self._set_inputs(iid, after[iid].inputs)
                    # Undo leaves cards collapsed or expanded as they are, since that is no edit.
                    if (card := self._find_node_card(iid)) is not None:
                        card.preview_output = after[iid].preview_output
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
        self._sync_card_bridge()
        self._status_bar.sync_collapse_toggle()
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
        self._draw_groups(model.groups)
        self._report = copy.deepcopy(model.report)
        self._global_filters.load(model.filters)
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
                rejected = self._rejected_config.get(instance_id, {}).pop(key, _MISSING)
                try:
                    setattr(config_state, key, config_value(config_state.param[key], value))
                except Exception as exc:
                    logger.warning("Config '%s' rejected on %s: %s", key, instance_id, exc)
                    if rejected is not _MISSING:
                        self._rejected_config[instance_id][key] = rejected
        item["config"] = copy.deepcopy(target)

    def _set_inputs(self, instance_id: str, inputs: dict[str, t.Any]):
        """Give a placed node exactly *inputs* as the literal values of its inputs.

        A value its input refuses, e.g. one of a port the component no longer
        has, is logged and kept in the item, so saving writes it back unchanged.
        """
        item = next(i for i in self._tile_items if i["instance_id"] == instance_id)
        spec = self._component_specs.get(item["component_id"])
        target = {
            spec.resolve_name(k) if spec else k: v for k, v in inputs.items() if v is not None
        }
        graph = self._dataflow_graph
        current = graph.literals(instance_id)
        for port in current:
            if port not in target:
                graph.clear_literal(instance_id, port)
        for port, value in target.items():
            if port in current and stored_literal(current[port]) == value:
                continue
            try:
                graph.set_literal(instance_id, port, value)
            except ValueError as exc:
                logger.warning("Input value '%s' rejected on %s: %s", port, instance_id, exc)
                graph.clear_literal(instance_id, port)
        if target:
            item["inputs"] = copy.deepcopy(target)
        else:
            item.pop("inputs", None)
        if current or target:
            self._refresh_inspector_inputs()

    def _refresh_inspector_inputs(self):
        if (inspector := getattr(self, "_inspector", None)) is not None:
            inspector._refresh_inputs()

    def input_values(self, instance_id: str) -> dict[str, t.Any]:
        """Return the literal values set on the unconnected inputs of a placed node, by port.

        A connected input's literal is listed too; it applies once the
        connection is removed.
        """
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        if item is None:
            raise KeyError(f"No component '{instance_id}' is placed.")
        return copy.deepcopy(item.get("inputs") or {})

    def set_input_value(self, instance_id: str, port: str, value: t.Any):
        """Give an input of a placed node a literal value, used while nothing is connected to it.

        A connection always wins over the literal, which applies again once the
        connection is removed. None removes the literal, and the input goes
        back to its default. The change is recorded in the edit history.

        Raises
        ------
        KeyError
            If no component is placed under *instance_id*.
        ValueError
            If the component has no such input, the input cannot hold a
            literal, e.g. a DataFrame, or it refuses *value*.
        """
        self.set_input_values(instance_id, {port: value})

    def set_input_values(self, instance_id: str, values: t.Mapping[str, t.Any]):
        """Set or remove (with None) the literal values of several inputs, as one edit.

        Every value is checked before any is set, so a refused one changes
        nothing; see :meth:`set_input_value`.
        """
        changed = self._set_input_values(instance_id, values)
        if not changed:
            return
        name = self._node_name(instance_id)
        labels = ", ".join(changed.values())
        cleared = all(values.get(port) is None for port in changed)
        self._record_edit(
            f"{'Cleared' if cleared else 'Set'} {labels} of {name}",
            op="set_input",
            args={"name": name, "keys": sorted(changed)},
        )

    def _set_input_values(self, instance_id: str, values: t.Mapping[str, t.Any]) -> dict[str, str]:
        """Apply literal values to a node's inputs without recording an edit.

        Returns the label of each input that changed, by port. Raises like
        :meth:`set_input_value`, before changing anything.
        """
        item = next((i for i in self._tile_items if i["instance_id"] == instance_id), None)
        if item is None:
            raise KeyError(f"No component '{instance_id}' is placed.")
        if "placeholder" in item:
            raise ValueError(f"'{instance_id}' could not be loaded, so its inputs cannot change.")
        spec = self._component_specs[item["component_id"]]
        ports = {p.name: p for p in spec.inputs}
        resolved: dict[str, t.Any] = {}
        for key, value in values.items():
            port = ports.get(spec.resolve_name(key))
            if port is None:
                raise ValueError(f"'{spec.title}' has no input '{key}'.")
            if value is not None and (reason := check_literal(port, value)) is not None:
                raise ValueError(reason)
            resolved[port.name] = value
        inputs = dict(item.get("inputs") or {})
        changed = {}
        for port, value in resolved.items():
            if value is None:
                if port not in inputs:
                    continue
                del inputs[port]
            else:
                stored = stored_literal(value)
                if inputs.get(port, _MISSING) == stored:
                    continue
                inputs[port] = stored
            changed[port] = port_label(ports[port])
        # Recorded before the graph is changed, so whatever reacts to the new
        # value, e.g. the component, finds the node's inputs already up to date.
        if inputs:
            item["inputs"] = inputs
        else:
            item.pop("inputs", None)
        for port in changed:
            if resolved[port] is None:
                self._dataflow_graph.clear_literal(instance_id, port)
            else:
                self._dataflow_graph.set_literal(instance_id, port, resolved[port])
        if changed:
            self._refresh_inspector_inputs()
        return changed

    def _restore_refusal(self, text: str) -> str | None:
        """Return why restoring a version of the history is refused, or None if it is allowed."""
        try:
            self._restore_model(text, notify=False)
        except PermissionError as exc:
            return error_text(exc)
        except (ValueError, KeyError, TypeError) as exc:
            return f"That version cannot be read: {error_text(exc)}"
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

        unplaced: set[str] = set()
        model = self._imported_model(data, unplaced)
        before = dashboard_to_dict(self)
        automatic = self._keep_automatic_layout(model, unplaced)
        if not keep_turns:
            present = {item["instance_id"] for item in self._tile_items}
            with self._quiet_history():
                self.load_model(model)
                self._apply_automatic_layout(*automatic)
                self._compute_expects()
            self._fit_placed_nodes()
            self._record_import(model, before, present)
            return model
        model.parameters = self._kept_parameters(model.parameters)
        model.turns = self._turn_log.snapshot()
        present = {item["instance_id"] for item in self._tile_items}
        for item in model.items:
            item.origin = self._origins.get(item.instance_id)
        model.report = self._kept_block_origins(model.report)
        self.ensure_components_loaded(self._model_component_ids(model))
        with self._quiet_history():
            self._hydrate_model(model, keep_open_turns=True)
            self._apply_automatic_layout(*automatic)
            self._compute_expects(missing_only=True)
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

    def _kept_block_origins(self, report: dict | None) -> dict | None:
        """Give the blocks of an imported *report* the origins of the same blocks in the current one.

        Like node origins, block origins are only recorded by the editor, so
        a file loaded with ``keep_turns`` sets none of its own.
        """
        if report is None:
            return None
        origins: dict[str, list[str]] = {}
        for block in (self._report or {}).get("blocks") or []:
            if block.get("origin") is not None:
                key = json.dumps({k: v for k, v in block.items() if k != "origin"}, sort_keys=True)
                origins.setdefault(key, []).append(block["origin"])
        blocks = []
        for block in report.get("blocks") or []:
            entry = {k: v for k, v in block.items() if k != "origin"}
            kept = origins.get(json.dumps(entry, sort_keys=True))
            if kept:
                entry["origin"] = kept.pop(0)
            blocks.append(entry)
        return {**report, "blocks": blocks}

    def _keep_automatic_layout(
        self, model: DashboardModel, unplaced: set[str]
    ) -> tuple[set[str], set[str], dict[str, dict], set[str]]:
        """Work out which nodes and tiles of an imported *model* the editor lays out.

        A node the file gives no position keeps the one it has on the canvas,
        unless the editor chose that one. The nodes the editor placed and
        tiles it laid out stay its own while the file leaves them as they are.
        Returns the nodes to lay out, the tiles the editor laid out with
        their entries before, and the tiles to lay out.
        """
        positions = self._absolute_positions()
        floating = set(unplaced)
        for item in model.items:
            position = positions.get(item.instance_id)
            if position is None:
                continue
            if item.instance_id in self._floating_nodes:
                if item.instance_id in unplaced or (item.x, item.y) == (
                    position["x"],
                    position["y"],
                ):
                    floating.add(item.instance_id)
            elif item.instance_id in unplaced:
                item.x, item.y = position["x"], position["y"]
                floating.discard(item.instance_id)
        entries = {
            e["instance_id"]: e for e in self.layout if e["instance_id"] in self._auto_tiles
        }
        laid_out = {e.get("instance_id") for e in model.tile_layout}
        # A tile the file gives no layout keeps the one it has, unless the editor chose it.
        kept = {item["instance_id"] for item in self._tile_items} - self._auto_tiles
        kept -= self._deferred_tiles or set()
        unarranged = {item.instance_id for item in model.items} - laid_out - kept
        return floating, set(self._auto_tiles), entries, unarranged

    def _apply_automatic_layout(
        self,
        floating: set[str],
        auto_tiles: set[str],
        entries: dict[str, dict],
        new_tiles: set[str],
    ):
        """Lay out the nodes and tiles of a loaded import, see `_keep_automatic_layout`."""
        placed = {item["instance_id"] for item in self._tile_items}
        self._floating_nodes = floating & placed

        def size(entry):
            return {k: entry.get(k) for k in ("width", "height", "visible")}

        layout = {e["instance_id"]: e for e in self.layout}
        self._auto_tiles = {
            iid
            for iid in (auto_tiles & placed) - new_tiles
            if size(layout.get(iid, {})) == size(entries.get(iid, {}))
        }
        self._settle_nodes()
        self._place_new_tiles(new_tiles & placed)

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

    def _imported_model(
        self, data: dict | str | bytes, unplaced: set[str] | None = None
    ) -> DashboardModel:
        """Parse an export into a model that takes the loaded dashboard's identity.

        The ids of the nodes a dashboard in the YAML format gives no position
        are added to *unplaced*.
        """
        from panel_flowdash.agent.yaml_format import (
            dashboard_from_dict,
            is_dashboard_yaml,
            read_dashboard_text,
            unplaced_nodes,
        )

        current = self.dashboard
        if isinstance(data, (str, bytes)):
            data = read_dashboard_text(data)
        if is_dashboard_yaml(data):
            # Names map back to the ids of the canvas they were exported from.
            doc, data = data, dashboard_from_dict(data, base=self.export_dashboard())
            if unplaced is not None:
                unplaced |= unplaced_nodes(doc, data)
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

    def _add_from_ui(self, component_id, position=None, config=None) -> str | None:
        """Add *component_id*, reporting failures as notifications instead of raising."""
        if component_id in self._placed_singletons():
            title = self._component_entries[component_id].title
            self._notify("warning", f"'{title}' is already on the canvas.", duration=3000)
            return None
        try:
            instance_id = self.add_component(component_id, config=config, position=position)
        except KeyError:
            self._notify("warning", "Select a valid component first.", duration=3000)
        except PermissionError as exc:
            self._notify("error", error_text(exc), duration=5000)
        except Exception as exc:
            logger.exception("Failed to add component '%s'", component_id)
            self._notify("error", f"Failed to add component: {error_text(exc)}", duration=5000)
        else:
            entry = self._component_entries[component_id]
            self._notify("success", f"Added component: {entry.title}", duration=3000)
            return instance_id
        return None

    def _on_palette_clicked(self, item):
        component_id = item.get("component_id") if isinstance(item, dict) else None
        if component_id is None:
            return
        instance_id = self._add_from_ui(component_id, config=item.get("config"))
        if self._phone and instance_id is not None:
            # The sheet covers the canvas, so the new node is shown and selected
            # for its settings to open in the inspector's sheet.
            self._palette_sheet.open = False
            self._reveal_above_sheet(instance_id)
            self._select_node(instance_id)

    def _on_component_dropped(self, payload):
        if payload.get("drop_type") == _FILES_DROP_TYPE:
            self._on_files_dropped(payload.get("data") or [])
            return
        self._add_dropped(payload)

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
            self._notify(
                "error", f"Could not load {file['name']}: {error_text(exc)}", duration=5000
            )
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
        # `_import_from_ui` is a coroutine, so it runs as its own task on the event loop.
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
            self._notify(
                "error", f"Could not load the dashboard: {error_text(exc)}", duration=5000
            )
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
            item = {}
        label = (payload.get("data") or {}).get("label")
        if label and item.get("label") != label:
            # The palette changed while the entry was dragged, e.g. its presets were
            # refreshed, so the path may point elsewhere; the dragged label still names it.
            item = next(
                (
                    i
                    for group in self._palette.items
                    for i in group.get("items", ())
                    if i.get("label") == label
                ),
                {},
            )
        component_id = item.get("component_id")
        if component_id is None:
            return
        target = payload.get("target") or {}
        wire = target.get("direction") in ("input", "output") and target.get("handle_id")
        position = payload["position"]
        if wire:
            anchor = self._absolute_positions()[target["node_id"]]
            offset = -_DROP_WIRE_OFFSET if target["direction"] == "input" else _DROP_WIRE_OFFSET
            position = {"x": anchor["x"] + offset, "y": anchor["y"]}
        instance_id = self._add_from_ui(component_id, position=position, config=item.get("config"))
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
        bridges = []
        for candidate in candidates:
            reason = self._validate_connection(*candidate)
            if reason is None:
                result = self.connect(*candidate)
                if result is True:
                    return
                if bridge := self._bridge(*candidate):
                    bridges.append((candidate, bridge))
                    continue
                reason = result
            reasons.append(reason)
        if bridges:
            self._transform_offer.offer(*bridges[0])
            return
        title = self._component_entries[component_id].title
        detail = f": {reasons[0]}" if reasons else ""
        self._notify("warning", f"Added '{title}' but could not connect it{detail}", duration=5000)

    def _has_unsent_edits(self) -> bool:
        """Whether the inspector holds typed text not applied yet, which a save should include."""
        inspector = getattr(self, "_inspector", None)
        if inspector is not None and inspector.has_unsent_text():
            return True
        return self._has_code_drafts()

    def _on_code_drafts(self, *_events):
        self.code_drafts = self._has_code_drafts()
        self._sync_toolbar_state()

    def drop_code_drafts(self):
        """Forget the edits the code panel holds without running them, e.g. when leaving the dashboard."""
        panel = getattr(self, "_code_panel", None)
        if panel is not None:
            panel.drop_all_drafts()

    def _has_code_drafts(self) -> bool:
        """Whether the code panel holds an edit that has not been run."""
        panel = getattr(self, "_code_panel", None)
        return panel is not None and panel.has_drafts

    def _save_from_button(self):
        """Save, once an edit the click just applied, e.g. by leaving a code field, is checked.

        Leaving the field sends its text before the click, but the config
        check that applies it finishes later, so saving at once would store
        the config without it.
        """
        inspector = getattr(self, "_inspector", None)
        draft = inspector._draft if inspector is not None else None
        if draft is None or not draft.busy:
            self._on_save_clicked()
            return

        async def save_when_checked():
            # The button shows it is busy while the check runs.
            self._save_button.loading = True
            try:
                await inspector.settle()
            finally:
                self._save_button.loading = False
            self._on_save_clicked()

        param.parameterized.async_executor(save_when_checked)

    def _on_save_clicked(self) -> bool:
        try:
            model = self.save()
        except DashboardConflictError as exc:
            logger.info("Store refused to save dashboard: %s", exc)
            self._notify("error", error_text(exc), duration=5000)
            self._show_save_conflict()
        except DashboardStoreError as exc:
            logger.info("Store refused to save dashboard: %s", exc)
            self._notify("error", error_text(exc), duration=5000)
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
            self._notify("error", error_text(exc), duration=5000)
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
