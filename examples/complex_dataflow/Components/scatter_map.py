"""Scatter plot of turbine locations colored by capacity."""

import pandas as pd
import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Location Map")
class app(pn.viewable.Viewer):
    """Scatter plot of turbine lat/lon colored by capacity."""

    filtered = param.DataFrame()

    def __panel__(self):
        import hvplot.pandas  # noqa: F401

        df = self.filtered if self.filtered is not None else pd.DataFrame()
        if df.empty or "xlong" not in df.columns:
            return pn.pane.Markdown("*No location data available.*")

        plot = df.hvplot.points(
            x="xlong",
            y="ylat",
            c="t_cap",
            cmap="viridis",
            title="Turbine Locations",
            hover_cols=["p_name", "t_cap", "p_year"],
            responsive=True,
            height=400,
        )
        return pn.pane.HoloViews(plot, sizing_mode="stretch_both")
