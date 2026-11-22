"""The admin page: a list of the project file's sections and the selected section's settings."""

from __future__ import annotations

import logging
import typing as t
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin import (
    ai as _ai,  # noqa: F401  (registers the ai section)
    permissions as _permissions,  # noqa: F401
)
from panel_flowdash.admin.form import SettingsForm, using_app
from panel_flowdash.admin.sections import BUILTIN_SECTIONS
from panel_flowdash.project import Project, ProjectSection, sections
from panel_flowdash.ui.text import alert_pane, escape_markdown
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger(__name__)

ADMIN_ROUTE = "/admin"

# Below this width the section list gives way to a select above the section.
NARROW_WIDTH = 1100

_HIDE_WHEN_NARROW = (
    f"@media (max-width: {NARROW_WIDTH - 0.05}px) {{ :host {{ display: none !important; }} }}"
)
_HIDE_WHEN_WIDE = (
    f"@media (min-width: {NARROW_WIDTH}px) {{ :host {{ display: none !important; }} }}"
)

_NAV_SX = {
    "py": 0,
    "& .MuiListItemButton-root": {
        "pl": "21px",
        "pr": 2,
        "py": 0.75,
        "borderLeft": "3px solid transparent",
    },
    "& .MuiListItemIcon-root": {"minWidth": "34px"},
    "& .MuiListItemText-primary": {"fontSize": "0.9375rem", "letterSpacing": "0.15px"},
    "& .MuiListItemButton-root.Mui-selected": {
        "borderLeftColor": "primary.main",
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.12)",
    },
    "& .MuiListItemButton-root.Mui-selected .MuiListItemText-primary": {"fontWeight": 500},
    "& .MuiListItemButton-root.Mui-selected:hover": {
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.16)",
    },
}

_GROUP_SX = {"color": "text.secondary", "fontSize": "0.8125rem", "letterSpacing": "0.4px"}

_STATUS_CSS = """
:host { align-self: flex-start; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 0.8125rem;
  letter-spacing: 0.25px; color: var(--mui-palette-text-secondary); white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--mui-palette-warning-main, #ed6c02); }
.status svg { width: 16px; height: 16px; fill: var(--mui-palette-success-main, #2e7d32); }
"""

_SAVED_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 '
    '10 10 10-4.48 10-10S17.52 2 12 2m-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8z"/></svg>'
)


def admin_sections() -> list[ProjectSection]:
    """Return the sections with a settings view: the built-in ones first, then by group and title."""
    registered = [s for s in sections().values() if s.settings_view is not None]
    builtin = [s for name in BUILTIN_SECTIONS for s in registered if s.name == name]
    others = sorted(
        (s for s in registered if s.name not in BUILTIN_SECTIONS),
        key=lambda s: (s.group == "Extensions", s.group, s.label.lower()),
    )
    return [*builtin, *others]


