"""Empty, error and system states: not found, access denied, loading, empty canvas, unsaved changes."""

from __future__ import annotations

import difflib
import html
import threading
import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewer

from panel_flowdash.auth import ANONYMOUS_USER
from panel_flowdash.ui.text import text_typography
from panel_flowdash.widgets import BUILTIN_COMPONENTS

if t.TYPE_CHECKING:
    from panel_flowdash.dashboard_store import DashboardModel
    from panel_flowdash.editor import FlowDash
    from panel_flowdash.registry import RegistryEntry

# Icon circle colours per tone, from the theme palette so both modes keep their contrast.
_TONES = {
    "primary": {
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.12)",
        "color": "primary.main",
    },
    "error": {
        "bgcolor": "rgba(var(--mui-palette-error-mainChannel) / 0.12)",
        "color": "error.main",
    },
    "neutral": {"bgcolor": "action.selected", "color": "text.secondary"},
}

# How many similar dashboards a not-found page suggests, and how close a match must be.
_SIMILAR_LIMIT = 3
_SIMILAR_CUTOFF = 0.6

# How many changes the unsaved-changes dialog lists before summarising the rest.
CHANGES_LIMIT = 6

# Tags that mark a component as a data source, as the palette's role groups read them.
_SOURCE_TAGS = {"source", "data source", "datasource"}


