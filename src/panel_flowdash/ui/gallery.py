"""The component gallery at ``/components``: searchable cards and a detail panel."""

from __future__ import annotations

import inspect
import pathlib
import typing as t
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from html import escape

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.component_spec import ComponentSpec, _ports_from_metadata
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.port_types import normalize_port_type
from panel_flowdash.ui.roles import ROLE_ICONS, ROLES, component_role, is_builtin
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.registry import RegistryEntry

DESCRIPTION = "Building blocks for dashboards. Outputs of one connect to inputs of another."

ROLE_FILTERS = {
    "all": "All",
    "source": "Data",
    "transform": "Transforms",
    "view": "Views",
    "widget": "Widgets",
}

ROLE_NAMES = {
    "source": "Data",
    "transform": "Transform",
    "view": "View",
    "widget": "Widget",
    "other": "Component",
}

_NEW_DASHBOARD = {"label": "New dashboard", "icon": "add", "dashboard_id": None}

# The dashboard routes' prefix; not imported from the app, which imports this module.
_DASH_ROUTE_PREFIX = "/dash/"

_ICON_CSS = """
.mi {
  font-family: 'Material Icons';
  font-weight: normal;
  font-style: normal;
  font-size: 18px;
  line-height: 1;
  letter-spacing: normal;
  text-transform: none;
  display: inline-block;
  white-space: nowrap;
  direction: ltr;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: 'liga';
}
"""

_BADGE_CSS = """
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 20px;
  white-space: nowrap;
  background: var(--mui-palette-action-selected);
  color: var(--mui-palette-text-primary);
}
.badge.out {
  background: rgba(var(--mui-palette-primary-mainChannel) / 0.14);
}
"""

_CARD_CSS = (
    _ICON_CSS
    + _BADGE_CSS
    + """
:host { display: block; width: 100%; }
.card { cursor: pointer; color: var(--mui-palette-text-primary); }
.preview {
  height: 96px;
  display: grid;
  place-items: center;
  border-bottom: 1px solid var(--mui-palette-divider);
  background: var(--mui-palette-action-hover);
  color: var(--mui-palette-text-secondary);
}
.preview .mi { font-size: 36px; }
.body { padding: 12px 16px 14px; min-width: 0; }
.head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.head .mi { color: var(--mui-palette-primary-main); }
.title {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  font-weight: 500;
  line-height: 22px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.role { flex: none; font-size: 12px; color: var(--mui-palette-text-secondary); }
.description {
  margin: 4px 0 8px;
  font-size: 13px;
  line-height: 20px;
  color: var(--mui-palette-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.usage { margin-top: 10px; font-size: 12px; color: var(--mui-palette-text-secondary); }
"""
)

_PAPER_CSS = """
:host { position: relative; }
:host .MuiPaper-root { transition: box-shadow 0.15s, border-color 0.15s; overflow: hidden; }
:host(:hover) .MuiPaper-root {
  box-shadow: 0 4px 8px 3px rgba(0, 0, 0, 0.1), 0 1px 3px rgba(0, 0, 0, 0.14);
}
:host(.selected) .MuiPaper-root {
  border-color: var(--mui-palette-primary-main);
  box-shadow: 0 0 0 1px var(--mui-palette-primary-main);
}
"""

_GRID_CSS = """
:host {
  flex: none !important;
  height: auto !important;
  max-height: none !important;
  display: grid !important;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
  align-items: start;
}
/* Column's scroll button would take a grid cell. */
.scroll-button { display: none !important; }
"""

_MAIN_CSS = """
:host { box-sizing: border-box; padding: 24px 32px 64px; overflow-y: auto; min-height: 0; min-width: 0; }
@media (max-width: 600px) {
  :host { padding: 16px 16px 48px; }
}
"""

# Beside the cards on wide screens, over the whole page on narrow ones.
_DETAIL_CSS = """
:host { flex: 0 0 400px !important; width: 400px !important; height: 100%; }
:host .MuiPaper-root { height: 100%; overflow-y: auto; box-sizing: border-box; }
@media (max-width: 900px) {
  :host {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 1300;
  }
}
"""

