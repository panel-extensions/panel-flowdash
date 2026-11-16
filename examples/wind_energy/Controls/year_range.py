"""A range slider over commissioning years, published from a Parameter."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

FIRST_YEAR, LAST_YEAR = 1981, 2022


@register(
    component=True,
    page=False,
    sidebar=True,
    title="Year Range",
    icon="date_range",
    description="Limit the turbines to those commissioned between two years.",
    tags=["widget", "filter"],
)
class app(pn.viewable.Viewer):
    years = param.Range(
        default=(FIRST_YEAR, LAST_YEAR),
        bounds=(FIRST_YEAR, LAST_YEAR),
        metadata={"flowdash": {"output": True, "label": "Years"}},
    )

    def __panel__(self):
        return pmui.IntRangeSlider.from_param(
            self.param.years,
            label="Commissioned",
            step=1,
            format="0",
            sizing_mode="stretch_width",
        )
