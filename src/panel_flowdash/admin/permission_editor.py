"""Reusable admin editor for per-resource :class:`~panel_flowdash.auth.Permission` rules.

Provides :class:`PermissionEditor` (embeddable inline in a drawer or form, or
standalone with Clear/Cancel/Save actions), :class:`PermissionDialog` (modal
dialog wrapper), and principal parsing/discovery helpers shared by MCP settings
and host apps such as Lumen.
"""

from __future__ import annotations

import contextlib
import re
import typing as t
from collections.abc import Callable, Iterable, Sequence

import panel as pn
import panel_material_ui as pmui
import param
from panel.util import edit_readonly
from panel.viewable import Viewer

from panel_flowdash.admin.kit import caption, cell
from panel_flowdash.auth import Permission
from panel_flowdash.project import Problem

_SPLIT = re.compile(r"[,\n]+")
_UNSET: t.Any = object()


def parse_name_list(text: str) -> list[str]:
    """Split comma- or newline-separated user or group names, stripping blanks and duplicates."""
    seen: dict[str, None] = {}
    for part in _SPLIT.split(text or ""):
        name = part.strip()
        if name:
            seen[name] = None
    return list(seen)


def format_name_list(names: Iterable[str]) -> str:
    """Format a set or sequence of user or group names as comma-separated text."""
    return ", ".join(sorted(names))


def known_principals(app: t.Any = None, project: t.Any = None) -> tuple[list[str], list[str]]:
    """Discover known user and group names from *app* and *project* for helper hints."""
    users: set[str] = set()
    groups: set[str] = set()
    if app is not None:
        with contextlib.suppress(Exception):
            if callable(getattr(app, "_known_users", None)):
                users |= {str(u) for u in app._known_users() if u}
        with contextlib.suppress(Exception):
            if callable(getattr(app, "_known_groups", None)):
                groups |= {str(g) for g in app._known_groups() if g}
    if project is not None:
        perms = (getattr(project, "data", None) or {}).get("permissions")
        if isinstance(perms, dict):
            admin_groups = perms.get("admin_groups")
            if isinstance(admin_groups, list):
                groups |= {str(g) for g in admin_groups if isinstance(g, str) and g.strip()}
            people = perms.get("people")
            if isinstance(people, dict):
                for user, info in people.items():
                    if isinstance(user, str) and user.strip():
                        users.add(user.strip())
                    if isinstance(info, dict) and isinstance(info.get("groups"), list):
                        groups |= {
                            str(g) for g in info["groups"] if isinstance(g, str) and g.strip()
                        }
    return sorted(users), sorted(groups)


def _set_text(widget: pmui.TextInput, text: str) -> None:
    with edit_readonly(widget):
        widget.param.update(value=text, value_input=text)


