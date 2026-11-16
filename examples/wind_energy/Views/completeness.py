"""How complete the turbine records are, field by field."""

import panel as pn
import param

from panel_flowdash import register

from ._common import heading, message, vega

FIELDS = {
    "p_year": "Commissioning year",
    "t_cap": "Capacity",
    "t_manu": "Manufacturer",
    "t_model": "Model",
    "t_hh": "Hub height",
    "t_rd": "Rotor diameter",
}


@register(
    component=True,
    page=False,
    title="Data Completeness",
    icon="fact_check",
    description="Share of turbine records that have each field filled in.",
    tags=["view", "quality"],
    extensions=["vega"],
    default_size={"width": 460, "height": 300},
    min_size={"width": 300, "height": 220},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    @param.depends("turbines")
    def _chart(self):
        df = self.turbines
        if df is None or df.empty:
            return message("No turbines to check.")
        values = [
            {"field": label, "complete": round(float(df[column].notna().mean()), 4)}
            for column, label in FIELDS.items()
        ]
        spec = {
            "data": {"values": values},
            "mark": {"type": "bar", "cornerRadiusEnd": 2},
            "encoding": {
                "y": {"field": "field", "type": "nominal", "sort": None, "title": None},
                "x": {
                    "field": "complete",
                    "type": "quantitative",
                    "title": None,
                    "axis": {"format": ".0%"},
                    "scale": {"domain": [0, 1]},
                },
                "color": {
                    "condition": {"test": "datum.complete < 0.9", "value": "#e76f51"},
                    "value": "#2a9d8f",
                },
                "tooltip": [
                    {"field": "field", "title": "Field"},
                    {"field": "complete", "title": "Complete", "format": ".1%"},
                ],
            },
        }
        worst = min(values, key=lambda v: v["complete"])
        return pn.Column(
            heading(
                "Data completeness",
                f"{len(df):,} records; {worst['field'].lower()} is the least complete",
            ),
            vega(spec),
            sizing_mode="stretch_both",
        )

    def __panel__(self):
        return pn.panel(self._chart, sizing_mode="stretch_both")
