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

    # What the alert says; derived by a watcher, because a reference bound to
    # `checked` would hand a failure on to the alert instead of explaining it.
    _alert = param.Dict(default={"title": "", "object": "", "severity": "info"})

    @param.depends("checked", watch=True, on_init=True)
    def _explain(self):
        value = self.checked
        if isinstance(value, ReactiveError):
            reason = str(value.exception).removeprefix("Selection failed the quality gate: ")
            self._alert = {
                "title": "Selection blocked",
                "object": f"{reason}. Widen the selection to continue.",
                "severity": "error",
            }
        elif value is not None:
            self._alert = {
                "title": "Selection passed",
                "object": f"{len(value):,} turbines passed every check.",
                "severity": "success",
            }
        else:
            self._alert = self.param._alert.default

    def __panel__(self):
        alert = self.param._alert
        return pn.Column(
            heading(
                "Gate status",
                pn.bind(lambda a: "" if a["title"] else "Waiting for the gate", alert),
            ),
            pmui.Alert(
                title=pn.bind(lambda a: a["title"], alert),
                object=pn.bind(lambda a: a["object"], alert),
                severity=pn.bind(lambda a: a["severity"], alert),
                margin=(4, 10),
                sizing_mode="stretch_width",
                visible=pn.bind(lambda a: bool(a["title"]), alert),
            ),
            sizing_mode="stretch_width",
        )
