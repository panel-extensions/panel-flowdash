"""Questions the assistant's drawer offers to start a new dashboard with."""

from __future__ import annotations

import asyncio
import inspect
import logging
import typing as t

import panel_material_ui as pmui

from panel_flowdash.ui.text import text_typography

if t.TYPE_CHECKING:
    from .ops import DashboardOps
    from .session import AgentSession

logger = logging.getLogger(__name__)

#: How many starter questions the drawer offers at most.
STARTER_LIMIT = 3

# Each question is about one table, the tables taken in turn.
_PHRASES = (
    "What's in {table}?",
    "Add a table of {table}",
    "Chart the key numbers in {table}",
)

#: The CSS class of the starter questions in the drawer, and of each question's button.
STARTERS_CLASS = "flowdash-starters"
STARTER_CLASS = "flowdash-starter"


def data_tables(editor) -> list[str]:
    """Return the titles of the tables the person may place, from the palette's data sources.

    A data source with palette presets offers each preset, a concrete
    table, rather than the source itself. Nothing is imported or loaded.
    """
    from panel_flowdash.ui.palette import editor_presets, palette_entry

    titles: list[str] = []
    for component_id, entry in editor._component_entries.items():
        if not editor._component_offered(component_id):
            continue
        base = palette_entry(entry, editor._component_specs.get(component_id))
        if base.role != "source":
            continue
        presets = editor_presets(editor, entry)
        for title in [preset["title"] for preset in presets] or [base.title]:
            if title and title not in titles:
                titles.append(title)
    return titles


def default_starter_questions(ops: DashboardOps, limit: int = STARTER_LIMIT) -> list[str]:
    """Return up to *limit* questions about the tables the person may place, see `data_tables`.

    The questions take the tables in turn, so with several each is asked
    about once before any twice.
    """
    tables = data_tables(ops.editor)
    if not tables:
        return []
    return [
        _PHRASES[i % len(_PHRASES)].format(table=tables[i % len(tables)])
        for i in range(min(limit, max(len(tables), len(_PHRASES))))
    ]


class StarterQuestions:
    """Starter questions in the assistant's drawer while a new dashboard has had no request.

    Clicking one asks the assistant. They go once the assistant gets a
    request, another dashboard opens or edit mode ends, and show only on
    the drawer's chat.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        self.questions: list[str] = []
        self._dashboard: str | None = None
        self._task: asyncio.Task | None = None
        self._list = pmui.Column(margin=0, sizing_mode="stretch_width")
        self.view = pmui.Column(
            text_typography(
                "Start with a question",
                variant="caption",
                margin=(0, 0, 2, 2),
                sx={"color": "text.secondary"},
            ),
            self._list,
            visible=False,
            margin=(10, 12, 2, 12),
            sizing_mode="stretch_width",
            css_classes=[STARTERS_CLASS],
        )
        editor = session.app._editor
        session.runner.state.param.watch(self._on_running, "running")
        editor.param.watch(self._on_editor, ["editable", "read_only", "dashboard"])
        session.drawer.param.watch(lambda _event: self._sync(), "tab")

    @property
    def shown(self) -> bool:
        """Whether the questions are offered."""
        return bool(self.questions)

    def offer(self, hook: t.Callable[[DashboardOps], t.Any] | None = None):
        """Offer the starter questions for the open dashboard, from *hook* or `default_starter_questions`.

        A hook may return its questions or an awaitable of them. One that
        fails is logged and offers none.
        """
        self.clear()
        editor = self.session.app._editor
        if editor.dashboard is None or editor.read_only or self.session.runner.state.running:
            return
        dashboard = editor.dashboard.dashboard_id
        try:
            found = (hook or default_starter_questions)(self.session.ops)
        except Exception:
            logger.exception("The starter questions hook failed")
            return
        if inspect.isawaitable(found):
            self._dashboard = dashboard
            self._task = task = asyncio.ensure_future(found)
            task.add_done_callback(lambda done: self._arrived(done, dashboard))
            return
        self._show(found, dashboard)

    def _arrived(self, task: asyncio.Future, dashboard: str):
        if task.cancelled() or task is not self._task:
            return
        self._task = None
        if task.exception() is not None:
            logger.error("The starter questions hook failed", exc_info=task.exception())
            return
        if self._dashboard == dashboard:
            self._show(task.result(), dashboard)

    def _show(self, questions, dashboard: str):
        questions = [q.strip() for q in questions or () if isinstance(q, str) and q.strip()]
        self._dashboard = dashboard
        self.questions = questions[:STARTER_LIMIT]
        self._list.objects = [self._button(question) for question in self.questions]
        self._sync()

    def _button(self, question: str) -> pmui.Button:
        button = pmui.Button(
            label=question,
            icon="chat_bubble_outline",
            variant="outlined",
            size="small",
            margin=(0, 0, 6, 0),
            sizing_mode="stretch_width",
            css_classes=[STARTER_CLASS],
            sx={
                "textTransform": "none",
                "justifyContent": "flex-start",
                "textAlign": "left",
                "fontWeight": 400,
            },
        )
        button.on_click(lambda _event: self.ask(question))
        return button

    def ask(self, question: str):
        """Ask the assistant *question*, as clicking it does."""
        self.clear()
        self.session.chrome.ask(question)

    def clear(self):
        """Stop offering the questions."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._dashboard = None
        self.questions = []
        if self._list.objects:
            self._list.objects = []
        self._sync()

    def _sync(self):
        visible = self.shown and self.session.drawer.tab == "chat"
        if self.view.visible != visible:
            self.view.visible = visible

    def _on_running(self, event):
        if event.new:
            self.clear()

    def _on_editor(self, *_events):
        editor = self.session.app._editor
        current = editor.dashboard.dashboard_id if editor.dashboard is not None else None
        if self._dashboard is not None and (
            not editor.editable or editor.read_only or current != self._dashboard
        ):
            self.clear()
