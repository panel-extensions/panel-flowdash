"""Fits the agent's drawer into the rest of the app: the inspector, full screen and the palette."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.ui.text import escape_markdown, text_typography

if t.TYPE_CHECKING:
    from .session import AgentSession

#: Below this window width the drawer overlays the app instead of narrowing it.
OVERLAY_BELOW = 1200


def _node(value: t.Any) -> str:
    return str(value or "").split(".")[0] or "a node"


def _keys(config: t.Any) -> str:
    return ", ".join(map(str, config)) if isinstance(config, dict) and config else "its config"


_EXPORT_NAMES = {
    "html": "a web page",
    "pdf": "a PDF",
    "docx": "a Word document",
    "md": "Markdown",
    "ipynb": "a notebook",
}

#: The assistant's steps in the user's words, by tool name. Imperative, so a label
#: reads right whether the step is running, done, failed or waiting for approval.
TOOL_LABELS: dict[str, str | t.Callable[[dict], str]] = {
    "list_components": "Look up components",
    "list_dashboards": "List dashboards",
    "get_dashboard": "Read the dashboard",
    "open_dashboard": "Open a dashboard",
    "validate_dashboard": "Check the dashboard",
    "preview_dashboard": "Preview the dashboard",
    "add_node": lambda a: f"Add {str(a.get('component', 'a component')).split('/')[-1]}",
    "remove_node": lambda a: f"Remove {a['name']}",
    "set_config": lambda a: (
        f"Change {a['name']}: {_keys({**(a.get('config') or {}), **(a.get('inputs') or {})})}"
    ),
    "connect": lambda a: f"Connect {_node(a.get('source'))} to {_node(a.get('target'))}",
    "disconnect": lambda a: f"Disconnect {_node(a.get('source'))} from {_node(a.get('target'))}",
    "set_transform": lambda a: (
        f"Change the steps from {_node(a.get('source'))} to {_node(a.get('target'))}"
    ),
    "place_tile": lambda a: f"Place the tile of {a['name']}",
    "arrange_canvas": "Arrange the canvas",
    "arrange_layout": "Arrange the tile layout",
    "arrange_tiles": "Arrange the tile layout",
    "group_nodes": lambda a: f"Group {', '.join(a.get('nodes') or [])}",
    "ungroup": lambda a: f"Ungroup {a['name']}",
    "set_settings": lambda a: f"Change the settings: {_keys(a.get('settings'))}",
    "set_title": lambda a: f"Title the dashboard {a.get('title')}",
    "add_filter": lambda a: f"Add a filter on {a.get('label') or a.get('column')}",
    "remove_filter": lambda a: f"Remove the filter on {a.get('column')}",
    "declare_parameter": lambda a: f"Make {a.get('node')}.{a.get('field')} a parameter",
    "remove_parameter": lambda a: f"Remove parameter {a.get('name')}",
    "replace_dashboard": "Rewrite the dashboard",
    "get_report": "Read the report",
    "report_text": "Read the report",
    "edit_report": "Edit the report",
    "set_report": "Write the report",
    "ask_in_report": "Add a question to the report",
    "run_pending": "Answer a question of the report",
    "export_report": lambda a: (
        f"Export the report as {_EXPORT_NAMES.get(str(a.get('format', 'html')), 'a file')}"
    ),
    "save_dashboard": "Save the dashboard",
    "delete_dashboard": lambda a: f"Delete {a['title']}",
    "share_dashboard": "Change who can see the dashboard",
    "list_templates": "Look up dashboard templates",
    "new_dashboard_from_template": lambda a: (
        "Start a dashboard from the "
        f"{str(a.get('template') or 'chosen').split(':')[-1].replace('-', ' ')} template"
    ),
    "list_commands": "Look up commands",
    "run_command": lambda a: f"Run {a['command']}",
}

_NOTICE_STYLES = {
    "position": "fixed",
    "left": "50%",
    "bottom": "24px",
    "transform": "translateX(-50%)",
    "z-index": "1400",
    "align-items": "center",
    "gap": "8px",
    "padding": "4px 6px 4px 16px",
    "border-radius": "6px",
    "background": "var(--mui-palette-grey-900, #323232)",
    "color": "#fff",
    "box-shadow": "0 6px 20px rgba(0, 0, 0, 0.3)",
}


def _summary(request) -> str:
    """Return the first sentence of an approval's description, or its tool's name."""
    text = (getattr(request, "description", None) or "").strip().split("\n")[0]
    text = text.split(". ")[0].rstrip(".")
    if not text:
        return request.tool_name
    return text if len(text) <= 80 else text[:79] + "…"


class SessionChrome:
    """Connects a session's drawer to the parts of the app it shares the screen with.

    - Below :data:`OVERLAY_BELOW` pixels the drawer overlays the app, and the
      assistant's steps are named with :data:`TOOL_LABELS`.
    - The drawer's state is reported to the editor's sidecar, which decides
      whether the two fit side by side (see :mod:`panel_flowdash.ui.sidecar`)
      and closes the drawer when the person opens the sidecar in a window
      where they share one slot.
    - An approval that waits on a surface out of sight shows a notice whose
      Review button brings that surface back: one of the assistant's in full
      screen or while Explore or Ask's feed is shown opens the drawer in the
      feed's place, and one of the Explore agent's, asked before leaving
      Explore, shows Explore again.
    - The command palette offers to ask the assistant whatever was typed. A
      question asked while the assistant is busy is sent once it is done.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        # Which surface the notice's Review button brings back: "drawer" or "explore".
        self._review_target = "drawer"
        # The approvals the notice tells of, and those whose notice the person dismissed.
        self._shown: set[str] = set()
        self._dismissed: set[str] = set()
        # Questions asked while the assistant is busy, each with the pending turn it answers, if any.
        self._queued: list[tuple[str, str | None]] = []
        drawer = session.drawer
        drawer.overlay_below = OVERLAY_BELOW
        drawer.chat.tool_labels = TOOL_LABELS
        drawer.param.watch(self._on_open, "open")
        state = session.runner.state
        state.param.watch(self._on_pending, "pending_approvals")
        state.param.watch(self._on_running, "running")
        explore = session.explore_runner
        if explore is not None:
            explore.state.param.watch(self._on_explore_pending, "pending_approvals")

        # The description may come from a third-party tool: show it as plain text.
        self._notice_text = text_typography("", variant="body2", margin=0, sx={"color": "inherit"})
        self.notice = pmui.Row(
            self._notice_text,
            pmui.Button(
                label="Review",
                variant="text",
                size="small",
                margin=0,
                sx={"color": "#90caf9"},
                on_click=lambda e: self.review(),
            ),
            pmui.IconButton(
                icon="close",
                description="Dismiss",
                size="small",
                margin=0,
                sx={"color": "inherit"},
                on_click=lambda e: self.dismiss(),
            ),
            visible=False,
            margin=0,
            styles=_NOTICE_STYLES,
        )
        editor = session.app._editor
        if editor is not None:
            # Inside the editor, so the notice is on screen when the editor is in full screen.
            editor._view.append(self.notice)
            editor.param.watch(self._on_fullscreen, "fullscreen")
            editor.param.watch(self._on_explore, ["explore", "ask", "editable"])
            editor._sidecar.on_close_drawer = lambda: setattr(drawer, "open", False)
            editor._split_view.storage.param.watch(
                lambda _e: self._report_drawer(), "window_width"
            )
            self._report_drawer()

        palette = session.app._command_palette
        if palette is not None:
            palette.ask = self.ask

    # ------------------------------------------------------------------
    # Asking from the palette
    # ------------------------------------------------------------------

    def ask(self, text: str, turn: str | None = None):
        """Open the drawer on the chat and send *text* to the assistant.

        With *turn*, the id of a pending turn asking *text*, the request answers that turn.
        """
        text = text.strip()
        if not text:
            return
        drawer = self.session.drawer
        drawer.param.update(open=True, tab="chat")
        if self.session.runner.state.running:
            self._queued.append((text, turn))
            return
        self._submit(text, turn)

    def _submit(self, text: str, turn: str | None = None):
        chat = self.session.drawer.chat
        runner = self.session.runner

        async def submit():
            runner.answering = turn
            try:
                await chat.submit(text)
            finally:
                runner.answering = None
                if turn is not None:
                    self.session.pending_done(turn)

        pn.state.execute(submit)

    def _on_running(self, event):
        if not event.new and self._queued:
            self._submit(*self._queued.pop(0))

    # ------------------------------------------------------------------
    # Full screen
    # ------------------------------------------------------------------

    def review(self):
        """Leave full screen and bring back the surface whose approval is pending.

        The drawer opened in view mode turns Ask off, so the docked feed makes room for it.
        """
        self.notice.visible = False
        editor = self.session.app._editor
        if editor is not None:
            editor.fullscreen = False
        if self._review_target == "explore":
            if editor is not None:
                editor.show_ask()
            return
        if editor is not None:
            editor.explore = False
        self.session.drawer.param.update(open=True, tab="chat")

    def _drawer_hidden(self) -> bool:
        editor = self.session.app._editor
        return editor is not None and (editor.fullscreen or editor.explore or editor.ask_docked)

    def _explore_hidden(self) -> bool:
        editor = self.session.app._editor
        return editor is not None and (editor.fullscreen or not editor.feed_shown)

    def _on_pending(self, event):
        self._refresh_notice()

    def _on_explore_pending(self, event):
        self._refresh_notice()

    def dismiss(self):
        """Hide the notice until another approval waits; those it told of are not shown again."""
        self._dismissed.update(self._shown)
        self.notice.visible = False

    def _show_notice(self, waiting: list, target: str = "drawer"):
        who = "The assistant" if target == "drawer" else "Explore"
        self._review_target = target
        self._shown = {request.tool_call_id for request in waiting}
        n = len(waiting)
        text = (
            f"{who} is waiting for your approval: {_summary(waiting[-1])}"
            if n == 1
            else f"{who} is waiting for {n} approvals, the latest: {_summary(waiting[-1])}"
        )
        self._notice_text.object = escape_markdown(text)
        self.notice.visible = True

    def _refresh_notice(self):
        """Show the notice for approvals waiting out of sight and not dismissed, or hide it."""
        pending = {request.tool_call_id for request in self._waiting(self.session.runner)}
        explore = self.session.explore_runner
        if explore is not None:
            pending |= {request.tool_call_id for request in self._waiting(explore)}
        # Decided approvals are forgotten, so their ids never hide a later notice.
        self._dismissed &= pending
        drawer = [
            r for r in self._waiting(self.session.runner) if r.tool_call_id not in self._dismissed
        ]
        others = (
            [r for r in self._waiting(explore) if r.tool_call_id not in self._dismissed]
            if explore is not None
            else []
        )
        if drawer and self._drawer_hidden():
            self._show_notice(drawer)
        elif others and self._explore_hidden():
            self._show_notice(others, "explore")
        else:
            self.notice.visible = False

    @staticmethod
    def _waiting(runner) -> list:
        return list(runner.state.pending_approvals)

    def _on_fullscreen(self, event):
        self._refresh_notice()

    def _on_explore(self, event):
        self._refresh_notice()

    # ------------------------------------------------------------------
    # The sidecar
    # ------------------------------------------------------------------

    def _report_drawer(self):
        editor = self.session.app._editor
        sidecar = getattr(editor, "_sidecar", None)
        if sidecar is None:
            return
        drawer = self.session.drawer
        window = editor._split_view.storage.window_width
        # Below its overlay width the drawer covers the page rather than taking from it.
        pushes = window is None or window >= (drawer.overlay_below or 0)
        sidecar.drawer_changed(drawer.open, drawer.width if pushes else 0)

    def _on_open(self, event):
        self._report_drawer()
