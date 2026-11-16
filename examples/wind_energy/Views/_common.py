"""Helpers shared by the view components.

The views build their panes once and bind what changes to them, so a new
selection updates the text and data in place. Returning freshly built panes
from a ``pn.bind`` or ``param.depends`` function would instead replace the
whole view on every change: the tile empties for a frame and its charts and
maps are drawn again from scratch.
"""

import panel as pn
import panel_material_ui as pmui

from panel_flowdash import current_dashboard

# Axis and legend colours readable on light and dark backgrounds alike.
_MUTED = "#8a8f98"
_GRID = "rgba(138, 143, 152, 0.25)"

VEGA_CONFIG = {
    "background": None,
    "view": {"stroke": None},
    "font": "Roboto, Helvetica, Arial, sans-serif",
    "axis": {
        "labelColor": _MUTED,
        "titleColor": _MUTED,
        "gridColor": _GRID,
        "domainColor": _GRID,
        "tickColor": _GRID,
        "titleFontWeight": "normal",
    },
    "legend": {"labelColor": _MUTED, "titleColor": _MUTED},
}


def setting(name, default):
    """Return a reference to a dashboard setting, or *default* outside a dashboard.

    Call it while the component is created, so the subscription is released
    with the tile.
    """
    settings = getattr(current_dashboard(), "settings", None)
    if settings is not None and name in settings.param:
        return settings.param[name]
    return default


def heading(text, caption=None):
    """A compact heading drawn by the component itself, in place of a tile title.

    *text* and *caption* may be references, e.g. ``pn.bind(...)``, to update in place.
    """
    parts = [pmui.Typography(text, variant="subtitle2", margin=(4, 10, 0, 10))]
    if caption is not None:
        parts.append(
            pmui.Typography(
                caption, variant="caption", sx={"color": "text.secondary"}, margin=(0, 10)
            )
        )
    return pn.Column(*parts, margin=0, sizing_mode="stretch_width")


def message(text, **params):
    """A muted line shown when there is nothing to draw."""
    return pmui.Typography(
        text, variant="body2", sx={"color": "text.secondary"}, margin=10, **params
    )


def format_capacity(kw, unit="GW"):
    """Format a capacity in kW as MW or GW."""
    if unit == "MW":
        return f"{kw / 1e3:,.0f} MW"
    return f"{kw / 1e6:,.1f} GW"


def _full_spec(spec):
    if spec is None:
        return None
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": "container",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": VEGA_CONFIG,
        **spec,
    }


def vega(spec, **params):
    """A Vega-Lite pane that fills its tile.

    *spec* may be a reference returning a spec, or None while there is nothing
    to draw. When only the ``data`` values of the spec change, the pane passes
    the new values to the chart it already draws instead of drawing it again.
    """
    spec = _full_spec(spec) if isinstance(spec, dict) else pn.bind(_full_spec, spec)
    return pn.pane.Vega(
        spec, sizing_mode="stretch_both", min_height=160, margin=(0, 10, 6, 10), **params
    )


def is_empty(df):
    """Whether *df* has no rows to show, including before it arrives."""
    return df is None or df.empty


def has_rows(df):
    return not is_empty(df)
