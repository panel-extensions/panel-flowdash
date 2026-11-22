"""The options of one component's tile, shown in the sidecar's Tile tab.

One vocabulary for every tab: widths are steps of the grid's twelve columns,
heights are bounded by the component's size hints, and the frame, whether the
tile is shown and where the component goes (the grid, the top bar or the
sidebar) are set here too.
"""

from __future__ import annotations

import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.ui.tile_layout import (
    AREA_NAMES,
    FRAME_DEFAULT,
    GRID,
    GRID_COLUMNS,
    area_of,
    frame_choices,
    frame_value,
    move_buttons,
    tile_entry,
    to_columns,
    to_width,
    update_tile,
)

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.component_spec import ComponentSpec

AUTO = "auto"
HEIGHTS = (200, 300, 400, 500, 600, 800)

_COLUMN_NAMES = {
    12: "Full",
    9: "Three quarters",
    8: "Two thirds",
    6: "Half",
    4: "Third",
    3: "Quarter",
}


def width_label(columns: int) -> str:
    """Return the label of a width spanning *columns* of the grid, e.g. "Half (6/12)"."""
    fraction = f"{columns}/{GRID_COLUMNS}"
    name = _COLUMN_NAMES.get(columns)
    return f"{name} ({fraction})" if name else fraction


def width_options() -> dict[str, t.Any]:
    """Return the width choices by label: automatic, then each number of columns, widest first."""
    return {
        "Automatic": AUTO,
        **{width_label(n): n for n in range(GRID_COLUMNS, 0, -1)},
    }


def size_hints(spec: ComponentSpec | None) -> str:
    """Return a sentence naming a component's starting, smallest and largest size."""
    if spec is None:
        return ""

    def size(hint):
        return " x ".join(f"{hint[k]} px" for k in ("width", "height") if k in hint)

    parts = [
        f"{label} {size(hint)}"
        for label, hint in (
            ("Starts at", spec.default_size),
            ("at least", spec.min_size),
            ("at most", spec.max_size),
        )
        if hint
    ]
    return ("Size hints: " + ", ".join(parts) + ".") if parts else ""


def _caption(text: str, **kwargs) -> pmui.Typography:
    return pmui.Typography(text, variant="caption", sx={"color": "text.secondary"}, **kwargs)


