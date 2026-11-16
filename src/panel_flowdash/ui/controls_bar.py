"""The bar above the tiles: the dashboard's controls, the pipeline strip and the issue banner."""

from __future__ import annotations

import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash.ui.health import DashboardHealth
from panel_flowdash.ui.issue_banner import IssueBanner, mark_issue_targets
from panel_flowdash.ui.pipeline import PipelineStrip, pipeline_stages

# Sidebar components were laid out for a sidebar about this wide.
_CONTROL_WIDTH = 320


class ControlsBar(Viewer):
    """Shown above the tile grid while the dashboard is viewed or laid out.

    It holds the views of the components registered with ``sidebar=True``
    (when the editor's ``controls_bar`` is on) in a row that wraps on narrow
    screens and, in view mode, the pipeline strip and the issue banner. The
    bar hides itself while it has nothing to show. :meth:`sync` re-reads the
    editor; the editor calls it whenever its components change.
    """

    editor = param.Parameter(doc="The FlowDash editor whose controls are shown.")

    health = param.ClassSelector(class_=DashboardHealth, doc="The tiles' health.")

    def __init__(self, **params):
        super().__init__(**params)
        self.pipeline = PipelineStrip()
        self.banner = IssueBanner(health=self.health, on_show=self._mark_issues)
        self._wrappers: dict[int, pn.Column] = {}
        self._controls = pn.FlexBox(
            align_items="flex-start",
            flex_wrap="wrap",
            margin=0,
            sizing_mode=None,
            gap="4px 16px",
            styles={"flex": "1 1 auto", "min-width": "0"},
            visible=False,
        )
        self._bar = pn.FlexBox(
            self._controls,
            self.pipeline,
            align_items="center",
            flex_wrap="wrap",
            margin=(12, 16, 0, 16),
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-controls-bar"],
            gap="8px 16px",
            styles={
                "padding": "8px 16px",
                "border": "1px solid var(--mui-palette-divider)",
                "border-radius": "12px",
                "background": "var(--mui-palette-background-paper)",
            },
        )
        # Each part carries its own margin, so the column takes no room while both are hidden.
        self._view = pn.Column(self._bar, self.banner, margin=0, sizing_mode="stretch_width")
        self.health.param.watch(self._sync_pipeline, ["failed", "waiting", "origins"])
        self.editor.param.watch(lambda _event: self.sync(), ["controls_bar"])

    @property
    def controls(self) -> list:
        """The component views currently shown as controls."""
        return [wrapper[0] for wrapper in self._controls]

    def _wrap(self, view) -> pn.Column:
        # Reusing the wrapper keeps a control from re-rendering when the bar is rebuilt.
        wrapper = self._wrappers.get(id(view))
        if wrapper is None or wrapper[0] is not view:
            wrapper = self._wrappers[id(view)] = pn.Column(
                view, margin=0, width=_CONTROL_WIDTH, styles={"max-width": "100%"}
            )
        return wrapper

    @pn.io.hold()
    def sync(self):
        """Re-read the editor's controls, graph and mode."""
        editor = self.editor
        views = list(editor.sidebar) if editor.controls_bar else []
        wrappers = [self._wrap(view) for view in views]
        self._wrappers = {id(w[0]): w for w in wrappers}
        if list(self._controls) != wrappers:
            self._controls[:] = wrappers
        self._controls.visible = bool(wrappers)
        self.health.track()
        self.banner.visible = not editor.editable
        self._sync_pipeline()

    def _sync_pipeline(self, *_events):
        editor = self.editor
        stages = (
            []
            if editor.editable
            else pipeline_stages(editor._dataflow_graph.edges, self._titles(), self.health.status)
        )
        with pn.io.hold():
            self.pipeline.update(stages)
            self._bar.visible = bool(self._controls.visible or self.pipeline.visible)

    def _titles(self) -> dict[str, str]:
        editor = self.editor
        titles = {}
        for item in editor._tile_items:
            entry = editor._component_entries.get(item["component_id"])
            title = (item.get("config") or {}).get("title")
            titles[item["instance_id"]] = title or (entry.title if entry else item["component_id"])
        return titles

    def _mark_issues(self, affected: list[str]):
        grid = self.editor._tile_grid
        views = list(grid.objects)
        ids = list(self.editor._grid_ids)
        mark_issue_targets(views, [views[ids.index(i)] for i in affected if i in ids])

    def __panel__(self):
        """Render the bar and the banner."""
        return self._view
