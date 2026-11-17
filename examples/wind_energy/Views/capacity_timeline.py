"""Capacity commissioned per year, with the running total."""

import panel as pn
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message, setting, vega


def yearly_capacity(df, unit):
    """Capacity added per year and cumulatively, in *unit*."""
    scale = 1e6 if unit == "GW" else 1e3
    by_year = df.dropna(subset=["p_year"]).groupby("p_year")["t_cap"].sum().astype(float) / scale
    frame = by_year.rename("added").reset_index().rename(columns={"p_year": "year"})
    frame["year"] = frame["year"].astype(int)
    frame["total"] = frame["added"].cumsum()
    return frame.round(3)


@register(
    component=True,
    page=False,
    title="Capacity Over Time",
    icon="stacked_line_chart",
    description="Capacity commissioned each year as bars, with the cumulative fleet as a line.",
    tags=["view", "chart"],
    config=["cumulative"],
    extensions=["vega"],
    default_size={"width": 560, "height": 320},
    min_size={"width": 320, "height": 240},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    cumulative = param.Boolean(default=True, doc="Draw the running total as a line.")

    def __init__(self, **params):
        super().__init__(**params)
        self._unit = setting("capacity_unit", "GW")
        self._scheme = setting("color_scheme", "teals")

    def _spec(self, df, cumulative, unit, scheme):
        if is_empty(df):
            return None
        data = yearly_capacity(df, unit).to_dict(orient="records")
        x = {
            "field": "year",
            "type": "ordinal",
            "title": None,
            "axis": {"labelAngle": -45, "labelOverlap": True},
        }
        layers = [
            {
                "mark": {"type": "bar", "cornerRadiusTopLeft": 2, "cornerRadiusTopRight": 2},
                "encoding": {
                    "x": x,
                    "y": {"field": "added", "type": "quantitative", "title": f"Added ({unit})"},
                    "color": {
                        "field": "added",
                        "type": "quantitative",
                        "scale": {"scheme": scheme},
                        "legend": None,
                    },
                    "tooltip": [
                        {"field": "year", "title": "Year"},
                        {"field": "added", "title": f"Added ({unit})", "format": ",.2f"},
                        {"field": "total", "title": f"Total ({unit})", "format": ",.1f"},
                    ],
                },
            }
        ]
        if cumulative:
            layers.append(
                {
                    "mark": {"type": "line", "point": False, "strokeWidth": 2, "color": "#e76f51"},
                    "encoding": {
                        "x": x,
                        "y": {
                            "field": "total",
                            "type": "quantitative",
                            "title": f"Total ({unit})",
                        },
                    },
                }
            )
        spec = {"data": {"values": data}, "layer": layers}
        if cumulative:
            spec["resolve"] = {"scale": {"y": "independent"}}
        return spec

    def _caption(self, df, unit):
        if is_empty(df):
            return ""
        frame = yearly_capacity(df, unit)
        peak = frame.loc[frame["added"].idxmax()]
        return f"{frame['total'].iat[-1]:,.1f} {unit} in total, peak year {int(peak['year'])}"

    def __panel__(self):
        turbines = self.param.turbines
        return pn.Column(
            heading("Capacity over time", pn.bind(self._caption, turbines, self._unit)),
            vega(
                pn.bind(self._spec, turbines, self.param.cumulative, self._unit, self._scheme),
                visible=pn.bind(has_rows, turbines),
            ),
            message("No turbines to chart.", visible=pn.bind(is_empty, turbines)),
            sizing_mode="stretch_both",
        )
