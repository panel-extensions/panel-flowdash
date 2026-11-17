"""A single headline number computed from the turbine table."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._common import format_capacity, setting

METRICS = {
    "turbines": "Turbines",
    "capacity": "Installed capacity",
    "projects": "Projects",
    "median_year": "Median commissioning year",
    "median_size": "Median turbine size",
    "states": "States with wind",
}


def compute(df, metric, unit):
    """Return the headline value and a caption for *metric*."""
    if metric == "turbines":
        return f"{len(df):,}", f"in {df['p_name'].nunique():,} projects"
    if metric == "capacity":
        total = float(df["t_cap"].sum())
        return format_capacity(total, unit), f"{total / max(len(df), 1):,.0f} kW per turbine"
    if metric == "projects":
        return f"{df['p_name'].nunique():,}", f"in {df['t_state'].nunique()} states"
    if metric == "median_year":
        years = df["p_year"].dropna()
        if years.empty:
            return "n/a", "no commissioning years"
        return f"{int(years.median())}", f"{years.min()} to {years.max()}"
    if metric == "median_size":
        sizes = df["t_cap"].dropna()
        if sizes.empty:
            return "n/a", "no capacities"
        return f"{sizes.median() / 1000:,.2f} MW", f"largest {sizes.max() / 1000:,.1f} MW"
    states = df["t_state"].nunique()
    return f"{states}", "states and territories"


@register(
    component=True,
    page=False,
    title="KPI",
    icon="speed",
    description="One headline number: turbines, capacity, projects, median year or turbine size.",
    tags=["view", "kpi"],
    config=["metric", "label"],
    default_size={"width": 300, "height": 130},
    min_size={"width": 200, "height": 110},
    max_size={"height": 200},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    metric = param.Selector(default="turbines", objects=list(METRICS), doc="What to count.")

    label = param.String(default="", doc="Overrides the metric name shown above the number.")

    def __init__(self, **params):
        super().__init__(**params)
        self._unit = setting("capacity_unit", "GW")

    def _reading(self, df, metric, unit):
        if df is None:
            return "…", "waiting for data"
        return compute(df, metric, unit)

    def __panel__(self):
        metric = self.param.metric
        reading = (self.param.turbines, metric, self._unit)
        return pn.Column(
            pmui.Typography(
                pn.bind(lambda label, metric: label or METRICS[metric], self.param.label, metric),
                variant="overline",
                sx={"color": "text.secondary", "lineHeight": 1.6},
                margin=(6, 12, 0, 12),
            ),
            pmui.Typography(
                pn.bind(lambda *args: self._reading(*args)[0], *reading),
                variant="h4",
                sx={"fontWeight": 500},
                margin=(0, 12),
            ),
            pmui.Typography(
                pn.bind(lambda *args: self._reading(*args)[1], *reading),
                variant="caption",
                sx={"color": "text.secondary"},
                margin=(0, 12),
            ),
            margin=0,
        )
