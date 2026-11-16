"""The dashboard settings dialog: general settings, refresh schedule, sharing and delete."""

from __future__ import annotations

import math
import time
import typing as t
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash.auth import Permission

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

SECTIONS = ("general", "refresh", "sharing")

SECTION_LABELS = {"general": "General", "refresh": "Refresh", "sharing": "Sharing"}

SECTION_ICONS = {"general": "info", "refresh": "autorenew", "sharing": "group"}

# Settings with a dedicated control; any other parameter of the settings class
# is rendered generically in the General section.
DEDICATED_SETTINGS = ("description", "refresh_period", "viewers_can_inspect")

# Shorter labels for the generic form, which shows them as switches.
TILE_SETTINGS = {"tile_cards": "Frame tiles", "tile_titles": "Show tile titles"}

REFRESH_PRESETS = {"Off": 0, "1 min": 60, "5 min": 300, "15 min": 900, "1 hour": 3600}

CUSTOM = "Custom"

ROLE_LABELS = {"view": "Can view", "edit": "Can edit", "blocked": "Blocked"}

REMOVE = "remove"

Role = t.Literal["view", "edit", "blocked"]

Kind = t.Literal["user", "group"]


@dataclass(frozen=True)
class ShareEntry:
    """One person or group named in a dashboard's sharing rules, with its role."""

    name: str
    kind: Kind
    role: Role


@dataclass(frozen=True)
class SharingAccess:
    """What the sharing section shows for one dashboard.

    The application builds it for identities that may administer the
    dashboard; the dialog has no sharing section without one.
    """

    owner: str
    permission: Permission
    default_allow: bool = True
    group_sizes: Mapping[str, int] = field(default_factory=dict)
    groups: Sequence[str] = ()
    link: str = ""
    users: Sequence[str] = ()
    you: frozenset[str] = frozenset()


def entries_from_permission(permission: Permission, owner: str) -> tuple[list[ShareEntry], bool]:
    """Return the entries a permission names and whether everyone may view.

    A name in several lists gets the strongest role: blocked over edit over
    view, matching how access is evaluated. The owner keeps their own row, so
    rules naming the owner are not listed; an owner-only allow rule is how
    "only the people listed" is stored when nobody else may view.
    """
    roles: dict[tuple[str, Kind], Role] = {}
    for role, users, groups in (
        ("view", permission.allow_users, permission.allow_groups),
        ("edit", permission.allow_edit_users, permission.allow_edit_groups),
        ("blocked", permission.deny_users, permission.deny_groups),
    ):
        for kind, names in (("user", users), ("group", groups)):
            for name in names:
                if kind == "user" and name == owner:
                    continue
                roles[(name, kind)] = role
    entries = [
        ShareEntry(name, kind, role)
        for (name, kind), role in sorted(roles.items(), key=lambda item: item[0][0].lower())
    ]
    everyone = not (permission.allow_users or permission.allow_groups)
    return entries, everyone


def permission_from_entries(
    entries: Sequence[ShareEntry], *, everyone: bool, owner: str
) -> Permission:
    """Build the permission for *entries*, the inverse of `entries_from_permission`.

    With *everyone* on there are no view rules, so the project default lets
    everyone view and "Can view" entries add nothing. With it off and nobody
    listed as a viewer, the owner is the only view rule, which still lets
    editors view but nobody else.
    """
    lists: dict[str, set[str]] = {
        key: set()
        for key in (
            "allow_users",
            "allow_groups",
            "allow_edit_users",
            "allow_edit_groups",
            "deny_users",
            "deny_groups",
        )
    }
    prefix = {"view": "allow", "edit": "allow_edit", "blocked": "deny"}
    for entry in entries:
        if entry.role == "view" and everyone:
            continue
        lists[f"{prefix[entry.role]}_{entry.kind}s"].add(entry.name)
    if not everyone and not (lists["allow_users"] or lists["allow_groups"]):
        lists["allow_users"].add(owner)
    return Permission.from_spec(**lists)


