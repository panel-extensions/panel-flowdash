"""Loads the wind turbines dataset and outputs it as a DataFrame."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


@register(component=True, sidebar=True, title="Wind Turbines Source")
class app(pn.viewable.Viewer):
    """Loads wind turbines data from the HoloViz datasets."""

    n_rows = param.Integer(default=1000, bounds=(100, 500000))
    filters = param.List(default=[])

    @param.output(param.DataFrame)
    @param.depends("n_rows", "filters")
    def dataset(self):
        df = pn.state.as_cached(
            "data",
            pd.read_parquet,
            path="http://datasets.holoviz.org/windturbines/v1/windturbines.parquet",
        )
        df = df.head(self.n_rows)
        for expr in self.filters:
            if isinstance(expr, str) and expr:
                try:
                    df = df.query(expr)
                except Exception:
                    pass
        return df

    def __panel__(self):
        return pn.Column(
            pmui.IntInput.from_param(self.param.n_rows, label="Max rows"),
            pn.pane.Markdown(
                pn.rx("Loading up to **{n_rows}** rows. Active filters: **{n_filters}**").format(
                    n_rows=self.param.n_rows,
                    n_filters=pn.rx(len)(self.param.filters),
                )
            ),
        )