class EmptyState(Viewer):
    """A centred explanation of an empty or failed page: an icon, a title, text and actions.

    ``body`` holds extra content shown between the text and the actions, such
    as a list of suggestions.
    """

    actions = param.List(default=[], doc="Buttons offering the next step, primary first.")

    body = param.List(default=[], doc="Extra content shown above the actions.")

    icon = param.String(default="info", doc="Material icon name shown in the tinted circle.")

    text = param.String(default="", doc="One or two sentences explaining what happened.")

    title = param.String(default="", doc="What happened, in a few words.")

    tone = param.Selector(
        default="primary",
        objects=list(_TONES),
        doc="Colour of the icon circle: 'primary', 'error' or 'neutral'.",
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._icon = pmui.Avatar(
            content=f":material/{self.icon}:",
            margin=0,
            align="center",
            sx={
                "width": 64,
                "height": 64,
                "borderRadius": "20px",
                "fontSize": "32px",
                **_TONES[self.tone],
            },
        )
        self._title = text_typography(
            self.title,
            variant="h5",
            margin=(16, 0, 0, 0),
            align="center",
            styles={"text-align": "center"},
        )
        self._text = text_typography(
            self.text,
            variant="body1",
            margin=(8, 0, 0, 0),
            align="center",
            sx={"color": "text.secondary"},
            styles={"text-align": "center", "max-width": "480px"},
            visible=bool(self.text),
        )
        self._layout = pn.Column(
            self._icon,
            self._title,
            self._text,
            *self.body,
            pn.FlexBox(
                *self.actions,
                gap="8px",
                justify_content="center",
                margin=(20, 0, 0, 0),
                sizing_mode="stretch_width",
                visible=bool(self.actions),
            ),
            margin=(72, 16, 32, 16),
            sizing_mode="stretch_width",
            styles={"align-items": "center"},
            css_classes=["flowdash-empty-state"],
        )

    def __panel__(self):
        """Render the state."""
        return self._layout


def _button(label: str, on_click: t.Callable | None, variant: str = "contained", **kwargs):
    button = pmui.Button(label=label, variant=variant, margin=0, **kwargs)
    if on_click is not None:
        button.on_click(lambda _event: on_click())
    return button


def similar_dashboards(
    ref: str, dashboards: t.Iterable[DashboardModel], limit: int = _SIMILAR_LIMIT
) -> list[DashboardModel]:
    """Return the *dashboards* whose id or title is closest to *ref*, best match first.

    Matches ignore case. A title containing *ref* (or contained in it) counts
    as a match, so a partial name finds the dashboard, as does an id with a
    typo. Only pass dashboards the user may open, so no other title is shown.
    """
    needle = ref.strip().lower()
    if not needle:
        return []
    scored = []
    for model in dashboards:
        candidates = (model.dashboard_id.lower(), model.title.lower())
        score = max(difflib.SequenceMatcher(None, needle, c).ratio() for c in candidates)
        title = candidates[1]
        if len(needle) >= 3 and (needle in title or title in needle):
            score = max(score, 0.9)
        if score >= _SIMILAR_CUTOFF:
            scored.append((score, model))
    scored.sort(key=lambda pair: -pair[0])
    return [model for _, model in scored[:limit]]


def dashboard_not_found(
    dashboard_id: str,
    similar: list[DashboardModel],
    *,
    on_open: t.Callable[[str], None],
    on_all: t.Callable[[], None],
) -> EmptyState:
    """Build the page for a dashboard id that does not exist (or that the user may not see)."""
    body = []
    if similar:
        menu = pmui.MenuList(
            items=[
                {
                    "label": model.title,
                    "secondary": model.dashboard_id,
                    "icon": "space_dashboard",
                    "dashboard_id": model.dashboard_id,
                }
                for model in similar
            ],
            dense=False,
            margin=0,
            sizing_mode="stretch_width",
        )
        menu.on_click(lambda item: on_open(item["dashboard_id"]))
        paper = pmui.Paper(
            pmui.Typography(
                "Similar dashboards",
                variant="caption",
                margin=(8, 16, 0, 16),
                sx={"color": "text.secondary"},
            ),
            menu,
            variant="outlined",
            margin=0,
            max_width=420,
            sizing_mode="stretch_width",
        )
        body.append(
            pn.Row(
                pn.HSpacer(),
                paper,
                pn.HSpacer(),
                margin=(20, 0, 0, 0),
                sizing_mode="stretch_width",
            )
        )
    return EmptyState(
        icon="search_off",
        tone="error",
        title="This dashboard does not exist",
        text=(
            f"No dashboard has the id {dashboard_id}. It may have been deleted, "
            "or the link is incomplete."
        ),
        body=body,
        actions=[_button("All dashboards", on_all, icon="apps")],
    )


def signed_in_as(user: str) -> str:
    """Say who the user is signed in as, or that they are not signed in."""
    if not user or user == ANONYMOUS_USER:
        return "You're not signed in."
    return f"You're signed in as {user}."


def access_denied(
    *,
    user: str,
    subject: str | None = None,
    owner: str | None = None,
    on_home: t.Callable[[], None] | None = None,
) -> EmptyState:
    """Build the page for a dashboard or page that exists but the user may not open.

    *subject* names what was refused (a page title; dashboards pass ``None``
    so their title is not revealed). *owner* is who to ask for access.
    """
    ask = (
        f"Ask the owner, {owner}, to add you."
        if owner
        else "Ask an administrator of this app to give you access."
    )
    return EmptyState(
        icon="lock",
        tone="neutral",
        title=f"You don't have access to {subject or 'this dashboard'}",
        text=f"{signed_in_as(user)} {ask}",
        actions=[_button("Go home", on_home, icon="home")] if on_home is not None else [],
    )


def page_not_found(path: str, *, on_home: t.Callable[[], None]) -> EmptyState:
    """Build the page for a URL that matches no dashboard, page or route."""
    return EmptyState(
        icon="explore_off",
        tone="error",
        title="This page does not exist",
        text=f"Nothing is at {path}. Check the address, or start again from home.",
        actions=[_button("Go home", on_home, icon="home")],
    )


class LoadingSkeleton(Viewer):
    """Placeholder of a dashboard while it loads: a header line and tile blocks."""

    tiles = param.Integer(default=4, bounds=(1, None), doc="Number of tile placeholders.")

    def __init__(self, **params):
        super().__init__(**params)

        def block(height, width=None, **kwargs):
            return pmui.Skeleton(
                pn.Spacer(height=height),
                variant="rounded",
                animation="wave",
                height=height,
                width=width,
                sizing_mode="stretch_width" if width is None else "fixed",
                **kwargs,
            )

        header = pn.Column(
            block(28, 320, margin=(0, 0, 8, 0)),
            block(14, 200, margin=0),
            margin=(16, 16, 8, 16),
            sizing_mode="stretch_width",
        )
        tiles = [
            pn.Column(
                block(14, 160, margin=(12, 12, 10, 12)),
                block(240, margin=(0, 12, 12, 12)),
                sizing_mode="stretch_width",
                styles={
                    "flex": "1 1 calc(50% - 16px)",
                    "min-width": "280px",
                    "border": "1px solid var(--mui-palette-divider)",
                    "border-radius": "12px",
                },
                margin=0,
            )
            for _ in range(self.tiles)
        ]
        self._layout = pn.Column(
            header,
            pn.FlexBox(*tiles, gap="16px", margin=(8, 16), sizing_mode="stretch_width"),
            sizing_mode="stretch_width",
            css_classes=["flowdash-loading"],
        )

    def __panel__(self):
        """Render the placeholder."""
        return self._layout


def _is_source(entry: RegistryEntry, editor: FlowDash) -> bool | None:
    """Whether *entry* is a data source; ``None`` when its ports are not known yet."""
    meta = entry.metadata
    if any(str(tag).strip().lower() in _SOURCE_TAGS for tag in meta.tags):
        return True
    spec = editor._component_specs.get(entry.app_id)
    if spec is not None:
        consumed = {port.type for port in spec.inputs}
        return any(port.type not in consumed for port in spec.outputs)
    if meta.provides or meta.requires:
        return bool(meta.provides) and not meta.requires
    return None


def first_data_source(editor: FlowDash) -> str | None:
    """Return the id of the first component the editor offers that is a data source.

    Components are imported lazily, so for one whose ports are not known yet
    a "source" in its title or id stands in for the port check.
    """
    guess = None
    for app_id, entry in editor._component_entries.items():
        if app_id in BUILTIN_COMPONENTS or not editor._component_authorized(app_id):
            continue
        source = _is_source(entry, editor)
        if source:
            return app_id
        if source is None and guess is None and "source" in f"{entry.title} {app_id}".lower():
            guess = app_id
    return guess


class StartCard(Viewer):
    """The empty canvas's guide: three steps to a first dashboard and one-click starts."""

    editor = param.Parameter(doc="The FlowDash editor whose canvas is empty.")

    def __init__(self, **params):
        super().__init__(**params)
        self._title = pmui.Typography("Start with a data source", variant="subtitle1", margin=0)
        self._steps = pn.pane.HTML(margin=(4, 0, 4, 0), sizing_mode="stretch_width")
        self.add_button = pmui.Button(
            icon="add", variant="contained", size="small", margin=0, visible=False
        )
        self.add_button.on_click(self._on_add)
        self.import_button = pmui.Button(
            label="Import a .json file", icon="upload_file", variant="text", size="small", margin=0
        )
        self.import_button.on_click(lambda _event: self.editor._dashboard_bar.open_import())
        self._actions = pn.FlexBox(
            self.add_button, self.import_button, gap="8px", margin=(8, 0, 0, 0)
        )
        self._source: str | None = None
        self._card = pmui.Paper(
            self._title,
            self._steps,
            self._actions,
            variant="outlined",
            width=380,
            margin=0,
            # The canvas's bottom panel; lift the card to about the middle of the canvas.
            stylesheets=[":host { margin-bottom: max(24px, calc(50vh - 300px)) !important; }"],
            sx={
                "p": 2.5,
                "borderStyle": "dashed",
                "borderWidth": 2,
                "borderRadius": "16px",
                "bgcolor": "background.paper",
            },
            css_classes=["flowdash-start-card"],
        )
        self.editor.param.watch(self.refresh, ["dashboard", "read_only", "editable"])
        self.refresh()

    def refresh(self, *_events):
        """Pick the data source to offer and reflect whether the canvas may be edited."""
        editor = self.editor
        # Without a dashboard (the components route) the canvas is a scratch pad
        # that cannot be saved, so it is read-only but still open to drops.
        locked = editor.read_only and editor.dashboard is not None
        self._source = None if locked else first_data_source(editor)
        entry = editor._component_entries.get(self._source) if self._source else None
        name = f"<b>{html.escape(entry.title)}</b>" if entry is not None else "a data source"
        if locked:
            self._title.object = "This dashboard is empty"
            self._steps.object = (
                "<p style='margin:0;color:var(--mui-palette-text-secondary)'>"
                "Someone who can edit it has not added any components yet.</p>"
            )
        else:
            self._title.object = "Start with a data source"
            self._steps.object = (
                "<ol style='margin:0;padding-left:20px;line-height:1.7;"
                "color:var(--mui-palette-text-secondary)'>"
                f"<li>Drag {name} from the palette onto the canvas</li>"
                "<li>Drop a view on its output port to connect it</li>"
                "<li>Switch to <b>Layout</b> to arrange the tiles</li></ol>"
            )
        self.add_button.param.update(
            label=f"Add {entry.title}" if entry is not None else "",
            visible=entry is not None,
        )
        self._actions.visible = not locked

    def _on_add(self, _event):
        if self._source is not None:
            self.editor._add_from_ui(self._source)

    def __panel__(self):
        """Render the card."""
        return self._card


def _instance_title(model: DashboardModel, instance_id: str, titles: dict[str, str]) -> str:
    for item in model.items:
        if item.instance_id == instance_id:
            return item.config.get("title") or titles.get(item.component_id, item.component_id)
    return instance_id


def _edge_key(edge) -> tuple[str, str, str, str]:
    return (edge.source, edge.source_port, edge.target, edge.target_port)


def describe_changes(
    saved: DashboardModel | None,
    current: DashboardModel,
    titles: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """List what changed from the *saved* dashboard to the *current* canvas.

    Returns ``(icon, text)`` pairs: added and removed components, added and
    removed connections, changed transforms, changed component and dashboard
    settings, and one entry for moved or resized tiles. *titles* maps
    component ids to their titles.
    """
    titles = titles or {}
    before = {item.instance_id: item for item in saved.items} if saved is not None else {}
    after = {item.instance_id: item for item in current.items}
    changes: list[tuple[str, str]] = []

    def name(model, instance_id):
        return _instance_title(model, instance_id, titles)

    for instance_id in after.keys() - before.keys():
        changes.append(("add_circle", f"Added {name(current, instance_id)}"))
    for instance_id in before.keys() - after.keys():
        changes.append(("remove_circle", f"Removed {name(saved, instance_id)}"))
    moved = False
    for instance_id in after.keys() & before.keys():
        old, new = before[instance_id], after[instance_id]
        if old.config != new.config:
            changes.append(("tune", f"Changed the settings of {name(current, instance_id)}"))
        if (old.x, old.y) != (new.x, new.y):
            moved = True

    old_edges = {_edge_key(e): e for e in saved.edges} if saved is not None else {}
    new_edges = {_edge_key(e): e for e in current.edges}

    def connection(model, key):
        source, _source_port, target, target_port = key
        return f"{name(model, source)} to {name(model, target)}, {target_port}"

    for key in sorted(new_edges.keys() - old_edges.keys()):
        changes.append(("link", f"Connected {connection(current, key)}"))
    for key in sorted(old_edges.keys() - new_edges.keys()):
        changes.append(("link_off", f"Disconnected {connection(saved, key)}"))
    for key in sorted(new_edges.keys() & old_edges.keys()):
        if (old_edges[key].transform or []) != (new_edges[key].transform or []):
            changes.append(
                ("functions", f"Changed the transform into {name(current, key[2])}, {key[3]}")
            )

    if saved is not None:
        changed = sorted(
            key
            for key in saved.settings.keys() | current.settings.keys()
            if saved.settings.get(key) != current.settings.get(key)
        )
        if changed:
            labels = ", ".join(key.replace("_", " ") for key in changed)
            changes.append(("settings", f"Changed dashboard settings: {labels}"))
        layout_changed = (
            saved.tile_layout != current.tile_layout
            or saved.responsive_layouts != current.responsive_layouts
        )
        if moved or (layout_changed and before.keys() == after.keys()):
            changes.append(("open_with", "Moved or resized components"))
    return changes


class UnsavedChangesDialog(Viewer):
    """Asks what to do with unsaved changes before leaving the editor, listing them.

    The answers call ``on_leave`` (discard and go), ``on_save`` (save, then
    go) or ``on_stay``; each closes the dialog first.
    """

    on_leave = param.Callable(doc="Called when the user leaves without saving.")

    on_save = param.Callable(doc="Called when the user saves and leaves.")

    on_stay = param.Callable(doc="Called when the user keeps editing.")

    def __init__(self, **params):
        super().__init__(**params)
        self.leave_button = pmui.Button(
            label="Leave without saving", color="error", variant="text", margin=0
        )
        self.stay_button = pmui.Button(label="Keep editing", variant="text", margin=0)
        self.save_button = pmui.Button(label="Save and leave", variant="contained", margin=0)
        self._answered = threading.Lock()
        self.leave_button.on_click(self._answer_with("on_leave"))
        self.stay_button.on_click(self._answer_with("on_stay"))
        self.save_button.on_click(self._answer_with("on_save"))
        self._changes = pn.pane.HTML(
            margin=(0, 0, 8, 0), sizing_mode="stretch_width", stylesheets=[_CHANGES_CSS]
        )
        self.changes: list[tuple[str, str]] = []
        self.dialog = pmui.Dialog(
            objects=[
                pn.Column(
                    self._changes,
                    pn.Row(
                        self.leave_button,
                        pn.layout.HSpacer(),
                        self.stay_button,
                        self.save_button,
                        sizing_mode="stretch_width",
                        margin=(8, 0, 0, 0),
                    ),
                    sizing_mode="stretch_width",
                )
            ],
            title="Save changes before leaving?",
            open=False,
            min_width=440,
        )

    def _answer_with(self, name: str):
        def answer(_event):
            # Under threaded Panel two clicks can arrive together; only the first counts.
            if not self._answered.acquire(blocking=False):
                return
            try:
                self.dialog.open = False
                callback = getattr(self, name)
                if callback is not None:
                    callback()
            finally:
                self._answered.release()

        return answer

    def show(self, changes: list[tuple[str, str]]):
        """Open the dialog listing *changes*, at most :data:`CHANGES_LIMIT` of them."""
        self.changes = list(changes)
        shown = self.changes[:CHANGES_LIMIT]
        rows = "".join(
            f"<li><span class='material-icons' aria-hidden='true'>{icon}</span>"
            f"<span>{html.escape(text)}</span></li>"
            for icon, text in shown
        )
        rest = len(self.changes) - len(shown)
        if rest:
            rows += f"<li class='more'><span>and {rest} more change{'' if rest == 1 else 's'}</span></li>"
        if not rows:
            rows = "<li><span>Your changes since the last save are lost if you leave.</span></li>"
        self._changes.object = f"<ul class='changes'>{rows}</ul>"
        self.dialog.open = True

    def __panel__(self):
        """Render the dialog."""
        return self.dialog


class LeavePageGuard(JSComponent):
    """Has the browser ask before the page is reloaded, closed or left while `active`.

    Navigation inside the app goes through :class:`UnsavedChangesDialog`; this
    covers leaving the page itself, where the browser shows its own prompt.
    """

    active = param.Boolean(default=False, doc="Whether leaving the page asks first.")

    _esm = """
    export function render({model}) {
      const onBeforeUnload = (event) => {
        if (model.active) {
          event.preventDefault()
          event.returnValue = ""
        }
      }
      window.addEventListener("beforeunload", onBeforeUnload)
      model.on("remove", () => window.removeEventListener("beforeunload", onBeforeUnload))
    }
    """

    def __init__(self, **params):
        params.setdefault("height", 0)
        params.setdefault("width", 0)
        params.setdefault("margin", 0)
        super().__init__(**params)


_CHANGES_CSS = """
.changes { list-style: none; margin: 0; padding: 0; font-size: 14px;
  color: var(--mui-palette-text-secondary); }
.changes li { display: flex; gap: 10px; align-items: center; padding: 4px 0; }
.changes li.more { padding-left: 28px; }
.changes .material-icons { font-family: 'Material Icons'; font-size: 18px; line-height: 1;
  font-style: normal; font-weight: normal; letter-spacing: normal; text-transform: none;
  white-space: nowrap; direction: ltr; -webkit-font-smoothing: antialiased;
  font-feature-settings: 'liga'; }
"""
