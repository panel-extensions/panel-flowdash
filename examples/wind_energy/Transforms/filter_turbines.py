"""Narrow the turbine table to a state and a range of commissioning years."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


@register(
    component=True,
    page=False,
    title="Filter Turbines",
    icon="filter_alt",
    description="Keeps the turbines in one state and commissioning-year range; unset inputs keep everything.",
    tags=["transform", "filter"],
    requires=[
        {"key": "turbines", "type": "Turbines", "label": "Turbines"},
        {"key": "state", "type": "str", "label": "State", "required": False},
        {"key": "years", "type": "tuple", "label": "Years", "required": False},
    ],
    provides=[{"key": "filtered", "type": "Turbines", "label": "Filtered"}],
    default_size={"width": 360, "height": 120},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)
    state = param.String(default="All", allow_None=True)
    years = param.Range(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("turbines", "state", "years")
    def filtered(self):
        df = self.turbines
        if df is None:
            return None
        if self.state and self.state != "All":
            df = df[df["t_state"] == self.state]
        if self.years is not None:
            start, end = self.years
            df = df[df["p_year"].between(start, end).fillna(False)]
        return df.copy()

    def _summary(self, turbines, state, years):
        df = self.filtered()
        if df is None:
            return "Waiting for turbines"
        where = "all states" if not state or state == "All" else state
        span = f", {years[0]:.0f} to {years[1]:.0f}" if years else ""
        return f"**{len(df):,}** of {len(turbines):,} turbines ({where}{span})"

    def __panel__(self):
        return pn.Column(
            pmui.Typography("Filter", variant="subtitle2", margin=(4, 10, 0, 10)),
            pn.pane.Markdown(
                pn.bind(self._summary, self.param.turbines, self.param.state, self.param.years),
                margin=(0, 10),
            ),
        )
