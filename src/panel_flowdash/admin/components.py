"""The Components section of the admin view: every registered component, and who may place it.

It edits the project file's ``components`` section (see
`panel_flowdash.component_settings`). The table's switch turns a
component off or on at once; the drawer shows what the component is and
saves who may place it, and whether the agent needs approval to, with
Done.
"""

from __future__ import annotations

import copy
import fnmatch
import importlib.metadata
import pathlib
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin.form import current_app, current_section
from panel_flowdash.admin.kit import Table, caption, cell, chip, esc, lock
from panel_flowdash.component_settings import dump_components, load_components
from panel_flowdash.project import Problem, Project, ProjectError, ProjectSection, register_section
from panel_flowdash.ui.gallery import ROLE_FILTERS, GalleryEntry, gallery_entry, usage_index
from panel_flowdash.ui.palette import editor_presets
from panel_flowdash.value_text import error_text
from panel_flowdash.widgets import BUILTIN_COMPONENTS

if t.TYPE_CHECKING:
    from panel_flowdash.registry import RegistryEntry

FILTERS = {**ROLE_FILTERS, "off": "Off"}

_CHIP_SX = {"fontSize": "0.875rem"}

_ROW_CSS = """
.k .comp { display: flex; align-items: center; gap: 12px; min-width: 0; cursor: pointer; }
.k .comp .mi { font-family: 'Material Icons'; font-size: 20px; line-height: 1; flex: 0 0 auto;
  color: var(--mui-palette-primary-main); font-style: normal; font-weight: normal;
  -webkit-font-feature-settings: 'liga'; font-feature-settings: 'liga'; }
.k .comp .text { min-width: 0; }
.k .off { opacity: 0.6; }
.k a { color: var(--mui-palette-primary-main); text-decoration: none; }
"""

_DETAILS_CSS = """
.k dl { display: grid; grid-template-columns: 120px 1fr; gap: 10px 16px; margin: 0; }
.k dt { color: var(--mui-palette-text-secondary); font-size: 0.8125rem; letter-spacing: 0.25px; }
.k dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.k .preset { display: flex; flex-direction: column; padding: 8px 12px;
  border: 1px solid var(--mui-palette-divider); border-top: none; }
.k .preset:first-child { border-top: 1px solid var(--mui-palette-divider);
  border-radius: 4px 4px 0 0; }
.k .preset:last-child { border-radius: 0 0 4px 4px; }
.k .preset:only-child { border-radius: 4px; }
.k a { color: var(--mui-palette-primary-main); text-decoration: none; }
"""

_OVERLINE_SX = {
    "color": "text.secondary",
    "fontSize": "0.75rem",
    "fontWeight": 500,
    "letterSpacing": "1.2px",
    "lineHeight": 2,
}


def _icon(name: str) -> str:
    return f'<span class="mi" aria-hidden="true">{esc(name)}</span>'


def component_source(entry: RegistryEntry, project_dir: str | None = None) -> str:
    """Return where a component comes from: ``project``, ``built in`` or a library and its version."""
    if entry.app_id in BUILTIN_COMPONENTS:
        return "built in"
    path = entry.module_path
    if path is not None and project_dir is not None:
        try:
            pathlib.Path(path).resolve().relative_to(pathlib.Path(project_dir).resolve())
        except ValueError:
            pass
        else:
            return "project"
    package = (entry.module_name or "").split(".")[0]
    if not package:
        return "project"
    try:
        version = importlib.metadata.version(package.replace("_", "-"))
    except importlib.metadata.PackageNotFoundError:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return "project" if path is not None else package
    return f"{package} {version}"


def _ports(entry: GalleryEntry) -> str:
    if not entry.ports_known:
        return '<span class="muted" title="Known once the component is imported">Not loaded</span>'
    if not entry.inputs and not entry.outputs:
        return '<span class="muted">None</span>'
    return f"{len(entry.inputs)} in · {len(entry.outputs)} out"


