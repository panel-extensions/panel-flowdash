"""Displays a DataFrame in a Tabulator table."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Data Table")
class app(pn.viewable.Viewer):
    """Renders a DataFrame as an interactive Tabulator table."""

    filtered = param.DataFrame()

    def _transform(self, df):
        columns = ["t_state", "t_county", "p_name", "p_year", "t_cap", "t_hh", "t_rd"]
        available = [c for c in columns if df is not None and c in df.columns]
        return pn.widgets.Tabulator(
            df[available] if available else df,
            initial_page_size=10,
            pagination="remote",
        )

    def __panel__(self):
        return self.param.filtered.rx.pipe(self._transform)
