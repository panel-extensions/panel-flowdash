"""A tile overlay that replaces stale content with the failure behind it."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

    from panel_flowdash.dataflow_engine import InputFailure, InputFailures

# The scrim hides stale content without taking part in layout, so a tile is
# sized by its content as it is without failure handling. It is derived from the
# theme to read in both light and dark mode.
_SCRIM_STYLES = {
    "position": "absolute",
    "inset": "0",
    "z-index": "10",
    "background": "color-mix(in srgb, var(--panel-background-color, #fff) 90%, transparent)",
    "overflow": "auto",
}

_SIZING_PARAMS = (
    "sizing_mode",
    "width",
    "height",
    "min_width",
    "min_height",
    "max_width",
    "max_height",
)


def with_failure_overlay(
    view: Viewable,
    failures: InputFailures,
    describe: Callable[[dict[str, InputFailure]], str],
) -> Viewable:
    """Wrap *view* so that it is covered by a message while any of its inputs is failed.

    The wrapper takes over the view's sizing so that it lays out the same way in
    a tile or on the canvas, and the overlay is a sibling that is only visible
    while *failures* is non-empty.
    """
    message = pmui.Typography(
        failures.param.failed.rx.pipe(describe),
        variant="caption",
        margin=0,
        sx={"whiteSpace": "pre-line", "overflowWrap": "break-word"},
    )
    alert = pmui.Alert(
        message,
        severity="error",
        variant="outlined",
        margin=4,
        styles={"width": "calc(100% - 8px)"},
        sx={"px": 1, "py": 0.25},
    )
    overlay = pn.Column(
        alert,
        css_classes=["flowdash-failure"],
        margin=0,
        styles=_SCRIM_STYLES,
        visible=failures.param.failed.rx.len() > 0,
    )
    sizing = {name: getattr(view, name) for name in _SIZING_PARAMS}
    return pn.Column(view, overlay, margin=0, styles={"position": "relative"}, **sizing)
