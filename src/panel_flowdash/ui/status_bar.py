"""The status bar under the wiring canvas: issues, counts, key hints and zoom."""

from __future__ import annotations

import typing as t
import weakref
from dataclasses import dataclass
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.groups import component_nodes
from panel_flowdash.ui.node_card import NodeCard
from panel_flowdash.ui.tile_layout import GRID_COLUMNS, layout_summary

_KEYS_CSS = """
.fd-keys { display: flex; align-items: center; gap: 12px; white-space: nowrap;
  font-size: 12px; color: var(--mui-palette-text-secondary, inherit); }
kbd { font: inherit; font-size: 11px; padding: 0 5px; margin-right: 4px; border-radius: 4px;
  border: 1px solid var(--mui-palette-divider, rgba(127, 127, 127, 0.4)); }
"""


_NOWRAP = {"color": "text.secondary", "whiteSpace": "nowrap"}


@dataclass(frozen=True)
class Issue:
    """Something on the canvas that keeps an output from being up to date."""

    instance_id: str | None
    title: str
    status: str
    detail: str
    edge_id: str | None = None


def component_title(editor, instance_id: str) -> str:
    """Return the title of a placed component, or its id if its spec is unknown."""
    spec = editor._component_specs.get(editor._component_id_for_instance(instance_id) or "")
    return spec.title if spec else instance_id


def connection_title(editor, mapping) -> str:
    """Name a connection by its ends, e.g. 'State Filter to Capacity by Year'."""
    return f"{component_title(editor, mapping[0])} to {component_title(editor, mapping[2])}"


def canvas_issues(editor) -> list[Issue]:
    """List the components that failed, wait for input or are unavailable, then broken edges.

    An edge is broken when the transform being edited on it has a failing step
    or is refused by its target, or when it could not be restored.
    """
    issues = [
        Issue(node["id"], card.title, card.status, card.status_detail)
        for node in editor._flow.nodes
        if isinstance(card := node.get("view"), NodeCard) and card.status != "ok"
    ]
    issues += [
        Issue(
            None,
            f"{transform.issue} on {connection_title(editor, mapping)}",
            "failed",
            transform.message or "",
            edge_id=edge_id,
        )
        for edge_id, transform in editor._transform_editors.items()
        if transform.issue and (mapping := editor._edge_id_map.get(edge_id))
    ]
    issues += [
        Issue(None, "Connection", "unavailable", unresolved.reason)
        for unresolved in editor._unresolved_edges
    ]
    return issues


def select_on_canvas(flow, node_id: str | None = None, edge_id: str | None = None):
    """Select one node or edge on the canvas, or clear the selection."""
    for node in flow.nodes:
        selected = node["id"] == node_id
        if bool(node.get("selected")) != selected:
            flow.patch_node_props(node["id"], {"selected": selected})
    for edge in flow.edges:
        selected = edge["id"] == edge_id
        if bool(edge.get("selected")) != selected:
            flow.patch_edge_props(edge["id"], {"selected": selected})
    # On a page the canvas reports the new selection itself. Setting it here too
    # would reach the page before the patches, which then reports the old one.
    if not flow._models:
        flow.selection = {
            "nodes": [node_id] if node_id else [],
            "edges": [edge_id] if edge_id else [],
        }


def _flush(watcher_ref: weakref.ref[CanvasWatcher]):
    # Weak, so a callback the session has yet to run does not keep the editor alive.
    if (watcher := watcher_ref()) is not None:
        watcher._pending = False
        watcher._callback()


class CanvasWatcher:
    """Call *callback* when nodes or edges change, or a node card changes its status.

    In a served session the calls of one burst, e.g. every card of a dashboard
    being loaded changing its status, are coalesced into one on the next tick.
    """

    def __init__(self, flow, callback: t.Callable[[], None]):
        self._flow = flow
        self._callback = callback
        self._cards: dict[int, tuple[NodeCard, param.parameterized.Watcher]] = {}
        self._pending = False
        flow.param.watch(self._sync, ["nodes", "edges"])
        self._sync()

    def _changed(self):
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self._callback()
        elif not self._pending:
            self._pending = True
            pn.state.execute(partial(_flush, weakref.ref(self)), schedule=True)

    def _sync(self, *_events):
        cards = {
            id(card): card
            for node in self._flow.nodes
            if isinstance(card := node.get("view"), NodeCard)
        }
        for key in set(self._cards) - set(cards):
            card, watcher = self._cards.pop(key)
            card.param.unwatch(watcher)
        # Weak, so a component kept alive elsewhere does not keep the editor alive
        # through its node state, its card and this watcher.
        owner = weakref.ref(self)

        def changed(*_events):
            if (watcher := owner()) is not None:
                watcher._changed()

        for key, card in cards.items():
            if key not in self._cards:
                self._cards[key] = (card, card.param.watch(changed, "status"))
        self._changed()


