"""The dashboard's controls: a slim row above the tiles, or a collapsible panel beside them."""

from __future__ import annotations

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
# keep every label in the notch, with the placeholder shown while empty.
_NOTCHED_LABEL = """
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


class ControlsBar(Viewer):
    """The views of the components registered with ``sidebar=True``, beside the tiles.

    With ``position="top"`` they sit in one slim row above the tile grid that
    wraps on narrow screens; with ``position="left"`` they are stacked in a
    panel left of the grid that collapses to a narrow strip. Either way the
    inputs use Material UI's small size and the controls take no room while
    the dashboard has none (or the editor's ``controls_bar`` is off, except in
    full screen, where nothing around the editor is visible).
    :meth:`sync` re-reads the editor, which calls it whenever its components
    change, and :meth:`layout` places the controls around the tile grid.
    """

    editor = param.Parameter(doc="The FlowDash editor whose controls are shown.")

    position = param.Selector(
        default="top", objects=list(POSITIONS), doc="Where the controls sit around the tiles."
    )

    collapsed = param.Boolean(default=False, doc="Whether the left panel is collapsed.")

    beside_canvas = param.Boolean(
        default=False,
        doc="""
        Whether the grid is shown beside the canvas (the editor's Split tab).
        The pane is too narrow for a panel there, so the controls sit in the
        row above the grid whatever the position.""",
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._wrappers: dict[int, pn.Column] = {}
        self._controls = pn.FlexBox(
            align_items="flex-start",
            flex_wrap="wrap",
            margin=0,
            sizing_mode="stretch_width",
            # The widgets' own margins space them as far apart as the tiles.
            gap="0px",
        )
        self._row = pn.Column(
            self._controls,
            # Widgets keep a 10px margin of their own, so the outlines start
            # where the tiles do.
            margin=(4, 16, 0, 0),
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-controls-bar"],
        )
        self._hide = pmui.IconButton(
            icon="chevron_left", description="Hide controls", size="small", margin=0
        )
        self._hide.on_click(lambda _event: setattr(self, "collapsed", True))
        self._show = pmui.IconButton(
            icon="tune", description="Show controls", size="small", margin=0
        )
        self._show.on_click(lambda _event: setattr(self, "collapsed", False))
        self._heading = pmui.Typography(
            "Controls",
            variant="overline",
            margin=0,
            sx={"color": "text.secondary", "letterSpacing": "0.08em", "lineHeight": 2},
        )
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
            pn.Row(self._heading, pn.HSpacer(), self._hide, margin=0, align="center"),
            margin=0,
            elevation=0,
            width=_PANEL_WIDTH,
            sizing_mode="stretch_height",
            visible=False,
            css_classes=["fd-controls-panel"],
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
            css_classes=["fd-controls-strip"],
            sx={**panel_sx, "alignItems": "center"},
        )
        # Built once: a new Row on every layout would re-render the grid and all its tiles.
        self._grid_slot = pn.Column(margin=0, sizing_mode="stretch_both")
        self._left_row = pn.Row(
            self._panel, self._strip, self._grid_slot, margin=0, sizing_mode="stretch_both"
        )
        self.editor.param.watch(lambda _event: self.sync(), ["controls_bar", "fullscreen"])

    @property
    def controls(self) -> list:
        """The component views currently shown as controls."""
        return [wrapper[0] for wrapper in self._wrappers.values()]

    @property
    def visible(self) -> bool:
        """Whether there are controls to show."""
        return bool(self._wrappers)

    def _wrap(self, view) -> pn.Column:
        # Reusing the wrapper keeps a control from re-rendering when the bar is rebuilt.
        wrapper = self._wrappers.get(id(view))
        if wrapper is None or wrapper[0] is not view:
            compact_inputs(view)
            wrapper = pn.Column(
                view,
                margin=0,
                styles={"max-width": "100%"},
                stylesheets=[] if getattr(view, "height", None) else [_FIT_CONTENT],
            )
        return wrapper

    @property
    def _left(self) -> bool:
        return self.position == "left" and not self.beside_canvas

    @pn.io.hold()
    @param.depends("position", "collapsed", "beside_canvas", watch=True)
    def sync(self):
        """Re-read the editor's controls and lay them out for the position."""
        editor = self.editor
        views = list(editor.sidebar) if editor.controls_bar or editor.fullscreen else []
        wrappers = [self._wrap(view) for view in views]
        self._wrappers = {id(w[0]): w for w in wrappers}
        left = self._left
        for wrapper in wrappers:
            # In the panel a control takes its full width; in the row about a select's.
            wrapper.param.update(
                width=None if left else _CONTROL_WIDTH,
                sizing_mode="stretch_width" if left else "fixed",
            )
        top = [] if left else wrappers
        if list(self._controls) != top:
            self._controls[:] = top
        self._row.visible = bool(top)
        body = wrappers if left and not self.collapsed else []
        if list(self._panel)[1:] != body:
            self._panel[1:] = body
        self._panel.visible = bool(body)
        self._strip.visible = left and bool(wrappers) and self.collapsed

    def layout(self, grid) -> list:
        """Return the workspace items placing the controls around *grid*."""
        if self._left:
            if list(self._grid_slot) != [grid]:
                self._grid_slot[:] = [grid]
            return [self._left_row]
        if len(self._grid_slot):
            # The grid may only have one parent once it moves to the row's side.
            self._grid_slot[:] = []
        return [self._row, grid]

    def __panel__(self):
        """Render the row of controls (the panel is placed by :meth:`layout`)."""
        return self._row
