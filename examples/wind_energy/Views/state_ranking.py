"""States ranked by capacity, turbine count or capacity per person."""

import panel as pn
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message, vega

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

    def _top(self, df, metric, top_n):
        return df.sort_values(metric, ascending=False).head(top_n)

    def _spec(self, df, metric, top_n):
        if is_empty(df):
            return None
        title = METRICS[metric]
        return {
            "data": {"values": self._top(df, metric, top_n).to_dict(orient="records")},
            "mark": {"type": "bar", "cornerRadiusEnd": 2},
            "encoding": {
                "y": {"field": "name", "type": "nominal", "sort": "-x", "title": None},
                "x": {"field": metric, "type": "quantitative", "title": title},
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

    def _caption(self, df, metric, top_n):
        if is_empty(df):
            return ""
        leader = self._top(df, metric, top_n).iloc[0]
        return f"{leader['name']} leads on {METRICS[metric].lower()}"

    def __panel__(self):
        ranking = (self.param.by_state, self.param.metric, self.param.top_n)
        return pn.Column(
            heading("State ranking", pn.bind(self._caption, *ranking)),
            vega(pn.bind(self._spec, *ranking), visible=pn.bind(has_rows, self.param.by_state)),
            message("No states to rank.", visible=pn.bind(is_empty, self.param.by_state)),
            sizing_mode="stretch_both",
        )