class AdminPage(Viewer):
    """The admin view of a FlowDash app.

    It lists every registered project file section that has a
    ``settings_view`` and shows the selected one. A section whose view is a
    `SettingsForm` gets the page's Save and Discard and the unsaved-changes
    dot; any other view is shown as it is and saves on its own.
    """

    dirty = param.Boolean(default=False, doc="Whether a section has unsaved changes.")

    section = param.String(default=None, allow_None=True, doc="The section shown.")

    def __init__(
        self,
        app: FlowDashApp,
        project: Project,
        *,
        on_select: t.Callable[[str], None] | None = None,
        on_saved: t.Callable[[str], None] | None = None,
        **params,
    ):
        super().__init__(**params)
        self._app = app
        self.project = project
        self._on_select = on_select
        self._on_saved = on_saved
        self._sections = {s.name: s for s in admin_sections()}
        self._views: dict[str, t.Any] = {}
        self._saved: set[str] = set()

        self._title = pmui.Typography(
            "",
            variant="h5",
            margin=0,
            sx={"fontWeight": 400, "fontSize": "1.625rem", "lineHeight": 1.3, "letterSpacing": 0},
        )
        self._description = pmui.Typography(
            "",
            variant="body2",
            margin=(6, 0, 0, 0),
            sx={"color": "text.secondary", "maxWidth": "720px", "letterSpacing": "0.25px"},
        )
        self._status = pn.pane.HTML(
            "", margin=(10, 8, 0, 0), stylesheets=[_STATUS_CSS], styles={"flex": "0 0 auto"}
        )
        self._discard_button = pmui.Button(
            label="Discard",
            variant="outlined",
            color="default",
            margin=0,
            disabled=True,
            styles={"flex": "0 0 auto"},
        )
        self._save_button = pmui.Button(
            label="Save",
            icon="save",
            variant="contained",
            margin=0,
            disabled=True,
            styles={"flex": "0 0 auto"},
        )
        self._discard_button.on_click(lambda _event: self.discard())
        self._save_button.on_click(lambda _event: self.save())
        self._narrow_select = pmui.Select(
            options={s.label: s.name for s in self._sections.values()},
            label="Section",
            width=240,
            margin=(0, 0, 16, 0),
            stylesheets=[_HIDE_WHEN_WIDE],
        )
        self._narrow_select.param.watch(self._on_narrow_select, "value")
        header = pn.Row(
            pn.Column(
                self._title,
                self._description,
                margin=0,
                sizing_mode="stretch_width",
                styles={"min-width": "0"},
            ),
            self._status,
            self._discard_button,
            self._save_button,
            sizing_mode="stretch_width",
            margin=(0, 0, 8, 0),
            styles={"align-items": "flex-start", "gap": "8px"},
        )
        self._body = pn.Column(sizing_mode="stretch_width", margin=0)
        self._content = pn.Column(
            self._narrow_select,
            header,
            self._body,
            sizing_mode="stretch_width",
            margin=0,
            styles={"padding": "24px 32px 48px 32px", "min-width": "0", "flex": "1 1 auto"},
            css_classes=["flowdash-admin-content"],
        )
        self._menus: dict[str, pmui.MenuList] = {}
        self._nav = self._build_nav()
        self._layout = pn.Row(
            self._nav,
            self._content,
            sizing_mode="stretch_both",
            margin=0,
            styles={"min-height": "100%", "align-items": "stretch"},
            css_classes=["flowdash-admin"],
        )
        first = next(iter(self._sections), None)
        if first is not None:
            self.show(first)
        else:
            self._title.object = "Admin"
            self._body[:] = [
                alert_pane("No project file section has admin settings.", alert_type="info")
            ]

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _signed_in(self) -> str:
        identity = self._app._identity
        groups = sorted(identity.groups & self._app._admin_groups())
        if identity.oauth_user is None:
            return f"{identity.user} · no sign-in"
        if groups:
            return f"Signed in as {identity.user} · {', '.join(groups)}"
        return f"Signed in as {identity.user}"

    def _build_nav(self):
        groups: dict[str, list[ProjectSection]] = {}
        for section in self._sections.values():
            groups.setdefault(section.group or "Extensions", []).append(section)
        order = {"App": 0, "Extensions": 2}
        groups = dict(sorted(groups.items(), key=lambda item: (order.get(item[0], 1), item[0])))
        items: list = [
            pmui.Typography("Admin", variant="h6", margin=(20, 24, 0, 24), sx={"fontWeight": 400}),
            pmui.Typography(
                self._signed_in(),
                variant="caption",
                margin=(2, 24, 16, 24),
                sx={"color": "text.secondary", "letterSpacing": "0.4px"},
            ),
        ]
        for group, members in groups.items():
            items.append(
                pmui.Typography(group, variant="body2", margin=(8, 24, 4, 24), sx=_GROUP_SX)
            )
            menu = pmui.MenuList(
                items=[
                    {"label": s.label, "icon": s.icon or "settings", "name": s.name}
                    for s in members
                ],
                dense=False,
                margin=0,
                sizing_mode="stretch_width",
                sx=_NAV_SX,
                css_classes=[f"flowdash-admin-nav-{group.lower()}"],
            )
            menu.param.watch(self._on_menu, "active")
            self._menus[group] = menu
            items.append(menu)
        items.append(pn.layout.VSpacer())
        path = Path(self.project.path).name if self.project.path else None
        where = f"Saved in {path}. " if path else "Not saved to a file. "
        items.append(
            pmui.Typography(
                f"Settings saved here apply to everyone. {where}"
                "Values set in code are shown locked.",
                variant="caption",
                margin=0,
                sx={
                    "color": "text.secondary",
                    "letterSpacing": "0.25px",
                    "borderTop": 1,
                    "borderColor": "divider",
                    "display": "block",
                    "px": 3,
                    "py": 2,
                },
                sizing_mode="stretch_width",
            )
        )
        return pn.Column(
            *items,
            width=240,
            margin=0,
            sizing_mode="stretch_height",
            styles={
                "flex": "0 0 240px",
                "border-right": "1px solid var(--mui-palette-divider)",
                "min-height": "calc(100vh - 56px)",
            },
            stylesheets=[_HIDE_WHEN_NARROW],
            css_classes=["flowdash-admin-nav"],
        )

    def _on_menu(self, event):
        if not event.new:
            return
        menu = event.obj
        item = menu.items[event.new[0]]
        name = item.get("name")
        if name and name != self.section:
            self.select(name)

    def _on_narrow_select(self, event):
        if event.new and event.new != self.section:
            self.select(event.new)

    def select(self, name: str):
        """Show section *name*, through the app's routing when it has one."""
        if self._on_select is not None:
            self._on_select(name)
        else:
            self.show(name)

    @property
    def sections(self) -> dict[str, ProjectSection]:
        """Return the sections listed, by name."""
        return dict(self._sections)

    # ------------------------------------------------------------------
    # The section shown
    # ------------------------------------------------------------------

    def view(self, name: str):
        """Return the settings view of section *name*, building it the first time."""
        if name not in self._views:
            section = self._sections[name]
            try:
                with using_app(self._app):
                    view = section.settings_view(self.project, True)
            except Exception as exc:
                logger.exception("Could not build the settings of section %r", name)
                view = alert_pane(
                    f"The settings of **{escape_markdown(section.label)}** could not be shown: "
                    f"{escape_markdown(error_text(exc))}",
                    alert_type="danger",
                )
            if isinstance(view, SettingsForm):
                view.param.watch(lambda _event, name=name: self._on_dirty(name), "dirty")
            self._views[name] = view
        return self._views[name]

    def show(self, name: str | None):
        """Show section *name*, or the first one when it is not listed."""
        if name not in self._sections:
            name = next(iter(self._sections), None)
            if name is None:
                return
        section = self._sections[name]
        view = self.view(name)
        with pn.io.hold():
            self.section = name
            self._title.object = section.label
            self._description.object = section.description
            self._description.visible = bool(section.description)
            self._body[:] = [view]
            for menu in self._menus.values():
                index = next(
                    (i for i, item in enumerate(menu.items) if item.get("name") == name), None
                )
                active = None if index is None else (index,)
                if menu.active != active:
                    menu.active = active
            if self._narrow_select.value != name:
                self._narrow_select.value = name
            self._sync_actions()

    def _form(self) -> SettingsForm | None:
        view = self._views.get(self.section) if self.section else None
        return view if isinstance(view, SettingsForm) else None

    def _on_dirty(self, name: str):
        if self._views.get(name) is not None and self._views[name].dirty:
            self._saved.discard(name)
        self._sync_actions()

    def _sync_actions(self):
        form = self._form()
        dirty = form is not None and form.dirty
        for widget in (self._discard_button, self._save_button):
            widget.visible = form is not None
            widget.disabled = not dirty
        if dirty:
            status = '<span class="status"><span class="dot"></span>Unsaved changes</span>'
        elif form is not None and self.section in self._saved:
            status = f'<span class="status">{_SAVED_ICON}All changes saved</span>'
        else:
            status = ""
        self._status.object = status
        self.dirty = bool(self.dirty_sections())

    def dirty_sections(self) -> list[str]:
        """Return the titles of the sections with unsaved changes."""
        return [
            self._sections[name].label
            for name, view in self._views.items()
            if isinstance(view, SettingsForm) and view.dirty
        ]

    # ------------------------------------------------------------------
    # Save and Discard
    # ------------------------------------------------------------------

    def save(self, name: str | None = None) -> bool:
        """Save section *name* (the one shown by default); False if it was rejected."""
        name = name or self.section
        view = self._views.get(name) if name else None
        if not isinstance(view, SettingsForm):
            return True
        if not view.save():
            self._sync_actions()
            return False
        self._saved.add(name)
        self._sync_actions()
        if self._on_saved is not None:
            self._on_saved(name)
        return True

    def save_all(self) -> bool:
        """Save every section with unsaved changes; False if one was rejected."""
        names = [
            name
            for name, view in self._views.items()
            if isinstance(view, SettingsForm) and view.dirty
        ]
        return all([self.save(name) for name in names])

    def discard(self, name: str | None = None):
        """Put section *name* (the one shown by default) back to what is saved."""
        name = name or self.section
        view = self._views.get(name) if name else None
        if isinstance(view, SettingsForm):
            view.reset()
        self._sync_actions()

    def discard_all(self):
        """Put every section back to what is saved."""
        for view in self._views.values():
            if isinstance(view, SettingsForm) and view.dirty:
                view.reset()
        self._sync_actions()

    def __panel__(self):
        """Render the page."""
        return self._layout
