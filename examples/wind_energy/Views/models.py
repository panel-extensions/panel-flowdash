"""The most common turbine models and their typical dimensions."""

import panel as pn
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message


def model_table(df, top_n):
    """Turbine count and median dimensions per model, most common first."""
    known = df.dropna(subset=["t_model"])
    grouped = known.groupby(["t_manu", "t_model"], observed=True)
    table = grouped.agg(
        turbines=("case_id", "size"),
        capacity_kw=("t_cap", "median"),
        rotor_m=("t_rd", "median"),
        hub_m=("t_hh", "median"),
        first_year=("p_year", "min"),
    ).reset_index()
    table = table.sort_values("turbines", ascending=False).head(top_n)
    for column in ("capacity_kw", "rotor_m", "hub_m"):
        table[column] = table[column].astype(float).round().astype("Int64")
    return table.rename(
        columns={
            "t_manu": "Maker",
            "t_model": "Model",
            "turbines": "Count",
            "capacity_kw": "kW",
            "rotor_m": "Rotor",
            "hub_m": "Hub",
            "first_year": "Since",
        }
    ).reset_index(drop=True)


@register(
    component=True,
    page=False,
    title="Turbine Models",
    icon="settings_input_component",
    description="The most installed turbine models with median capacity, rotor diameter and hub height.",
    tags=["view", "table"],
    config=["top_n"],
    extensions=["tabulator"],
    default_size={"width": 560, "height": 340},
    min_size={"width": 380, "height": 220},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    top_n = param.Integer(default=15, bounds=(5, 50), doc="Number of models listed.")

    @param.depends("turbines", "top_n")
    def _models(self):
        if is_empty(self.turbines):
            return None
        return model_table(self.turbines, self.top_n)

    def _caption(self, table):
        if table is None:
            return ""
        models = self.turbines["t_model"].nunique()
        return f"Top {len(table)} of {models:,} models; median kW, rotor and hub in metres"

    def __panel__(self):
        table = pn.widgets.Tabulator(
            self._models,
            formatters={"Since": {"type": "plaintext"}},
            disabled=True,
            show_index=False,
            layout="fit_columns",
            theme="simple",
            sizing_mode="stretch_both",
            margin=(0, 10, 10, 10),
            visible=pn.bind(has_rows, self.param.turbines),
        )
        return pn.Column(
            heading("Turbine models", pn.bind(self._caption, table.param.value)),
            table,
            message("No turbines to list.", visible=pn.bind(is_empty, self.param.turbines)),
            sizing_mode="stretch_both",
        )