_PORTS_CSS = (
    _ICON_CSS
    + """
:host { display: block; width: 100%; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left;
  font-weight: 500;
  font-size: 12px;
  color: var(--mui-palette-text-secondary);
  padding: 0 8px 6px 0;
  border-bottom: 1px solid var(--mui-palette-divider);
}
td {
  vertical-align: top;
  padding: 8px 8px 8px 0;
  border-bottom: 1px solid var(--mui-palette-divider);
  color: var(--mui-palette-text-primary);
}
td .mi { font-size: 16px; color: var(--mui-palette-text-secondary); vertical-align: -3px; }
td.name { font-weight: 500; white-space: nowrap; }
.muted { color: var(--mui-palette-text-secondary); }
"""
)

_DETAIL_ICON_CSS = (
    _ICON_CSS
    + """
.box {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: var(--mui-palette-primary-main);
  background: rgba(var(--mui-palette-primary-mainChannel) / 0.12);
}
.box .mi { font-size: 22px; }
"""
)

_SECTION_SX = {"px": 2.5, "py": 2, "borderBottom": 1, "borderColor": "divider"}

_CHIP_SX = {"fontSize": "0.875rem"}


@dataclass(frozen=True)
class GalleryPort:
    """A component port as the gallery lists it."""

    name: str
    type: str | None = None
    label: str | None = None
    # None for outputs.
    required: bool | None = None


@dataclass(frozen=True)
class DashboardRef:
    """A dashboard a component is placed in."""

    dashboard_id: str
    title: str

    @property
    def path(self) -> str:
        return f"{_DASH_ROUTE_PREFIX}{self.dashboard_id}"


@dataclass(frozen=True)
class GalleryEntry:
    """What the gallery shows and searches for one component."""

    component_id: str
    title: str
    role: str
    icon: str | None = None
    description: str | None = None
    module: str | None = None
    tags: tuple[str, ...] = ()
    inputs: tuple[GalleryPort, ...] = ()
    outputs: tuple[GalleryPort, ...] = ()
    ports_known: bool = True
    builtin: bool = False
    page_path: str | None = None
    used_in: tuple[DashboardRef, ...] = ()

    @property
    def role_name(self) -> str:
        return ROLE_NAMES[self.role]

    @property
    def display_icon(self) -> str:
        return self.icon or ROLE_ICONS[self.role]

    @property
    def usage(self) -> str:
        """The usage line of a card, e.g. "In 3 dashboards" or "Built in"."""
        count = len(self.used_in)
        if self.builtin:
            return "Built in" if not count else f"Built in, in {_dashboards(count)}"
        return f"In {_dashboards(count)}" if count else "Not used yet"

    def input_types(self) -> set[str]:
        return {normalize_port_type(p.type) for p in self.inputs if p.type} - {None}

    def accepts(self, port_type: str | None) -> bool:
        """Whether one of the inputs takes *port_type* (any type when None)."""
        return port_type is None or normalize_port_type(port_type) in self.input_types()

    def matches(self, query: str) -> bool:
        """Whether every word of *query* occurs in the title, description, tags or ports."""
        words = query.lower().split()
        if not words:
            return True
        haystack = " ".join(
            [
                self.title,
                self.description or "",
                *self.tags,
                *(
                    f"{p.name} {p.label or ''} {p.type or ''}"
                    for p in (*self.inputs, *self.outputs)
                ),
            ]
        ).lower()
        return all(word in haystack for word in words)


def _dashboards(count: int) -> str:
    return f"{count} dashboard{'' if count == 1 else 's'}"


def _ports(spec: ComponentSpec | None, entry: RegistryEntry):
    if spec is None:
        outputs, inputs = _ports_from_metadata(entry.metadata)
    else:
        outputs, inputs = spec.outputs, spec.inputs
    return (
        tuple(GalleryPort(p.name, p.type, p.label, p.required) for p in inputs),
        tuple(GalleryPort(p.name, p.type, p.label) for p in outputs),
    )


