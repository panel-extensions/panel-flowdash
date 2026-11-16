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
