"""A page-level banner naming the tiles that failed or wait for input."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.ui.health import DashboardHealth

#: Marks the tiles "Show issues" scrolls to and highlights.
ISSUE_CLASS = "fd-issue-target"


class IssueBanner(Viewer):
    """Say how many tiles failed or wait for input, with a Show issues action.

    Hidden while every tile is up to date. ``on_show`` is called with the ids
    of the affected tiles, failed ones first; it marks their views with
    :data:`ISSUE_CLASS` and the banner then scrolls to them.
    """

    health = param.ClassSelector(class_=DashboardHealth, doc="The health to report.")

    on_show = param.Callable(
        default=None,
        doc="Called with the affected tile ids to mark their views before scrolling.",
    )

    visible = param.Boolean(default=True, doc="Whether the banner may show at all.")

    def __init__(self, **params):
        super().__init__(**params)
        self._lead = pmui.Typography(variant="body2", margin=0, sx={"fontWeight": 500})
        self._rest = pmui.Typography(variant="body2", margin=0)
        self.show_button = pmui.Button(
            label="Show issues",
            variant="text",
            size="small",
            color="error",
            margin=0,
            styles={"margin-left": "auto", "flex-shrink": "0"},
        )
        self.show_button.on_click(lambda _event: self.show_issues())
        self._focus = pmui.Reveal(color="error")
        # The children go in the alert's message area, laid out as one wrapping
        # line with the action pushed to the end.
        self._banner = pmui.Alert(
            self._lead,
            self._rest,
            self.show_button,
            severity="error",
            variant="outlined",
            margin=(12, 16, 0, 16),
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-issue-banner"],
            sx={
                "borderRadius": "12px",
                "borderColor": "transparent",
                "bgcolor": "color-mix(in srgb, var(--mui-palette-error-main) 12%, transparent)",
                "color": "text.primary",
                "alignItems": "center",
                "py": "2px",
                "& .MuiAlert-icon": {"py": "7px"},
                "& .MuiAlertTitle-root": {"display": "none"},
                "& .MuiAlert-message": {
                    "display": "flex",
                    "alignItems": "center",
                    "flexWrap": "wrap",
                    "columnGap": "6px",
                    "width": "100%",
                },
            },
        )
        self.health.param.watch(self.sync, ["failed", "waiting", "tiles"])
        self.param.watch(self.sync, "visible")
        self.sync()

    def sync(self, *_events):
        """Reflect the health in the banner."""
        lead, rest = self.health.sentence()
        with pn.io.hold():
            self._lead.object = lead
            self._rest.object = rest
            self._rest.visible = bool(rest)
            self._banner.visible = bool(self.visible and lead)

    def show_issues(self) -> list[str]:
        """Scroll to and highlight the affected tiles, returning their ids."""
        affected = self.health.affected
        if not affected:
            return []
        if self.on_show is not None:
            self.on_show(affected)
        self._focus.scroll_to(ISSUE_CLASS)
        return affected

    def __panel__(self):
        """Render the banner and its focus helper, which must stay mounted while the banner hides."""
        return pn.Column(self._banner, self._focus, margin=0, sizing_mode="stretch_width")


def mark_issue_targets(views: t.Iterable[pn.viewable.Viewable], targets: t.Iterable):
    """Give exactly the *targets* among *views* the :data:`ISSUE_CLASS`."""
    targets = list(targets)
    for view in views:
        classes = [c for c in view.css_classes if c != ISSUE_CLASS]
        if any(view is target for target in targets):
            classes.append(ISSUE_CLASS)
        if classes != view.css_classes:
            view.css_classes = classes
