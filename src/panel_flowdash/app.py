"""Application builder: scans a project directory and constructs the Panel app."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import traceback
import uuid
import warnings
from functools import cache, partial
from html import escape
from pathlib import Path
from typing import Any

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
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

pn.config.notifications = True

logger = logging.getLogger("panel_flowdash")


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


def build_app_class(
    project_dir: Path,
    *,
    store: DashboardStore,
    title: str = "FlowDash",
    default_page: str | None = None,
) -> type:
    """Build a Viewer class configured for the given project directory.

    Returns a class (not an instance) that can be used with pn.serve.
    The class also exposes a `build_routes()` classmethod for route generation.
    """
    registry = build_registry(project_dir)
    page_entries = {app_id: entry for app_id, entry in registry.items() if entry.metadata.page}
    component_entries = {
        app_id: entry for app_id, entry in registry.items() if entry.metadata.component
    }
    component_specs = build_component_specs(registry)
    session_state_class = build_session_state_class(registry)

    resolved_default_page = default_page or next(iter(page_entries), "")

    class FlowDashApp(pn.viewable.Viewer):
        """Dynamically generated FlowDash application."""

        _store = store
        _registry = registry
        _page_entries = page_entries
        _component_entries = component_entries
        _component_specs = component_specs
        _session_state_class = session_state_class
        _title = title
        _project_dir = project_dir
        _default_page = resolved_default_page

        _main_task: asyncio.Task | None = None

        def __init__(self):
            super().__init__()
            self._session_state = self._session_state_class()
            self._user_id = self._resolve_user_id()
            self._loading = False
            self._edge_id_map: dict[str, tuple[str, str, str, str]] = {}
            self._current_dashboard: DashboardModel | None = None
            self._tile_items: list[dict] = []
            self._tile_objects: list[pn.viewable.Viewable] = []
            self._sidebar_views: list[pn.viewable.Viewable] = []
            self._sidebar_container = pn.Column(sizing_mode="stretch_width")
            self._component_picker = self._make_component_picker()
            self._component_status = pn.pane.Alert("", alert_type="primary", visible=False)
            self._dataflow_graph = DataflowGraph(
                self._component_specs,
                on_error=self._on_wiring_error,
            )
            self._flow_canvas = self._build_flow_canvas()
            self._component_view = self._build_component_view()
            self._page = pmui.Page(
                title=self._title,
                theme_config={"palette": {"primary": {"main": "#0072B5"}}},
                sidebar=self.get_sidebar(),
            )
            pn.state.onload(self._load_page_layout)

        def _resolve_user_id(self) -> str:
            if pn.state.user:
                return pn.state.user
            return "default"

        @cache
        def _accepted_injected_params(self, app):
            if inspect.isclass(app) and issubclass(app, pn.viewable.Viewer):
                return {
                    p
                    for p in ("config", "executor", "instance_config", "context")
                    if hasattr(app, p)
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
            for comp_id, spec in self._component_specs.items():
                type_key = comp_id.replace("/", "__")
                node_types[type_key] = pr.NodeType(
                    type=type_key,
                    label=spec.title,
                    inputs=[
                        {"id": port.name, "label": port.label or port.name} for port in spec.inputs
                    ],
                    outputs=[
                        {"id": port.name, "label": port.label or port.name}
                        for port in spec.outputs
                    ],
                )

            flow = pr.ReactFlow(
                nodes=[],
                edges=[],
                node_types=node_types,
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

            flow.on("edge_added", _on_edge_added)
            flow.on("edge_deleted", _on_edge_deleted)
            flow.on("node_deleted", _on_node_deleted)

            self._flow = flow
            return flow

        def _instantiate_for_node(self, entry, node_state):
            """Create a live component view wired to the node_state."""
            app_fn = entry.app

            if not callable(app_fn):
                return pn.panel(app_fn)

            if inspect.isclass(app_fn) and issubclass(app_fn, pn.viewable.Viewer):
                return self._instantiate_viewer_for_node(app_fn, entry, node_state)

            sig = inspect.signature(app_fn)
            kwargs = {}
            if "config" in sig.parameters:
                kwargs["config"] = node_state
            if "context" in sig.parameters:
                kwargs["context"] = "component"

            result = app_fn(**kwargs)
            return pn.panel(result)

        def _instantiate_viewer_for_node(self, viewer_cls, entry, node_state):
            """Instantiate a Viewer and wire its params to the node_state."""
            spec = self._component_specs.get(entry.app_id)
            instance = viewer_cls()

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
            self._clear_button = pmui.Button(
                icon="delete_sweep", color="danger", variant="outlined"
            )
            self._save_button = pmui.Button(icon="save", color="primary", variant="outlined")
            self._add_button.on_click(lambda _event: self._add_component_to_graph())
            self._clear_button.on_click(lambda _event: self._clear_components())
            self._save_button.on_click(lambda _event: self._save_current_dashboard())

            no_components = len(self._component_entries) == 0
            self._component_picker.disabled = no_components
            self._add_button.disabled = no_components

            if no_components:
                self._component_status.object = "No component-enabled apps found."
                self._component_status.alert_type = "warning"
                self._component_status.visible = True

            self._mode_toggle = pmui.RadioButtonGroup(
                options={":material/cable:": "wiring", ":material/dashboard:": "dashboard"},
                value="wiring",
                styles={"margin-left": "auto"},
            )
            self._workspace_area = pn.Column(
                self._flow_canvas,
                sizing_mode="stretch_both",
            )

            def _on_mode_change(event):
                if event.new == "dashboard":
                    self._workspace_area[:] = [self._tile_grid]
                    self._rebuild_tile_grid()
                else:
                    self._workspace_area[:] = [self._flow_canvas]

            self._mode_toggle.param.watch(_on_mode_change, "value")

            self._controls_row = pn.Row(
                self._component_picker,
                self._add_button,
                self._clear_button,
                self._save_button,
                self._mode_toggle,
                sizing_mode="stretch_width",
                align="center",
            )
            return pn.Column(
                self._controls_row,
                self._component_status,
                self._workspace_area,
                sizing_mode="stretch_both",
                styles={"height": "100%"},
            )

        @property
        def _tile_grid(self):
            if not hasattr(self, "_tile__grid"):
                self._tile__grid = TileGrid(
                    card=False,
                    close_action="hide",
                    editable=False,
                    local_save=False,
                    min_height=320,
                    sizing_mode="stretch_both",
                )
            return self._tile__grid

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

        def _set_component_status(self, message: str, *, alert_type: str = "primary"):
            self._component_status.object = message
            self._component_status.alert_type = alert_type
            self._component_status.visible = bool(message)

        def _add_component_to_graph(self):
            component_id = self._component_picker.value
            entry = self._component_entries.get(component_id)
            if entry is None:
                self._set_component_status("Select a valid component first.", alert_type="warning")
                return

            spec = self._component_specs.get(component_id)
            if spec is None:
                return

            type_key = component_id.replace("/", "__")
            instance_id = f"{type_key}_{uuid.uuid4().hex[:6]}"

            node_state = self._dataflow_graph.add_node(instance_id, component_id)

            try:
                view = self._instantiate_for_node(entry, node_state)
            except Exception as e:
                logger.exception("Failed to add component '%s'", component_id)
                self._dataflow_graph.remove_node(instance_id)
                self._set_component_status(f"Failed to add component: {e}", alert_type="danger")
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
                data={"component_id": component_id},
            )
            node_dict = node.to_dict()
            node_dict["view"] = view
            self._flow.add_node(node_dict)

            self._tile_items.append(
                {"instance_id": instance_id, "component_id": component_id, "config": {}}
            )
            self._tile_objects.append(view)

            self._set_component_status(
                f"Added component `{entry.title}` ({instance_id}).",
                alert_type="success",
            )

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
            pending = getattr(self, "_pending_tile_layout", [])
            if pending:
                self._tile_grid.layout = pending
                self._pending_tile_layout = []

        @pn.io.hold()
        def _clear_components(self):
            for node_id in list(self._dataflow_graph.node_ids):
                self._dataflow_graph.remove_node(node_id)
            self._tile_items = []
            self._tile_objects = []
            self._edge_id_map.clear()
            self._sidebar_views = []
            self._sidebar_container.objects = []
            self._flow.nodes = []
            self._flow.edges = []
            self._set_component_status("Cleared all component tiles.", alert_type="primary")

        def _save_current_dashboard(self):
            if self._current_dashboard is None:
                self._set_component_status(
                    "No dashboard loaded. Create one from the sidebar.",
                    alert_type="warning",
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
            self._store.save_dashboard(self._current_dashboard)
            self._set_component_status(
                f'Dashboard "{self._current_dashboard.title}" saved.',
                alert_type="success",
            )

        @pn.io.hold()
        async def _load_dashboard(self, dashboard_id: str):
            dashboard = self._store.load_dashboard(self._user_id, dashboard_id)
            if dashboard is None:
                self._page.main = [
                    pn.pane.Alert(f"Dashboard not found: {dashboard_id}", alert_type="danger")
                ]
                return
            self._current_dashboard = dashboard
            self._loading = True

            for node_id in list(self._dataflow_graph.node_ids):
                self._dataflow_graph.remove_node(node_id)
            self._tile_items = []
            self._tile_objects = []
            self._edge_id_map.clear()
            self._flow.nodes = []
            self._flow.edges = []

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

                try:
                    view = self._instantiate_for_node(entry, node_state)
                except Exception:
                    logger.exception(
                        "Error loading component '%s' (%s)", component_id, instance_id
                    )
                    self._dataflow_graph.remove_node(instance_id)
                    continue

                position = {"x": item.x, "y": item.y}
                node = pr.NodeSpec(
                    id=instance_id,
                    type=type_key,
                    position=position,
                    label=spec.title,
                    data={"component_id": component_id},
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
            self._pending_tile_layout = dashboard.tile_layout or []
            self._set_component_status(
                f'Loaded dashboard "{dashboard.title}" with {len(self._tile_items)} tiles.',
                alert_type="primary",
            )
            self._show_view_mode()
            self._page.main = [self._component_view]

        def _create_new_dashboard(self, title_str: str):
            title_str = title_str.strip()
            if not title_str:
                self._set_component_status(
                    "Dashboard title cannot be empty.", alert_type="warning"
                )
                return
            dashboard = self._store.create_dashboard(self._user_id, title_str)
            self._current_dashboard = dashboard
            self._tile_items = []
            self._tile_objects = []
            self._set_component_status(
                f'Created new dashboard "{dashboard.title}".',
                alert_type="success",
            )
            self._refresh_sidebar_dashboards()
            if pn.state.location:
                pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}"
            self._show_edit_mode()
            self._page.main = [self._component_view]

        def _delete_dashboard(self, dashboard_id: str):
            self._store.delete_dashboard(self._user_id, dashboard_id)
            if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
                self._current_dashboard = None
                self._tile_items = []
                self._tile_objects = []
            self._refresh_sidebar_dashboards()
            self._set_component_status("Dashboard deleted.", alert_type="primary")

        def _rename_dashboard(self, dashboard_id: str, new_title: str):
            new_title = new_title.strip()
            if not new_title:
                return
            self._store.rename_dashboard(self._user_id, dashboard_id, new_title)
            if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
                self._current_dashboard.title = new_title
            self._refresh_sidebar_dashboards()

        def _refresh_sidebar_dashboards(self):
            dash_items = self._get_dashboard_menu_items()
            items = list(self._menu_list.items)
            items[-1] = {**items[-1], "items": dash_items}
            self._menu_list.items = items

        async def _load_page_layout(self):
            if pn.state.location is None:
                return
            pathname = pn.state.location.pathname

            if pathname == "/":
                if self._default_page:
                    pn.state.location.pathname = f"/{self._default_page}"
                    return await self._load_page_layout()
                self._page.main = [self._component_view]
                return

            if pathname == COMPONENTS_ROUTE:
                self._current_dashboard = None
                self._sidebar_container.objects = []
                self._show_edit_mode()
                self._page.main = [self._component_view]
                return

            if pathname.startswith(DASH_ROUTE_PREFIX):
                dashboard_id = pathname[len(DASH_ROUTE_PREFIX) :].strip("/")
                if dashboard_id:
                    await self._load_dashboard(dashboard_id)
                    return

            self._sidebar_container.objects = []
            key = tuple(pathname.strip("/").split("/"))
            if len(key) == 2 and self._entry_from_key(key):
                self._page.main = [await self._render_page(key)]
            else:
                self._page.main = [f"Invalid URL: {pathname}"]

        @pn.io.hold()
        def _show_edit_mode(self):
            self._controls_row.visible = True
            self._component_status.visible = bool(self._component_status.object)
            self._tile_grid.editable = True
            self._tile_grid.card = True
            if self._mode_toggle.value == "wiring":
                self._workspace_area[:] = [self._flow_canvas]
            else:
                self._workspace_area[:] = [self._tile_grid]
                self._rebuild_tile_grid()

        @pn.io.hold()
        def _show_view_mode(self):
            self._controls_row.visible = False
            self._component_status.visible = False
            self._tile_grid.param.update(card=False, editable=False)
            self._workspace_area[:] = [self._tile_grid]
            self._rebuild_tile_grid()

        _DASHBOARD_ACTIONS = [
            {"label": "Edit", "icon": "edit"},
            {"label": "Rename", "icon": "drive_file_rename_outline"},
            {"label": "Delete", "icon": "delete"},
        ]

        def _get_dashboard_menu_items(self) -> list[dict]:
            items = []
            dashboards = self._store.list_dashboards(self._user_id)
            for d in dashboards:
                items.append(
                    {
                        "icon": "dashboard",
                        "label": d.title,
                        "path": f"{DASH_ROUTE_PREFIX}{d.dashboard_id}",
                        "href": f"{DASH_ROUTE_PREFIX}{d.dashboard_id}",
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
            path = item.get("path", "")
            dashboard_id = self._dashboard_id_from_path(path)
            if dashboard_id:
                pn.state.execute(partial(self._load_dashboard_edit, dashboard_id))

        async def _load_dashboard_edit(self, dashboard_id: str):
            await self._load_dashboard(dashboard_id)
            self._show_edit_mode()

        @pn.io.hold()
        def _on_action_rename(self, item):
            path = item.get("path", "")
            dashboard_id = self._dashboard_id_from_path(path)
            if not dashboard_id:
                return
            self._dialog_name_input.value = item.get("label", "")
            self._dialog_context = {"action": "rename", "dashboard_id": dashboard_id}
            self._dialog.title = "Rename Dashboard"
            self._dialog.open = True

        @pn.io.hold()
        def _on_action_delete(self, item):
            path = item.get("path", "")
            dashboard_id = self._dashboard_id_from_path(path)
            if not dashboard_id:
                return
            self._dialog_name_input.value = item.get("label", "")
            self._dialog_name_input.disabled = True
            self._dialog_context = {"action": "delete", "dashboard_id": dashboard_id}
            self._dialog.title = "Delete Dashboard"
            self._dialog.open = True

        @pn.io.hold()
        def _on_action_create(self, item):
            self._dialog_name_input.value = ""
            self._dialog_name_input.disabled = False
            self._dialog_context = {"action": "create"}
            self._dialog.title = "Create Dashboard"
            self._dialog.open = True

        @pn.io.hold()
        def _on_dialog_confirm(self, _event):
            ctx = self._dialog_context
            self._dialog.open = False
            if not ctx:
                return
            action = ctx.get("action")
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
            self._dialog_name_input.disabled = False
            self._dialog_context = {}

        @pn.io.hold()
        def _build_dialog(self):
            self._dialog_name_input = pmui.TextInput(
                label="Name",
                sizing_mode="stretch_width",
            )
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
            return self._dialog

        def get_sidebar(self):
            sections: dict[str, list[RegistryEntry]] = {}
            for entry in self._page_entries.values():
                sections.setdefault(entry.section, []).append(entry)

            menu_items = [
                {
                    "label": section.replace("_", " "),
                    "selectable": False,
                    "icon": None,
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
                for section, section_apps in sorted(sections.items())
            ]
            menu_items.append(
                {
                    "label": "Custom Apps",
                    "selectable": False,
                    "icon": None,
                    "items": self._get_dashboard_menu_items(),
                }
            )

            current_path = pn.state.location.pathname if pn.state.location is not None else ""
            pathname = "/" + (current_path.strip("/") or self._default_page)
            initial_active = next(
                (
                    (si, pi)
                    for si, s in enumerate(menu_items)
                    for pi, p in enumerate(s["items"])
                    if p["path"] == pathname
                ),
                None,
            )

            def on_click(event):
                if "path" not in event or pn.state.location is None:
                    return
                path = event["path"]
                if path == "__new_dashboard__":
                    return
                if path == pn.state.location.pathname:
                    return
                pn.state.location.pathname = path
                pn.state.execute(self._load_page_layout)

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

            dialog = self._build_dialog()

            return [self._menu_list, dialog, self._sidebar_container]

        def __panel__(self):
            return self._page

        @classmethod
        def build_routes(cls) -> dict[str, type]:
            """Generate route mapping for pn.serve."""
            routes: dict[str, Any] = {
                "/": cls,
                COMPONENTS_ROUTE: cls,
                f"{DASH_ROUTE_PREFIX}{{custom_app}}": cls,
            }
            for app_id in cls._page_entries:
                routes[f"/{app_id}"] = cls
            return routes

    return FlowDashApp
