"""Marks the nodes and tiles an agent edit added or changed, until the next run."""

from __future__ import annotations

import typing as t
from functools import partial

from .diff import DashboardChange, dashboard_changes
from .ops import OperationError

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
    sees the change in whichever view is open. It compares each version the
    session's agent publishes of the open dashboard with the dashboard as it
    was when the run started, so it shows everything the run changed so far,
    and is cleared when the next run starts, or by :meth:`clear`.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        self.marked: dict[str, str] = {}
        self._editor: FlowDash | None = None
        self._store = None
        self._watchers: list = []
        self._baseline: str | None = None
        self._grids: set[int] = set()
        state = session.runner.state
        state.param.watch(self._on_running, "running")
        state.param.watch(lambda e: self._follow_store(), "artifacts")
        self._follow_store()

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
        if not event.new:
            return
        self.clear()
        try:
            self._baseline = self.session.ops.get_dashboard()
        except OperationError:
            self._baseline = None

    def _follow_store(self):
        store = self.session.runner.state.artifacts
        if store is self._store:
            return
        for obj, watcher in self._watchers:
            obj.param.unwatch(watcher)
        self._store = store
        self._watchers = [
            (store, store.param.watch(lambda e: self._follow_artifacts(), "artifacts"))
        ]
        self._follow_artifacts()

    def _follow_artifacts(self):
        watched = {id(obj) for obj, _ in self._watchers}
        for artifact in self._store:
            if id(artifact) not in watched and artifact.artifact_id.startswith("dashboard:"):
                watcher = artifact.param.watch(partial(self._on_versions, artifact), "versions")
                self._watchers.append((artifact, watcher))
                self._on_versions(artifact)

    def _on_versions(self, artifact, *events):
        versions = artifact.versions
        before = (
            self._baseline
            if self._baseline is not None
            else (versions[-2].value if len(versions) > 1 else None)
        )
        if not versions or before is None:
            return
        self.show(changed_nodes(dashboard_changes(before, versions[-1].value)))

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