def _module_label(entry: RegistryEntry) -> str | None:
    """Return the component's file within its project section, else its module name."""
    path = entry.module_path
    if path is not None:
        path = pathlib.Path(path)
        return f"{path.parent.name}/{path.name}"
    return entry.module_name or None


def _description(entry: RegistryEntry, spec: ComponentSpec | None) -> str | None:
    """Return the declared description, else the first paragraph of the docstring."""
    description = entry.metadata.description or (spec.description if spec else None)
    if description or entry.app is None:
        return description
    # The component's own docstring only: an inherited one describes a base class.
    doc = getattr(entry.app, "__doc__", None)
    if not isinstance(doc, str) or not doc.strip():
        return None
    return " ".join(inspect.cleandoc(doc).split("\n\n")[0].split())


def gallery_entry(
    entry: RegistryEntry,
    spec: ComponentSpec | None = None,
    *,
    page_path: str | None = None,
    used_in: Iterable[DashboardRef] = (),
) -> GalleryEntry:
    """Describe *entry* for the gallery.

    Until the component is imported (*spec* is None) the ports come from the
    ``provides``/``requires`` declared on ``@register``.
    """
    meta = entry.metadata
    ports_known = spec is not None or bool(meta.provides or meta.requires)
    inputs, outputs = _ports(spec, entry)
    return GalleryEntry(
        component_id=entry.app_id,
        title=entry.title,
        role=component_role(entry, inputs, outputs, ports_known=ports_known),
        icon=meta.icon,
        description=_description(entry, spec),
        module=_module_label(entry),
        tags=tuple(str(tag) for tag in meta.tags),
        inputs=inputs,
        outputs=outputs,
        ports_known=ports_known,
        builtin=is_builtin(entry),
        page_path=page_path,
        used_in=tuple(used_in),
    )


def usage_index(
    dashboards: Iterable[DashboardModel], entries: Mapping[str, RegistryEntry]
) -> dict[str, list[DashboardRef]]:
    """Map component ids to the dashboards placing them, from the stored models alone.

    Items saved under a component's former id (its ``aliases``) count for it.
    """
    aliases = {alias: cid for cid, entry in entries.items() for alias in entry.metadata.aliases}
    usage: dict[str, list[DashboardRef]] = {}
    for model in dashboards:
        ref = DashboardRef(model.dashboard_id, model.title)
        for cid in dict.fromkeys(aliases.get(i.component_id, i.component_id) for i in model.items):
            usage.setdefault(cid, []).append(ref)
    return usage


def _icon(name: str) -> str:
    return f'<span class="mi" aria-hidden="true">{escape(name)}</span>'


def _badges(entry: GalleryEntry) -> str:
    badges = [f'<span class="badge">in: {escape(p.label or p.name)}</span>' for p in entry.inputs]
    badges += [
        f'<span class="badge out">out: {escape(p.label or p.name)}</span>' for p in entry.outputs
    ]
    return f'<div class="badges">{"".join(badges)}</div>' if badges else ""


def card_html(entry: GalleryEntry) -> str:
    """Render a component card: a static icon preview, title, role, ports and usage."""
    description = escape(entry.description or "")
    return (
        f'<div class="card"><div class="preview">{_icon(entry.display_icon)}</div>'
        '<div class="body"><div class="head">'
        f'{_icon(entry.display_icon)}<span class="title">{escape(entry.title)}</span>'
        f'<span class="role">{escape(entry.role_name)}</span></div>'
        f'<div class="description" title="{description}">{description or "&nbsp;"}</div>'
        f'{_badges(entry)}<div class="usage">{escape(entry.usage)}</div></div></div>'
    )


