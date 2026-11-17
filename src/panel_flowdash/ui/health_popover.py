"""The health chip in the dashboard header and its popover with the pipeline and the issues."""

from __future__ import annotations

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.ui.health import FAILED, OK, WAITING, DashboardHealth
from panel_flowdash.ui.issue_banner import IssueBanner, mark_issue_targets
from panel_flowdash.ui.pipeline import PipelineStrip, pipeline_stages
from panel_flowdash.ui.popover_access import NON_MODAL_POPUP_SX, PopoverAccess, link_popup

_COLORS = {OK: "success", WAITING: "warning", FAILED: "error"}


def chip_label(health: DashboardHealth) -> str:
    """Return the chip's text: "9/9 up to date", or the counts of failed and waiting tiles."""
    tiles = len(health.tiles)
    if not tiles:
        return ""
    parts = []
    if health.failed:
        parts.append(f"{len(health.failed)} failed")
    if health.waiting:
        parts.append(f"{len(health.waiting)} waiting")
    if parts:
        return " · ".join(parts)
    return f"{tiles}/{tiles} up to date"


def _chip_sx(status: str) -> dict:
    color = _COLORS[status]
    return {
        "textTransform": "none",
        "minWidth": 0,
        "height": "28px",
        "px": "10px",
        "gap": "6px",
        "borderRadius": "14px",
        "fontWeight": 500,
        "whiteSpace": "nowrap",
        "color": f"{color}.main",
        "bgcolor": f"color-mix(in srgb, var(--mui-palette-{color}-main) 12%, transparent)",
        "&:hover": {
            "bgcolor": f"color-mix(in srgb, var(--mui-palette-{color}-main) 20%, transparent)"
        },
        "&::before": {
            "content": '""',
            "width": "8px",
            "height": "8px",
            "borderRadius": "50%",
            "flexShrink": 0,
            "bgcolor": f"{color}.main",
        },
    }


class HealthPopover(Viewer):
    """A chip saying whether the tiles are up to date, opening the pipeline and the issues.

    The chip is coloured by the worst tile status and counts the failed and
    waiting tiles. Its popover shows the pipeline strip (source, transform
    and view stages, each with a status dot) and, while some tiles failed or
    wait, the issue banner with its Show issues action. :meth:`sync` re-reads
    the editor's graph; the health's own changes are followed automatically.
    """

    editor = param.Parameter(doc="The FlowDash editor whose tiles are reported.")

    health = param.ClassSelector(class_=DashboardHealth, doc="The tiles' health.")

    def __init__(self, **params):
        super().__init__(**params)
        self.pipeline = PipelineStrip()
        self.banner = IssueBanner(health=self.health, on_show=self._show_issues)
        self.chip = pmui.Button(
            label="",
            variant="text",
            size="small",
            margin=0,
            visible=False,
            css_classes=["fd-health-chip"],
            sx=_chip_sx(OK),
        )
        self.chip.on_click(self._toggle)
        self._ok_note = pmui.Typography(
            "Every tile shows current data.",
            variant="body2",
            margin=0,
            sx={"color": "text.secondary"},
        )
        self.popup = pmui.Popup(
            self.pipeline,
            self._ok_note,
            self.banner._banner,
            anchor_origin={"horizontal": "left", "vertical": "bottom"},
            transform_origin={"horizontal": "left", "vertical": "top"},
            elevation=4,
            hide_backdrop=True,
            css_classes=["fd-health-popover"],
            sx={
                **NON_MODAL_POPUP_SX,
                "& .MuiPopover-paper": {
                    **NON_MODAL_POPUP_SX["& .MuiPopover-paper"],
                    "p": 2,
                    "mt": 1,
                    "maxWidth": "min(560px, calc(100vw - 32px))",
                    "display": "flex",
                    "flexDirection": "column",
                    "gap": 1.5,
                    "borderRadius": "12px",
                },
            },
        )
        self.banner._banner.margin = 0
        self._access = PopoverAccess(
            button_class="fd-health-chip",
            popup_class="fd-health-popover",
            label="Dashboard health",
            margin=0,
            width=0,
            height=0,
        )
        link_popup(self.popup, self._access)
        # The popup anchors to its parent, and the focus helper must stay mounted
        # while the popup is closed, so both sit next to the chip.
        self._view = pn.Row(
            self.chip,
            self.popup,
            self._access,
            self.banner._focus,
            margin=0,
            align="center",
            # Only as wide as the chip, so the chip flows inline with the text around it.
            styles={"width": "fit-content", "flex": "0 0 auto"},
        )
        self.health.param.watch(self.sync, ["failed", "waiting", "tiles", "origins"])

    @property
    def visible(self) -> bool:
        """Whether the chip is shown, i.e. the dashboard has tiles."""
        return self.chip.visible

    def _toggle(self, _event=None):
        self.popup.open = not self.popup.open

    def _show_issues(self, affected: list[str]):
        editor = self.editor
        views = list(editor._tile_grid.objects)
        ids = list(editor._grid_ids)
        mark_issue_targets(views, [views[ids.index(i)] for i in affected if i in ids])
        self.popup.open = False

    def _titles(self) -> dict[str, str]:
        editor = self.editor
        titles = {}
        for item in editor._tile_items:
            entry = editor._component_entries.get(item["component_id"])
            title = (item.get("config") or {}).get("title")
            titles[item["instance_id"]] = title or (entry.title if entry else item["component_id"])
        return titles

    def sync(self, *_events):
        """Re-read the health and the dataflow graph."""
        editor = self.editor
        health = self.health
        status = FAILED if health.failed else WAITING if health.waiting else OK
        stages = pipeline_stages(editor._dataflow_graph.edges, self._titles(), health.status)
        label = chip_label(health)
        with pn.io.hold():
            self.pipeline.update(stages)
            self._ok_note.visible = status == OK
            self.chip.param.update(
                label=label,
                visible=bool(label),
                sx=_chip_sx(status),
                description=f"{health.summary()}. Show the pipeline and any issues.",
            )
            if not label:
                self.popup.open = False

    def __panel__(self):
        """Render the chip with its popover."""
        return self._view