def refresh_choice(period: float) -> str:
    """Return the preset label for a refresh *period*, or "Custom"."""
    for label, seconds in REFRESH_PRESETS.items():
        if period == seconds:
            return label
    return CUSTOM


def _initials(name: str) -> str:
    local = name.split("@")[0]
    parts = [part for part in local.replace("_", " ").replace(".", " ").split() if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return local[:2].upper()


def _seconds(value: float) -> str:
    return f"{value:.1f} s" if value < 10 else f"{value:.0f} s"


class SettingsDialog(param.Parameterized):
    """One dialog for a dashboard's settings, its refresh schedule and its sharing.

    The editor owns it and mounts `dialog`. Settings are edited on a copy and
    applied to the editor's ``settings`` on **Save settings**. An application
    supplies the sharing, rename and delete callbacks; without them the dialog
    only offers the dashboard settings.
    """

    delete = param.Callable(
        default=None,
        doc="""
        Called with the dashboard id and title to ask for deleting the
        dashboard. Without it the dialog offers no Delete.""",
    )

    rename = param.Callable(
        default=None,
        doc="""
        Called with the dashboard id and the new title. Returns None once
        renamed, or the reason it was not. Without it the title is read-only.""",
    )

    save_sharing = param.Callable(
        default=None,
        doc="""
        Called with the dashboard id and the new Permission when the sharing
        changed. Returns whether it was stored.""",
    )

    section = param.Selector(default="general", objects=list(SECTIONS), doc="The section shown.")

    sharing = param.Callable(
        default=None,
        doc="""
        Called with a dashboard id when the dialog opens. Returns the
        SharingAccess to show, or None when the current identity may not
        change the dashboard's sharing.""",
    )

    def __init__(self, editor: FlowDash, **params):
        super().__init__(**params)
        self._editor = editor
        self._draft = editor.settings_class()
        self._target_id: str | None = None
        self._target_title = ""
        self._access: SharingAccess | None = None
        self._initial_entries: list[ShareEntry] = []
        self._entries: list[ShareEntry] = []
        self._initial_everyone = True
        self._requested: tuple[str, str | None, str] | None = None
        self._refresh_began: float | None = None
        self._refresh_duration: float | None = None
        self._last_refresh: float | None = None
        # Registered before any component subscribes, so the first watcher runs
        # before every component's and the last one after all of them.
        editor.param.watch(self._on_refresh_start, "refresh")
        editor.param.watch(self._on_refresh_end, "refresh", precedence=10**6)

        self._subtitle = pmui.Typography(
            variant="caption", sx={"color": "text.secondary"}, margin=(0, 0, 8, 0)
        )
        self._nav = pmui.MenuList(items=[], dense=True, sizing_mode="stretch_width", margin=0)
        self._nav.param.watch(self._on_nav, "active")
        self._delete_button = pmui.Button(
            label="Delete",
            icon="delete",
            color="error",
            variant="text",
            sizing_mode="stretch_width",
            sx={"justifyContent": "flex-start", "pl": 2},
            margin=(4, 0),
        )
        self._delete_button.on_click(self._on_delete)
        nav = pn.Column(
            self._nav,
            self._delete_button,
            width=190,
            margin=(0, 16, 0, 0),
            styles={"flex": "0 0 auto"},
        )

        self._general = self._build_general()
        self._refresh = self._build_refresh()
        self._sharing = self._build_sharing()
        self._sections = {
            "general": self._general,
            "refresh": self._refresh,
            "sharing": self._sharing,
        }
        content = pn.Column(
            *self._sections.values(),
            sizing_mode="stretch_width",
            min_height=360,
            margin=0,
            styles={"min-width": "0", "flex": "1 1 320px"},
        )

        self._footer_note = pmui.Typography(
            "Changes to sharing apply when you save.",
            variant="caption",
            sx={"color": "text.secondary"},
            align="center",
            margin=(0, 8, 0, 0),
        )
        self._cancel_button = pmui.Button(label="Cancel", variant="text", align="center")
        self._save_button = pmui.Button(label="Save settings", color="primary", align="center")
        self._cancel_button.on_click(lambda _event: self.dialog.param.update(open=False))
        self._save_button.on_click(self._on_save)
        footer = pmui.FlexBox(
            self._footer_note,
            pn.layout.HSpacer(),
            self._cancel_button,
            self._save_button,
            align_items="center",
            flex_wrap="wrap",
            sizing_mode="stretch_width",
            margin=(12, 0, 0, 0),
            sx={"borderTop": 1, "borderColor": "divider", "pt": 1.5, "gap": 1},
        )
        body = pmui.FlexBox(
            nav,
            content,
            flex_wrap="wrap",
            sizing_mode="stretch_width",
            margin=0,
            sx={"rowGap": 2},
        )
        self.dialog = pmui.Dialog(
            objects=[self._subtitle, body, footer],
            title="Dashboard settings",
            open=False,
            show_close_button=True,
            width_option="md",
            sx={"& .MuiDialog-paper": {"minWidth": "min(760px, 100%)"}},
        )
        self.dialog.param.watch(self._on_open, "open")
        editor.param.watch(self._sync_inspect, "inspect_outputs")
        self._sync_inspect()
        self._show_section()

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _heading(self, text: str):
        return pmui.Typography(
            text, variant="subtitle1", sx={"fontWeight": 500}, margin=(0, 0, 4, 0)
        )

    def _build_general(self):
        self._title_input = pmui.TextInput(label="Name", sizing_mode="stretch_width")
        self._title_input.param.watch(self._on_title_input, "value_input")
        self._description_input = pmui.TextAreaInput.from_param(
            self._draft.param.description,
            helper_text="Shown under the title and on the dashboard card.",
            rows=2,
            auto_grow=True,
            sizing_mode="stretch_width",
        )
        self._inspect_switch = pmui.Switch.from_param(
            self._draft.param.viewers_can_inspect, margin=(10, 10, 0, 10)
        )
        self._inspect_help = pmui.Typography(
            "People who may only view the dashboard get the inspect button on its tiles. "
            "Editors always do.",
            variant="caption",
            sx={"color": "text.secondary"},
            margin=(0, 10, 4, 10),
        )
        extra = [name for name in self._draft.param if name not in ("name", *DEDICATED_SETTINGS)]
        widgets = {
            name: {"type": pmui.Switch, "name": label}
            for name, label in TILE_SETTINGS.items()
            if name in extra
        }
        # `pn.Param` would pick a slider over the whole bounds for a bounded number.
        self._extra_form = pn.Param(
            self._draft,
            parameters=extra,
            widgets=widgets,
            show_name=False,
            expand_button=False,
            sizing_mode="stretch_width",
            margin=0,
            visible=bool(extra),
        )
        return pn.Column(
            self._heading("General"),
            self._title_input,
            self._description_input,
            self._extra_form,
            self._inspect_switch,
            self._inspect_help,
            sizing_mode="stretch_width",
            margin=0,
        )

    def _build_refresh(self):
        self._refresh_choice = pmui.Pill(
            options=[*REFRESH_PRESETS, CUSTOM],
            value="Off",
            sizing_mode="stretch_width",
            margin=(8, 10),
            sx={
                "& .MuiChip-colorPrimary, & .MuiChip-colorPrimary:hover": {
                    "bgcolor": "primary.main",
                    "color": "primary.contrastText",
                }
            },
        )
        self._refresh_choice.param.watch(self._on_refresh_choice, "value")
        self._custom_period = pmui.NumberInput.from_param(
            self._draft.param.refresh_period, visible=False, width=200
        )
        self._draft.param.watch(self._on_draft_period, "refresh_period")
        if "tile_cards" in self._draft.param:
            self._draft.param.watch(self._sync_tile_titles, "tile_cards")
        self._refresh_info = pmui.Typography(
            variant="caption", sx={"color": "text.secondary"}, margin=(4, 10)
        )
        return pn.Column(
            self._heading("Refresh"),
            pmui.Typography(
                "Reload every component's data on a schedule. Viewers can still refresh by hand.",
                variant="body2",
                sx={"color": "text.secondary"},
                margin=(0, 10),
            ),
            self._refresh_choice,
            self._custom_period,
            self._refresh_info,
            sizing_mode="stretch_width",
            margin=0,
        )

    def _build_sharing(self):
        self._link_input = pmui.TextInput(label="Link", sizing_mode="stretch_width")
        self._copy_button = pmui.Button(
            label="Copy link", icon="link", variant="outlined", align="center", margin=(10, 10)
        )
        self._copy_button.js_on_click(
            args={"link": self._link_input},
            code="navigator.clipboard?.writeText(link.data.value)",
        )
        self._add_input = pmui.AutocompleteInput(
            label="Add people or groups",
            placeholder="Name, login or group",
            restrict=False,
            options=[],
            min_characters=0,
            sizing_mode="stretch_width",
        )
        self._add_input.param.watch(self._on_add, "value")
        self._people = pn.Column(sizing_mode="stretch_width", margin=(4, 0))
        self._everyone_switch = pmui.Switch(label="", margin=(0, 10), align="center")
        self._everyone_switch.param.watch(self._on_everyone, "value")
        self._everyone_help = pmui.Typography(
            variant="caption", sx={"color": "text.secondary"}, margin=(0, 10)
        )
        self._everyone_row = pn.Row(
            pn.Column(
                pmui.Typography(
                    "Everyone in the project can view", variant="subtitle2", margin=(0, 10)
                ),
                self._everyone_help,
                sizing_mode="stretch_width",
                margin=0,
            ),
            self._everyone_switch,
            sizing_mode="stretch_width",
            margin=(8, 0),
        )
        return pn.Column(
            self._heading("Sharing"),
            pn.Row(self._link_input, self._copy_button, sizing_mode="stretch_width", margin=0),
            self._add_input,
            self._people,
            self._everyone_row,
            sizing_mode="stretch_width",
            margin=0,
        )

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------

    def show(self, section: str = "general", dashboard_id: str | None = None, title: str = ""):
        """Open the dialog on *section* for a dashboard, by default the editor's."""
        self._requested = (section, dashboard_id, title)
        if self.dialog.open:
            self._load()
        else:
            self.dialog.open = True

    def _on_open(self, event):
        if event.new:
            self._load()

    @pn.io.hold()
    def _load(self):
        """Fill every section from the target dashboard and the editor's settings."""
        section, dashboard_id, title = self._requested or ("general", None, "")
        self._requested = None
        current = self._editor.dashboard
        if dashboard_id is None and current is not None:
            dashboard_id, title = current.dashboard_id, current.title
        self._target_id = dashboard_id
        self._target_title = title or (current.title if current is not None else "")
        sharing = self.sharing if dashboard_id is not None else None
        self._access = sharing(dashboard_id) if sharing is not None else None

        for name in self._editor._settings_names:
            setattr(self._draft, name, getattr(self._editor.settings, name))
        self._refresh_choice.value = refresh_choice(self._draft.refresh_period)
        self._custom_period.visible = self._refresh_choice.value == CUSTOM
        self._title_input.param.update(
            value=self._target_title,
            disabled=self.rename is None or self._access is None,
            error_state=False,
            helper_text="",
            visible=dashboard_id is not None,
        )
        self._load_sharing()
        self._update_refresh_info()

        editable = self._settings_editable()
        for widget in (
            self._description_input,
            self._extra_form,
            self._inspect_switch,
            self._refresh_choice,
            self._custom_period,
        ):
            widget.disabled = not editable
        self._sync_tile_titles()
        owner = self._access.owner if self._access else None
        if not self._target_title:
            subtitle = ""
        elif owner is None:
            subtitle = self._target_title
        elif self._access is not None and owner in self._access.you:
            subtitle = f"{self._target_title}, owned by you"
        else:
            subtitle = f"{self._target_title}, owned by {owner}"
        self._subtitle.param.update(object=subtitle, visible=bool(subtitle))
        self._sync_nav(section)
        self._sync_footer()

    def _settings_editable(self) -> bool:
        """Whether the dialog edits the editor's settings, not just another dashboard's sharing."""
        current = self._editor.dashboard
        own = self._target_id is None or (
            current is not None and current.dashboard_id == self._target_id
        )
        return own and not self._editor.read_only

    def _available_sections(self) -> list[str]:
        own = self._settings_editable()
        sections = []
        if own or self._target_id is not None:
            sections.append("general")
        if own:
            sections.append("refresh")
        if self._access is not None:
            sections.append("sharing")
        return sections

    def _sync_nav(self, section: str):
        sections = self._available_sections()
        if section not in sections:
            section = sections[0] if sections else "general"
        self._nav.items = [
            {"label": SECTION_LABELS[name], "icon": SECTION_ICONS[name]} for name in sections
        ]
        self._nav_sections = sections
        self._delete_button.visible = (
            self.delete is not None and self._access is not None and self._target_id is not None
        )
        self.section = section
        if section in sections:
            self._nav.active = (sections.index(section),)

    def _on_nav(self, event):
        if event.new:
            index = event.new[0]
            if index < len(self._nav_sections):
                self.section = self._nav_sections[index]

    @param.depends("section", watch=True)
    def _show_section(self):
        for name, column in self._sections.items():
            column.visible = name == self.section

    def _sync_inspect(self, *_events):
        visible = self._editor.inspect_outputs
        self._inspect_switch.visible = visible
        self._inspect_help.visible = visible

    def _sync_footer(self):
        editable = self._settings_editable()
        note = []
        if self._access is not None:
            note.append("Changes to sharing apply when you save.")
        if editable and self._editor.dirty:
            note.append("Settings are saved with your other unsaved changes.")
        self._footer_note.param.update(object=" ".join(note), visible=bool(note))
        self._save_button.disabled = not (editable or self._access is not None)

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------

    def _on_title_input(self, _event):
        self._title_input.param.update(error_state=False, helper_text="")

    def _sync_tile_titles(self, *_events):
        # Titles live in the card's bar, so they mean nothing without the frame.
        titles = self._extra_form._widgets.get("tile_titles")
        if titles is not None:
            titles.disabled = not (self._settings_editable() and self._draft.tile_cards)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _on_refresh_choice(self, event):
        custom = event.new == CUSTOM
        self._custom_period.visible = custom
        if not custom:
            self._draft.refresh_period = REFRESH_PRESETS[event.new]
        self._update_refresh_info()

    def _on_draft_period(self, event):
        if self._refresh_choice.value != CUSTOM:
            self._refresh_choice.value = refresh_choice(event.new)
        self._update_refresh_info()

    def _on_refresh_start(self, _event):
        self._refresh_began = time.monotonic()

    def _on_refresh_end(self, _event):
        if self._refresh_began is not None:
            self._refresh_duration = time.monotonic() - self._refresh_began
            self._refresh_began = None
        self._last_refresh = time.time()

    def _next_refresh(self) -> float | None:
        """Return when the running refresh timer fires next, if it runs."""
        editor = self._editor
        started = getattr(editor, "_refresh_started", None)
        period = editor._refresh_ms / 1000
        if editor._refresh_timer is None or started is None or not period:
            return None
        elapsed = max(time.time() - started, 0)
        return started + (math.floor(elapsed / period) + 1) * period

    def _update_refresh_info(self):
        parts = []
        if self._draft.refresh_period != self._editor.settings.refresh_period:
            if self._draft.refresh_period:
                parts.append("The new schedule starts when you save.")
        else:
            upcoming = self._next_refresh() if self._settings_editable() else None
            if upcoming is not None:
                parts.append(
                    f"Next refresh at {time.strftime('%H:%M', time.localtime(upcoming))}."
                )
        if self._refresh_duration is not None:
            parts.append(f"Last refresh took {_seconds(self._refresh_duration)}.")
        self._refresh_info.param.update(object=" ".join(parts), visible=bool(parts))

    # ------------------------------------------------------------------
    # Sharing
    # ------------------------------------------------------------------

    def _load_sharing(self):
        access = self._access
        if access is None:
            self._initial_entries, self._initial_everyone = [], True
            self._entries = []
            self._people[:] = []
            return
        entries, everyone = entries_from_permission(access.permission, access.owner)
        self._initial_entries, self._initial_everyone = list(entries), everyone
        self._entries = list(entries)
        self._link_input.value = access.link
        self._add_input.param.update(
            options=sorted({*access.users, *access.groups}, key=str.lower), value=""
        )
        # Without the project default there is nothing per dashboard that opens
        # it to everyone, so the switch has nothing to turn on.
        self._everyone_switch.param.update(value=everyone and access.default_allow)
        self._everyone_row.visible = access.default_allow
        self._render_people()

    def _kind_of(self, name: str) -> tuple[str, Kind]:
        access = self._access
        if name.startswith("group:"):
            return name[len("group:") :].strip(), "group"
        if access is not None and name in access.groups and name not in access.users:
            return name, "group"
        return name, "user"

    def _on_add(self, event):
        value = (event.new or "").strip()
        if not value:
            return
        name, kind = self._kind_of(value)
        if name and (self._access is None or kind != "user" or name != self._access.owner):
            if not any(e.name == name and e.kind == kind for e in self._entries):
                self._entries.append(ShareEntry(name, kind, "view"))
                self._render_people()
        self._add_input.value = ""

    def set_role(self, name: str, kind: Kind, role: Role | t.Literal["remove"]):
        """Give a listed person or group another role, or remove them."""
        entries = [e for e in self._entries if not (e.name == name and e.kind == kind)]
        if role != REMOVE:
            index = next(
                (i for i, e in enumerate(self._entries) if e.name == name and e.kind == kind),
                len(entries),
            )
            entries.insert(index, ShareEntry(name, kind, role))
        self._entries = entries
        self._render_people()

    def _on_everyone(self, _event):
        self._update_everyone_help()

    def _update_everyone_help(self):
        access = self._access
        if access is None:
            return
        viewers = sum(entry.role == "view" for entry in self._entries)
        if self._everyone_switch.value and viewers:
            text = (
                "Everyone can view, so the people and groups with Can view are "
                "removed when you save."
            )
        elif self._everyone_switch.value:
            text = "The project default. Turn off to limit access to the people above."
        else:
            text = "Only the people and groups above can view."
        self._everyone_help.object = text

    def _person_row(self, entry: ShareEntry):
        if entry.role == "blocked":
            avatar = pmui.Avatar(":material/block:", sx={"bgcolor": "error.main"})
        elif entry.kind == "group":
            avatar = pmui.Avatar(
                ":material/groups:", sx={"bgcolor": "action.selected", "color": "text.primary"}
            )
        else:
            avatar = pmui.Avatar(_initials(entry.name), sx={"bgcolor": "primary.main"})
        caption = []
        if entry.kind == "group":
            size = self._access.group_sizes.get(entry.name) if self._access else None
            caption.append(f"Group, {size} member{'s' * (size != 1)}" if size else "Group")
        if entry.role == "blocked":
            caption.append("always blocked, even if listed above")
        role = pmui.Select(
            options={**{label: key for key, label in ROLE_LABELS.items()}, "Remove": REMOVE},
            value=entry.role,
            variant="standard",
            size="small",
            width=120,
            align="center",
            margin=(0, 10),
        )
        role.param.watch(
            lambda event, entry=entry: self.set_role(entry.name, entry.kind, event.new), "value"
        )
        return self._row(avatar, entry.name, ", ".join(caption), role)

    def _row(self, avatar, title: str, caption: str, action):
        avatar.param.update(
            margin=0,
            sx={
                **avatar.sx,
                "width": 36,
                "height": 36,
                "fontSize": "0.875rem",
                "borderRadius": "50%",
            },
        )
        return pn.Row(
            pn.Column(avatar, width=36, margin=(4, 12, 4, 10), align="center"),
            pn.Column(
                pmui.Typography(title, variant="subtitle2", margin=0),
                pmui.Typography(
                    caption, variant="caption", sx={"color": "text.secondary"}, margin=0
                ),
                sizing_mode="stretch_width",
                margin=0,
                align="center",
            ),
            action,
            css_classes=["share-entry"],
            sizing_mode="stretch_width",
            margin=(2, 0),
        )

    def _render_people(self):
        access = self._access
        if access is None:
            return
        you = " (you)" if access.owner in access.you else ""
        owner = self._row(
            pmui.Avatar(_initials(access.owner), sx={"bgcolor": "secondary.main"}),
            f"{access.owner}{you}",
            "",
            pmui.Typography(
                "Owner",
                variant="body2",
                sx={"color": "text.secondary"},
                align="center",
                margin=(0, 22),
            ),
        )
        self._people[:] = [owner, *(self._person_row(entry) for entry in self._entries)]
        self._update_everyone_help()

    def _permission(self) -> Permission | None:
        access = self._access
        if access is None:
            return None
        everyone = self._everyone_switch.value if access.default_allow else False
        return permission_from_entries(self._entries, everyone=everyone, owner=access.owner)

    def _sharing_changed(self) -> bool:
        access = self._access
        if access is None:
            return False
        if self._entries != self._initial_entries:
            return True
        if access.default_allow and self._everyone_switch.value != self._initial_everyone:
            return True
        return False

    # ------------------------------------------------------------------
    # Saving and deleting
    # ------------------------------------------------------------------

    def _on_delete(self, _event):
        if self.delete is None or self._target_id is None:
            return
        self.dialog.open = False
        self.delete(self._target_id, self._target_title)

    @pn.io.hold()
    def _on_save(self, _event=None) -> bool:
        """Apply the dialog's changes, returning whether all of them were applied."""
        target = self._target_id
        title = (self._title_input.value_input or self._title_input.value or "").strip()
        if (
            target is not None
            and self.rename is not None
            and self._access is not None
            and title != self._target_title
        ):
            error = self.rename(target, title)
            if error:
                self._title_input.param.update(error_state=True, helper_text=error)
                self._sync_nav("general")
                return False
            self._target_title = title

        if target is not None and self.save_sharing is not None and self._sharing_changed():
            if not self.save_sharing(target, self._permission()):
                return False
            self._initial_entries = list(self._entries)
            self._initial_everyone = self._everyone_switch.value

        if self._settings_editable():
            self._apply_settings()
        self.dialog.open = False
        return True

    def _apply_settings(self):
        """Apply the edited settings, saving the dashboard if nothing else is unsaved."""
        editor = self._editor
        changed = {
            name: getattr(self._draft, name)
            for name in editor._settings_names
            if getattr(self._draft, name) != getattr(editor.settings, name)
        }
        if not changed:
            return
        had_changes = editor.dirty
        editor.settings.param.update(**changed)
        # Settings changed on their own are saved right away, also from view mode
        # where there is no Save button. Next to unsaved canvas changes they join
        # those, so this never saves work the user has not chosen to save.
        if not had_changes and editor.dashboard is not None and editor.store is not None:
            editor._on_save_clicked()


def share_link(dashboard_path: str) -> str:
    """Return the absolute URL of *dashboard_path* on the current page's server."""
    location = pn.state.location
    href = getattr(location, "href", "") if location is not None else ""
    if not href:
        return dashboard_path
    parts = urlsplit(href)
    return f"{parts.scheme}://{parts.netloc}{dashboard_path}"
