"""The dashboard sections of the sidecar, shown while nothing is selected.

They are the screen size the tiles are arranged for and the placed components
that have no tile on the dashboard; "Arrange for" is shown in the Layout tab
only. The options of one tile are in the sidecar's Tile tab, see
:mod:`panel_flowdash.ui.tile_options`.
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
from panel_flowdash.ui.tile_layout import GRID, move_buttons, off_grid, update_tile

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.ui.tile_layout import OffGrid

_SECTION_SX = {"color": "text.secondary", "fontWeight": 500}


_ICON_CSS = """
span { font-family: "Material Icons"; font-size: 18px; line-height: 1;
  font-feature-settings: "liga"; color: var(--mui-palette-text-secondary); }
"""


# Applied to the tile grid. TileGrid hides its handles outside edit mode, and
# the Split tab hides them too, so only the grid in the Layout tab shows them. The handles are faint icons by
# default; here every tile gets a dashed frame, a drag pill at the top and a
# resize grip in the corner that stay visible. The tiles keep the padding they
# have in view mode, where TileGrid draws no card, so content does not move
# when the dashboard switches between arranging and viewing.
GRID_EDIT_CSS = """
:host {
  --tile-padding: 0px;
}
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
:host(.fd-unframed-band) .muuri-grid.muuri-constrained { box-shadow: none; }
:host(.fd-split-preview) .muuri-handle { display: none !important; }
:host(.fd-split-preview) .muuri-grid .muuri-grid-item:not(:hover):not(.muuri-selected) { outline: none; }
"""


def _caption(text: str, **kwargs) -> pmui.Typography:
    return pmui.Typography(text, variant="caption", sx={"color": "text.secondary"}, **kwargs)


def _heading(text: str, margin=(16, 0, 8, 0)) -> pmui.Typography:
    return pmui.Typography(text, variant="subtitle2", margin=margin, sx=_SECTION_SX)


class LayoutPanel(Viewer):
    """The bands the tiles are arranged for, and the components off the grid."""

    band = param.String(
        default=None,
        allow_None=True,
        doc="Label of the breakpoint band the tiles are arranged for, or None for the base band.",
    )

    editor = param.Parameter(doc="The FlowDash editor whose tile grid is arranged.")

    visible = param.Boolean(
        default=True, doc="Whether the Layout tab, which arranges the tiles, is shown."
    )

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
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-arrange-for"],
        )
        self.band_select.param.watch(self._on_band_selected, "value")
        self.reset_band_button.on_click(self._on_reset_band)
        self._off_grid = pn.Column(margin=0, sizing_mode="stretch_width")
        self.off_grid_section = pn.Column(
            _heading("Not on the dashboard"),
            _caption(
                "Placed components without a tile: those shown beside the dashboard "
                "and hidden tiles, such as the data sources the assistant left off. "
                "Show a hidden one to put it back.",
                margin=(0, 0, 8, 0),
            ),
            self._off_grid,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-off-grid"],
        )
        # Tiles hidden in the browser arrive as a new layout.
        self.editor._tile_grid.param.watch(self._on_layout, ["layout", "objects"])
        self.editor._tile_grid.param.watch(lambda _event: self.sync_bands(), "responsive_layouts")
        self.editor._view_controls.param.watch(self._on_layout, "placed")
        self.sync_bands()

    @property
    def arrange_section(self) -> Viewable:
        """The band picker, shown only when the grid has more than one band."""
        return self._bands

    def _on_layout(self, _event):
        if self.editor.editable and not self._syncing:
            self.refresh()

    @param.depends("visible", watch=True)
    def _on_visible(self):
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

    def band_hint(self) -> str:
        """Say which band's layout a tile's width and height apply to, if not the one shown."""
        grid = self.editor._tile_grid
        names = band_names(grid.breakpoints)
        sources = band_sources(grid.breakpoints, grid.reference_width, grid.responsive_layouts)
        band = self.band_select.value
        base = next((names[label] for label, source in sources.items() if source == BASE), None)
        if band is not None and sources.get(band) != BASE and base is not None:
            return f"Width and height set here apply to the {base} layout."
        return ""

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
            self.editor._record_edit("Changed the tile layout", op="layout")

    @pn.io.hold()
    def refresh(self):
        """Re-read the components off the grid."""
        editor = self.editor
        listed = off_grid(editor)
        self._off_grid[:] = (
            [self._off_grid_row(entry) for entry in listed]
            if listed
            else [_caption("Every placed component has a tile.")]
        )

    def _off_grid_row(self, entry: OffGrid) -> Viewable:
        icon = pn.pane.HTML(
            f'<span aria-hidden="true">{"visibility_off" if entry.hidden else "tune"}</span>',
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
        elif entry.area != GRID:
            item = next(
                i for i in self.editor._tile_items if i["instance_id"] == entry.instance_id
            )
            objects += move_buttons(self.editor, item, areas=[GRID], margin=0)
        return pmui.Paper(
            pn.Row(*objects, margin=0, sizing_mode="stretch_width", align="center"),
            variant="outlined",
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
            sx={"px": 1.5, "py": 1},
        )

    def show_tile(self, instance_id: str):
        """Put a hidden tile back on the dashboard."""
        self._syncing = True
        try:
            update_tile(self.editor, instance_id, visible=True)
        finally:
            self._syncing = False
        self.refresh()

    def __panel__(self):
        """Render the sections, as the sidecar does while nothing is selected."""
        return pn.Column(
            self._bands, self.off_grid_section, margin=(0, 16, 16, 16), sizing_mode="stretch_width"
        )
