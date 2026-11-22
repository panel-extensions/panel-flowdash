"""The Permissions section of the admin view: people and groups, and what the agent may do.

It edits the project file's ``permissions`` section (see
`panel_flowdash.permissions`) with the page's Save and Discard. Settings
the app was given in code, such as ``default_allow`` in ``__init__.py``,
show locked, and the code's sensitive components and approval rules are
listed with the built-in ones, which cannot be removed.
"""

from __future__ import annotations

import copy
import datetime as dt
import fnmatch
import typing as t

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.admin.access import admin_name
from panel_flowdash.admin.form import Group, Setting, SettingsForm, current_app
from panel_flowdash.admin.kit import Table, caption, cell, chip, clear_text, esc, heading, lock
from panel_flowdash.permissions import describe_rule, load_permissions, rule_problems
from panel_flowdash.project import Project, ProjectSection, register_section

POLICY_GROUPS = (
    Group(
        "Access policy",
        (
            Setting(
                "default_allow",
                "Without sharing rules",
                "What a dashboard nobody has shared allows.",
                kind="choice",
                options={"Everyone can view": True, "Only its owner": False},
                default=True,
                target="auth_config.default_allow",
            ),
            Setting(
                "admin_groups",
                "Admin groups",
                "Members can open Admin and edit every dashboard.",
                kind="names",
                unit="a group",
                default=[],
                target="auth_config.admin_groups",
                from_target=lambda value: sorted(value or ()),
            ),
        ),
    ),
)

EXTRA = ("people", "assistant_groups", "sensitive_components", "approval_rules")

_EMPTY: dict[str, t.Any] = {
    "people": {},
    "assistant_groups": None,
    "sensitive_components": [],
    "approval_rules": [],
}

# The approval rules every agent has, shown so admins know they are there.
BUILT_IN_RULES = (
    ("save_dashboard, delete_*, share_*", "Always, and the palette's save, delete and share"),
    ("add_node, set_config, load YAML", "places or changes a sensitive component"),
)

_AVATAR_COLORS = ("#00796b", "#5e35b1", "#c2185b", "#ef6c00", "#1565c0", "#6d4c41", "#2e7d32")

_PERSON_CSS = """
.k .person { display: flex; align-items: center; gap: 12px; min-width: 0; }
.k .initials { width: 32px; height: 32px; border-radius: 50%; color: #fff; font-size: 0.8125rem;
  display: flex; align-items: center; justify-content: center; flex: 0 0 auto; font-weight: 500; }
.k .person .who { min-width: 0; }
"""


def _initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").replace("_", " ").replace("@", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _seen(value: str | None) -> str:
    if not value:
        return "never"
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    seconds = (dt.datetime.now(dt.UTC) - when).total_seconds()
    if seconds < 120:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    return f"{int(seconds // 86400)} days ago"


def _clean(value: dict[str, t.Any]) -> dict[str, t.Any]:
    """Drop the settings left at their default, so the file says only what was set."""
    people = {
        user: {
            k: v
            for k, v in person.items()
            if not (k == "groups" and not v) and not (k == "assistant" and v is True)
        }
        for user, person in (value.get("people") or {}).items()
    }
    value = {**value, "people": {u: p for u, p in people.items() if p}}
    return {
        k: v
        for k, v in value.items()
        if v is not None
        and not (k in EXTRA and v in ([], {}))
        and not (k == "admin_groups" and v == [])
    }


