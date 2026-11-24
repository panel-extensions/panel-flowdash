"""Public specification for navigation sections in the FlowDash sidebar rail and drawer.

An application or extension adds a navigation section (such as Reports or a
Data catalog) by passing :class:`SidebarSection` objects to
:class:`~panel_flowdash.app.FlowDashApp` via its ``sections`` parameter or by
calling :meth:`~panel_flowdash.app.FlowDashApp.add_section`.

The built-in Dashboards navigation uses the same :class:`SidebarSection`
mechanism (with ``id="dashboards"`` and ``order=0``).
"""

from __future__ import annotations

import typing as t
from collections.abc import Callable, Sequence
from dataclasses import dataclass

if t.TYPE_CHECKING:
    from panel_flowdash.auth import Identity

__all__ = [
    "SidebarAction",
    "SidebarItem",
    "SidebarSection",
]


@dataclass(frozen=True)
class SidebarItem:
    """One destination listed inside a :class:`SidebarSection`.

    Attributes
    ----------
    title
        Display label shown in the rail menu and narrow-screen drawer.
    url
        App route (e.g. ``"/reports/q4"``) opened through the app's router, or
        an external ``http://``/``https://`` URL opened in a new tab.
    subtitle
        Optional secondary text such as the owner or last updated time, shown
        under the item title in the narrow-screen drawer.
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
        Either a zero-argument callable invoked when clicked, or a route/URL
        string to open through the app's router (or in a new tab if external).
    icon
        Material icon name shown beside the label. Defaults to ``"add"``.
    """

    label: str
    action: Callable[[], t.Any] | str
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

    Attributes
    ----------
    id
        Unique section identifier (e.g. ``"dashboards"``, ``"reports"``).
        Registering a section with an existing ``id`` replaces the earlier one.
    label
        Heading displayed in the rail button and the narrow-screen drawer.
    icon
        Material icon name for the section (e.g. ``"space_dashboard"``).
    items
        Optional callable ``items(user) -> Sequence[SidebarItem]`` returning
        the destinations visible to the current :class:`~panel_flowdash.Identity`.
    order
        Sort key controlling section order (ascending). Ties preserve
        registration order. The built-in Dashboards section uses ``0``; custom
        sections default to ``100`` so they appear after Dashboards.
    new_action
        Optional :class:`SidebarAction` (such as "+ New") placed at the end of
        the section's items.
    visible
        Optional predicate ``visible(user) -> bool`` deciding whether the
        section appears for the current :class:`~panel_flowdash.Identity`.
    path
        Optional landing route (or zero-argument callable returning a route or
        ``None``). When resolved to a non-empty string, clicking the section
        navigates directly to that route instead of opening an item menu.
    """

    id: str
    label: str
    icon: str
    items: Callable[[Identity], Sequence[SidebarItem]] | None = None
    order: float = 100
    new_action: SidebarAction | None = None
    visible: Callable[[Identity], bool] | None = None
    path: str | Callable[[], str | None] | None = None

    def __post_init__(self):  # noqa: D105
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("SidebarSection.id must be a non-empty string.")
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
