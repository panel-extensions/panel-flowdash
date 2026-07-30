"""The embeddable dataflow editor.

:class:`FlowDash` is the reusable half of the framework: a ReactFlow wiring
canvas plus a tile-grid layout editor over a set of components. It knows nothing
about routing, pages, navigation or identity, so it can be dropped into any Panel
app, notebook or template. :class:`~panel_flowdash.app.FlowDashApp` builds the
full multi-page application on top of it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import typing as t
import uuid
from contextlib import contextmanager

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
from panel.viewable import Children, Viewer
from panel_tiles import TileGrid

from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_spec import build_component_specs
from panel_flowdash.dashboard_store import (
    BaseDashboardStore,
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
)
from panel_flowdash.dataflow_engine import DataflowGraph
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.util import notify

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.component_spec import ComponentSpec

logger = logging.getLogger("panel_flowdash")

_FLOW_STYLESHEET = """\
.react-flow__node {
  padding: 0;
  border-radius: 6px;
  border: 1px solid var(--xy-node-border, var(--panel-border-color));
  background-color: var(--xy-node-background-color, var(--panel-background-color));
  box-shadow: 0 1px 2px var(--panel-shadow-color);
  color: var(--xy-node-color, var(--panel-on-background-color));
  font-size: 13px;
  min-width: 140px;
}
.react-flow__handle {
  width: 14px;
  height: 14px;
  border: 1px solid black;
  background: transparent;
}"""


class FlowDash(Viewer):
    """A dataflow wiring canvas and dashboard layout editor over a set of components.

    The editor pairs a ReactFlow canvas, where components are placed and their
    typed ports wired together, with a tile grid that lays the same components
    out as a dashboard. Everything to do with routing, pages and identity lives
    in :class:`~panel_flowdash.app.FlowDashApp` instead, so this can be embedded
    anywhere.

    Parameters
    ----------
    components
        The components to offer. Accepts a decorated function, a ``Viewer``
        subclass, a mapping of explicit component ids, a project directory to
        scan, or a list mixing any of those. See
        :func:`~panel_flowdash.component_library.normalize_components`.

    Examples
    --------
    >>> editor = FlowDash(components=[ticker_select, price_chart])
    >>> src = editor.add_component("Components/ticker_select")
    >>> dst = editor.add_component("Components/price_chart")
    >>> editor.connect(src, "ticker", dst, "ticker")
    True
    """

    breakpoints = param.List(default=[768, 1200], doc="Responsive breakpoints for the tile grid.")

    components = param.Parameter(
        default=None,
        doc="""
        The components to offer in the editor. A decorated function, a Viewer
        subclass, a mapping of explicit component ids, a project directory, or a
        list mixing any of those. Read at construction time.""",
    )

    dashboard = param.ClassSelector(
        class_=DashboardModel,
        default=None,
        doc="""
        The dashboard currently loaded. Updated by `load`, `load_model`,
        `new_dashboard` and `save`. May be passed at construction as either a
        DashboardModel or, when a store is configured, a dashboard id or title.""",
    )

    dirty = param.Boolean(
        default=False,
        doc="""
        Whether the canvas has unsaved changes. Managed by the editor; watch it
        to prompt before discarding work.""",
    )

    editable = param.Boolean(
        default=True,
        doc="""
        Whether the dashboard can be edited. When False the toolbar is hidden
        and the tile grid is shown locked, giving a pure view of the dashboard.""",
    )

    mode = param.Selector(
        default="wiring",
        objects=["wiring", "dashboard"],
        doc="""
        Which workspace is shown: 'wiring' for the ReactFlow canvas, 'dashboard'
        for the tile grid.""",
    )

    notifications = param.Boolean(
        default=True,
        doc="""
        Whether to surface user-facing messages as Panel notifications. When
        disabled (or when no notification area exists) messages are logged.""",
    )

    preview = param.Boolean(
        default=False,
        doc="""
        Preview the dashboard as an end user sees it without leaving edit mode.
        Only meaningful while `editable` and in 'dashboard' mode.""",
    )

    read_only = param.Boolean(
        default=False,
        doc="""
        Whether saving is forbidden. The canvas can still be rearranged but
        `save` refuses. Set this from your own authorization logic.""",
    )

    saved = param.Event(doc="Triggered after a dashboard is successfully saved.")

    sidebar = Children(
        default=[],
        doc="""
        Views of placed components that declare `sidebar=True`, which are kept
        out of the tile grid. Managed by the editor; render these wherever your
        layout wants them.""",
    )

    store = param.ClassSelector(
        class_=BaseDashboardStore,
        default=None,
        doc="""
        Dashboard persistence backend. Accepts a store instance or a path to a
        SQLite file. When None the editor is ephemeral and `save` merely returns
        the model for the caller to persist.""",
    )

    toolbar = param.Boolean(
        default=True, doc="Whether to render the editor toolbar above the workspace."
    )

    toolbar_extra = Children(
        default=[], doc="Additional items appended to the right of the toolbar."
    )

    user = param.String(
        default="local", doc="Principal recorded as the owner of dashboards created here."
    )

    def __init__(self, components=None, **params):
        if components is not None:
            params["components"] = components
        if isinstance(params.get("store"), (str, pathlib.Path)):
            params["store"] = DashboardStore(params["store"])
        # Held back until the canvas exists, and resolved from an id if needed.
        dashboard = params.pop("dashboard", None)

        super().__init__(**params)

        self._registry: dict[str, RegistryEntry] = normalize_components(self.components)
        self._component_entries = {k: v for k, v in self._registry.items() if v.metadata.component}
        self._component_specs: dict[str, ComponentSpec] = {}
        self._components_loaded = False

        self._muted = False
        self._grid_populated = False
        self._edge_count = 0
        self._edge_id_map: dict[str, tuple[str, str, str, str]] = {}
        self._tile_items: list[dict] = []
        self._tile_objects: list[Viewable] = []
        self._pending_tile_layout: list[dict] = []
        self._pending_breakpoints: list[int] = []
        self._pending_responsive_layouts: dict = {}

        self._dataflow_graph = DataflowGraph({}, on_error=self._on_wiring_error)
        self._component_picker = self._make_component_picker()
        self._flow = self._build_flow_canvas()
        self._view = self._build_component_view()

        # Components handed over as live objects need no import, so their specs
        # can be built eagerly and the editor is usable the moment it is
        # constructed. Directory-scanned entries are imported lazily instead,
        # off the event loop, by `ensure_components_loaded_async`.
        if all(entry.app is not None for entry in self._component_entries.values()):
            self._build_specs()

        self._apply_mode_state()
        if dashboard is not None:
            self._init_dashboard(dashboard)

    def _init_dashboard(self, dashboard):
        """Resolve the ``dashboard`` constructor argument to a loaded model."""
        if isinstance(dashboard, DashboardModel):
            self.load_model(dashboard)
        elif self.store is not None:
            self.load(dashboard)
        else:
            raise ValueError(
                f"Cannot load dashboard {dashboard!r} without a store; pass a "
                "DashboardModel or configure store=."
            )

    # ------------------------------------------------------------------
    # Component loading
    # ------------------------------------------------------------------

    def _load_entries(self) -> list[str]:
        """Import every component module, collecting failures rather than raising."""
        errors: list[str] = []
        for entry in self._component_entries.values():
            try:
                entry.load()
            except Exception as exc:
                errors.append(f"{entry.app_id}: {exc}")
        return errors

    def _build_specs(self, errors: list[str] | None = None):
        """Introspect the loaded components and rebuild the graph and canvas.

        Replaces the dataflow graph wholesale, so this must run before any node
        is placed.
        """
        for msg in errors or []:
            logger.warning("Failed to load component: %s", msg)
            self._notify("warning", f"Component load failed: {msg}", duration=6000)
        self._component_specs = build_component_specs(self._registry)
        self._dataflow_graph = DataflowGraph(self._component_specs, on_error=self._on_wiring_error)
        self._rebuild_flow_canvas()
        self._components_loaded = True

    def ensure_components_loaded(self):
        """Import all component modules and build their specs, if not done already.

        Called automatically whenever specs are needed. On a live server prefer
        :meth:`ensure_components_loaded_async`, which imports off the event loop.
        """
        if self._components_loaded:
            return
        self._build_specs(self._load_entries())

    async def ensure_components_loaded_async(self):
        """Async :meth:`ensure_components_loaded`, importing off the event loop."""
        if self._components_loaded:
            return
        already_loaded = all(e.app is not None for e in self._component_entries.values())
        errors = [] if already_loaded else await asyncio.to_thread(self._load_entries)
        self._build_specs(errors)

    @property
    def component_specs(self) -> dict[str, ComponentSpec]:
        """Specs for the available components, keyed by component id."""
        self.ensure_components_loaded()
        return self._component_specs

    @property
    def graph(self) -> DataflowGraph:
        """The live dataflow graph wiring the placed components together."""
        return self._dataflow_graph

    # ------------------------------------------------------------------
    # Notifications and error reporting
    # ------------------------------------------------------------------

    def _notify(self, severity: str, message: str, duration: int = 3000):
        """Surface a message to the user, or log it when notifications are unavailable."""
        notify(severity, message, duration=duration, enabled=self.notifications)

    def _on_wiring_error(self, source_id, source_port, target_id, target_port, exc):
        logger.error(
            "Runtime wiring error (%s.%s -> %s.%s): %s",
            source_id,
            source_port,
            target_id,
            target_port,
            exc,
            exc_info=exc,
        )
        self._notify(
            "error",
            f"Runtime wiring error ({source_port} → {target_port}): {exc}",
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Canvas construction
    # ------------------------------------------------------------------

    def _make_component_picker(self):
        groups: dict[str, dict[str, str]] = {}
        for app_id, entry in self._component_entries.items():
            section = entry.section.replace("_", " ")
            groups.setdefault(section, {})[entry.title] = app_id
        value = next(iter(self._component_entries), None)
        return pmui.Select(
            label="Component",
            groups=groups,
            value=value,
            searchable=True,
            filter_on_search=True,
            size="small",
        )

    def _node_types_from_specs(self):
        node_types = {}
        node_editors = {}
        for comp_id, spec in self._component_specs.items():
            type_key = comp_id.replace("/", "__")
            node_types[type_key] = pr.NodeType(
                type=type_key,
                label=spec.title,
                schema=spec.config_state_class,
                inputs=[
                    {"id": port.name, "label": port.label or port.name} for port in spec.inputs
                ],
                outputs=[
                    {"id": port.name, "label": port.label or port.name} for port in spec.outputs
                ],
            )
            if spec.config_editor is not None:
                node_editors[type_key] = spec.config_editor
        return node_types, node_editors

    def _rebuild_flow_canvas(self):
        """Update node_types on the live ReactFlow canvas after component load."""
        node_types, node_editors = self._node_types_from_specs()
        self._flow.param.update(node_types=node_types, node_editors=node_editors)

    def _build_flow_canvas(self):
        node_types, node_editors = self._node_types_from_specs()

        flow = pr.ReactFlow(
            nodes=[],
            edges=[],
            node_types=node_types,
            node_editors=node_editors,
            editable=True,
            enable_connect=True,
            show_minimap=True,
            sizing_mode="stretch_both",
            min_height=600,
            stylesheets=[_FLOW_STYLESHEET],
        )

        def _on_edge_added(event):
            if self._muted:
                return
            edge = event.get("edge", event) if isinstance(event, dict) else {}
            src_id = edge.get("source", "")
            tgt_id = edge.get("target", "")
            src_handle = edge.get("sourceHandle", "")
            tgt_handle = edge.get("targetHandle", "")
            if src_id and tgt_id and src_handle and tgt_handle:
                result = self._dataflow_graph.add_edge(src_id, src_handle, tgt_id, tgt_handle)
                if result is True:
                    edge_id = edge.get("id", "")
                    if edge_id:
                        self._edge_id_map[edge_id] = (src_id, src_handle, tgt_id, tgt_handle)
                    self.dirty = True
                    self._notify("success", f"Wired: {src_handle} → {tgt_handle}", duration=3000)
                else:
                    logger.warning("Edge rejected: %s", result)
                    self._notify("error", result, duration=5000)
                    flow.remove_edge(edge.get("id", ""))

        def _on_edge_deleted(event):
            if self._muted:
                return
            edge_id = event.get("edge_id", "") if isinstance(event, dict) else ""
            if not edge_id:
                return
            mapping = self._edge_id_map.pop(edge_id, None)
            if mapping:
                self._dataflow_graph.remove_edge(*mapping)
                self.dirty = True

        def _on_node_data_changed(event):
            if self._muted:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            patch = event.get("patch", {}) if isinstance(event, dict) else {}
            if not node_id or not patch:
                return
            self._apply_config_patch(node_id, patch)

        def _on_node_deleted(event):
            if self._muted:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            if node_id:
                self._forget_node(node_id)
                self.dirty = True
                self._rebuild_sidebar()

        flow.on("edge_added", _on_edge_added)
        flow.on("edge_deleted", _on_edge_deleted)
        flow.on("node_data_changed", _on_node_data_changed)
        flow.on("node_deleted", _on_node_deleted)

        return flow

    @contextmanager
    def _muted_canvas(self):
        """Suppress the ReactFlow event handlers for the duration of the block.

        ``ReactFlow.add_edge`` and friends emit the same events the frontend
        does, so a programmatic mutation would otherwise re-enter the handler
        that is already performing it: the handler would see the edge as a
        second connection to an occupied input, reject it and remove it again.
        """
        previous, self._muted = self._muted, True
        try:
            yield
        finally:
            self._muted = previous

    def _forget_node(self, node_id: str):
        """Drop a node from the graph, the tile bookkeeping and the edge map."""
        self._dataflow_graph.remove_node(node_id)
        idx = next(
            (i for i, item in enumerate(self._tile_items) if item["instance_id"] == node_id),
            None,
        )
        if idx is not None:
            self._tile_items.pop(idx)
            self._tile_objects.pop(idx)
        for edge_id, mapping in list(self._edge_id_map.items()):
            if node_id in (mapping[0], mapping[2]):
                del self._edge_id_map[edge_id]

    # ------------------------------------------------------------------
    # Config state
    # ------------------------------------------------------------------

    def _apply_config_patch(self, node_id, patch):
        """Apply an editor patch to a node's config state and persist it."""
        config_state = self._dataflow_graph.get_config_state(node_id)
        applied = {}
        for key, value in patch.items():
            if config_state is not None and hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, value)
                except Exception as exc:
                    logger.warning("Config '%s' rejected on %s: %s", key, node_id, exc)
                    continue
            applied[key] = value
        if not applied:
            return
        for item in self._tile_items:
            if item["instance_id"] == node_id:
                item.setdefault("config", {}).update(applied)
                break
        self.dirty = True

    def _seed_config_state(self, instance_id, config):
        """Overlay saved config onto a node's config state and return node data seed."""
        config_state = self._dataflow_graph.get_config_state(instance_id)
        if config_state is None:
            return {}
        for key, value in (config or {}).items():
            if hasattr(config_state.param, key):
                try:
                    setattr(config_state, key, value)
                except Exception as exc:
                    logger.warning("Saved config '%s' rejected on %s: %s", key, instance_id, exc)
        return {name: getattr(config_state, name) for name in config_state.param if name != "name"}

    def _bind_config_to_viewer(self, instance, config_state):
        """Sync config-state params onto a Viewer instance, live."""
        for name in config_state.param:
            if name == "name" or not hasattr(instance.param, name):
                continue
            try:
                setattr(instance, name, getattr(config_state, name))
            except Exception as exc:
                logger.warning("Config '%s' could not be set: %s", name, exc)
                continue

            def _propagate(event, _name=name):
                try:
                    setattr(instance, _name, event.new)
                except Exception as exc:
                    logger.warning("Config '%s' update failed: %s", _name, exc)

            config_state.param.watch(_propagate, name)

    # ------------------------------------------------------------------
    # Component instantiation
    # ------------------------------------------------------------------

    def _instantiate_for_node(self, entry, node_state, config_state=None):
        """Create a live component view wired to the node_state."""
        app_fn = entry.load()

        if not callable(app_fn):
            return pn.panel(app_fn)

        if inspect.isclass(app_fn) and issubclass(app_fn, pn.viewable.Viewer):
            return self._instantiate_viewer_for_node(app_fn, entry, node_state, config_state)

        sig = inspect.signature(app_fn)
        kwargs = {}
        if "config" in sig.parameters:
            kwargs["config"] = node_state
        if "instance_config" in sig.parameters and config_state is not None:
            kwargs["instance_config"] = config_state
        if "context" in sig.parameters:
            kwargs["context"] = "component"

        result = app_fn(**kwargs)
        return pn.panel(result)

    def _instantiate_viewer_for_node(self, viewer_cls, entry, node_state, config_state=None):
        """Instantiate a Viewer and wire its params to the node_state."""
        spec = self._component_specs.get(entry.app_id)
        instance = viewer_cls()

        if config_state is not None:
            self._bind_config_to_viewer(instance, config_state)

        input_names = [p.name for p in spec.inputs] if spec else []
        for name in input_names:
            if not hasattr(instance.param, name):
                continue

            def _propagate_input(event, _name=name):
                setattr(instance, _name, event.new)

            node_state.param.watch(_propagate_input, name)

        output_info = instance.param.outputs()
        for name, (_, method, _) in output_info.items():
            if not hasattr(node_state.param, name):
                continue
            method_name = method.__name__ if callable(method) else method
            deps = instance.param.method_dependencies(method_name)
            dep_names = [d.name for d in deps if d.name != "name"]

            def _propagate_output(event, _method=method, _name=name):
                try:
                    val = _method() if callable(_method) else getattr(instance, _method)()
                    setattr(node_state, _name, val)
                except Exception as exc:
                    logger.error("Output '%s' failed: %s", _name, exc, exc_info=exc)

            if dep_names:
                instance.param.watch(_propagate_output, dep_names)
            try:
                val = method() if callable(method) else getattr(instance, method)()
                setattr(node_state, name, val)
            except Exception:
                pass

        return pn.panel(instance)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_component_view(self):
        self._add_button = pmui.Button(icon="add", color="primary", variant="outlined")
        self._clear_button = pmui.Button(icon="delete_sweep", color="danger", variant="outlined")
        self._save_button = pmui.Button(icon="save", color="primary", variant="outlined")
        self._add_button.on_click(lambda _event: self._on_add_clicked())
        self._clear_button.on_click(lambda _event: self.clear())
        self._save_button.on_click(lambda _event: self._on_save_clicked())

        no_components = len(self._component_entries) == 0
        self._component_picker.disabled = no_components
        self._add_button.disabled = no_components

        self._preview_switch = pmui.Switch(label="Preview", align="center", margin=(0, 10))
        self._preview_switch.link(self, value="preview", bidirectional=True)
        self._mode_toggle = pmui.RadioButtonGroup(
            options={":material/cable:": "wiring", ":material/dashboard:": "dashboard"},
            value=self.mode,
        )
        self._mode_toggle.link(self, value="mode", bidirectional=True)
        self._workspace_area = pn.Column(self._flow, sizing_mode="stretch_both", scroll="y-auto")

        self._controls_row = pn.Row(sizing_mode="stretch_width", align="center")
        self._sync_toolbar_extra()
        return pn.Column(
            self._controls_row,
            self._workspace_area,
            sizing_mode="stretch_both",
        )

    @property
    def _tile_grid(self):
        if not hasattr(self, "_tile__grid"):
            self._tile__grid = TileGrid(
                breakpoints=list(self.breakpoints),
                card=False,
                close_action="hide",
                editable=False,
                local_save=False,
                min_height=320,
                sizing_mode="stretch_both",
            )
        return self._tile__grid

    def _apply_responsive_config(self, breakpoints, responsive_layouts):
        if breakpoints:
            self._tile_grid.breakpoints = breakpoints
        if responsive_layouts:
            self._tile_grid.responsive_layouts = responsive_layouts

    @param.depends("toolbar_extra", watch=True)
    def _sync_toolbar_extra(self):
        """Re-seat caller-supplied toolbar items around the built-in controls."""
        self._controls_row[:] = [
            self._component_picker,
            self._add_button,
            self._clear_button,
            self._save_button,
            pn.layout.HSpacer(),
            *self.toolbar_extra,
            self._preview_switch,
            self._mode_toggle,
        ]

    @pn.io.hold()
    @param.depends("editable", "mode", "preview", "toolbar", watch=True)
    def _apply_mode_state(self):
        """Reconcile the toolbar and the workspace with the display params."""
        interactive = self.editable and not self.preview
        showing_grid = self.mode == "dashboard" or not self.editable
        self._controls_row.visible = self.toolbar and self.editable
        self._preview_switch.visible = self.editable and self.mode == "dashboard"
        self._tile_grid.param.update(editable=interactive, card=interactive)
        if showing_grid:
            self._workspace_area[:] = [self._tile_grid]
            self._rebuild_tile_grid()
        else:
            self._stash_tile_layout()
            self._workspace_area[:] = [self._flow]
            self._rebuild_sidebar()

    def _stash_tile_layout(self):
        """Remember the grid's layout before the grid leaves the workspace."""
        if not self._grid_populated:
            return
        self._pending_tile_layout = self._tile_grid.layout
        self._pending_breakpoints = self._tile_grid.breakpoints
        self._pending_responsive_layouts = self._tile_grid.responsive_layouts

    def _rebuild_sidebar(self):
        """Publish views of the placed components that opted into sidebar placement.

        Independent of the wiring/dashboard toggle, so it runs whenever the
        canvas changes rather than only when the tile grid is visible.
        """
        sidebar_views = []
        for i, item in enumerate(self._tile_items):
            entry = self._component_entries.get(item["component_id"])
            if entry is None or not entry.metadata.sidebar:
                continue
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = pn.pane.Markdown(f"*{entry.title}*")
            sidebar_views.append(view)
        self.sidebar = sidebar_views

    @pn.io.hold()
    def _rebuild_tile_grid(self):
        grid_views = []
        for i, item in enumerate(self._tile_items):
            entry = self._component_entries.get(item["component_id"])
            if entry is None or entry.metadata.sidebar:
                continue
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = pn.pane.Markdown(f"*{entry.title}*")
            grid_views.append(view)
        self._tile_grid[:] = grid_views
        self._grid_populated = True
        self._rebuild_sidebar()
        if self._pending_tile_layout:
            self._tile_grid.layout = self._pending_tile_layout
            self._pending_tile_layout = []
        if self._pending_breakpoints or self._pending_responsive_layouts:
            self._apply_responsive_config(
                self._pending_breakpoints, self._pending_responsive_layouts
            )
            self._pending_breakpoints = []
            self._pending_responsive_layouts = {}

    @property
    def layout(self) -> list[dict]:
        """The current tile layout, whether or not the grid is on screen."""
        if self._grid_populated:
            return self._tile_grid.layout
        return list(self._pending_tile_layout)

    # ------------------------------------------------------------------
    # Public canvas API
    # ------------------------------------------------------------------

    def add_component(
        self,
        component_id: str,
        config: dict | None = None,
        position: dict | tuple | None = None,
    ) -> str:
        """Place a component on the canvas and return its instance id.

        Parameters
        ----------
        component_id
            Id of a registered component.
        config
            Design-time configuration overrides for this instance.
        position
            Canvas position as ``{"x": ..., "y": ...}`` or ``(x, y)``. Defaults
            to the next free slot in a three-column grid.

        Returns
        -------
        str
            The new instance's id, for use with `connect` and `remove_component`.

        Raises
        ------
        KeyError
            If *component_id* is not a registered component.
        """
        self.ensure_components_loaded()
        if component_id not in self._component_specs:
            raise KeyError(
                f"Unknown component '{component_id}'. Available: {sorted(self._component_specs)}"
            )
        type_key = component_id.replace("/", "__")
        instance_id = self._place(
            component_id,
            f"{type_key}_{uuid.uuid4().hex[:6]}",
            config or {},
            position,
        )
        self.dirty = True
        self._rebuild_sidebar()
        return instance_id

    def _place(self, component_id, instance_id, config, position=None) -> str:
        """Instantiate a component and add it to the graph, canvas and tile list."""
        entry = self._component_entries[component_id]
        spec = self._component_specs[component_id]

        node_state = self._dataflow_graph.add_node(instance_id, component_id)
        config_state = self._dataflow_graph.get_config_state(instance_id)
        config_data = self._seed_config_state(instance_id, config)
        try:
            view = self._instantiate_for_node(entry, node_state, config_state)
        except Exception:
            self._dataflow_graph.remove_node(instance_id)
            raise

        if position is None:
            count = len(self._tile_items)
            position = {"x": (count % 3) * 350, "y": (count // 3) * 250}
        elif isinstance(position, tuple):
            position = {"x": position[0], "y": position[1]}

        node = pr.NodeSpec(
            id=instance_id,
            type=component_id.replace("/", "__"),
            position=position,
            label=spec.title,
            data=config_data,
        )
        node_dict = node.to_dict()
        node_dict["view"] = view
        with self._muted_canvas():
            self._flow.add_node(node_dict)

        self._tile_items.append(
            {
                "instance_id": instance_id,
                "component_id": component_id,
                "config": dict(config),
            }
        )
        self._tile_objects.append(view)
        return instance_id

    def remove_component(self, instance_id: str):
        """Remove a placed component along with its edges and its tile."""
        self._forget_node(instance_id)
        with self._muted_canvas():
            self._flow.remove_node(instance_id)
        self.dirty = True
        self._rebuild_sidebar()

    def connect(
        self, source_id: str, source_port: str, target_id: str, target_port: str
    ) -> bool | str:
        """Wire an output port to an input port.

        Returns
        -------
        bool or str
            ``True`` on success, or a message explaining the rejection (unknown
            port, type mismatch, cycle, or an input that is already connected).
        """
        result = self._dataflow_graph.add_edge(source_id, source_port, target_id, target_port)
        if result is not True:
            return result
        self._edge_count += 1
        edge_id = f"e{self._edge_count}"
        self._edge_id_map[edge_id] = (source_id, source_port, target_id, target_port)
        with self._muted_canvas():
            self._flow.add_edge(
                {
                    "id": edge_id,
                    "source": source_id,
                    "target": target_id,
                    "sourceHandle": source_port,
                    "targetHandle": target_port,
                    "markerEnd": {"type": "arrowclosed"},
                }
            )
        self.dirty = True
        return True

    def disconnect(self, source_id: str, source_port: str, target_id: str, target_port: str):
        """Remove the edge between two ports."""
        mapping = (source_id, source_port, target_id, target_port)
        self._dataflow_graph.remove_edge(*mapping)
        for edge_id, existing in list(self._edge_id_map.items()):
            if existing == mapping:
                del self._edge_id_map[edge_id]
                with self._muted_canvas():
                    self._flow.remove_edge(edge_id)
        self.dirty = True

    @pn.io.hold()
    def clear(self):
        """Remove every component and edge from the canvas."""
        had_items = bool(self._tile_items)
        self._reset_canvas()
        if had_items:
            self.dirty = True
        self._notify("info", "Cleared all component tiles.", duration=3000)

    def _reset_canvas(self):
        """Tear down all node/edge state and clear the ReactFlow canvas."""
        for node_id in list(self._dataflow_graph.node_ids):
            self._dataflow_graph.remove_node(node_id)
        self._tile_items = []
        self._tile_objects = []
        self._edge_id_map.clear()
        self._edge_count = 0
        self.sidebar = []
        with self._muted_canvas():
            self._flow.param.update(nodes=[], edges=[])
        if self._grid_populated:
            self._tile_grid[:] = []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_model(self, title: str | None = None) -> DashboardModel:
        """Serialize the current canvas into a :class:`DashboardModel`.

        The returned model is detached from the editor, so this is the seam to
        use when persisting to something other than the configured store.
        """
        current = self.dashboard
        positions = {
            node.get("id", ""): (
                node.get("position", {}).get("x", 0),
                node.get("position", {}).get("y", 0),
            )
            for node in self._flow.nodes
        }
        model = DashboardModel(
            dashboard_id=current.dashboard_id if current else uuid.uuid4().hex[:12],
            user_id=current.user_id if current else self.user,
            title=title or (current.title if current else "Untitled"),
        )
        if current is not None:
            model.version = current.version
            model.permission = current.permission
        model.items = [
            DashboardItem(
                instance_id=item["instance_id"],
                component_id=item["component_id"],
                x=positions.get(item["instance_id"], (0, 0))[0],
                y=positions.get(item["instance_id"], (0, 0))[1],
                config=item.get("config", {}),
            )
            for item in self._tile_items
        ]
        model.edges = [
            DashboardEdge(
                source=edge["source"],
                source_port=edge["source_port"],
                target=edge["target"],
                target_port=edge["target_port"],
            )
            for edge in self._dataflow_graph.edges
        ]
        # Read the layout through `layout` and the pending-config fields so that
        # saving from wiring mode, where the grid is off screen, cannot clobber a
        # layout that was loaded from storage but never rendered.
        model.tile_layout = self.layout
        if self._grid_populated:
            model.breakpoints = self._tile_grid.breakpoints
            model.responsive_layouts = self._tile_grid.responsive_layouts
        else:
            model.breakpoints = list(self._pending_breakpoints)
            model.responsive_layouts = dict(self._pending_responsive_layouts)
        return model

    @pn.io.hold()
    def load_model(self, model: DashboardModel):
        """Hydrate the canvas from a :class:`DashboardModel`.

        Components the model references but this editor does not offer are
        skipped with a warning rather than aborting the load.
        """
        self.ensure_components_loaded()
        self.dashboard = model
        with self._muted_canvas():
            self._reset_canvas()
            for item in model.items:
                if item.component_id not in self._component_specs:
                    logger.warning(
                        "Skipping unknown component '%s' (%s)",
                        item.component_id,
                        item.instance_id,
                    )
                    continue
                try:
                    self._place(
                        item.component_id,
                        item.instance_id,
                        item.config,
                        {"x": item.x, "y": item.y},
                    )
                except Exception:
                    logger.exception(
                        "Error loading component '%s' (%s)",
                        item.component_id,
                        item.instance_id,
                    )
            for edge in model.edges:
                result = self.connect(edge.source, edge.source_port, edge.target, edge.target_port)
                if result is not True:
                    logger.warning(
                        "Skipping saved edge %s.%s -> %s.%s: %s",
                        edge.source,
                        edge.source_port,
                        edge.target,
                        edge.target_port,
                        result,
                    )

        self._grid_populated = False
        self._pending_tile_layout = model.tile_layout or []
        self._pending_breakpoints = model.breakpoints or []
        self._pending_responsive_layouts = model.responsive_layouts or {}
        self.dirty = False
        self._apply_mode_state()

    def load(self, dashboard_id: str):
        """Load a dashboard from the configured store, by id or title."""
        if self.store is None:
            raise ValueError("Cannot load a dashboard without a store.")
        model = self.store.find_by_id_or_title(dashboard_id)
        if model is None:
            raise KeyError(f"Dashboard not found: {dashboard_id}")
        self.load_model(model)

    def new_dashboard(self, title: str) -> DashboardModel:
        """Start a new empty dashboard, persisting it if a store is configured."""
        if self.store is not None:
            model = self.store.create_dashboard(self.user, title)
        else:
            model = DashboardModel(
                dashboard_id=uuid.uuid4().hex[:12], user_id=self.user, title=title
            )
        self._reset_canvas()
        self._pending_tile_layout = []
        self._pending_breakpoints = []
        self._pending_responsive_layouts = {}
        self.dashboard = model
        self.dirty = False
        return model

    def save(self, title: str | None = None) -> DashboardModel:
        """Persist the current canvas and return the saved model.

        With no store configured the model is still built and returned, so the
        caller can persist it themselves.

        Raises
        ------
        RuntimeError
            If :attr:`read_only` is set.
        """
        if self.read_only:
            raise RuntimeError("This dashboard is read-only.")
        model = self.to_model(title=title)
        if self.store is not None:
            self.store.save_dashboard(model)
        self.dashboard = model
        self.dirty = False
        self.param.trigger("saved")
        return model

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------

    def _on_add_clicked(self):
        component_id = self._component_picker.value
        try:
            self.add_component(component_id)
        except KeyError:
            self._notify("warning", "Select a valid component first.", duration=3000)
        except Exception as exc:
            logger.exception("Failed to add component '%s'", component_id)
            self._notify("error", f"Failed to add component: {exc}", duration=5000)
        else:
            entry = self._component_entries[component_id]
            self._notify("success", f"Added component: {entry.title}", duration=3000)

    def _on_save_clicked(self):
        try:
            model = self.save()
        except RuntimeError:
            self._notify("error", "This dashboard is read-only.", duration=4000)
        except Exception as exc:
            logger.exception("Failed to save dashboard")
            self._notify("error", f"Save failed: {exc}", duration=5000)
        else:
            self._notify("success", f'Dashboard "{model.title}" saved.', duration=3000)

    def __panel__(self):
        """Render the editor."""
        return self._view
