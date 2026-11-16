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

    @param.depends("turbines")
    def _view(self):
        if self.turbines is None:
            return heading("Raw data export", "Waiting for turbines")
        return pn.Column(
            heading("Raw data export", f"{len(self.turbines):,} records"),
            pmui.FileDownload(
                callback=self._csv,
                filename="turbines.csv",
                label="Download CSV",
                variant="outlined",
                margin=(4, 10),
            ),
        )

    def __panel__(self):
        return pn.panel(self._view)
