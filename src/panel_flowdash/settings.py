"""Dashboard-wide settings that are persisted with each dashboard."""

from __future__ import annotations

import param


class DashboardSettings(param.Parameterized):
    """Values that apply to a whole dashboard rather than to one component.

    The editor keeps one instance in ``FlowDash.settings``, fills it from the
    loaded dashboard and writes its current values back on save. Subclass it to
    add application-specific parameters and pass the subclass as
    ``FlowDash(settings_class=...)``. Every parameter must hold a JSON
    serializable value, since the stores persist settings as JSON.
    """

    description = param.String(
        default="",
        label="Description",
        doc="""
        A sentence or two on what the dashboard shows. Empty by default.""",
    )

    refresh_period = param.Number(
        default=0.0,
        bounds=(0, 86400),
        step=1,
        label="Refresh period (s)",
        doc="""
        Seconds between automatic refreshes of the dashboard, which the editor
        announces by triggering its ``refresh`` event. 0 turns automatic
        refreshing off.""",
    )

    controls_position = param.Selector(
        default="top",
        objects=["top", "left"],
        label="Controls position",
        doc="""
        Where the dashboard's controls (components registered with
        ``sidebar=True``) go: "top" in a slim row above the tiles, "left" in
        a collapsible panel beside them. Defaults to the editor's
        ``controls_position``.""",
    )

    tile_cards = param.Boolean(
        default=True,
        label="Show tiles as cards",
        doc="""
        Whether each tile is drawn as a framed card with a bar for its status
        and actions. Without the card the tile renders as the component's own
        view, and its actions appear over its top-right corner on hover or
        keyboard focus.""",
    )

    tile_titles = param.Boolean(
        default=False,
        label="Show tile titles",
        doc="""
        Whether a card's bar also shows the tile's title and the component's
        description. Off by default, since most components show their own
        heading. Only applies while ``tile_cards`` is on.""",
    )

    viewers_can_inspect = param.Boolean(
        default=False,
        label="Viewers can inspect outputs",
        doc="""
        Whether people who may only view the dashboard get the inspect button
        on its tiles, when the editor has ``inspect_outputs`` on. Editors and
        administrators always get it. Leave it off when an output carries
        data a tile deliberately hides.""",
    )
