"""Marks the nodes and tiles an agent edit added or changed, until the next run or the end of editing."""

from __future__ import annotations

import typing as t

from .diff import DashboardChange, dashboard_changes

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

    from .session import AgentSession

#: CSS classes set on a tile the assistant added or changed.
TILE_CLASSES = {"add": "fd-agent-added", "change": "fd-agent-changed"}

# Same colours as the tile outlines. They are drawn over a failure outline,
# which shows again once they are removed.
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

#: The ring for the tile classes, added to the tile grid's stylesheet. It is
#: drawn on the grid item that holds the tile, which is the card while the
#: tiles are arranged and the tile's own box in view mode, so it sits on the
#: tile's visible edge with its radius (the tile's 12px in view mode) and
#: moves and resizes with it. It is a pseudo-element above the content, since
#: a view that fills the tile would cover an inset shadow.
GRID_CSS = """
.muuri-grid-item:has(> * > .fd-agent-added) {
  --fd-agent-outline: var(--mui-palette-success-main, #2e7d32);
}
.muuri-grid-item:has(> * > .fd-agent-changed) {
  --fd-agent-outline: var(--mui-palette-warning-main, #ed6c02);
}
.muuri-grid-item:has(> * > :is(.fd-agent-added, .fd-agent-changed))::after {
  border: 2px solid var(--fd-agent-outline);
  border-radius: inherit;
  box-sizing: border-box;
  content: "";
  inset: 0;
  pointer-events: none;
  position: absolute;
  z-index: 30;
}
.muuri-no-card .muuri-grid-item:has(> * > :is(.fd-agent-added, .fd-agent-changed))::after {
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
    when the user stops editing or discards the changes, when the dashboard
    is reloaded, or by :meth:`clear`.
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
        """Mark nodes of the open dashboard, given ``{node name: "add" | "change"}``.

        Only the marks that differ from the ones shown are patched, and the
        canvas is sent again only when one did, since that resends every node.
        """
        editor = self.session.app._editor
        if editor is not self._editor:
            self.clear()
        if editor is None:
            return
        ids = {name: iid for iid, name in self.session.ops._names().items()}
        wanted = {ids[name]: kind for name, kind in marks.items() if name in ids}
        styles = self._node_styles(editor)
        self._editor = editor
        if id(editor) not in self._grids:
            # Tiles are built when the grid is (re)built, possibly after the edit.
            self._grids.add(id(editor))
            editor._tile_grid.param.watch(lambda e, ed=editor: self._sync_tiles(ed), "objects")
            if GRID_CSS not in editor._tile_grid.stylesheets:
                editor._tile_grid.stylesheets = [*editor._tile_grid.stylesheets, GRID_CSS]
            editor.param.watch(lambda e, ed=editor: self._on_editable(ed, e.new), "editable")
            editor.param.watch(
                lambda e, ed=editor: self._on_load(ed), "dashboard", onlychanged=False
            )
        changed = False
        for instance_id in [iid for iid in self.marked if iid not in wanted]:
            del self.marked[instance_id]
            editor._set_node_outline(instance_id, None)
            self._set_tile_class(instance_id, None)
            changed = True
        for instance_id, kind in wanted.items():
            # A node rebuilt since it was marked, e.g. by a reload, has lost its style.
            style = _NODE_STYLES[kind]
            shown = styles.get(instance_id) or {}
            if self.marked.get(instance_id) == kind and style.items() <= shown.items():
                continue
            self.marked[instance_id] = kind
            editor._set_node_outline(instance_id, style)
            self._set_tile_class(instance_id, kind)
            changed = True
        if changed:
            self._resync_canvas(editor)

    @staticmethod
    def _node_styles(editor) -> dict[str, t.Any]:
        pending = editor._canvas_batch[0] if editor._canvas_batch is not None else []
        return {node["id"]: node.get("style") for node in [*editor._flow.nodes, *pending]}

    def clear(self):
        editor = self._editor
        if editor is not None:
            for instance_id in self.marked:
                editor._set_node_outline(instance_id, None)
                self._set_tile_class(instance_id, None)
            self._resync_canvas(editor)
        self.marked = {}
        self._editor = None

    def _on_running(self, event):
        if event.new:
            self.clear()
            self._run_marks = {}

    def _forget(self, editor):
        if editor is self._editor:
            self.clear()
            self._run_marks = {}

    def _on_editable(self, editor, editable: bool):
        if not editable:
            self._forget(editor)

    def _on_load(self, editor):
        # Edits, undo and redo load a model quietly; opening a dashboard or
        # discarding its changes reloads it, and the marks no longer apply.
        if editor._hydrating and not editor._history_quiet:
            self._forget(editor)

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
        classes = [c for c in tile.css_classes if c not in TILE_CLASSES.values()]
        if kind is not None:
            classes.append(TILE_CLASSES[kind])
        tile.css_classes = classes
