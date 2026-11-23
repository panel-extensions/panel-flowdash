"""The dashboard's controls: a slim row above the tiles, or a collapsible panel beside them."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer
from panel_material_ui.widgets.base import MaterialWidget

# Controls in the row are about this wide. The panel leaves its controls the
# width of the sidebar they were laid out for before there was a controls row.
_CONTROL_WIDTH = 240
_PANEL_WIDTH = 320

# Views are laid out for a tile, which most fill with stretch_both. Inside the
# bar there is no height to fill, so the stretched view (and the wrapper, which
# Panel makes stretch to match) would collapse to nothing and the content
# would spill onto the tiles below; in the left panel's column each wrapper
# would instead take an equal share of the height. Sizing the wrapper and the
# view to their content keeps the controls as tall as they are. Widths go the
# other way: a stretched widget keeps its default width as a minimum, which
# would spill out of the wrapper onto the next control, so the view may not
# get wider than the wrapper.
_FIT_CONTENT = """
:host {
  height: auto;
  max-height: none;
  flex: 0 0 auto;
}
[class*="bk-"] {
  height: auto;
  max-height: none;
  flex: 0 0 auto;
  min-width: 0 !important;
  max-width: 100% !important;
}
"""

# Material UI shows an outlined input's label inside the empty box and hides
# its placeholder, and moves the label into the notch once there is a value,
# so in a row of controls an empty one looks unlike its neighbours. These rules
# keep every label in the notch, with the placeholder shown while empty. A
# select's height also follows its font's line height, which differs from a
# text input's with some fonts, so small outlined inputs get MUI's 40px.
_NOTCHED_LABEL = """
.MuiOutlinedInput-root.MuiInputBase-sizeSmall {
  min-height: 40px;
}
.MuiInputLabel-outlined[data-shrink="false"] {
  transform: translate(14px, -9px) scale(0.75);
  max-width: calc(133% - 32px);
}
.MuiOutlinedInput-notchedOutline > legend {
  max-width: 100% !important;
}
:host label[data-shrink="false"] + .MuiInputBase-formControl .MuiInputBase-input::placeholder {
  opacity: 0.6 !important;
}
"""

POSITIONS = ("top", "left")


def _row_width(view) -> int:
    """Return the width of *view* in the row: a select's, or a widget's minimum and margins if wider.

    A layout gets a select's width whatever its minimum, since _FIT_CONTENT lets
    it shrink, but a widget's minimum also sizes its input, which would spill
    out of a narrower wrapper onto the next control.
    """
    minimum = view.min_width if isinstance(view, pn.widgets.WidgetBase) else None
    if not minimum:
        return _CONTROL_WIDTH
    margin = getattr(view, "margin", 0) or 0
    if isinstance(margin, int):
        sides = 2 * margin
    elif len(margin) == 2:
        sides = 2 * margin[1]
    else:
        sides = margin[1] + margin[3]
    return max(_CONTROL_WIDTH, minimum + sides)


def compact_inputs(view):
    """Switch the Material UI widgets in *view* to their small size, where they have one.

    Their labels also stay in the outline's notch, as a filled input shows it.
    """
    if not hasattr(view, "select"):
        return
    for widget in view.select(MaterialWidget):
        if _NOTCHED_LABEL not in widget.stylesheets:
            widget.stylesheets = [*widget.stylesheets, _NOTCHED_LABEL]
        # Selects keep the number of visible rows in `size` and the MUI size in `visual_size`.
        for name in ("visual_size", "size"):
            size = widget.param[name] if name in widget.param else None
            if isinstance(size, param.Selector) and "small" in size.objects:
                if getattr(widget, name) != "small":
                    setattr(widget, name, "small")
                break


_HANDLE_CSS = """
:host {
  position: absolute;
  left: -6px;
  top: 50%;
  transform: translateY(-50%);
  z-index: 2;
}
.fd-control-handle {
  font-family: "Material Icons";
  font-size: 18px;
  line-height: 24px;
  width: 16px;
  height: 24px;
  overflow: hidden;
  cursor: grab;
  color: var(--mui-palette-text-secondary, #666);
  opacity: 0.6;
  user-select: none;
  touch-action: none;
}
.fd-control-handle:hover {
  opacity: 1;
}
"""


# While tiles are arranged the top bar stays in view as the grid scrolls, so a
# tile dragged down a long dashboard, which scrolls it, can still be dropped there.
_STICKY = "fd-controls-sticky"
_STICKY_CSS = """
:host(.fd-controls-sticky) {
  position: sticky;
  top: 0;
  z-index: 3;
  background: var(--mui-palette-background-default, #fff);
}
"""


class ControlsBar(Viewer):
    """The views of the components shown beside the tiles: a top bar and a sidebar.

    Components in the top bar sit in one slim row above the tile grid that
    wraps on narrow screens; those in the sidebar are stacked in a panel left
    of the grid that collapses to a narrow strip. Controls (components
    registered with ``sidebar=True``) go where the dashboard's
    ``controls_position`` says unless they were moved, and controls, KPIs and
    text can be moved between the grid and either area (see
    :func:`panel_flowdash.ui.tile_layout.move_to_area`). Either way the
    inputs use Material UI's small size and the areas take no room while they
    are empty (or the editor's ``controls_bar`` is off, except in full screen,
    where nothing around the editor is visible). :meth:`sync` re-reads the
    editor, which calls it whenever its components change, and
    :meth:`layout` places the areas around the tile grid.
    """

    editor = param.Parameter(doc="The FlowDash editor whose controls are shown.")

    position = param.Selector(
        default="top",
        objects=list(POSITIONS),
        doc="Where controls that were not moved go: the top bar or the sidebar.",
    )

    collapsed = param.Boolean(default=False, doc="Whether the sidebar is collapsed.")

    beside_canvas = param.Boolean(
        default=False,
        doc="""
        Whether the grid is shown beside the canvas (the editor's Split tab).
        The pane is too narrow for a sidebar there, so its components sit in
        the row above the grid too.""",
    )

    placed = param.Dict(
        default={},
        doc="The area of each component shown beside the grid, by instance id.",
    )

    editing = param.Boolean(
        default=False,
        doc="Whether the tiles are being arranged, which gives each component a drag handle.",
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._wrappers: dict[str, pn.Column] = {}
        # The provenance chip each wrapper shows, by instance id, with what it says.
        self._provenance: dict[str, tuple[t.Any, pn.Row]] = {}
        self._handles: dict[str, pn.pane.HTML] = {}
        # The key each component's handle and wrapper carry on the page, by instance id.
        self.keys: dict[str, str] = {}
        self._uid = f"fd{id(self):x}"
        self._key_count = 0
        self._controls = pn.FlexBox(
            # Inputs, switches and sliders differ in height, so they share a centre line.
            align_items="center",
            flex_wrap="wrap",
            margin=0,
            sizing_mode="stretch_width",
            # The widgets' own margins space them as far apart as the tiles.
            gap="0px",
        )
        from panel_flowdash.ui.global_filters import FilterBar

        # The dashboard's global filters, first in the row.
        self._filter_bar = FilterBar(
            filters=self.editor._global_filters, editable=self.editor.editable
        )
        self.editor.param.watch(
            lambda event: setattr(self._filter_bar, "editable", event.new), "editable"
        )
        self._filter_bar.param.watch(lambda _event: self.sync(), "has_content")
        self._row = pn.Column(
            self._filter_bar,
            self._controls,
            # Widgets keep a 10px margin of their own, so the outlines start
            # where the tiles do.
            margin=(4, 16, 0, 0),
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-controls-bar", f"fd-area-top-{self._uid}"],
            stylesheets=[_STICKY_CSS],
        )
        self._hide = pmui.IconButton(
            icon="chevron_left", description="Hide sidebar", size="small", margin=0
        )
        self._hide.on_click(lambda _event: setattr(self, "collapsed", True))
        self._show = pmui.IconButton(
            icon="tune", description="Show sidebar", size="small", margin=0
        )
        self._show.on_click(lambda _event: setattr(self, "collapsed", False))
        panel_sx = {
            "borderRadius": 0,
            "borderRight": 1,
            "borderColor": "divider",
            "bgcolor": "background.paper",
            "px": 1,
            "py": 1,
            "gap": 1.5,
            "overflowY": "auto",
            "flexShrink": 0,
        }
        self._panel = pmui.Paper(
            # No heading: what the sidebar holds can be any control, KPI or text.
            pn.Row(pn.HSpacer(), self._hide, margin=0, align="center"),
            margin=0,
            elevation=0,
            width=_PANEL_WIDTH,
            sizing_mode="stretch_height",
            visible=False,
            css_classes=["fd-controls-panel", f"fd-area-sidebar-{self._uid}"],
            sx=panel_sx,
        )
        # Collapsed, the panel gives way to a strip holding the button that reopens it.
        self._strip = pmui.Paper(
            self._show,
            margin=0,
            elevation=0,
            width=48,
            sizing_mode="stretch_height",
            visible=False,
            css_classes=["fd-controls-strip", f"fd-area-sidebar-{self._uid}"],
            sx={**panel_sx, "alignItems": "center"},
        )
        # Built once: a new Row on every layout would re-render the grid and all its tiles.
        # The grid scrolls beside the sidebar, which so stays in view.
        self._grid_slot = pn.Column(margin=0, sizing_mode="stretch_both", scroll="y-auto")
        self._left_row = pn.Row(
            self._panel, self._strip, self._grid_slot, margin=0, sizing_mode="stretch_both"
        )
        self._side = False
        self._shown: list[pn.Column] = []
        self.editor.param.watch(lambda _event: self.sync(), ["controls_bar", "fullscreen"])

    @property
    def uid(self) -> str:
        """The suffix of the CSS classes that mark this bar's areas on the page."""
        return self._uid

    @property
    def controls(self) -> list:
        """The component views currently shown beside the grid, top bar first."""
        return [wrapper[-1] for wrapper in self._shown]

    @property
    def visible(self) -> bool:
        """Whether there are controls to show."""
        return bool(self._shown)

    @property
    def side(self) -> bool:
        """Whether the sidebar (open or collapsed) is shown, which :meth:`layout` places left of the grid."""
        return self._side

    def _handle(self, key: str) -> pn.pane.HTML:
        handle = self._handles.get(key)
        if handle is None:
            handle = self._handles[key] = pn.pane.HTML(
                f'<div class="fd-control-handle" data-fd-key="{key}" '
                'title="Drag to move">drag_indicator</div>',
                margin=0,
                stylesheets=[_HANDLE_CSS],
            )
        return handle

    def _wrap(self, instance_id: str, view) -> pn.Column:
        # Reusing the wrapper keeps a control from re-rendering when the bar is rebuilt.
        wrapper = self._wrappers.get(instance_id)
        if wrapper is None or wrapper[-1] is not view:
            self._handles.pop(self.keys.get(instance_id, ""), None)
            compact_inputs(view)
            self._key_count += 1
            key = self.keys[instance_id] = f"{self._uid}-{self._key_count}"
            note = pn.Row(
                margin=(0, 0, 0, 10), visible=False, css_classes=["fd-control-provenance"]
            )
            self._provenance[instance_id] = (None, note)
            wrapper = pn.Column(
                self._handle(key),
                note,
                view,
                margin=0,
                styles={"max-width": "100%", "position": "relative"},
                stylesheets=[] if getattr(view, "height", None) else [_FIT_CONTENT],
                css_classes=[f"fd-control-{self._uid}", f"fd-key-{key}"],
            )
        wrapper[0].visible = self.editing
        self._sync_note(instance_id)
        return wrapper

    def _sync_note(self, instance_id: str):
        from panel_flowdash.ui.provenance import provenance_chip

        shown, note = self._provenance.get(instance_id, (None, None))
        if note is None:
            return
        editor = self.editor
        found = editor.node_provenance(instance_id) if editor._provenance_shown() else None
        provenance = found[0] if found is not None else None
        if provenance == shown:
            return
        self._provenance[instance_id] = (provenance, note)
        note[:] = [] if provenance is None else [provenance_chip(provenance)]
        note.visible = provenance is not None

    def sync_provenance(self):
        """Show or hide each component's provenance chip, after an edit or a change of mode."""
        for instance_id in self._wrappers:
            self._sync_note(instance_id)

    @pn.io.hold()
    @param.depends("collapsed", "beside_canvas", "editing", watch=True)
    def sync(self) -> bool:
        """Re-read the editor's areas and lay them out; return whether the sidebar came or went.

        When it did, the caller places the areas around the grid again with :meth:`layout`.
        """
        from panel_flowdash.ui.tile_layout import SIDEBAR, TOP

        editor = self.editor
        shown = editor.controls_bar or editor.fullscreen
        areas = getattr(editor, "_area_views", {}) if shown else {}
        top_views, side_views = list(areas.get(TOP, ())), list(areas.get(SIDEBAR, ()))
        if self.beside_canvas:
            top_views, side_views = top_views + side_views, []
        top = [self._wrap(iid, view) for iid, view in top_views]
        side = [self._wrap(iid, view) for iid, view in side_views]
        self._shown = top + side
        live = {iid for iid, _ in top_views + side_views}
        for iid in [iid for iid in self._wrappers if iid not in live]:
            self._handles.pop(self.keys.pop(iid, ""), None)
            self._provenance.pop(iid, None)
            del self._wrappers[iid]
        self._wrappers.update(
            {iid: w for (iid, _), w in zip(top_views + side_views, self._shown, strict=True)}
        )
        for wrapper in top:
            # In the row a control takes about a select's width, or more where it needs it.
            wrapper.param.update(width=_row_width(wrapper[-1]), sizing_mode="fixed")
        for wrapper in side:
            wrapper.param.update(width=None, sizing_mode="stretch_width")
        if list(self._controls) != top:
            self._controls[:] = top
        self._row.visible = bool(top) or (shown and self._filter_bar.has_content)
        classes = [c for c in self._row.css_classes if c != _STICKY]
        self._row.css_classes = [*classes, _STICKY] if self.editing else classes
        body = side if not self.collapsed else []
        if list(self._panel)[1:] != body:
            self._panel[1:] = body
        self._panel.visible = bool(body)
        self._strip.visible = bool(side) and self.collapsed
        changed, self._side = self._side != bool(side), bool(side)
        return changed

    def layout(self, grid) -> list:
        """Return the workspace items placing the areas around *grid*."""
        if self._side:
            if list(self._grid_slot) != [self._row, grid]:
                self._grid_slot[:] = [self._row, grid]
            return [self._left_row]
        if len(self._grid_slot):
            # The grid and the row may only have one parent once they move out of the slot.
            self._grid_slot[:] = []
        return [self._row, grid]

    def __panel__(self):
        """Render the row of controls (the sidebar is placed by :meth:`layout`)."""
        return self._row
