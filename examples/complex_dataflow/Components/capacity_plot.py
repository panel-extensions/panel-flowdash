"""Plots turbine capacity by year for filtered data using Vega-Lite."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Capacity by Year")
class app(pn.viewable.Viewer):
    """Bar chart showing total installed capacity per year using Vega-Lite."""

    filtered = param.DataFrame()

    def _transform(self, df):
        if df is None or df.empty or "p_year" not in df.columns:
            return pn.pane.Markdown("*No data to plot.*")

        by_year = (
            df.groupby("p_year")["t_cap"]
            .sum()
            .reset_index()
            .rename(columns={"p_year": "Year", "t_cap": "Total Capacity (kW)"})
        )

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": "Installed Capacity by Year",
            "data": {"values": by_year.to_dict(orient="records")},
            "mark": {"type": "bar", "cornerRadiusTopLeft": 3, "cornerRadiusTopRight": 3},
            "encoding": {
                "x": {
                    "field": "Year",
                    "type": "ordinal",
                    "axis": {"labelAngle": -45},
                },
                "y": {
                    "field": "Total Capacity (kW)",
                    "type": "quantitative",
                },
                "color": {
                    "field": "Total Capacity (kW)",
                    "type": "quantitative",
                    "scale": {"scheme": "viridis"},
                    "legend": None,
                },
            },
            "width": "container",
            "height": "container",
        }

        return pn.pane.Vega(spec, sizing_mode="stretch_both", min_width=300, min_height=300)

    def __panel__(self):
        return self.param.filtered.rx.pipe(self._transform)
