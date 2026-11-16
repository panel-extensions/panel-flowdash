"""The launcher: the home page listing dashboards, code pages and components."""

from __future__ import annotations

import copy
import io
import json
import typing as t
import uuid
from datetime import datetime, timezone
from functools import partial
from html import escape

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.auth import Permission
from panel_flowdash.component_spec import _ports_from_metadata
from panel_flowdash.dashboard_store import BaseDashboardStore, DashboardModel
from panel_flowdash.editor import export_filename, export_model
from panel_flowdash.ui import roles

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.registry import RegistryEntry

# The app's component catalog route; not imported from the app, which imports this module.
COMPONENTS_ROUTE = "/components"

OPEN_ACTION = {"label": "Open", "icon": "visibility"}

# Height of a grid card's thumbnail; the actions menu sits just below it.
_THUMB_HEIGHT = 148

# How many component cards the Components section shows before "Browse all".
_FEATURED_COMPONENTS = 3

# Tile sizes TileGrid falls back to for a tile without a saved layout entry.
_DEFAULT_TILE_WIDTH = 50
_DEFAULT_TILE_HEIGHT = 300
_DEFAULT_REFERENCE_WIDTH = 1200
# Thumbnail tiles are drawn at least this tall (in grid pixels) so their icon fits.
_MIN_TILE_HEIGHT = 120.0

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
  word-wrap: normal;
  direction: ltr;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: 'liga';
}
"""

_CARD_CSS = (
    _ICON_CSS
    + f"""
