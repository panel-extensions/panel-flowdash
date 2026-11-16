"""The command palette: Ctrl/Cmd+K search over dashboards, components and actions, plus shortcuts."""

from __future__ import annotations

import logging
import typing as t

import param
from panel.custom import JSComponent

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger(__name__)

PLACEHOLDER = "Search dashboards, components and actions"

# Shown in the palette's side column and the `?` list. Only keys that work are listed.
SHORTCUTS = [
    {"group": "Viewing", "label": "Search and commands", "keys": ["Mod", "K"]},
    {"group": "Viewing", "label": "Refresh", "keys": ["R"]},
    {"group": "Viewing", "label": "Edit mode", "keys": ["E"]},
    {"group": "Viewing", "label": "Keyboard shortcuts", "keys": ["?"]},
    {"group": "Editing", "label": "Save", "keys": ["Mod", "S"]},
    {"group": "Editing", "label": "Back to view mode", "keys": ["Esc"]},
]

# Shortcuts the browser sends, each running the action of the same name.
_SHORTCUT_ACTIONS = frozenset({"refresh", "edit", "view", "save"})


class CommandPalette(JSComponent):
    """A search field that opens a command palette, and the app's keyboard shortcuts.

    The field and the dialog are rendered in the browser, which also matches
    and ranks the results. When the palette opens it asks *on_open* for
    :attr:`items`, so the server sends the list once per opening, and a
    chosen item comes back as its ``id`` through *on_command*.

    Shortcuts are registered on the document and ignored while typing in a
    text field: Ctrl/Cmd+K opens the palette, ``?`` lists the shortcuts, and
    R (refresh), E (edit), Esc (back to view, outside dialogs) and Ctrl/Cmd+S
    (save, in edit mode) are sent to *on_command* as ``shortcut:<name>``.
    """

    items = param.List(
        default=[],
        doc="""
        Palette entries, each a dict with ``id``, ``group``, ``title`` and
        optionally ``subtitle``, ``icon``, ``keywords``, ``keys`` (shortcut
        chips) and ``hint`` (text on the right).""",
    )

    placeholder = param.String(default=PLACEHOLDER, doc="Text in the search field.")

    shortcuts = param.List(default=SHORTCUTS, doc="Shortcuts listed beside the results.")

    _esm = "command_palette.js"

    def __init__(
        self,
        on_open: t.Callable[[], list[dict]] | None = None,
        on_command: t.Callable[[str], None] | None = None,
        **params,
    ):
        super().__init__(**params)
        self.on_open = on_open
        self.on_command = on_command

    def _handle_msg(self, msg):
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "open" and self.on_open is not None:
            items = self.on_open()
            if items != self.items:
                self.items = items
        elif kind in ("run", "shortcut") and self.on_command is not None:
            command = msg.get("id") if kind == "run" else f"shortcut:{msg.get('name')}"
            if isinstance(command, str):
                self.on_command(command)


