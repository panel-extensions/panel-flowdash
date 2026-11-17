"""Download the selected turbine records; restricted to the analysts group."""

import io

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._common import heading


@register(
    component=True,
    page=False,
    title="Raw Data Export",
    icon="download",
    description="Downloads the selected turbine records as CSV. Only members of the analysts group may use it.",
    tags=["view", "export"],
    allow_groups=["analysts"],
    default_size={"width": 360, "height": 150},
    min_size={"width": 260, "height": 120},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    def _csv(self):
        buffer = io.StringIO()
        self.turbines.to_csv(buffer, index=False)
        buffer.seek(0)
        return buffer

    def _caption(self, turbines):
        return "Waiting for turbines" if turbines is None else f"{len(turbines):,} records"

    def __panel__(self):
        turbines = self.param.turbines
        download = pmui.FileDownload(
            callback=self._csv,
            filename="turbines.csv",
            label="Download CSV",
            margin=(4, 10),
            visible=pn.bind(lambda df: df is not None, turbines),
        )
        # Released Panel warns about `button_style` when a Material FileDownload is
        # constructed with `variant`; drop once holoviz/panel#8752 is the minimum.
        download.variant = "outlined"
        return pn.Column(heading("Raw data export", pn.bind(self._caption, turbines)), download)