class PermissionsForm(SettingsForm):
    """The Permissions section, saved with the admin page's Save like the other forms."""

    def __init__(self, project: Project, editable: bool = True, **params):
        super().__init__(
            project,
            "permissions",
            POLICY_GROUPS,
            editable=editable,
            columns=1,
            from_form=_clean,
            **params,
        )

    # ------------------------------------------------------------------
    # Values beyond the policy rows
    # ------------------------------------------------------------------

    def _load_extra(self):
        self._extra_initial = {
            key: copy.deepcopy(self._initial.get(key, _EMPTY[key])) for key in EXTRA
        }
        self._extra = copy.deepcopy(self._extra_initial)

    @property
    def extra(self) -> dict[str, t.Any]:
        """Return the people, assistant groups, sensitive components and rules as edited."""
        return copy.deepcopy(self._extra)

    @staticmethod
    def _norm(key: str, value: t.Any) -> t.Any:
        if key == "people":
            return {user: person for user, person in (value or {}).items() if person}
        return value

    def _extra_changes(self) -> dict[str, t.Any]:
        if getattr(self, "_extra", None) is None:
            return {}
        return {
            key: copy.deepcopy(self._extra[key])
            for key in EXTRA
            if self._norm(key, self._extra[key]) != self._norm(key, self._extra_initial[key])
        }

    def changes(self) -> dict[str, t.Any]:
        """Return what changed since the form was loaded or saved, the tables included."""
        return {**super().changes(), **self._extra_changes()}

    def _sync_dirty(self):
        super()._sync_dirty()
        if not self.dirty and self._extra_changes():
            self.dirty = True

    def save(self) -> bool:
        """Write the section and redraw the tables from what was saved."""
        if not super().save():
            return False
        self._load_extra()
        self._render()
        self._sync_dirty()
        return True

    def reset(self):
        """Put the controls and tables back to the saved values."""
        super().reset()
        self._load_extra()
        self._render()
        self._sync_dirty()

    def _changed(self):
        self._sync_dirty()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self, aside):
        self._app = current_app()
        self._load_extra()
        policy = super()._build(None)
        self._people = Table(
            [
                ("Person", "2fr"),
                ("Groups", "2.4fr"),
                ("Role", "0.8fr"),
                ("Owns", "56px"),
                ("Last seen", "1fr"),
                ("Assistant", "88px"),
                ("", "40px"),
            ],
            footer=caption(
                "Roles are derived: Admin from the admin groups and Admins under General, Editor "
                "from owning a dashboard or an edit rule on one.",
                margin=(12, 12),
            ),
            empty="No one has opened the app yet.",
            css_classes=["flowdash-admin-people"],
        )
        self._new_person = pmui.TextInput(
            placeholder="User name, then Enter", size="small", width=220, margin=0
        )
        self._new_person.param.watch(lambda _event: self.add_person(), "enter_pressed")
        add_person = pmui.Button(
            label="Add person", icon="person_add", variant="text", size="small", margin=0
        )
        add_person.on_click(lambda _event: self.add_person())
        people_tab = pn.Column(
            policy,
            heading("People", self._new_person, add_person),
            self._people,
            margin=0,
            sizing_mode="stretch_width",
        )
        # Who gets the assistant.
        self._assistant_groups = pmui.MultiChoice(
            options=[], value=[], placeholder="Everyone", width=360, margin=0, label="Groups"
        )
        self._assistant_groups.param.watch(self._on_assistant_groups, "value")
        self._assistant_text = cell("")
        # Sensitive components.
        self._sensitive = Table(
            [("Component", "1.3fr"), ("Matches", "1.5fr"), ("Set", "1fr"), ("", "40px")],
            empty="No sensitive components.",
            css_classes=["flowdash-admin-sensitive"],
        )
        self._new_pattern = pmui.TextInput(
            placeholder="Id or pattern, e.g. Sources/*", size="small", width=240, margin=0
        )
        self._new_pattern.param.watch(lambda _event: self.add_pattern(), "enter_pressed")
        add_pattern = pmui.Button(
            label="Add pattern", icon="add", variant="text", size="small", margin=0
        )
        add_pattern.on_click(lambda _event: self.add_pattern())
        # Approval rules.
        self._rules = Table(
            [("Tool", "1.6fr"), ("When", "1.8fr"), ("Set", "1fr"), ("On", "72px"), ("", "40px")],
            css_classes=["flowdash-admin-rules"],
        )
        self._new_tool = pmui.TextInput(
            placeholder="Tool, e.g. run_sql", size="small", width=180, margin=0
        )
        self._new_when = pmui.TextInput(
            placeholder="Optional: argument=value", size="small", width=200, margin=0
        )
        self._new_tool.param.watch(lambda _event: self.add_rule(), "enter_pressed")
        add_rule = pmui.Button(
            label="Add rule", icon="add", variant="text", size="small", margin=0
        )
        add_rule.on_click(lambda _event: self.add_rule())
        self._agent_alert = pmui.Alert(
            object="",
            severity="error",
            visible=False,
            sizing_mode="stretch_width",
            margin=(8, 0, 0, 0),
        )
        agent_tab = pn.Column(
            heading("Who gets the assistant", margin=(16, 0, 4, 0)),
            pn.Row(
                self._assistant_text,
                self._assistant_groups,
                margin=0,
                sizing_mode="stretch_width",
                styles={
                    "align-items": "center",
                    "gap": "16px",
                    "padding": "10px 0",
                    "border-bottom": "1px solid var(--mui-palette-divider)",
                },
                css_classes=["flowdash-setting", "flowdash-setting-assistant_groups"],
            ),
            heading(
                "Sensitive components",
                cell(
                    '<span class="muted small"><code>agent_sensitive_components</code></span>',
                    sizing_mode=None,
                ),
                self._new_pattern,
                add_pattern,
            ),
            self._sensitive,
            heading(
                "Approval rules",
                cell(
                    '<span class="muted small"><code>agent_approval_rules</code></span>',
                    sizing_mode=None,
                ),
                self._new_tool,
                self._new_when,
                add_rule,
            ),
            self._agent_alert,
            self._rules,
            margin=0,
            sizing_mode="stretch_width",
        )
        self._tabs = pmui.Tabs(
            ("People and groups", people_tab),
            ("Agent", agent_tab),
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-permission-tabs"],
        )
        self._render()
        return pn.Column(self._tabs, sizing_mode="stretch_width", margin=0)

    # ------------------------------------------------------------------
    # What the tables show
    # ------------------------------------------------------------------

    def _known_groups(self) -> list[str]:
        groups: set[str] = set()
        if self._app is not None:
            groups |= set(self._app._known_groups())
        for person in self._extra["people"].values():
            groups |= set(person.get("groups") or ())
        groups |= set(self._extra["assistant_groups"] or ())
        groups |= set(self._widget_value("admin_groups") or ())
        return sorted(groups)

    def people(self) -> dict[str, dict[str, t.Any]]:
        """Return everyone the app knows of, with their groups, role, dashboards and last visit."""
        app = self._app
        code_groups: dict[str, set[str]] = {}
        seen: dict[str, dict[str, t.Any]] = {}
        admins: set[str] = set()
        owned: dict[str, int] = {}
        editors: set[str] = set()
        if app is not None:
            config = app._code_auth_config
            for user, groups in (config.user_groups if config else {}).items():
                code_groups[user] = set(groups)
            seen = app._usage_ledger().people()
            admins = {admin_name(entry) for entry in app._configured_admins()}
            try:
                dashboards = app.store._all_dashboards()
            except Exception:
                dashboards = []
            for model in dashboards:
                owned[model.owner] = owned.get(model.owner, 0) + 1
                permission = getattr(model, "permission", None)
                if permission is not None:
                    editors |= set(permission.allow_edit_users)
        file_people = self._extra["people"]
        users = dict.fromkeys([*seen, *code_groups, *admins, *owned, *file_people])
        users.pop("anonymous", None)
        admin_groups = set(self._widget_value("admin_groups") or ())
        if app is not None and app._code_auth_config is not None:
            admin_groups |= (
                set(app._code_auth_config.admin_groups)
                if "admin_groups" in app._auth_code_fields()
                else set()
            )
        result = {}
        for user in users:
            fixed = code_groups.get(user, set()) | set((seen.get(user) or {}).get("groups") or ())
            mine = set((file_people.get(user) or {}).get("groups") or ())
            groups = fixed | mine
            if user in admins or groups & admin_groups:
                role = "Admin"
            elif owned.get(user) or user in editors:
                role = "Editor"
            else:
                role = "Viewer"
            result[user] = {
                "fixed_groups": sorted(fixed - mine),
                "groups": sorted(mine),
                "role": role,
                "owns": owned.get(user, 0),
                "last_seen": (seen.get(user) or {}).get("last_seen"),
                "assistant": (file_people.get(user) or {}).get("assistant", True),
                "in_file": user in file_people,
            }
        return result

    def _assistant_lock(self) -> str | None:
        if self._app is None or self._app.agent_authorize is None:
            return None
        return self._app._code_setting_source("agent_authorize") or "agent_authorize set in code"

    def _render(self):
        if not hasattr(self, "_tabs"):
            return
        groups = self._known_groups()
        locked = not self.editable
        rows = []
        for index, (user, person) in enumerate(self.people().items()):
            color = _AVATAR_COLORS[index % len(_AVATAR_COLORS)]
            who = cell(
                f'<div class="person"><span class="initials" style="background:{color}">'
                f'{esc(_initials(user))}</span><span class="who"><span class="main">{esc(user)}</span>'
                "</span></div>",
                stylesheets=[_PERSON_CSS],
            )
            fixed = "".join(chip(g) for g in person["fixed_groups"])
            choose = pmui.MultiChoice(
                options=sorted(set(groups) | set(person["groups"])),
                value=list(person["groups"]),
                placeholder="Add groups",
                sizing_mode="stretch_width",
                margin=0,
                disabled=locked,
            )
            choose.param.watch(lambda event, user=user: self.set_groups(user, event.new), "value")
            role_tone = {"Admin": "primary", "Editor": "", "Viewer": "outlined"}[person["role"]]
            switch = pmui.Switch(
                value=bool(person["assistant"]),
                margin=0,
                disabled=locked or bool(self._assistant_lock()),
            )
            switch.param.watch(
                lambda event, user=user: self.set_assistant(user, event.new), "value"
            )
            remove = pmui.IconButton(
                icon="delete_outline",
                description=f"Remove {user}'s settings",
                size="small",
                margin=0,
                visible=person["in_file"],
                disabled=locked,
            )
            remove.on_click(lambda _event, user=user: self.remove_person(user))
            rows.append(
                (
                    user,
                    [
                        who,
                        pn.Column(cell(fixed), choose, margin=0, sizing_mode="stretch_width")
                        if fixed
                        else choose,
                        cell(chip(person["role"], role_tone)),
                        cell(f"<span>{person['owns']}</span>"),
                        cell(f'<span class="muted">{esc(_seen(person["last_seen"]))}</span>'),
                        switch,
                        remove,
                    ],
                )
            )
        self._people.set_rows(rows)
        # Who gets the assistant.
        where = self._assistant_lock()
        description = (
            "People outside these groups get no Assistant button, and Explore is read-only for "
            "them. Empty: everyone."
        )
        self._assistant_text.object = (
            f'<div class="k"><div class="label" style="display:flex;gap:8px;align-items:center;'
            f'font-size:1rem">Assistant{lock(where) if where else ""}</div>'
            f'<div class="muted" style="font-size:0.8125rem;margin-top:2px">{esc(description)}</div></div>'
        )
        chosen = list(self._extra["assistant_groups"] or [])
        self._assistant_groups.param.update(
            options=sorted(set(groups) | set(chosen)), value=chosen, disabled=locked or bool(where)
        )
        self._render_sensitive()
        self._render_rules()

    def _components(self) -> dict[str, str]:
        if self._app is None:
            return {}
        try:
            entries = self._app._component_entries
        except Exception:
            return {}
        return {cid: (entry.metadata.title or cid) for cid, entry in entries.items()}

    def _render_sensitive(self):
        components = self._components()
        code = list(self._app.agent_sensitive_components) if self._app is not None else []
        where = (
            self._app._code_setting_source("agent_sensitive_components") if self._app else None
        ) or "in code"
        rows = []
        for pattern in [*code, *(p for p in self._extra["sensitive_components"] if p not in code)]:
            matches = [
                title for cid, title in components.items() if fnmatch.fnmatchcase(cid, pattern)
            ]
            if len(matches) == 1:
                matched = esc(matches[0])
            elif matches:
                matched = f"{len(matches)} components"
            else:
                matched = '<span class="muted">No component yet</span>'
            in_code = pattern in code
            remove = pmui.IconButton(
                icon="delete_outline",
                description=f"Remove {pattern}",
                size="small",
                margin=0,
                visible=not in_code,
                disabled=not self.editable,
            )
            remove.on_click(lambda _event, pattern=pattern: self.remove_pattern(pattern))
            rows.append(
                (
                    pattern,
                    [
                        cell(f"<code>{esc(pattern)}</code>"),
                        cell(f'<span class="main">{matched}</span>'),
                        cell(
                            lock("in code", where)
                            if in_code
                            else '<span class="muted">Added here</span>'
                        ),
                        remove,
                    ],
                )
            )
        self._sensitive.set_rows(rows)

    def _render_rules(self):
        rows = []
        for tool, when in BUILT_IN_RULES:
            rows.append(
                (
                    tool,
                    [
                        cell(f"<code>{esc(tool)}</code>"),
                        cell(f'<span class="main">{esc(when)}</span>'),
                        cell(chip("Built in")),
                        pmui.Switch(value=True, disabled=True, margin=0),
                        pn.Spacer(width=0),
                    ],
                )
            )
        code = list(self._app.agent_approval_rules) if self._app is not None else []
        where = (
            self._app._code_setting_source("agent_approval_rules") if self._app else None
        ) or "in code"
        for rule in code:
            tool, when = describe_rule(rule)
            rows.append(
                (
                    f"code-{tool}",
                    [
                        cell(f"<code>{esc(tool)}</code>"),
                        cell(f'<span class="main">{esc(when)}</span>'),
                        cell(lock("in code", where)),
                        pmui.Switch(value=True, disabled=True, margin=0),
                        pn.Spacer(width=0),
                    ],
                )
            )
        for index, rule in enumerate(self._extra["approval_rules"]):
            tool, when = describe_rule(rule)
            switch = pmui.Switch(
                value=rule.get("enabled", True) is not False,
                margin=0,
                disabled=not self.editable,
            )
            switch.param.watch(
                lambda event, index=index: self.enable_rule(index, event.new), "value"
            )
            remove = pmui.IconButton(
                icon="delete_outline",
                description=f"Remove the rule for {tool}",
                size="small",
                margin=0,
                disabled=not self.editable,
            )
            remove.on_click(lambda _event, index=index: self.remove_rule(index))
            rows.append(
                (
                    f"file-{index}-{tool}",
                    [
                        cell(f"<code>{esc(tool)}</code>"),
                        cell(f'<span class="main">{esc(when)}</span>'),
                        cell('<span class="muted">Added here</span>'),
                        switch,
                        remove,
                    ],
                )
            )
        self._rules.set_rows(rows)

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------

    def _person(self, user: str) -> dict[str, t.Any]:
        return self._extra["people"].setdefault(user, {})

    def _prune(self, user: str):
        person = self._extra["people"].get(user)
        if (
            person is not None
            and not person.get("groups")
            and person.get("assistant", True) is True
        ):
            # Kept in the table while edited; dropped from the file when it says nothing.
            person.pop("groups", None)
            person.pop("assistant", None)

    def set_groups(self, user: str, groups: t.Sequence[str]):
        """Give *user* *groups*, on top of those from code and sign-in."""
        self._person(user)["groups"] = sorted(dict.fromkeys(groups))
        self._prune(user)
        self._changed()

    def set_assistant(self, user: str, allowed: bool):
        """Give the assistant to *user*, or keep it from them."""
        person = self._person(user)
        if allowed:
            person.pop("assistant", None)
        else:
            person["assistant"] = False
        self._prune(user)
        self._changed()

    def add_person(self, user: str | None = None):
        """List *user* (or the name typed) so their groups and assistant can be set."""
        user = (user or self._new_person.value_input or self._new_person.value or "").strip()
        if not user:
            return
        self._extra["people"].setdefault(user, {})
        clear_text(self._new_person)
        self._render()
        self._changed()

    def remove_person(self, user: str):
        """Drop what the project file says about *user*."""
        self._extra["people"].pop(user, None)
        self._render()
        self._changed()

    def _on_assistant_groups(self, event):
        value = list(event.new) or None
        if value != self._extra["assistant_groups"]:
            self._extra["assistant_groups"] = value
            self._changed()

    def add_pattern(self, pattern: str | None = None):
        """Mark the components matching *pattern* (or the one typed) as sensitive."""
        pattern = (
            pattern or self._new_pattern.value_input or self._new_pattern.value or ""
        ).strip()
        if not pattern:
            return
        if pattern not in self._extra["sensitive_components"]:
            self._extra["sensitive_components"].append(pattern)
        clear_text(self._new_pattern)
        self._render_sensitive()
        self._changed()

    def remove_pattern(self, pattern: str):
        """Stop treating the components matching *pattern* as sensitive, unless code says so."""
        self._extra["sensitive_components"] = [
            p for p in self._extra["sensitive_components"] if p != pattern
        ]
        self._render_sensitive()
        self._changed()

    def add_rule(self, tool: str | None = None, when: str | None = None) -> bool:
        """Ask before calls to *tool*, or only those whose argument has a value (``name=value``)."""
        tool = (tool or self._new_tool.value_input or self._new_tool.value or "").strip()
        when = (
            when
            if when is not None
            else (self._new_when.value_input or self._new_when.value or "")
        ).strip()
        rule: dict[str, t.Any] = {"tool": tool}
        if when:
            name, sep, value = when.partition("=")
            if not sep or not name.strip():
                self._agent_alert.param.update(
                    object="Write the condition as argument=value, e.g. command=action:refresh.",
                    visible=True,
                )
                return False
            rule["when"] = {name.strip(): value.strip()}
        problems = rule_problems(rule)
        if problems:
            self._agent_alert.param.update(object=problems[0][1], visible=True)
            return False
        self._agent_alert.visible = False
        self._extra["approval_rules"].append(rule)
        for widget in (self._new_tool, self._new_when):
            clear_text(widget)
        self._render_rules()
        self._changed()
        return True

    def enable_rule(self, index: int, enabled: bool):
        """Turn the project file's rule *index* on or off."""
        rule = self._extra["approval_rules"][index]
        if enabled:
            rule.pop("enabled", None)
        else:
            rule["enabled"] = False
        self._changed()

    def remove_rule(self, index: int):
        """Remove the project file's rule *index*."""
        del self._extra["approval_rules"][index]
        self._render_rules()
        self._changed()


def permissions_settings(project: Project, editable: bool) -> PermissionsForm:
    """Build the Permissions section of the admin view."""
    return PermissionsForm(project, editable)


PERMISSIONS = ProjectSection(
    "permissions",
    load_permissions,
    title="Permissions",
    icon="admin_panel_settings",
    description=(
        "Who can use the app, who administers it, and what the agent may do on someone's behalf."
    ),
    settings_view=permissions_settings,
    group="App",
)

register_section(PERMISSIONS)
