"""Public specification for navigation sections in the FlowDash sidebar rail and drawer.

An application or extension adds a navigation section (such as Reports or a
Data catalog) by passing :class:`SidebarSection` objects to
:class:`~panel_flowdash.app.FlowDashApp` via its ``sections`` parameter or by
calling :meth:`~panel_flowdash.app.FlowDashApp.add_section`.

Every callback on a section (``items``, ``visible``, ``path``, ``view`` and
:attr:`SidebarAction.action`) receives a :class:`SidebarContext` carrying the
session's ``app``, ``user`` (:class:`~panel_flowdash.Identity`) and
``section``. Sections can also serve their own routes by setting both
``route`` and ``view``: FlowDash then routes ``/<route>`` and
``/<route>/...`` to ``view(ctx, path)`` without registering them under Pages.
Pass routed sections to :meth:`~panel_flowdash.app.FlowDashApp.build_routes`
so direct loads and browser reloads resolve cleanly.

The built-in Dashboards navigation uses the same :class:`SidebarSection`
mechanism (with ``id="dashboards"`` and ``order=0``). Sidebar sections render
only when ``FlowDashApp.nav_variant`` is ``"rail"`` (the default).
"""

from __future__ import annotations

import re
import typing as t
from collections.abc import Callable, Sequence
from dataclasses import dataclass

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.auth import Identity

__all__ = [
    "RESERVED_ROUTE_PREFIXES",
    "RESERVED_SECTION_IDS",
    "SidebarAction",
    "SidebarContext",
    "SidebarItem",
    "SidebarSection",
]

#: Section ids reserved for built-in rail destinations.
RESERVED_SECTION_IDS: frozenset[str] = frozenset({"home", "components", "pages", "help", "admin"})

#: Top-level URL path prefixes reserved for built-in app, auth and Bokeh endpoints.
RESERVED_ROUTE_PREFIXES: frozenset[str] = frozenset(
    {
        "access-denied",
        "admin",
        "autoload.js",
        "components",
        "dash",
        "login",
        "logout",
        "metadata",
        "static",
        "ws",
    }
)

# Valid CSS-safe identifiers for section ids (used in rail menu classes).
_SECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class SidebarContext:
    """Session context passed to :class:`SidebarSection` and :class:`SidebarAction` callables.

    Attributes
    ----------
    app
        The session's :class:`~panel_flowdash.app.FlowDashApp` instance.
    user
        The session's resolved :class:`~panel_flowdash.Identity`.
    section
        The :class:`SidebarSection` being evaluated.
    """

    app: FlowDashApp
    user: Identity
    section: SidebarSection

    @property
    def is_admin(self) -> bool:
        """Whether this session administers the project (cached per session).

        Use this for showing or hiding UI. Before performing a privileged write
        or admin action, re-check with :meth:`still_admin`.
        """
        return self.app.is_admin

    def still_admin(self) -> bool:
        """Whether this session is still an admin, with the project file read again."""
        return self.app.still_admin()

    def navigate(self, url: str) -> None:
        """Navigate this session to *url*.

        Internal routes open through the app's router; external ``http://`` and
        ``https://`` URLs open in a new browser tab.
        """
        self.app._navigate_url(url)

    def refresh(self, section_id: str | None = None) -> None:
        """Refresh cached section items; defaults to this section's id."""
        self.app.refresh_sections(self.section.id if section_id is None else section_id)


@dataclass(frozen=True)
class SidebarItem:
    """One destination listed inside a :class:`SidebarSection`.

    Attributes
    ----------
    title
        Display label shown in the rail menu and narrow-screen drawer.
    url
        App route (e.g. ``"/reports/q4"`` or ``"/reports/view?id=q1"``) opened
        through the app's router, or an external ``http://``/``https://`` URL
        opened in a new tab.
    subtitle
        Optional secondary text such as the owner or last updated time. Shown
        under the item title in the narrow-screen drawer; desktop rail menus
        show titles only (``pmui.MenuButton`` does not render secondary text).
    icon
        Optional Material icon name for the item.
    """

    title: str
    url: str
    subtitle: str | None = None
    icon: str | None = None

    def __post_init__(self):  # noqa: D105
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("SidebarItem.title must be a non-empty string.")
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("SidebarItem.url must be a non-empty string.")


