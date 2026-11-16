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
    entry_authorized,
    resolve_identity,
)
from panel_flowdash.dashboard_store import (
    BaseDashboardStore,
    DashboardModel,
    DashboardStoreError,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry, build_registry
from panel_flowdash.session_state import build_session_state_class, check_requirements
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.ui.command_palette import CommandPaletteController
from panel_flowdash.ui.gallery import ComponentGallery
from panel_flowdash.ui.launcher import (
    Launcher,
    build_downloader,
    copy_dashboard,
    parse_dashboard_file,
    send_download,
    unique_title,
)
from panel_flowdash.ui.shell import HEADER_SX, AppShell
from panel_flowdash.util import notify, page_dialog, panel_call, panel_viewer

logger = logging.getLogger("panel_flowdash")

if t.TYPE_CHECKING:

    class _DASHBOARD_ACTION_TYPE(t.TypedDict):
        label: str
        icon: str


COMPONENTS_ROUTE = "/components"
DASH_ROUTE_PREFIX = "/dash/"

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

    editor_class = param.ClassSelector(
        class_=FlowDash,
        default=FlowDash,
        is_instance=False,
        doc="""
        The `FlowDash` subclass instantiated as the app's editor. Read once at
        construction; the same editor is reused for every dashboard.""",
    )

    editor_options = param.Dict(
        default={},
        doc="""
        Extra keyword arguments passed through to the `editor_class`
        constructor, e.g. `value_repr` or `popup_trigger`. Entries override
        the defaults the app derives from its own parameters (such as
        `breakpoints` and `notifications`), and `toolbar_extra` items are
        placed after the app's Share button. `authorize_component`,
        `components`, `store`, `user` and `include_page_dialog` are supplied
        by the app and raise a ValueError if given here.
        `dashboard`, `editable` and `read_only` are accepted but then managed
        by the app's routing and authorization. Read once at construction;
        the same editor is reused for every dashboard, so later changes have
        no effect.""",
    )

    home_dashboard = param.String(
        default=None,
        doc="""
        Dashboard shown on the homepage ('/'). Accepts a dashboard id or
        title. When unset, the homepage shows the dashboard grid launcher.""",
    )

    include_builtin_components = param.Boolean(
        default=True, doc="Include built-in widget components in the dashboard editor."
    )

    nav_variant = param.Selector(
        default="rail",
        objects=["left", "menubar", "rail", "right"],
        doc="""
        How navigation is rendered. 'rail' shows a navigation rail on the left
        (Home, Dashboards, Components, Pages, Help) and a breadcrumb with a
        dashboard switcher in the top bar; on narrow screens the rail moves
        behind a menu button. 'left' and 'right' dock a MenuList in a drawer
        on that side of the page; 'menubar' places a MenuBar in the page
        header with quick-action icons alongside it.""",
    )

    notifications = param.Boolean(
        default=True,
        doc="""
        Whether to surface user-facing messages as Panel notifications. When
        disabled (or when no notification area exists) messages are logged.""",
    )

    package = param.String(
        default=None,
        doc="""
        Importable package that `project_dir` is, if any. Modules are then named
        `<package>.<Section>.<name>` instead of relative to `project_dir`.""",
    )

    page_options = param.Dict(
        default={},
        doc="""
        Extra keyword arguments passed through to the underlying
        `panel_material_ui.Page`, overriding the app's own defaults.""",
    )

    project_dir = param.Path(doc="Path to the project directory.")

    propagate_failures = param.Boolean(
        default=False,
        doc="""
        Carry a failing output or edge transform downstream instead of leaving
        the connected inputs on their last value. Passed to the embedded
        editor, see `FlowDash.propagate_failures`.""",
    )

    settings_class = param.ClassSelector(
        class_=DashboardSettings,
        default=DashboardSettings,
        is_instance=False,
        doc="""
        The dashboard-wide settings class handed to the editor. Subclass
        `DashboardSettings` to persist application-specific values with each
        dashboard.""",
    )

    sidebar = Children(default=[], doc="Items prepended to the sidebar.")

    store = param.ClassSelector(
        class_=BaseDashboardStore, doc="Dashboard store instance for persistence."
    )

    title = param.String(default="FlowDash", doc="Application title shown in the browser tab.")

    _launcher: Launcher | None = None

    _main_task: asyncio.Task | None = None

    # Bumped by every navigation. A load that awaited something checks it before
    # touching the page, so a slow load cannot overwrite a newer navigation.
    _nav_generation: int = 0

    _RESERVED_EDITOR_OPTIONS = (
        "authorize_component",
        "components",
        "include_page_dialog",
        "store",
        "user",
    )

    def __init__(self, registry: dict[str, RegistryEntry] | None = None, **params):
        super().__init__(**params)

        if self.notifications:
            pn.config.notifications = True

        if self.auth_config is None:
            self.auth_config = AuthConfig()

        if registry is None:
            registry = build_registry(pathlib.Path(self.project_dir), package=self.package)
        page_entries = {k: v for k, v in registry.items() if v.metadata.page}
        # Session state is built from AST metadata — no imports needed.
        session_state_class = build_session_state_class(registry)
        self._registry = registry
        self._page_entries = page_entries
        # Pages hidden from this session's menus until their rules are resolved.
        self._pending_pages = [e for e in page_entries.values() if e.metadata.auth_pending]
        self._session_state_class = session_state_class

        self._session_state = self._session_state_class()
        self._identity = resolve_identity(self.auth_config)
        self._user_id = self._resolve_user_id()
        self._sidebar_container = pn.Column(sizing_mode="stretch_width")
        self._share_button = pmui.Button(
            label="Share",
            icon="share",
            variant="text",
            size="small",
            margin=(5, 2),
            visible=False,
        )
        self._share_button.on_click(lambda _event: self._share_current_dashboard())
        # The editor owns the canvas, tile grid and persistence; this class adds
        # routing, navigation, pages and authorization on top of it.
        self._editor = self._build_editor()
        self._editor._dashboard_bar.on_mode = self._on_dashboard_bar_mode
        self._editor.show_in_editor = self._show_in_editor
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
            anchor="left" if self.nav_variant == "left" else "right",
            inline=True,
            variant="docked",
            width_policy="fixed",
        )
        if self.nav_variant == "menubar":
            self._build_nav_bar()
        self._menubar_mode = self.nav_variant == "menubar"
        self._shell = AppShell(self) if self.nav_variant == "rail" else None
        self._command_palette = CommandPaletteController.install(self)
        self._build_dialog()
        self._build_unsaved_dialog()
        self._build_share_dialog()
        # Not in the page header: Material UI gives header children an inverted
        # palette, which would turn the dialog into a header-coloured box.
        shared_dialog = page_dialog()
        self._page_dialogs = [] if shared_dialog is None else [shared_dialog]
        self._page_dialogs.append(self._build_downloader())
        # The share dialog is a portaled overlay; mount it once inside the
        # always-present nav drawer rather than in each _page.main layout.
        page_kwargs = {
            "title": self.title,
            "theme_config": {"palette": {"primary": {"main": "#0072B5"}}},
            "sidebar_open": False,
            "sidebar": self.param.sidebar.rx() + [self._sidebar_container],  # noqa: RUF005
            "contextbar": self.param.contextbar.rx(),
            # A persistent contextbar reserves its width even while empty, which
            # pushes the main area past the viewport when the sidebar is open.
            "contextbar_variant": param.bind(
                lambda items: "persistent" if items else "temporary", self.param.contextbar
            ),
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
        if self._shell is not None:
            page_kwargs.setdefault("header", self._shell.header)
            page_kwargs["sx"] = {**HEADER_SX, **page_kwargs["sx"]}
        self._page_sx = page_kwargs["sx"]
        page_kwargs["sx"] = self._page_sx_for_sidebar()
        self._page = pmui.Page(**page_kwargs)
        self.param.watch(self._sync_sidebar_toggle, "sidebar")
        self._sidebar_container.param.watch(self._sync_sidebar_toggle, "objects")
        pn.state.onload(self._load_page_layout)

    def _page_sx_for_sidebar(self) -> dict:
        """Return the page's ``sx``, hiding the sidebar's menu button while the sidebar is empty.

        The sidebar keeps its (empty) container rather than dropping it: changing
        the page's children re-renders it, which blanks views that move between
        layouts in the same update.
        """
        if self.sidebar or self._sidebar_container.objects:
            return self._page_sx
        return {**self._page_sx, "& .header .MuiIconButton-edgeStart": {"display": "none"}}

    def _sync_sidebar_toggle(self, *_events):
        sx = self._page_sx_for_sidebar()
        if sx != self._page.sx:
            self._page.sx = sx

    def _begin_navigation(self) -> int:
        """Start a navigation, superseding the one in flight, and return its generation."""
        self._nav_generation += 1
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
        self._main_task = None
        # A dialog opened by the previous page's components must not outlive it,
        # nor an unanswered confirmation act on components that are being replaced.
        for dialog in self._page_dialogs:
            dialog.open = False
        return self._nav_generation

    def _is_current_navigation(self, generation: int) -> bool:
        return generation == self._nav_generation

    async def _run_navigation_task(self, coroutine):
        """Await *coroutine* as a task that the next navigation cancels.

        Returns ``None`` if it was cancelled by a newer navigation. If the task
        awaiting it is cancelled itself, e.g. when the session is torn down,
        the cancellation propagates (Python >= 3.11).
        """
        task = asyncio.create_task(coroutine)
        self._main_task = task
        try:
            return await task
        except asyncio.CancelledError:
            # Task.cancelling() is new in Python 3.11; before it the two cases
            # look the same from here, so the cancellation is always swallowed.
            cancelling = getattr(asyncio.current_task(), "cancelling", None)
            if cancelling is not None and cancelling():
                raise
            return None

    @asynccontextmanager
    async def _loading_screen(self, delay: float = 0.5):
        """Show a loading placeholder if the block takes longer than *delay* seconds."""
        generation = self._nav_generation

        async def _show_after_delay():
            await asyncio.sleep(delay)
            if not self._is_current_navigation(generation):
                return
            self._page.main = [
                pmui.LinearProgress(sizing_mode="stretch_width"),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
                self._share_dialog,
            ]

        task = asyncio.create_task(_show_after_delay())
        try:
            yield
        finally:
            task.cancel()

    def _nav_content(self, content):
        """Wrap page content with the navigation rail or docked nav drawer.

        In menubar mode navigation lives in the page header, so the content is
        returned as-is; in the 'rail' variant it follows the navigation rail
        and in the 'left'/'right' variants it is paired with the docked drawer
        on that side.
        """
        if self._shell is not None:
            return self._shell.wrap(content)
        if self._menubar_mode:
            return content
        if self.nav_variant == "left":
            objects = [self._nav_drawer, content]
        else:
            objects = [content, self._nav_drawer]
        return pn.Row(*objects, sizing_mode="stretch_both")

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
        """Whether the current identity may access a page/component entry.

        Never imports: an entry whose rules are still pending is denied here
        until :meth:`_resolve_pending_entries` or :meth:`_render_page` has
        imported it off the event loop.
        """
        return entry_authorized(
            entry, self._identity, default_allow=self._default_allow(), load=False
        )

    def _can_use_component(self, entry: RegistryEntry) -> bool:
        """Whether the current identity may place and see a component.

        A component without rules is open to everyone who may edit a
        dashboard, whatever ``default_allow`` says, so a private-by-default
        project keeps its unrestricted building blocks (and the built-in
        widgets) usable. Never imports, like :meth:`_can_access_entry`.
        """
        return entry_authorized(entry, self._identity, default_allow=True, load=False)

    @staticmethod
    def _unresolved(entry: RegistryEntry) -> bool:
        return entry.metadata.auth_pending and entry.load_error is None

    @staticmethod
    async def _import_pending(entry: RegistryEntry):
        """Import *entry* off the event loop so its decorator supplies the real rules."""
        try:
            await asyncio.to_thread(entry.load)
        except Exception:
            logger.exception(
                "Could not import '%s' to resolve its permissions; denying access", entry.app_id
            )

    async def _resolve_pending_entries(self):
        """Import pages whose rules are pending, off the event loop, then refresh the menus.

        Registry entries are shared across sessions, so each module is imported
        once per process; one whose import fails stays denied and is not retried.
        """
        if not self._pending_pages:
            return
        for entry in self._pending_pages:
            if self._unresolved(entry):
                await self._import_pending(entry)
        if all(entry.metadata.auth_pending for entry in self._pending_pages):
            return
        self._pending_pages = [e for e in self._pending_pages if e.metadata.auth_pending]
        menu_items = self._build_nav_menu_items()
        self._menu_list.param.update(
            items=menu_items,
            expanded=list(range(len(menu_items))),
            active=self._initial_menu_active(menu_items),
        )
        self._refresh_menu_bar()

    def _admin_groups(self) -> frozenset[str]:
        return self.auth_config.admin_groups if self.auth_config else frozenset()

    def _accessible_page_entries(self) -> dict[str, RegistryEntry]:
        """Page entries the current identity is authorized to see."""
        return {
            app_id: entry
            for app_id, entry in self._page_entries.items()
            if self._can_access_entry(entry)
        }

    def _can_administer_dashboard(self, dashboard: str | DashboardModel) -> bool:
        """Whether the current identity may rename/delete/share a dashboard.

        Pass the model when it is already loaded, as listings do, to skip a
        second store lookup per dashboard.
        """
        if isinstance(dashboard, DashboardModel):
            return self.store.can_administer_model(self._identity, dashboard, self._admin_groups())
        return self.store.can_administer(self._identity, dashboard, self._admin_groups())

    def _can_edit_dashboard(self, dashboard: str | DashboardModel) -> bool:
        """Whether the current identity may edit and save a dashboard's contents."""
        if isinstance(dashboard, DashboardModel):
            return self.store.can_edit_model(self._identity, dashboard, self._admin_groups())
        return self.store.can_edit(self._identity, dashboard, self._admin_groups())

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

        if self._unresolved(entry):
            # A failed import leaves the rules pending and access is refused below.
            await self._import_pending(entry)
        if not self._can_access_entry(entry):
            return self._access_denied_view(entry.title)

        try:
            async with self._loading_screen():
                return await self._run_navigation_task(
                    self._instantiate_entry(entry, context="page")
                )
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
        options = dict(self.editor_options)
        reserved = sorted(set(options) & set(self._RESERVED_EDITOR_OPTIONS))
        if reserved:
            raise ValueError(
                f"editor_options may not set {', '.join(map(repr, reserved))}; "
                "the app supplies these to the editor. Configure the registry, "
                "store and identity on FlowDashApp itself; the app lays out the "
                "page dialog."
            )
        toolbar_extra = [self._share_button, *(options.pop("toolbar_extra", None) or [])]
        editor = self.editor_class(
            authorize_component=self._can_use_component,
            components=self._registry,
            include_page_dialog=False,
            store=self.store,
            user=self._user_id,
            **{
                "include_builtin_components": self.include_builtin_components,
                "breakpoints": self.breakpoints,
                "notifications": self.notifications,
                "propagate_failures": self.propagate_failures,
                "settings_class": self.settings_class,
                **options,
                "toolbar_extra": toolbar_extra,
            },
        )
        editor.param.watch(self._on_editor_sidebar, ["controls_bar", "sidebar"])
        editor.param.watch(self._on_editor_saved, "saved")
        return editor

    def _on_editor_sidebar(self, _event):
        """Mirror the editor's sidebar views into the page sidebar, unless its controls bar shows them."""
        editor = self._editor
        views = [] if editor.controls_bar else list(editor.sidebar)
        self._sidebar_container.objects = views
        self._page.sidebar_open = bool(views)

    def _on_editor_saved(self, _event):
        """Follow the editor to a dashboard it saved under a new id ("Save as copy")."""
        current = self._current_dashboard
        location = pn.state.location
        if current is None or location is None:
            return
        if not location.pathname.startswith(DASH_ROUTE_PREFIX):
            return
        path = f"{DASH_ROUTE_PREFIX}{current.dashboard_id}"
        if location.pathname.rstrip("/") == path:
            return
        self._refresh_sidebar_dashboards()
        location.param.update(pathname=path, search="?edit=true")
        self._sync_menu_active(path)
        self._apply_layout_config(self._component_view, path)

    def _notify(self, severity: str, message: str, duration: int = 3000):
        """Surface a message to the user, or log it when notifications are unavailable."""
        notify(severity, message, duration=duration, enabled=self.notifications)

    def _notify_store_failure(self, action: str, exc: Exception) -> str:
        """Report a failed store operation and return the message shown to the user.

        Call it from the ``except`` block that caught *exc*.
        """
        if isinstance(exc, DashboardStoreError):
            logger.info("Store refused to %s dashboard: %s", action, exc)
            message = str(exc)
        else:
            logger.exception("Failed to %s dashboard", action)
            message = f"Could not {action} the dashboard. The error has been logged."
        self._notify("error", message, duration=5000)
        return message

    def _show_dialog_name_error(self, message: str):
        self._dialog_name_input.param.update(error_state=True, helper_text=message)

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

    def _save_current_dashboard(self) -> bool:
        """Save the loaded dashboard, refusing when the user only has read access.

        Returns whether the dashboard was saved.
        """
        current = self._current_dashboard
        if current is None:
            self._notify(
                "warning", "No dashboard loaded. Create one from the sidebar.", duration=4000
            )
            return False
        if not self._can_edit_dashboard(current.dashboard_id):
            self._editor.read_only = True
            self._notify("error", "You have view-only access to this dashboard.", duration=4000)
            return False
        self._editor.read_only = False
        return self._editor._on_save_clicked()

    def _share_current_dashboard(self):
        if self._current_dashboard is None:
            self._notify("warning", "No dashboard loaded.", duration=3000)
            return
        self._open_share_dialog(
            self._current_dashboard.dashboard_id, self._current_dashboard.title
        )

    async def _load_dashboard(
        self, dashboard_id: str, edit: bool = False, generation: int | None = None
    ):
        """Load a dashboard into the page, unless navigation *generation* was superseded."""
        if generation is None:
            generation = self._nav_generation
        dashboard = self.store.load_for_access(
            self._identity, dashboard_id, default_allow=self._default_allow()
        )
        if dashboard is not None:
            # Only the components this dashboard places, imported off the event
            # loop. The editor palette needs no imports (it is built from the
            # scanned metadata) and `add_component` imports on demand, so even
            # edit mode does not pay for the whole catalog.
            await self._ensure_components_loaded({item.component_id for item in dashboard.items})
        if not self._is_current_navigation(generation):
            return
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
        if edit and dashboard is not None and not self._can_edit_dashboard(dashboard_id):
            # A viewer with read access followed an edit link; downgrade to view.
            edit = False
        if dashboard is None:
            self._page.main = [
                self._nav_content(
                    pn.pane.Alert(f"Dashboard not found: {dashboard_id}", alert_type="danger")
                ),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
                self._share_dialog,
            ]
            return

        # Before loading, so only someone who may save it is told about items it
        # could not restore.
        self._editor.read_only = not self._can_edit_dashboard(dashboard_id)
        self._editor.load_model(dashboard)
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
            *self._page_dialogs,
            self._share_dialog,
        ]

    def _create_new_dashboard(self, title_str: str) -> bool:
        """Create a dashboard and open it for editing, returning whether it was created."""
        title_str = title_str.strip()
        if not title_str:
            self._notify("warning", "Dashboard title cannot be empty.", duration=3000)
            return False
        try:
            dashboard = self.store.create_dashboard(self._editor.user, title_str)
        except Exception as exc:
            self._show_dialog_name_error(self._notify_store_failure("create", exc))
            return False
        self._begin_navigation()
        self._editor._start_dashboard(dashboard)
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
            *self._page_dialogs,
            self._share_dialog,
        ]
        return True

    def _delete_dashboard(self, dashboard_id: str) -> bool:
        """Delete a dashboard, returning whether it was deleted."""
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to delete this dashboard.")
            return False
        was_current = bool(
            self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id
        )
        try:
            owner = self.store.get_owner(dashboard_id) or self._user_id
            deleted = self.store.delete_dashboard(owner, dashboard_id)
        except Exception as exc:
            self._notify_store_failure("delete", exc)
            return False
        if not deleted:
            self._notify("error", "The dashboard could not be deleted.", duration=5000)
            self._refresh_sidebar_dashboards()
            return False
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
                *self._page_dialogs,
                self._share_dialog,
            ]
        return True

    def _rename_dashboard(self, dashboard_id: str, new_title: str) -> bool:
        """Rename a dashboard, returning whether it was renamed."""
        new_title = new_title.strip()
        if not new_title:
            return False
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to rename this dashboard.")
            return False
        try:
            owner = self.store.get_owner(dashboard_id) or self._user_id
            renamed = self.store.rename_dashboard(owner, dashboard_id, new_title)
        except Exception as exc:
            self._show_dialog_name_error(self._notify_store_failure("rename", exc))
            return False
        if not renamed:
            message = "The dashboard could not be renamed."
            self._notify("error", message, duration=5000)
            self._show_dialog_name_error(message)
            self._refresh_sidebar_dashboards()
            return False
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.title = new_title
        self._refresh_sidebar_dashboards()
        return True

    def _refresh_sidebar_dashboards(self):
        dash_items = self._get_dashboard_menu_items()
        items = list(self._menu_list.items)
        items[-1] = {**items[-1], "items": dash_items}
        self._menu_list.items = items
        self._refresh_menu_bar()
        if self._shell is not None:
            self._shell.refresh()
        self._editor._dashboard_bar.sync()
        location = pn.state.location
        if self._launcher is not None and location is not None and location.pathname == "/":
            self._launcher.refresh()

    def _build_launcher(self):
        """Return the launcher page, refreshed from the store; one instance per session."""
        if self._launcher is None:
            self._launcher = Launcher(app=self)
        else:
            self._launcher.refresh()
        return self._launcher

    def _launcher_navigate(self, path, *_args):
        self._request_navigation(path)

    def _apply_layout_config(self, content, route):
        """Run the ``configure_layout`` hook for the current navigation, if any."""
        if self.configure_layout is None:
            return
        try:
            self.configure_layout(self, content, route)
        except Exception:
            logger.exception("configure_layout hook failed for route '%s'", route)

    async def _show_home_dashboard(self, generation: int) -> bool:
        """Render the configured home dashboard on '/'. Returns whether it was shown.

        Falls back to the launcher grid (by returning ``False``) if the
        configured dashboard cannot be resolved or the current identity is not
        authorized to view it.
        """
        model = self.store.find_by_id_or_title(
            self.home_dashboard, self._identity, default_allow=self._default_allow()
        )
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
            await self._run_navigation_task(
                self._load_dashboard(model.dashboard_id, edit=False, generation=generation)
            )
        return True

    async def _load_page_layout(self):
        if pn.state.location is None:
            return
        generation = self._begin_navigation()
        pathname = pn.state.location.pathname
        # Importing pending pages awaits, so a newer navigation may have started.
        await self._resolve_pending_entries()
        if not self._is_current_navigation(generation):
            return

        if pathname == "/":
            if self.home_dashboard and await self._show_home_dashboard(generation):
                return
            if not self._is_current_navigation(generation):
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
                *self._page_dialogs,
            ]
            return

        if pathname == COMPONENTS_ROUTE:
            # On Panel 1.10 the page update below never reaches the browser when
            # it is made in the same tick as the pathname change, so let that go
            # out first.
            await asyncio.sleep(0)
            if not self._is_current_navigation(generation):
                return
            self._current_dashboard = None
            # Arriving from a dashboard must not keep its components running.
            self._reset_canvas()
            gallery = ComponentGallery(self)
            async with self._loading_screen():
                await self._run_navigation_task(gallery.load())
            if not self._is_current_navigation(generation):
                return
            self._apply_layout_config(gallery, pathname)
            self._page.main = [
                self._nav_content(gallery),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
            ]
            return

        if pathname.startswith(DASH_ROUTE_PREFIX):
            dashboard_id = pathname[len(DASH_ROUTE_PREFIX) :].strip("/")
            if dashboard_id:
                search = pn.state.location.search or ""
                edit_requested = "edit=true" in search
                async with self._loading_screen():
                    await self._run_navigation_task(
                        self._load_dashboard(
                            dashboard_id, edit=edit_requested, generation=generation
                        )
                    )
                return

        self._current_dashboard = None

        self._sidebar_container.objects = []
        key = tuple(pathname.strip("/").split("/"))
        if len(key) == 2 and self._entry_from_key(key):
            content = await self._render_page(key)
            if not self._is_current_navigation(generation):
                return
            self._apply_layout_config(content, pathname)
            wrapper = pmui.Column(content, sizing_mode="stretch_width")
            self._page.main = [
                self._nav_content(wrapper),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
            ]
        else:
            self._apply_layout_config(None, pathname)
            main = [
                f"Invalid URL: {pathname}",
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
            ]
            if self._shell is not None:
                main[0] = self._nav_content(main[0])
            elif not self._menubar_mode:
                main.append(self._nav_drawer)
            self._page.main = main

    @pn.io.hold()
    def _show_edit_mode(self):
        current = self._current_dashboard
        _can_admin, can_edit = (
            self._dashboard_rights(current.dashboard_id) if current else (False, False)
        )
        self._share_button.visible = False
        self._editor.param.update(editable=True, read_only=not can_edit)
        if pn.state.location is not None:
            pn.state.location.param.update(search="?edit=true")

    @pn.io.hold()
    def _show_view_mode(self):
        current = self._current_dashboard
        can_admin, can_edit = (
            self._dashboard_rights(current.dashboard_id) if current else (False, False)
        )
        self._share_button.visible = can_admin
        # The editor treats a read-only session as a viewer, e.g. for inspecting
        # outputs. Without a dashboard (the components route) there is nothing to refuse.
        self._editor.param.update(editable=False, read_only=current is not None and not can_edit)
        if pn.state.location is not None:
            pn.state.location.param.update(search="")

    def _on_dashboard_bar_mode(self, mode: str):
        """Switch modes from the dashboard bar, keeping ``?edit=true`` in step.

        Leaving edit mode with unsaved changes goes through the unsaved-changes
        dialog, whose answers reload the dashboard in view mode.
        """
        location = pn.state.location
        if mode == "edit":
            self._show_edit_mode()
        elif self._dirty and self._current_dashboard is not None and location is not None:
            self._request_navigation(location.pathname)
        else:
            self._show_view_mode()

    def _show_in_editor(self, _instance_id: str):
        """Open the current dashboard on its wiring canvas, from a tile's "Show in editor"."""
        if self._current_dashboard is None:
            return
        self._editor.mode = "wiring"
        self._show_edit_mode()

    def _dashboard_rights(self, dashboard: str | DashboardModel) -> tuple[bool, bool]:
        """Whether the current identity may administer and may edit a dashboard."""
        if isinstance(dashboard, str):
            dashboard = self.store._load_any(dashboard)
            if dashboard is None:
                return False, False
        if self._can_administer_dashboard(dashboard):
            return True, True
        return False, self._can_edit_dashboard(dashboard)

    _ADMIN_DASHBOARD_ACTIONS = (
        {"label": "Edit", "icon": "edit"},
        {"label": "Rename", "icon": "drive_file_rename_outline"},
        {"label": "Duplicate", "icon": "content_copy"},
        {"label": "Share", "icon": "person_add"},
        {"label": "Download as file", "icon": "download"},
        {"label": "Delete", "icon": "delete", "color": "error"},
    )

    _EDITOR_DASHBOARD_ACTIONS = ({"label": "Edit", "icon": "edit"},)

    _VIEWER_DASHBOARD_ACTIONS = (
        {"label": "Duplicate", "icon": "content_copy"},
        {"label": "Download as file", "icon": "download"},
    )

    def _dashboard_actions(self, can_admin: bool, can_edit: bool = False) -> tuple:
        """Menu actions for a dashboard, gated by administration and edit rights.

        An editor gets ``_EDITOR_DASHBOARD_ACTIONS`` followed by the viewer
        actions it does not already have.
        """
        if can_admin:
            return self._ADMIN_DASHBOARD_ACTIONS
        if not can_edit:
            return self._VIEWER_DASHBOARD_ACTIONS
        keys = {self._action_key(action) for action in self._EDITOR_DASHBOARD_ACTIONS}
        return (
            *self._EDITOR_DASHBOARD_ACTIONS,
            *(a for a in self._VIEWER_DASHBOARD_ACTIONS if self._action_key(a) not in keys),
        )

    @staticmethod
    def _action_key(action: dict) -> str:
        """Return the key an action dispatches under, matching pmui's ``on_action`` lookup."""
        return action.get("action", action["label"])

    def _get_dashboard_menu_items(self) -> list[dict]:
        items = []
        dashboards = self.store.list_accessible(
            self._identity, default_allow=self._default_allow()
        )
        for d in dashboards:
            item = {
                "icon": "dashboard",
                "label": d.title,
                "path": f"{DASH_ROUTE_PREFIX}{d.dashboard_id}",
                "disable_link": True,
            }
            actions = self._dashboard_actions(*self._dashboard_rights(d))
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

    def _on_dashboard_action(self, action: str, dashboard_id: str, title: str):
        """Run the dashboard *action* on a dashboard.

        Every entry point (launcher card, nav drawer, header MenuBar) decodes its
        event and calls this. A subclass adds an action by extending
        ``_ADMIN_DASHBOARD_ACTIONS``, ``_EDITOR_DASHBOARD_ACTIONS`` or
        ``_VIEWER_DASHBOARD_ACTIONS``, then overrides this method to handle it
        and calls ``super()`` for the rest. The nav drawer registers a handler
        for every action of the three roles.

        *action* is the key from :meth:`_action_key`: the action dict's
        ``"action"`` entry if it has one, otherwise its ``"label"``. That is the
        same lookup pmui's ``MenuList.on_action`` uses.
        """
        if action == "Open":
            self._request_navigation(f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        elif action == "Edit":
            self._edit_dashboard(dashboard_id)
        elif action == "Rename":
            self._open_rename_dialog(dashboard_id, title)
        elif action == "Duplicate":
            self._duplicate_dashboard(dashboard_id)
        elif action == "Share":
            self._open_share_dialog(dashboard_id, title)
        elif action == "Download as file":
            self._download_dashboard(dashboard_id)
        elif action == "Delete":
            self._open_delete_dialog(dashboard_id, title)

    def _readable_dashboard(self, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard the current identity may open, or notify that it is gone."""
        model = self.store.load_for_access(
            self._identity, dashboard_id, default_allow=self._default_allow()
        )
        if model is None:
            self._notify("error", "The dashboard no longer exists or is not shared with you.")
        return model

    def _duplicate_dashboard(self, dashboard_id: str) -> DashboardModel | None:
        """Copy a dashboard as "<title> copy", owned by the current user and not shared."""
        source = self._readable_dashboard(dashboard_id)
        if source is None:
            return None
        title = unique_title(self.store, self._user_id, f"{source.title} copy")
        try:
            model = copy_dashboard(self.store, source, self._user_id, title)
        except Exception as exc:
            self._notify_store_failure("duplicate", exc)
            return None
        self._refresh_sidebar_dashboards()
        self._notify("success", f'Created "{model.title}".', duration=3000)
        return model

    def _download_dashboard(self, dashboard_id: str) -> bool:
        """Download the saved dashboard as the file the editor's Download button writes."""
        model = self._readable_dashboard(dashboard_id)
        if model is None:
            return False
        send_download(self._downloader, model)
        return True

    def _import_dashboard_file(self, filename: str, content: str | bytes) -> DashboardModel | None:
        """Save a downloaded dashboard file as a new dashboard of the user and open it."""
        try:
            parsed = parse_dashboard_file(content, self._user_id)
        except ValueError as exc:
            self._notify("error", f"Could not import {filename}: {exc}", duration=5000)
            return None
        title = unique_title(self.store, self._user_id, parsed.title)
        try:
            model = copy_dashboard(self.store, parsed, self._user_id, title)
        except Exception as exc:
            self._notify_store_failure("import", exc)
            return None
        self._refresh_sidebar_dashboards()
        self._notify("success", f'Imported "{model.title}".', duration=3000)
        self._request_navigation(f"{DASH_ROUTE_PREFIX}{model.dashboard_id}")
        return model

    def _build_downloader(self):
        """Build the hidden download behind "Download as file", mounted on every page."""
        self._downloader = build_downloader()
        return self._downloader

    def _edit_dashboard(self, dashboard_id: str):
        self._nav_drawer.open = False
        target_path = f"{DASH_ROUTE_PREFIX}{dashboard_id}"
        if self._dirty and self._current_dashboard is not None:
            self._request_navigation(target_path, search="?edit=true")
            return
        if pn.state.location:
            pn.state.location.param.update(pathname=target_path, search="?edit=true")
        pn.state.execute(partial(self._load_dashboard_edit, dashboard_id))

    async def _load_dashboard_edit(self, dashboard_id: str):
        generation = self._begin_navigation()
        await self._run_navigation_task(
            self._load_dashboard(dashboard_id, edit=True, generation=generation)
        )

    @pn.io.hold()
    def _open_rename_dialog(self, dashboard_id: str, title: str):
        self._reset_dialog({"action": "rename", "dashboard_id": dashboard_id}, title)
        self._dialog.title = "Rename dashboard"
        self._nav_drawer.open = False
        self._dialog.open = True

    @pn.io.hold()
    def _open_delete_dialog(self, dashboard_id: str, title: str):
        self._reset_dialog({"action": "delete", "dashboard_id": dashboard_id}, title)
        self._dialog_message.object = self._delete_consequence(dashboard_id)
        self._dialog.title = f"Delete {title}?"
        self._nav_drawer.open = False
        self._dialog.open = True

    def _delete_consequence(self, dashboard_id: str) -> str:
        """Describe what deleting the dashboard removes, with counts when it loads."""
        try:
            model = self.store.load_for_access(
                self._identity, dashboard_id, default_allow=self._default_allow()
            )
        except Exception:
            logger.exception("Failed to load dashboard %s for the delete dialog", dashboard_id)
            model = None
        if model is None or not (model.items or model.edges):
            subject = "It is"
        else:
            n_items, n_edges = len(model.items), len(model.edges)
            subject = (
                f"Its {n_items} component{'s' * (n_items != 1)} and "
                f"{n_edges} connection{'s' * (n_edges != 1)} are"
            )
        return f"{subject} removed for everyone it is shared with. This can't be undone."

    def _on_nav_menu_action(self, action: str, item: dict):
        """Decode a nav drawer action into a dashboard id and dispatch it."""
        path = item.get("path", "")
        if path == "__new_dashboard__":
            if action == "Create":
                self._open_create_dialog()
            return
        dashboard_id = self._dashboard_id_from_path(path)
        if dashboard_id:
            self._on_dashboard_action(action, dashboard_id, item.get("label", ""))

    @pn.io.hold()
    def _open_create_dialog(self):
        self._reset_dialog({"action": "create"}, "")
        self._dialog.title = "Create dashboard"
        self._dialog.open = True
        self._nav_drawer.open = False

    def _reset_dialog(self, context: dict, subject: str):
        """Prepare the dashboard dialog for *context*, acting on the dashboard titled *subject*."""
        action = context["action"]
        deleting = action == "delete"
        # The context must be in place before the input value changes, since
        # the value watcher validates against it (rename excludes its own id).
        self._dialog_context = context
        self._dialog_subject = subject
        self._dialog_name_input.param.update(value=subject, visible=not deleting)
        self._dialog_delete_input.param.update(value="", placeholder=subject, visible=deleting)
        self._dialog_name_input.param.update(error_state=False, helper_text="")
        self._dialog_message.param.update(object="", visible=deleting)
        label, color = {
            "create": ("Create", "primary"),
            "rename": ("Rename", "primary"),
            "delete": ("Delete dashboard", "error"),
        }[action]
        self._dialog_confirm_button.param.update(
            label=label, color=color, disabled=not self._dialog_valid()
        )

    def _validate_dashboard_name(self, title: str) -> str | None:
        """Return an error message if the title is invalid, else None."""
        title = title.strip()
        if not title:
            return "Name cannot be empty."
        exclude_id = self._dialog_context.get("dashboard_id")
        if self.store.title_exists(self._user_id, title, exclude_id=exclude_id):
            return "A dashboard with this name already exists."
        return None

    def _dialog_valid(self) -> bool:
        """Whether the dialog's primary action may run with the current input."""
        action = self._dialog_context.get("action")
        if action == "delete":
            return self._dialog_delete_input.value_input == self._dialog_subject
        if action in ("create", "rename"):
            return self._validate_dashboard_name(self._dialog_name_input.value_input or "") is None
        return False

    def _on_dialog_name_changed(self, event):
        if event.obj is self._dialog_name_input:
            error = self._validate_dashboard_name(event.new or "")
            self._dialog_name_input.param.update(
                error_state=error is not None, helper_text=error or ""
            )
        self._dialog_confirm_button.disabled = not self._dialog_valid()

    @pn.io.hold()
    def _on_dialog_confirm(self, _event):
        ctx = self._dialog_context
        if not ctx:
            return
        action = ctx.get("action")
        name = self._dialog_name_input.value_input or ""
        if action in ("create", "rename"):
            error = self._validate_dashboard_name(name)
            if error:
                self._show_dialog_name_error(error)
                return
        elif not self._dialog_valid():
            return
        done = False
        did = ctx.get("dashboard_id", "")
        if action == "create":
            done = self._create_new_dashboard(name)
        elif action == "rename":
            done = name.strip() == self._dialog_subject or (
                bool(did) and self._rename_dashboard(did, name)
            )
        elif action == "delete":
            done = bool(did) and self._delete_dashboard(did)
        if not done:
            return
        self._dialog.open = False
        self._dialog_name_input.param.update(error_state=False, helper_text="")
        self._dialog_context = {}

    def _build_dialog(self):
        self._dialog_context: dict = {}
        self._dialog_subject = ""
        self._dialog_name_input = pmui.TextInput(label="Name", sizing_mode="stretch_width")
        self._dialog_message = pmui.Typography(
            variant="body2", sx={"color": "text.secondary"}, visible=False
        )
        self._dialog_delete_input = pmui.TextInput(
            label="Type the name to confirm", sizing_mode="stretch_width", visible=False
        )
        self._dialog_confirm_button = pmui.Button(label="Create", color="primary", disabled=True)
        cancel_btn = pmui.Button(label="Cancel", variant="text")
        for widget in (self._dialog_name_input, self._dialog_delete_input):
            widget.param.watch(self._on_dialog_name_changed, "value_input")
            widget.param.watch(self._on_dialog_confirm, "enter_pressed")
        self._dialog_confirm_button.on_click(self._on_dialog_confirm)
        cancel_btn.on_click(lambda _: setattr(self._dialog, "open", False))
        self._dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    self._dialog_message,
                    self._dialog_name_input,
                    self._dialog_delete_input,
                    pn.Row(
                        pn.layout.HSpacer(),
                        cancel_btn,
                        self._dialog_confirm_button,
                        sizing_mode="stretch_width",
                    ),
                    sizing_mode="stretch_width",
                )
            ],
            title="Dashboard",
            open=False,
            min_width=400,
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
        self._share_edit_groups = pmui.MultiChoice(
            label="Editor groups",
            helper_text="May also edit and save, but not rename, delete or share.",
            **common,
        )
        self._share_edit_users = pmui.MultiChoice(
            label="Editor users",
            helper_text="May also edit and save, but not rename, delete or share.",
            **common,
        )
        self._share_widgets = (
            self._share_allow_groups,
            self._share_allow_users,
            self._share_deny_groups,
            self._share_deny_users,
            self._share_edit_groups,
            self._share_edit_users,
        )
        confirm_btn = pmui.Button(label="Save sharing", color="primary")
        cancel_btn = pmui.Button(label="Cancel", color="light")
        confirm_btn.on_click(self._on_share_confirm)
        cancel_btn.on_click(lambda _: setattr(self._share_dialog, "open", False))
        self._share_dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    pmui.Typography(
                        "Grant access by group or user. With no view rules the "
                        "project default applies. Editors can always view.",
                        variant="body2",
                        styles={"opacity": "0.7"},
                    ),
                    self._share_allow_groups,
                    self._share_allow_users,
                    self._share_edit_groups,
                    self._share_edit_users,
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
            (self._share_edit_groups, known_groups, perm.allow_edit_groups),
            (self._share_edit_users, known_users, perm.allow_edit_users),
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
            allow_edit_groups=self._share_edit_groups.value,
            allow_edit_users=self._share_edit_users.value,
        )
        try:
            updated = self.store.set_permission(dashboard_id, permission)
        except Exception as exc:
            self._notify_store_failure("share", exc)
            return
        if not updated:
            self._notify("error", "Sharing could not be updated.", duration=5000)
            return
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.permission = permission
        self._share_dialog.open = False
        self._share_context = {}
        self._refresh_sidebar_dashboards()
        self._notify("success", "Sharing updated.", duration=3000)

    def _build_unsaved_dialog(self):
        self._pending_navigation: tuple[str, str] | None = None

        discard_btn = pmui.Button(label="Discard", color="danger", variant="outlined")
        save_btn = pmui.Button(label="Save & Continue", color="primary")
        stay_btn = pmui.Button(label="Cancel", color="light")

        def _on_discard(_event):
            self._unsaved_dialog.open = False
            self._dirty = False
            pending = self._pending_navigation
            self._pending_navigation = None
            if pending:
                self._navigate_to(*pending)

        def _on_save(_event):
            self._unsaved_dialog.open = False
            if not self._save_current_dashboard():
                self._pending_navigation = None
                return
            pending = self._pending_navigation
            self._pending_navigation = None
            if pending:
                self._navigate_to(*pending)

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

    def _navigate_to(self, path: str, search: str = ""):
        if pn.state.location is None:
            return
        pn.state.location.param.update(pathname=path, search=search)
        self._sync_menu_active(path)
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None:
            # Loading runs without the document lock, so started right away it
            # can send the new page before the changes above (URL, breadcrumb)
            # leave, and the browser rejects it: "reference ... isn't known".
            # A locked next-tick callback runs once they went out.
            doc.add_next_tick_callback(partial(pn.state.execute, self._load_page_layout))
        else:
            pn.state.execute(self._load_page_layout)

    def _sync_menu_active(self, path: str):
        if self._shell is not None:
            self._shell.sync(path)
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

    def _request_navigation(self, path: str, search: str = ""):
        if self._dirty and self._current_dashboard is not None:
            self._pending_navigation = (path, search)
            self._unsaved_dialog.open = True
        else:
            self._navigate_to(path, search)

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

        actions = (
            *self._dashboard_actions(True),
            *self._dashboard_actions(False, True),
            *self._dashboard_actions(False),
        )
        # "Create" lives on the fixed "New Dashboard" row, not in the action tuples.
        keys = dict.fromkeys([*(self._action_key(action) for action in actions), "Create"])
        for key in keys:
            self._menu_list.on_action(key, partial(self._on_nav_menu_action, key))

        return self._menu_list

    def _build_menu_bar_items(self) -> list[dict]:
        """MenuBar equivalent of the nav tree, with dashboard management submenus.

        MenuBar has no inline action buttons, so each dashboard is exposed as a
        submenu (Open plus the actions from :meth:`_dashboard_actions`) and the
        action key is encoded via a ``nav_action`` key routed in
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
            for action in self._dashboard_actions(*self._dashboard_rights(d)):
                entries.append(
                    {
                        **{key: action[key] for key in ("label", "icon") if key in action},
                        "path": path,
                        "nav_action": self._action_key(action),
                        "title": d.title,
                    }
                )
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
        dashboard_id = self._dashboard_id_from_path(item.get("path", ""))
        if dashboard_id:
            self._on_dashboard_action(action, dashboard_id, item.get("title", ""))

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
            registry = build_registry(pathlib.Path(project_dir), package=params.get("package"))

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
