"""The app shell: top bar content (breadcrumb, search slot) and the navigation rail."""

from __future__ import annotations

import logging
import typing as t

import panel as pn
import panel_material_ui as pmui

# The app module imports this one; its route constants are only read at runtime.
from panel_flowdash import app as app_module
from panel_flowdash.sidebar import SidebarAction, SidebarItem, SidebarSection

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger("panel_flowdash")

DOCS_URL = "https://panel-extensions.github.io/panel-flowdash/"


def _is_external_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def _normalize_route(url: str) -> str:
    return "/" + url.partition("?")[0].strip("/")


# Material UI's sm breakpoint, below which the page moves its rail into the
# sidebar drawer: the rail buttons show above it, the navigation list below it.
NARROW_BREAKPOINT = 600

_HIDE_WHEN_NARROW = (
    f"@media (max-width: {NARROW_BREAKPOINT - 0.05}px) {{ :host {{ display: none !important; }} }}"
)
_HIDE_WHEN_WIDE = (
    f"@media (min-width: {NARROW_BREAKPOINT}px) {{ :host {{ display: none !important; }} }}"
)

HEADER_SX = {
    # The top bar is one 56px row; the page's spacers below it follow suit.
    "& .MuiToolbar-root": {"minHeight": "56px"},
    "& .header .MuiToolbar-root": {"gap": "4px"},
    "& .header .title": {"fontSize": "1.25rem", "fontWeight": 600},
    # The header items may get narrower than their content, so the breadcrumb
    # can truncate, while the brand keeps its width.
    "& .header .MuiToolbar-root > .MuiBox-root": {"minWidth": 0},
    "& .header .MuiToolbar-root > .MuiBox-root:has(.title)": {"flexShrink": 0},
    f"@media (max-width: {NARROW_BREAKPOINT - 0.05}px)": {"& .header .title": {"display": "none"}},
}

# On a dashboard the top bar also holds the dashboard's actions, so the brand
# gives way to them on screens narrower than this. The edit actions are wider.
_BRAND_BREAKPOINT = 1280
_EDIT_BRAND_BREAKPOINT = 1600


def _hide_brand_below(width: int) -> dict:
    return {f"@media (max-width: {width - 0.05}px)": {"& .header .title": {"display": "none"}}}


DASHBOARD_HEADER_SX = _hide_brand_below(_BRAND_BREAKPOINT)
DASHBOARD_EDIT_HEADER_SX = _hide_brand_below(_EDIT_BRAND_BREAKPOINT)

# The breadcrumb is the one part of the top bar that gives way when space
# runs out: its current crumb truncates with an ellipsis.
_SHRINK = ":host { flex: 0 1 auto !important; min-width: 0 !important; }"
# The dashboard switcher truncates its title but keeps room for a few letters
# and its arrow, so the items after it never cover it.
_SHRINK_SWITCHER = ":host { flex: 0 1 auto !important; min-width: 64px !important; }"
# On a dashboard the top bar also holds its actions, so the crumbs before the
# switcher give way where the brand does, before the title has to truncate; the
# rail leads to them as well.
_HIDE_WHEN_COMPACT = (
    f"@media (max-width: {_BRAND_BREAKPOINT - 0.05}px) {{ :host {{ display: none !important; }} }}"
)

_CRUMB_SX = {
    "textTransform": "none",
    "minWidth": 0,
    "px": 1,
    "fontSize": "0.9375rem",
    "fontWeight": 400,
    "color": "text.secondary",
    "whiteSpace": "nowrap",
}

_CURRENT_CRUMB_SX = {
    **_CRUMB_SX,
    "color": "text.primary",
    "fontWeight": 500,
    # Block layout, as text-overflow cannot ellipsize the text of a flex container.
    "display": "block",
    "maxWidth": "100%",
    "overflow": "hidden",
    "textOverflow": "ellipsis",
}

# The switcher's arrow sits in the padding, so the ellipsis stops before it.
# The block layout gives the icon's box the text's line height, so it centres
# its arrow itself rather than leaving it at the top of that box.
_SWITCHER_SX = {
    **_CURRENT_CRUMB_SX,
    "pr": "28px",
    "& .MuiButton-endIcon": {
        "position": "absolute",
        "right": "4px",
        "top": "50%",
        "transform": "translateY(-50%)",
        "m": 0,
        "display": "flex",
        "alignItems": "center",
    },
}

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
        "color": "primary.text",
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.14)",
    },
}


class Crumb(t.NamedTuple):
    """One breadcrumb: a label and the path it links to (None for the current page)."""

    label: str
    path: str | None = None


