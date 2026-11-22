"""The Dashboards section of the admin view: every dashboard in the store, whoever owns it.

It works on the dashboard store, not the project file. Share, Change
owner, Prune history and Delete act on the selected dashboards after a
confirmation; each checks again that the caller is an admin before it
changes anything and is written to the admin log with who did it and when.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import logging
import os
import typing as t
import zipfile
from functools import partial
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin.form import current_app, current_section
from panel_flowdash.admin.kit import Table, caption, cell, chip, clear_text, esc, heading
from panel_flowdash.admin.permissions import _AVATAR_COLORS, _PERSON_CSS, _initials
from panel_flowdash.component_settings import TURNED_OFF, placement_refusal
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.history import _size, compact
from panel_flowdash.project import Problem, Project, ProjectError, ProjectSection
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger(__name__)

FILTERS = {
    "all": "All",
    "mine": "Mine",
    "private": "Private",
    "issues": "Has issues",
    "stale": "Not opened in 90 days",
}

STALE_DAYS = 90

PAGE_SIZE = 25

_CHIP_SX = {"fontSize": "0.875rem"}

_MENU_SX = {
    "minWidth": 0,
    "padding": "6px",
    "borderRadius": "50%",
    "color": "text.secondary",
    "& .MuiButton-startIcon": {"margin": 0},
    "& .MuiButton-endIcon": {"display": "none"},
}

_ROW_CSS = """
.k .dash .main { font-weight: 400; }
.k .health { display: inline-flex; align-items: center; gap: 6px; }
.k .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block;
  background: var(--mui-palette-success-main, #2e7d32); }
.k .dot.warning { background: var(--mui-palette-warning-main, #ed6c02); }
.k .dot.error { background: var(--mui-palette-error-main, #d32f2f); }
"""

_LOG_CSS = """
.k ul { margin: 0; padding: 0; list-style: none; }
.k li { padding: 4px 0; font-size: 0.8125rem; color: var(--mui-palette-text-secondary); }
.k li b { font-weight: 500; color: var(--mui-palette-text-primary); }
"""


def _when(value: dt.datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    seconds = (dt.datetime.now(dt.UTC) - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)} days ago"
    return f"{value.day} {value:%b %Y}" if seconds > 300 * 86400 else f"{value.day} {value:%b}"


def _bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024**2:.1f} MB"


def _parse_time(value: t.Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


@dataclasses.dataclass
class DashboardRow:
    """What the Dashboards table shows for one dashboard."""

    model: DashboardModel
    versions: int = 0
    history_bytes: int = 0
    edited_by: str | None = None
    last_opened: dt.datetime | None = None
    access: tuple[str, ...] = ()
    private: bool = False
    tiles: int = 0
    issues: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.model.dashboard_id


def history_stats(records: t.Sequence[dict[str, t.Any]]) -> tuple[int, int, str | None]:
    """Return a stored history's number of versions, their size and who made the latest."""
    published = [r for r in records if r.get("type") == "ARTIFACT_PUBLISHED"]
    if not published:
        return 0, 0, None
    last = published[-1]
    if last.get("actor") == "agent":
        by = "the assistant"
    else:
        by = last.get("user") or None
    return len(published), sum(map(_size, published)), by


class DashboardsAdmin(Viewer):
    """The Dashboards section: the table, its filters, the bulk bar and their dialogs."""

    filter = param.Selector(default="all", objects=list(FILTERS))

    page = param.Integer(default=0, bounds=(0, None))

    def __init__(self, project: Project, editable: bool = True, **params):
        super().__init__(**params)
        self.project = project
        self.editable = editable
        self._app: FlowDashApp | None = current_app()
        self.selection: set[str] = set()
        self._syncing = False
        self._rows: list[DashboardRow] = []
        self._alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._chips = {
            key: pmui.Chip(
                label=label, size="medium", margin=0, sx=_CHIP_SX, css_classes=[f"filter-{key}"]
            )
            for key, label in FILTERS.items()
        }
        for key, widget in self._chips.items():
            widget.on_click(partial(self._set_filter, key))
        self._store_note = caption("", styles={"white-space": "nowrap"})
        chips = pn.Row(
            *self._chips.values(),
            pn.layout.HSpacer(),
            self._store_note,
            margin=(0, 0, 16, 0),
            sizing_mode="stretch_width",
            styles={"gap": "8px", "align-items": "center", "flex-wrap": "wrap"},
        )
        self._build_bulk_bar()
        self._select_page = pmui.Checkbox(value=False, margin=0, label="")
        self._select_page.param.watch(self._on_select_page, "value")
        self._table = Table(
            [
                (self._select_page, "40px"),
                ("Dashboard", "2.4fr"),
                ("Owner", "1.4fr"),
                ("Last edited", "1.3fr"),
                ("Versions", "0.9fr"),
                ("Access", "1.2fr"),
                ("Health", "0.8fr"),
                ("", "40px"),
            ],
            toolbar=self._bulk,
            footer=self._build_pager(),
            empty="No dashboards match.",
            css_classes=["flowdash-admin-dashboards"],
        )
        self._log = cell("", stylesheets=[_LOG_CSS])
        self._build_dialog()
        self._layout = pn.Column(
            self._alert,
            chips,
            self._table,
            heading("Admin log", margin=(24, 0, 4, 0)),
            self._log,
            self._dialog,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-dashboards-section"],
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _store(self):
        return self._app.store if self._app is not None else None

    def _ledger(self):
        return self._app._usage_ledger() if self._app is not None else None

    def _default_allow(self) -> bool:
        return self._app._default_allow() if self._app is not None else True

    def _user_names(self) -> set[str]:
        if self._app is None:
            return set()
        return set(self._app._identity.user_names)

    def home_dashboard(self) -> str | None:
        """Return the id or title the app opens as its home page, if any."""
        if self._app is None:
            return None
        return self._app.home_dashboard or None

    def home_locked(self) -> str | None:
        """Return where the home page is set in code, if it is."""
        if self._app is None:
            return None
        return self._app._code_setting_source("home_dashboard")

    def _issues(self, model: DashboardModel) -> tuple[int, tuple[str, ...]]:
        app = self._app
        if app is None:
            return len(model.items), ()
        editor = app._editor
        entries = editor._component_entries
        aliases = editor._component_aliases()
        settings = app._component_settings() or {}
        issues = []
        for item in model.items:
            cid = editor._resolve_component_id(item.component_id, aliases)
            entry = entries.get(cid)
            if entry is None:
                issues.append(f"{item.instance_id}: unknown component {cid}")
            elif entry.load_error is not None:
                issues.append(f"{item.instance_id}: {entry.title} failed to load")
            elif placement_refusal(settings, cid, None) == TURNED_OFF:
                issues.append(f"{item.instance_id}: {entry.title} is {TURNED_OFF}")
        return len(model.items), tuple(issues)

    def _access(self, model: DashboardModel) -> tuple[tuple[str, ...], bool]:
        """Return who can view *model*, as chips, and whether only its owner can."""
        permission = model.permission
        names = tuple(sorted(permission.allow_groups)) + tuple(
            sorted(u for u in permission.allow_users if u != model.owner)
        )
        if names:
            return names, False
        if permission.allow_users or not self._default_allow():
            return ("Private",), True
        return ("Everyone",), False

    def load_rows(self) -> list[DashboardRow]:
        """Read every dashboard in the store with its history, access and health."""
        store = self._store()
        if store is None:
            return []
        try:
            models = store._all_dashboards()
        except Exception as exc:
            logger.exception("Could not list the dashboards")
            self._show_error(f"Could not list the dashboards: {error_text(exc)}")
            return []
        ledger = self._ledger()
        opened = ledger.dashboards() if ledger is not None else {}
        rows = []
        for model in models:
            try:
                records = store.load_history(model.dashboard_id)
            except Exception:
                records = []
            versions, size, by = history_stats(records)
            access, private = self._access(model)
            tiles, issues = self._issues(model)
            rows.append(
                DashboardRow(
                    model=model,
                    versions=versions,
                    history_bytes=size,
                    edited_by=by,
                    last_opened=_parse_time(
                        (opened.get(model.dashboard_id) or {}).get("last_opened")
                    ),
                    access=access,
                    private=private,
                    tiles=tiles,
                    issues=issues,
                )
            )
        return rows

    def _passes(self, row: DashboardRow, key: str) -> bool:
        if key == "mine":
            return row.model.owner in self._user_names()
        if key == "private":
            return row.private
        if key == "issues":
            return bool(row.issues)
        if key == "stale":
            seen = row.last_opened or row.model.updated_at
            if seen is None:
                return True
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=dt.UTC)
            return (dt.datetime.now(dt.UTC) - seen).days >= STALE_DAYS
        return True

    def counts(self) -> dict[str, int]:
        """Return how many dashboards each filter chip shows."""
        return {key: sum(self._passes(r, key) for r in self._rows) for key in FILTERS}

    def visible(self) -> list[DashboardRow]:
        """Return the dashboards the filter chip passes."""
        return [r for r in self._rows if self._passes(r, self.filter)]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _set_filter(self, key: str, *_args):
        self.param.update(filter=key, page=0)

    @param.depends("filter", "page", watch=True)
    def _redraw(self):
        self._render()

    def refresh(self):
        """Read the store again and redraw everything."""
        self._rows = self.load_rows()
        known = {r.id for r in self._rows}
        self.selection &= known
        self._render()

    def _render(self):
        counts = self.counts()
        visible = self.visible()
        pages = max(1, -(-len(visible) // PAGE_SIZE))
        page = min(self.page, pages - 1)
        shown = visible[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        with pn.io.hold():
            for key, widget in self._chips.items():
                active = key == self.filter
                widget.param.update(
                    label=f"{FILTERS[key]} {counts[key]}",
                    icon="check" if active else None,
                    variant="filled" if active else "outlined",
                    color="primary" if active else "default",
                )
            self._store_note.object = self._store_text()
            self._table.set_rows([(r.id, self._cells(r)) for r in shown])
            self._set_page_box(bool(shown) and all(r.id in self.selection for r in shown))
            first = page * PAGE_SIZE + 1 if visible else 0
            self._pager_text.object = f"{first}-{page * PAGE_SIZE + len(shown)} of {len(visible)}"
            self._prev.disabled = page == 0
            self._next.disabled = page >= pages - 1
            self._sync_bulk()
            self._render_log()

    def _store_text(self) -> str:
        store = self._store()
        if store is None:
            return ""
        if isinstance(store, DashboardStore):
            path = Path(store._db_path)
            try:
                size = f" · {_bytes(os.path.getsize(path))}"
            except OSError:
                size = ""
            return f"Store: {path.name} (SQLite){size}"
        folder = getattr(store, "path", None)
        if folder is not None:
            return f"Store: {Path(folder).name}/ (JSON files)"
        return f"Store: {type(store).__name__}"

    def _cells(self, row: DashboardRow) -> list:
        model = row.model
        check = pmui.Checkbox(value=row.id in self.selection, margin=0, label="")
        check.param.watch(lambda event, did=row.id: self.select(did, event.new), "value")
        description = (model.settings or {}).get("description") or ""
        home = self.home_dashboard()
        home_chip = chip("Home", "primary") if home in (model.dashboard_id, model.title) else ""
        title = cell(
            f'<div class="dash"><span class="main">{esc(model.title)} {home_chip}</span>'
            f'<span class="main muted small">{esc(description) or "&nbsp;"}</span></div>',
            stylesheets=[_ROW_CSS],
        )
        owner = model.owner
        color = _AVATAR_COLORS[sum(map(ord, owner)) % len(_AVATAR_COLORS)]
        who = cell(
            f'<div class="person"><span class="initials" style="background:{color}">'
            f'{esc(_initials(owner))}</span><span class="who"><span class="main">{esc(owner)}</span>'
            "</span></div>",
            stylesheets=[_PERSON_CSS],
        )
        edited = cell(
            f'<span class="main">{esc(_when(model.updated_at))}</span>'
            + (
                f'<span class="main muted small">by {esc(row.edited_by)}</span>'
                if row.edited_by
                else ""
            )
        )
        versions = cell(
            f'<span class="main">{row.versions}</span>'
            f'<span class="main muted small">{_bytes(row.history_bytes) if row.versions else "No history"}</span>'
        )
        if row.access == ("Everyone",):
            access = chip("Everyone", "outlined")
        elif row.access == ("Private",):
            access = chip("Private")
        else:
            access = "".join(chip(name, "outlined") for name in row.access)
        ok = row.tiles - len(row.issues)
        tone = "" if not row.issues else ("error" if ok == 0 else "warning")
        health = cell(
            f'<span class="health" title="{esc("; ".join(row.issues))}">'
            f'<span class="dot {tone}"></span>{ok}/{row.tiles}</span>',
            stylesheets=[_ROW_CSS],
        )
        menu = pmui.MenuButton(
            items=self._menu_items(row),
            icon="more_vert",
            label="",
            variant="text",
            color="default",
            size="small",
            icon_size="20px",
            margin=0,
            sx=_MENU_SX,
            description=f"More actions for {model.title}",
            css_classes=["flowdash-admin-dashboard-menu"],
        )
        menu.on_click(lambda item, did=row.id: self.run(item.get("action"), did))
        return [check, title, who, edited, versions, cell(access), health, menu]

    def _menu_items(self, row: DashboardRow) -> list[dict[str, t.Any]]:
        items = [
            {"label": "Open", "icon": "open_in_new", "action": "open"},
            {"label": "Duplicate", "icon": "content_copy", "action": "duplicate"},
        ]
        if self._app is not None and getattr(self._app, "_agent_session", None) is not None:
            items.append({"label": "Browse history", "icon": "history", "action": "history"})
        items.append({"label": "Dashboard settings", "icon": "settings", "action": "settings"})
        locked = self.home_locked()
        items.append(
            {
                "label": "Make home page",
                "icon": "home",
                "action": "home",
                "disabled": bool(locked) or self.home_dashboard() == row.id,
            }
        )
        return items

    def _render_log(self):
        ledger = self._ledger()
        entries = ledger.admin_log()[-8:] if ledger is not None else []
        if not entries:
            self._log.object = (
                '<div class="k"><span class="muted small">No admin actions yet.</span></div>'
            )
            return
        lines = []
        for entry in reversed(entries):
            titles = ", ".join(
                d.get("title") or d.get("id", "") for d in entry.get("dashboards") or []
            )
            what = titles or entry.get("component", "")
            extra = f" to {entry['owner']}" if entry.get("owner") else ""
            lines.append(
                f"<li><b>{esc(entry.get('user', '?'))}</b> {esc(entry.get('action', ''))} "
                f"{esc(what)}{esc(extra)} · {esc(_when(_parse_time(entry.get('when'))))}</li>"
            )
        self._log.object = f'<div class="k"><ul>{"".join(lines)}</ul></div>'

    def _build_pager(self):
        self._pager_text = caption("")
        self._prev = pmui.IconButton(
            icon="chevron_left", description="Previous page", size="small", margin=0
        )
        self._next = pmui.IconButton(
            icon="chevron_right", description="Next page", size="small", margin=0
        )
        self._prev.on_click(lambda _event: self.param.update(page=max(0, self.page - 1)))
        self._next.on_click(lambda _event: self.param.update(page=self.page + 1))
        return pn.Row(
            pn.layout.HSpacer(),
            caption(f"Rows per page: {PAGE_SIZE}"),
            self._pager_text,
            self._prev,
            self._next,
            margin=0,
            sizing_mode="stretch_width",
            styles={"padding": "8px 12px", "gap": "16px", "align-items": "center"},
        )

    # ------------------------------------------------------------------
    # Selection and the bulk bar
    # ------------------------------------------------------------------

    def _build_bulk_bar(self):
        self._bulk_count = pmui.Typography(
            "", variant="body2", margin=0, sx={"color": "primary.main", "fontWeight": 500}
        )

        def button(label, icon, action, color="primary"):
            widget = pmui.Button(
                label=label, icon=icon, variant="text", size="small", color=color, margin=0
            )
            widget.on_click(lambda _event: self.ask(action))
            return widget

        clear = pmui.IconButton(
            icon="close", description="Clear selection", size="small", margin=0
        )
        clear.on_click(lambda _event: self.clear_selection())
        self._bulk = pn.Row(
            self._bulk_count,
            button("Share", "share", "share"),
            button("Change owner", "manage_accounts", "owner"),
            button("Download YAML", "download", "download"),
            button("Prune history", "history", "prune"),
            button("Delete", "delete", "delete", color="error"),
            pn.layout.HSpacer(),
            clear,
            margin=0,
            visible=False,
            sizing_mode="stretch_width",
            styles={
                "padding": "8px 12px",
                "gap": "12px",
                "align-items": "center",
                "flex-wrap": "wrap",
                "background": "rgba(var(--mui-palette-primary-mainChannel) / 0.08)",
                "border-bottom": "1px solid var(--mui-palette-divider)",
            },
            css_classes=["flowdash-admin-bulk"],
        )

    def _sync_bulk(self):
        count = len(self.selection)
        self._bulk.visible = bool(count)
        self._bulk_count.object = f"{count} selected"

    def select(self, dashboard_id: str, selected: bool = True):
        """Add *dashboard_id* to the selection, or take it out."""
        if selected:
            self.selection.add(dashboard_id)
        else:
            self.selection.discard(dashboard_id)
        self._sync_bulk()
        shown = self._shown_ids()
        self._set_page_box(bool(shown) and all(i in self.selection for i in shown))

    def _set_page_box(self, value: bool):
        self._syncing = True
        try:
            self._select_page.value = value
        finally:
            self._syncing = False

    def _shown_ids(self) -> list[str]:
        visible = self.visible()
        return [r.id for r in visible[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]]

    def _on_select_page(self, event):
        if self._syncing:
            return
        ids = self._shown_ids()
        if event.new:
            self.selection |= set(ids)
        else:
            self.selection -= set(ids)
        self._render()

    def clear_selection(self):
        """Select nothing."""
        self.selection.clear()
        self._render()

    def _selected_rows(self) -> list[DashboardRow]:
        return [r for r in self._rows if r.id in self.selection]

    # ------------------------------------------------------------------
    # Confirmation dialog
    # ------------------------------------------------------------------

    def _build_dialog(self):
        self._pending: tuple[str, list[str]] | None = None
        self._dialog_text = cell("")
        self._share_choice = pmui.RadioBoxGroup(options={}, value=None, margin=0, inline=False)
        self._share_groups = pmui.MultiChoice(
            label="Groups and people", options=[], sizing_mode="stretch_width", margin=0
        )
        self._owner_input = pmui.AutocompleteInput(
            label="New owner",
            options=[],
            restrict=False,
            min_characters=0,
            sizing_mode="stretch_width",
            margin=0,
        )
        self._keep_input = pmui.IntInput(
            label="Versions to keep", value=20, start=1, width=180, margin=0
        )
        self._dialog_alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        cancel = pmui.Button(label="Cancel", variant="text", color="default", margin=0)
        cancel.on_click(lambda _event: self.cancel())
        self._confirm = pmui.Button(label="Confirm", variant="contained", margin=0)
        self._confirm.on_click(lambda _event: self.confirm())
        self._dialog = pmui.Dialog(
            pn.Column(
                self._dialog_alert,
                self._dialog_text,
                self._share_choice,
                self._share_groups,
                self._owner_input,
                self._keep_input,
                pn.Row(
                    pn.layout.HSpacer(),
                    cancel,
                    self._confirm,
                    margin=(8, 0, 0, 0),
                    sizing_mode="stretch_width",
                    styles={"gap": "8px"},
                ),
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px", "min-width": "420px"},
            ),
            title="",
            open=False,
            close_on_click=True,
            css_classes=["flowdash-admin-confirm"],
        )

    def _known_people(self) -> list[str]:
        people: set[str] = {r.model.owner for r in self._rows}
        app = self._app
        if app is not None:
            try:
                people |= set(app._usage_ledger().people())
            except Exception:
                pass
            people |= set(app._configured_admins())
            config = app._code_auth_config
            if config is not None:
                people |= set(config.user_groups)
        people.discard("anonymous")
        return sorted(people)

    def ask(self, action: str, dashboard_ids: t.Sequence[str] | None = None):
        """Open the confirmation of *action* (``share``, ``owner``, ``prune``, ``delete``)."""
        ids = list(dashboard_ids) if dashboard_ids is not None else sorted(self.selection)
        rows = [r for r in self._rows if r.id in ids]
        if not rows:
            return
        if action == "download":
            self.download(ids)
            return
        n = len(rows)
        noun = f"{n} dashboard{'' if n == 1 else 's'}"
        titles = ", ".join(r.model.title for r in rows[:5]) + (" and more" if n > 5 else "")
        self._pending = (action, ids)
        widgets = {
            self._share_choice: action == "share",
            self._share_groups: action == "share",
            self._owner_input: action == "owner",
            self._keep_input: action == "prune",
        }
        if action == "share":
            title, text, label, color = (
                f"Share {noun}",
                f"Who can view {esc(titles)}? Edit rights and exclusions stay as they are.",
                "Share",
                "primary",
            )
            options = {"Only the owner": "owner", "These groups and people": "names"}
            if self._default_allow():
                options = {"Everyone": "everyone", **options}
            names = sorted(
                set(self._app._known_groups() if self._app is not None else ())
                | set(self._known_people())
            )
            self._share_choice.param.update(options=options, value=next(iter(options.values())))
            self._share_groups.param.update(options=names, value=[])
        elif action == "owner":
            title, text, label, color = (
                f"Change the owner of {noun}",
                f"The new owner can rename, share and delete {esc(titles)}; the current owners "
                "lose those rights.",
                "Change owner",
                "primary",
            )
            self._owner_input.options = self._known_people()
            clear_text(self._owner_input)
        elif action == "prune":
            drop = sum(r.versions for r in rows)
            title, text, label, color = (
                f"Prune the history of {noun}",
                f"Keep only the latest versions of {esc(titles)} ({drop} versions now). Older "
                "versions are deleted and can't be restored.",
                "Prune history",
                "warning",
            )
        elif action == "delete":
            title, text, label, color = (
                f"Delete {noun}?",
                f"<b>{esc(titles)}</b> and {'its' if n == 1 else 'their'} history will be deleted "
                "for everyone. This can't be undone.",
                f"Delete {noun}",
                "error",
            )
        else:
            return
        with pn.io.hold():
            for widget, shown in widgets.items():
                widget.visible = shown
            self._dialog_alert.visible = False
            self._dialog_text.object = f'<div class="k"><span>{text}</span></div>'
            self._confirm.param.update(label=label, color=color)
            self._dialog.param.update(title=title, open=True)

    def cancel(self):
        """Close the confirmation without doing anything."""
        self._pending = None
        self._dialog.open = False

    def confirm(self) -> bool:
        """Carry out the action the dialog asks to confirm."""
        if self._pending is None:
            return False
        action, ids = self._pending
        if action == "share":
            choice = self._share_choice.value
            done = self.share(ids, choice, list(self._share_groups.value or []))
        elif action == "owner":
            owner = (self._owner_input.value_input or self._owner_input.value or "").strip()
            done = self.change_owner(ids, owner)
        elif action == "prune":
            done = self.prune(ids, int(self._keep_input.value or 1))
        elif action == "delete":
            done = self.delete(ids)
        else:
            done = False
        if done:
            self._pending = None
            self._dialog.open = False
        else:
            self._dialog_alert.param.update(object=self._alert.object, visible=True)
        return done

    # ------------------------------------------------------------------
    # Actions, each checked on the server
    # ------------------------------------------------------------------

    def _show_error(self, message: str):
        self._alert.param.update(object=message, visible=True)

    def _allowed(self) -> bool:
        if self._app is not None and self.editable and self._app._still_admin():
            return True
        self._show_error("Only admins can change other people's dashboards.")
        return False

    def _record(self, action: str, ids: t.Sequence[str], **details):
        ledger = self._ledger()
        if ledger is None:
            return
        titles = {r.id: r.model.title for r in self._rows}
        ledger.record(
            self._app._identity.user,
            action,
            [{"id": i, "title": titles.get(i, i)} for i in ids],
            **details,
        )

    def _done(self, message: str):
        self._alert.visible = False
        if self._app is not None:
            self._app._notify("success", message)
        self.refresh()

    def share(self, dashboard_ids: t.Sequence[str], who: str, names: t.Sequence[str] = ()) -> bool:
        """Set who can view the dashboards: ``everyone``, the ``owner`` only or ``names``."""
        if not self._allowed():
            return False
        if who == "names" and not names:
            self._show_error("Pick the groups or people to share with.")
            return False
        groups = set(self._app._known_groups())
        store = self._store()
        changed = []
        for did in dashboard_ids:
            model = store._load_any(did)
            if model is None:
                continue
            if who == "everyone":
                users, allowed_groups = frozenset(), frozenset()
            elif who == "owner":
                users, allowed_groups = frozenset({model.owner}), frozenset()
            else:
                allowed_groups = frozenset(n for n in names if n in groups)
                users = frozenset(n for n in names if n not in groups)
            permission = dataclasses.replace(
                model.permission, allow_users=users, allow_groups=allowed_groups
            )
            try:
                store.set_permission(did, permission)
            except Exception as exc:
                self._show_error(f"Could not share {model.title}: {error_text(exc)}")
                return False
            changed.append(did)
        self._record("shared", changed, access=who if who != "names" else sorted(names))
        self._done(f"Shared {len(changed)} dashboard{'' if len(changed) == 1 else 's'}.")
        return True

    def change_owner(self, dashboard_ids: t.Sequence[str], owner: str) -> bool:
        """Give the dashboards to *owner*."""
        if not self._allowed():
            return False
        owner = owner.strip()
        if not owner:
            self._show_error("Name the new owner.")
            return False
        store = self._store()
        changed = []
        for did in dashboard_ids:
            try:
                if store.set_owner(did, owner):
                    changed.append(did)
            except Exception as exc:
                self._show_error(f"Could not change the owner: {error_text(exc)}")
                return False
        self._record("gave", changed, owner=owner)
        self._done(f"{owner} now owns {len(changed)} dashboard{'' if len(changed) == 1 else 's'}.")
        return True

    def prune(self, dashboard_ids: t.Sequence[str], keep: int) -> bool:
        """Drop all but the latest *keep* versions of each dashboard's history."""
        if not self._allowed():
            return False
        keep = max(1, int(keep))
        store = self._store()
        dropped = 0
        for did in dashboard_ids:
            before = history_stats(store.load_history(did))[0]
            try:
                kept = store.rewrite_history(did, lambda records: compact(records, keep, None))
            except Exception as exc:
                self._show_error(f"Could not prune the history: {error_text(exc)}")
                return False
            dropped += before - history_stats(kept)[0]
        self._record("pruned the history of", dashboard_ids, kept=keep, dropped=dropped)
        self._done(f"Dropped {dropped} version{'' if dropped == 1 else 's'}.")
        return True

    def delete(self, dashboard_ids: t.Sequence[str]) -> bool:
        """Delete the dashboards and their history."""
        if not self._allowed():
            return False
        store = self._store()
        titles = {r.id: r.model.title for r in self._rows}
        deleted = []
        for did in dashboard_ids:
            owner = store.get_owner(did)
            if owner is None:
                continue
            try:
                if store.delete_dashboard(owner, did):
                    deleted.append(did)
            except Exception as exc:
                self._show_error(f"Could not delete {titles.get(did, did)}: {error_text(exc)}")
                break
        if deleted:
            self._record("deleted", deleted)
            ledger = self._ledger()
            if ledger is not None:
                ledger.forget_dashboards(deleted)
            self._forget_home(deleted, titles)
        self.selection -= set(deleted)
        if len(deleted) < len(dashboard_ids) and self._alert.visible:
            self.refresh()
            return False
        self._done(f"Deleted {len(deleted)} dashboard{'' if len(deleted) == 1 else 's'}.")
        return True

    def _forget_home(self, deleted: t.Sequence[str], titles: dict[str, str]):
        """Stop opening a deleted dashboard as the home page, unless code sets it."""
        if self.home_locked():
            return
        try:
            general = dict(current_section(self.project, "general") or {})
        except ProjectError:
            return
        home = general.get("home_dashboard")
        if home is None or not any(home in (did, titles.get(did)) for did in deleted):
            return
        general.pop("home_dashboard")
        try:
            self.project.set("general", general)
        except (ProjectError, OSError) as exc:
            logger.warning("Could not clear the deleted home page: %s", exc)
            return
        if self._app is not None:
            self._app.home_dashboard = None

    def download(self, dashboard_ids: t.Sequence[str]) -> bool:
        """Download the dashboards as YAML, several in a zip file."""
        if not self._allowed():
            return False
        from panel_flowdash.ui.launcher import export_filename, export_model, export_text

        store = self._store()
        files = []
        for did in dashboard_ids:
            model = store._load_any(did)
            if model is not None:
                restricted = self._app._editor._restricted_ids(model)
                hidden = [
                    e.to_dict() for e in model.edges if {e.source, e.target} & set(restricted)
                ]
                text = export_text(export_model(model, restricted), "yaml", hidden)
                files.append((export_filename(model.title, "yaml"), text))
        if not files:
            return False
        downloader = self._app._downloader
        if len(files) == 1:
            name, text = files[0]
            payload: io.IOBase = io.StringIO(text)
        else:
            buffer = io.BytesIO()
            used: set[str] = set()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for filename, text in files:
                    stem, n = filename, 2
                    while filename in used:
                        filename = stem.replace(".yaml", f" {n}.yaml")
                        n += 1
                    used.add(filename)
                    archive.writestr(filename, text)
            buffer.seek(0)
            name, payload = "dashboards.zip", buffer
        downloader.data = None
        downloader.param.update(callback=lambda: payload, filename=name)
        downloader.transfer(download=True)
        return True

    def run(self, action: str | None, dashboard_id: str):
        """Run the row menu's *action* on *dashboard_id*."""
        app = self._app
        if app is None or action is None:
            return
        path = f"/dash/{dashboard_id}"
        if action == "open":
            app._navigate_to(path)
        elif action == "history":
            app._navigate_to(path, "?edit=true&open=history")
        elif action == "settings":
            app._navigate_to(path, "?edit=true&open=settings")
        elif action == "duplicate":
            self.duplicate(dashboard_id)
        elif action == "home":
            self.make_home(dashboard_id)

    def duplicate(self, dashboard_id: str) -> str | None:
        """Copy a dashboard as "<title> copy", owned by the admin and not shared."""
        if not self._allowed():
            return None
        from panel_flowdash.ui.launcher import copy_dashboard, unique_title

        store = self._store()
        source = store._load_any(dashboard_id)
        if source is None:
            return None
        user = self._app._user_id
        try:
            model = copy_dashboard(
                store, source, user, unique_title(store, user, f"{source.title} copy")
            )
        except Exception as exc:
            self._show_error(f"Could not duplicate {source.title}: {error_text(exc)}")
            return None
        self._record("duplicated", [dashboard_id])
        self._done(f'Created "{model.title}".')
        return model.dashboard_id

    def make_home(self, dashboard_id: str) -> bool:
        """Make *dashboard_id* the app's home page, in the project file's ``general`` section."""
        if not self._allowed():
            return False
        locked = self.home_locked()
        if locked:
            self._show_error(f"The home page is set in code ({locked}).")
            return False
        try:
            general = dict(current_section(self.project, "general") or {})
            general["home_dashboard"] = dashboard_id
            self.project.set("general", general)
        except ProjectError as exc:
            self._show_error("\n\n".join(p.message for p in exc.problems))
            return False
        except OSError as exc:
            self._show_error(f"Could not write the project file: {error_text(exc)}")
            return False
        self._app.home_dashboard = dashboard_id
        self._record("made the home page", [dashboard_id])
        self._done("Home page changed.")
        return True

    def __panel__(self):
        """Render the section."""
        return self._layout


def dashboards_admin(project: Project, editable: bool) -> DashboardsAdmin:
    """Build the Dashboards section of the admin view."""
    return DashboardsAdmin(project, editable)


def _no_section(section):
    raise ProjectError([Problem("`dashboards` is not a project file section.")])


# Listed in the admin view, but not a section of the project file: it is not registered.
DASHBOARDS = ProjectSection(
    "dashboards",
    _no_section,
    title="Dashboards",
    icon="dashboard",
    description="Every dashboard in the store, whoever owns it.",
    settings_view=dashboards_admin,
    group="Content",
)