class PermissionEditor(Viewer):
    """Admin editor for a :class:`~panel_flowdash.auth.Permission`'s allow/deny fields.

    Can be used standalone (``show_actions=True``, ``framed=True`` or in a
    dialog) or embedded inside another form or drawer (``show_actions=False``,
    ``framed=False``).

    Parameters
    ----------
    permission
        Initial :class:`~panel_flowdash.auth.Permission` to populate.
    title
        Heading text shown when *show_title* is True.
    caption_text
        Secondary helper text shown above the fields.
    save_label
        Label for the primary save button when *show_actions* is True.
    framed
        Whether to wrap the editor in an outlined ``Paper`` container.
    show_actions
        Whether to render the Clear / Cancel / Save button row.
    show_title
        Whether to render the title above the caption; defaults to *framed*.
    disabled
        When True, disables all inputs and save/clear actions.
    app, project
        Optional app and project used by :func:`known_principals` to populate
        helper hints under the allow fields.
    effective_display
        Optional hook ``(Permission) -> str | Viewable | None`` returning
        escaped HTML or a Panel component summarizing effective access (such as
        inherited rules). Refreshed live on every edit.
    lockout_check
        Optional hook ``(Permission) -> str | None`` returning a warning
        message if saving the permission would lock out the saving admin.
        Refreshed live on every edit.
    on_save
        Optional callback ``(Permission) -> Sequence[Problem | str] | None``
        called by :meth:`save`. Returned problems are shown in the error alert.
    on_cancel
        Optional callback invoked by :meth:`cancel`.
    on_change
        Optional callback ``(Permission) -> Any`` invoked when any input changes.
    effective_stylesheets
        Extra CSS stylesheets attached to the HTML cell used when
        *effective_display* returns an HTML string.
    css_classes
        Additional CSS classes added to the outer layout element.
    """

    disabled = param.Boolean(default=False)

    def __init__(
        self,
        permission: Permission | None = None,
        *,
        title: str = "Edit access rules",
        caption_text: str = (
            "Comma-separated user or group names. Leave all fields empty to clear this rule."
        ),
        save_label: str = "Save access",
        framed: bool = True,
        show_actions: bool = True,
        show_title: bool | None = None,
        disabled: bool = False,
        app: t.Any = None,
        project: t.Any = None,
        effective_display: Callable[[Permission], t.Any] | None = None,
        lockout_check: Callable[[Permission], str | None] | None = None,
        on_save: Callable[[Permission], Sequence[Problem | str] | None] | None = None,
        on_cancel: Callable[[], t.Any] | None = None,
        on_change: Callable[[Permission], t.Any] | None = None,
        effective_stylesheets: Sequence[str] = (),
        css_classes: Sequence[str] = (),
        **params,
    ):
        super().__init__(disabled=disabled, **params)
        self._app = app
        self._project = project
        self._effective_display = effective_display
        self._lockout_check = lockout_check
        self._on_save = on_save
        self._on_cancel = on_cancel
        self._on_change = on_change
        self._effective_stylesheets = tuple(effective_stylesheets)
        self._loading = False
        self._texts: dict[str, str] = {
            "allow_users": "",
            "allow_groups": "",
            "deny_users": "",
            "deny_groups": "",
        }

        self._title = pmui.Typography(title, variant="subtitle2", margin=0)
        self._caption = caption(caption_text, margin=(2, 0, 4, 0)) if caption_text else None
        self._effective_cell = cell(
            "",
            stylesheets=list(self._effective_stylesheets),
            css_classes=["flowdash-permission-effective"],
        )
        self._effective_box = pn.Column(
            self._effective_cell,
            margin=0,
            sizing_mode="stretch_width",
            visible=False,
        )
        self._lockout_warn = pmui.Alert(
            object="",
            severity="warning",
            visible=False,
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["flowdash-permission-lockout-warning"],
        )
        self._alert = pmui.Alert(
            object="",
            severity="error",
            visible=False,
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["flowdash-permission-error"],
        )

        self._allow_users = self._make_input("allow_users", "Allow users", "e.g. alice, bob")
        self._allow_groups = self._make_input("allow_groups", "Allow groups", "e.g. hr, finance")
        self._deny_users = self._make_input("deny_users", "Deny users", "e.g. contractor")
        self._deny_groups = self._make_input("deny_groups", "Deny groups", "e.g. guests")

        self._clear_btn = pmui.Button(
            label="Clear rule",
            variant="text",
            color="default",
            size="small",
            margin=0,
            css_classes=["flowdash-permission-clear"],
        )
        self._clear_btn.on_click(lambda _event: self.clear())
        self._cancel_btn = pmui.Button(
            label="Cancel",
            variant="outlined",
            color="default",
            size="small",
            margin=0,
            css_classes=["flowdash-permission-cancel"],
        )
        self._cancel_btn.on_click(lambda _event: self.cancel())
        self._save_btn = pmui.Button(
            label=save_label,
            icon="save",
            variant="contained",
            color="primary",
            size="small",
            margin=0,
            css_classes=["flowdash-permission-save"],
        )
        self._save_btn.on_click(lambda _event: self.save())

        render_title = framed if show_title is None else show_title
        items: list[t.Any] = []
        if render_title:
            items.append(self._title)
        if self._caption is not None:
            items.append(self._caption)
        items.extend(
            [
                self._effective_box,
                self._lockout_warn,
                self._alert,
                pn.Row(
                    self._allow_users,
                    self._allow_groups,
                    margin=(4, 0, 0, 0),
                    sizing_mode="stretch_width",
                    styles={"gap": "12px", "flex-wrap": "wrap"},
                ),
                pn.Row(
                    self._deny_users,
                    self._deny_groups,
                    margin=(4, 0, 0, 0),
                    sizing_mode="stretch_width",
                    styles={"gap": "12px", "flex-wrap": "wrap"},
                ),
            ]
        )
        if show_actions:
            items.append(
                pn.Row(
                    self._clear_btn,
                    pn.layout.HSpacer(),
                    self._cancel_btn,
                    self._save_btn,
                    margin=(10, 0, 0, 0),
                    sizing_mode="stretch_width",
                    styles={"gap": "8px", "align-items": "center"},
                )
            )
        inner = pn.Column(
            *items,
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": "6px"},
        )
        classes = ["flowdash-permission-editor", *css_classes]
        if framed:
            self._layout: t.Any = pmui.Paper(
                inner,
                variant="outlined",
                margin=0,
                sizing_mode="stretch_width",
                sx={"p": "14px 16px", "borderRadius": "8px"},
                css_classes=classes,
            )
        else:
            inner.css_classes = classes
            self._layout = inner

        self._sync_disabled()
        self.load(permission)

    @property
    def allow_users_input(self) -> pmui.TextInput:
        """Text input for ``allow_users``."""
        return self._allow_users

    @property
    def allow_groups_input(self) -> pmui.TextInput:
        """Text input for ``allow_groups``."""
        return self._allow_groups

    @property
    def deny_users_input(self) -> pmui.TextInput:
        """Text input for ``deny_users``."""
        return self._deny_users

    @property
    def deny_groups_input(self) -> pmui.TextInput:
        """Text input for ``deny_groups``."""
        return self._deny_groups

    def _make_input(self, key: str, label: str, placeholder: str) -> pmui.TextInput:
        css = key.replace("_", "-")
        widget = pmui.TextInput(
            label=label,
            value="",
            placeholder=placeholder,
            size="small",
            sizing_mode="stretch_width",
            margin=0,
            styles={"flex": "1 1 180px", "min-width": "160px"},
            css_classes=[f"flowdash-permission-{css}"],
        )

        def on_edit(event):
            if self._loading:
                return
            self._texts[key] = event.new or ""
            self.refresh_preview()
            if self._on_change is not None:
                self._on_change(self.permission())

        widget.param.watch(on_edit, "value")
        widget.param.watch(on_edit, "value_input")
        return widget

    @param.depends("disabled", watch=True)
    def _sync_disabled(self) -> None:
        locked = bool(self.disabled)
        for widget in (
            self._allow_users,
            self._allow_groups,
            self._deny_users,
            self._deny_groups,
            self._clear_btn,
            self._save_btn,
        ):
            widget.disabled = locked

    def load(
        self,
        permission: Permission | None = None,
        *,
        title: str | None = None,
        app: t.Any = None,
        project: t.Any = None,
        disabled: bool | None = None,
        effective_display: Callable[[Permission], t.Any] | None = _UNSET,
        lockout_check: Callable[[Permission], str | None] | None = _UNSET,
    ) -> None:
        """Populate the editor from *permission* and refresh hints and live previews."""
        if app is not None:
            self._app = app
        if project is not None:
            self._project = project
        if title is not None:
            self._title.object = title
        if disabled is not None:
            self.disabled = disabled
        if effective_display is not _UNSET:
            self._effective_display = effective_display
        if lockout_check is not _UNSET:
            self._lockout_check = lockout_check

        perm = permission or Permission()
        values = {
            "allow_users": format_name_list(perm.allow_users),
            "allow_groups": format_name_list(perm.allow_groups),
            "deny_users": format_name_list(perm.deny_users),
            "deny_groups": format_name_list(perm.deny_groups),
        }
        users, groups = known_principals(self._app, self._project)
        user_hint = (
            f"Known: {', '.join(users[:6])}" + (f" +{len(users) - 6}" if len(users) > 6 else "")
            if users
            else ""
        )
        group_hint = (
            f"Known: {', '.join(groups[:6])}" + (f" +{len(groups) - 6}" if len(groups) > 6 else "")
            if groups
            else ""
        )
        self._loading = True
        try:
            with pn.io.hold():
                self._texts.update(values)
                _set_text(self._allow_users, values["allow_users"])
                _set_text(self._allow_groups, values["allow_groups"])
                _set_text(self._deny_users, values["deny_users"])
                _set_text(self._deny_groups, values["deny_groups"])
                self._allow_users.helper_text = user_hint
                self._allow_groups.helper_text = group_hint
                self._deny_users.helper_text = "Denied even for admins"
                self._deny_groups.helper_text = "Denied even for admins"
                self._alert.visible = False
        finally:
            self._loading = False
        self.refresh_preview()

    def permission(self) -> Permission:
        """Return the :class:`~panel_flowdash.auth.Permission` represented by the inputs."""
        return Permission.from_spec(
            allow_users=parse_name_list(self._texts["allow_users"]),
            allow_groups=parse_name_list(self._texts["allow_groups"]),
            deny_users=parse_name_list(self._texts["deny_users"]),
            deny_groups=parse_name_list(self._texts["deny_groups"]),
        )

    def refresh_preview(self) -> None:
        """Re-evaluate *effective_display* and *lockout_check* for the current inputs."""
        perm = self.permission()
        if self._effective_display is not None:
            rendered = self._effective_display(perm)
            if rendered is None or rendered == "":
                self._effective_box.visible = False
            elif isinstance(rendered, str):
                self._effective_cell.object = rendered
                if list(self._effective_box) != [self._effective_cell]:
                    self._effective_box[:] = [self._effective_cell]
                self._effective_box.visible = True
            else:
                self._effective_box[:] = [rendered]
                self._effective_box.visible = True
        else:
            self._effective_box.visible = False

        if self._lockout_check is not None:
            reason = self._lockout_check(perm)
            self._lockout_warn.param.update(object=reason or "", visible=bool(reason))
        else:
            self._lockout_warn.param.update(object="", visible=False)

    def clear(self) -> None:
        """Clear all four fields."""
        if self.disabled:
            return
        self._loading = True
        try:
            with pn.io.hold():
                for key, widget in (
                    ("allow_users", self._allow_users),
                    ("allow_groups", self._allow_groups),
                    ("deny_users", self._deny_users),
                    ("deny_groups", self._deny_groups),
                ):
                    self._texts[key] = ""
                    _set_text(widget, "")
                self._alert.visible = False
        finally:
            self._loading = False
        self.refresh_preview()
        if self._on_change is not None:
            self._on_change(self.permission())

    def show_problems(self, problems: Sequence[Problem | str]) -> None:
        """Display validation or save *problems* in the editor's error alert."""
        messages = [p.message if isinstance(p, Problem) else str(p) for p in problems]
        self._alert.param.update(object="\n\n".join(messages), visible=bool(messages))

    def save(self) -> bool:
        """Invoke *on_save* with the current permission; show any returned problems."""
        if self._on_save is None or self.disabled:
            return False
        problems = list(self._on_save(self.permission()) or ())
        self.show_problems(problems)
        return not problems

    def cancel(self) -> None:
        """Clear problems and invoke *on_cancel*."""
        self._alert.visible = False
        if self._on_cancel is not None:
            self._on_cancel()

    def __panel__(self):
        """Render the permission editor."""
        return self._layout


