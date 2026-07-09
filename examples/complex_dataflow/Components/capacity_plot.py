"""Plots turbine capacity by year for filtered data using Vega-Lite."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Capacity by Year", config=["title", "color_scheme"])
class app(pn.viewable.Viewer):
    """Bar chart showing total installed capacity per year using Vega-Lite.

    ``title`` and ``color_scheme`` are set in the node editor rather than wired.
    """

    filtered = param.DataFrame()

    title = param.String(default="Installed Capacity by Year", doc="Chart title.")

    color_scheme = param.Selector(
        default="viridis",
        objects=["viridis", "magma", "plasma", "blues", "greens", "oranges"],
        doc="Vega-Lite color scheme for the bars.",
    )

    @param.depends("filtered", "title", "color_scheme")
    def _transform(self):
        df = self.filtered
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
            "title": self.title,
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
                    "scale": {"scheme": self.color_scheme},
                    "legend": None,
                },
            },
            "width": "container",
            "height": "container",
        }

        return pn.pane.Vega(spec, sizing_mode="stretch_both", min_width=300, min_height=300)

    def __panel__(self):
        return pn.panel(self._transform)
