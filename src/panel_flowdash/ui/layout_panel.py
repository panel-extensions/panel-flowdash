"""The panel beside the tile grid in the Layout tab of the editor.

It picks the screen size the tiles are arranged for, changes the size of one
tile at a time, which is more precise than dragging its corner, and lists the
placed components that have no tile on the dashboard with the reason, so a
tile hidden with its close handle can be put back.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.ui.text import text_typography
from panel_flowdash.ui.tile_bands import (
    BASE,
    CUSTOM,
    band_names,
    band_sources,
    band_summary,
    base_band,
)
from panel_flowdash.ui.tile_layout import (
    GRID_COLUMNS,
    HIDDEN,
    grid_tiles,
    off_grid,
    tile_entry,
    tile_title,
    to_columns,
    to_width,
    update_tile,
)

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.ui.tile_layout import OffGrid

LAYOUT_PANEL_WIDTH = 320
_COLLAPSED_WIDTH = 44

_SECTION_SX = {"color": "text.secondary", "fontWeight": 500}

_AUTO = "auto"
_HEIGHTS = (200, 300, 400, 500, 600, 800)

_REASON_ICONS = {HIDDEN: "visibility_off"}

_ICON_CSS = """
span { font-family: "Material Icons"; font-size: 18px; line-height: 1;
  font-feature-settings: "liga"; color: var(--mui-palette-text-secondary); }
