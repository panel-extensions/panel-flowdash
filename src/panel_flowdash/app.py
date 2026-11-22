"""Application builder: scans a project directory and constructs the Panel app."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import os
import pathlib
import traceback
import typing as t
from contextlib import asynccontextmanager
from functools import cache, partial
from urllib.parse import parse_qs, parse_qsl, urlencode

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Children, Viewer

from panel_flowdash.admin.access import (
    AdminTokenExchange,
    is_admin,
    show_admin_link,
)
from panel_flowdash.admin.page import ADMIN_ROUTE, AdminPage
from panel_flowdash.admin.sections import (
    FontLoader,
    claim_admin,
    font_href,
    project_admins,
    project_overrides,
)
from panel_flowdash.auth import (
    ANONYMOUS_USER,
    AuthConfig,
    Permission,
    entry_authorized,
    is_authorized,
    resolve_identity,
)
from panel_flowdash.component_library import merge_libraries
from panel_flowdash.dashboard_store import (
    BaseDashboardStore,
    DashboardModel,
    DashboardStoreError,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.project import (
    PROJECT_FILE,
    Project,
    ProjectError,
    _parse as parse_project_data,
    load_project,
)
from panel_flowdash.registry import RegistryEntry, build_registry
from panel_flowdash.session_state import build_session_state_class, check_requirements
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.ui.command_palette import CommandPaletteController
from panel_flowdash.ui.dashboard_bar import (
    COMPACT_BREAKPOINT,
    EDIT_COMPACT_BREAKPOINT,
    ICON_ONLY_WHEN_COMPACT,
    LABEL_LINE_HEIGHT,
    TOOLTIP_KEYFRAMES,
    tooltip_css,
)
from panel_flowdash.ui.gallery import ComponentGallery
from panel_flowdash.ui.launcher import (
    Launcher,
    build_downloader,
    copy_dashboard,
    parse_dashboard_file,
    send_download,
    unique_title,
)
from panel_flowdash.ui.settings_dialog import SharingAccess, share_link
from panel_flowdash.ui.shell import (
    DASHBOARD_EDIT_HEADER_SX,
    DASHBOARD_HEADER_SX,
    HEADER_SX,
    AppShell,
)
from panel_flowdash.ui.states import (
    LeavePageGuard,
    LoadingSkeleton,
    UnsavedChangesDialog,
    access_denied,
    dashboard_not_found,
    describe_changes,
    page_not_found,
    similar_dashboards,
)
from panel_flowdash.ui.text import (
    alert_pane,
    code_span,
    escape_html,
    escape_markdown,
    markdown_pane,
)
from panel_flowdash.util import notify, page_dialog, panel_call, panel_viewer
from panel_flowdash.value_text import error_text, redact_secrets
from panel_flowdash.widgets import BUILTIN_COMPONENTS

logger = logging.getLogger("panel_flowdash")

if t.TYPE_CHECKING:

    class _DASHBOARD_ACTION_TYPE(t.TypedDict):
        label: str
        icon: str


ACCESS_DENIED_ROUTE = "/access-denied"
COMPONENTS_ROUTE = "/components"
DASH_ROUTE_PREFIX = "/dash/"
#: Opens a dashboard in the full screen layout, e.g. for a wall display.
KIOSK_QUERY = "kiosk=1"


def _merged(base: dict | None, update: dict | None) -> dict:
    """Return *base* with *update* merged into it, nested dicts merged key by key."""
    merged = dict(base or {})
    for key, value in (update or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merged(merged[key], value)
        else:
            merged[key] = value
    return merged


# The project file each served directory writes when it has none yet, shared by its sessions.
_SETTINGS_PROJECTS: dict[str, Project] = {}


def _unloaded_project(path: pathlib.Path, context: dict) -> Project | None:
    """Read the project file at *path* without loading its sections.

    Each section is loaded when it is first asked for, so one the file gets
    wrong is reported where it is used rather than stopping the app.
    """
    try:
        data, positions = parse_project_data(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeDecodeError, ProjectError) as exc:
        logger.warning("Not reading the project file %s: %s", path, exc)
        return None
    return Project(str(path), data, positions, context)


def _is_kiosk(search: str | None) -> bool:
    return parse_qs((search or "").lstrip("?")).get("kiosk") == ["1"]


def _without_query(search: str | None, *names: str) -> str:
    """*search* without the parameters *names*, keeping every other one in order."""
    kept = [
        (k, v)
        for k, v in parse_qsl((search or "").lstrip("?"), keep_blank_values=True)
        if k not in names
    ]
    return f"?{urlencode(kept)}" if kept else ""


# The assistant's header toggle drops its label where the other top bar actions
# do: below COMPACT_BREAKPOINT, and below EDIT_COMPACT_BREAKPOINT beside the edit
# actions. It sits at the end of the bar, so its tooltip opens to its left.
_EDIT_HEADER_CLASS = "fd-edit-header"
_TOGGLE_ICON_ONLY = f"""
  .pa-toggle > span:not(.pa-badge) {{ display: none; }}
  .pa-toggle {{ padding: 6px; min-height: calc({LABEL_LINE_HEIGHT} + 12px); }}
  {tooltip_css(".pa-toggle{state}", "attr(aria-label)", placement="left")}
"""
_TOGGLE_ICON_ONLY_WHEN_COMPACT = f"""
{TOOLTIP_KEYFRAMES}
@media (max-width: {COMPACT_BREAKPOINT - 0.05}px) {{ {_TOGGLE_ICON_ONLY} }}
@media (max-width: {EDIT_COMPACT_BREAKPOINT - 0.05}px) {{
  :host(.{_EDIT_HEADER_CLASS}) {{ {_TOGGLE_ICON_ONLY} }}
}}
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


# Logged when an agent is configured but its dependencies are missing.
AGENT_EXTRA_MISSING = (
    "The FlowDash agent needs the agent extra: "
    "pip install 'panel-flowdash[agent]', plus the client of your model provider, "
    "e.g. 'pydantic-ai-slim[openai]'"
)


