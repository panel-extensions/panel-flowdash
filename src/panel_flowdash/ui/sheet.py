"""Bottom sheets: where the editor's side panels go on a phone, and the buttons opening them."""

from __future__ import annotations

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent

from panel_flowdash.ui.dashboard_bar import SHOW_ON_PHONE

# A sheet scrolls as a whole; the panels in it take their natural height.
SHEET_SX = {"borderTopLeftRadius": 12, "borderTopRightRadius": 12, "overflowY": "auto"}

TOUCH_TARGET_SX = {"width": "44px", "height": "44px"}

INSPECTOR_SHEET_HEIGHT = "55dvh"

# Set on a sheet button while the inspector's sheet is open, to lift it above the sheet.
ABOVE_SHEET_CLASS = "fd-above-sheet"

# Over the canvas's bottom right corner, clear of the status bar and the canvas
# attribution, or above the inspector's sheet while it is open. Over that sheet
# (z-index 1200) but under a modal sheet's backdrop (1300).
_BUTTON_CSS = f"""
:host {{ position: fixed !important; right: 16px; bottom: 60px; z-index: 1250;
  transition: bottom 225ms cubic-bezier(0, 0, 0.2, 1); }}
:host(.{ABOVE_SHEET_CLASS}) {{ bottom: calc({INSPECTOR_SHEET_HEIGHT} + 16px); }}
"""


class SheetLabel(JSComponent):
    """Names the sheet it is placed in for assistive technology.

    The Drawer's paper takes no attributes, and an ``aria-labelledby`` cannot
    reach the title in another shadow root, so the label is set on the paper
    (or its dialog) from inside. A temporary sheet renders a new paper each
    time it opens, which resizes this element, so the label is set again then.
    """

    label = param.String(doc="The sheet's accessible name, its visible title.")

    _esm = """
    export function render({ el, model }) {
      const apply = () => {
        let node = el
        while (node) {
          if (node.getAttribute && (node.getAttribute("role") === "dialog" ||
              node.classList.contains("MuiDrawer-paper"))) {
            if (!node.getAttribute("role")) { node.setAttribute("role", "region") }
            node.setAttribute("aria-label", model.label)
            if (node.getAttribute("role") === "dialog") { return }
          }
          node = node.parentElement || (node.getRootNode && node.getRootNode().host)
        }
      }
      const observer = new ResizeObserver(apply)
      observer.observe(el)
      model.on("label", apply)
      model.on("remove", () => observer.disconnect())
      apply()
    }
    """


def bottom_sheet(title: str, css_class: str, height: str) -> tuple[pmui.Drawer, pn.Column]:
    """Return a modal bottom sheet with *title* and a close button, and the column it holds."""
    close = pmui.IconButton(
        icon="close", description="Close", margin=0, align="center", sx=TOUCH_TARGET_SX
    )
    header = pmui.Row(
        pmui.Typography(title, variant="subtitle1", margin=0, sx={"fontWeight": 500}),
        close,
        SheetLabel(label=title, width=0, height=0, margin=0),
        sizing_mode="stretch_width",
        margin=0,
        sx={"alignItems": "center", "justifyContent": "space-between", "pl": 2, "pr": 1},
    )
    body = pn.Column(sizing_mode="stretch_width", margin=0)
    sheet = pmui.Drawer(
        header,
        body,
        anchor="bottom",
        variant="temporary",
        sx={**SHEET_SX, "height": height},
        css_classes=[css_class],
    )
    close.on_click(lambda _event: setattr(sheet, "open", False))
    return sheet, body


def inspector_sheet() -> pmui.Drawer:
    """Return the inspector's sheet.

    Persistent, without a backdrop, so the canvas above it still takes a tap on
    another node, which the sheet then shows.
    """
    return pmui.Drawer(
        anchor="bottom",
        variant="persistent",
        width=0,
        height=0,
        sizing_mode="fixed",
        sx={**SHEET_SX, "height": INSPECTOR_SHEET_HEIGHT, "boxShadow": 8},
        css_classes=["fd-inspector-sheet"],
    )


def sheet_button(icon: str, label: str, css_class: str) -> pmui.Fab:
    """Return a floating button opening a bottom sheet, shown on phones only."""
    return pmui.Fab(
        icon=icon,
        description=label,
        color="primary",
        visible=False,
        margin=0,
        stylesheets=[_BUTTON_CSS, SHOW_ON_PHONE],
        css_classes=[css_class],
    )