class TileOptions:
    """The tile options of the component *instance_id*, kept in step with the grid.

    Changes made elsewhere, e.g. a tile resized or hidden on the grid or a frame
    set by the agent, are shown here, and :meth:`release` stops following them.
    """

    def __init__(
        self,
        editor,
        instance_id: str,
        item: dict,
        spec: ComponentSpec | None,
        style: t.Callable[[Viewable], None] | None = None,
    ):
        self.editor = editor
        self.instance_id = instance_id
        self.item = item
        self.spec = spec
        self._syncing = False
        self._area: str | None = None
        self._releases: list[t.Callable[[], None]] = []
        self.show_switch = pmui.Switch(label="Show on dashboard", margin=(4, 0, 4, 0))
        self.width_select = pmui.Select(
            label="Width",
            options=width_options(),
            sizing_mode="stretch_width",
            margin=(8, 4, 8, 0),
        )
        self.height_select = pmui.Select(
            label="Height",
            options={"Automatic": AUTO},
            value=AUTO,
            sizing_mode="stretch_width",
            margin=(8, 0, 8, 4),
        )
        self.frame_select = pmui.Select(
            label="Frame",
            options={"Default": FRAME_DEFAULT},
            value=FRAME_DEFAULT,
            description="Whether the tile is drawn as a card in view mode",
            sizing_mode="stretch_width",
            margin=(12, 0, 8, 0),
        )
        for widget in (self.width_select, self.height_select, self.frame_select):
            if style is not None:
                style(widget)
        self.size_hint = _caption("", margin=(0, 0, 4, 0))
        self.band_hint = _caption("", margin=(0, 0, 4, 0), css_classes=["fd-tile-band-hint"])
        self.arrange_button = pmui.Button(
            label="Arrange tiles",
            icon="dashboard",
            variant="text",
            size="small",
            margin=(12, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        self.arrange_button.on_click(lambda _event: setattr(editor, "mode", "dashboard"))
        self._moves = pn.FlexBox(margin=0, gap="0px 16px")
        # A switch for each global filter, off where the view opts out of it.
        self._filter_switches: dict[str, pmui.Switch] = {}
        self.filters = pn.Column(margin=(12, 0, 0, 0), sizing_mode="stretch_width", visible=False)
        self.layout = pn.Column(
            margin=0, sizing_mode="stretch_width", css_classes=["fd-tile-options"]
        )
        self.show_switch.param.watch(self._on_show, "value")
        self.width_select.param.watch(self._on_width, "value")
        self.height_select.param.watch(self._on_height, "value")
        self.frame_select.param.watch(self._on_frame, "value")
        for owner, names in (
            (editor._tile_grid, ["layout", "objects", "responsive_layouts", "preview_band"]),
            (editor.settings, ["tile_cards"]),
            (editor._view_controls, ["placed"]),
            (editor, ["mode"]),
            (editor._global_filters, ["filters"]),
        ):
            watcher = owner.param.watch(self._on_changed, names)
            self._releases.append(partial(owner.param.unwatch, watcher))
        self.sync()

    def release(self):
        for release in self._releases:
            release()
        self._releases = []

    def _on_changed(self, *_events):
        if not self._syncing:
            self.sync()

    @pn.io.hold()
    def sync(self):
        """Show the tile's current options, rebuilding the tab when the component changed area."""
        editor = self.editor
        area = area_of(editor, self.item)
        self._syncing = True
        try:
            if area != self._area or not self.layout.objects:
                self._area = area
                self.layout[:] = self._objects(area)
            if area == GRID:
                self._sync_values()
            self._sync_filters()
        finally:
            self._syncing = False

    def _objects(self, area: str | None) -> list[Viewable]:
        moves = move_buttons(self.editor, self.item, then=self._refocus, margin=(4, 0, 0, 0))
        # A button with a tooltip cannot be given the focus, see _refocus.
        for button in moves:
            button.description = None
        self._moves[:] = moves
        if area != GRID:
            where = AREA_NAMES.get(area, "sidebar")
            return [
                _caption(
                    f"This component is shown in the {where} of the dashboard rather than "
                    "as a tile.",
                    margin=(12, 0, 0, 0),
                ),
                self._moves,
                self.filters,
            ]
        objects: list[Viewable] = [
            self.show_switch,
            pn.Row(self.width_select, self.height_select, margin=0, sizing_mode="stretch_width"),
            self.size_hint,
            self.band_hint,
            self.frame_select,
            self.filters,
            self._moves,
            self.arrange_button,
        ]
        return objects

    def _refocus(self):
        # The pressed button went with the rebuilt tab, which may be a new one for
        # the same component; the first move left there takes the focus.
        def focus():
            current = getattr(self.editor._inspector, "tile_options", None) or self
            button = next(iter(current._moves.objects), None)
            if button is not None:
                button.focus()

        if pn.state.curdoc is None or pn.state.curdoc.session_context is None:
            focus()
        else:
            pn.state.execute(focus, schedule=True)

    def _sync_values(self):
        editor = self.editor
        entry = tile_entry(editor, self.instance_id)
        spec = self.spec
        width = entry.get("width")
        self.show_switch.value = entry.get("visible", True)
        self.width_select.value = to_columns(width) if isinstance(width, (int, float)) else AUTO
        height = entry.get("height", ((spec and spec.default_size) or {}).get("height"))
        low = ((spec and spec.min_size) or {}).get("height", 0)
        high = ((spec and spec.max_size) or {}).get("height", float("inf"))
        choices = {h for h in HEIGHTS if low <= h <= high}
        if isinstance(height, (int, float)):
            height = round(height)
            choices.add(height)
        else:
            height = AUTO
        self.height_select.param.update(
            options={"Automatic": AUTO, **{f"{h} px": h for h in sorted(choices)}},
            value=height,
        )
        self.frame_select.param.update(
            options=frame_choices(editor, self.instance_id), value=frame_value(entry)
        )
        # The browser shows the label of the value, which changing only the options does not resend.
        self.frame_select.param.trigger("value")
        self.size_hint.object = size_hints(spec)
        self.size_hint.visible = bool(self.size_hint.object)
        band = editor._layout_panel.band_hint() if editor.mode == "dashboard" else ""
        self.band_hint.object = band
        self.band_hint.visible = bool(band)
        self.arrange_button.visible = editor.mode != "dashboard"

    def _sync_filters(self):
        """Show a switch per global filter, on unless this view opts out of it."""
        from panel_flowdash.global_filters import filter_label

        filters = self.editor._global_filters.filters
        switches = []
        for entry in filters:
            column = entry["column"]
            switch = self._filter_switches.get(column)
            if switch is None:
                switch = self._filter_switches[column] = pmui.Switch(
                    margin=(0, 0, 0, 0), css_classes=["fd-tile-filter-switch"]
                )
                switch.param.watch(partial(self._on_filter, column), "value")
            switch.label = f"Filtered by {filter_label(entry)}"
            switch.value = self.instance_id not in entry.get("exclude", ())
            switches.append(switch)
        self._filter_switches = {
            c: w
            for c, w in self._filter_switches.items()
            if any(f["column"] == c for f in filters)
        }
        objects = []
        if switches:
            objects = [
                pmui.Typography("Global filters", variant="subtitle2", margin=(0, 0, 2, 0)),
                *switches,
                _caption(
                    "Turn a filter off for a view that should always show every row, "
                    "such as a reference chart.",
                    margin=(2, 0, 0, 0),
                ),
            ]
        if [o for o in self.filters if isinstance(o, pmui.Switch)] != switches:
            self.filters[:] = objects
        self.filters.visible = bool(switches)

    def _on_filter(self, column: str, event):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.editor.exclude_from_filter(column, self.instance_id, excluded=not event.new)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    def _update(self, **patch):
        self._syncing = True
        try:
            update_tile(self.editor, self.instance_id, **patch)
        finally:
            self._syncing = False

    def _on_show(self, event):
        if not self._syncing:
            self._update(visible=event.new)

    def _on_width(self, event):
        if not self._syncing and event.new is not None:
            self._update(width=None if event.new == AUTO else to_width(event.new))

    def _on_height(self, event):
        if not self._syncing and event.new is not None:
            self._update(height=None if event.new == AUTO else event.new)

    def _on_frame(self, event):
        if not self._syncing and event.new is not None:
            self._update(framed=None if event.new == FRAME_DEFAULT else event.new)