:host {{ display: block; width: 100%; }}
.card {{ cursor: pointer; color: var(--mui-palette-text-primary); }}
.thumb {{
  position: relative;
  height: {_THUMB_HEIGHT}px;
  overflow: hidden;
  box-sizing: border-box;
  border-bottom: 1px solid var(--mui-palette-divider);
  background: var(--mui-palette-action-hover);
  border-radius: 4px 4px 0 0;
}}
.thumb .inner {{ position: absolute; inset: 10px; }}
.thumb .tile {{ position: absolute; box-sizing: border-box; padding: 3px; }}
.thumb .tile > div {{
  width: 100%;
  height: 100%;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mui-palette-background-paper);
  border: 1px solid var(--mui-palette-divider);
  border-radius: 6px;
  color: var(--mui-palette-text-secondary);
  overflow: hidden;
}}
.thumb.empty {{
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 12px;
  color: var(--mui-palette-text-secondary);
}}
.thumb.empty .mi {{ font-size: 28px; }}
.body {{ padding: 12px 48px 14px 16px; min-width: 0; }}
.title {{
  font-size: 15px;
  font-weight: 500;
  line-height: 22px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.meta {{
  font-size: 12px;
  line-height: 18px;
  color: var(--mui-palette-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; min-height: 22px; }}
.chip {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border-radius: 11px;
  font-size: 12px;
  background: var(--mui-palette-action-selected);
  color: var(--mui-palette-text-primary);
}}
.chip .mi {{ font-size: 14px; }}
.row {{ display: flex; align-items: center; gap: 16px; padding: 10px 56px 10px 12px; }}
.row .thumb {{
  flex: none;
  width: 112px;
  height: 64px;
  border: 1px solid var(--mui-palette-divider);
  border-radius: 6px;
}}
.row .thumb .inner {{ inset: 4px; }}
.row .thumb .tile {{ padding: 1.5px; }}
.row .thumb .tile > div {{ border-radius: 3px; }}
.row .thumb .mi {{ font-size: 12px; }}
.row .thumb.empty span:not(.mi) {{ display: none; }}
.row .body {{ flex: 1; padding: 0; }}
.row .chips {{ margin-top: 0; flex-wrap: nowrap; }}
"""
)

_LAYOUT_CSS = """
:host { box-sizing: border-box; padding: 24px 32px 64px; }
@media (max-width: 600px) {
  :host { padding: 16px 16px 48px; }
}
"""

_PAPER_CSS = """
:host { position: relative; }
:host .MuiPaper-root { transition: box-shadow 0.15s; overflow: hidden; }
:host(:hover) .MuiPaper-root {
  box-shadow: 0 4px 8px 3px rgba(0, 0, 0, 0.1), 0 1px 3px rgba(0, 0, 0, 0.14);
}
"""

_GRID_CSS = """
:host {
  display: grid !important;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
  align-items: start;
}
"""

_ITEM_CSS = (
    _ICON_CSS
    + """
:host { display: block; width: 100%; }
.item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  cursor: pointer;
  color: var(--mui-palette-text-primary);
}
.icon {
  flex: none;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--mui-palette-action-selected);
  color: var(--mui-palette-primary-main);
}
.text { min-width: 0; }
.name {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.caption {
  font-size: 12px;
  color: var(--mui-palette-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
"""
)

_MENU_SX = {
    "minWidth": 0,
    "padding": "6px",
    "borderRadius": "50%",
    "color": "text.secondary",
    "& .MuiButton-startIcon": {"margin": 0},
    "& .MuiButton-endIcon": {"display": "none"},
}


def relative_time(when: datetime | None, now: datetime | None = None) -> str | None:
    """Describe *when* relative to *now*, e.g. "12 minutes ago" or "yesterday"."""
    if when is None:
        return None
    now = now or datetime.now(timezone.utc)
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    local, today = when.astimezone(), now.astimezone().date()
    days = (today - local.date()).days
    if days <= 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if local.year == today.year:
        return f"on {local:%b} {local.day}"
    return f"on {local:%b} {local.day}, {local.year}"


def unique_title(store: BaseDashboardStore, user: str, title: str) -> str:
    """Return *title*, numbered if *user* already owns a dashboard with that title."""
    candidate, n = title, 1
    while store.title_exists(user, candidate):
        n += 1
        candidate = f"{title} {n}"
    return candidate


def copy_dashboard(
    store: BaseDashboardStore, source: DashboardModel, user: str, title: str
) -> DashboardModel:
    """Save the contents of *source* as a new, unshared dashboard of *user*."""
    created = store.create_dashboard(user, title)
    model = copy.deepcopy(source)
    model.dashboard_id, model.user_id = created.dashboard_id, created.user_id
    model.title, model.revision = created.title, created.revision
    model.permission = Permission()
    model.updated_at = None
    try:
        store.save_dashboard(model)
    except Exception:
        # Leave no empty dashboard behind for a copy that could not be written.
        store.delete_dashboard(created.user_id, created.dashboard_id)
        raise
    return model


def parse_dashboard_file(content: str | bytes, user: str) -> DashboardModel:
    """Parse a downloaded dashboard file into a model owned by *user*.

    Raises
    ------
    ValueError
        If *content* is not valid JSON or not a dashboard export.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Not a FlowDash dashboard export.")
    title = data.get("title")
    try:
        return DashboardModel.from_dict(
            {
                **data,
                "dashboard_id": uuid.uuid4().hex[:12],
                "user_id": user,
                "title": title.strip() if isinstance(title, str) and title.strip() else "Untitled",
                "permission": None,
                "revision": None,
            }
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed dashboard export: {exc!r}") from exc


def build_downloader() -> pmui.FileDownload:
    """Build a hidden file download that :func:`send_download` drives from the server."""
    return pmui.FileDownload(
        auto=True,
        filename="dashboard.json",
        label="Download",
        visible=False,
        css_classes=["flowdash-downloader"],
    )


def send_download(
    downloader: pmui.FileDownload, model: DashboardModel, restricted: t.Collection[str] = ()
):
    """Make the browser save *model* as the same file the editor's Download writes.

    The items in *restricted* are written as :func:`export_model` describes.
    """
    payload = json.dumps(export_model(model, restricted), indent=2)
    # Clearing first makes downloading the same file twice a change the browser sees.
    downloader.data = None
    downloader.param.update(
        callback=lambda: io.StringIO(payload), filename=export_filename(model.title)
    )
    downloader.transfer(download=True)


_ROLE_NAMES = {"source": "Data source", "transform": "Transform", "view": "View"}


def component_role(entry: RegistryEntry) -> str | None:
    """Return "Data source", "Transform" or "View", by the rules the palette and gallery use.

    Widgets and components whose ports are unknown get no label: ports are
    only known once the component was imported or declared on ``@register``,
    and the launcher never imports components just to label them.
    """
    return _ROLE_NAMES.get(_role(entry))


def _role(entry: RegistryEntry) -> str:
    meta = entry.metadata
    spec = entry.spec
    if spec is None:
        outputs, inputs = _ports_from_metadata(meta)
    else:
        outputs, inputs = spec.outputs, spec.inputs
    ports_known = spec is not None or bool(meta.provides or meta.requires)
    return roles.component_role(entry, inputs, outputs, ports_known=ports_known)


def component_icon(entry: RegistryEntry) -> str | None:
    """Return a component's declared icon, else the icon of its role when that is known."""
    if entry.metadata.icon:
        return entry.metadata.icon
    role = _role(entry)
    return None if role == "other" else roles.ROLE_ICONS[role]


def _wiring_roles(model: DashboardModel) -> dict[str, str]:
    """Map each placed instance to the role it plays in the dashboard's saved wiring."""
    sources = {edge.source for edge in model.edges}
    targets = {edge.target for edge in model.edges}
    wiring = {}
    for item in model.items:
        wired_in, wired_out = item.instance_id in targets, item.instance_id in sources
        if wired_in:
            wiring[item.instance_id] = "transform" if wired_out else "view"
        elif wired_out:
            wiring[item.instance_id] = "source"
    return wiring


def _icon(name: str) -> str:
    return f'<span class="mi" aria-hidden="true">{escape(name)}</span>'


def _chip(icon: str | None, text: str, title: str | None = None) -> str:
    tooltip = f' title="{escape(title)}"' if title else ""
    return f'<span class="chip"{tooltip}>{_icon(icon) if icon else ""}{escape(text)}</span>'


def _tile_boxes(model: DashboardModel) -> list[tuple[float, float, float, float, str]]:
    """Pack the visible tiles left to right the way TileGrid flows them.

    Returns ``(left, top, width, height, instance_id)`` per tile, with left
    and width in percent of the grid width and top and height in pixels.
    """
    layout = {
        spec.get("instance_id"): spec for spec in model.tile_layout if isinstance(spec, dict)
    }
    tiles = []
    for order, item in enumerate(model.items):
        spec = layout.get(item.instance_id, {})
        if spec.get("visible", True) is False:
            continue
        try:
            width = min(max(float(spec.get("width") or _DEFAULT_TILE_WIDTH), 5.0), 100.0)
            height = max(float(spec.get("height") or _DEFAULT_TILE_HEIGHT), _MIN_TILE_HEIGHT)
            index = int(spec.get("index", order))
        except (TypeError, ValueError):
            width, height, index = _DEFAULT_TILE_WIDTH, _DEFAULT_TILE_HEIGHT, order
        tiles.append((index, order, width, height, item.instance_id))
    boxes = []
    left = top = row_height = 0.0
    for _, _, width, height, instance_id in sorted(tiles):
        if left and left + width > 100.01:
            left, top, row_height = 0.0, top + row_height, 0.0
        boxes.append((left, top, width, height, instance_id))
        left += width
        row_height = max(row_height, height)
    return boxes


def thumbnail_html(model: DashboardModel, icons: dict[str, str | None]) -> str:
    """Draw the dashboard's saved tile layout as blocks with component icons.

    *icons* maps component ids to their icon; components without one are
    drawn with the icon of the role they play in this dashboard's wiring.
    """
    boxes = _tile_boxes(model)
    if not boxes:
        return (
            '<div class="thumb empty">'
            f"{_icon('space_dashboard')}<span>No components yet</span></div>"
        )
    reference = float(model.reference_width or _DEFAULT_REFERENCE_WIDTH)
    bottom = max(top + height for _, top, _, height, _ in boxes)
    # Short layouts keep their proportions; tall ones show their top part.
    span = min(max(bottom, reference * 0.45), reference * 0.9)
    components = {item.instance_id: item.component_id for item in model.items}
    wiring = _wiring_roles(model)
    tiles = "".join(
        f'<div class="tile" style="left:{left:.2f}%;top:{top / span * 100:.2f}%;'
        f'width:{width:.2f}%;height:{height / span * 100:.2f}%">'
        f"<div>{_icon(_tile_icon(icons, components[instance_id], wiring.get(instance_id)))}"
        "</div></div>"
        for left, top, width, height, instance_id in boxes
    )
    return f'<div class="thumb" aria-hidden="true"><div class="inner">{tiles}</div></div>'


def _grid(cards: list) -> pn.Column:
    return pn.Column(*cards, margin=0, sizing_mode="stretch_width", stylesheets=[_GRID_CSS])


def _tile_icon(icons: dict[str, str | None], component_id: str, role: str | None) -> str:
    if component_id not in icons:
        return "help_outline"
    return icons[component_id] or roles.ROLE_ICONS.get(role or "other")


class Launcher(Viewer):
    """The home page: dashboard cards with filters and actions, pages and components.

    Card actions are dispatched through :meth:`FlowDashApp._on_dashboard_action`,
    so actions a subclass adds to the app's action tuples appear here as well.
    """

    filter = param.Selector(
        default="all", objects=["all", "owned", "shared"], doc="Which dashboards are listed."
    )

    sort = param.Selector(
        default="edited", objects=["edited", "name"], doc="Order of the dashboard cards."
    )

    view_mode = param.Selector(
        default="grid", objects=["grid", "list"], doc="Show dashboards as cards or rows."
    )

    _FILTER_LABELS: t.ClassVar[dict[str, str]] = {
        "all": "All",
        "owned": "Owned by me",
        "shared": "Shared with me",
    }

    _SORT_LABELS: t.ClassVar[dict[str, str]] = {"edited": "Last edited", "name": "Name"}

    def __init__(self, app: FlowDashApp, **params):
        super().__init__(**params)
        self._app = app
        self._dashboards: list[DashboardModel] = []
        self._menus: dict[str, pmui.MenuButton] = {}
        self._chips = {
            key: pmui.Chip(label=label, size="medium", margin=0)
            for key, label in self._FILTER_LABELS.items()
        }
        for key, chip in self._chips.items():
            chip.on_click(partial(self._set_filter, key))
        self._sort_menu = pmui.MenuButton(
            items=[{"label": label, "sort": key} for key, label in self._SORT_LABELS.items()],
            icon="sort",
            variant="text",
            color="default",
            size="small",
            margin=0,
            on_click=lambda item: setattr(self, "sort", item["sort"]),
        )
        self._view_toggle = pmui.RadioButtonGroup(
            options={":material/grid_view:": "grid", ":material/view_list:": "list"},
            value=self.view_mode,
            size="small",
            variant="outlined",
            margin=0,
        )
        self._view_toggle.link(self, value="view_mode", bidirectional=True)
        self._new_button = pmui.Button(
            label="New dashboard",
            icon="add",
            variant="contained",
            margin=0,
            on_click=lambda _: self._app._open_create_dialog(),
        )
        self._import_input = self._make_import_input("Import file")
        self._empty_import_input = self._make_import_input("Import a file")
        self._cards = pn.Column(sizing_mode="stretch_width", margin=0)
        self._layout = pn.Column(
            sizing_mode="stretch_width",
            margin=0,
            styles={"max-width": "1240px", "margin": "0 auto"},
            stylesheets=[_LAYOUT_CSS],
            css_classes=["flowdash-launcher"],
        )
        self.refresh()

    def __panel__(self):
        """Render the launcher."""
        return self._layout

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self):
        """Reload the dashboards from the store and rebuild the page."""
        app = self._app
        self._dashboards = app.store.list_accessible(
            app._identity, default_allow=app._default_allow()
        )
        with pn.io.hold():
            self._layout.objects = self._page_sections()
            self._render_cards()

    def _owned(self, model: DashboardModel) -> bool:
        return model.owner in self._app._identity.user_names

    def _visible_dashboards(self) -> list[DashboardModel]:
        if self.filter == "owned":
            models = [m for m in self._dashboards if self._owned(m)]
        elif self.filter == "shared":
            models = [m for m in self._dashboards if not self._owned(m)]
        else:
            models = list(self._dashboards)
        if self.sort == "name":
            return sorted(models, key=lambda m: (m.title.casefold(), m.dashboard_id))
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        # Stable, so dashboards without a timestamp keep the store's recency order.
        return sorted(models, key=lambda m: m.updated_at or oldest, reverse=True)

    def _component_icons(self) -> dict[str, str | None]:
        return {cid: component_icon(entry) for cid, entry in self._app._component_entries.items()}

    def _usage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for model in self._dashboards:
            for component_id in {item.component_id for item in model.items}:
                counts[component_id] = counts.get(component_id, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Page structure
    # ------------------------------------------------------------------

    def _page_sections(self) -> list:
        sections = []
        if self._dashboards:
            sections += [self._header(), self._toolbar(), self._cards]
        else:
            sections.append(self._empty_state())
        pages = self._pages_section()
        if pages is not None:
            sections.append(pages)
        if self._dashboards:
            components = self._components_section()
            if components is not None:
                sections.append(components)
        return sections

    def _header(self):
        return pn.FlexBox(
            pn.Column(
                pmui.Typography(
                    "Dashboards",
                    variant="h5",
                    margin=0,
                    sx={"fontSize": "1.5rem", "fontWeight": 400},
                ),
                pmui.Typography(
                    "Dataflow dashboards built from the components in this project.",
                    variant="body2",
                    sx={"color": "text.secondary"},
                    margin=(2, 0, 0, 0),
                ),
                margin=0,
                sizing_mode="stretch_width",
                styles={"flex": "1 1 240px"},
            ),
            pn.Row(self._import_input, self._new_button, margin=0, styles={"gap": "8px"}),
            align_items="flex-end",
            gap="16px",
            margin=(0, 0, 20, 0),
            sizing_mode="stretch_width",
        )

    def _toolbar(self):
        return pn.FlexBox(
            *self._chips.values(),
            pn.Spacer(styles={"flex": "1 1 0"}, width=0, margin=0),
            self._sort_menu,
            self._view_toggle,
            align_items="center",
            gap="8px",
            margin=(0, 0, 16, 0),
            sizing_mode="stretch_width",
        )

    def _section_heading(self, title: str, aside) -> pn.Row:
        return pn.Row(
            pmui.Typography(
                title, variant="h6", margin=0, sx={"fontSize": "1.25rem", "fontWeight": 500}
            ),
            pn.HSpacer(),
            aside,
            align="center",
            margin=(40, 0, 12, 0),
            sizing_mode="stretch_width",
        )

    def _pages_section(self):
        # Components that are also pages are listed under Components instead.
        entries = sorted(
            (
                entry
                for entry in self._app._accessible_page_entries().values()
                if not entry.metadata.component
            ),
            key=lambda e: (e.section, e.name),
        )
        if not entries:
            return None
        cards = [
            self._item_card(
                entry.metadata.icon or "article",
                entry.title,
                entry.section.replace("_", " "),
                partial(self._app._launcher_navigate, entry.page_path),
                css_class="flowdash-page-card",
            )
            for entry in entries
        ]
        return pn.Column(
            self._section_heading(
                "Pages",
                pmui.Typography(
                    "Written in code for this project",
                    variant="caption",
                    sx={"color": "text.secondary"},
                    margin=0,
                ),
            ),
            _grid(cards),
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-launcher-pages"],
        )

    def _usable_components(self) -> dict[str, RegistryEntry]:
        """Return the components the user may place, as the editor offers them."""
        app = self._app
        return {
            cid: entry
            for cid, entry in app._component_entries.items()
            if app._can_use_component(entry)
        }

    def _project_components(self) -> list[tuple[str, RegistryEntry]]:
        """Return the usable components defined by the project rather than built in."""
        return [
            (cid, entry)
            for cid, entry in self._usable_components().items()
            if cid in self._app._registry
        ]

    def _components_section(self):
        app = self._app
        project = self._project_components()
        if not project:
            return None
        usage = self._usage_counts()
        featured = sorted(project, key=lambda ce: (-usage.get(ce[0], 0), ce[1].title.casefold()))
        cards = []
        for cid, entry in featured[:_FEATURED_COMPONENTS]:
            used = usage.get(cid, 0)
            caption = (
                f"Used in {used} dashboard{'' if used == 1 else 's'}" if used else "Not used yet"
            )
            role = component_role(entry)
            cards.append(
                self._item_card(
                    component_icon(entry) or "widgets",
                    entry.title,
                    f"{role}, {caption[0].lower()}{caption[1:]}" if role else caption,
                    partial(app._launcher_navigate, COMPONENTS_ROUTE),
                    css_class="flowdash-component-card",
                )
            )
        browse = pmui.Button(
            label=f"Browse all {len(self._usable_components())}",
            end_icon="chevron_right",
            variant="text",
            size="small",
            margin=0,
            on_click=partial(app._launcher_navigate, COMPONENTS_ROUTE),
        )
        return pn.Column(
            self._section_heading("Components", browse),
            _grid(cards),
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-launcher-components"],
        )

    def _item_card(self, icon: str, title: str, caption: str, on_click, css_class: str):
        html = pn.pane.HTML(
            f'<div class="item"><span class="icon">{_icon(icon)}</span>'
            f'<div class="text"><div class="name">{escape(title)}</div>'
            f'<div class="caption">{escape(caption)}</div></div></div>',
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_ITEM_CSS],
        )
        clickable = pmui.Clickable(object=html, sizing_mode="stretch_width", margin=0)
        clickable.on_click(lambda *_: on_click())
        return pmui.Paper(
            clickable,
            variant="outlined",
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_PAPER_CSS],
            css_classes=[css_class],
        )

    def _make_import_input(self, label: str) -> pmui.FileInput:
        file_input = pmui.FileInput(
            label=label,
            icon="upload_file",
            accept=".json,application/json",
            variant="outlined",
            margin=0,
            css_classes=["flowdash-import"],
        )
        file_input.param.watch(self._on_import, "value")
        return file_input

    def _on_import(self, event):
        file_input = event.obj
        if event.new is None:
            return
        filename = file_input.filename or "dashboard.json"
        content = event.new
        file_input.clear()
        self._app._import_dashboard_file(filename, content)

    def _empty_state(self):
        app = self._app
        examples = sorted(entry.title for _, entry in self._project_components())[:2]
        if len(examples) == 2:
            text = (
                f"Connect components like {examples[0]} and {examples[1]} on a canvas, "
                "then arrange their views as tiles."
            )
        else:
            text = "Connect components on a canvas, then arrange their views as tiles."
        actions = [self._new_button, self._empty_import_input]
        total = len(self._usable_components())
        if total:
            actions.append(
                pmui.Button(
                    label=f"Browse {total} component{'' if total == 1 else 's'}",
                    variant="text",
                    margin=0,
                    on_click=partial(app._launcher_navigate, COMPONENTS_ROUTE),
                )
            )
        return pn.Column(
            pn.pane.HTML(
                _icon("space_dashboard"),
                margin=0,
                align="center",
                stylesheets=[
                    _ICON_CSS,
                    ".mi { font-size: 48px; color: var(--mui-palette-text-secondary); }",
                ],
            ),
            pmui.Typography(
                "Build your first dashboard", variant="h6", margin=(8, 0, 0, 0), align="center"
            ),
            pmui.Typography(
                text,
                variant="body2",
                sx={"color": "text.secondary"},
                margin=0,
                align="center",
                styles={"text-align": "center", "max-width": "440px"},
            ),
            pn.FlexBox(
                *actions,
                gap="8px",
                justify_content="center",
                margin=(16, 0, 0, 0),
                sizing_mode="stretch_width",
            ),
            margin=(48, 0, 8, 0),
            sizing_mode="stretch_width",
            css_classes=["flowdash-launcher-empty"],
        )

    # ------------------------------------------------------------------
    # Dashboard cards
    # ------------------------------------------------------------------

    @param.depends("filter", "sort", "view_mode", watch=True)
    def _render_cards(self):
        for key, chip in self._chips.items():
            selected = key == self.filter
            label = self._FILTER_LABELS[key]
            if key == "all":
                label = f"{label} {len(self._dashboards)}"
            chip.param.update(
                label=label,
                icon="check" if selected else None,
                color="primary" if selected else "default",
                variant="filled" if selected else "outlined",
            )
        self._sort_menu.label = self._SORT_LABELS[self.sort]
        icons = self._component_icons()
        self._menus = {}
        cards = [self._dashboard_card(model, icons) for model in self._visible_dashboards()]
        if not cards:
            self._cards.objects = [
                pmui.Typography(
                    "No dashboards match this filter.",
                    variant="body2",
                    sx={"color": "text.secondary"},
                    margin=(24, 0),
                    css_classes=["flowdash-launcher-no-match"],
                )
            ]
        elif self.view_mode == "grid":
            self._cards.objects = [_grid(cards)]
        else:
            self._cards.objects = [
                pn.Column(*cards, margin=0, sizing_mode="stretch_width", styles={"gap": "8px"})
            ]

    def _set_filter(self, key: str, *_):
        self.filter = key

    def _meta_text(self, model: DashboardModel) -> str:
        owner = "you" if self._owned(model) else model.owner
        when = relative_time(model.updated_at)
        return f"Edited {when} by {owner}" if when else f"Owned by {owner}"

    def _sharing_chip(self, model: DashboardModel) -> str:
        permission = model.permission
        names = sorted(permission.allow_users | permission.allow_groups)
        if names:
            return _chip("group", f"Shared with {len(names)}", ", ".join(names))
        if self._app._default_allow():
            return _chip("public", "Everyone", "Everyone in the project can view")
        return _chip("lock", "Only you", "Only the owner and admins can open it")

    def _card_html(self, model: DashboardModel, icons: dict[str, str | None]) -> str:
        chips = []
        if not model.items:
            chips.append(_chip(None, "Empty"))
        else:
            count = len(model.items)
            chips.append(_chip(None, f"{count} component{'' if count == 1 else 's'}"))
        chips.append(self._sharing_chip(model))
        layout = "row" if self.view_mode == "list" else "card"
        return (
            f'<div class="card {layout}">{thumbnail_html(model, icons)}'
            f'<div class="body"><div class="title" title="{escape(model.title)}">'
            f"{escape(model.title)}</div>"
            f'<div class="meta">{escape(self._meta_text(model))}</div>'
            f'<div class="chips">{"".join(chips)}</div></div></div>'
        )

    def _menu_items(self, model: DashboardModel) -> list[dict | None]:
        """Open, then the app's actions with a divider before destructive ones."""
        app = self._app
        items: list[dict | None] = [OPEN_ACTION]
        for action in app._dashboard_actions(*app._dashboard_rights(model)):
            if action.get("color") == "error" and items[-1] is not None:
                items.append(None)
            items.append(dict(action))
        return items

    def _dashboard_card(self, model: DashboardModel, icons: dict[str, str | None]):
        dashboard_id, title = model.dashboard_id, model.title
        html = pn.pane.HTML(
            self._card_html(model, icons),
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_CARD_CSS],
        )
        clickable = pmui.Clickable(object=html, sizing_mode="stretch_width", margin=0)
        clickable.on_click(lambda *_: self._dispatch(dashboard_id, title, OPEN_ACTION))
        items = self._menu_items(model)
        # MenuButton has no per-item colour, so destructive items are styled by position.
        danger = [
            f".MuiMenu-list > :nth-child({i + 1}) {{ color: var(--mui-palette-error-main); }}"
            for i, item in enumerate(items)
            if item is not None and item.get("color") == "error"
        ]
        top = "calc(50% - 16px)" if self.view_mode == "list" else f"{_THUMB_HEIGHT + 8}px"
        menu = pmui.MenuButton(
            items=items,
            icon="more_vert",
            label="",
            variant="text",
            color="default",
            size="small",
            icon_size="20px",
            description=f"More actions for {title}",
            margin=0,
            sx=_MENU_SX,
            stylesheets=danger,
            # No z-index or transform here: either would trap the open menu in a
            # stacking context below the cards that follow.
            styles={"position": "absolute", "right": "6px", "top": top},
            css_classes=["flowdash-card-menu"],
            on_click=partial(self._dispatch, dashboard_id, title),
        )
        self._menus[dashboard_id] = menu
        return pmui.Paper(
            clickable,
            menu,
            variant="outlined",
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_PAPER_CSS],
            css_classes=["flowdash-dashboard-card"],
        )

    def _dispatch(self, dashboard_id: str, title: str, item: dict):
        self._app._on_dashboard_action(self._app._action_key(item), dashboard_id, title)
