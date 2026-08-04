"""Application builder: scans a project directory and constructs the Panel app."""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import traceback
import typing as t
from contextlib import asynccontextmanager
from functools import cache, partial
from html import escape

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Children, Viewer

from panel_flowdash.auth import (
    AuthConfig,
    Permission,
    is_authorized,
    resolve_identity,
)
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry, build_registry
from panel_flowdash.session_state import build_session_state_class, check_requirements
from panel_flowdash.util import notify, panel_call, panel_viewer

logger = logging.getLogger("panel_flowdash")

if t.TYPE_CHECKING:

    class _DASHBOARD_ACTION_TYPE(t.TypedDict):
        label: str
        icon: str


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

_LAUNCHER_NEW_CARD_CSS = """
:host {
  cursor: pointer;
  transition: box-shadow 0.2s, border-color 0.2s;
}
:host .MuiPaper-root {
  border: 1px dashed var(--mui-palette-divider, rgba(0,0,0,0.23));
  box-shadow: none;
  background: transparent;
}
:host(:hover) .MuiPaper-root {
  border-color: var(--mui-palette-primary-main, #0072b5);
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

    auth_config = param.ClassSelector(
        class_=AuthConfig,
        doc="""
        Project-level authorization configuration controlling group discovery,
        the admin groups and the default access policy.""",
    )

    breakpoints = param.List(default=[768, 1200], doc="Responsive breakpoints for the tile grid.")

    configure_layout = param.Callable(
        default=None,
        doc="""
        Optional callback invoked on every navigation with
        (app, content, route). Use it to set `app.sidebar` and
        `app.contextbar` for the page currently being served.""",
    )

    contextbar = Children(default=[], doc="Items prepended to the contextbar.")

    contextbar_open = param.Boolean(default=False, doc="Whether the contextbar is open.")

    home_dashboard = param.String(
        default=None,
        doc="""
        Dashboard shown on the homepage ('/'). Accepts a dashboard id or
        title. When unset, the homepage shows the dashboard grid launcher.""",
    )

    nav_variant = param.Selector(
        default="drawer",
        objects=["drawer", "menubar"],
        doc="""
        Where the navigation menu is rendered. 'drawer' docks a MenuList in a
        right-hand drawer; 'menubar' places a MenuBar in the page header with
        quick-action icons alongside it.""",
    )

    notifications = param.Boolean(
        default=True,
        doc="""
        Whether to surface user-facing messages as Panel notifications. When
        disabled (or when no notification area exists) messages are logged.""",
    )

    page_options = param.Dict(
        default={},
        doc="""
        Extra keyword arguments passed through to the underlying
        `panel_material_ui.Page`, overriding the app's own defaults.""",
    )

    project_dir = param.Path(doc="Path to the project directory.")

    sidebar = Children(default=[], doc="Items prepended to the sidebar.")

    store = param.ClassSelector(
        class_=DashboardStore, doc="DashboardStore instance for persistence."
    )

    title = param.String(default="FlowDash", doc="Application title shown in the browser tab.")

    _main_task: asyncio.Task | None = None

    def __init__(self, registry: dict[str, RegistryEntry] | None = None, **params):
        super().__init__(**params)

        if self.notifications:
            pn.config.notifications = True

        if self.auth_config is None:
            self.auth_config = AuthConfig()

        if registry is None:
            registry = build_registry(pathlib.Path(self.project_dir))
        page_entries = {k: v for k, v in registry.items() if v.metadata.page}
        # Session state is built from AST metadata — no imports needed.
        session_state_class = build_session_state_class(registry)
        self._registry = registry
        self._page_entries = page_entries
        self._session_state_class = session_state_class

        self._session_state = self._session_state_class()
        self._identity = resolve_identity(self.auth_config)
        self._user_id = self._resolve_user_id()
        self._sidebar_container = pn.Column(sizing_mode="stretch_width")
        self._share_button = pmui.Button(
            icon="share", color="primary", variant="outlined", visible=False
        )
        self._share_button.on_click(lambda _event: self._share_current_dashboard())
        # The editor owns the canvas, tile grid and persistence; this class adds
        # routing, navigation, pages and authorization on top of it.
        self._editor = self._build_editor()
        self._menu_bar = None
        self._nav_menu = self._build_nav_menu()
        self._nav_drawer = pmui.Drawer(
            pmui.Typography(
                "Navigation",
                variant="overline",
                margin=(8, 16, 0, 16),
                styles={"opacity": "0.6", "letter-spacing": "0.08em"},
            ),
            pmui.Divider(margin=(4, 0, 4, 0)),
            self._nav_menu,
            anchor="right",
            inline=True,
            variant="docked",
            width_policy="fixed",
        )
        if self.nav_variant == "menubar":
            self._build_nav_bar()
        self._menubar_mode = self.nav_variant == "menubar"
        self._build_dialog()
        self._build_unsaved_dialog()
        self._build_share_dialog()
        # The share dialog is a portaled overlay; mount it once inside the
        # always-present nav drawer rather than in each _page.main layout.
        page_kwargs = {
            "title": self.title,
            "theme_config": {"palette": {"primary": {"main": "#0072B5"}}},
            "sidebar_open": False,
            "sidebar": self.param.sidebar.rx() + [self._sidebar_container],  # noqa: RUF005
            "contextbar": self.param.contextbar.rx(),
            "contextbar_variant": "persistent",
            "contextbar_open": self.param.contextbar_open,
            **self.page_options,
        }
        overflow_patch = {".main-content": {"overflow-x": "hidden"}}
        if "sx" in page_kwargs:
            page_kwargs["sx"].update(**overflow_patch)
        else:
            page_kwargs["sx"] = {".main-content": {"overflow-x": "hidden"}}
        if self._menu_bar is not None:
            page_kwargs.setdefault("header", [self._menu_bar])
        self._page = pmui.Page(**page_kwargs)
        pn.state.onload(self._load_page_layout)

    @asynccontextmanager
    async def _loading_screen(self, delay: float = 0.5):
        """Show a loading placeholder if the block takes longer than *delay* seconds."""

        async def _show_after_delay():
            await asyncio.sleep(delay)
            self._page.main = [
                pmui.LinearProgress(sizing_mode="stretch_width"),
                self._dialog,
                self._unsaved_dialog,
                self._share_dialog,
            ]

        task = asyncio.create_task(_show_after_delay())
        try:
            yield
        finally:
            task.cancel()

    def _nav_content(self, content):
        """Wrap page content with the docked nav drawer, unless in menubar mode.

        In menubar mode navigation lives in the page header, so the content is
        returned as-is; in drawer mode it is paired with the docked drawer.
        """
        if self._menubar_mode:
            return content
        return pn.Row(content, self._nav_drawer, sizing_mode="stretch_both")

    def _resolve_user_id(self) -> str:
        return self._identity.user

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

    def _default_allow(self) -> bool:
        return self.auth_config.default_allow if self.auth_config else True

    def _can_access_entry(self, entry: RegistryEntry) -> bool:
        """Whether the current identity may access a page/component entry."""
        authorize = entry.metadata.authorize
        if authorize is not None:
            try:
                return bool(authorize(self._identity))
            except Exception:
                logger.exception("authorize callback failed for '%s'", entry.app_id)
                return False
        return is_authorized(
            entry.metadata.permission,
            self._identity,
            default_allow=self._default_allow(),
        )

    def _admin_groups(self) -> frozenset[str]:
        return self.auth_config.admin_groups if self.auth_config else frozenset()

    def _accessible_page_entries(self) -> dict[str, RegistryEntry]:
        """Page entries the current identity is authorized to see."""
        return {
            app_id: entry
            for app_id, entry in self._page_entries.items()
            if self._can_access_entry(entry)
        }

    def _can_administer_dashboard(self, dashboard_id: str) -> bool:
        """Whether the current identity may edit/delete/share a dashboard."""
        return self.store.can_administer(self._identity, dashboard_id, self._admin_groups())

    def _access_denied_view(self, title: str | None = None):
        """Generate the view shown when the user is not authorized for the target."""
        label = f" **{title}**" if title else ""
        return pmui.Alert(
            object=(
                f"Access denied.{label} You are not authorized to view this "
                f"(signed in as `{self._identity.user}`)."
            ),
            severity="error",
            title="Access denied",
            sizing_mode="stretch_width",
        )

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

        app = await asyncio.to_thread(entry.load)
        if not callable(app):
            return pn.panel(app)
        kwargs = self._add_kwargs_dict(app, context=context, instance_config=instance_config)
        if inspect.isasyncgenfunction(app):
            # Cannot be awaited to a single value, so defer it to a ParamFunction
            # that iterates it on the event loop as the page renders.
            return panel_call(app, **kwargs)
        if inspect.iscoroutinefunction(app):
            return await app(**kwargs)
        result = await asyncio.to_thread(app, **kwargs)
        if isinstance(result, pn.viewable.Viewer):
            return panel_viewer(result)
        return pn.panel(result)

    async def _render_page(self, key):
        entry = self._entry_from_key(key)
        if entry is None:
            return f"Unknown page: {'/'.join(key)}"

        if not self._can_access_entry(entry):
            return self._access_denied_view(entry.title)

        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()

        try:
            async with self._loading_screen():
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

    def _build_editor(self) -> FlowDash:
        """Construct the embedded editor and wire it into the app shell."""
        editor = FlowDash(
            components=self._registry,
            breakpoints=self.breakpoints,
            notifications=self.notifications,
            store=self.store,
            toolbar_extra=[self._share_button],
            user=self._user_id,
        )
        editor.param.watch(self._on_editor_sidebar, "sidebar")
        return editor

    def _on_editor_sidebar(self, event):
        """Mirror the editor's sidebar views into the page sidebar."""
        self._sidebar_container.objects = list(event.new)
        self._page.sidebar_open = bool(event.new)

    def _notify(self, severity: str, message: str, duration: int = 3000):
        """Surface a message to the user, or log it when notifications are unavailable."""
        notify(severity, message, duration=duration, enabled=self.notifications)

    @property
    def _component_entries(self) -> dict[str, RegistryEntry]:
        """Component entries offered by the editor."""
        return self._editor._component_entries

    @property
    def _component_view(self):
        """The editor view, mounted into `_page.main` for editor routes."""
        return self._editor

    @property
    def _current_dashboard(self) -> DashboardModel | None:
        return self._editor.dashboard

    @_current_dashboard.setter
    def _current_dashboard(self, value):
        self._editor.dashboard = value

    @property
    def _dirty(self) -> bool:
        return self._editor.dirty

    @_dirty.setter
    def _dirty(self, value):
        self._editor.dirty = value

    def _reset_canvas(self):
        """Clear the editor canvas and the mirrored page sidebar."""
        self._editor._reset_canvas()
        self._sidebar_container.objects = []

    async def _ensure_components_loaded(self, component_ids: t.Iterable[str] | None = None):
        """Load component modules and build their specs, if not done yet.

        Defaults to the whole catalog, which is what the editor palette needs.
        Dashboard routes pass the components the dashboard places instead.
        """
        await self._editor.ensure_components_loaded_async(component_ids)

    def _save_current_dashboard(self):
        """Save the loaded dashboard, refusing when the user only has read access."""
        current = self._current_dashboard
        if current is None:
            self._notify(
                "warning", "No dashboard loaded. Create one from the sidebar.", duration=4000
            )
            return
        if not self._can_administer_dashboard(current.dashboard_id):
            self._editor.read_only = True
            self._notify("error", "You have view-only access to this dashboard.", duration=4000)
            return
        self._editor.read_only = False
        self._editor._on_save_clicked()

    def _share_current_dashboard(self):
        if self._current_dashboard is None:
            self._notify("warning", "No dashboard loaded.", duration=3000)
            return
        self._open_share_dialog(
            self._current_dashboard.dashboard_id, self._current_dashboard.title
        )

    async def _load_dashboard(self, dashboard_id: str, edit: bool = False):
        dashboard = self.store.load_for_access(
            self._identity, dashboard_id, default_allow=self._default_allow()
        )
        if dashboard is not None:
            # Only the components this dashboard places, imported off the event
            # loop. The editor palette needs no imports (it is built from the
            # scanned metadata) and `add_component` imports on demand, so even
            # edit mode does not pay for the whole catalog.
            await self._ensure_components_loaded({item.component_id for item in dashboard.items})
        with pn.io.hold():
            self._load_dashboard_sync(dashboard_id, edit=edit, dashboard=dashboard)

    def _load_dashboard_sync(
        self,
        dashboard_id: str,
        edit: bool = False,
        dashboard: DashboardModel | None = None,
    ):
        if dashboard is None:
            dashboard = self.store.load_for_access(
                self._identity, dashboard_id, default_allow=self._default_allow()
            )
        if edit and dashboard is not None and not self._can_administer_dashboard(dashboard_id):
            # A viewer with read access followed an edit link; downgrade to view.
            edit = False
        if dashboard is None:
            self._page.main = [
                self._nav_content(
                    pn.pane.Alert(f"Dashboard not found: {dashboard_id}", alert_type="danger")
                ),
                self._dialog,
                self._unsaved_dialog,
                self._share_dialog,
            ]
            return

        self._editor.load_model(dashboard)
        self._notify(
            "info",
            f'Loaded dashboard "{dashboard.title}" with {len(dashboard.items)} tiles.',
            duration=3000,
        )
        if edit:
            self._show_edit_mode()
        else:
            self._show_view_mode()
        self._sync_menu_active(f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        self._apply_layout_config(self._component_view, f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        self._page.main = [
            self._nav_content(self._component_view),
            self._dialog,
            self._unsaved_dialog,
            self._share_dialog,
        ]

    def _create_new_dashboard(self, title_str: str):
        title_str = title_str.strip()
        if not title_str:
            self._notify("warning", "Dashboard title cannot be empty.", duration=3000)
            return
        dashboard = self._editor.new_dashboard(title_str)
        self._sidebar_container.objects = []

        self._notify("success", f'Created new dashboard "{dashboard.title}".', duration=3000)
        self._refresh_sidebar_dashboards()
        if pn.state.location:
            pn.state.location.param.update(
                pathname=f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}",
                search="?edit=true",
            )
        self._show_edit_mode()
        self._sync_menu_active(f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}")
        self._apply_layout_config(
            self._component_view, f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}"
        )
        self._page.main = [
            self._nav_content(self._component_view),
            self._dialog,
            self._unsaved_dialog,
            self._share_dialog,
        ]

    def _delete_dashboard(self, dashboard_id: str):
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to delete this dashboard.")
            return
        was_current = bool(
            self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id
        )
        owner = self.store.get_owner(dashboard_id) or self._user_id
        self.store.delete_dashboard(owner, dashboard_id)
        if was_current:
            self._current_dashboard = None
            self._reset_canvas()
            self._dirty = False

        self._refresh_sidebar_dashboards()
        self._notify("info", "Dashboard deleted.", duration=3000)

        if was_current:
            self._navigate_to("/")
        elif pn.state.location is not None and pn.state.location.pathname == "/":
            self._page.main = [
                self._nav_content(self._build_launcher()),
                self._dialog,
                self._unsaved_dialog,
                self._share_dialog,
            ]

    def _rename_dashboard(self, dashboard_id: str, new_title: str):
        new_title = new_title.strip()
        if not new_title:
            return
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to rename this dashboard.")
            return
        owner = self.store.get_owner(dashboard_id) or self._user_id
        self.store.rename_dashboard(owner, dashboard_id, new_title)
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.title = new_title
        self._refresh_sidebar_dashboards()

    def _refresh_sidebar_dashboards(self):
        dash_items = self._get_dashboard_menu_items()
        items = list(self._menu_list.items)
        items[-1] = {**items[-1], "items": dash_items}
        self._menu_list.items = items
        self._refresh_menu_bar()

    def _build_launcher(self):
        sections: dict[str, list[RegistryEntry]] = {}
        for entry in self._accessible_page_entries().values():
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

        dashboards = self.store.list_accessible(
            self._identity, default_allow=self._default_allow()
        )
        dash_cards = []
        for d in dashboards:
            can_admin = self._can_administer_dashboard(d.dashboard_id)
            speed_dial = None
            if can_admin:
                speed_dial = pmui.SpeedDial(
                    items=self._dashboard_speed_dial_items(can_admin),
                    icon="more_vert",
                    direction="down",
                    color="default",
                    size="small",
                    persistent_tooltips=True,
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
            wrapper_objects = [clickable]
            if speed_dial is not None:
                wrapper_objects.append(speed_dial)
            wrapper = pn.Column(
                *wrapper_objects,
                styles={"position": "relative", "overflow": "visible"},
                sizing_mode="fixed",
                width=200,
                height=140,
            )
            dash_cards.append(wrapper)

        new_card = pmui.Card(
            pmui.ButtonIcon(
                icon="add",
                icon_size="3em",
                disabled=True,
                stylesheets=[":host { pointer-events: none; opacity: 1; }"],
            ),
            title="New Dashboard",
            collapsible=False,
            stylesheets=[_LAUNCHER_NEW_CARD_CSS],
            title_variant="h4",
            width=200,
            height=140,
        )
        new_clickable = pmui.Clickable(object=new_card)
        new_clickable.on_click(lambda *_args: self._open_create_dialog())
        dash_cards.append(new_clickable)

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
            self._dialog.param.update(title="Rename Dashboard", open=True)
        elif label == "Delete":
            self._dialog_name_input.param.update(value=title, disabled=True)
            self._dialog_context = {"action": "delete", "dashboard_id": dashboard_id}
            self._dialog.title = "Delete Dashboard"
            self._dialog.open = True

    def _apply_layout_config(self, content, route):
        """Run the ``configure_layout`` hook for the current navigation, if any."""
        if self.configure_layout is None:
            return
        try:
            self.configure_layout(self, content, route)
        except Exception:
            logger.exception("configure_layout hook failed for route '%s'", route)

    async def _show_home_dashboard(self) -> bool:
        """Render the configured home dashboard on '/'. Returns whether it was shown.

        Falls back to the launcher grid (by returning ``False``) if the
        configured dashboard cannot be resolved or the current identity is not
        authorized to view it.
        """
        model = self.store.find_by_id_or_title(self.home_dashboard)
        if model is None:
            logger.warning("Configured home dashboard not found: '%s'", self.home_dashboard)
            return False
        if (
            self.store.load_for_access(
                self._identity, model.dashboard_id, default_allow=self._default_allow()
            )
            is None
        ):
            return False
        async with self._loading_screen():
            await self._load_dashboard(model.dashboard_id, edit=False)
        return True

    async def _load_page_layout(self):
        if pn.state.location is None:
            return
        pathname = pn.state.location.pathname

        if pathname == "/":
            if self.home_dashboard and await self._show_home_dashboard():
                return
            self._current_dashboard = None
            self._sidebar_container.objects = []
            self._page.sidebar_open = False
            launcher = self._build_launcher()
            self._apply_layout_config(launcher, pathname)
            self._page.main = [
                self._nav_content(launcher),
                self._dialog,
                self._unsaved_dialog,
            ]
            return

        if pathname == COMPONENTS_ROUTE:
            self._current_dashboard = None
            self._sidebar_container.objects = []
            # No preload: the component picker is built from the scanned metadata
            # and `add_component` imports the one component being placed, so an
            # empty editor costs no imports.
            self._show_edit_mode()
            self._apply_layout_config(self._component_view, pathname)
            self._page.main = [
                self._nav_content(self._component_view),
                self._dialog,
                self._unsaved_dialog,
            ]
            return

        if pathname.startswith(DASH_ROUTE_PREFIX):
            dashboard_id = pathname[len(DASH_ROUTE_PREFIX) :].strip("/")
            if dashboard_id:
                search = pn.state.location.search or ""
                edit_requested = "edit=true" in search
                async with self._loading_screen():
                    await self._load_dashboard(dashboard_id, edit=edit_requested)
                return

        self._current_dashboard = None

        self._sidebar_container.objects = []
        key = tuple(pathname.strip("/").split("/"))
        if len(key) == 2 and self._entry_from_key(key):
            content = await self._render_page(key)
            self._apply_layout_config(content, pathname)
            wrapper = pmui.Column(content, sizing_mode="stretch_width")
            self._page.main = [
                self._nav_content(wrapper),
                self._dialog,
                self._unsaved_dialog,
            ]
        else:
            self._apply_layout_config(None, pathname)
            main = [
                f"Invalid URL: {pathname}",
                self._dialog,
                self._unsaved_dialog,
            ]
            if not self._menubar_mode:
                main.append(self._nav_drawer)
            self._page.main = main

    @pn.io.hold()
    def _show_edit_mode(self):
        current = self._current_dashboard
        can_admin = current is not None and self._can_administer_dashboard(current.dashboard_id)
        self._share_button.visible = can_admin
        self._editor.param.update(editable=True, read_only=not can_admin)
        if pn.state.location is not None:
            pn.state.location.param.update(search="?edit=true")

    @pn.io.hold()
    def _show_view_mode(self):
        self._share_button.visible = False
        self._editor.editable = False
        if pn.state.location is not None:
            pn.state.location.param.update(search="")

    _ADMIN_DASHBOARD_ACTIONS = (
        {"label": "Edit", "icon": "edit"},
        {"label": "Rename", "icon": "drive_file_rename_outline"},
        {"label": "Delete", "icon": "delete"},
    )

    _VIEWER_DASHBOARD_ACTIONS = ()

    def _dashboard_actions(self, can_admin: bool) -> tuple:
        """Menu actions for a dashboard, gated by administration rights."""
        return self._ADMIN_DASHBOARD_ACTIONS if can_admin else self._VIEWER_DASHBOARD_ACTIONS

    def _dashboard_speed_dial_items(self, can_admin: bool) -> list[dict]:
        """SpeedDial items for a launcher dashboard card, gated by admin rights."""
        return [dict(action) for action in self._dashboard_actions(can_admin)]

    def _get_dashboard_menu_items(self) -> list[dict]:
        items = []
        dashboards = self.store.list_accessible(
            self._identity, default_allow=self._default_allow()
        )
        for d in dashboards:
            can_admin = self._can_administer_dashboard(d.dashboard_id)
            item = {
                "icon": "dashboard",
                "label": d.title,
                "path": f"{DASH_ROUTE_PREFIX}{d.dashboard_id}",
                "disable_link": True,
            }
            actions = self._dashboard_actions(can_admin)
            if actions:
                item["actions"] = list(actions)
            items.append(item)
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
        self._nav_drawer.open = False
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
        self._nav_drawer.open = False
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
        self._nav_drawer.open = False
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
        self._nav_drawer.open = False

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

    def _build_share_dialog(self):
        """Build the (owner/admin-only) dashboard sharing dialog."""
        self._share_context: dict = {}
        common = dict(sizing_mode="stretch_width", solid=True, delete_button=True)
        self._share_allow_groups = pmui.MultiChoice(
            label="Allow groups", helper_text="Members of any listed group.", **common
        )
        self._share_allow_users = pmui.MultiChoice(
            label="Allow users", helper_text="OAuth logins or system users.", **common
        )
        self._share_deny_groups = pmui.MultiChoice(
            label="Deny groups", helper_text="Deny always wins.", **common
        )
        self._share_deny_users = pmui.MultiChoice(
            label="Deny users", helper_text="Deny always wins.", **common
        )
        self._share_widgets = (
            self._share_allow_groups,
            self._share_allow_users,
            self._share_deny_groups,
            self._share_deny_users,
        )
        confirm_btn = pmui.Button(label="Save sharing", color="primary")
        cancel_btn = pmui.Button(label="Cancel", color="light")
        confirm_btn.on_click(self._on_share_confirm)
        cancel_btn.on_click(lambda _: setattr(self._share_dialog, "open", False))
        self._share_dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    pmui.Typography(
                        "Grant access by group or user. With no rules the project "
                        "default applies.",
                        variant="body2",
                        styles={"opacity": "0.7"},
                    ),
                    self._share_allow_groups,
                    self._share_allow_users,
                    self._share_deny_groups,
                    self._share_deny_users,
                    pn.Row(confirm_btn, cancel_btn),
                    sizing_mode="stretch_width",
                )
            ],
            title="Share Dashboard",
            open=False,
            min_width=420,
        )
        return self._share_dialog

    def _known_groups(self) -> list[str]:
        """Discoverable group names to offer in the sharing dialog."""
        groups: set[str] = set(self._identity.groups)
        if self.auth_config is not None:
            groups |= set(self.auth_config.admin_groups)
            for member_groups in self.auth_config.user_groups.values():
                groups |= set(member_groups)
        return sorted(groups)

    def _known_users(self) -> list[str]:
        """Discoverable user names to offer in the sharing dialog."""
        users: set[str] = set(self._identity.user_names)
        if self.auth_config is not None:
            users |= set(self.auth_config.user_groups)
        return sorted(users)

    @pn.io.hold()
    def _open_share_dialog(self, dashboard_id: str, title: str):
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to share this dashboard.")
            return
        model = self.store.load_for_access(
            self._identity, dashboard_id, default_allow=self._default_allow()
        )
        perm = model.permission if model else Permission()
        self._share_context = {"dashboard_id": dashboard_id}

        # Seed options from discoverable names, extended with any values already
        # stored on the permission so custom entries render (MultiChoice shows
        # out-of-option values as removable chips).
        known_groups = self._known_groups()
        known_users = self._known_users()
        for widget, options, selected in (
            (self._share_allow_groups, known_groups, perm.allow_groups),
            (self._share_allow_users, known_users, perm.allow_users),
            (self._share_deny_groups, known_groups, perm.deny_groups),
            (self._share_deny_users, known_users, perm.deny_users),
        ):
            widget.param.update(
                options=sorted(set(options) | set(selected)),
                value=sorted(selected),
            )

        self._share_dialog.title = f"Share “{title}”" if title else "Share Dashboard"
        self._share_dialog.open = True

    @pn.io.hold()
    def _on_share_confirm(self, _event):
        ctx = self._share_context
        dashboard_id = ctx.get("dashboard_id", "")
        if not dashboard_id:
            return
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to share this dashboard.")
            self._share_dialog.open = False
            return
        permission = Permission.from_spec(
            allow_groups=self._share_allow_groups.value,
            allow_users=self._share_allow_users.value,
            deny_groups=self._share_deny_groups.value,
            deny_users=self._share_deny_users.value,
        )
        self.store.set_permission(dashboard_id, permission)
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.permission = permission
        self._share_dialog.open = False
        self._share_context = {}
        self._refresh_sidebar_dashboards()
        self._notify("success", "Sharing updated.", duration=3000)

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

    def _build_nav_menu_items(self) -> list[dict]:
        """Assemble the nav tree: Home, page sections and the Custom Apps group."""
        sections: dict[str, list[RegistryEntry]] = {}
        for entry in self._accessible_page_entries().values():
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
        return menu_items

    def _initial_menu_active(self, menu_items: list[dict]):
        """Index of the menu item matching the current pathname, if any."""
        current_path = pn.state.location.pathname if pn.state.location is not None else ""
        pathname = "/" + current_path.strip("/")
        for si, s in enumerate(menu_items):
            if s.get("path") == pathname:
                return (si,)
            for pi, p in enumerate(s.get("items", [])):
                if p.get("path") == pathname:
                    return (si, pi)
        return None

    def _on_nav_click(self, event):
        """Shared click handler for both the drawer MenuList and header MenuBar."""
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

    def _build_nav_menu(self):
        menu_items = self._build_nav_menu_items()

        self._menu_list = pmui.MenuList(
            items=menu_items,
            on_click=self._on_nav_click,
            dense=True,
            expanded=list(range(len(menu_items))),
            active=self._initial_menu_active(menu_items),
            width_policy="max",
        )

        self._menu_list.on_action("Edit", self._on_action_edit)
        self._menu_list.on_action("Rename", self._on_action_rename)
        self._menu_list.on_action("Delete", self._on_action_delete)
        self._menu_list.on_action("Create", self._on_action_create)

        return self._menu_list

    def _build_menu_bar_items(self) -> list[dict]:
        """MenuBar equivalent of the nav tree, with dashboard management submenus.

        MenuBar has no inline action buttons, so each dashboard is exposed as a
        submenu (Open plus, for administrators, Edit/Rename/Delete) and the
        management verbs are encoded via a ``nav_action`` key routed in
        :meth:`_on_menu_bar_click`.
        """
        sections: dict[str, list[RegistryEntry]] = {}
        for entry in self._accessible_page_entries().values():
            sections.setdefault(entry.section, []).append(entry)

        nav_items: list[dict] = [{"label": "Home", "icon": "home", "path": "/"}]
        for section, section_apps in sorted(sections.items()):
            nav_items.append(
                {
                    "label": section.replace("_", " "),
                    "icon": self._section_icon(section),
                    "items": [
                        {"label": page_entry.title, "path": page_entry.page_path}
                        for page_entry in sorted(section_apps, key=lambda e: e.name)
                    ],
                }
            )

        dashboards = self.store.list_accessible(
            self._identity, default_allow=self._default_allow()
        )
        dash_items: list[dict] = [
            {"label": "New Dashboard", "icon": "add", "path": "__new_dashboard__"},
        ]
        if dashboards:
            dash_items.append(None)
        for d in dashboards:
            path = f"{DASH_ROUTE_PREFIX}{d.dashboard_id}"
            entries = [{"label": "Open", "icon": "open_in_new", "path": path}]
            if self._can_administer_dashboard(d.dashboard_id):
                entries += [
                    {"label": "Edit", "icon": "edit", "path": path, "nav_action": "edit"},
                    {
                        "label": "Rename",
                        "icon": "drive_file_rename_outline",
                        "path": path,
                        "nav_action": "rename",
                        "title": d.title,
                    },
                    {
                        "label": "Delete",
                        "icon": "delete",
                        "path": path,
                        "nav_action": "delete",
                        "title": d.title,
                    },
                ]
            dash_items.append({"label": d.title, "icon": "dashboard", "items": entries})

        return [
            {"label": "Navigate", "icon": "menu", "items": nav_items},
            {"label": "Dashboards", "icon": "dashboard_customize", "items": dash_items},
        ]

    def _on_menu_bar_click(self, item):
        """Route a MenuBar click, dispatching dashboard management verbs."""
        action = item.get("nav_action") if isinstance(item, dict) else None
        if action is None:
            self._on_nav_click(item)
            return
        synthetic = {"path": item.get("path", ""), "label": item.get("title", "")}
        if action == "edit":
            self._on_action_edit(synthetic)
        elif action == "rename":
            self._on_action_rename(synthetic)
        elif action == "delete":
            self._on_action_delete(synthetic)

    def _build_nav_bar(self):
        """Build the header MenuBar and its quick-action icons (menubar variant)."""
        self._menu_bar = pmui.MenuBar(
            items=self._build_menu_bar_items(),
            on_click=self._on_menu_bar_click,
            color="default",
            margin=(0, 0, 0, 30),
            variant="outlined",
            sx={"border": "none", "boxShadow": "none"},
        )
        return self._menu_bar

    def _refresh_menu_bar(self):
        """Rebuild the header MenuBar items after the dashboard list changes."""
        if self._menu_bar is not None:
            self._menu_bar.items = self._build_menu_bar_items()

    def __panel__(self):
        """Render the app."""
        return self._page

    @classmethod
    def build_routes(
        cls,
        project_dir: str | pathlib.Path,
        registry: dict[str, RegistryEntry] | None = None,
        **params,
    ) -> dict[str, t.Any]:
        """Generate route mapping for pn.serve."""
        if registry is None:
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