class AppShell:
    """The chrome around a FlowDashApp page in the ``rail`` navigation variant.

    It provides the top bar content placed in the page header (the breadcrumb
    with a dashboard switcher, and :attr:`search_slot`) and the navigation
    placed in the page's rail (:attr:`rail_content`): the rail buttons, and on
    narrow screens, where the page moves its rail into the sidebar drawer, a
    navigation list. The menu, theme and contextbar toggles are the page's own.

    Call :meth:`sync` with the route after every navigation and
    :meth:`refresh` when the dashboards or pages the user can see change.
    """

    def __init__(self, app: FlowDashApp):
        self._app = app
        location = pn.state.location
        self._path = "/" + (location.pathname if location is not None else "").strip("/")

        self.breadcrumb = pn.Row(margin=0, align="center", min_width=0, stylesheets=[_SHRINK])
        self.dashboard_switcher = pmui.MenuButton(
            label="",
            variant="text",
            size="small",
            margin=0,
            sx=_SWITCHER_SX,
            stylesheets=[_SHRINK_SWITCHER],
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
            margin=0,
            sizing_mode="stretch_both",
            sx={
                "alignItems": "center",
                "gap": 1,
                "py": 1.5,
                "borderRadius": 0,
                "bgcolor": "transparent",
            },
            stylesheets=[_HIDE_WHEN_NARROW],
        )
        self._rail_buttons: dict[str, pmui.Button | pmui.MenuButton] = {}
        self._rail_layout: list[tuple] | None = None
        self._rendered_section_items: dict[str, list[SidebarItem]] = {}
        self._nav_list = pmui.MenuList(
            items=[],
            dense=False,
            sizing_mode="stretch_width",
            on_click=self._on_nav_list_click,
        )
        self.nav_list = pn.Column(
            pmui.Typography(app.title, variant="h6", margin=(8, 16, 8, 16)),
            self._nav_list,
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_HIDE_WHEN_WIDE],
        )
        app._editor._dashboard_bar.param.watch(lambda _event: self._render_breadcrumb(), "layout")
        app._editor.param.watch(lambda _event: self._render_dashboard_slots(), "editable")
        self._header = [
            self.breadcrumb,
            self.dashboard_lead,
            pn.layout.HSpacer(),
            self.dashboard_actions,
            self.search_slot,
        ]
        self.refresh()

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    @property
    def header(self) -> list:
        """Items for the page header, left to right."""
        return self._header

    @property
    def rail_content(self) -> list:
        """Items for the page rail: the rail buttons and, for narrow screens, the list."""
        return [self.rail, self.nav_list]

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
        if self._is_admin_route():
            return "admin"
        if path == "/":
            return "home"
        for section in self._visible_sections():
            if self._section_matches_path(section, path):
                return section.id
        if path.strip("/") in {e.page_path.strip("/") for e in self._page_entries()}:
            return "pages"
        return ""

    def _ordered_sections(self) -> list[SidebarSection]:
        return sorted(self._app.sections, key=lambda s: s.order)

    def _section_visible(self, section: SidebarSection) -> bool:
        if section.visible is None:
            return True
        try:
            return bool(section.visible(self._app._identity))
        except Exception:
            logger.exception(
                "SidebarSection %r visible predicate failed; hiding section", section.id
            )
            return False

    def _visible_sections(self) -> list[SidebarSection]:
        return [s for s in self._ordered_sections() if self._section_visible(s)]

    def _section_by_id(self, section_id: str) -> SidebarSection | None:
        return next((s for s in self._app.sections if s.id == section_id), None)

    def _section_path(self, section: SidebarSection) -> str | None:
        path = section.path() if callable(section.path) else section.path
        return path or None

    def _section_items(self, section: SidebarSection) -> list[SidebarItem]:
        if section.items is None:
            return []
        try:
            raw = section.items(self._app._identity) or ()
        except Exception:
            logger.exception("SidebarSection %r items callable failed", section.id)
            return []
        items: list[SidebarItem] = []
        for item in raw:
            if not isinstance(item, SidebarItem):
                raise TypeError(
                    f"SidebarSection {section.id!r} items must return SidebarItem instances, "
                    f"got {type(item).__name__}."
                )
            items.append(item)
        return items

    @staticmethod
    def _item_dict(item: SidebarItem) -> dict:
        entry: dict[str, t.Any] = {"label": item.title, "icon": item.icon}
        if _is_external_url(item.url):
            entry["href"] = item.url
        else:
            entry["path"] = item.url
        if item.subtitle:
            entry["secondary"] = item.subtitle
        return entry

    @staticmethod
    def _action_dict(section_id: str, action: SidebarAction) -> dict:
        entry: dict[str, t.Any] = {"label": action.label, "icon": action.icon}
        if isinstance(action.action, str):
            if _is_external_url(action.action):
                entry["href"] = action.action
            else:
                entry["path"] = action.action
        else:
            entry["section_action"] = section_id
        return entry

    def _section_rail_item(self, section: SidebarSection) -> dict:
        section_path = self._section_path(section)
        if section_path is not None:
            self._rendered_section_items.pop(section.id, None)
            key = "href" if _is_external_url(section_path) else "path"
            return {
                "key": section.id,
                "label": section.label,
                "icon": section.icon,
                key: section_path,
            }
        section_items = self._section_items(section)
        self._rendered_section_items[section.id] = section_items
        menu_items: list[dict | None] = [self._item_dict(item) for item in section_items]
        if section.new_action is not None:
            if menu_items:
                menu_items.append(None)
            menu_items.append(self._action_dict(section.id, section.new_action))
        return {
            "key": section.id,
            "label": section.label,
            "icon": section.icon,
            "items": menu_items,
        }

    def _section_matches_path(self, section: SidebarSection, path: str) -> bool:
        section_path = self._section_path(section)
        if section_path is not None and not _is_external_url(section_path):
            sp = _normalize_route(section_path)
            if sp != "/" and (path == sp or path.startswith(f"{sp}/")):
                return True
        items = self._rendered_section_items.get(section.id)
        if items is None:
            items = self._section_items(section)
        for item in items:
            if not _is_external_url(item.url):
                item_path = _normalize_route(item.url)
                if item_path != "/" and path == item_path:
                    return True
        if section.new_action is not None and isinstance(section.new_action.action, str):
            if not _is_external_url(section.new_action.action):
                action_path = _normalize_route(section.new_action.action)
                if action_path != "/" and path == action_path:
                    return True
        return False

    def _run_section_action(self, section_id: str):
        section = next((s for s in self._visible_sections() if s.id == section_id), None)
        if section is None or section.new_action is None:
            return
        action = section.new_action.action
        if isinstance(action, str):
            self._navigate(action)
        else:
            action()

    def _page_entries(self) -> list:
        entries = self._app._accessible_page_entries().values()
        return sorted(entries, key=lambda e: (e.section, e.name))

    def _dashboard_items(self) -> list[dict]:
        section = self._section_by_id("dashboards")
        if section is not None and section.items is not None:
            return [self._item_dict(item) for item in self._section_items(section)]
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
        items: list[dict] = [{"key": "home", "label": "Home", "icon": "home", "path": "/"}]
        for section in self._visible_sections():
            items.append(self._section_rail_item(section))
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
        if self._app._is_admin:
            items.append(
                {
                    "key": "admin",
                    "label": "Admin",
                    "icon": "admin_panel_settings",
                    "path": app_module.ADMIN_ROUTE,
                }
            )
        items.append({"key": "help", "label": "Help", "icon": "help_outline", "href": DOCS_URL})
        return items

    def _render_rail(self):
        items = self.rail_items()
        drawer_items = self._drawer_items(items)
        self._nav_list.items = drawer_items
        # Set expanded after items: TreeLikeBase._update_expanded remaps expanded on items change.
        self._nav_list.expanded = [(i,) for i, item in enumerate(drawer_items) if "items" in item]
        layout = [
            (
                item["key"],
                item["label"],
                item["icon"],
                item.get("path"),
                item.get("href"),
                "items" in item,
            )
            for item in items
        ]
        if layout == self._rail_layout:
            # Replacing the buttons would drop a click that reaches the old ones
            # before the browser renders the new ones, e.g. right after a save.
            for item in items:
                if "items" in item:
                    self._rail_buttons[item["key"]].items = item["items"]
            return
        self._rail_layout = layout
        objects = []
        self._rail_buttons = {}
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
            if item["key"] == ("admin" if self._app._is_admin else "help"):
                objects.append(pn.layout.VSpacer())
            self._rail_buttons[item["key"]] = button
            objects.append(button)
        self.rail[:] = objects

    def _nav_list_active(self, active: str) -> tuple[int, ...] | None:
        path = self._path
        for si, item in enumerate(self._nav_list.items):
            if not isinstance(item, dict):
                continue
            item_path = item.get("path")
            if item_path is not None and _normalize_route(item_path) == path:
                return (si,)
            for pi, child in enumerate(item.get("items", [])):
                if not isinstance(child, dict):
                    continue
                child_path = child.get("path")
                if child_path is not None and _normalize_route(child_path) == path:
                    return (si, pi)
        if active:
            for si, item in enumerate(self._nav_list.items):
                if (
                    isinstance(item, dict)
                    and item.get("key") == active
                    and item.get("selectable", True)
                ):
                    return (si,)
        return None

    def _mark_active(self):
        active = self._active()
        for key, button in self._rail_buttons.items():
            sx = _RAIL_ACTIVE_SX if key == active else _RAIL_ITEM_SX
            if button.sx != sx:
                button.sx = sx
        nav_active = self._nav_list_active(active)
        if self._nav_list.active != nav_active:
            self._nav_list.active = nav_active

    def _drawer_items(self, rail_items: list[dict]) -> list[dict]:
        """Return the rail as a list for the narrow-screen drawer, menus expanded inline."""
        items = []
        for item in rail_items:
            entry = {k: item[k] for k in ("key", "label", "icon", "path", "href") if k in item}
            if "items" in item:
                entry["items"] = [i for i in item["items"] if i is not None]
                entry["selectable"] = False
            items.append(entry)
        return items

    def _on_nav_list_click(self, item):
        if not isinstance(item, dict):
            return
        if "section_action" in item:
            self._app._page.sidebar_open = False
            self._run_section_action(item["section_action"])
            return
        if "path" not in item:
            return
        # On narrow screens the list sits in the page's temporary sidebar drawer.
        self._app._page.sidebar_open = False
        self._navigate(item["path"])

    def _on_menu_item(self, item):
        if not isinstance(item, dict):
            return
        if "section_action" in item:
            self._run_section_action(item["section_action"])
        elif "path" in item:
            self._navigate(item["path"])

    def _navigate(self, path: str):
        route, sep, query = path.partition("?")
        if sep:
            self._app._request_navigation(route, search=f"?{query}")
        else:
            self._app._on_nav_click({"path": route})

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
        if self._is_admin_route() and self._app._is_admin:
            page = self._app._admin_page
            section = page.sections.get(page.section) if page is not None else None
            if section is None:
                return [Crumb("Admin")]
            return [Crumb("Admin", app_module.ADMIN_ROUTE), Crumb(section.label)]
        for entry in self._page_entries():
            if path.strip("/") == entry.page_path.strip("/"):
                return [home, Crumb(entry.section.replace("_", " ")), Crumb(entry.title)]
        for sidebar_section in self._visible_sections():
            section_path = self._section_path(sidebar_section)
            if section_path is not None and not _is_external_url(section_path):
                sp = _normalize_route(section_path)
                if sp != "/" and path == sp:
                    return [home, Crumb(sidebar_section.label)]
            items = self._rendered_section_items.get(sidebar_section.id)
            if items is None:
                items = self._section_items(sidebar_section)
            for item in items:
                if not _is_external_url(item.url) and _normalize_route(item.url) == path:
                    parent_path = (
                        section_path
                        if section_path is not None and not _is_external_url(section_path)
                        else None
                    )
                    return [home, Crumb(sidebar_section.label, parent_path), Crumb(item.title)]
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
        dashboard = self._is_dashboard_route()
        beside_actions = dashboard and self._app._editor._dashboard_bar.in_header
        hide_before_last = [_HIDE_WHEN_NARROW, *([_HIDE_WHEN_COMPACT] if beside_actions else [])]
        parts: list = []
        for i, crumb in enumerate(crumbs):
            last = i == len(crumbs) - 1
            if i:
                parts.append(
                    pmui.Typography(
                        "\u203a",
                        margin=0,
                        align="center",
                        sx={"color": "text.secondary", "fontSize": "1.25rem", "lineHeight": 1},
                        stylesheets=hide_before_last,
                    )
                )
            if last and dashboard:
                self.dashboard_switcher.param.update(
                    label=crumb.label,
                    # The full title, should the crumb be truncated.
                    description=crumb.label,
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
                stylesheets=[_SHRINK] if last else hide_before_last,
                description=crumb.label if last else None,
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
        showing = self._showing_dashboard() and bar.in_header
        lead, actions = ([bar.lead], [bar.actions]) if showing else ([], [])
        if list(self.dashboard_lead) != lead:
            self.dashboard_lead[:] = lead
        if list(self.dashboard_actions) != actions:
            self.dashboard_actions[:] = actions
        for palette in self.search_slot:
            if "compact" in palette.param:
                palette.compact = showing
        editing = self._app._editor.editable
        self._app._set_dashboard_header(("edit" if editing else "view") if showing else None)

    def _is_admin_route(self) -> bool:
        route = app_module.ADMIN_ROUTE
        return self._path == route or self._path.startswith(f"{route}/")

    def _is_dashboard_route(self) -> bool:
        return self._path.startswith(app_module.DASH_ROUTE_PREFIX) or (
            self._path == "/" and self._app._current_dashboard is not None
        )

    def _on_switch(self, item):
        if isinstance(item, dict) and "path" in item:
            self._navigate(item["path"])


_ALL_DASHBOARDS = {"label": "All dashboards", "icon": "apps", "path": "/"}