def _size(entry: RegistryEntry) -> str:
    meta = entry.metadata
    bits = []
    default = meta.default_size or {}
    if default:
        width, height = (
            default.get("w") or default.get("width"),
            default.get("h") or default.get("height"),
        )
        bits.append(f"default {width or '?'} x {height or '?'}")
    minimum = meta.min_size or {}
    if minimum.get("height") or minimum.get("h"):
        bits.append(f"min height {minimum.get('height') or minimum.get('h')}")
    if meta.framed is not None:
        bits.append("framed" if meta.framed else "unframed")
    return ", ".join(str(b) for b in bits) or "Default"


class ComponentsSettings(Viewer):
    """The Components section: the table of registered components and a drawer for one of them."""

    editable = param.Boolean(default=True)

    filter = param.Selector(default="all", objects=list(FILTERS))

    query = param.String(default="")

    selected = param.String(default=None, allow_None=True, doc="The component the drawer shows.")

    def __init__(self, project: Project, editable: bool = True, **params):
        super().__init__(editable=editable, **params)
        self.project = project
        self._app = current_app()
        self._alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._search = pmui.TextInput(
            placeholder="Search by name, id, tag or port",
            size="small",
            width=280,
            margin=0,
        )
        self._search.param.watch(
            lambda event: setattr(self, "query", event.new or ""), "value_input"
        )
        self._chips = {
            key: pmui.Chip(
                label=label, size="medium", margin=0, sx=_CHIP_SX, css_classes=[f"filter-{key}"]
            )
            for key, label in FILTERS.items()
        }
        for key, widget in self._chips.items():
            widget.on_click(partial(self._set_filter, key))
        toolbar = pn.Row(
            self._search,
            *self._chips.values(),
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "padding": "10px 12px",
                "align-items": "center",
                "gap": "8px",
                "flex-wrap": "wrap",
                "border-bottom": "1px solid var(--mui-palette-divider)",
            },
        )
        self._table = Table(
            [
                ("On", "64px"),
                ("Component", "2.4fr"),
                ("Group", "0.9fr"),
                ("Tags", "1.3fr"),
                ("Source", "1.1fr"),
                ("Ports", "0.9fr"),
                ("Used in", "0.7fr"),
                ("Presets", "0.6fr"),
                ("Access", "1.1fr"),
            ],
            toolbar=toolbar,
            footer=caption(
                "Off hides a component from the component list and the agent; dashboards "
                "using it keep working, and their editors are told.",
                margin=(12, 12),
            ),
            empty="No components match.",
            css_classes=["flowdash-admin-components"],
        )
        self._build_drawer()
        self._layout = pn.Column(
            self._alert,
            self._table,
            self._drawer,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-components-section"],
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def settings(self) -> dict[str, dict[str, t.Any]]:
        """Return the ``components`` section as the file has it now."""
        try:
            return copy.deepcopy(dict(current_section(self.project, "components") or {}))
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return {}

    def _entries(self) -> dict[str, RegistryEntry]:
        if self._app is None:
            return {}
        return dict(self._app._component_entries)

    def _dashboards(self) -> list:
        if self._app is None or self._app.store is None:
            return []
        try:
            return self._app.store._all_dashboards()
        except Exception:
            return []

    def _sensitive_patterns(self) -> list[tuple[str, str]]:
        """Return the sensitive component patterns set elsewhere, each with where it is set."""
        app = self._app
        if app is None:
            return []
        where = app._code_setting_source("agent_sensitive_components") or "set in code"
        patterns = [(p, where) for p in app.agent_sensitive_components]
        permissions = app._project_section("permissions")
        patterns += [
            (p, "under Permissions") for p in permissions.get("sensitive_components") or ()
        ]
        return patterns

    def sensitive_lock(self, component_id: str) -> str | None:
        """Return where a pattern marking *component_id* sensitive is set, outside this section."""
        entry = self._entries().get(component_id)
        ids = {component_id, *(entry.metadata.aliases if entry is not None else ())}
        for pattern, where in self._sensitive_patterns():
            if any(fnmatch.fnmatchcase(cid, pattern) for cid in ids):
                return f"{pattern} {where}"
        return None

    def rows(self) -> list[dict[str, t.Any]]:
        """Return what the table shows for each registered component, in display order."""
        entries = self._entries()
        settings = self.settings()
        usage = usage_index(self._dashboards(), entries)
        specs = self._app._editor._component_specs if self._app is not None else {}
        project_dir = getattr(self._app, "project_dir", None)
        rows = []
        for cid, entry in entries.items():
            described = gallery_entry(entry, specs.get(cid), used_in=usage.get(cid, ()))
            own = settings.get(cid) or {}
            rows.append(
                {
                    "id": cid,
                    "entry": entry,
                    "described": described,
                    "enabled": own.get("enabled", True) is not False,
                    "placed_by": list(own.get("placed_by") or []),
                    "approval": own.get("approval") is True,
                    "source": component_source(entry, project_dir),
                    "used_in": described.used_in,
                    "presets": self._presets(entry),
                }
            )
        order = list(ROLE_FILTERS)
        rows.sort(
            key=lambda r: (
                order.index(r["described"].role) if r["described"].role in order else len(order),
                r["described"].title.casefold(),
            )
        )
        return rows

    def _presets(self, entry: RegistryEntry) -> list[dict[str, t.Any]] | None:
        """Return the component's palette presets, or None while that needs an import."""
        meta = entry.metadata
        if meta.palette_presets is None and not meta.presets_pending:
            return []
        if entry.app is None or self._app is None:
            return None
        try:
            return editor_presets(self._app._editor, entry)
        except Exception:
            return None

    def counts(self, rows: t.Sequence[dict[str, t.Any]]) -> dict[str, int]:
        """Return how many rows each filter chip shows."""
        counts = dict.fromkeys(FILTERS, 0)
        counts["all"] = len(rows)
        for row in rows:
            role = row["described"].role
            if role in counts:
                counts[role] += 1
            if not row["enabled"]:
                counts["off"] += 1
        return counts

    def _passes(self, row: dict[str, t.Any]) -> bool:
        if self.filter == "off" and row["enabled"]:
            return False
        if self.filter not in ("all", "off") and row["described"].role != self.filter:
            return False
        words = self.query.lower().split()
        haystack = f"{row['id']} {' '.join(row['entry'].metadata.aliases)}".lower()
        return all(word in haystack for word in words) or row["described"].matches(self.query)

    def visible_rows(self) -> list[dict[str, t.Any]]:
        """Return the rows passing the search and the filter chip."""
        return [row for row in self.rows() if self._passes(row)]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _set_filter(self, key: str, *_args):
        self.filter = key

    @param.depends("filter", "query", watch=True)
    def refresh(self):
        """Draw the chips and the table from the registry and the project file."""
        rows = self.rows()
        counts = self.counts(rows)
        with pn.io.hold():
            for key, widget in self._chips.items():
                active = key == self.filter
                widget.param.update(
                    label=f"{FILTERS[key]} {counts[key]}",
                    icon="check" if active else None,
                    variant="filled" if active else "outlined",
                    color="primary" if active else "default",
                    visible=key == "all" or counts[key] > 0 or active,
                )
            self._table.set_rows(
                [(row["id"], self._cells(row)) for row in rows if self._passes(row)]
            )

    def _cells(self, row: dict[str, t.Any]) -> list:
        cid, described = row["id"], row["described"]
        switch = pmui.Switch(
            value=row["enabled"],
            margin=0,
            disabled=not self.editable,
        )
        switch.param.watch(lambda event, cid=cid: self.set_enabled(cid, event.new), "value")
        title = cell(
            f'<div class="comp{"" if row["enabled"] else " off"}">{_icon(described.display_icon)}'
            f'<span class="text"><span class="main">{esc(described.title)}</span>'
            f'<span class="mono">{esc(cid)}</span></span></div>',
            stylesheets=[_ROW_CSS],
        )
        open_drawer = pmui.Clickable(object=title, sizing_mode="stretch_width", margin=0)
        open_drawer.on_click(lambda *_args, cid=cid: self.open_component(cid))
        tags = "".join(chip(tag) for tag in described.tags) or '<span class="muted">None</span>'
        presets = row["presets"]
        presets_text = (
            '<span class="muted">Yes</span>'
            if presets is None
            else (str(len(presets)) if presets else '<span class="muted">None</span>')
        )
        used = len(row["used_in"])
        access = (
            "".join(chip(g, "outlined") for g in row["placed_by"])
            if row["placed_by"]
            else '<span class="muted">Everyone</span>'
        )
        if row["approval"] or self.sensitive_lock(cid):
            access += chip("Approval", "warning")
        return [
            switch,
            open_drawer,
            cell(f'<span class="main">{esc(described.role_name)}</span>'),
            cell(tags),
            cell(f'<span class="main muted">{esc(row["source"])}</span>'),
            cell(f'<span class="main">{_ports(described)}</span>'),
            cell(f'<span class="main">{used if used else "-"}</span>'),
            cell(f'<span class="main">{presets_text}</span>'),
            cell(access),
        ]

    # ------------------------------------------------------------------
    # Writing the section
    # ------------------------------------------------------------------

    def _write(self, settings: dict[str, dict[str, t.Any]]) -> bool:
        if not self.editable:
            return False
        try:
            self.project.set("components", settings)
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return False
        except OSError as exc:
            self._show_problems([Problem(f"Could not write the project file: {error_text(exc)}")])
            return False
        self._alert.visible = False
        return True

    def _show_problems(self, problems: t.Sequence[Problem]):
        self._alert.param.update(
            object="\n\n".join(p.message for p in problems), visible=bool(problems)
        )

    def _record(self, action: str, component_id: str):
        app = self._app
        if app is None:
            return
        try:
            app._usage_ledger().record(app._identity.user, action, component=component_id)
        except Exception:
            pass

    def set_enabled(self, component_id: str, enabled: bool) -> bool:
        """Turn *component_id* on or off for everyone, writing the project file at once."""
        if self._app is not None and not self._app._still_admin():
            self._show_problems([Problem("Only admins can turn components on or off.")])
            self.refresh()
            return False
        settings = self.settings()
        own = settings.setdefault(component_id, {})
        if enabled:
            own.pop("enabled", None)
        else:
            own["enabled"] = False
        if not self._write(settings):
            self.refresh()
            return False
        self._record("turned on" if enabled else "turned off", component_id)
        self.refresh()
        if self.selected == component_id:
            self._render_drawer()
        return True

    def set_access(
        self, component_id: str, placed_by: t.Sequence[str], approval: bool | None = None
    ) -> bool:
        """Save who may place *component_id* and whether the agent needs approval to."""
        if self._app is not None and not self._app._still_admin():
            self._drawer_alert.param.update(
                object="Only admins can change who may place a component.", visible=True
            )
            return False
        settings = self.settings()
        own = settings.setdefault(component_id, {})
        groups = sorted(dict.fromkeys(g.strip() for g in placed_by if g.strip()))
        if groups:
            own["placed_by"] = groups
        else:
            own.pop("placed_by", None)
        if approval is not None:
            if approval:
                own["approval"] = True
            else:
                own.pop("approval", None)
        if not self._write(settings):
            self._drawer_alert.param.update(object=self._alert.object, visible=True)
            return False
        self.refresh()
        return True

    # ------------------------------------------------------------------
    # The drawer
    # ------------------------------------------------------------------

    def _build_drawer(self):
        self._drawer_icon = cell("", stylesheets=[_ROW_CSS], sizing_mode=None, width=28)
        self._drawer_title = pmui.Typography("", variant="h6", margin=0, sx={"fontWeight": 400})
        self._drawer_subtitle = caption("")
        close = pmui.IconButton(icon="close", description="Close", size="small", margin=0)
        close.on_click(lambda _event: self.close_component())
        self._drawer_alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._details = cell("", stylesheets=[_DETAILS_CSS])
        self._presets_list = cell("", stylesheets=[_DETAILS_CSS])
        self._placed_by = pmui.MultiChoice(
            label="Who can place it",
            placeholder="Everyone",
            options=[],
            sizing_mode="stretch_width",
            margin=0,
        )
        self._approval = pmui.Switch(
            label="Agent needs approval to place it", value=False, margin=0
        )
        self._approval_note = cell("")
        self._code_rules = cell("")
        open_list = pmui.Button(
            label="Open in the component list", variant="outlined", color="default", margin=0
        )
        open_list.on_click(lambda _event: self._open_list())
        done = pmui.Button(label="Done", variant="contained", margin=0)
        done.on_click(lambda _event: self.done())
        overline = lambda text: pmui.Typography(
            text, variant="overline", sx=_OVERLINE_SX, margin=(12, 0, 0, 0)
        )
        head = pn.Row(
            self._drawer_icon,
            pn.Column(
                self._drawer_title, self._drawer_subtitle, margin=0, sizing_mode="stretch_width"
            ),
            close,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "align-items": "flex-start",
                "gap": "8px",
                "padding": "20px 20px 12px 20px",
                "box-sizing": "border-box",
                "flex": "0 0 auto",
            },
        )
        fields = pn.Column(
            self._drawer_alert,
            self._details,
            overline("Presets in the component list"),
            self._presets_list,
            overline("Access"),
            self._placed_by,
            self._code_rules,
            self._approval,
            self._approval_note,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "gap": "12px",
                "padding": "4px 20px 20px 20px",
                "overflow-y": "auto",
                "flex": "1 1 auto",
                "min-height": "0",
                "box-sizing": "border-box",
            },
        )
        foot = pn.Row(
            open_list,
            pn.layout.HSpacer(),
            done,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "gap": "8px",
                "border-top": "1px solid var(--mui-palette-divider)",
                "padding": "16px 20px",
                "box-sizing": "border-box",
                "flex": "0 0 auto",
            },
        )
        body = pn.Column(
            head,
            fields,
            foot,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "height": "100vh",
                "display": "flex",
                "flex-direction": "column",
                "box-sizing": "border-box",
            },
            css_classes=["flowdash-admin-component-drawer"],
        )
        self._drawer = pmui.Drawer(body, anchor="right", variant="temporary", size=460, open=False)
        self._drawer.param.watch(self._on_drawer_open, "open")

    def _on_drawer_open(self, event):
        if not event.new:
            self.selected = None

    def open_component(self, component_id: str):
        """Open the drawer on *component_id*."""
        if component_id not in self._entries():
            return
        self.selected = component_id
        self._load_presets(component_id)
        self._render_drawer()
        self._drawer.open = True

    def _load_presets(self, component_id: str):
        entry = self._entries().get(component_id)
        if entry is None or self._app is None or entry.app is not None:
            return
        if entry.metadata.palette_presets is None and not entry.metadata.presets_pending:
            return
        try:
            self._app._editor.ensure_components_loaded([component_id])
        except Exception:
            pass

    def _render_drawer(self):
        row = next((r for r in self.rows() if r["id"] == self.selected), None)
        if row is None:
            return
        cid, entry, described = row["id"], row["entry"], row["described"]
        used = row["used_in"]
        status = "enabled" if row["enabled"] else "turned off"
        where = (
            f"in {len(used)} dashboard{'' if len(used) == 1 else 's'}" if used else "not used yet"
        )
        tags = "".join(chip(t) for t in described.tags) or '<span class="muted">None</span>'
        inputs = (
            "".join(
                chip(
                    " · ".join(
                        b
                        for b in (p.label or p.name, p.type, "required" if p.required else "")
                        if b
                    ),
                    "outlined",
                )
                for p in described.inputs
            )
            if described.ports_known
            else '<span class="muted">Shown once the component is imported</span>'
        ) or '<span class="muted">None</span>'
        aliases = "".join(f"<code>{esc(a)}</code> " for a in entry.metadata.aliases) or (
            '<span class="muted">None</span>'
        )
        used_links = (
            ", ".join(f'<a href="{esc(ref.path)}?edit=true">{esc(ref.title)}</a>' for ref in used)
            or '<span class="muted">No dashboard yet</span>'
        )
        details = [
            ("Id", f"<code>{esc(cid)}</code>"),
            ("Source", esc(row["source"])),
            ("Group, tags", f"{chip(described.role_name, 'primary')}{tags}"),
            ("Inputs", inputs),
            ("Size", esc(_size(entry))),
            ("Aliases", aliases),
            ("Used in", used_links),
        ]
        description = (
            f'<p style="margin:0 0 12px 0">{esc(described.description)}</p>'
            if described.description
            else ""
        )
        presets = row["presets"]
        if presets is None:
            presets_html = '<span class="muted">Shown once the component is imported.</span>'
        elif not presets:
            presets_html = '<span class="muted">No presets.</span>'
        else:
            presets_html = (
                "<div>"
                + "".join(
                    f'<div class="preset"><span class="main">{esc(p.get("title", ""))}</span>'
                    f'<span class="muted small">{esc(p.get("description") or "")}</span></div>'
                    for p in presets
                )
                + "</div>"
            )
        meta = entry.metadata
        code_rules = [
            f"{label} {', '.join(values)}"
            for label, values in (
                ("Users", meta.allow_users),
                ("Groups", meta.allow_groups),
                ("Not users", meta.deny_users),
                ("Not groups", meta.deny_groups),
            )
            if values
        ]
        if meta.authorize is not None:
            code_rules.append("an authorize callback")
        groups = sorted(
            set(self._app._known_groups() if self._app is not None else ()) | set(row["placed_by"])
        )
        sensitive = self.sensitive_lock(cid)
        with pn.io.hold():
            self._drawer_alert.visible = False
            self._drawer_icon.object = (
                f'<div class="k"><div class="comp">{_icon(described.display_icon)}</div></div>'
            )
            self._drawer_title.object = described.title
            self._drawer_subtitle.object = f"{described.role_name} · {status} · {where}"
            self._details.object = (
                f'<div class="k">{description}<dl>'
                + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in details)
                + "</dl></div>"
            )
            self._presets_list.object = f'<div class="k">{presets_html}</div>'
            self._placed_by.param.update(
                options=groups, value=list(row["placed_by"]), disabled=not self.editable
            )
            self._code_rules.param.update(
                object=(
                    f'<div class="k"><span class="muted small">Also limited in code to '
                    f"{esc('; '.join(code_rules))}.</span></div>"
                    if code_rules
                    else ""
                ),
                visible=bool(code_rules),
            )
            self._approval.param.update(
                value=bool(row["approval"] or sensitive),
                disabled=not self.editable or bool(sensitive),
            )
            self._approval_note.param.update(
                object=(
                    f'<div class="k">{lock("Always", sensitive)} '
                    f'<span class="muted small">Matched by {esc(sensitive)}.</span></div>'
                    if sensitive
                    else '<div class="k"><span class="muted small">Adds it to '
                    "<code>agent_sensitive_components</code>.</span></div>"
                ),
            )

    def done(self) -> bool:
        """Save the drawer's access settings, if they changed, and close it."""
        cid = self.selected
        if cid is None:
            self._drawer.open = False
            return True
        row = next((r for r in self.rows() if r["id"] == cid), None)
        placed_by = list(self._placed_by.value or [])
        approval = None if self.sensitive_lock(cid) else bool(self._approval.value)
        changed = row is not None and (
            sorted(placed_by) != sorted(row["placed_by"])
            or (approval is not None and approval != row["approval"])
        )
        if changed and not self.set_access(cid, placed_by, approval):
            return False
        self.close_component()
        return True

    def close_component(self):
        """Close the drawer without saving."""
        self._drawer.open = False
        self.selected = None

    def _open_list(self):
        if self._app is not None:
            self._app._navigate_to("/components")

    def __panel__(self):
        """Render the section."""
        return self._layout


def components_settings(project: Project, editable: bool) -> ComponentsSettings:
    """Build the Components section of the admin view."""
    return ComponentsSettings(project, editable)


COMPONENTS = ProjectSection(
    "components",
    load_components,
    title="Components",
    icon="widgets",
    description=(
        "Everything people can place on a dashboard: the project's own, the built-in ones "
        "and installed libraries."
    ),
    dump=dump_components,
    settings_view=components_settings,
    group="Content",
)

register_section(COMPONENTS)
