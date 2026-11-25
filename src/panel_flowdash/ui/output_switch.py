"""The Dashboard/Report switch: whether the dashboard bar shows and edits the tile grid or the report.

It is interim, until a report becomes an artifact of its own, and kept in
this one place so that it can go in one change: the dashboard bar only
places :attr:`OutputSwitch.toggle` and asks the switch about the output.
"""

from __future__ import annotations

import typing as t

import panel_material_ui as pmui
import param

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

_OUTPUT_OPTIONS = {
    ":material/dashboard: Dashboard": "dashboard",
    ":material/description: Report": "report",
}

# Below this width the Report segment, second in the switch, leaves it for the
# overflow menu, so the dashboard title keeps its room. While Report is shown
# it stays, so the switch still says where the reader is.
REPORT_MENU_BREAKPOINT = 1100
_REPORT_IN_MENU = f"""
@media (max-width: {REPORT_MENU_BREAKPOINT - 0.05}px) {{
  .MuiToggleButton-root:nth-child(2):not(.Mui-selected) {{ display: none !important; }}
}}
"""

#: The overflow menu's entry for the report, while the switch has no room for it.
REPORT_ITEM = {"label": "Report", "icon": "description", "action": "output:report"}


class OutputSwitch:
    """Shows the dashboard's tile grid or its report, in view and in edit mode alike.

    In edit mode the report's Layout tab is its block editor, and the
    Dataflow tab, which both outputs share, keeps the output it was opened
    from, so Layout goes back to it. The report has no Split tab.
    """

    def __init__(self, editor: FlowDash, **params: t.Any):
        self.editor = editor
        # The output edit mode returns to from the Dataflow tab.
        self._edited = "report" if editor.surface == "report" else "dashboard"
        self._syncing = False
        self.toggle = pmui.RadioButtonGroup(
            options=_OUTPUT_OPTIONS,
            value=self.output,
            size="small",
            css_classes=["fd-output-switch"],
            **{**params, "stylesheets": [*params.get("stylesheets", []), _REPORT_IN_MENU]},
        )
        self.toggle.param.watch(self._on_toggle, "value")

    @property
    def output(self) -> str:
        """The output on screen: the report or the dashboard, in the Dataflow tab the one it came from."""
        editor = self.editor
        if editor.surface == "report":
            return "report"
        return self._edited if self._in_dataflow() else "dashboard"

    def tab(self) -> str | None:
        """Return the edit tab on screen: Layout for the report, else the editor's mode outside other surfaces."""
        editor = self.editor
        if editor.surface == "report":
            return "dashboard"
        return editor.mode if editor.surface == "workspace" else None

    def tabs(self, tabs: dict[str, str]) -> dict[str, str]:
        """Return the edit *tabs* offered for the output on screen: the report has no Split."""
        if self.output != "report":
            return tabs
        return {label: tab for label, tab in tabs.items() if tab != "split"}

    def choose_tab(self, tab: str) -> bool:
        """Show the report's block editor for its Layout tab, returning whether *tab* was the report's."""
        if tab == "dashboard" and self.output == "report":
            self.editor.surface = "report"
            return True
        if tab == "split":
            self._edited = "dashboard"
        return False

    def edit(self):
        """Note the output that edit mode starts on, the one on screen."""
        self._edited = "report" if self.editor.surface == "report" else "dashboard"

    def menu_items(self, narrow: bool) -> list[dict]:
        """Return the overflow menu's entries for the outputs the switch has no room for."""
        return [REPORT_ITEM] if narrow and self.output != "report" else []

    def show(self, output: str):
        """Show *output*, e.g. picked in the overflow menu."""
        self.toggle.value = output

    def sync(self):
        """Show the editor's output in the switch, which is not a choice of the person's."""
        editor = self.editor
        if editor.surface == "report":
            self._edited = "report"
        elif editor.surface == "workspace" and editor.mode != "wiring":
            self._edited = "dashboard"
        output = self.output
        if self.toggle.value != output:
            self._syncing = True
            try:
                self.toggle.value = output
            finally:
                self._syncing = False

    def _in_dataflow(self) -> bool:
        editor = self.editor
        return editor.editable and editor.surface == "workspace" and editor.mode == "wiring"

    def _on_toggle(self, event):
        """Show the dashboard or the report; from the Dataflow tab in its Layout."""
        output = event.new
        if self._syncing or output is None or (output == self.output and not self._in_dataflow()):
            return
        editor = self.editor
        self._edited = output
        with param.parameterized.batch_call_watchers(editor):
            if output == "report":
                editor.surface = "report"
            else:
                if editor.editable and editor.mode == "wiring":
                    editor.mode = "dashboard"
                editor.surface = "workspace"