@dataclass(frozen=True)
class SidebarAction:
    """An optional create action (e.g. "+ New") shown at the end of a :class:`SidebarSection`.

    Attributes
    ----------
    label
        Text shown for the action, e.g. ``"New report"``.
    action
        Either a callable ``action(ctx: SidebarContext)`` invoked when clicked,
        or a route/URL string opened through the app's router (or in a new tab
        if external).
    icon
        Material icon name shown beside the label. Defaults to ``"add"``.
    """

    label: str
    action: Callable[[SidebarContext], t.Any] | str
    icon: str = "add"

    def __post_init__(self):  # noqa: D105
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("SidebarAction.label must be a non-empty string.")
        if not isinstance(self.action, str) and not callable(self.action):
            raise TypeError(
                "SidebarAction.action must be a callable or a route/URL string, "
                f"got {type(self.action).__name__}."
            )
        if isinstance(self.action, str) and not self.action.strip():
            raise ValueError("SidebarAction.action route must be a non-empty string.")


@dataclass(frozen=True)
class SidebarSection:
    """Specification for a navigation section in the FlowDash rail and drawer.

    Sidebar sections render only with ``nav_variant="rail"`` (the default).
    A section may either:

    - list destinations via ``items`` and/or ``new_action`` (rendered as a
      menu in the desktop rail and an expanded group in the narrow drawer),
    - link directly to a landing route via ``path`` (rendered as a single
      button), or
    - pass a callable ``path(ctx)`` returning a route string or ``None`` to
      switch dynamically between direct-link mode and menu mode (as the
      built-in Dashboards section does for ``home_dashboard``).

    Setting both ``route`` and ``view`` lets the section serve its own routes
    at ``/<route>`` and ``/<route>/<subpath>`` through ``view(ctx, subpath)``
    without appearing in Pages. When ``route`` and ``view`` are set without
    ``items``, ``new_action`` or ``path``, the section links directly to
    ``/<route>``.

    Attributes
    ----------
    id
        Unique section identifier (e.g. ``"dashboards"``, ``"reports"``).
        Must start with an ASCII letter or digit and contain only ASCII
        letters, digits, ``"-"`` or ``"_"``. Built-in ids in
        :data:`RESERVED_SECTION_IDS` are rejected. Registering a section with
        an existing ``id`` replaces the earlier one.
    label
        Heading displayed in the rail button, narrow-screen drawer and
        breadcrumbs.
    icon
        Material icon name for the section (e.g. ``"space_dashboard"``).
    items
        Optional callable ``items(ctx: SidebarContext) -> Sequence[SidebarItem]``
        returning the destinations visible in this session. Results are cached
        per refresh and re-fetched when the section's menu or the narrow drawer
        opens or when :meth:`~panel_flowdash.app.FlowDashApp.refresh_sections`
        is called.
    order
        Sort key controlling section order (ascending). Ties preserve
        registration order. The built-in Dashboards section uses ``0``; custom
        sections default to ``100`` so they appear after Dashboards.
    new_action
        Optional :class:`SidebarAction` (such as "+ New") placed at the end of
        the section's items.
    visible
        Optional predicate ``visible(ctx: SidebarContext) -> bool`` deciding
        whether the section appears in the navigation and whether its ``route``
        may be opened.
    path
        Optional landing route (or callable ``path(ctx: SidebarContext) -> str | None``).
        A static string ``path`` cannot be combined with ``items`` or
        ``new_action``; use ``route`` and ``view`` to serve a landing page
        alongside items, or a callable ``path(ctx)`` to switch modes dynamically.
    route
        Optional URL prefix served by this section, e.g. ``"reports"`` for
        ``/reports`` and ``/reports/...``. Leading and trailing slashes are
        stripped. Must be paired with ``view``, must not use a prefix in
        :data:`RESERVED_ROUTE_PREFIXES`, and must not clash with a registry
        page section. Subpaths whose final segment is ``ws``, ``metadata``,
        ``autoload.js`` or ``static`` (or that contain a ``static/`` segment)
        are reserved for Bokeh/Panel endpoints and cannot be reloaded directly.
        Pass routed sections to :meth:`~panel_flowdash.app.FlowDashApp.build_routes`
        for direct loads and reloads; routed sections added later via
        ``add_section()`` work in-session only.
    view
        Optional callable ``view(ctx: SidebarContext, path: str) -> Viewable``
        (sync or async) rendering ``/<route>`` (``path == ""``) and
        ``/<route>/<subpath>`` (``path == "<subpath>"``). Must be paired with
        ``route``.
    """

    id: str
    label: str
    icon: str
    items: Callable[[SidebarContext], Sequence[SidebarItem]] | None = None
    order: float = 100
    new_action: SidebarAction | None = None
    visible: Callable[[SidebarContext], bool] | None = None
    path: str | Callable[[SidebarContext], str | None] | None = None
    route: str | None = None
    view: Callable[[SidebarContext, str], t.Any] | None = None

    def __post_init__(self):  # noqa: D105
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("SidebarSection.id must be a non-empty string.")
        if not _SECTION_ID_RE.fullmatch(self.id):
            raise ValueError(
                f"SidebarSection.id {self.id!r} must start with an ASCII letter or digit and "
                "contain only ASCII letters, digits, '-' or '_'."
            )
        if self.id in RESERVED_SECTION_IDS:
            reserved = ", ".join(sorted(RESERVED_SECTION_IDS))
            raise ValueError(
                f"SidebarSection.id {self.id!r} is reserved for built-in navigation ({reserved})."
            )
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("SidebarSection.label must be a non-empty string.")
        if not isinstance(self.icon, str) or not self.icon.strip():
            raise ValueError("SidebarSection.icon must be a non-empty string.")
        if self.items is not None and not callable(self.items):
            raise TypeError(
                f"SidebarSection.items must be callable or None, got {type(self.items).__name__}."
            )
        if self.new_action is not None and not isinstance(self.new_action, SidebarAction):
            raise TypeError(
                "SidebarSection.new_action must be a SidebarAction or None, "
                f"got {type(self.new_action).__name__}."
            )
        if self.visible is not None and not callable(self.visible):
            raise TypeError(
                "SidebarSection.visible must be callable or None, "
                f"got {type(self.visible).__name__}."
            )
        if self.path is not None and not isinstance(self.path, str) and not callable(self.path):
            raise TypeError(
                "SidebarSection.path must be a string, a callable or None, "
                f"got {type(self.path).__name__}."
            )
        if isinstance(self.path, str) and (self.items is not None or self.new_action is not None):
            raise ValueError(
                "SidebarSection with a static 'path' cannot also set 'items' or 'new_action'; "
                "use 'route' and 'view' to serve a section landing page alongside items, or "
                "pass a callable 'path(ctx)' returning a route or None to switch modes dynamically."
            )
        if (self.route is None) != (self.view is None):
            raise ValueError("SidebarSection.route and SidebarSection.view must be set together.")
        if self.route is not None:
            if not isinstance(self.route, str):
                raise TypeError(
                    f"SidebarSection.route must be a string or None, got {type(self.route).__name__}."
                )
            normalized = self.route.strip("/")
            if (
                not normalized
                or any(ch.isspace() for ch in normalized)
                or "?" in normalized
                or "#" in normalized
            ):
                raise ValueError(
                    "SidebarSection.route must be a clean URL prefix without spaces or query, "
                    f"got {self.route!r}."
                )
            top_segment = normalized.split("/", 1)[0]
            if top_segment in RESERVED_ROUTE_PREFIXES:
                reserved = ", ".join(sorted(RESERVED_ROUTE_PREFIXES))
                raise ValueError(
                    f"SidebarSection.route {self.route!r} collides with a reserved route prefix "
                    f"({reserved})."
                )
            object.__setattr__(self, "route", normalized)
        if self.view is not None and not callable(self.view):
            raise TypeError(
                f"SidebarSection.view must be callable or None, got {type(self.view).__name__}."
            )
