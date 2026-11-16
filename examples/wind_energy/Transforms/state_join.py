"""Join per-state turbine totals with state population."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


@register(
    component=True,
    page=False,
    title="Join State Population",
    icon="merge",
    description="Totals turbines and capacity per state and joins them with population to get watts per person.",
    tags=["transform", "join", "census"],
    requires=[
        {"key": "turbines", "type": "DataFrame", "label": "Turbines"},
        {"key": "population", "type": "StatePopulation", "label": "Population"},
    ],
    provides=[{"key": "by_state", "type": "DataFrame", "label": "By state"}],
    default_size={"width": 360, "height": 120},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)
    population = param.DataFrame(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("turbines", "population")
    def by_state(self):
        if self.turbines is None or self.population is None:
            return None
        totals = (
            self.turbines.groupby("t_state", observed=True)
            .agg(turbines=("case_id", "size"), capacity_kw=("t_cap", "sum"))
            .reset_index()
            .rename(columns={"t_state": "state"})
        )
        totals["capacity_mw"] = (totals.pop("capacity_kw").astype(float) / 1000).round(1)
        joined = totals.merge(self.population, on="state", how="inner")
        joined["watts_per_person"] = (
            joined["capacity_mw"] * 1e6 / joined["population_2023"]
        ).round(1)
        columns = [
            "state",
            "name",
            "region",
            "turbines",
            "capacity_mw",
            "population_2023",
            "watts_per_person",
        ]
        return joined[columns].sort_values("capacity_mw", ascending=False, ignore_index=True)

    def _summary(self, turbines, population):
        joined = self.by_state()
        if joined is None:
            return "Waiting for turbines and population"
        unmatched = sorted(set(turbines["t_state"].dropna()) - set(population["state"]))
        note = f"; no population for {', '.join(unmatched)}" if unmatched else ""
        return f"Joined **{len(joined)}** states{note}"

    def __panel__(self):
        return pn.Column(
            pmui.Typography("State join", variant="subtitle2", margin=(4, 10, 0, 10)),
            pn.pane.Markdown(
                pn.bind(self._summary, self.param.turbines, self.param.population),
                margin=(0, 10),
            ),
        )
