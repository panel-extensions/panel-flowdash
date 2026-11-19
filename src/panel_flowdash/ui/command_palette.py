"""The command palette: Ctrl/Cmd+K search over dashboards, components and actions, plus shortcuts."""

from __future__ import annotations

import functools
import logging
import typing as t

import panel as pn
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
    {"group": "Viewing", "label": "Full screen", "keys": ["F"]},
    {"group": "Viewing", "label": "Keyboard shortcuts", "keys": ["?"]},
    {"group": "Editing", "label": "Save", "keys": ["Mod", "S"]},
    {"group": "Editing", "label": "Undo", "keys": ["Mod", "Z"]},
    {"group": "Editing", "label": "Redo", "keys": ["Mod", "Shift", "Z"]},
    {"group": "Editing", "label": "Dataflow and Layout side by side", "keys": ["S"]},
    {"group": "Editing", "label": "Back to view mode", "keys": ["Esc"]},
]

# Shortcuts the browser sends, each running the action of the same name.
_SHORTCUT_ACTIONS = frozenset({"refresh", "edit", "view", "save", "split", "undo", "redo"})


class CommandPalette(JSComponent):
    """A search field that opens a command palette, and the app's keyboard shortcuts.

    The field and the dialog are rendered in the browser, which also matches
    and ranks the results. When the palette opens it asks *on_open* for
    :attr:`items`, so the server sends the list once per opening, and a
    chosen item comes back as its ``id`` through *on_command*.

    Shortcuts are registered on the document and ignored while typing in a
    text field: Ctrl/Cmd+K opens the palette, ``?`` lists the shortcuts, and
    R (refresh), E (edit), Esc (back to view, outside dialogs), S (split view,
    in edit mode), Ctrl/Cmd+S (save, in edit mode) and Ctrl/Cmd+Z and
    Ctrl/Cmd+Shift+Z or Ctrl+Y (undo and redo, in edit mode) are sent to
    *on_command* as ``shortcut:<name>``. F and the ``action:fullscreen`` item toggle the
    dashboard's full screen in the browser, which only grants it during the
    key press or click.
    """

    items = param.List(
        default=[],
        doc="""
        Palette entries, each a dict with ``id``, ``group``, ``title`` and
        optionally ``subtitle``, ``icon``, ``keywords``, ``keys`` (shortcut
        chips) and ``hint`` (text on the right).""",
    )

    compact = param.Boolean(
        default=False,
        doc="""
        Whether the search field shrinks to an icon button unless the window
        is very wide, leaving the top bar to a dashboard's actions.""",
    )

    placeholder = param.String(default=PLACEHOLDER, doc="Text in the search field.")

    ask_label = param.String(
        default=None,
        allow_None=True,
        doc="""
        When set, a query is also offered as a question for an assistant, in
        an item titled with this label and the query. It comes first when the
        query reads like a sentence or matches nothing, and runs as
        ``ask:<query>``.""",
    )

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
        self._ask: t.Callable[[str], None] | None = None
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

    @property
    def ask(self) -> t.Callable[[str], None] | None:
        """Called with the query when the user asks the assistant from the palette."""
        return self._ask

    @ask.setter
    def ask(self, ask: t.Callable[[str], None] | None):
        self._ask = ask
        self.palette.ask_label = "Ask the assistant" if ask is not None else None

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
            refreshed = editor._dashboard_bar.refresh_text
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
                    actions.extend(self._history_items())
                splitting = editor.mode == "split"
                actions.append(
                    {
                        "id": "action:split",
                        "title": "Close split view" if splitting else "Split view",
                        "subtitle": "Back to one tab"
                        if splitting
                        else "Dataflow and Layout side by side",
                        "icon": "vertical_split",
                        "keywords": "split side by side canvas grid layout dataflow",
                        "keys": ["S"],
                    }
                )
                if not editor.read_only and editor._tile_items:
                    actions.append(
                        {
                            "id": "action:arrange_canvas",
                            "title": "Arrange dataflow",
                            "subtitle": "Lay out the nodes from sources to views",
                            "icon": "account_tree",
                            "keywords": "arrange layout tidy auto canvas graph nodes",
                        }
                    )
                    actions.append(
                        {
                            "id": "action:arrange_layout",
                            "title": "Arrange layout",
                            "subtitle": "Lay out the tiles by kind for every screen size",
                            "icon": "dashboard",
                            "keywords": "arrange layout tidy auto tiles grid",
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
            else:
                if self._can_edit():
                    actions.append(
                        {
                            "id": "action:edit",
                            "title": "Edit dashboard",
                            "icon": "edit",
                            "keys": ["E"],
                        }
                    )
                actions.append(
                    {
                        "id": "action:fullscreen",
                        "title": "Exit full screen" if editor.fullscreen else "Full screen",
                        "subtitle": ""
                        if editor.fullscreen
                        else "Show only the tiles and controls",
                        "icon": "fullscreen_exit" if editor.fullscreen else "fullscreen",
                        "keys": ["F"],
                        "keywords": "present presentation kiosk wall display maximize",
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

    def _history_items(self) -> list[dict]:
        """Undo and Redo, while there is something to undo or redo."""
        editor = self._editor
        items = []
        if editor.can_undo:
            items.append(
                {
                    "id": "action:undo",
                    "title": "Undo",
                    "subtitle": editor.edit_history.undo_label,
                    "icon": "undo",
                    "keys": ["Mod", "Z"],
                    "keywords": "undo revert back history",
                }
            )
        if editor.can_redo:
            items.append(
                {
                    "id": "action:redo",
                    "title": "Redo",
                    "subtitle": editor.edit_history.redo_label,
                    "icon": "redo",
                    "keys": ["Mod", "Shift", "Z"],
                    "keywords": "redo again forward history",
                }
            )
        return items

    def _step_history(self, action: str):
        """Undo or redo, importing the components the version needs off the event loop first."""
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self._apply_step(action)
            return

        async def step():
            editor = self._editor
            from panel_flowdash.history import components_of

            target = editor.edit_history.step_value(action)
            if target is not None:
                await editor.ensure_components_loaded_async(components_of(target))
            # Applied locked to avoid the unlocked hold race fixed in holoviz/panel#9117.
            pn.state.execute(functools.partial(self._apply_step, action), schedule=True)

        pn.state.execute(step)

    def _apply_step(self, action: str):
        from panel_flowdash.history import HistoryBusy

        editor = self._editor
        try:
            done = editor.undo() if action == "undo" else editor.redo()
        except (PermissionError, HistoryBusy) as exc:
            editor._notify("warning", str(exc), duration=5000)
            return
        if not done:
            editor._notify("info", f"Nothing to {action}.", duration=2000)

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
        elif kind == "ask" and self._ask is not None:
            self._ask(value)

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
        elif action == "split" and editor.editable:
            editor.toggle_split()
        elif action == "arrange_canvas" and editor.editable and not editor.read_only:
            editor.arrange_canvas()
        elif action == "arrange_layout" and editor.editable and not editor.read_only:
            editor.arrange_layout()
        elif action == "save" and editor.editable:
            app._save_current_dashboard()
        elif action in ("undo", "redo") and editor.editable and not editor.read_only:
            self._step_history(action)
        elif action == "fullscreen" and not editor.editable:
            editor.fullscreen = not editor.fullscreen
        elif action == "settings" and not editor.read_only:
            editor._settings_button.clicks += 1
