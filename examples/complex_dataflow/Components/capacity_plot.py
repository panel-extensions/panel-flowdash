"""Plots turbine capacity by year for filtered data."""

import param
import panel as pn
import pandas as pd

from panel_flowdash import register


@register(component=True, title="Capacity by Year")
class app(pn.viewable.Viewer):
    """Bar chart showing total installed capacity per year."""

    filtered = param.DataFrame()

    def __panel__(self):
        import hvplot.pandas  # noqa: F401

        df = self.filtered if self.filtered is not None else pd.DataFrame()
        if df.empty or "p_year" not in df.columns:
            return pn.pane.Markdown("*No data to plot.*")

        by_year = (
            df.groupby("p_year")["t_cap"]
            .sum()
            .reset_index()
            .rename(columns={"p_year": "Year", "t_cap": "Total Capacity (kW)"})
        )
        plot = by_year.hvplot.bar(
            x="Year",
            y="Total Capacity (kW)",
            title="Installed Capacity by Year",
            rot=45,
            responsive=True,
            height=350,
        )
        return pn.pane.HoloViews(plot, sizing_mode="stretch_both")
