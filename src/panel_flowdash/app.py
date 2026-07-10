"""Application builder: scans a project directory and constructs the Panel app."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pathlib
import traceback
import typing as t
import uuid
import warnings
from functools import cache, partial
from html import escape
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
from panel.viewable import Viewer
from panel_tiles import TileGrid

from panel_flowdash.component_spec import build_component_specs
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
)
from panel_flowdash.dataflow_engine import DataflowGraph
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry
from panel_flowdash.session_state import build_session_state_class, check_requirements

pn.extension("tabulator", "vega", "deckgl", notifications=True)

logger = logging.getLogger("panel_flowdash")

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    class _DASHBOARD_ACTION_TYPE(t.TypedDict):
        label: str
        icon: str


def build_registry(project_dir: Path) -> dict[str, RegistryEntry]:
    """Scan a project directory for page/component modules.

    Expects a structure like:
        project_dir/
            SectionA/
                page1.py   (exports `app`)
                page2.py
            SectionB/
                widget.py

    Each .py file must export an `app` object decorated with @register.
    """
    registry: dict[str, RegistryEntry] = {}

    for section_dir in sorted(project_dir.glob("*")):
        if not section_dir.is_dir() or section_dir.name.startswith(("_", ".")):
            continue
        section = section_dir.name
        for module_path in sorted(section_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue

            module_name = ".".join(module_path.relative_to(project_dir).with_suffix("").parts)
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                warnings.warn(
                    f"Failed to import '{module_name}': {exc}",
                    stacklevel=2,
                )
                continue

            if not hasattr(module, "app"):
                warnings.warn(f"Module '{module_name}' has no 'app' export.", stacklevel=2)
                continue

            app = module.app
            metadata = PanelAppMetadata.from_app(app)
            if not metadata.page and not metadata.component:
                warnings.warn(
                    f"Module '{module_name}' ignored: page=False and component=False.",
                    stacklevel=2,
                )
                continue

            app_id = f"{section}/{module_path.stem}"
            registry[app_id] = RegistryEntry(
                app_id=app_id,
                section=section,
                name=module_path.stem,
                page_path=f"/{app_id}",
                module_name=module_name,
                app=app,
                metadata=metadata,
            )

    return registry


COMPONENTS_ROUTE = "/components"
DASH_ROUTE_PREFIX = "/dash/"

_LAUNCHER_CARD_CSS = """
:host {
  cursor: pointer;
  transition: box-shadow 0.2s;
}
:host(:hover) {
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
:host .MuiCardContent-root {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
}
"""

_LAUNCHER_DASH_CARD_CSS = """
:host {
  cursor: pointer;
  transition: box-shadow 0.2s;
  overflow: visible;
}
:host(:hover) {
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
:host .MuiCardContent-root {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
}
"""

_COMPONENT_PALETTE_CARD_CSS = """
:host {
  cursor: pointer;
  border-radius: 6px;
  transition: background-color 0.15s;
}
:host(:hover) {
  background-color: rgba(0, 114, 181, 0.08);
}
"""

_LAUNCHER_SPEED_DIAL_CSS = """
:host {
  position: absolute;
  top: 12px;
  right: 0px;
  z-index: 100;
}
:host .MuiSpeedDial-fab {
  width: 28px;
  height: 28px;
  min-height: unset;
  box-shadow: none;
}
"""


class FlowDashApp(Viewer):
    """FlowDash application: scans a project directory and serves its pages and components."""

    breakpoints = param.List(default=[768, 1200], doc="Responsive breakpoints for the tile grid.")

    project_dir = param.Path(doc="Path to the project directory.")

    store = param.ClassSelector(
        class_=DashboardStore, doc="DashboardStore instance for persistence."
    )

    title = param.String(default="FlowDash", doc="Application title shown in the browser tab.")

    _main_task: asyncio.Task | None = None

    def __init__(self, registry: dict[str, RegistryEntry] | None = None, **params):
        super().__init__(**params)

        if not registry:
            registry = build_registry(pathlib.Path(self.project_dir))
        page_entries = {k: v for k, v in registry.items() if v.metadata.page}
        component_entries = {k: v for k, v in registry.items() if v.metadata.component}
        component_specs = build_component_specs(registry)
        session_state_class = build_session_state_class(registry)
        self._registry = registry
        self._page_entries = page_entries
        self._component_entries = component_entries
        self._component_specs = component_specs
        self._session_state_class = session_state_class

        self._session_state = self._session_state_class()
        self._user_id = self._resolve_user_id()
        self._loading = False
        self._dirty = False
        self._edge_id_map: dict[str, tuple[str, str, str, str]] = {}
        self._current_dashboard: DashboardModel | None = None
        self._tile_items: list[dict] = []
        self._tile_objects: list[Viewable] = []
        self._sidebar_views: list[Viewable] = []
        self._sidebar_container = pn.Column(sizing_mode="stretch_width")
        self._component_picker = self._make_component_picker()
        self._dataflow_graph = DataflowGraph(
            self._component_specs,
            on_error=self._on_wiring_error,
        )
        self._flow_canvas = self._build_flow_canvas()
        self._component_view = self._build_component_view()
        self._nav_menu = self._build_nav_menu()
        self._contextbar = pn.Column(
            pmui.Typography(
                "Navigation",
                variant="overline",
                margin=(8, 16, 0, 16),
                styles={"opacity": "0.6", "letter-spacing": "0.08em"},
            ),
            pmui.Divider(margin=(4, 0, 4, 0)),
            self._nav_menu,
            sizing_mode="stretch_width",
        )
        self._build_dialog()
        self._build_unsaved_dialog()
        self._page = pmui.Page(
            title=self.title,
            theme_config={"palette": {"primary": {"main": "#0072B5"}}},
            sidebar_open=False,
            sidebar=[self._sidebar_container],
            contextbar=[self._contextbar],
        )
        pn.state.onload(self._load_page_layout)

    def _resolve_user_id(self) -> str:
        if pn.state.user:
            return pn.state.user
        return "default"

    @staticmethod
    @cache
    def _accepted_injected_params(app):
        if inspect.isclass(app) and issubclass(app, pn.viewable.Viewer):
            return {
                p for p in ("config", "executor", "instance_config", "context") if hasattr(app, p)
            }
        return inspect.signature(app).parameters.keys() & {
            "config",
            "executor",
            "instance_config",
            "context",
        }

    def _add_kwargs_dict(self, app, *, context: str, instance_config: dict | None = None):
        params = self._accepted_injected_params(app)
        kwargs = {}
        if "context" in params:
            kwargs["context"] = context
        if "instance_config" in params and instance_config is not None:
            kwargs["instance_config"] = instance_config
        if "config" in params:
            kwargs["config"] = self._session_state
        return kwargs

    def _entry_from_key(self, key):
        app_id = "/".join(key)
        return self._page_entries.get(app_id)

    async def _instantiate_entry(
        self,
        entry: RegistryEntry,
        *,
        context: str,
        instance_config: dict | None = None,
    ):
        unsatisfied = check_requirements(self._session_state, entry.metadata.requires)
        blocking = [u for u in unsatisfied if u["blocking"]]
        if blocking:
            keys = ", ".join(u["key"] for u in blocking)
            return pn.pane.Alert(
                f"**{entry.title}** is waiting for: `{keys}`",
                alert_type="warning",
            )

        app = entry.app
        if not callable(app):
            return pn.panel(app)
        kwargs = self._add_kwargs_dict(app, context=context, instance_config=instance_config)
        if inspect.iscoroutinefunction(app):
            return await app(**kwargs)
        return await asyncio.to_thread(lambda: pn.panel(app(**kwargs)))

    async def _render_page(self, key):
        entry = self._entry_from_key(key)
        if entry is None:
            return f"Unknown page: {'/'.join(key)}"

        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()

        try:
            coroutine = self._instantiate_entry(entry, context="page")
            self._main_task = asyncio.create_task(coroutine)
            return await self._main_task
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.exception("Error rendering page '%s'", "/".join(key))
            err_name = type(e).__name__
            return pn.pane.Alert(
                f"**{err_name}**: {e}\n<hr>\n<pre> {escape(traceback.format_exc())}</pre>\n",
                alert_type="danger",
                styles={"color": "black"},
            )

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

    def _build_flow_canvas(self):
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
            stylesheets=[
                """\
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
            ],
        )

        def _on_edge_added(event):
            if self._loading:
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
                    self._dirty = True
                    pn.state.notifications.success(
                        f"Wired: {src_handle} → {tgt_handle}", duration=3000
                    )
                else:
                    logger.warning("Edge rejected: %s", result)
                    pn.state.notifications.error(result, duration=5000)
                    flow.remove_edge(edge.get("id", ""))

        def _on_edge_deleted(event):
            if self._loading:
                return
            edge_id = event.get("edge_id", "") if isinstance(event, dict) else ""
            if not edge_id:
                return
            mapping = self._edge_id_map.pop(edge_id, None)
            if mapping:
                self._dataflow_graph.remove_edge(*mapping)
                self._dirty = True

        def _on_node_data_changed(event):
            if self._loading:
                return
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            patch = event.get("patch", {}) if isinstance(event, dict) else {}
            if not node_id or not patch:
                return
            self._apply_config_patch(node_id, patch)

        def _on_node_deleted(event):
            node_id = event.get("node_id", "") if isinstance(event, dict) else ""
            if node_id:
                self._dataflow_graph.remove_node(node_id)
                idx = next(
                    (
                        i
                        for i, item in enumerate(self._tile_items)
                        if item["instance_id"] == node_id
                    ),
                    None,
                )
                if idx is not None:
                    self._tile_items.pop(idx)
                    self._tile_objects.pop(idx)
                self._dirty = True

        flow.on("edge_added", _on_edge_added)
        flow.on("edge_deleted", _on_edge_deleted)
        flow.on("node_data_changed", _on_node_data_changed)
        flow.on("node_deleted", _on_node_deleted)

        self._flow = flow
        return flow

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
        self._dirty = True

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

    def _instantiate_for_node(self, entry, node_state, config_state=None):
        """Create a live component view wired to the node_state."""
        app_fn = entry.app

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

    def _build_component_view(self):
        self._add_button = pmui.Button(icon="add", color="primary", variant="outlined")
        self._clear_button = pmui.Button(icon="delete_sweep", color="danger", variant="outlined")
        self._save_button = pmui.Button(icon="save", color="primary", variant="outlined")
        self._add_button.on_click(lambda _event: self._add_component_to_graph())
        self._clear_button.on_click(lambda _event: self._clear_components())
        self._save_button.on_click(lambda _event: self._save_current_dashboard())

        no_components = len(self._component_entries) == 0
        self._component_picker.disabled = no_components
        self._add_button.disabled = no_components

        self._preview_switch = pmui.Switch(
            label="Preview",
            value=False,
            align="center",
            margin=(0, 10),
        )
        self._preview_switch.param.watch(
            lambda e: self._tile_grid.param.update(editable=not e.new, card=not e.new), "value"
        )
        self._mode_toggle = pmui.RadioButtonGroup(
            options={":material/cable:": "wiring", ":material/dashboard:": "dashboard"},
            value="wiring",
        )
        self._workspace_area = pn.Column(
            self._flow_canvas, sizing_mode="stretch_both", scroll="y-auto"
        )

        self._preview_switch.visible = False

        @pn.io.hold()
        def _on_mode_change(event):
            if event.new == "dashboard":
                self._workspace_area[:] = [self._tile_grid]
                self._rebuild_tile_grid()
                self._preview_switch.visible = True
            else:
                self._pending_tile_layout = self._tile_grid.layout
                self._pending_breakpoints = self._tile_grid.breakpoints
                self._pending_responsive_layouts = self._tile_grid.responsive_layouts
                self._workspace_area[:] = [self._flow_canvas]
                self._preview_switch.visible = False
                self._preview_switch.value = False

        self._mode_toggle.param.watch(_on_mode_change, "value")

        self._controls_row = pn.Row(
            self._component_picker,
            self._add_button,
            self._clear_button,
            self._save_button,
            pn.layout.HSpacer(),
            self._preview_switch,
            self._mode_toggle,
            sizing_mode="stretch_width",
            align="center",
        )
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
        pn.state.notifications.error(
            f"Runtime wiring error ({source_port} → {target_port}): {exc}",
            duration=5000,
        )

    def _add_component_to_graph(self):
        component_id = self._component_picker.value
        entry = self._component_entries.get(component_id)
        if entry is None:
            pn.state.notifications.warning("Select a valid component first.", duration=3000)
            return

        spec = self._component_specs.get(component_id)
        if spec is None:
            return

        type_key = component_id.replace("/", "__")
        instance_id = f"{type_key}_{uuid.uuid4().hex[:6]}"

        node_state = self._dataflow_graph.add_node(instance_id, component_id)
        config_state = self._dataflow_graph.get_config_state(instance_id)
        config_data = self._seed_config_state(instance_id, {})

        try:
            view = self._instantiate_for_node(entry, node_state, config_state)
        except Exception as e:
            logger.exception("Failed to add component '%s'", component_id)
            self._dataflow_graph.remove_node(instance_id)
            pn.state.notifications.error(f"Failed to add component: {e}", duration=5000)
            return

        node_count = len(self._tile_items)
        col = node_count % 3
        row = node_count // 3
        position = {"x": col * 350, "y": row * 250}

        node = pr.NodeSpec(
            id=instance_id,
            type=type_key,
            position=position,
            label=spec.title,
            data=config_data,
        )
        node_dict = node.to_dict()
        node_dict["view"] = view
        self._flow.add_node(node_dict)

        self._tile_items.append(
            {"instance_id": instance_id, "component_id": component_id, "config": {}}
        )
        self._tile_objects.append(view)
        self._dirty = True

        pn.state.notifications.success(f"Added component: {entry.title}", duration=3000)

    @pn.io.hold()
    def _rebuild_tile_grid(self):
        grid_views = []
        sidebar_views = []
        for i, item in enumerate(self._tile_items):
            component_id = item["component_id"]
            entry = self._component_entries.get(component_id)
            if entry is None:
                continue
            view = self._tile_objects[i] if i < len(self._tile_objects) else None
            if view is None:
                view = pn.pane.Markdown(f"*{entry.title}*")
            if entry.metadata.sidebar:
                sidebar_views.append(view)
            else:
                grid_views.append(view)
        self._tile_grid[:] = grid_views
        self._sidebar_views = sidebar_views
        self._sidebar_container.objects = sidebar_views
        self._page.sidebar_open = bool(sidebar_views)
        pending = getattr(self, "_pending_tile_layout", [])
        if pending:
            self._tile_grid.layout = pending
            self._pending_tile_layout = []
        pending_bp = getattr(self, "_pending_breakpoints", [])
        pending_rl = getattr(self, "_pending_responsive_layouts", {})
        if pending_bp or pending_rl:
            self._apply_responsive_config(pending_bp, pending_rl)
            self._pending_breakpoints = []
            self._pending_responsive_layouts = {}

    @pn.io.hold()
    def _clear_components(self):
        had_items = bool(self._tile_items)
        self._reset_canvas()
        if had_items:
            self._dirty = True
        pn.state.notifications.info("Cleared all component tiles.", duration=3000)

    def _save_current_dashboard(self):
        if self._current_dashboard is None:
            pn.state.notifications.warning(
                "No dashboard loaded. Create one from the sidebar.", duration=4000
            )
            return

        positions = {}
        for node in self._flow.nodes:
            node_id = node.get("id", "")
            pos = node.get("position", {})
            positions[node_id] = (pos.get("x", 0), pos.get("y", 0))

        self._current_dashboard.items = [
            DashboardItem(
                instance_id=item["instance_id"],
                component_id=item["component_id"],
                x=positions.get(item["instance_id"], (0, 0))[0],
                y=positions.get(item["instance_id"], (0, 0))[1],
                config=item.get("config", {}),
            )
            for item in self._tile_items
        ]
        self._current_dashboard.edges = [
            DashboardEdge(
                source=edge["source"],
                source_port=edge["source_port"],
                target=edge["target"],
                target_port=edge["target_port"],
            )
            for edge in self._dataflow_graph.edges
        ]
        self._current_dashboard.tile_layout = self._tile_grid.layout
        self._current_dashboard.breakpoints = self._tile_grid.breakpoints
        self._current_dashboard.responsive_layouts = self._tile_grid.responsive_layouts

        try:
            self.store.save_dashboard(self._current_dashboard)
        except Exception as exc:
            logger.exception("Failed to save dashboard")
            pn.state.notifications.error(f"Save failed: {exc}", duration=5000)
            return
        self._dirty = False
        pn.state.notifications.success(
            f'Dashboard "{self._current_dashboard.title}" saved.', duration=3000
        )

    def _reset_canvas(self):
        """Tear down all node/edge state and clear the ReactFlow canvas."""
        for node_id in list(self._dataflow_graph.node_ids):
            self._dataflow_graph.remove_node(node_id)
        self._tile_items = []
        self._tile_objects = []
        self._edge_id_map.clear()
        self._sidebar_views = []
        self._sidebar_container.objects = []
        self._flow.nodes = []
        self._flow.edges = []

    async def _load_dashboard(self, dashboard_id: str, edit: bool = False):
        with pn.io.hold():
            self._load_dashboard_sync(dashboard_id, edit=edit)

    def _load_dashboard_sync(self, dashboard_id: str, edit: bool = False):
        dashboard = self.store.load_dashboard(self._user_id, dashboard_id)
        if dashboard is None:
            self._page.main = [
                pn.pane.Alert(f"Dashboard not found: {dashboard_id}", alert_type="danger"),
                self._dialog,
                self._unsaved_dialog,
            ]
            return
        self._current_dashboard = dashboard
        self._loading = True

        self._reset_canvas()

        for item in dashboard.items:
            component_id = item.component_id
            entry = self._component_entries.get(component_id)
            if entry is None:
                continue
            spec = self._component_specs.get(component_id)
            if spec is None:
                continue

            instance_id = item.instance_id
            type_key = component_id.replace("/", "__")
            node_state = self._dataflow_graph.add_node(instance_id, component_id)
            config_state = self._dataflow_graph.get_config_state(instance_id)
            config_data = self._seed_config_state(instance_id, item.config)

            try:
                view = self._instantiate_for_node(entry, node_state, config_state)
            except Exception:
                logger.exception("Error loading component '%s' (%s)", component_id, instance_id)
                self._dataflow_graph.remove_node(instance_id)
                continue

            position = {"x": item.x, "y": item.y}
            node = pr.NodeSpec(
                id=instance_id,
                type=type_key,
                position=position,
                label=spec.title,
                data=config_data,
            )
            node_dict = node.to_dict()
            node_dict["view"] = view
            self._flow.add_node(node_dict)

            self._tile_items.append(item.to_dict())
            self._tile_objects.append(view)

        edge_counter = 0
        for edge in dashboard.edges:
            success = self._dataflow_graph.add_edge(
                edge.source, edge.source_port, edge.target, edge.target_port
            )
            if success is True:
                edge_counter += 1
                edge_id = f"e{edge_counter}"
                self._edge_id_map[edge_id] = (
                    edge.source,
                    edge.source_port,
                    edge.target,
                    edge.target_port,
                )
                self._flow.add_edge(
                    {
                        "id": edge_id,
                        "source": edge.source,
                        "target": edge.target,
                        "sourceHandle": edge.source_port,
                        "targetHandle": edge.target_port,
                        "markerEnd": {"type": "arrowclosed"},
                    }
                )

        self._loading = False
        self._dirty = False
        self._pending_tile_layout = dashboard.tile_layout or []
        self._pending_breakpoints = dashboard.breakpoints or []
        self._pending_responsive_layouts = dashboard.responsive_layouts or {}

        pn.state.notifications.info(
            f'Loaded dashboard "{dashboard.title}" with {len(self._tile_items)} tiles.',
            duration=3000,
        )
        if edit:
            self._show_edit_mode()
        else:
            self._show_view_mode()
        self._sync_menu_active(f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        self._page.main = [self._component_view, self._dialog, self._unsaved_dialog]

    def _create_new_dashboard(self, title_str: str):
        title_str = title_str.strip()
        if not title_str:
            pn.state.notifications.warning("Dashboard title cannot be empty.", duration=3000)
            return
        dashboard = self.store.create_dashboard(self._user_id, title_str)
        self._current_dashboard = dashboard
        self._reset_canvas()
        self._dirty = False

        pn.state.notifications.success(
            f'Created new dashboard "{dashboard.title}".', duration=3000
        )
        self._refresh_sidebar_dashboards()
        if pn.state.location:
            pn.state.location.param.update(
                pathname=f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}",
                search="?edit=true",
            )
        self._show_edit_mode()
        self._sync_menu_active(f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}")
        self._page.main = [self._component_view, self._dialog, self._unsaved_dialog]

    def _delete_dashboard(self, dashboard_id: str):
        was_current = bool(
            self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id
        )
        self.store.delete_dashboard(self._user_id, dashboard_id)
        if was_current:
            self._current_dashboard = None
            self._reset_canvas()
            self._dirty = False

        self._refresh_sidebar_dashboards()
        pn.state.notifications.info("Dashboard deleted.", duration=3000)

        if was_current:
            self._navigate_to("/")
        elif pn.state.location is not None and pn.state.location.pathname == "/":
            self._page.main = [
                self._build_launcher(),
                self._dialog,
                self._unsaved_dialog,
            ]

    def _rename_dashboard(self, dashboard_id: str, new_title: str):
        new_title = new_title.strip()
        if not new_title:
            return
        self.store.rename_dashboard(self._user_id, dashboard_id, new_title)
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.title = new_title
        self._refresh_sidebar_dashboards()

    def _refresh_sidebar_dashboards(self):
        dash_items = self._get_dashboard_menu_items()
        items = list(self._menu_list.items)
        items[-1] = {**items[-1], "items": dash_items}
        self._menu_list.items = items

    def _build_launcher(self):
        sections: dict[str, list[RegistryEntry]] = {}
        for entry in self._page_entries.values():
            sections.setdefault(entry.section, []).append(entry)

        accordion_items = []
        component_item = None

        for section, entries in sorted(sections.items()):
            cards = []
            for entry in sorted(entries, key=lambda e: e.name):
                icon_name = entry.metadata.icon or "article"
                card = pmui.Card(
                    pmui.ButtonIcon(
                        icon=icon_name,
                        icon_size="3em",
                        disabled=True,
                        stylesheets=[":host { pointer-events: none; opacity: 1; }"],
                    ),
                    title=entry.title,
                    title_variant="h4",
                    collapsible=False,
                    stylesheets=[_LAUNCHER_CARD_CSS],
                    width=200,
                    height=140,
                )
                clickable = pmui.Clickable(object=card)
                clickable.on_click(partial(self._launcher_navigate, entry.page_path))
                cards.append(clickable)

            section_label = section.replace("_", " ")
            content = pn.FlexBox(*cards, gap="12px", margin=(0, 0, 12, 0))
            if section_label.lower() == "components":
                component_item = (section_label, content)
            else:
                accordion_items.append((section_label, content))

        dashboards = self.store.list_dashboards(self._user_id)
        if dashboards:
            dash_cards = []
            for d in dashboards:
                speed_dial = pmui.SpeedDial(
                    items=[
                        {"label": "Edit", "icon": "edit"},
                        {"label": "Rename", "icon": "drive_file_rename_outline"},
                        {"label": "Delete", "icon": "delete"},
                    ],
                    icon="more_vert",
                    direction="down",
                    color="default",
                    size="small",
                    stylesheets=[_LAUNCHER_SPEED_DIAL_CSS],
                )
                speed_dial.param.watch(
                    partial(self._on_launcher_dash_action, d.dashboard_id, d.title), "value"
                )

                card = pmui.Card(
                    pmui.ButtonIcon(
                        icon="dashboard",
                        icon_size="3em",
                        disabled=True,
                        stylesheets=[":host { pointer-events: none; opacity: 1; }"],
                    ),
                    title=d.title,
                    collapsible=False,
                    stylesheets=[_LAUNCHER_CARD_CSS],
                    title_variant="h4",
                    width=200,
                    height=140,
                )
                path = f"{DASH_ROUTE_PREFIX}{d.dashboard_id}"
                clickable = pmui.Clickable(object=card)
                clickable.on_click(partial(self._launcher_navigate, path))
                wrapper = pn.Column(
                    clickable,
                    speed_dial,
                    styles={"position": "relative", "overflow": "visible"},
                    sizing_mode="fixed",
                    width=200,
                    height=140,
                )
                dash_cards.append(wrapper)
            accordion_items.append(
                ("Custom Apps", pn.FlexBox(*dash_cards, gap="12px", margin=(0, 0, 12, 0)))
            )

        if component_item:
            accordion_items.append(component_item)

        active = list(range(len(accordion_items)))
        if component_item:
            active.remove(len(accordion_items) - 1)

        return pmui.Accordion(
            *accordion_items,
            active=active,
            toggle=False,
            sizing_mode="stretch_both",
            margin=20,
        )

    def _launcher_navigate(self, path, *_args):
        self._request_navigation(path)

    def _on_launcher_dash_action(self, dashboard_id, title, event):
        value = event.new if hasattr(event, "new") else event
        label = value.get("label") if isinstance(value, dict) else value
        if label == "Edit":
            if pn.state.location:
                pn.state.location.param.update(
                    pathname=f"{DASH_ROUTE_PREFIX}{dashboard_id}",
                    search="?edit=true",
                )
            pn.state.execute(partial(self._load_dashboard_edit, dashboard_id))
        elif label == "Rename":
            self._dialog_name_input.param.update(
                value=title, disabled=False, error_state=False, helper_text=""
            )
            self._dialog_context = {"action": "rename", "dashboard_id": dashboard_id}
            self._dialog.title = "Rename Dashboard"
            self._dialog.open = True
        elif label == "Delete":
            self._dialog_name_input.param.update(value=title, disabled=True)
            self._dialog_context = {"action": "delete", "dashboard_id": dashboard_id}
            self._dialog.title = "Delete Dashboard"
            self._dialog.open = True

    async def _load_page_layout(self):
        if pn.state.location is None:
            return
        pathname = pn.state.location.pathname

        if pathname == "/":
            self._current_dashboard = None
            self._sidebar_container.objects = []
            self._page.sidebar_open = False
            self._page.main = [self._build_launcher(), self._dialog, self._unsaved_dialog]
            return

        if pathname == COMPONENTS_ROUTE:
            self._current_dashboard = None
            self._sidebar_container.objects = []
            self._show_edit_mode()
            self._page.main = [self._component_view, self._dialog, self._unsaved_dialog]
            return

        if pathname.startswith(DASH_ROUTE_PREFIX):
            dashboard_id = pathname[len(DASH_ROUTE_PREFIX) :].strip("/")
            if dashboard_id:
                search = pn.state.location.search or ""
                edit_requested = "edit=true" in search
                await self._load_dashboard(dashboard_id, edit=edit_requested)
                return

        self._current_dashboard = None

        self._sidebar_container.objects = []
        key = tuple(pathname.strip("/").split("/"))
        if len(key) == 2 and self._entry_from_key(key):
            self._page.main = [await self._render_page(key), self._dialog, self._unsaved_dialog]
        else:
            self._page.main = [f"Invalid URL: {pathname}", self._dialog, self._unsaved_dialog]

    @pn.io.hold()
    def _show_edit_mode(self):
        self._controls_row.visible = True
        self._tile_grid.param.update(editable=True, card=True)
        if self._mode_toggle.value == "wiring":
            self._workspace_area[:] = [self._flow_canvas]
        else:
            self._workspace_area[:] = [self._tile_grid]
            self._rebuild_tile_grid()
        if pn.state.location is not None:
            pn.state.location.param.update(search="?edit=true")

    @pn.io.hold()
    def _show_view_mode(self):
        self._controls_row.visible = False
        self._tile_grid.param.update(card=False, editable=False)
        self._workspace_area[:] = [self._tile_grid]
        self._rebuild_tile_grid()
        if pn.state.location is not None:
            pn.state.location.param.update(search="")

    _DASHBOARD_ACTIONS = (
        {"label": "Edit", "icon": "edit"},
        {"label": "Rename", "icon": "drive_file_rename_outline"},
        {"label": "Delete", "icon": "delete"},
    )

    def _get_dashboard_menu_items(self) -> list[dict]:
        items = []
        dashboards = self.store.list_dashboards(self._user_id)
        for d in dashboards:
            items.append(
                {
                    "icon": "dashboard",
                    "label": d.title,
                    "path": f"{DASH_ROUTE_PREFIX}{d.dashboard_id}",
                    "disable_link": True,
                    "actions": self._DASHBOARD_ACTIONS,
                }
            )
        items.append(
            {
                "icon": "add",
                "label": "New Dashboard",
                "path": "__new_dashboard__",
                "disable_link": True,
                "actions": [{"label": "Create", "icon": "add", "inline": True}],
            }
        )
        return items

    def _dashboard_id_from_path(self, path: str) -> str | None:
        if path and path.startswith(DASH_ROUTE_PREFIX):
            return path[len(DASH_ROUTE_PREFIX) :].strip("/")
        return None

    def _on_action_edit(self, item):
        self._page.contextbar_open = False
        path = item.get("path", "")
        dashboard_id = self._dashboard_id_from_path(path)
        if not dashboard_id:
            return
        target_path = f"{DASH_ROUTE_PREFIX}{dashboard_id}"
        if self._dirty and self._current_dashboard is not None:
            self._pending_navigation = target_path
            self._unsaved_dialog.open = True
        else:
            pn.state.execute(partial(self._load_dashboard_edit, dashboard_id))

    async def _load_dashboard_edit(self, dashboard_id: str):
        await self._load_dashboard(dashboard_id, edit=True)

    @pn.io.hold()
    def _on_action_rename(self, item):
        path = item.get("path", "")
        dashboard_id = self._dashboard_id_from_path(path)
        if not dashboard_id:
            return
        self._dialog_name_input.param.update(
            value=item.get("label", ""), disabled=False, error_state=False, helper_text=""
        )
        self._dialog_context = {"action": "rename", "dashboard_id": dashboard_id}
        self._dialog.title = "Rename Dashboard"
        self._page.contextbar_open = False
        self._dialog.open = True

    @pn.io.hold()
    def _on_action_delete(self, item):
        path = item.get("path", "")
        dashboard_id = self._dashboard_id_from_path(path)
        if not dashboard_id:
            return
        self._dialog_name_input.param.update(value=item.get("label", ""), disabled=True)
        self._dialog_context = {"action": "delete", "dashboard_id": dashboard_id}
        self._dialog.title = "Delete Dashboard"
        self._page.contextbar_open = False
        self._dialog.open = True

    @pn.io.hold()
    def _on_action_create(self, item):
        self._open_create_dialog()

    @pn.io.hold()
    def _open_create_dialog(self):
        self._dialog_name_input.param.update(
            value="", disabled=False, error_state=False, helper_text=""
        )
        self._dialog_context = {"action": "create"}
        self._dialog.title = "Create Dashboard"
        self._dialog.open = True
        self._page.contextbar_open = False

    def _validate_dashboard_name(self, title: str) -> str | None:
        """Return an error message if the title is invalid, else None."""
        title = title.strip()
        if not title:
            return "Name cannot be empty."
        exclude_id = self._dialog_context.get("dashboard_id")
        if self.store.title_exists(self._user_id, title, exclude_id=exclude_id):
            return "A dashboard with this name already exists."
        return None

    def _on_dialog_name_changed(self, event):
        error = self._validate_dashboard_name(event.new)
        self._dialog_name_input.error_state = error is not None
        self._dialog_name_input.helper_text = error or ""

    @pn.io.hold()
    def _on_dialog_confirm(self, _event):
        ctx = self._dialog_context
        if not ctx:
            return
        action = ctx.get("action")
        if action in ("create", "rename"):
            error = self._validate_dashboard_name(self._dialog_name_input.value)
            if error:
                self._dialog_name_input.error_state = True
                self._dialog_name_input.helper_text = error
                return
        self._dialog.open = False
        if action == "create":
            t = self._dialog_name_input.value
            if t:
                self._create_new_dashboard(t)
        elif action == "rename":
            new_t = self._dialog_name_input.value
            did = ctx.get("dashboard_id", "")
            if new_t and did:
                self._rename_dashboard(did, new_t)
        elif action == "delete":
            did = ctx.get("dashboard_id", "")
            if did:
                self._delete_dashboard(did)
        self._dialog_name_input.param.update(disabled=False, error_state=False, helper_text="")
        self._dialog_context = {}

    def _build_dialog(self):
        self._dialog_name_input = pmui.TextInput(
            label="Name",
            sizing_mode="stretch_width",
        )
        self._dialog_name_input.param.watch(self._on_dialog_name_changed, "value_input")
        confirm_btn = pmui.Button(label="Confirm", color="primary")
        cancel_btn = pmui.Button(label="Cancel", color="light")
        confirm_btn.on_click(self._on_dialog_confirm)
        cancel_btn.on_click(lambda _: setattr(self._dialog, "open", False))
        self._dialog_context: dict = {}
        self._dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    self._dialog_name_input,
                    pn.Row(confirm_btn, cancel_btn),
                    sizing_mode="stretch_width",
                )
            ],
            title="Dashboard",
            open=False,
            min_width=350,
        )

    def _build_unsaved_dialog(self):
        self._pending_navigation: str | None = None

        discard_btn = pmui.Button(label="Discard", color="danger", variant="outlined")
        save_btn = pmui.Button(label="Save & Continue", color="primary")
        stay_btn = pmui.Button(label="Cancel", color="light")

        def _on_discard(_event):
            self._unsaved_dialog.open = False
            self._dirty = False
            path = self._pending_navigation
            self._pending_navigation = None
            if path:
                self._navigate_to(path)

        def _on_save(_event):
            self._unsaved_dialog.open = False
            self._save_current_dashboard()
            path = self._pending_navigation
            self._pending_navigation = None
            if path:
                self._navigate_to(path)

        def _on_stay(_event):
            self._unsaved_dialog.open = False
            self._pending_navigation = None

        discard_btn.on_click(_on_discard)
        save_btn.on_click(_on_save)
        stay_btn.on_click(_on_stay)

        self._unsaved_dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    pn.pane.Markdown("You have unsaved changes. What would you like to do?"),
                    pn.Row(save_btn, discard_btn, stay_btn),
                    sizing_mode="stretch_width",
                )
            ],
            title="Unsaved Changes",
            open=False,
            min_width=400,
        )
        return self._unsaved_dialog

    def _navigate_to(self, path: str):
        if pn.state.location is None:
            return
        pn.state.location.param.update(pathname=path, search="")
        self._sync_menu_active(path)
        pn.state.execute(self._load_page_layout)

    def _sync_menu_active(self, path: str):
        items = self._menu_list.items
        for si, section in enumerate(items):
            if section.get("path") == path:
                self._menu_list.active = (si,)
                return
            for pi, item in enumerate(section.get("items", [])):
                if item.get("path") == path:
                    self._menu_list.active = (si, pi)
                    return
        self._menu_list.active = None

    def _request_navigation(self, path: str):
        if self._dirty and self._current_dashboard is not None:
            self._pending_navigation = path
            self._unsaved_dialog.open = True
        else:
            self._navigate_to(path)

    _SECTION_ICONS: t.ClassVar[dict[str, str]] = {
        "components": "widgets",
        "pages": "description",
    }

    def _section_icon(self, section: str) -> str:
        """Pick a menu icon for a page section, keyed by its (normalized) name."""
        return self._SECTION_ICONS.get(section.replace("_", " ").lower(), "folder")

    def _build_nav_menu(self):
        sections: dict[str, list[RegistryEntry]] = {}
        for entry in self._page_entries.values():
            sections.setdefault(entry.section, []).append(entry)

        menu_items = [
            {
                "label": "Home",
                "icon": "home",
                "path": "/",
                "disable_link": True,
            },
        ]
        for section, section_apps in sorted(sections.items()):
            menu_items.append(
                {
                    "label": section.replace("_", " "),
                    "selectable": False,
                    "icon": self._section_icon(section),
                    "items": [
                        {
                            "icon": None,
                            "label": page_entry.title,
                            "path": page_entry.page_path,
                            "href": page_entry.page_path,
                            "disable_link": True,
                        }
                        for page_entry in sorted(section_apps, key=lambda e: e.name)
                    ],
                }
            )
        menu_items.append(
            {
                "label": "Custom Apps",
                "selectable": False,
                "icon": "dashboard_customize",
                "items": self._get_dashboard_menu_items(),
            }
        )

        current_path = pn.state.location.pathname if pn.state.location is not None else ""
        pathname = "/" + current_path.strip("/")
        initial_active = None
        for si, s in enumerate(menu_items):
            if s.get("path") == pathname:
                initial_active = (si,)
                break
            for pi, p in enumerate(s.get("items", [])):
                if p.get("path") == pathname:
                    initial_active = (si, pi)
                    break
            if initial_active:
                break

        def on_click(event):
            if "path" not in event or pn.state.location is None:
                return
            path = event["path"]
            if path == "__new_dashboard__":
                self._open_create_dialog()
                return
            if path == pn.state.location.pathname:
                if "edit=true" in (pn.state.location.search or ""):
                    pn.state.location.param.update(search="")
                    self._show_view_mode()
                return
            self._request_navigation(path)

        self._menu_list = pmui.MenuList(
            items=menu_items,
            on_click=on_click,
            dense=True,
            expanded=list(range(len(menu_items))),
            active=initial_active,
            sizing_mode="stretch_width",
        )

        self._menu_list.on_action("Edit", self._on_action_edit)
        self._menu_list.on_action("Rename", self._on_action_rename)
        self._menu_list.on_action("Delete", self._on_action_delete)
        self._menu_list.on_action("Create", self._on_action_create)

        return self._menu_list

    def __panel__(self):
        """Render the app."""
        return self._page

    @classmethod
    def build_routes(cls, project_dir: str | pathlib.Path, **params) -> dict[str, t.Any]:
        """Generate route mapping for pn.serve."""
        registry = build_registry(pathlib.Path(project_dir))

        def factory():
            return cls(registry=registry, **params)

        routes: dict[str, t.Any] = {
            "/": factory,
            COMPONENTS_ROUTE: factory,
            f"{DASH_ROUTE_PREFIX}[^/]+": factory,
        }
        for app_id, v in registry.items():
            if v.metadata.page:
                routes[f"/{app_id}"] = factory
        return routes
