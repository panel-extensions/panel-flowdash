"""Displays a DataFrame in a Tabulator table."""

import param
import panel as pn
import pandas as pd

from panel_flowdash import register


@register(component=True, title="Data Table")
class app(pn.viewable.Viewer):
    """Renders a DataFrame as an interactive Tabulator table."""

    filtered = param.DataFrame()

    def __panel__(self):
        df = self.param.filtered.rx()
        columns = ["t_state", "t_county", "p_name", "p_year", "t_cap", "t_hh", "t_rd"]
        available = [c for c in columns if c in df.columns]
        return pn.widgets.Tabulator(
            df[available] if available else df,
            sizing_mode="stretch_both",
            page_size=20,
            pagination="remote",
        )