class CommandPaletteController:
    """Builds the palette's items from an app's state and runs the chosen ones.

    Every action goes through the same methods as the matching button, so
    permissions and the unsaved-changes dialog apply as they do there.
    """

    def __init__(self, app: FlowDashApp):
        self._app = app
        self.palette = CommandPalette(
            on_open=self.items,
            on_command=self.run,
            margin=(0, 8),
            align="center",
        )

    @classmethod
    def install(cls, app: FlowDashApp) -> CommandPaletteController | None:
        """Place a palette in the app shell's search slot; None without a shell."""
        shell = app._shell
        if shell is None:
            return None
        controller = cls(app)
        shell.search_slot[:] = [controller.palette]
        return controller

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def _editor(self):
        return self._app._editor

    def _dashboard(self):
        return self._app._current_dashboard

    def _editing(self) -> bool:
        editor = self._editor
        return self._dashboard() is not None and editor.editable and not editor.read_only

    def _can_edit(self) -> bool:
        dashboard = self._dashboard()
        return dashboard is not None and self._app._dashboard_rights(dashboard)[1]

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def items(self) -> list[dict]:
        """Return every entry the palette offers now, in group order."""
        try:
            return [
                *self._dashboard_items(),
                *self._component_items(),
                *self._tile_items(),
                *self._action_items(),
            ]
        except Exception:
            logger.exception("Could not list the command palette's items")
            return self._action_items()

    def _dashboard_items(self) -> list[dict]:
        app = self._app
        current = self._dashboard()
        user_names = app._identity.user_names
        items = []
        dashboards = app.store.list_accessible(app._identity, default_allow=app._default_allow())
        for model in dashboards:
            if current is not None and model.dashboard_id == current.dashboard_id:
                subtitle = "Current dashboard"
            elif model.owner in user_names:
                subtitle = "Owned by you"
            else:
                subtitle = f"Shared by {model.owner}"
            items.append(
                {
                    "id": f"dashboard:{model.dashboard_id}",
                    "group": "Dashboards",
                    "title": model.title,
                    "subtitle": subtitle,
                    "icon": "space_dashboard",
                }
            )
        return items

    def _component_items(self) -> list[dict]:
        editor = self._editor
        adding = self._editing()
        placed = editor._placed_singletons()
        items = []
        for component_id, entry in editor._component_entries.items():
            if not editor._component_authorized(component_id):
                continue
            meta = entry.metadata
            if adding and component_id in placed:
                subtitle = "Already on this dashboard"
            elif adding:
                subtitle = "Add to this dashboard"
            else:
                subtitle = meta.description or "Open in the component library"
            items.append(
                {
                    "id": f"component:{component_id}",
                    "group": "Components",
                    "title": entry.title,
                    "subtitle": subtitle,
                    "icon": meta.icon or "widgets",
                    "hint": "Add" if adding else "View",
                    "keywords": " ".join([entry.section, *meta.tags, meta.description or ""]),
                }
            )
        return items

    def _tile_items(self) -> list[dict]:
        """Inspect and Expand for each tile in view mode, where the tile offers them."""
        editor = self._editor
        if self._dashboard() is None or editor.editable:
            return []
        items = []
        for item in editor._tile_items:
            instance_id = item["instance_id"]
            tile = editor._inspect_tiles.get(instance_id)
            if tile is None:
                continue
            title = self._tile_title(item)
            if self._can_inspect(item):
                items.append(
                    {
                        "id": f"inspect:{instance_id}",
                        "group": "On this dashboard",
                        "title": f"Inspect {title}",
                        "subtitle": "Show the data behind the tile",
                        "icon": "data_object",
                    }
                )
            # Tiles framed with a title bar can be expanded to the full window.
            if callable(getattr(tile, "expand", None)):
                items.append(
                    {
                        "id": f"expand:{instance_id}",
                        "group": "On this dashboard",
                        "title": f"Expand {title}",
                        "icon": "open_in_full",
                    }
                )
        return items

    def _tile_title(self, item: dict) -> str:
        instance_title = getattr(self._editor, "_instance_title", None)
        if instance_title is not None:
            return instance_title(item["instance_id"])
        if "placeholder" in item:
            return item["placeholder"].title
        title = item.get("config", {}).get("title")
        if isinstance(title, str) and title:
            return title
        spec = self._editor._component_specs.get(item["component_id"])
        return spec.title if spec is not None else item["instance_id"]

    def _can_inspect(self, item: dict) -> bool:
        editor = self._editor
        if "placeholder" in item or not editor._can_inspect():
            return False
        spec = editor._component_specs.get(item["component_id"])
        return bool(spec and spec.outputs)

    def _action_items(self) -> list[dict]:
        editor = self._editor
        dashboard = self._dashboard()
        actions = []
        if dashboard is not None:
            refreshed = editor._refresh_label.object
            actions.append(
                {
                    "id": "action:refresh",
                    "title": "Refresh all tiles",
                    "subtitle": refreshed or "",
                    "icon": "refresh",
                    "keys": ["R"],
                }
            )
            if editor.editable:
                if not editor.read_only:
                    actions.append(
                        {
                            "id": "action:save",
                            "title": "Save dashboard",
                            "subtitle": "Unsaved changes"
                            if editor.dirty
                            else "No unsaved changes",
                            "icon": "save",
                            "keys": ["Mod", "S"],
                        }
                    )
                actions.append(
                    {
                        "id": "action:view",
                        "title": "Back to view mode",
                        "icon": "visibility",
                        "keys": ["Esc"],
                    }
                )
            elif self._can_edit():
                actions.append(
                    {
                        "id": "action:edit",
                        "title": "Edit dashboard",
                        "icon": "edit",
                        "keys": ["E"],
                    }
                )
            if not editor.read_only:
                actions.append(
                    {
                        "id": "action:settings",
                        "title": "Dashboard settings",
                        "icon": "tune",
                    }
                )
        dark = self._app._page.dark_theme
        actions.append(
            {
                "id": "action:theme",
                "title": f"Switch to {'light' if dark else 'dark'} theme",
                "icon": "light_mode" if dark else "dark_mode",
                "keywords": "toggle theme dark light mode",
            }
        )
        actions.append(
            {
                "id": "action:home",
                "title": "All dashboards",
                "subtitle": "Go to the home page",
                "icon": "home",
                "keywords": "home launcher",
            }
        )
        return [{**action, "group": "Actions"} for action in actions]

    # ------------------------------------------------------------------
    # Running items
    # ------------------------------------------------------------------

    def run(self, command: str):
        """Run the item or shortcut with id *command*, if it applies now."""
        # The app module imports this one, so its route constants are imported here.
        from panel_flowdash.app import DASH_ROUTE_PREFIX

        kind, _, value = command.partition(":")
        if kind == "shortcut":
            # A shortcut only does what the palette would offer in this state.
            if value in _SHORTCUT_ACTIONS and any(
                item["id"] == f"action:{value}" for item in self._action_items()
            ):
                self._run_action(value)
        elif kind == "dashboard":
            self._navigate(f"{DASH_ROUTE_PREFIX}{value}")
        elif kind == "component":
            self._run_component(value)
        elif kind == "inspect":
            self._inspect(value)
        elif kind == "expand":
            tile = self._editor._inspect_tiles.get(value)
            if tile is not None and callable(getattr(tile, "expand", None)):
                tile.expand()
        elif kind == "action":
            self._run_action(value)

    def _navigate(self, path: str):
        self._app._on_nav_click({"path": path})

    def _run_component(self, component_id: str):
        editor = self._editor
        if component_id not in editor._component_entries:
            return
        if self._editing():
            editor._add_from_ui(component_id)
        else:
            from panel_flowdash.app import COMPONENTS_ROUTE

            self._navigate(COMPONENTS_ROUTE)

    def _inspect(self, instance_id: str):
        editor = self._editor
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        tile = editor._inspect_tiles.get(instance_id)
        if item is None or tile is None or editor.editable or not self._can_inspect(item):
            return
        editor._on_inspect_clicked(instance_id, tile, None)

    def _run_action(self, action: str):
        app, editor = self._app, self._editor
        dashboard = self._dashboard()
        if action == "theme":
            app._page.dark_theme = not app._page.dark_theme
        elif action == "home":
            self._navigate("/")
        elif dashboard is None:
            return
        elif action == "refresh":
            editor.param.trigger("refresh")
        elif action == "edit" and not editor.editable and self._can_edit():
            editor._dashboard_bar.request_mode("edit")
        elif action == "view" and editor.editable:
            editor._dashboard_bar.request_mode("view")
        elif action == "save" and editor.editable:
            app._save_current_dashboard()
        elif action == "settings" and not editor.read_only:
            editor._settings_button.clicks += 1
