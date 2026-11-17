"""Installed capacity by turbine manufacturer."""

import pandas as pd
import panel as pn
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message, setting, vega


def manufacturer_share(df, top_n, unit):
    """Capacity per manufacturer in *unit*; the rest are folded into "Other"."""
    scale = 1e6 if unit == "GW" else 1e3
    by_manu = df.groupby(df["t_manu"].fillna("Unknown"))["t_cap"].sum().astype(float) / scale
    by_manu = by_manu.sort_values(ascending=False)
    top = by_manu.head(top_n)
    rest = by_manu.iloc[top_n:].sum()
    if rest > 0:
        top = pd.concat([top, pd.Series({"Other": rest})])
    frame = top.rename("capacity").rename_axis("manufacturer").reset_index()
    frame["share"] = frame["capacity"] / frame["capacity"].sum()
    return frame.round(4)


@register(
    component=True,
    page=False,
    title="Manufacturers",
    icon="factory",
    description="Installed capacity of the largest turbine manufacturers, the rest grouped as Other.",
    tags=["view", "chart"],
    config=["top_n"],
    extensions=["vega"],
    default_size={"width": 420, "height": 320},
    min_size={"width": 300, "height": 240},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    top_n = param.Integer(default=8, bounds=(3, 20), doc="Manufacturers shown before Other.")

    def __init__(self, **params):
        super().__init__(**params)
        self._unit = setting("capacity_unit", "GW")
        self._scheme = setting("color_scheme", "teals")

    def _spec(self, df, top_n, unit, scheme):
        if is_empty(df):
            return None
        frame = manufacturer_share(df, top_n, unit)
        return {
            "data": {"values": frame.to_dict(orient="records")},
            "mark": {"type": "bar", "cornerRadiusEnd": 2},
            "encoding": {
                "y": {"field": "manufacturer", "type": "nominal", "sort": None, "title": None},
                "x": {"field": "capacity", "type": "quantitative", "title": unit},
                "color": {
                    "field": "capacity",
                    "type": "quantitative",
                    "scale": {"scheme": scheme},
                    "legend": None,
                },
                "tooltip": [
                    {"field": "manufacturer", "title": "Manufacturer"},
                    {"field": "capacity", "title": unit, "format": ",.2f"},
                    {"field": "share", "title": "Share", "format": ".0%"},
                ],
            },
        }

    def _caption(self, df, unit):
        if is_empty(df):
            return ""
        leader = manufacturer_share(df, 1, unit).iloc[0]
        return f"{leader['manufacturer']} built {leader['share']:.0%} of capacity"

    def __panel__(self):
        turbines = self.param.turbines
        return pn.Column(
            heading("Manufacturers", pn.bind(self._caption, turbines, self._unit)),
            vega(
                pn.bind(self._spec, turbines, self.param.top_n, self._unit, self._scheme),
                visible=pn.bind(has_rows, turbines),
            ),
            message("No turbines to chart.", visible=pn.bind(is_empty, turbines)),
            sizing_mode="stretch_both",
        )
