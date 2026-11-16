"""States ranked by capacity, turbine count or capacity per person."""

import panel as pn
import param

from panel_flowdash import register

from ._common import heading, message, vega

METRICS = {
    "capacity_mw": "Capacity (MW)",
    "watts_per_person": "Watts per person",
    "turbines": "Turbines",
}


@register(
    component=True,
    page=False,
    title="State Ranking",
    icon="leaderboard",
    description="Ranks states by capacity, turbine count or watts per person, coloured by Census region.",
    tags=["view", "chart", "census"],
    config=["metric", "top_n"],
    extensions=["vega"],
    default_size={"width": 560, "height": 380},
    min_size={"width": 320, "height": 260},
)
class app(pn.viewable.Viewer):
    by_state = param.DataFrame(default=None, allow_None=True)

    metric = param.Selector(
        default="watts_per_person", objects=list(METRICS), doc="Ranking measure."
    )

    top_n = param.Integer(default=15, bounds=(5, 52), doc="Number of states shown.")

    @param.depends("by_state", "metric", "top_n")
    def _chart(self):
        df = self.by_state
        if df is None or df.empty:
            return message("No states to rank.")
        top = df.sort_values(self.metric, ascending=False).head(self.top_n)
        title = METRICS[self.metric]
        spec = {
            "data": {"values": top.to_dict(orient="records")},
            "mark": {"type": "bar", "cornerRadiusEnd": 2},
            "encoding": {
                "y": {"field": "name", "type": "nominal", "sort": "-x", "title": None},
                "x": {"field": self.metric, "type": "quantitative", "title": title},
                "color": {
                    "field": "region",
                    "type": "nominal",
                    "scale": {"scheme": "tableau10"},
                    "legend": {"orient": "bottom", "title": None},
                },
                "tooltip": [
                    {"field": "name", "title": "State"},
                    {"field": "region", "title": "Region"},
                    {"field": "capacity_mw", "title": "MW", "format": ",.0f"},
                    {"field": "turbines", "title": "Turbines", "format": ","},
                    {"field": "population_2023", "title": "Population", "format": ","},
                    {"field": "watts_per_person", "title": "W per person", "format": ",.0f"},
                ],
            },
        }
        leader = top.iloc[0]
        return pn.Column(
            heading("State ranking", f"{leader['name']} leads on {title.lower()}"),
            vega(spec),
            sizing_mode="stretch_both",
        )

    def __panel__(self):
        return pn.panel(self._chart, sizing_mode="stretch_both")
