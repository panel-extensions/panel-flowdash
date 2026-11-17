"""Marks the nodes and tiles an agent edit added or changed, until the next run."""

from __future__ import annotations

import typing as t

from .diff import DashboardChange, dashboard_changes

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

    from .session import AgentSession

#: CSS classes set on a tile the assistant added or changed.
TILE_CLASSES = {"add": "fd-agent-added", "change": "fd-agent-changed"}

# Same colours as the tile outlines; a node may already carry the failure outline,
# which the next failure update restores.
_NODE_STYLES = {
    "add": {
        "borderColor": "var(--mui-palette-success-main, #2e7d32)",
        "boxShadow": "0 0 0 2px var(--mui-palette-success-main, #2e7d32)",
    },
    "change": {
        "borderColor": "var(--mui-palette-warning-main, #ed6c02)",
        "boxShadow": "0 0 0 2px var(--mui-palette-warning-main, #ed6c02)",
    },
}

#: Outlines for the tile classes, added to the tiles' stylesheet.
TILE_CSS = """
:host(.fd-agent-added) {
  outline: 2px solid var(--mui-palette-success-main, #2e7d32);
  outline-offset: 2px;
  border-radius: 12px;
}
:host(.fd-agent-changed) {
  outline: 2px solid var(--mui-palette-warning-main, #ed6c02);
  outline-offset: 2px;
  border-radius: 12px;
}
"""


def changed_nodes(changes: t.Iterable[DashboardChange]) -> dict[str, str]:
    """Return ``{node name: "add" | "change"}`` for the nodes a list of changes touches.

    Rewired edges and changed transforms mark the node the edge feeds. Removed
    nodes are left out, since there is nothing left to mark.
    """
    marks: dict[str, str] = {}
    for change in changes:
        parts = list(change.path)
        if parts[0] == "nodes" and len(parts) > 1:
            if change.op == "add" and len(parts) == 2:
                marks[parts[1]] = "add"
            elif change.op != "remove" or len(parts) > 2:
                marks.setdefault(parts[1], "change")
        elif parts[0] == "edges":
            edge = change.new or change.old or {}
            target = str(edge.get("to", "")).split(".")[0]
            if target:
                marks.setdefault(target, "change")
        elif parts[:2] == ["layout", "tiles"] and len(parts) > 2:
            marks.setdefault(parts[2], "change")
    return marks


class ChangeHighlighter:
    """Outlines what the assistant's last edit added (green) or changed (amber).

    The outline is on the canvas node and on the dashboard tile, so the user
    sees the change in whichever view is open. Each edit tool reports the
    dashboard just before and after its change, so the user's own edits made
    while the agent works are never outlined. The marks add up over a run,
    on the dashboard the run edits, and are cleared when the next run starts,
    or by :meth:`clear`.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        self.marked: dict[str, str] = {}
        self._editor: FlowDash | None = None
        self._run_marks: dict[str, str] = {}
        self._run_dashboard: str | None = None
        self._grids: set[int] = set()
        session.runner.state.param.watch(self._on_running, "running")
        session.runner.deps.edit_listeners.append(self._on_edit)

    def show(self, marks: dict[str, str]):
        """Mark nodes of the open dashboard, given ``{node name: "add" | "change"}``."""
        self.clear()
        editor = self.session.app._editor
        if editor is None:
            return
        ids = {name: iid for iid, name in self.session.ops._names().items()}
        self._editor = editor
        if id(editor) not in self._grids:
            # Tiles are built when the grid is (re)built, possibly after the edit.
            self._grids.add(id(editor))
            editor._tile_grid.param.watch(lambda e, ed=editor: self._sync_tiles(ed), "objects")
        for name, kind in marks.items():
            instance_id = ids.get(name)
            if instance_id is None:
                continue
            self.marked[instance_id] = kind
            editor._patch_canvas_props("node", instance_id, {"style": _NODE_STYLES[kind]})
            self._set_tile_class(instance_id, kind)
        self._resync_canvas(editor)

    def clear(self):
        editor = self._editor
        if editor is not None:
            for instance_id in self.marked:
                editor._patch_canvas_props("node", instance_id, {"style": None})
                self._set_tile_class(instance_id, None)
            self._resync_canvas(editor)
        self.marked = {}
        self._editor = None

    def _on_running(self, event):
        if event.new:
            self.clear()
            self._run_marks = {}

    def _on_edit(self, before: str | None, after: str):
        current = self.session.app._current_dashboard
        dashboard_id = current.dashboard_id if current is not None else None
        if dashboard_id != self._run_dashboard:
            self._run_marks = {}
            self._run_dashboard = dashboard_id
        for name, kind in changed_nodes(dashboard_changes(before, after)).items():
            if self._run_marks.get(name) != "add":
                self._run_marks[name] = kind
        self.show(self._run_marks)

    @staticmethod
    def _resync_canvas(editor):
        # A patch only reaches a canvas that is on screen; a canvas shown later
        # (view mode, the Layout tab) renders from the nodes list, so send it again.
        if editor._canvas_batch is None:
            editor._flow.param.trigger("nodes")

    def _sync_tiles(self, editor):
        if editor is self._editor:
            for instance_id, kind in self.marked.items():
                self._set_tile_class(instance_id, kind)

    def _set_tile_class(self, instance_id: str, kind: str | None):
        tile = self._editor._inspect_tiles.get(instance_id)
        if tile is None:
            return
        if TILE_CSS not in tile.stylesheets:
            tile.stylesheets = [*tile.stylesheets, TILE_CSS]
        classes = [c for c in tile.css_classes if c not in TILE_CLASSES.values()]
        if kind is not None:
            classes.append(TILE_CLASSES[kind])
        tile.css_classes = classes