class StatusBar(Viewer):
    """A one-line bar under the canvas summarizing the dashboard's state.

    It says whether every output is up to date or how many issues there are
    (clicking the count selects the first component or connection with an
    issue), counts the components and connections, lists key hints and shows
    the zoom.
    In the Layout tab it describes the tile grid instead: its columns, the
    tiles on it and the components without a tile.
    The canvas follows the page's light or dark theme through it, since it is
    a Material component shown alongside the canvas in every edit session.
    """

    editor = param.Parameter(doc="The FlowDash editor whose canvas the bar describes.")

    visible = param.Boolean(default=True, doc="Whether the bar is shown.")

    def __init__(self, **params):
        super().__init__(**params)
        flow = self.editor._flow
        self.issues_button = pmui.Button(
            label="All outputs up to date",
            icon="check_circle",
            variant="text",
            size="small",
            color="success",
            margin=0,
            styles={"flex": "none"},
            sx={"textTransform": "none", "fontWeight": 400, "whiteSpace": "nowrap"},
        )
        self.issues_button.on_click(self._on_issues_clicked)
        self.counts = pmui.Typography(
            variant="caption", margin=(0, 12), styles={"flex": "none"}, sx=_NOWRAP
        )
        self.keys = pn.pane.HTML(
            '<div class="fd-keys"><span><kbd>Space</kbd>drag to pan</span>'
            "<span><kbd>Del</kbd>remove</span></div>",
            margin=(0, 12),
            styles={"flex": "none"},
            stylesheets=[_KEYS_CSS],
        )
        self.zoom = pmui.Typography(
            variant="caption", margin=0, styles={"flex": "none"}, sx=_NOWRAP
        )
        self.grid_label = pmui.Typography(
            f"{GRID_COLUMNS}-column grid",
            variant="caption",
            margin=(0, 12, 0, 0),
            styles={"flex": "none"},
            sx=_NOWRAP,
        )
        self.minimap_toggle = pmui.ToggleIcon(
            icon="map",
            active_icon="map",
            value=flow.show_minimap,
            description="Show minimap",
            size="small",
            margin=(0, 0, 0, 4),
            styles={"flex": "none"},
        )
        self.minimap_toggle.link(flow, value="show_minimap", bidirectional=True)
        settings = self.editor.settings
        self.preview_toggle = pmui.ToggleIcon(
            icon="preview",
            active_icon="preview",
            value=settings.node_previews,
            description="Preview values on the canvas",
            size="small",
            margin=(0, 0, 0, 4),
            styles={"flex": "none"},
        )
        # Like the minimap, a view of this session only, which starts from the dashboard's setting.
        self.preview_toggle.param.watch(
            lambda event: self.editor._show_node_previews(event.new), "value"
        )
        settings.param.watch(self._follow_preview_setting, "node_previews")
        self._bar = pn.FlexBox(
            self.grid_label,
            self.issues_button,
            self.counts,
            pn.layout.HSpacer(),
            self.keys,
            self.zoom,
            self.preview_toggle,
            self.minimap_toggle,
            align_items="center",
            flex_wrap="nowrap",
            margin=0,
            sizing_mode="stretch_width",
            height=32,
            visible=self.param.visible,
            styles={
                "padding": "0 12px",
                "overflow": "hidden",
                "background": "var(--mui-palette-background-paper)",
                "border-top": "1px solid var(--mui-palette-divider)",
            },
        )
        self.issues_button.param.watch(self._follow_theme, "dark_theme")
        flow.color_mode = "dark" if self.issues_button.dark_theme else "light"
        flow.param.watch(self._sync_zoom, "viewport")
        self._sync_zoom()
        self._watcher = CanvasWatcher(flow, self.sync)
        self.editor.param.watch(lambda _event: self.sync(), "mode")
        self.editor._tile_grid.param.watch(self._on_tile_layout, ["layout", "objects"])

    def _on_tile_layout(self, _event):
        if self.editor.mode == "dashboard":
            self.sync()

    def _follow_preview_setting(self, event):
        self.preview_toggle.value = event.new

    def _follow_theme(self, event):
        self.editor._flow.color_mode = "dark" if event.new else "light"

    def _sync_zoom(self, *_events):
        zoom = (self.editor._flow.viewport or {}).get("zoom") or 1
        self.zoom.object = f"Zoom {zoom * 100:.0f}%"

    @pn.io.hold()
    @param.depends("visible", watch=True)
    def sync(self):
        """Recount the components, connections and issues."""
        if not self.visible:
            return
        editor = self.editor
        layout = editor.mode == "dashboard"
        self.grid_label.visible = layout
        for item in (
            self.issues_button,
            self.keys,
            self.zoom,
            self.preview_toggle,
            self.minimap_toggle,
        ):
            item.visible = not layout
        if layout:
            self.counts.object = layout_summary(editor)
            return
        issues = canvas_issues(editor)
        if issues:
            label = f"1 issue: {issues[0].title}" if len(issues) == 1 else f"{len(issues)} issues"
            self.issues_button.param.update(
                label=label,
                icon="error_outline",
                color="error" if any(i.status == "failed" for i in issues) else "warning",
            )
        else:
            self.issues_button.param.update(
                label="All outputs up to date",
                icon="check_circle",
                color="success",
            )
        components = len(component_nodes(editor._flow.nodes))
        connections = len(editor._flow.edges)
        self.counts.object = (
            f"{components} component{'' if components == 1 else 's'}, "
            f"{connections} connection{'' if connections == 1 else 's'}"
        )

    def _on_issues_clicked(self, _event):
        first = next((i for i in canvas_issues(self.editor) if i.instance_id or i.edge_id), None)
        if first is not None:
            select_on_canvas(self.editor._flow, node_id=first.instance_id, edge_id=first.edge_id)

    def __panel__(self):
        """Render the bar."""
        return self._bar
