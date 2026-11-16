"""State population estimates from the US Census Bureau, bundled as CSV."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._data import load_population


@register(
    component=True,
    page=False,
    title="State Population",
    icon="groups",
    description="Census Bureau population estimates and region for each state, 2020 and 2023.",
    tags=["source", "census"],
    provides=[{"key": "population", "type": "StatePopulation", "label": "Population"}],
    default_size={"width": 360, "height": 150},
    min_size={"width": 260, "height": 120},
)
class app(pn.viewable.Viewer):
    @param.output(param.DataFrame)
    def population(self):
        return load_population()

    def __panel__(self):
        df = self.population()
        return pn.Column(
            pmui.Typography("State population", variant="subtitle2", margin=(4, 10, 0, 10)),
            pn.pane.Markdown(
                f"**{len(df)}** states and territories, "
                f"**{df['population_2023'].sum() / 1e6:,.1f} M** people in 2023.",
                margin=(0, 10),
            ),
            sizing_mode="stretch_width",
        )