"""


# Applied to the tile grid. TileGrid hides its handles outside edit mode, so
# only the grid in the Layout tab shows them. The handles are faint icons by
# default; here every tile gets a dashed frame, a drag pill at the top and a
# resize grip in the corner that stay visible.
GRID_EDIT_CSS = """
.muuri-grid:not(.muuri-no-handles) .muuri-grid-item {
  outline: 1px dashed var(--mui-palette-divider, rgba(127, 127, 127, 0.5));
  outline-offset: 2px;
}
.muuri-grid:not(.muuri-no-handles) .muuri-grid-item:hover {
  outline: 2px solid var(--mui-palette-primary-main, #0072b5);
}
.muuri-handle { opacity: 0.7 !important; }
.muuri-grid-item:hover .muuri-handle, .muuri-handle:focus { opacity: 1 !important; }
.muuri-handle.drag {
  top: 4px;
  left: calc(50% - 18px);
  width: 36px;
  height: 16px;
  padding: 2px 9px;
  box-sizing: border-box;
  border-radius: 8px;
  -webkit-mask-image: none;
  mask-image: none;
  color: var(--mui-palette-text-secondary, rgba(0, 0, 0, 0.6));
  background: radial-gradient(circle, currentColor 1.2px, transparent 1.6px) center / 6px 6px
      repeat content-box,
    var(--mui-palette-action-selected, rgba(127, 127, 127, 0.16));
}
.muuri-handle.resize {
  width: 18px;
  height: 18px;
  bottom: 2px;
  right: 2px;
  background-color: var(--mui-palette-primary-main, #0072b5);
}
.muuri-grid:not(.muuri-no-handles) .muuri-grid-item.muuri-selected {
  outline: 2px solid var(--mui-palette-primary-main, #0072b5);
  outline-offset: 5px;
}
.muuri-no-handles .muuri-grid-item.muuri-selected { outline: none; }
:host(.fd-unframed-band) .muuri-grid.muuri-constrained { box-shadow: none; }
"""


def _caption(text: str, **kwargs) -> pmui.Typography:
    return pmui.Typography(text, variant="caption", sx={"color": "text.secondary"}, **kwargs)


def _heading(text: str, margin=(16, 0, 8, 0)) -> pmui.Typography:
    return pmui.Typography(text, variant="subtitle2", margin=margin, sx=_SECTION_SX)


class LayoutPanel(Viewer):
    """A collapsible panel with the screen size, the selected tile and the components off the grid."""

    band = param.String(
        default=None,
        allow_None=True,
        doc="Label of the breakpoint band the tiles are arranged for, or None for the base band.",
    )

    collapsed = param.Boolean(default=False, doc="Whether the panel is folded to a narrow strip.")

    editor = param.Parameter(doc="The FlowDash editor whose tile grid is arranged.")

    selected = param.String(
        default=None, allow_None=True, doc="Instance id of the tile whose options are shown."
    )

    visible = param.Boolean(default=True, doc="Whether the panel is shown.")

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self.band_select = pmui.RadioButtonGroup(
            options={}, size="small", sizing_mode="stretch_width", margin=(0, 0, 8, 0)
        )
        self.band_summary = _caption("", margin=0)
        self.reset_band_button = pmui.Button(
            label="Reset to automatic",
            icon="restart_alt",
            variant="text",
            size="small",
            margin=(4, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        self._bands = pn.Column(
            _heading("Arrange for", margin=(0, 0, 8, 0)),
            self.band_select,
            self.band_summary,
            self.reset_band_button,
            pmui.Divider(margin=(16, 0, 16, 0)),
            margin=0,
            sizing_mode="stretch_width",
        )
        self.band_select.param.watch(self._on_band_selected, "value")
        self.reset_band_button.on_click(self._on_reset_band)
        self.tile_select = pmui.Select(
            label="Tile", options={}, sizing_mode="stretch_width", margin=(0, 0, 8, 0)
        )
        self.width_select = pmui.Select(
            label="Width",
            options={f"{n} of {GRID_COLUMNS}": n for n in range(1, GRID_COLUMNS + 1)},
            sizing_mode="stretch_width",
            margin=(8, 4, 8, 0),
        )
        self.height_select = pmui.Select(
            label="Height",
            options={"Automatic": _AUTO},
            value=_AUTO,
            sizing_mode="stretch_width",
            margin=(8, 0, 8, 4),
        )
        self.hide_button = pmui.Button(
            label="Hide tile",
            icon="visibility_off",
            variant="text",
            size="small",
            margin=(4, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        self.size_hint = _caption("", margin=(0, 0, 4, 0))
        self.band_hint = _caption("", margin=(0, 0, 4, 0))
        self._tile_options = pn.Column(
            pn.Row(self.width_select, self.height_select, margin=0, sizing_mode="stretch_width"),
            self.size_hint,
            self.band_hint,
            self.hide_button,
            margin=0,
            sizing_mode="stretch_width",
        )
        self._no_tile = _caption("Place a component with a view to arrange its tile.")
        self._off_grid = pn.Column(margin=0, sizing_mode="stretch_width")
        self.tile_select.param.watch(self._on_tile_selected, "value")
        self.width_select.param.watch(self._on_width, "value")
        self.height_select.param.watch(self._on_height, "value")
        self.hide_button.on_click(self._on_hide)

        self.collapse_button = pmui.IconButton(
            icon="keyboard_double_arrow_right",
            description="Hide layout panel",
            size="small",
            margin=(8, 4, 0, 0),
            align="end",
        )
        self.expand_button = pmui.IconButton(
            icon="keyboard_double_arrow_left",
            description="Show layout panel",
            size="small",
            margin=(8, 4),
        )
        self.collapse_button.on_click(lambda _event: setattr(self, "collapsed", True))
        self.expand_button.on_click(lambda _event: setattr(self, "collapsed", False))
        self._body = pn.Column(
            self._bands,
            _heading("Selected tile", margin=(0, 0, 12, 0)),
            self.tile_select,
            self._tile_options,
            self._no_tile,
            pmui.Divider(margin=(16, 0, 0, 0)),
            _heading("Not on the dashboard"),
            _caption(
                "Placed components without a tile: those shown beside the dashboard "
                "and tiles you hid. Show a hidden one to put it back.",
                margin=(0, 0, 8, 0),
            ),
            self._off_grid,
            sizing_mode="stretch_width",
            margin=(0, 16, 16, 16),
        )
        self._panel = pmui.Column(
            self.collapse_button,
            self._body,
            self.expand_button,
            width=LAYOUT_PANEL_WIDTH,
            sizing_mode="stretch_height",
            scroll="y-auto",
            margin=0,
            visible=self.param.visible,
            sx={"borderLeft": 1, "borderColor": "divider", "bgcolor": "background.paper"},
        )
        self._sync_collapsed()
        # Tiles hidden or resized in the browser arrive as a new layout.
        self.editor._tile_grid.param.watch(self._on_layout, ["layout", "objects"])
        self.editor._tile_grid.param.watch(lambda _event: self.sync_bands(), "responsive_layouts")
        self.sync_bands()

    def _on_layout(self, _event):
        if self.visible and not self._syncing:
            self.refresh()

    @param.depends("visible", watch=True)
    def _on_visible(self):
        if self.visible:
            self._follow_canvas_selection()
            self.refresh()

    def _follow_canvas_selection(self):
        selected = self.editor._selected_tile_id()
        if selected is not None:
            self.selected = selected

    def select(self, instance_id: str):
        """Show the options of the tile of *instance_id*, e.g. one clicked on the grid."""
        if instance_id == self.selected:
            return
        self.selected = instance_id
        if self.visible:
            self.refresh()

    # ------------------------------------------------------------------
    # Screen sizes
    # ------------------------------------------------------------------

    @pn.io.hold()
    def sync_bands(self):
        """Show the bands of the grid's breakpoints, the one arranged for and how each is arranged."""
        grid = self.editor._tile_grid
        names = band_names(grid.breakpoints)
        sources = band_sources(grid.breakpoints, grid.reference_width, grid.responsive_layouts)
        band = (
            self.band if self.band in names else base_band(grid.breakpoints, grid.reference_width)
        )
        self._syncing = True
        try:
            self.band_select.param.update(
                options={name: label for label, name in names.items()}, value=band
            )
        finally:
            self._syncing = False
        self._bands.visible = len(names) > 1
        self.band_summary.object = band_summary(names, sources)
        self.reset_band_button.visible = sources.get(band) == CUSTOM
        base = next((names[label] for label, source in sources.items() if source == BASE), None)
        self.band_hint.object = (
            f"Width and height set here apply to the {base} layout."
            if band is not None and sources.get(band) != BASE
            else ""
        )
        self.band_hint.visible = bool(self.band_hint.object)

    def _on_band_selected(self, event):
        if self._syncing or event.new is None:
            return
        self.band = event.new
        self.editor._sync_preview_band()

    def _on_reset_band(self, _event):
        grid = self.editor._tile_grid
        band = self.band_select.value
        if band in grid.responsive_layouts:
            grid.reset_responsive_layout(band)
            self.editor._record_edit("Changed the tile layout")

    @pn.io.hold()
    def refresh(self):
        """Re-read the tiles and the components off the grid."""
        editor = self.editor
        tiles = grid_tiles(editor)
        ids = [item["instance_id"] for item in tiles]
        if self.selected not in ids:
            self.selected = ids[0] if ids else None
        self._syncing = True
        try:
            self.tile_select.param.update(
                options=self._tile_options_for(tiles), value=self.selected
            )
            self._sync_tile_options()
        finally:
            self._syncing = False
        self.tile_select.visible = self._tile_options.visible = bool(tiles)
        self._no_tile.visible = not tiles
        listed = off_grid(editor)
        self._off_grid[:] = (
            [self._off_grid_row(entry) for entry in listed]
            if listed
            else [_caption("Every placed component has a tile.")]
        )

    def _tile_options_for(self, tiles: list[dict]) -> dict[str, str]:
        options: dict[str, str] = {}
        for item in tiles:
            title = label = tile_title(self.editor, item)
            n = 1
            while label in options:
                n += 1
                label = f"{title} ({n})"
            options[label] = item["instance_id"]
        return options

    def _sync_tile_options(self):
        if self.selected is None:
            return
        entry = tile_entry(self.editor, self.selected)
        spec = self._spec(self.selected)
        height = entry.get("height", ((spec and spec.default_size) or {}).get("height"))
        low = ((spec and spec.min_size) or {}).get("height", 0)
        high = ((spec and spec.max_size) or {}).get("height", float("inf"))
        choices = {h for h in _HEIGHTS if low <= h <= high}
        if isinstance(height, (int, float)):
            height = round(height)
            choices.add(height)
        else:
            height = _AUTO
        self.width_select.value = to_columns(entry.get("width"))
        self.height_select.param.update(
            options={"Automatic": _AUTO, **{f"{h} px": h for h in sorted(choices)}},
            value=height,
        )
        self.size_hint.object = self._size_hint(spec)
        self.size_hint.visible = bool(self.size_hint.object)

    def _spec(self, instance_id: str):
        item = next((i for i in self.editor._tile_items if i["instance_id"] == instance_id), None)
        if item is None or "placeholder" in item:
            return None
        return self.editor._component_specs.get(item["component_id"])

    @staticmethod
    def _size_hint(spec) -> str:
        if spec is None:
            return ""
        parts = [
            f"{label} {hint['width']} px wide"
            for label, hint in (("at least", spec.min_size), ("at most", spec.max_size))
            if hint and "width" in hint
        ]
        return ("Kept " + " and ".join(parts) + ".") if parts else ""

    def _off_grid_row(self, entry: OffGrid) -> Viewable:
        icon = pn.pane.HTML(
            f'<span aria-hidden="true">{_REASON_ICONS.get(entry.reason, "tune")}</span>',
            width=20,
            margin=(2, 10, 0, 0),
            stylesheets=[_ICON_CSS],
        )
        objects: list[Viewable] = [
            icon,
            pn.Column(
                text_typography(entry.title, variant="body2", margin=0),
                _caption(entry.reason, margin=0),
                margin=0,
                sizing_mode="stretch_width",
            ),
        ]
        if entry.restorable:
            show = pmui.Button(
                label="Show",
                icon="visibility",
                variant="text",
                size="small",
                margin=0,
                description=f"Show {entry.title} on the dashboard",
                sx={"textTransform": "none"},
            )
            show.on_click(lambda _event, iid=entry.instance_id: self.show_tile(iid))
            objects.append(show)
        return pmui.Paper(
            pn.Row(*objects, margin=0, sizing_mode="stretch_width", align="center"),
            variant="outlined",
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
            sx={"px": 1.5, "py": 1},
        )

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    def _update(self, **patch):
        if self.selected is None:
            return
        self._syncing = True
        try:
            update_tile(self.editor, self.selected, **patch)
        finally:
            self._syncing = False
        self.refresh()

    def _on_tile_selected(self, event):
        if not self._syncing and event.new is not None:
            self.selected = event.new
            self._syncing = True
            try:
                self._sync_tile_options()
            finally:
                self._syncing = False
            self.editor._set_selected_tile(event.new)

    def _on_width(self, event):
        if not self._syncing and event.new is not None:
            self._update(width=to_width(event.new))

    def _on_height(self, event):
        if not self._syncing and event.new is not None:
            self._update(height=None if event.new == _AUTO else event.new)

    def _on_hide(self, _event):
        self._update(visible=False)

    def show_tile(self, instance_id: str):
        """Put a hidden tile back on the dashboard and select it."""
        self.selected = instance_id
        self._update(visible=True)

    # ------------------------------------------------------------------
    # Collapsing
    # ------------------------------------------------------------------

    @param.depends("collapsed", watch=True)
    def _sync_collapsed(self):
        collapsed = self.collapsed
        self.collapse_button.visible = self._body.visible = not collapsed
        self.expand_button.visible = collapsed
        self._panel.width = _COLLAPSED_WIDTH if collapsed else LAYOUT_PANEL_WIDTH

    def __panel__(self):
        """Render the panel."""
        return self._panel
