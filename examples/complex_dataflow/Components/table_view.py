"""Displays a DataFrame in a Tabulator table."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Data Table", config=["title", "page_size"])
class app(pn.viewable.Viewer):
    """Renders a DataFrame as an interactive Tabulator table.

    ``title`` and ``page_size`` are design-time configuration options set in the
    node editor rather than wired input ports.
    """

    filtered = param.DataFrame()

    title = param.String(default="", doc="Optional heading shown above the table.")

    page_size = param.Integer(default=10, bounds=(5, 100), doc="Rows per page.")

    def _transform(self, df):
        columns = ["t_state", "t_county", "p_name", "p_year", "t_cap", "t_hh", "t_rd"]
        available = [c for c in columns if df is not None and c in df.columns]
        return pn.widgets.Tabulator(
            df[available] if available else df,
            initial_page_size=self.page_size,
            pagination="remote",
        )

    def __panel__(self):
        title = self.param.title.rx()
        table = self.param.filtered.rx.pipe(self._transform)
        heading = pn.pane.Markdown(
            "### " + title,
            visible=title.rx.bool(),
        )
        return pn.Column(heading, table)
