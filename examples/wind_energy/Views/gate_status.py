"""Shows whether the quality gate let the selection through, failures included."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._common import heading

# Failures only arrive as values with Param 2.5 and later.
ReactiveError = getattr(param, "ReactiveError", ())


@register(
    component=True,
    page=False,
    title="Gate Status",
    icon="rule",
    description="Says whether the quality gate passed and, if not, why; it receives the failure itself.",
    tags=["view", "quality"],
    handles_failures=["checked"],
    default_size={"width": 360, "height": 150},
    min_size={"width": 260, "height": 120},
)
class app(pn.viewable.Viewer):
    checked = param.Parameter(default=None, doc="The gate's output, or the failure it raised.")

    @param.depends("checked")
    def _status(self):
        value = self.checked
        if isinstance(value, ReactiveError):
            reason = str(value.exception).removeprefix("Selection failed the quality gate: ")
            return pn.Column(
                heading("Gate status"),
                pmui.Alert(
                    title="Selection blocked",
                    object=f"{reason}. Widen the selection to continue.",
                    severity="error",
                    margin=(4, 10),
                    sizing_mode="stretch_width",
                ),
            )
        if value is None:
            return heading("Gate status", "Waiting for the gate")
        return pn.Column(
            heading("Gate status"),
            pmui.Alert(
                title="Selection passed",
                object=f"{len(value):,} turbines passed every check.",
                severity="success",
                margin=(4, 10),
                sizing_mode="stretch_width",
            ),
        )

    def __panel__(self):
        return pn.panel(self._status, sizing_mode="stretch_width")
