"""The US Wind Turbine Database, bundled as a compressed CSV."""

import datetime as dt

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import current_dashboard, register

from ._data import load_turbines


@register(
    component=True,
    page=False,
    title="US Wind Turbines",
    icon="wind_power",
    description="Every utility-scale wind turbine in the US with its project, model and location (USWTDB).",
    tags=["source", "wind", "USWTDB"],
    config=["complete_only"],
    provides=[{"key": "turbines", "type": "Turbines", "label": "Turbines"}],
    default_size={"width": 360, "height": 150},
    min_size={"width": 260, "height": 120},
)
class app(pn.viewable.Viewer):
    complete_only = param.Boolean(
        default=False,
        label="Complete records only",
        doc="Drop turbines without a commissioning year, capacity or manufacturer.",
    )

    _loaded_at = param.String(default="")

    def __init__(self, **params):
        super().__init__(**params)
        self._reload()
        dashboard = current_dashboard()
        if dashboard is not None:
            dashboard.param.watch(lambda _: self._reload(), "refresh")

    def _reload(self):
        self._loaded_at = dt.datetime.now().strftime("%H:%M:%S")

    @param.output(param.DataFrame)
    @param.depends("complete_only", "_loaded_at")
    def turbines(self):
        df = load_turbines()
        if self.complete_only:
            df = df.dropna(subset=["p_year", "t_cap", "t_manu"])
        return df

    def _summary(self, complete_only, loaded_at):
        df = self.turbines()
        return (
            f"**{len(df):,}** turbines in **{df['p_name'].nunique():,}** projects across "
            f"**{df['t_state'].nunique()}** states and territories. Loaded {loaded_at}."
        )

    def __panel__(self):
        return pn.Column(
            pmui.Typography(
                "US Wind Turbine Database", variant="subtitle2", margin=(4, 10, 0, 10)
            ),
            pn.pane.Markdown(
                pn.bind(self._summary, self.param.complete_only, self.param._loaded_at),
                margin=(0, 10),
            ),
            sizing_mode="stretch_width",
        )