class FlowDashApp(Viewer):
    """FlowDash application: scans a project directory and serves its pages and components."""

    admin_link = param.Boolean(
        default=True,
        doc="""
        Whether the admin link can claim the admin view: ``/admin#token=...``
        with the server's admin token, which `flowdash serve` prints at
        startup while the project has no admin (see
        `panel_flowdash.admin.admin_path`). Whoever opens it is added to the
        project file's ``general.admins`` and is admin from then on: the
        signed-in user, or without sign-in the account that runs the server.
        Members of the `auth_config`'s ``admin_groups`` are admins either way.""",
    )

    agent = param.Parameter(
        default=None,
        doc="""
        The AI agent that helps the signed-in user build and change
        dashboards, with that user's rights (needs ``panel-flowdash[agent]``).
        None gives the default agent when `agent_model` (or the
        ``FLOWDASH_AGENT_MODEL`` environment variable) names a model and no
        agent otherwise; True requires a model; a string is the default
        agent's model, e.g. ``"openai:gpt-5"``; False turns the agent off; a
        ``pydantic_ai.Agent`` built with `panel_flowdash.agent.create_agent`
        (or taking ``FlowDashDeps``) is used as is.""",
    )

    explore_agent = param.Parameter(
        default=None,
        doc="""
        The AI agent the Explore view asks, which answers questions about
        the data behind the open dashboard, separately from the assistant
        in the drawer, which edits it. Each has its own runs, and the
        picked model is shared. None gives the default Explore agent on the
        assistant's model (see `panel_flowdash.agent.create_explore_agent`)
        whenever there is an assistant; False turns asking in the Explore
        view off, leaving it a read-only record of the dashboard's turns; a
        ``pydantic_ai.Agent`` taking ``FlowDashDeps`` is used as is.""",
    )

    agent_authorize = param.Callable(
        default=None,
        doc="""
        Called with the signed-in `Identity` to decide whether this session
        gets the agent, e.g. to roll it out to some groups first. None offers
        it to everyone when an agent is configured.""",
    )

    agent_sensitive_components = param.List(
        default=[],
        item_type=str,
        doc="""
        Ids or glob patterns (``"Sources/*"``) of components whose config can
        reach outside the dashboard, such as a SQL source or one fetching a
        URL; former ids of a renamed component count too. The agent needs the
        user's approval to place one, preview or load YAML that places one,
        change its config or connect an edge into it.""",
    )

    agent_approval_rules = param.List(
        default=[],
        doc="""
        More calls the agent needs the user's approval for, on top of saving,
        deleting, sharing and the sensitive components: tool names, or
        ``panel_agents.ApprovalRule`` objects for calls with particular
        arguments, e.g. ``ApprovalRule("run_command", when=lambda args:
        args["command"] == "action:refresh")``.""",
    )

    agent_model = param.String(
        default=None,
        allow_None=True,
        doc="""
        The model of the default agent, e.g. ``"anthropic:<model>"`` or
        ``"openai:<model>"``. Defaults to the ``FLOWDASH_AGENT_MODEL``
        environment variable.""",
    )

    agent_models = param.List(
        default=[],
        doc="""
        Models the person using the assistant may pick from, in a small
        select below the chat's input that shows when there are two or
        more: pydantic-ai model strings such as ``"openai:<model>"``, dicts
        with an ``id`` and optional ``label`` and ``description`` (e.g. a
        cost hint), or ``panel_agents.ModelChoice`` objects. `agent_model`
        is the default and is offered first if it is not listed; without
        it the first model is the default. Defaults to the
        ``FLOWDASH_AGENT_MODELS`` environment variable, a comma-separated
        list of ``id`` or ``id=label``. Each id is offered once. A model
        whose provider package or API key is missing is listed disabled
        with the reason under its label. The pick applies from the next
        request and the browser remembers it.""",
    )

    agent_palette = param.Boolean(
        default=True,
        doc="""
        Whether the agent gets the command palette as tools (list_commands
        and run_command), e.g. to open a dashboard or refresh it. Turn it off
        for an agent with a narrow job, whose requests then do not carry
        their schemas.""",
    )

    agent_model_settings = param.Dict(
        default=None,
        allow_None=True,
        doc="""
        pydantic-ai model settings for every request to the agent, e.g.
        ``{"temperature": 0}``, merged over the agent's own.""",
    )

    auth_config = param.ClassSelector(
        class_=AuthConfig,
        doc="""
        Project-level authorization configuration controlling group discovery,
        the admin groups and the default access policy.""",
    )

    breakpoints = param.List(default=[768, 1200], doc="Responsive breakpoints for the tile grid.")

    component_libraries = param.List(
        default=[],
        item_type=str,
        doc="""
        Installed libraries whose components the editor offers next to the
        project's own: module names, e.g. ``"my_library"``, or
        ``"module:attribute"`` naming a mapping of component id to
        component, a list of components or a single one. A module offers
        its ``__flowdash_components__``, or else every object it exports
        that ``@register`` marks as a component. Their permissions and
        `authorize_component` apply as for the project's components. An id
        that the project, the built-in components or another library
        already uses raises a ``ComponentLibraryError``.""",
    )

    configure_layout = param.Callable(
        default=None,
        doc="""
        Optional callback invoked on every navigation with
        (app, content, route). Use it to set `app.sidebar` and
        `app.contextbar` for the page currently being served.""",
    )

    contextbar = Children(default=[], doc="Items prepended to the contextbar.")

    contextbar_open = param.Boolean(default=False, doc="Whether the contextbar is open.")

    controls_position = param.Selector(
        default="top",
        objects=["top", "left"],
        doc="""
        Where a dashboard's controls (components registered with
        ``sidebar=True``) go unless the dashboard's settings say otherwise:
        "top" in a slim row above the tiles, "left" in a collapsible panel
        beside them. Passed to the editor, see `FlowDash.controls_position`.""",
    )

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
        `authorize_dashboard`, `components`, `store`, `user` and `include_page_dialog` are supplied
        by the app and raise a ValueError if given here.
        `dashboard`, `editable` and `read_only` are accepted but then managed
        by the app's routing and authorization. Read once at construction;
        the same editor is reused for every dashboard, so later changes have
        no effect.""",
    )

    header_layout = param.Selector(
        default="compact",
        objects=["compact", "full"],
        doc="""
        How a dashboard's title, health and actions are laid out unless the
        dashboard's settings say otherwise. "compact" puts them in the top bar
        next to the breadcrumb (with the default "rail" navigation) and
        shrinks the search field to an icon; "full" shows them in a row of
        their own under the top bar, with a large title, the description and
        when the dashboard was last refreshed. Passed to the editor, see
        `FlowDash.header_layout`.""",
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

    project = param.ClassSelector(
        class_=(Project, str, os.PathLike),
        default=None,
        doc="""
        The project file (``flowdash.yaml``) whose sections configure the
        app, as a path or a loaded `panel_flowdash.project.Project`. A path
        is loaded when the app is created, with the project directory and
        the component registry as the loaders' context, raising
        `ProjectError` with the file, line and column of each problem. See
        the project file reference.""",
    )

    propagate_failures = param.Boolean(
        default=False,
        doc="""
        Carry a failing output or edge transform downstream instead of leaving
        the connected inputs on their last value. Passed to the embedded
        editor, see `FlowDash.propagate_failures`.""",
    )

    settings_sources = param.Dict(
        default={},
        doc="""
        Where parameters given in code come from, by name, e.g.
        ``{"home_dashboard": "__init__.py", "title": "--title"}``. The admin
        view names it on each setting the parameter locks. `flowdash serve`
        fills it in.""",
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
        "authorize_dashboard",
        "components",
        "include_page_dialog",
        "store",
        "user",
    )

    def __init__(self, registry: dict[str, RegistryEntry] | None = None, **params):
        super().__init__(**params)
        # The parameters given in code, which win over the project file's settings.
        self._explicit = frozenset(params)

        if self.notifications:
            pn.config.notifications = True

        if self.auth_config is None:
            self.auth_config = AuthConfig()

        if registry is None:
            registry = build_registry(pathlib.Path(self.project_dir), package=self.package)
        registry = self._with_libraries(
            registry, self.component_libraries, self.include_builtin_components
        )
        if self.project is not None and not isinstance(self.project, Project):
            self.project = load_project(
                self.project, self._project_context(registry, self.project_dir)
            )
        self._registry = registry
        self._apply_project_settings()
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
        self._is_admin = is_admin(
            self._identity,
            self._admin_groups(),
            self._configured_admins(),
        )
        self._admin_page: AdminPage | None = None
        # Views of the open dashboard's sidebar components, shown after `sidebar`.
        self._sidebar_views: list = []
        self._sidebar_open_before_fullscreen: bool | None = None
        self._share_button = pmui.Button(
            label="Share",
            icon="share",
            variant="text",
            size="small",
            margin=(5, 2),
            description="Share",
            visible=False,
            stylesheets=[ICON_ONLY_WHEN_COMPACT],
        )
        self._share_button.on_click(lambda _event: self._share_current_dashboard())
        # The editor owns the canvas, tile grid and persistence; this class adds
        # routing, navigation, pages and authorization on top of it.
        self._editor = self._build_editor()
        self._editor._dashboard_bar.on_mode = self._on_dashboard_bar_mode
        self._editor.show_in_editor = self._show_in_editor
        self._editor.param.watch(self._on_fullscreen, "fullscreen")
        self._preset_tasks: set[asyncio.Task] = set()
        self._preset_loading: set[str] = set()
        self._editor.param.watch(self._on_editable, "editable")
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
        self._dashboard_header: str | None = None
        if self.nav_variant == "rail":
            # The top bar shows the dashboard's title (the breadcrumb) and actions.
            self._editor._dashboard_bar.placement = "header"
        self._shell = AppShell(self) if self.nav_variant == "rail" else None
        self._command_palette = CommandPaletteController.install(self)
        self._agent_session = self._build_agent_session()
        if self._agent_session is not None and self._shell is not None:
            toggle = self._agent_session.drawer.toggle
            toggle.stylesheets = [*toggle.stylesheets, _TOGGLE_ICON_ONLY_WHEN_COMPACT]
            self._shell.search_slot.append(toggle)
        self._build_dialog()
        self._build_unsaved_dialog()
        self._build_share_dialog()
        # Not in the page header: outside the rail variant the header is
        # primary-coloured and gives its children an inverted palette.
        shared_dialog = page_dialog()
        self._page_dialogs = [] if shared_dialog is None else [shared_dialog]
        self._page_dialogs.append(self._build_downloader())
        self._font_loader = FontLoader(
            href=font_href(self._project_font), width=0, height=0, margin=0, visible=False
        )
        self._page_dialogs.append(self._font_loader)
        if self.admin_link:
            doc = pn.state.curdoc
            if (
                doc is not None
                and doc.session_context is not None
                and not self._admin_groups()
                and not self._configured_admins()
            ):
                # Served some other way than `flowdash serve`, which prints it at startup.
                show_admin_link()
            self._token_exchange = AdminTokenExchange(
                width=0, height=0, margin=0, styles={"display": "none"}
            )
            self._token_exchange.param.watch(self._on_admin_token, "token")
            self._page_dialogs.append(self._token_exchange)
        self._leave_page_guard = LeavePageGuard()
        self._editor.param.watch(
            self._sync_leave_page_guard, ["dashboard", "dirty", "code_drafts"]
        )
        self._page_dialogs.append(self._leave_page_guard)
        page_kwargs = {
            "title": self.title,
            "theme_config": _merged(
                {"palette": {"primary": {"main": "#0072B5"}}}, self._project_theme_config
            ),
            **self._project_page_options,
            "sidebar_open": False,
            "sidebar": list(self.sidebar),
            "contextbar": self.param.contextbar.rx(),
            "contextbar_variant": "persistent",
            "contextbar_open": self.param.contextbar_open,
        }
        if self._menu_bar is not None:
            page_kwargs["header"] = [self._menu_bar]
        if self._shell is not None:
            page_kwargs.update(
                header=self._shell.header,
                header_color="paper",
                rail=self._shell.rail_content,
                sx=HEADER_SX,
            )
        theme_config = _merged(page_kwargs["theme_config"], self.page_options.get("theme_config"))
        page_kwargs.update(self.page_options)
        page_kwargs["theme_config"] = theme_config
        if self._shell is None and self._agent_session is not None:
            page_kwargs["header"] = [
                *page_kwargs.get("header", []),
                self._agent_session.drawer.toggle,
            ]
        self._page_sx = {**page_kwargs.get("sx", {}), **self.page_options.get("sx", {})}
        self._page = pmui.Page(**{**page_kwargs, "sx": self._header_sx()})
        self.param.watch(self._sync_page_sidebar, "sidebar")
        pn.state.onload(self._load_page_layout)

    # ------------------------------------------------------------------
    # Project file settings and the admin view
    # ------------------------------------------------------------------

    def _settings_project(self) -> Project:
        """Return the project the admin view writes: `project`, or the project directory's file.

        Without a project file, the admin view writes ``flowdash.yaml`` in
        the project directory, created on the first save.
        """
        if isinstance(self.project, Project):
            return self.project
        if getattr(self, "_own_project", None) is not None:
            return self._own_project
        if self.project_dir is None:
            self._own_project = Project(context=self._project_context(self._registry, None))
            return self._own_project
        path = pathlib.Path(self.project_dir).resolve() / PROJECT_FILE
        key = str(path)
        project = _SETTINGS_PROJECTS.get(key)
        if project is None:
            context = self._project_context(self._registry, self.project_dir)
            project = _unloaded_project(path, context) if path.is_file() else None
            project = project or Project(key, context=context)
            project = _SETTINGS_PROJECTS.setdefault(key, project)
        return project

    def _code_setting_source(self, target: str) -> str | None:
        """Return where the parameter *target* names was given in code, or None.

        *target* is a parameter name or a path into a dict parameter, e.g.
        ``"editor_options.mode"``.
        """
        top, *path = target.split(".")
        if top not in self._explicit or top not in self.param:
            return None
        value = getattr(self, top)
        for part in path:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        source = self.settings_sources.get(top)
        if source is None:
            return f"{top} set in code"
        if source.startswith("-"):
            return f"set with {source}"
        return f"{top} in {source}"

    def _code_setting_value(self, target: str) -> t.Any:
        """Return the value of the parameter, or the entry of a dict parameter, *target* names."""
        top, *path = target.split(".")
        value = getattr(self, top)
        for part in path:
            value = value[part]
        return value

    def _apply_project_settings(self):
        """Use the project file's settings for the parameters not given in code."""
        self._project_editor_options: dict[str, t.Any] = {}
        self._project_page_options: dict[str, t.Any] = {}
        self._project_theme_config: dict[str, t.Any] = {}
        self._project_font: str | None = None
        try:
            project = self._settings_project()
            overrides = project_overrides(
                project, lambda target: self._code_setting_source(target) is not None
            )
        except (ProjectError, OSError) as exc:
            logger.warning("Not using the project file's settings: %s", exc)
            return
        updates = {}
        for target, value in overrides.items():
            top, *path = target.split(".")
            if top == "editor_options":
                self._project_editor_options[path[0]] = value
            elif top == "page_options" and path[0] == "theme_config":
                nested = value
                for part in reversed(path[1:]):
                    nested = {part: nested}
                self._project_theme_config = _merged(self._project_theme_config, nested)
            elif top == "page_options":
                self._project_page_options[path[0]] = value
            elif top in self.param:
                updates[top] = value
        self._project_font = (
            ((project.get("theme") or {}).get("font_family"))
            if "fontFamily" in self._project_theme_config.get("typography", {})
            else None
        )
        if updates:
            self.param.update(updates)

    def _apply_theme(self):
        """Show the theme the project file now sets on this page."""
        self._apply_project_settings()
        page = self._page
        theme_config = _merged(
            _merged({"palette": {"primary": {"main": "#0072B5"}}}, self._project_theme_config),
            self.page_options.get("theme_config"),
        )
        updates = {"theme_config": theme_config, "title": self.title}
        for name in ("logo", "dark_theme"):
            if name in self._project_page_options and name not in self.page_options:
                updates[name] = self._project_page_options[name]
        page.param.update(updates)
        self._font_loader.href = font_href(self._project_font)

    def _configured_admins(self) -> list[str]:
        try:
            return project_admins(self._settings_project())
        except (ProjectError, OSError):
            return []

    def _on_admin_token(self, event):
        """Make whoever opened the admin link an admin, now and on every later start."""
        if not event.new:
            return
        location = pn.state.location
        if location is not None and "token=" in (location.hash or ""):
            # Otherwise the next navigation would write the fragment back into the address bar.
            location.hash = ""
        user = self._identity.user
        user = user if user and user != ANONYMOUS_USER else None
        try:
            outcome = claim_admin(self._settings_project(), user, event.new, self._admin_groups())
        except (ProjectError, OSError) as exc:
            logger.warning("Could not record %s as an admin: %s", user, exc)
            self._notify("warning", f"You're admin until the server restarts: {error_text(exc)}")
        else:
            if outcome == "invalid":
                logger.warning("Refused an admin link with a wrong or used token.")
                self._notify("error", "This admin link is not valid or was already used.")
                return
            if outcome == "taken":
                logger.warning("Refused an admin link: the project already has admins.")
                self._notify(
                    "error",
                    "This project already has admins. Ask one of them to add you under "
                    "Admin, General.",
                )
                return
            if user:
                self._notify("success", f"{user} is now an admin of this project.")
        if self._is_admin:
            return
        self._is_admin = True
        if self._shell is not None:
            self._shell.refresh()
        if location is not None and (location.pathname or "").startswith(ADMIN_ROUTE):
            pn.state.execute(self._load_page_layout)

    def _on_admin_saved(self, section: str):
        if section == "theme":
            self._apply_theme()

    def _show_admin_section(self, section: str):
        self._navigate_to(f"{ADMIN_ROUTE}/{section}")

    def _admin_view(self, pathname: str):
        """Return the admin page showing the section *pathname* names, or a refusal."""
        if not self._is_admin:
            return self._access_denied_view("Admin")
        if self._admin_page is None:
            self._admin_page = AdminPage(
                self,
                self._settings_project(),
                on_select=self._show_admin_section,
                on_saved=self._on_admin_saved,
            )
            self._admin_page.param.watch(self._sync_leave_page_guard, "dirty")
        section = pathname[len(ADMIN_ROUTE) :].strip("/") or None
        self._admin_page.show(section)
        return self._admin_page

    def _set_sidebar_views(self, views: list):
        self._sidebar_views = list(views)
        self._sync_page_sidebar()

    def _sync_page_sidebar(self, *_events):
        self._page.sidebar = [*self.sidebar, *self._sidebar_views]

    def _header_sx(self) -> dict:
        """Return the page's ``sx``, making room for a dashboard's actions in the top bar."""
        header_sx = {None: {}, "view": DASHBOARD_HEADER_SX, "edit": DASHBOARD_EDIT_HEADER_SX}
        return {**self._page_sx, **header_sx[self._dashboard_header]}

    def _set_dashboard_header(self, mode: str | None):
        """Make room in the top bar for the dashboard's "view" or "edit" actions, or None."""
        self._dashboard_header = mode
        if getattr(self, "_page", None) is not None and self._page.sx != (sx := self._header_sx()):
            self._page.sx = sx
        session = getattr(self, "_agent_session", None)
        if session is not None:
            toggle = session.drawer.toggle
            classes = [c for c in toggle.css_classes if c != _EDIT_HEADER_CLASS]
            if mode == "edit":
                classes.append(_EDIT_HEADER_CLASS)
            if classes != toggle.css_classes:
                toggle.css_classes = classes

    def _begin_navigation(self) -> int:
        """Start a navigation, superseding the one in flight, and return its generation."""
        self._nav_generation += 1
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
        self._main_task = None
        # Full screen belongs to the dashboard on screen, not to the next page.
        self._editor.fullscreen = False
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
        the cancellation propagates.
        """
        task = asyncio.create_task(coroutine)
        self._main_task = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
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
                self._nav_content(LoadingSkeleton()),
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

        In the 'menubar' and 'rail' variants navigation lives in the page
        header and rail, so the content is returned as-is; in the 'left'/'right'
        variants it is paired with the docked drawer on that side. With an
        agent, its drawer is docked on the right.
        """
        if self.nav_variant == "left":
            content = pn.Row(self._nav_drawer, content, sizing_mode="stretch_both")
        elif self.nav_variant == "right":
            content = pn.Row(content, self._nav_drawer, sizing_mode="stretch_both")
        if self._agent_session is not None:
            content = self._agent_session.dock(content)
        return content

    def _resolve_user_id(self) -> str:
        return self._identity.user

    @staticmethod
    @cache
    def _accepted_injected_params(app):
        if inspect.isclass(app) and issubclass(app, pn.viewable.Viewer):
            # An editor's `context` holds the host's resources, not where it is shown.
            names = ("config", "executor", "instance_config") + (
                () if issubclass(app, FlowDash) else ("context",)
            )
            return {p for p in names if hasattr(app, p)}
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

    @staticmethod
    def _with_libraries(
        registry: dict[str, RegistryEntry], libraries: t.Sequence[str], builtins: bool = True
    ) -> dict[str, RegistryEntry]:
        """Add the components of *libraries* to *registry*, refusing ids already taken."""
        reserved = BUILTIN_COMPONENTS if builtins else ()
        return merge_libraries(registry, libraries, reserved=reserved)

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

    def _authorize_dashboard(self, dashboard: DashboardModel, access: str) -> bool:
        """Whether the current identity may still view or edit the stored *dashboard*.

        The editor asks before every save, reload and discard, so sharing
        changes made from another session apply to an open dashboard.
        """
        if access == "edit":
            return self._can_edit_dashboard(dashboard)
        return is_authorized(
            dashboard.permission,
            self._identity,
            default_allow=self._default_allow(),
            owner=dashboard.owner,
        )

    def _access_denied_view(self, title: str | None = None, owner: str | None = None):
        """Generate the view shown when the user is not authorized for the target.

        Pass *owner* to name who can grant access; dashboards pass no *title*,
        so a dashboard's name is never shown to someone who may not open it.
        """
        return access_denied(
            user=self._identity.user,
            subject=title,
            owner=owner,
            on_home=partial(self._request_navigation, "/"),
        )

    def _missing_dashboard_view(self, dashboard_id: str):
        """Explain why *dashboard_id* cannot be shown: it does not exist, or it is not shared.

        When the id is random, only someone given a link learns that the
        dashboard exists, so they are told who owns it, to ask for access, but
        not its title or contents. When it can be guessed (see
        `BaseDashboardStore.guessable_id`), a dashboard the user may not open
        gets the same not-found page as a missing id, so guessing a title
        reveals nothing.
        """
        guessable = self.store.guessable_id(dashboard_id)
        owner = None if guessable else self.store.get_owner(dashboard_id)
        if owner is not None:
            return self._access_denied_view(owner=owner)
        accessible = self.store.list_accessible(
            self._identity, default_allow=self._default_allow()
        )
        return dashboard_not_found(
            dashboard_id,
            similar_dashboards(dashboard_id, accessible),
            on_open=lambda did: self._request_navigation(f"{DASH_ROUTE_PREFIX}{did}"),
            on_all=partial(self._request_navigation, "/"),
        )

    def _route_state_view(self, pathname: str):
        """Return the view for a route no dashboard or page handles: a refused page, or nothing.

        Requests for a page the user may not open are redirected to
        :data:`ACCESS_DENIED_ROUTE` by the serve command, with the page's path
        in the ``page`` query parameter.
        """
        go_home = partial(self._request_navigation, "/")
        if pathname.rstrip("/") == ACCESS_DENIED_ROUTE and pn.state.location is not None:
            query = parse_qs((pn.state.location.search or "").lstrip("?"))
            requested = "/" + (query.get("page") or [""])[0].strip("/")
            entry = next(
                (e for e in self._page_entries.values() if e.page_path == requested), None
            )
            if entry is not None:
                if self._can_access_entry(entry):
                    pn.state.execute(partial(self._navigate_to, requested), schedule=True)
                    return LoadingSkeleton()
                return self._access_denied_view(entry.title)
        return page_not_found(pathname, on_home=go_home)

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
            keys = ", ".join(code_span(u["key"]) for u in blocking)
            return alert_pane(
                f"**{escape_markdown(entry.title)}** is waiting for: {keys}",
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
            return markdown_pane(escape_markdown(f"Unknown page: {'/'.join(key)}"))

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
            return alert_pane(
                f"**{err_name}**: {escape_markdown(error_text(e))}\n<hr>\n<pre> {escape_html(redact_secrets(traceback.format_exc()))}</pre>\n",
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
            authorize_dashboard=self._authorize_dashboard,
            components=self._registry,
            include_page_dialog=False,
            store=self.store,
            user=self._user_id,
            **{
                "include_builtin_components": self.include_builtin_components,
                "breakpoints": self.breakpoints,
                "notifications": self.notifications,
                "controls_position": self.controls_position,
                "header_layout": self.header_layout,
                "propagate_failures": self.propagate_failures,
                "settings_class": self.settings_class,
                **self._project_editor_options,
                **options,
                "toolbar_extra": toolbar_extra,
            },
        )
        editor.param.watch(self._on_editor_sidebar, ["controls_bar", "fullscreen", "sidebar"])
        editor.param.watch(self._on_editor_saved, "saved")
        return editor

    @param.depends("agent_models", watch=True, on_init=True)
    def _check_agent_models(self):
        if self.agent_models:
            from panel_flowdash.agent.models import validate_models

            validate_models(self.agent_models)

    def _build_agent_session(self):
        """Connect this session to the configured agent, or return None when there is none."""
        if self.agent is False or (
            self.agent is None
            and not (
                self.agent_model
                or self.agent_models
                or os.environ.get("FLOWDASH_AGENT_MODEL")
                or os.environ.get("FLOWDASH_AGENT_MODELS")
            )
        ):
            return None
        if self.agent_authorize is not None:
            try:
                allowed = bool(self.agent_authorize(self._identity))
            except Exception:
                logger.exception("agent_authorize failed; leaving the agent out")
                allowed = False
            if not allowed:
                return None
        try:
            from panel_flowdash.agent.assistant import EDITING
            from panel_flowdash.agent.session import (
                AgentSession,
                resolve_agent,
                resolve_explore_agent,
            )
        except ImportError as exc:
            logger.error("%s (%s)", AGENT_EXTRA_MISSING, exc)
            return None
        from panel_flowdash.agent.models import configured_models

        models, model = configured_models(self.agent_models, self.agent_model)
        if isinstance(self.agent, str):
            models, model = configured_models(self.agent_models, self.agent)
        # With a choice the picker names every request's model, so an unusable default must not fail here.
        checks = {"defer_model_check": True} if len(models) > 1 else {}
        exploring = self.explore_agent is not False
        agent = resolve_agent(
            self.agent, model, **checks, **({"instructions": EDITING} if exploring else {})
        )
        if agent is None:
            return None
        made = self.agent is None or self.agent is True or isinstance(self.agent, str)
        # An assistant passed in runs on its own model, and the Explore agent made for it too.
        explore_agent = resolve_explore_agent(
            self.explore_agent, agent, model if made else None, **checks
        )
        # With a single model there is nothing to pick, and an agent passed in keeps its own model.
        # The chat names that model as configured when agent_models lists it or the configured id
        # is the agent's own model, else by the agent's model.
        explore_model = None
        if len(models) < 2:
            from panel_flowdash.agent.models import agent_model_choice, same_model

            configured = models[0] if models else None
            if configured is not None and not (made or self.agent_models):
                own = agent_model_choice(agent)
                if own is None or not same_model(configured.id, own.id):
                    configured = None
            if explore_agent is not None and self.explore_agent in (None, True):
                explore_model = agent_model_choice(explore_agent, configured)
            only = agent_model_choice(agent, configured)
            models, model = ([only], only.id) if only else ([], None)
        return AgentSession(
            self,
            agent,
            models=models,
            model=model,
            explore_agent=explore_agent,
            explore_model=explore_model,
        )

    def _on_editor_sidebar(self, *events):
        """Mirror the editor's sidebar views into the page sidebar, unless its controls bar shows them.

        The sidebar opens when it gets views and closes when it has none.
        Full screen shows the views in the controls bar, so the page sidebar
        is emptied meanwhile and reopened, or left collapsed, as the user had it.
        """
        editor = self._editor
        views = [] if editor.controls_bar or editor.fullscreen else list(editor.sidebar)
        had_views = bool(self._sidebar_views)
        restore = None
        if any(event.name == "fullscreen" for event in events):
            if editor.fullscreen:
                if had_views:
                    self._sidebar_open_before_fullscreen = self._page.sidebar_open
            else:
                restore = self._sidebar_open_before_fullscreen
                self._sidebar_open_before_fullscreen = None
        self._set_sidebar_views(views)
        if not views:
            self._page.sidebar_open = False
        elif not had_views:
            self._page.sidebar_open = True if restore is None else restore

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
            message = error_text(exc)
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

    def _has_unsaved_changes(self) -> bool:
        if self._admin_dirty():
            return True
        unsaved = self._dirty or self._editor.code_drafts
        return unsaved and self._current_dashboard is not None

    def _admin_dirty(self) -> bool:
        """Whether the admin page is shown with unsaved settings."""
        if self._admin_page is None or not self._admin_page.dirty or pn.state.location is None:
            return False
        path = pn.state.location.pathname or ""
        return path == ADMIN_ROUTE or path.startswith(f"{ADMIN_ROUTE}/")

    def _sync_leave_page_guard(self, *_events):
        self._leave_page_guard.active = self._has_unsaved_changes()

    def _leave_dashboard(self):
        """Forget the current dashboard, leaving edit mode, for a route that shows none."""
        self._current_dashboard = None
        # Editor actions such as the command palette's "Add to this dashboard"
        # would otherwise act on the hidden canvas.
        self._editor.param.update(editable=False, read_only=False)

    def _reset_canvas(self):
        """Clear the editor canvas and the mirrored page sidebar."""
        self._editor._reset_canvas()
        self._set_sidebar_views([])

    async def _ensure_components_loaded(self, component_ids: t.Iterable[str] | None = None):
        """Load component modules and build their specs, if not done yet.

        Defaults to the whole catalog, which is what the editor palette needs.
        Dashboard routes pass the components the dashboard places instead.
        """
        await self._editor.ensure_components_loaded_async(component_ids)

    def _save_current_dashboard(self) -> bool:
        """Save the loaded dashboard, returning whether it was saved.

        The editor checks edit access against the stored dashboard's current
        sharing rules (see `_authorize_dashboard`), the same check as the
        dashboard bar's Save and the settings dialog's auto-save.
        """
        if self._current_dashboard is None:
            self._notify(
                "warning", "No dashboard loaded. Create one from the sidebar.", duration=4000
            )
            return False
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
            placed = {item.component_id for item in dashboard.items}
            await self._ensure_components_loaded(
                placed | (self._preset_component_ids() if edit else set())
            )
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
            self._leave_dashboard()
            self._page.main = [
                self._nav_content(self._missing_dashboard_view(dashboard_id)),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
                self._share_dialog,
            ]
            return

        location = pn.state.location
        search = location.search if location is not None else ""
        kiosk = not edit and _is_kiosk(search)
        # Before loading, so only someone who may save it is told about items it
        # could not restore.
        self._editor.read_only = not self._can_edit_dashboard(dashboard_id)
        self._editor.load_model(dashboard)
        if edit:
            self._show_edit_mode()
        else:
            self._show_view_mode()
        if kiosk:
            # View mode clears the query; the link's other parameters are kept.
            location.search = _without_query(search, "edit")
            self._editor.fullscreen = True
        self._sync_menu_active(f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        self._apply_layout_config(self._component_view, f"{DASH_ROUTE_PREFIX}{dashboard_id}")
        self._page.main = [
            self._nav_content(self._component_view),
            self._dialog,
            self._unsaved_dialog,
            *self._page_dialogs,
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
        self._import_preset_components()
        self._set_sidebar_views([])

        self._notify("success", f'Created new dashboard "{dashboard.title}".', duration=3000)
        # Leave the launcher's route first, so the menus refresh without
        # rebuilding the launcher's cards only for the editor to replace them.
        if pn.state.location:
            pn.state.location.param.update(
                pathname=f"{DASH_ROUTE_PREFIX}{dashboard.dashboard_id}",
                search="?edit=true",
            )
        self._refresh_sidebar_dashboards()
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
        ]
        return True

    def _request_new_dashboard(self, title_str: str) -> bool:
        """Create a dashboard, first asking about unsaved changes on the current one.

        Returns False only if creating failed right away, so the create dialog
        stays open with the error.
        """
        if not self._has_unsaved_changes():
            return self._create_new_dashboard(title_str)
        self._confirm_unsaved(partial(self._create_new_dashboard, title_str))
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

    # Navigation swaps the page while it awaits, so it holds the Document lock
    # throughout. Unlocked, a locked callback running meanwhile on a worker
    # thread can change models of the incoming page and send that change
    # before the held page swap, which the browser rejects as referencing
    # models it does not know, so the page swap never shows. Browser events
    # wait for the navigation to finish meanwhile.
    @pn.io.with_lock
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
            self._leave_dashboard()
            self._set_sidebar_views([])
            self._page.sidebar_open = False
            launcher = self._build_launcher()
            self._apply_layout_config(launcher, pathname)
            # The launcher's Share action opens the editor's settings dialog,
            # which is otherwise only mounted with the editor.
            self._page.main = [
                self._nav_content(launcher),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
                self._share_dialog,
            ]
            return

        if pathname == COMPONENTS_ROUTE:
            self._leave_dashboard()
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

        if pathname == ADMIN_ROUTE or pathname.startswith(f"{ADMIN_ROUTE}/"):
            self._leave_dashboard()
            self._reset_canvas()
            self._set_sidebar_views([])
            view = self._admin_view(pathname)
            self._apply_layout_config(view, pathname)
            self._page.main = [
                self._nav_content(view),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
            ]
            if self._shell is not None:
                self._shell.sync(pathname)
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

        self._leave_dashboard()
        self._set_sidebar_views([])
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
                self._route_state_view(pathname),
                self._dialog,
                self._unsaved_dialog,
                *self._page_dialogs,
            ]
            if self._shell is not None:
                main[0] = self._nav_content(main[0])
            elif not self._menubar_mode:
                main.append(self._nav_drawer)
            self._page.main = main

    def _preset_component_ids(self) -> set[str]:
        """Return the scanned components whose palette presets only an import supplies."""
        return {
            component_id
            for component_id, entry in self._editor._component_entries.items()
            if entry.metadata.presets_pending and entry.app is None
        }

    def _on_editable(self, event):
        if event.new:
            self._import_preset_components()

    def _import_preset_components(self):
        """Import the scanned components with palette presets, off the event loop, then refresh the palette."""
        pending = self._preset_component_ids() - self._preset_loading
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._editor.ensure_components_loaded(pending)
            self._editor._palette_panel.refresh()
            return
        self._preset_loading |= pending
        task = loop.create_task(self._load_preset_components(pending))
        self._preset_tasks.add(task)
        task.add_done_callback(self._preset_tasks.discard)

    async def _load_preset_components(self, component_ids: set[str]):
        try:
            await self._ensure_components_loaded(component_ids)
        finally:
            self._preset_loading -= component_ids
        self._editor._palette_panel.refresh()

    @pn.io.hold()
    def _show_edit_mode(self):
        current = self._current_dashboard
        _can_admin, can_edit = (
            self._dashboard_rights(current.dashboard_id) if current else (False, False)
        )
        self._share_button.visible = False
        self._editor.param.update(editable=True, read_only=not can_edit)
        self._import_preset_components()
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

    def _on_fullscreen(self, event):
        """Drop ``?kiosk=1`` once the full screen layout it opened is left."""
        location = pn.state.location
        if not event.new and location is not None and _is_kiosk(location.search):
            location.search = _without_query(location.search, "kiosk")

    def _on_dashboard_bar_mode(self, mode: str):
        """Switch modes from the dashboard bar, keeping ``?edit=true`` in step.

        Leaving edit mode with unsaved changes goes through the unsaved-changes
        dialog, whose answers reload the dashboard in view mode.
        """
        location = pn.state.location
        if mode == "edit":
            self._show_edit_mode()
        elif self._has_unsaved_changes() and location is not None:
            self._request_navigation(location.pathname)
        else:
            self._show_view_mode()

    def _show_in_editor(self, _instance_id: str):
        """Open the current dashboard on its wiring canvas, from a tile's "Show in editor"."""
        if self._current_dashboard is None:
            return
        if self._editor.mode != "split":
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
        {"label": "Download as YAML", "icon": "data_object"},
        {"label": "Delete", "icon": "delete", "color": "error"},
    )

    _EDITOR_DASHBOARD_ACTIONS = ({"label": "Edit", "icon": "edit"},)

    _VIEWER_DASHBOARD_ACTIONS = (
        {"label": "Duplicate", "icon": "content_copy"},
        {"label": "Download as file", "icon": "download"},
        {"label": "Download as YAML", "icon": "data_object"},
    )

    def _dashboard_actions(
        self, can_admin: bool, can_edit: bool = False, can_copy: bool = True
    ) -> tuple:
        """Menu actions for a dashboard, gated by administration and edit rights.

        An editor gets ``_EDITOR_DASHBOARD_ACTIONS`` followed by the viewer
        actions it does not already have. A viewer gets no actions when
        *can_copy* is false, see :meth:`_can_copy_dashboard`.
        """
        if can_admin:
            return self._ADMIN_DASHBOARD_ACTIONS
        if not can_edit:
            return self._VIEWER_DASHBOARD_ACTIONS if can_copy else ()
        keys = {self._action_key(action) for action in self._EDITOR_DASHBOARD_ACTIONS}
        return (
            *self._EDITOR_DASHBOARD_ACTIONS,
            *(a for a in self._VIEWER_DASHBOARD_ACTIONS if self._action_key(a) not in keys),
        )

    def _dashboard_menu_actions(self, dashboard: DashboardModel) -> tuple:
        """Return the :meth:`_dashboard_actions` the current identity gets for *dashboard*."""
        return self._dashboard_actions(
            *self._dashboard_rights(dashboard), can_copy=self._can_copy_dashboard(dashboard)
        )

    def _can_copy_dashboard(self, dashboard: DashboardModel) -> bool:
        """Whether the current identity may duplicate or download *dashboard*.

        A copy is the copier's own dashboard, with full edit and inspect
        rights, so a viewer may only take one when the dashboard does not hide
        its outputs from viewers: either output inspection is off in this app
        or the dashboard's ``viewers_can_inspect`` setting allows it. Editors
        and administrators may always copy.
        """
        if self._can_edit_dashboard(dashboard):
            return True
        return not self._editor.inspect_outputs or bool(
            dashboard.settings.get("viewers_can_inspect", False)
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
            actions = self._dashboard_menu_actions(d)
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
        elif action == "Download as YAML":
            self._download_dashboard(dashboard_id, "yaml")
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

    def _copyable_dashboard(self, dashboard_id: str) -> DashboardModel | None:
        """Load a dashboard the current identity may copy, or notify why it may not."""
        model = self._readable_dashboard(dashboard_id)
        if model is not None and not self._can_copy_dashboard(model):
            self._notify(
                "error",
                "Only people who can edit this dashboard may duplicate or download it.",
                duration=4000,
            )
            return None
        return model

    def _duplicate_dashboard(self, dashboard_id: str) -> DashboardModel | None:
        """Copy a dashboard as "<title> copy", owned by the current user and not shared."""
        source = self._copyable_dashboard(dashboard_id)
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

    def _download_dashboard(self, dashboard_id: str, extension: str = "json") -> bool:
        """Download the saved dashboard as the file the editor's Download button writes."""
        model = self._copyable_dashboard(dashboard_id)
        if model is None:
            return False
        send_download(
            self._downloader,
            model,
            restricted=self._editor._restricted_ids(model),
            extension=extension,
        )
        return True

    def _import_dashboard_file(self, filename: str, content: str | bytes) -> DashboardModel | None:
        """Save a downloaded dashboard file as a new dashboard of the user and open it."""
        try:
            parsed = parse_dashboard_file(content, self._user_id)
        except ValueError as exc:
            self._notify("error", f"Could not import {filename}: {error_text(exc)}", duration=5000)
            return None
        self._editor._guard_restricted(parsed, None)
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
        current = self._current_dashboard
        if current is not None and current.dashboard_id == dashboard_id and self._editor.editable:
            return
        if self._has_unsaved_changes():
            self._request_navigation(target_path, search="?edit=true")
            return
        if pn.state.location:
            pn.state.location.param.update(pathname=target_path, search="?edit=true")
        pn.state.execute(partial(self._load_dashboard_edit, dashboard_id))

    @pn.io.with_lock
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
            done = self._request_new_dashboard(name)
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
        """Give the editor's settings dialog the sharing, rename and delete actions.

        Share and Settings open the same dialog, so ``_share_dialog`` is the
        editor's ``_settings_dialog``.
        """
        self._editor._settings_view.param.update(
            delete=self._open_delete_dialog,
            rename=self._rename_from_settings,
            save_sharing=self._save_sharing,
            sharing=self._sharing_access,
        )
        self._share_dialog = self._editor._settings_dialog
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

    def _group_sizes(self) -> dict[str, int]:
        """Members per group, as far as the static ``user_groups`` mapping knows them."""
        sizes: dict[str, int] = {}
        if self.auth_config is not None:
            for member_groups in self.auth_config.user_groups.values():
                for group in member_groups:
                    sizes[group] = sizes.get(group, 0) + 1
        return sizes

    def _sharing_access(self, dashboard_id: str) -> SharingAccess | None:
        """Return what the settings dialog's sharing section shows, for administrators only."""
        if not self._can_administer_dashboard(dashboard_id):
            return None
        model = self.store._load_any(dashboard_id)
        if model is None:
            return None
        return SharingAccess(
            owner=model.owner,
            permission=model.permission,
            default_allow=self._default_allow(),
            group_sizes=self._group_sizes(),
            groups=self._known_groups(),
            link=share_link(f"{DASH_ROUTE_PREFIX}{dashboard_id}"),
            users=self._known_users(),
            you=self._identity.user_names,
        )

    def _open_share_dialog(self, dashboard_id: str, title: str):
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to share this dashboard.")
            return
        self._editor._settings_view.show("sharing", dashboard_id=dashboard_id, title=title)

    def _rename_from_settings(self, dashboard_id: str, title: str) -> str | None:
        """Rename from the settings dialog, returning why the name was refused, if it was."""
        title = title.strip()
        if not title:
            return "Name cannot be empty."
        if self.store.title_exists(self._user_id, title, exclude_id=dashboard_id):
            return "A dashboard with this name already exists."
        if not self._rename_dashboard(dashboard_id, title):
            return "The dashboard could not be renamed."
        return None

    def _save_sharing(self, dashboard_id: str, permission: Permission) -> bool:
        """Store a dashboard's sharing rules, returning whether they were stored."""
        if not self._can_administer_dashboard(dashboard_id):
            self._notify("error", "You are not allowed to share this dashboard.")
            return False
        try:
            updated = self.store.set_permission(dashboard_id, permission)
        except Exception as exc:
            self._notify_store_failure("share", exc)
            return False
        if not updated:
            self._notify("error", "Sharing could not be updated.", duration=5000)
            return False
        if self._current_dashboard and self._current_dashboard.dashboard_id == dashboard_id:
            self._current_dashboard.permission = permission
        self._refresh_sidebar_dashboards()
        self._notify("success", "Sharing updated.", duration=3000)
        return True

    def _build_unsaved_dialog(self):
        # The (path, search) the pending action navigates to, if it navigates.
        self._pending_navigation: tuple[str, str] | None = None
        self._pending_action: t.Callable[[], object] | None = None

        def _take_pending():
            pending = self._pending_action
            self._pending_action = None
            self._pending_navigation = None
            return pending

        def _on_leave():
            if self._admin_dirty():
                self._admin_page.discard_all()
                pending = _take_pending()
                if pending:
                    pending()
                return
            self._dirty = False
            self._editor.drop_code_drafts()
            pending = _take_pending()
            if pending:
                pending()

        def _on_save():
            if self._admin_dirty():
                if not self._admin_page.save_all():
                    _take_pending()
                    return
                pending = _take_pending()
                if pending:
                    pending()
                return
            if not self._save_current_dashboard():
                _take_pending()
                return
            # Saving stores what was run; the drafts held in the code panel go with the page.
            self._editor.drop_code_drafts()
            pending = _take_pending()
            if pending:
                pending()

        def _on_stay():
            _take_pending()

        self._unsaved_view = UnsavedChangesDialog(
            on_leave=_on_leave, on_save=_on_save, on_stay=_on_stay
        )
        self._unsaved_dialog = self._unsaved_view.dialog
        return self._unsaved_dialog

    def _unsaved_changes(self) -> list[tuple[str, str]]:
        """List what the canvas changed since the dashboard was loaded or saved."""
        if self._admin_dirty():
            return [
                ("settings", f"{label} settings") for label in self._admin_page.dirty_sections()
            ]
        editor = self._editor
        saved = editor.dashboard
        if saved is not None:
            # Settings never stored are at their defaults, which `to_model` writes out.
            defaults = editor.settings_class()
            stored = {name: getattr(defaults, name) for name in editor._settings_names}
            saved = dataclasses.replace(saved, settings={**stored, **saved.settings})
        titles = {cid: entry.title for cid, entry in self._component_entries.items()}
        changes = describe_changes(saved, editor.to_model(), titles)
        if editor.code_drafts:
            changes.append(("code", "Edits in the code panel that were not run"))
        return changes

    def _navigate_to(self, path: str, search: str = ""):
        if pn.state.location is None:
            return
        pn.state.location.param.update(pathname=path, search=search)
        self._sync_menu_active(path)
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None:
            # Started right away, loading could send the new page before the
            # changes above (URL, breadcrumb) leave, and the browser rejects it:
            # "reference ... isn't known". A next-tick callback runs once they went out.
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

    def _confirm_unsaved(
        self, action: t.Callable[[], object], navigation: tuple[str, str] | None = None
    ):
        """Run *action*, after the unsaved-changes dialog if the dashboard has unsaved changes.

        Every navigation or mode change that would discard edits goes through
        here. "Leave without saving" and "Save and leave" run *action*;
        "Keep editing" drops it. *navigation* is the ``(path, search)`` that
        *action* navigates to, if it navigates.
        """
        if not self._has_unsaved_changes():
            action()
            return
        self._pending_action = action
        self._pending_navigation = navigation
        self._unsaved_view.show(self._unsaved_changes())

    def _request_navigation(self, path: str, search: str = ""):
        self._confirm_unsaved(partial(self._navigate_to, path, search), (path, search))

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
                # Back to view mode, asking about unsaved changes first like Done.
                if self._has_unsaved_changes():
                    self._request_navigation(path)
                    return
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
            for action in self._dashboard_menu_actions(d):
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

    @staticmethod
    def _project_context(registry, project_dir) -> dict[str, t.Any]:
        """Return the context section loaders get: the project directory and the registry."""
        return {
            "project_dir": pathlib.Path(project_dir) if project_dir is not None else None,
            "registry": registry,
        }

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
        # Merged once here, so every session shares the libraries' entries.
        registry = cls._with_libraries(
            registry,
            params.get("component_libraries", ()),
            params.get("include_builtin_components", True),
        )
        # Loaded once, after the libraries had their chance to register sections.
        project = params.get("project")
        if project is not None and not isinstance(project, Project):
            params["project"] = load_project(project, cls._project_context(registry, project_dir))

        if project_dir is not None and pathlib.Path(project_dir).is_dir():
            # The admin view writes the project file there when there is none yet.
            params.setdefault("project_dir", project_dir)

        def factory():
            return cls(registry=registry, **params)

        routes: dict[str, t.Any] = {
            "/": factory,
            ACCESS_DENIED_ROUTE: factory,
            COMPONENTS_ROUTE: factory,
            ADMIN_ROUTE: factory,
            f"{ADMIN_ROUTE}/[^/]+": factory,
            f"{DASH_ROUTE_PREFIX}[^/]+": factory,
        }
        for app_id, v in registry.items():
            if v.metadata.page:
                routes[f"/{app_id}"] = factory
        return routes