def ports_html(entry: GalleryEntry) -> str:
    """Render the detail panel's ports table; the label column only when a port has one."""
    if not entry.inputs and not entry.outputs:
        message = "No ports" if entry.ports_known else "Ports are listed once it has loaded"
        return f'<div class="muted">{message}</div>'
    ports = [("input", p) for p in entry.inputs] + [("output", p) for p in entry.outputs]
    labelled = any(port.label for _, port in ports)
    rows = []
    for direction, port in ports:
        if port.required is None:
            required = "&ndash;"
        else:
            required = "Required" if port.required else "Optional"
        label = f'<td class="muted">{escape(port.label or "")}</td>' if labelled else ""
        rows.append(
            "<tr>"
            f'<td class="name" title="{direction}">{_icon(direction)} {escape(port.name)}</td>'
            f"<td>{escape(port.type or 'any')}</td><td>{required}</td>{label}</tr>"
        )
    label_head = "<th>Label</th>" if labelled else ""
    return (
        f"<table><thead><tr><th>Port</th><th>Type</th><th>Required</th>{label_head}</tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )


class ComponentGallery(Viewer):
    """Cards for every component the user may place, with filters and a detail panel.

    Restricted components are left out, as in the editor palette. Usage counts
    come from one listing of the dashboards the user can access, without
    importing anything; :meth:`load` imports the components that do not
    declare their ports, off the event loop, so their ports and roles can be
    shown.
    """

    accepts = param.String(default=None, allow_None=True, doc="Only components taking this type.")

    query = param.String(default="", doc="Search text.")

    role = param.Selector(default="all", objects=list(ROLE_FILTERS), doc="Role filter.")

    selected = param.String(default=None, allow_None=True, doc="Component shown in the details.")

    def __init__(self, app: FlowDashApp, **params):
        super().__init__(**params)
        self._app = app
        self._entries: dict[str, GalleryEntry] = {}
        self._cards: dict[str, pmui.Paper] = {}
        self._editable: list[DashboardModel] = []

        self.search = pmui.TextInput(
            placeholder="Search by name, description, tag or port",
            variant="outlined",
            size="small",
            sizing_mode="stretch_width",
            min_width=200,
            max_width=340,
            margin=0,
            stylesheets=[":host { flex: 1 1 240px; }"],
            sx={"& .MuiOutlinedInput-root": {"borderRadius": 2, "bgcolor": "action.hover"}},
        )
        self.search.param.watch(self._on_search, "value_input")
        self.role_chips = {
            role: pmui.Chip(
                label=label, size="medium", margin=0, sx=_CHIP_SX, css_classes=[f"role-{role}"]
            )
            for role, label in ROLE_FILTERS.items()
        }
        for role, chip in self.role_chips.items():
            chip.on_click(partial(self._set_role, role))
        self.accepts_menu = pmui.MenuButton(
            label="Accepts",
            icon="input",
            variant="outlined",
            color="default",
            margin=0,
            sx={"textTransform": "none", "borderRadius": 4, "color": "text.primary"},
        )
        self.accepts_menu.on_click(self._on_accepts)
        self._empty = pmui.Typography(
            "No components match.",
            variant="body2",
            sx={"color": "text.secondary"},
            margin=(16, 0),
            visible=False,
        )
        self._grid = pn.Column(margin=0, sizing_mode="stretch_width", stylesheets=[_GRID_CSS])
        header = pn.Column(
            pmui.Typography("Components", variant="h4", margin=0, sx={"fontWeight": 400}),
            pmui.Typography(
                DESCRIPTION, variant="body1", margin=(4, 0, 0, 0), sx={"color": "text.secondary"}
            ),
            margin=(0, 0, 20, 0),
        )
        toolbar = pmui.Row(
            self.search,
            *self.role_chips.values(),
            self.accepts_menu,
            margin=(0, 0, 20, 0),
            sx={"flexWrap": "wrap", "gap": 1, "alignItems": "center"},
        )
        self._main = pn.Column(
            header,
            toolbar,
            self._empty,
            self._grid,
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[_MAIN_CSS],
            css_classes=["flowdash-gallery"],
        )

        self.close_button = pmui.IconButton(
            icon="close", description="Close details", size="small", margin=0
        )
        self.close_button.on_click(lambda _event: self.param.update(selected=None))
        self.add_menu = pmui.MenuButton(
            label="Add to dashboard",
            icon="add",
            variant="contained",
            margin=0,
            sx={"textTransform": "none", "borderRadius": 5},
        )
        self.add_menu.on_click(self._on_add)
        self.page_button = pmui.Button(
            label="Open as page",
            icon="open_in_new",
            variant="outlined",
            margin=0,
            sx={"textTransform": "none", "borderRadius": 5},
        )
        self.page_button.on_click(self._on_open_page)
        self.used_in = pmui.MenuList(items=[], dense=True, margin=0, on_click=self._on_used_in)
        self._detail_body = pn.Column(margin=0, sizing_mode="stretch_width")
        self.detail = pmui.Paper(
            self._detail_body,
            elevation=0,
            margin=0,
            visible=False,
            sizing_mode="stretch_both",
            sx={"borderRadius": 0, "borderLeft": 1, "borderColor": "divider"},
            stylesheets=[_DETAIL_CSS],
            css_classes=["flowdash-gallery-detail"],
        )
        self._layout = pn.Row(
            self._main,
            self.detail,
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[":host { min-width: 0 !important; }"],
        )
        self.refresh()

    def __panel__(self):
        """Return the cards beside the detail panel."""
        return self._layout

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _usable(self) -> dict[str, RegistryEntry]:
        app = self._app
        return {
            cid: entry
            for cid, entry in app._component_entries.items()
            if app._can_use_component(entry)
        }

    def _page_path(self, cid: str) -> str | None:
        app = self._app
        entry = app._page_entries.get(cid)
        if entry is None or not app._can_access_entry(entry):
            return None
        return entry.page_path

    async def load(self):
        """Import the components whose ports are not declared, then show their ports.

        Imports run off the event loop; components declaring ``provides`` or
        ``requires`` on ``@register`` are listed without being imported.
        """
        pending = [e.component_id for e in self._entries.values() if not e.ports_known]
        if not pending:
            return
        await self._app._ensure_components_loaded(pending)
        self.refresh()

    def refresh(self):
        """Rebuild the entries and cards from the registry and one dashboard listing."""
        app = self._app
        usable = self._usable()
        dashboards = app.store.list_accessible(app._identity, default_allow=app._default_allow())
        self._editable = [d for d in dashboards if app._can_edit_dashboard(d)]
        usage = usage_index(dashboards, usable)
        specs = app._editor._component_specs
        entries = [
            gallery_entry(
                entry,
                specs.get(cid),
                page_path=self._page_path(cid),
                used_in=usage.get(cid, ()),
            )
            for cid, entry in usable.items()
        ]
        entries.sort(key=lambda e: (ROLES.index(e.role), e.title.casefold()))
        self._entries = {e.component_id: e for e in entries}
        self._cards = {e.component_id: self._card(e) for e in entries}
        if self.selected not in self._entries:
            self.selected = None
        with pn.io.hold():
            self._grid.objects = list(self._cards.values())
            self._sync_accepts_items()
            self._apply_filters()
            self._render_detail()

    @property
    def entries(self) -> list[GalleryEntry]:
        """The listed components, in display order."""
        return list(self._entries.values())

    def visible_entries(self) -> list[GalleryEntry]:
        """Return the components passing the search, role and type filters."""
        return [e for e in self._entries.values() if self._passes(e)]

    def role_counts(self) -> dict[str, int]:
        """Count matches per role for the current search and type filter, as the chips do."""
        counts = dict.fromkeys(ROLE_FILTERS, 0)
        for entry in self._entries.values():
            if entry.matches(self.query) and entry.accepts(self.accepts):
                counts["all"] += 1
                if entry.role in counts:
                    counts[entry.role] += 1
        return counts

    def input_types(self) -> dict[str, str]:
        """Map the input types of the listed components from canonical to display name."""
        types: dict[str, str] = {}
        for entry in self._entries.values():
            for port in entry.inputs:
                canonical = normalize_port_type(port.type)
                if canonical is not None:
                    types.setdefault(canonical, port.type)
        return dict(sorted(types.items(), key=lambda kv: kv[1].casefold()))

    def _passes(self, entry: GalleryEntry) -> bool:
        return (
            (self.role == "all" or entry.role == self.role)
            and entry.matches(self.query)
            and entry.accepts(self.accepts)
        )

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    def _on_search(self, event):
        self.query = event.new or ""

    def _set_role(self, role: str, *_args):
        self.role = role

    def _on_accepts(self, item: dict):
        self.accepts = item.get("port_type")

    def _sync_accepts_items(self):
        types = self.input_types()
        self.accepts_menu.items = [
            {"label": "Any type", "port_type": None},
            *({"label": label, "port_type": canonical} for canonical, label in types.items()),
        ]
        self.accepts_menu.visible = bool(types)

    @param.depends("query", "role", "accepts", watch=True)
    def _apply_filters(self):
        with pn.io.hold():
            visible = 0
            for cid, card in self._cards.items():
                card.visible = self._passes(self._entries[cid])
                visible += card.visible
            self._empty.param.update(
                visible=not visible,
                object="No components match." if self._entries else "No components available.",
            )
            counts = self.role_counts()
            for role, chip in self.role_chips.items():
                active = role == self.role
                chip.param.update(
                    label=f"{ROLE_FILTERS[role]} {counts[role]}",
                    icon="check" if active else None,
                    variant="filled" if active else "outlined",
                    color="primary" if active else "default",
                    visible=role == "all" or counts[role] > 0 or active,
                )
            label = self.input_types().get(self.accepts or "", self.accepts)
            self.accepts_menu.param.update(
                label=f"Accepts {label}" if self.accepts else "Accepts",
                variant="contained" if self.accepts else "outlined",
                color="primary" if self.accepts else "default",
            )

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------

    def _card(self, entry: GalleryEntry) -> pmui.Paper:
        html = pn.pane.HTML(
            card_html(entry), sizing_mode="stretch_width", margin=0, stylesheets=[_CARD_CSS]
        )
        clickable = pmui.Clickable(object=html, sizing_mode="stretch_width", margin=0)
        cid = entry.component_id
        clickable.on_click(lambda *_: self.param.update(selected=cid))
        selected = ["selected"] if entry.component_id == self.selected else []
        return pmui.Paper(
            clickable,
            variant="outlined",
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_PAPER_CSS],
            css_classes=["flowdash-gallery-card", *selected],
        )

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    @param.depends("selected", watch=True)
    def _render_detail(self):
        entry = self._entries.get(self.selected or "")
        with pn.io.hold():
            for cid, card in self._cards.items():
                card.css_classes = ["flowdash-gallery-card"] + (
                    ["selected"] if cid == self.selected else []
                )
            if entry is None:
                self.detail.visible = False
                return
            self.add_menu.items = [
                *(
                    {"label": d.title, "icon": "dashboard", "dashboard_id": d.dashboard_id}
                    for d in self._editable
                ),
                *([None] if self._editable else []),
                dict(_NEW_DASHBOARD),
            ]
            self.page_button.visible = entry.page_path is not None
            self.used_in.items = [
                {"label": ref.title, "icon": "space_dashboard", "path": ref.path}
                for ref in entry.used_in
            ]
            self._detail_body.objects = [
                self._detail_header(entry),
                self._ports_section(entry),
                self._used_in_section(entry),
            ]
            self.detail.visible = True

    def _detail_header(self, entry: GalleryEntry):
        icon = pn.pane.HTML(
            f'<div class="box">{_icon(entry.display_icon)}</div>',
            margin=0,
            stylesheets=[_DETAIL_ICON_CSS],
        )
        title = pn.Column(
            pmui.Typography(entry.title, variant="h6", margin=0, sx={"lineHeight": 1.3}),
            pmui.Typography(
                entry.module or entry.component_id,
                variant="caption",
                margin=0,
                sx={"color": "text.secondary", "wordBreak": "break-all"},
            ),
            margin=0,
            sizing_mode="stretch_width",
        )
        meta = f"{entry.role_name}, {entry.usage[0].lower()}{entry.usage[1:]}"
        return pmui.Column(
            pmui.Row(
                icon,
                title,
                self.close_button,
                margin=0,
                sizing_mode="stretch_width",
                sx={"gap": 1.5, "alignItems": "flex-start"},
            ),
            pmui.Typography(
                entry.description or "No description.",
                variant="body2",
                margin=0,
                sx={"color": "text.secondary" if not entry.description else "text.primary"},
            ),
            pmui.Typography(meta, variant="caption", margin=0, sx={"color": "text.secondary"}),
            pmui.Row(self.add_menu, self.page_button, margin=0, sx={"gap": 1, "flexWrap": "wrap"}),
            margin=0,
            sizing_mode="stretch_width",
            sx={**_SECTION_SX, "pt": 2.5, "gap": 1.5},
        )

    def _section(self, title: str, *objects):
        return pmui.Column(
            pmui.Typography(title, variant="subtitle2", margin=0),
            *objects,
            margin=0,
            sizing_mode="stretch_width",
            sx={**_SECTION_SX, "gap": 1},
        )

    def _ports_section(self, entry: GalleryEntry):
        table = pn.pane.HTML(
            ports_html(entry), margin=0, sizing_mode="stretch_width", stylesheets=[_PORTS_CSS]
        )
        return self._section("Ports", table)

    def _used_in_section(self, entry: GalleryEntry):
        if entry.used_in:
            return self._section("Used in", self.used_in)
        return self._section(
            "Used in",
            pmui.Typography(
                "Not used in any dashboard you can open yet.",
                variant="body2",
                margin=0,
                sx={"color": "text.secondary"},
            ),
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_used_in(self, item: dict):
        if isinstance(item, dict) and item.get("path"):
            self._app._request_navigation(item["path"])

    def _on_open_page(self, _event):
        entry = self._entries.get(self.selected or "")
        if entry is not None and entry.page_path:
            self._app._request_navigation(entry.page_path)

    def _on_add(self, item: dict):
        if not isinstance(item, dict) or self.selected is None:
            return
        pn.state.execute(partial(self.add_to_dashboard, self.selected, item.get("dashboard_id")))

    def _new_dashboard_title(self, entry: GalleryEntry) -> str:
        app = self._app
        base = f"{entry.title} dashboard"
        title, n = base, 1
        while app.store.title_exists(app._user_id, title):
            n += 1
            title = f"{base} {n}"
        return title

    # Locked for the same reason as FlowDashApp._load_page_layout.
    @pn.io.with_lock
    async def add_to_dashboard(self, component_id: str, dashboard_id: str | None = None):
        """Open a dashboard in the editor with *component_id* placed on it.

        Without *dashboard_id* a new dashboard named after the component is
        created first. The placement is left unsaved, like any edit. Returns
        the new instance id, or None if nothing was placed.
        """
        app = self._app
        entry = self._entries.get(component_id)
        if entry is None:
            return None
        if dashboard_id is None:
            if not app._create_new_dashboard(self._new_dashboard_title(entry)):
                return None
        else:
            if pn.state.location is not None:
                pn.state.location.param.update(
                    pathname=f"{_DASH_ROUTE_PREFIX}{dashboard_id}", search="?edit=true"
                )
            generation = app._begin_navigation()
            await app._run_navigation_task(
                app._load_dashboard(dashboard_id, edit=True, generation=generation)
            )
            current = app._current_dashboard
            if (
                not app._is_current_navigation(generation)
                or current is None
                or current.dashboard_id != dashboard_id
            ):
                return None
        editor = app._editor
        if not editor.editable or editor.read_only:
            app._notify("error", "You cannot edit this dashboard.", duration=4000)
            return None
        try:
            instance_id = editor.add_component(component_id)
        except (KeyError, PermissionError, ValueError) as exc:
            app._notify("error", error_text(exc).strip("'\""), duration=5000)
            return None
        app._notify("success", f'Added "{entry.title}". Save to keep it.', duration=3000)
        return instance_id
