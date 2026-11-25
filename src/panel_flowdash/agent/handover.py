"""Hands what a person adds from Ask in view mode over to the assistant in edit mode.

Explore, Ask's feed in view mode, and the assistant, Ask in edit mode, keep
separate conversations. Adding an answer from a card in view mode switches
to edit mode, and that is the one place where the two meet: the new tile is
shown on the Layout tab, marked and scrolled into view, a notice offers to
undo the add in one step, and the assistant's input mentions the new node,
so the next instruction is about it.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.history import HistoryBusy
from panel_flowdash.ui.dashboard_bar import PHONE_BREAKPOINT
from panel_flowdash.ui.settled_scroll import SettledScroll
from panel_flowdash.ui.text import escape_markdown, text_typography
from panel_flowdash.value_text import error_text

from .chrome import _NOTICE_STYLES
from .highlight import TILE_CLASSES

if t.TYPE_CHECKING:
    from .session import AgentSession

# On a phone the notice clears the Layout tab's floating Arrange tiles button, whose top is
# 108 px above the bottom edge. In the dark theme it is lighter than the tiles under it.
_NOTICE_CSS = f"""
@media (max-width: {PHONE_BREAKPOINT - 0.05}px) {{ :host {{ bottom: 120px !important; }} }}
:host-context(.dark), :host-context([data-mui-color-scheme="dark"]) {{
  background: var(--mui-palette-grey-800, #424242) !important;
  border: 1px solid rgba(255, 255, 255, 0.16);
}}
"""

UNDO_BUSY = "Undo is available once the assistant finishes."

#: The icon of a mention by the role of the node's component, when the component names none.
_ROLE_ICONS = {
    "view": "bar_chart",
    "widget": "tune",
    "source": "table_chart",
    "transform": "table_chart",
}


class AddHandover:
    """Shows an item just added from Ask in view mode and hands it to the assistant.

    :meth:`hand_over` is called once the add has switched to edit mode. It
    shows the node's tile on the Layout tab, marked as added and scrolled into
    view, mentions the node in the assistant's input and opens the drawer
    beside the tiles. On a phone, where the drawer would cover them, the
    notice offers to open it instead.

    The notice's Undo takes the add back and returns to view mode, where Ask
    shows the card again, and to the surface the add came from, such as the
    Explore view. It is offered until the dashboard changes again, the
    person dismisses it or edit mode ends. The mention stays until it is
    sent, removed or edit mode ends.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        #: The name of the node handed over, while its Undo is offered.
        self.name: str | None = None
        # The node mentioned in the assistant's input, until edit mode ends.
        self._mentioned: str | None = None
        # The revision of the canvas before the add, if it had no unsaved changes then.
        self._saved: str | None = None
        # The view surface the add came from, shown again when edit mode ends.
        self._surface: str | None = None
        # The name gives way to the actions, cut short with an ellipsis.
        self._text = text_typography(
            "",
            variant="body2",
            margin=0,
            sx={
                "color": "inherit",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
                "whiteSpace": "nowrap",
            },
            styles={"flex": "1 1 auto", "min-width": "0", "overflow": "hidden"},
        )
        self.undo_button = pmui.Button(
            label="Undo",
            variant="text",
            size="small",
            margin=0,
            sx={"color": "#90caf9", "&.Mui-disabled": {"color": "rgba(255, 255, 255, 0.5)"}},
            styles={"flex-shrink": "0"},
            on_click=lambda _event: self.undo(),
        )
        self.assistant_button = pmui.Button(
            label="Assistant",
            variant="text",
            size="small",
            margin=0,
            visible=False,
            sx={"color": "#90caf9"},
            styles={"flex-shrink": "0"},
            on_click=lambda _event: self.open_assistant(),
        )
        self.notice = pmui.Row(
            self._text,
            self.undo_button,
            self.assistant_button,
            pmui.IconButton(
                icon="close",
                description="Dismiss",
                size="small",
                margin=0,
                sx={"color": "inherit"},
                styles={"flex-shrink": "0"},
                on_click=lambda _event: self.dismiss(),
            ),
            visible=False,
            margin=0,
            styles={**_NOTICE_STYLES, "max-width": "calc(100vw - 32px)", "flex-wrap": "nowrap"},
            stylesheets=[_NOTICE_CSS],
            css_classes=["fd-added-notice"],
        )
        self._scroll = SettledScroll()
        editor = session.app._editor
        editor._view.extend([self.notice, self._scroll])
        editor.param.watch(self._on_editable, "editable")
        editor.param.watch(self._on_edited, "edited")
        editor.param.watch(self._on_dashboard, "dashboard")
        editor.param.watch(self._on_saved, "saved")
        editor.param.watch(self._on_surface, "surface")
        session.drawer.param.watch(self._place_notice, ["open", "width"])
        # Undo waits while an agent's request runs, as the edit history does.
        for runner in session.runners:
            runner.state.param.watch(lambda _event: self._sync_undo(), "running")

    @property
    def _editor(self):
        return self.session.app._editor

    @pn.io.hold()
    def hand_over(self, name: str, surface: str = "workspace", saved: str | None = None):
        """Show the dashboard node *name*, just added in view mode, and hand it to the assistant.

        *surface* is the one the add came from, shown again when edit mode
        ends, and *saved* the revision of the canvas before the add, if it
        had no unsaved changes then.
        """
        editor = self._editor
        phone = editor._phone
        self._surface = None
        self.session.show_answer(name)
        self._scroll.scroll_to(TILE_CLASSES["add"])
        self.name, self._saved = name, saved
        self._surface = surface if surface != "workspace" else None
        self._mention(name)
        # A phone has room for the name and the actions only.
        self._text.object = escape_markdown(
            f"Added {name}" if phone else f"Added {name} to the dashboard"
        )
        # Only the edit history takes back the add alone.
        self.undo_button.visible = editor.edit_history.active
        self._sync_undo()
        self.assistant_button.visible = phone
        self.notice.visible = True
        if phone:
            # The drawer covers the page on a phone, so the tile stays in sight until asked for.
            editor.ask = False
        else:
            self.open_assistant()
        self._place_notice()

    def waiting(self, waiting: str | None):
        """Say on the notice that the add waits for *waiting*, e.g. "the assistant's edit", or stop.

        The Explore feed says so itself while it is on screen.
        """
        if waiting is not None:
            self.name = None
            self._text.object = escape_markdown(f"Waiting for {waiting} to finish…")
            for button in (self.undo_button, self.assistant_button):
                button.visible = False
            self.notice.visible = True
            self._place_notice()
        elif self.name is None:
            self.notice.visible = False

    def open_assistant(self):
        """Open the assistant's drawer on its chat, where the node is mentioned."""
        self._editor.ask = True
        self.session.drawer.param.update(open=True, tab="chat")

    def undo(self):
        """Take the add back and return to view mode, unless the dashboard has other unsaved changes."""
        editor = self._editor
        history = editor.edit_history
        if self.name is None or not history.active:
            return
        saved = self._saved
        try:
            undone = editor.undo()
        except HistoryBusy:
            self.session.app._notify("info", UNDO_BUSY, duration=5000)
            return
        except PermissionError as exc:
            self.session.app._notify("error", f"Could not undo: {error_text(exc)}", duration=8000)
            return
        # Back as it was saved, give or take the layout defaults a restore writes out.
        if undone and saved is not None and history.snapshot()[1] == saved:
            editor.dirty = False
        self._end()
        if editor.editable and not editor.dirty:
            editor._dashboard_bar.request_mode("view")

    def _sync_undo(self):
        busy = self._editor.edit_history.busy
        self.undo_button.param.update(disabled=busy, description=UNDO_BUSY if busy else None)

    def dismiss(self):
        """Hide the notice, keeping the add and the mention."""
        self.notice.visible = False
        self.name = None

    def _place_notice(self, *_events):
        """Centre the notice over the page beside the drawer.

        A phone's drawer covers the page, and the notice would cover its input.
        """
        drawer = self.session.drawer
        if drawer.open and self._editor._phone:
            self.dismiss()
        width = drawer.width if drawer.open and not self._editor._phone else 0
        self.notice.styles = {**self.notice.styles, "left": f"calc((100vw - {width}px) / 2)"}

    def _mention(self, name: str):
        chat = self.session.drawer.chat
        # panel-agents' mentions are newer than its oldest supported release.
        if "mentions" in chat.param:
            from panel_agents import Mention

            self._mentioned = name
            chat.mentions = [Mention(name, icon=self._icon(name))]

    def _forget_mention(self, name: str):
        chat = self.session.drawer.chat
        chat.mentions = [mention for mention in chat.mentions if mention.label != name]

    def _icon(self, name: str) -> str | None:
        editor = self._editor
        ids = {node: iid for iid, node in self.session.ops._names().items()}
        item = next((i for i in editor._tile_items if i["instance_id"] == ids.get(name)), None)
        if item is None:
            return None
        entry = editor._component_entries.get(item["component_id"])
        icon = entry.metadata.icon if entry is not None else None
        return icon or _ROLE_ICONS.get(editor._node_role(item["component_id"]))

    def _end(self):
        """Stop offering the Undo and drop the mention, which the assistant no longer needs."""
        self.dismiss()
        name, self._mentioned = self._mentioned, None
        if name is not None:
            self._forget_mention(name)

    def _on_dashboard(self, event):
        # Another dashboard, or this one loaded again, e.g. on discarding, ends the handover;
        # saving or an undo replaces the model of the same dashboard and leaves it.
        old, new = (getattr(model, "dashboard_id", None) for model in (event.old, event.new))
        editor = self._editor
        if old != new or new is None or (editor._hydrating and not editor._history_quiet):
            self._end()

    def _on_saved(self, _event):
        # The add is stored now, so Undo leaves it as an unsaved change to keep or save.
        self._saved = None

    def _on_edited(self, _event):
        # Another edit: one Undo would no longer take back just the add.
        if self.name is not None:
            self.dismiss()

    def _on_surface(self, _event):
        # The person went elsewhere, so ending edit mode keeps them there.
        self._surface = None

    def _on_editable(self, event):
        if event.new:
            return
        surface, self._surface = self._surface, None
        self._end()
        if surface is not None and self._editor.surface == "workspace":
            self._editor.surface = surface
