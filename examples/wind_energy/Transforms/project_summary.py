"""Collapse turbines into one row per wind project."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


def summarize_projects(df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per project, largest first."""
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "project",
                "state",
                "county",
                "year",
                "turbines",
                "capacity_mw",
                "manufacturer",
                "model",
            ]
        )
    grouped = df.groupby(["p_name", "t_state"], sort=False, observed=True)
    projects = grouped.agg(
        county=("t_county", "first"),
        year=("p_year", "min"),
        turbines=("case_id", "size"),
        capacity_kw=("t_cap", "sum"),
        manufacturer=("t_manu", lambda s: s.mode().iat[0] if s.notna().any() else pd.NA),
        model=("t_model", lambda s: s.mode().iat[0] if s.notna().any() else pd.NA),
    ).reset_index()
    projects = projects.rename(columns={"p_name": "project", "t_state": "state"})
    projects["capacity_mw"] = (projects.pop("capacity_kw").astype(float) / 1000).round(1)
    columns = [
        "project",
        "state",
        "county",
        "year",
        "turbines",
        "capacity_mw",
        "manufacturer",
        "model",
    ]
    return projects[columns].sort_values("capacity_mw", ascending=False, ignore_index=True)


@register(
    component=True,
    page=False,
    title="Project Summary",
    icon="table_rows",
    description="One row per wind project: location, first year, turbine count, capacity and main model.",
    tags=["transform", "aggregate"],
    default_size={"width": 360, "height": 120},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("turbines")
    def projects(self):
        return None if self.turbines is None else summarize_projects(self.turbines)

    def _summary(self, turbines):
        projects = self.projects()
        if projects is None:
            return "Waiting for turbines"
        return f"**{len(projects):,}** projects, **{projects['capacity_mw'].sum():,.0f} MW**"

    def __panel__(self):
        return pn.Column(
            pmui.Typography("Projects", variant="subtitle2", margin=(4, 10, 0, 10)),
            pn.pane.Markdown(pn.bind(self._summary, self.param.turbines), margin=(0, 10)),
        )
