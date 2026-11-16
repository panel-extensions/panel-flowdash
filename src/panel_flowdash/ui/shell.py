"""The app shell: top bar content (breadcrumb, search slot) and the navigation rail."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

# The app module imports this one; its route constants are only read at runtime.
from panel_flowdash import app as app_module

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

DOCS_URL = "https://panel-extensions.github.io/panel-flowdash/"

# Below this width the rail is hidden behind a menu button in the top bar.
NARROW_BREAKPOINT = 600

_HIDE_WHEN_NARROW = (
    f"@media (max-width: {NARROW_BREAKPOINT - 0.05}px) {{ :host {{ display: none !important; }} }}"
)
_HIDE_WHEN_WIDE = (
    f"@media (min-width: {NARROW_BREAKPOINT}px) {{ :host {{ display: none !important; }} }}"
)

# Material UI gives header children an inverted palette unless the app bar is
# styled exactly like this; the second rule then gives it the page's own colours.
HEADER_SX = {
    ".MuiAppBar-root": {"bgcolor": "primary.contrastText", "color": "primary.main"},
    "& .MuiAppBar-root.header": {
        "bgcolor": "background.paper",
        "backgroundImage": "none",
        "color": "text.primary",
        "boxShadow": "none",
        "borderBottom": 1,
        "borderColor": "divider",
    },
    # The top bar is one 56px row; the page's spacers below it follow suit.
    "& .MuiToolbar-root": {"minHeight": "56px"},
    "& .header .MuiToolbar-root": {"gap": "4px"},
    "& .header .title": {"color": "text.primary", "fontSize": "1.25rem", "fontWeight": 600},
    f"@media (max-width: {NARROW_BREAKPOINT - 0.05}px)": {"& .header .title": {"display": "none"}},
}

# On a dashboard the top bar also holds the dashboard's actions, so the brand
# gives way to them on screens narrower than this.
_BRAND_BREAKPOINT = 1280

DASHBOARD_HEADER_SX = {
    f"@media (max-width: {_BRAND_BREAKPOINT - 0.05}px)": {"& .header .title": {"display": "none"}},
}

_CRUMB_SX = {
    "textTransform": "none",
    "minWidth": 0,
    "px": 1,
    "fontSize": "0.9375rem",
    "fontWeight": 400,
    "color": "text.secondary",
    "whiteSpace": "nowrap",
}

_CURRENT_CRUMB_SX = {**_CRUMB_SX, "color": "text.primary", "fontWeight": 500}

_RAIL_ITEM_SX = {
    "flexDirection": "column",
    "minWidth": 0,
    "width": "72px",
    "px": 0,
    "py": 0.5,
    "textTransform": "none",
    "fontSize": "0.75rem",
    "fontWeight": 500,
    "lineHeight": 1.4,
    "color": "text.secondary",
    "& .MuiButton-startIcon": {
        "m": 0,
        "mb": "4px",
        "px": "18px",
        "py": "4px",
        "borderRadius": "16px",
        "transition": "background-color 0.15s",
    },
    "& .MuiButton-endIcon": {"display": "none"},
    "&:hover": {"bgcolor": "transparent"},
    "&:hover .MuiButton-startIcon": {"bgcolor": "action.hover"},
}

_RAIL_ACTIVE_SX = {
    **_RAIL_ITEM_SX,
    "color": "text.primary",
    "& .MuiButton-startIcon": {
        **_RAIL_ITEM_SX["& .MuiButton-startIcon"],
        "color": "primary.main",
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.14)",
    },
}


class Crumb(t.NamedTuple):
    """One breadcrumb: a label and the path it links to (None for the current page)."""

    label: str
    path: str | None = None


class AppShell:
    """The chrome around a FlowDashApp page in the ``rail`` navigation variant.

    It provides the top bar content placed in the page header (a menu button
    for narrow screens, the breadcrumb with a dashboard switcher, and
    :attr:`search_slot`) and the navigation rail placed left of the page
    content by :meth:`wrap`. The theme and contextbar toggles are the page's
    own and follow the header content.

    Call :meth:`sync` with the route after every navigation and
    :meth:`refresh` when the dashboards or pages the user can see change.
    """

    def __init__(self, app: FlowDashApp):
        self._app = app
        location = pn.state.location
        self._path = "/" + (location.pathname if location is not None else "").strip("/")

        self.menu_button = pmui.IconButton(
            icon="menu",
            description="Open navigation",
            size="medium",
            margin=(0, 4, 0, 0),
            stylesheets=[_HIDE_WHEN_WIDE],
        )
        self.menu_button.on_click(lambda _event: self.nav_drawer.param.update(open=True))
        self.breadcrumb = pn.Row(margin=0, align="center", min_width=0)
        self.dashboard_switcher = pmui.MenuButton(
            label="",
            variant="text",
            size="small",
            margin=0,
            sx={**_CURRENT_CRUMB_SX, "& .MuiButton-endIcon": {"ml": 0}},
        )
        self.dashboard_switcher.on_click(self._on_switch)
        # Filled by the command palette with its search field.
        self.search_slot = pn.Row(margin=0, align="center")
        # Filled with the dashboard bar's lead (info, health or save state) and
        # actions while a dashboard is shown; the breadcrumb is its title.
        self.dashboard_lead = pn.Row(margin=(0, 0, 0, 4), align="center", min_width=0)
        self.dashboard_actions = pn.Row(margin=0, align="center")

        self.rail = pmui.Paper(
            direction="column",
            elevation=0,
            width=80,
            margin=0,
            sizing_mode="stretch_height",
            sx={
                "alignItems": "center",
                "gap": 1,
                "py": 1.5,
                "borderRadius": 0,
                "borderRight": 1,
                "borderColor": "divider",
                "bgcolor": "background.paper",
                "flexShrink": 0,
            },
            stylesheets=[_HIDE_WHEN_NARROW],
        )
        self._nav_list = pmui.MenuList(items=[], dense=False, on_click=self._on_nav_list_click)
        self.nav_drawer = pmui.Drawer(
            pmui.Typography(app.title, variant="h6", margin=(16, 16, 8, 16)),
            self._nav_list,
            anchor="left",
            variant="temporary",
            size=280,
        )
        self._header = [
            self.menu_button,
            self.breadcrumb,
            self.dashboard_lead,
            pn.layout.HSpacer(),
            self.dashboard_actions,
            self.search_slot,
            self.nav_drawer,
        ]
        self.refresh()

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    @property
    def header(self) -> list:
        """Items for the page header, left to right."""
        return self._header

    def wrap(self, content):
        """Pair page *content* with the navigation rail."""
        return pn.Row(self.rail, content, sizing_mode="stretch_both", margin=0)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def sync(self, path: str):
        """Point the breadcrumb and the active rail item at the route *path*."""
        self._path = "/" + path.strip("/")
        with pn.io.hold():
            self._mark_active()
            self._render_breadcrumb()

    def refresh(self):
        """Rebuild the rail and switcher after the accessible dashboards or pages changed."""
        with pn.io.hold():
            self._render_rail()
            self._mark_active()
            self._render_breadcrumb()

    # ------------------------------------------------------------------
    # Navigation rail
    # ------------------------------------------------------------------

    def _active(self) -> str:
        path = self._path
        if path.startswith(app_module.DASH_ROUTE_PREFIX):
            return "dashboards"
        if path == app_module.COMPONENTS_ROUTE:
            return "components"
        if path == "/":
            return "home"
        if path.strip("/") in {e.page_path.strip("/") for e in self._page_entries()}:
            return "pages"
        return ""

    def _page_entries(self) -> list:
        entries = self._app._accessible_page_entries().values()
        return sorted(entries, key=lambda e: (e.section, e.name))

    def _dashboard_items(self) -> list[dict]:
        app = self._app
        dashboards = app.store.list_accessible(app._identity, default_allow=app._default_allow())
        return [
            {
                "label": d.title,
                "icon": "dashboard",
                "path": f"{app_module.DASH_ROUTE_PREFIX}{d.dashboard_id}",
            }
            for d in sorted(dashboards, key=lambda d: d.title.lower())
        ]

    def _page_items(self) -> list[dict]:
        items: list = []
        section = None
        for entry in self._page_entries():
            if section is not None and entry.section != section:
                items.append(None)
            section = entry.section
            items.append(
                {
                    "label": entry.title,
                    "icon": self._app._section_icon(entry.section),
                    "path": entry.page_path,
                }
            )
        return items

    def rail_items(self) -> list[dict]:
        """Return the rail destinations: a ``path`` to open, ``items`` for a menu or an ``href``."""
        items = [{"key": "home", "label": "Home", "icon": "home", "path": "/"}]
        if self._app.home_dashboard:
            # "/" shows the home dashboard, so the dashboards are listed in a menu.
            items.append(
                {
                    "key": "dashboards",
                    "label": "Dashboards",
                    "icon": "space_dashboard",
                    "items": self._dashboard_items(),
                }
            )
        else:
            items.append(
                {
                    "key": "dashboards",
                    "label": "Dashboards",
                    "icon": "space_dashboard",
                    "path": "/",
                }
            )
        items.append(
            {
                "key": "components",
                "label": "Components",
                "icon": "widgets",
                "path": app_module.COMPONENTS_ROUTE,
            }
        )
        pages = self._page_items()
        if pages:
            items.append({"key": "pages", "label": "Pages", "icon": "article", "items": pages})
        items.append({"key": "help", "label": "Help", "icon": "help_outline", "href": DOCS_URL})
        return items

    def _render_rail(self):
        objects = []
        self._rail_buttons = {}
        items = self.rail_items()
        for item in items:
            sx = _RAIL_ITEM_SX
            if "items" in item:
                button = pmui.MenuButton(
                    label=item["label"],
                    icon=item["icon"],
                    items=item["items"],
                    variant="text",
                    color="default",
                    icon_size="1.5rem",
                    margin=(0, 4),
                    sx=sx,
                )
                button.on_click(self._on_menu_item)
            else:
                button = pmui.Button(
                    label=item["label"],
                    icon=item["icon"],
                    variant="text",
                    color="default",
                    icon_size="1.5rem",
                    margin=(0, 4),
                    sx=sx,
                )
                if "href" in item:
                    button.param.update(href=item["href"], target="_blank")
                else:
                    button.on_click(lambda _event, path=item["path"]: self._navigate(path))
            if item["key"] == "help":
                objects.append(pn.layout.VSpacer())
            self._rail_buttons[item["key"]] = button
            objects.append(button)
        self.rail[:] = objects
        self._nav_list.items = self._drawer_items(items)

    def _mark_active(self):
        active = self._active()
        for key, button in self._rail_buttons.items():
            sx = _RAIL_ACTIVE_SX if key == active else _RAIL_ITEM_SX
            if button.sx != sx:
                button.sx = sx

    def _drawer_items(self, rail_items: list[dict]) -> list[dict]:
        """Return the rail as a list for the narrow-screen drawer, menus expanded inline."""
        items = []
        for item in rail_items:
            entry = {k: item[k] for k in ("label", "icon", "path", "href") if k in item}
            if "items" in item:
                entry["items"] = [i for i in item["items"] if i is not None]
                entry["selectable"] = False
            items.append(entry)
        return items

    def _on_nav_list_click(self, item):
        if "path" not in item:
            return
        self.nav_drawer.open = False
        self._navigate(item["path"])

    def _on_menu_item(self, item):
        if isinstance(item, dict) and "path" in item:
            self._navigate(item["path"])

    def _navigate(self, path: str):
        self._app._on_nav_click({"path": path})

    # ------------------------------------------------------------------
    # Breadcrumb
    # ------------------------------------------------------------------

    def crumbs(self) -> list[Crumb]:
        """Return the breadcrumb trail for the current route, the current page last."""
        path = self._path
        home = Crumb("Home", "/")
        if path.startswith(app_module.DASH_ROUTE_PREFIX):
            return [Crumb("Dashboards", "/"), Crumb(self._dashboard_title())]
        if path == "/":
            if self._app._current_dashboard is not None:
                return [Crumb(self._dashboard_title())]
            return []
        if path == app_module.COMPONENTS_ROUTE:
            return [home, Crumb("Components")]
        for entry in self._page_entries():
            if path.strip("/") == entry.page_path.strip("/"):
                return [home, Crumb(entry.section.replace("_", " ")), Crumb(entry.title)]
        return [home]

    def _dashboard_title(self) -> str:
        current = self._app._current_dashboard
        prefix = app_module.DASH_ROUTE_PREFIX
        on_route = not self._path.startswith(prefix) or (
            current is not None and self._path[len(prefix) :] == current.dashboard_id
        )
        # Until the dashboard has loaded, the previous one's title would be wrong.
        return current.title if current is not None and on_route else "Dashboard"

    def _render_breadcrumb(self):
        crumbs = self.crumbs()
        parts: list = []
        for i, crumb in enumerate(crumbs):
            last = i == len(crumbs) - 1
            if i:
                parts.append(
                    pmui.Typography(
                        "\u203a",
                        margin=0,
                        sx={"color": "text.secondary", "fontSize": "1.25rem", "lineHeight": 1},
                        stylesheets=[_HIDE_WHEN_NARROW],
                    )
                )
            if last and self._is_dashboard_route():
                self.dashboard_switcher.param.update(
                    label=crumb.label,
                    items=[*self._dashboard_items(), None, _ALL_DASHBOARDS],
                )
                parts.append(self.dashboard_switcher)
                continue
            button = pmui.Button(
                label=crumb.label,
                variant="text",
                color="default",
                size="small",
                margin=0,
                sx=(_CURRENT_CRUMB_SX if last else _CRUMB_SX)
                if crumb.path is not None
                else {**(_CURRENT_CRUMB_SX if last else _CRUMB_SX), "pointerEvents": "none"},
                stylesheets=[] if last else [_HIDE_WHEN_NARROW],
            )
            if crumb.path is not None:
                button.on_click(lambda _event, path=crumb.path: self._navigate(path))
            parts.append(button)
        self.breadcrumb[:] = parts
        self._render_dashboard_slots()

    def _showing_dashboard(self) -> bool:
        current = self._app._current_dashboard
        prefix = app_module.DASH_ROUTE_PREFIX
        if current is None:
            return False
        if self._path.startswith(prefix):
            return self._path[len(prefix) :] == current.dashboard_id
        return self._path == "/"

    def _render_dashboard_slots(self):
        """Seat the dashboard bar's parts in the top bar while a dashboard is shown."""
        bar = self._app._editor._dashboard_bar
        showing = self._showing_dashboard()
        lead, actions = ([bar.lead], [bar.actions]) if showing else ([], [])
        if list(self.dashboard_lead) != lead:
            self.dashboard_lead[:] = lead
        if list(self.dashboard_actions) != actions:
            self.dashboard_actions[:] = actions
        for palette in self.search_slot:
            if "compact" in palette.param:
                palette.compact = showing
        self._app._set_dashboard_header(showing)

    def _is_dashboard_route(self) -> bool:
        return self._path.startswith(app_module.DASH_ROUTE_PREFIX) or (
            self._path == "/" and self._app._current_dashboard is not None
        )

    def _on_switch(self, item):
        if isinstance(item, dict) and "path" in item:
            self._navigate(item["path"])


_ALL_DASHBOARDS = {"label": "All dashboards", "icon": "apps", "path": "/"}