class PermissionDialog(Viewer):
    """Modal dialog hosting a :class:`PermissionEditor`."""

    def __init__(
        self,
        *,
        title: str = "Access rules",
        save_label: str = "Save access",
        app: t.Any = None,
        project: t.Any = None,
        effective_display: Callable[[Permission], t.Any] | None = None,
        lockout_check: Callable[[Permission], str | None] | None = None,
        effective_stylesheets: Sequence[str] = (),
        css_classes: Sequence[str] = (),
        **params,
    ):
        super().__init__(**params)
        self._handler: Callable[[Permission], Sequence[Problem | str] | None] | None = None
        self.editor = PermissionEditor(
            framed=False,
            show_title=False,
            show_actions=True,
            save_label=save_label,
            app=app,
            project=project,
            effective_display=effective_display,
            lockout_check=lockout_check,
            effective_stylesheets=effective_stylesheets,
            on_save=self._handle_save,
            on_cancel=self.close,
        )
        # Put dialog classes on the content rendered inside MUI's portal.
        self.editor._layout.css_classes = [
            "flowdash-permission-dialog",
            "flowdash-permission-editor",
            *css_classes,
        ]
        self._dialog = pmui.Dialog(
            self.editor,
            title=title,
            open=False,
            show_close_button=True,
            width_option="sm",
        )

    @property
    def is_open(self) -> bool:
        """Whether the dialog is currently open."""
        return bool(self._dialog.open)

    def open(
        self,
        title: str,
        permission: Permission | None,
        on_save: Callable[[Permission], Sequence[Problem | str] | None],
        *,
        app: t.Any = None,
        project: t.Any = None,
        effective_display: Callable[[Permission], t.Any] | None = _UNSET,
        lockout_check: Callable[[Permission], str | None] | None = _UNSET,
    ) -> None:
        """Populate the dialog's editor and open it."""
        self._handler = on_save
        self.editor.load(
            permission,
            app=app,
            project=project,
            effective_display=effective_display,
            lockout_check=lockout_check,
        )
        self._dialog.param.update(title=title, open=True)

    def close(self) -> None:
        """Close the dialog."""
        self._dialog.open = False

    def _handle_save(self, perm: Permission) -> Sequence[Problem | str]:
        if self._handler is None:
            return []
        problems = list(self._handler(perm) or ())
        if not problems:
            self.close()
        return problems

    def __panel__(self):
        """Render the modal dialog."""
        return self._dialog


__all__ = [
    "PermissionDialog",
    "PermissionEditor",
    "format_name_list",
    "known_principals",
    "parse_name_list",
]
