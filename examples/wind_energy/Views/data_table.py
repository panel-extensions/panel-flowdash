"""Any table, paginated."""

import panel as pn
import param

from panel_flowdash import register

from ._common import heading, message

LABELS = {
    "project": "Project",
    "state": "State",
    "county": "County",
    "year": "Year",
    "turbines": "Turbines",
    "capacity_mw": "MW",
    "manufacturer": "Manufacturer",
    "model": "Model",
    "name": "Name",
    "region": "Region",
    "population_2023": "Population",
    "watts_per_person": "W per person",
}


@register(
    component=True,
    page=False,
    title="Data Table",
    icon="table_chart",
    description="Shows any table it is given, paginated; pair it with sort and head steps on the edge.",
    tags=["view", "table"],
    config=["label", "columns", "page_size"],
    extensions=["tabulator"],
    default_size={"width": 560, "height": 360},
    min_size={"width": 320, "height": 200},
)
class app(pn.viewable.Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    label = param.String(default="Table", doc="Heading shown above the table.")

    columns = param.List(default=[], item_type=str, doc="Columns to show; empty shows all.")

    page_size = param.Integer(default=10, bounds=(5, 100), doc="Rows per page.")

    @param.depends("table", "label", "columns", "page_size")
    def _view(self):
        df = self.table
        if df is not None and self.columns:
            df = df[[c for c in self.columns if c in df.columns]]
        if df is None or df.empty:
            return pn.Column(heading(self.label), message("No rows."))
        return pn.Column(
            heading(self.label, f"{len(df):,} rows"),
            pn.widgets.Tabulator(
                df.rename(columns=LABELS),
                formatters={"Year": {"type": "plaintext"}},
                disabled=True,
                show_index=False,
                pagination="local",
                page_size=self.page_size,
                layout="fit_columns",
                theme="simple",
                sizing_mode="stretch_both",
                margin=(0, 10, 10, 10),
            ),
            sizing_mode="stretch_both",
        )

    def __panel__(self):
        return pn.panel(self._view, sizing_mode="stretch_both")
