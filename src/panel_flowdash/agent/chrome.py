"""Fits the agent's drawer into the rest of the app: the inspector, full screen and the palette."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.ui.text import escape_markdown, text_typography

if t.TYPE_CHECKING:
    from .session import AgentSession

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

    - While the drawer is open the editor's inspector is folded, so the canvas
      keeps its width; it opens again when the drawer closes, unless the user
      opened or closed it in the meantime.
    - In full screen the drawer is out of sight, so a pending approval shows a
      notice whose Review button leaves full screen and opens the drawer.
    - The command palette offers to ask the assistant whatever was typed. A
      question asked while the assistant is busy is sent once it is done.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        self._folded = False
        self._expected: bool | None = None
        self._pending_seen = len(session.runner.state.pending_approvals)
        self._queued: list[str] = []
        drawer = session.drawer
        drawer.param.watch(self._on_open, "open")
        state = session.runner.state
        state.param.watch(self._on_pending, "pending_approvals")
        state.param.watch(self._on_running, "running")

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
                on_click=lambda e: self.notice.param.update(visible=False),
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
            editor._inspector.param.watch(self._on_user_toggle, "collapsed")
            # Entering edit mode with the drawer open shows the inspector beside it.
            editor._inspector.param.watch(self._on_inspector_shown, "visible")

        palette = session.app._command_palette
        if palette is not None:
            palette.ask = self.ask

    # ------------------------------------------------------------------
    # Asking from the palette
    # ------------------------------------------------------------------

    def ask(self, text: str):
        """Open the drawer on the chat and send *text* to the assistant."""
        text = text.strip()
        if not text:
            return
        drawer = self.session.drawer
        drawer.param.update(open=True, tab="chat")
        if self.session.runner.state.running:
            self._queued.append(text)
            return
        self._submit(text)

    def _submit(self, text: str):
        chat = self.session.drawer.chat

        async def submit():
            await chat.submit(text)

        pn.state.execute(submit)

    def _on_running(self, event):
        if not event.new and self._queued:
            self._submit(self._queued.pop(0))

    # ------------------------------------------------------------------
    # Full screen
    # ------------------------------------------------------------------

    def review(self):
        """Leave full screen and open the drawer on the pending approvals."""
        self.notice.visible = False
        editor = self.session.app._editor
        if editor is not None:
            editor.fullscreen = False
        self.session.drawer.param.update(open=True, tab="chat")

    def _on_pending(self, event):
        pending = len(event.new)
        editor = self.session.app._editor
        if pending > self._pending_seen and editor is not None and editor.fullscreen:
            self._show_notice(event.new[-1])
        elif not pending:
            self.notice.visible = False
        self._pending_seen = pending

    def _show_notice(self, request):
        self._notice_text.object = escape_markdown(
            f"The assistant is waiting for your approval: {_summary(request)}"
        )
        self.notice.visible = True

    def _on_fullscreen(self, event):
        pending = self.session.runner.state.pending_approvals
        if event.new and pending:
            self._show_notice(pending[-1])
        elif not event.new:
            self.notice.visible = False

    # ------------------------------------------------------------------
    # The inspector
    # ------------------------------------------------------------------

    def _on_inspector_shown(self, event):
        if event.new and self.session.drawer.open:
            self._fold(self.session.app._editor)

    def _fold(self, editor):
        inspector = editor._inspector
        if inspector.visible and not inspector.collapsed:
            self._set_collapsed(editor, True)
            self._folded = True

    def _on_open(self, event):
        editor = self.session.app._editor
        inspector = getattr(editor, "_inspector", None)
        if inspector is None:
            return
        if event.new:
            self._fold(editor)
        elif self._folded:
            if inspector.collapsed:
                self._set_collapsed(editor, False)
            self._folded = False

    def _set_collapsed(self, editor, collapsed: bool):
        self._expected = collapsed
        editor._inspector.collapsed = collapsed

    def _on_user_toggle(self, event):
        if event.new != self._expected:
            # The user (or the split view) opened or closed it: leave it as it is.
            self._folded = False
