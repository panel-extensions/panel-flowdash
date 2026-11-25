"""Questions the assistant's drawer offers to start a new dashboard with."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import typing as t
from dataclasses import dataclass

import panel as pn
import panel_material_ui as pmui
from panel.io.state import set_curdoc

from panel_flowdash.ui.text import text_typography

if t.TYPE_CHECKING:
    from panel_flowdash.auth import Identity

    from .session import AgentSession

logger = logging.getLogger(__name__)

#: How many starter questions the drawer offers at most.
STARTER_LIMIT = 3

#: Seconds an awaitable hook, or the palette presets a new dashboard waits for, may take.
STARTER_TIMEOUT = 10

# How many items of a hook's result are looked at for valid questions at most.
_SCAN_LIMIT = 50

# Each question is about one table, the tables taken in turn. A dashboard from a
# template has its nodes already, so it is not offered to add one.
_PHRASES = {
    "blank": (
        "What's in {table}?",
        "Add a table of {table}",
        "Chart the key numbers in {table}",
    ),
    "template": (
        "What's in {table}?",
        "Chart the key numbers in {table}",
    ),
}

#: The CSS class of the starter questions in the drawer, and of each question's button.
STARTERS_CLASS = "flowdash-starters"
STARTER_CLASS = "flowdash-starter"

# The phone breakpoint, below which each question is a touch target of 44 px.
_PHONE_MAX = "@media (max-width: 599.95px)"


@dataclass(frozen=True)
class StarterContext:
    """What a starter questions hook (`FlowDashApp.agent_starters`) is told about a new dashboard.

    It is read only, so a hook cannot change the dashboard; it may use the
    person's identity to offer questions about data they may see.
    """

    #: The signed-in person, with their groups and the claims of their login.
    identity: Identity
    dashboard_id: str
    title: str
    #: How the dashboard was made: ``"blank"`` or ``"template"``.
    origin: t.Literal["blank", "template"]
    #: The id of the template it was made from, if any.
    template: str | None
    #: The titles of the tables the person may place, see `data_tables`.
    tables: tuple[str, ...]


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


def default_starter_questions(context: StarterContext, limit: int = STARTER_LIMIT) -> list[str]:
    """Return up to *limit* questions about the tables in *context*.

    The questions take the tables in turn, so with several each is asked
    about once before any twice.
    """
    tables = context.tables
    phrases = _PHRASES.get(context.origin, _PHRASES["blank"])
    if not tables:
        return []
    return [
        phrases[i % len(phrases)].format(table=tables[i % len(tables)])
        for i in range(min(limit, max(len(tables), len(phrases))))
    ]


def starter_questions(found: t.Any, context: StarterContext) -> list[str]:
    """Return the questions a hook's result *found* offers, at most `STARTER_LIMIT`.

    None offers the default questions and an empty list none. A result that
    is not a list of strings offers none and is logged; items that are
    empty or not strings are left out.
    """
    if found is None:
        return default_starter_questions(context)
    if isinstance(found, (str, bytes)) or not isinstance(found, t.Iterable):
        logger.error(
            "The starter questions hook returned %s, not a list of questions",
            type(found).__name__,
        )
        return []
    questions: list[str] = []
    try:
        for item in itertools.islice(found, _SCAN_LIMIT):
            if isinstance(item, str) and item.strip():
                questions.append(item.strip())
                if len(questions) == STARTER_LIMIT:
                    break
    except Exception:
        logger.exception("The starter questions hook's questions could not be read")
        return []
    return questions


def _close(found: t.Any):
    """Close a coroutine that was never started, so it is not reported as never awaited."""
    if inspect.iscoroutine(found) and inspect.getcoroutinestate(found) == "CORO_CREATED":
        found.close()


class StarterQuestions:
    """Starter questions in the assistant's drawer while a new dashboard has had no request.

    Clicking one asks the assistant. They go once the assistant gets a
    request, another dashboard opens or edit mode ends, and show only on
    the drawer's chat. Questions that need waiting for, from an awaitable
    hook or from palette presets still loading, are dropped if the person
    typed or asked meanwhile, and the wait is given up after
    `STARTER_TIMEOUT` seconds or when the session ends.
    """

    def __init__(self, session: AgentSession):
        self.session = session
        self.questions: list[str] = []
        self._dashboard: str | None = None
        self._task: asyncio.Task | None = None
        # Whether the person typed or sent a message while the questions were awaited.
        self._interrupted = False
        self._docs: set[int] = set()
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
            margin=0,
            sizing_mode="stretch_width",
            css_classes=[STARTERS_CLASS],
            # The chat's own background: a drawer over the page draws its paper lighter.
            styles={
                "background": "var(--mui-palette-background-default)",
                "padding": "10px 12px 2px 12px",
            },
        )
        editor = session.app._editor
        session.runner.state.param.watch(self._on_running, "running")
        editor.param.watch(self._on_editor, ["editable", "read_only", "dashboard"])
        session.drawer.param.watch(lambda _event: self._sync(), "tab")
        session.drawer.chat.feed.active_widget.param.watch(
            self._on_input, ["value_input", "value"]
        )

    @property
    def shown(self) -> bool:
        """Whether the questions are offered."""
        return bool(self.questions)

    def offer(self, context: t.Callable[[], StarterContext], hook: t.Callable | None = None):
        """Offer the starter questions for the open dashboard, from *hook* or `default_starter_questions`.

        *context* makes the hook's `StarterContext`, once the palette presets
        loading for the new dashboard are in. A hook may return its questions
        or an awaitable of them, see `starter_questions`. One that fails is
        logged and offers none.
        """
        self.clear()
        editor = self.session.app._editor
        if editor.dashboard is None or editor.read_only or self.session.runner.state.running:
            return
        dashboard = editor.dashboard.dashboard_id
        self._dashboard = dashboard
        self._interrupted = False
        presets = [
            task for task in getattr(self.session.app, "_preset_tasks", ()) if not task.done()
        ]
        if presets:
            self._wait(self._resolve(context, hook, presets=presets), dashboard)
            return
        try:
            found = hook(context()) if hook is not None else None
        except Exception:
            logger.exception("The starter questions hook failed")
            return
        if inspect.isawaitable(found):
            self._wait(self._resolve(context, hook, found=found), dashboard, found)
            return
        self._show(starter_questions(found, context()))

    def _wait(self, resolving: t.Coroutine, dashboard: str, found: t.Any = None):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            resolving.close()
            _close(found)
            logger.error("The starter questions cannot be awaited without a running event loop")
            return
        doc = pn.state.curdoc
        if doc is not None and doc.session_context is not None and id(doc) not in self._docs:
            self._docs.add(id(doc))
            doc.on_session_destroyed(lambda _context: self.clear())
        self._task = task = loop.create_task(resolving)
        task.add_done_callback(lambda done: self._arrived(done, dashboard, doc, found))

    async def _resolve(self, context, hook, presets=(), found=None) -> list[str]:
        if presets:
            await asyncio.wait(presets, timeout=STARTER_TIMEOUT)
        made = context()
        if hook is not None and found is None:
            found = hook(made)
        if inspect.isawaitable(found):
            found = await asyncio.wait_for(found, STARTER_TIMEOUT)
        return starter_questions(found, made)

    def _arrived(self, task: asyncio.Future, dashboard: str, doc, found: t.Any = None):
        # A hook's awaitable is never awaited when the wait is cancelled before it starts.
        _close(found)
        if task.cancelled() or task is not self._task:
            return
        self._task = None
        error = task.exception()
        if isinstance(error, TimeoutError):
            logger.warning(
                "The starter questions took over %s s and were given up", STARTER_TIMEOUT
            )
            return
        if error is not None:
            logger.error("The starter questions hook failed", exc_info=error)
            return
        questions = task.result()

        def show():
            editor = self.session.app._editor
            current = editor.dashboard.dashboard_id if editor.dashboard is not None else None
            if (
                self._dashboard != dashboard
                or current != dashboard
                or not editor.editable
                or editor.read_only
                or self._interrupted
                or self.session.runner.state.running
            ):
                return
            self._show(questions)

        if doc is None or doc.session_context is None:
            show()
            return

        def locked():
            with set_curdoc(doc):
                show()

        # Changed under the document's lock, as the session's own callbacks are.
        doc.add_next_tick_callback(locked)

    def _show(self, questions: list[str]):
        self.questions = questions
        self._list.objects = [self._button(question) for question in questions]
        self._sync()

    def _button(self, question: str) -> pmui.Button:
        button = pmui.Button(
            label=question,
            icon="chat_bubble_outline",
            variant="outlined",
            color="default",
            margin=(0, 0, 6, 0),
            sizing_mode="stretch_width",
            css_classes=[STARTER_CLASS],
            sx={
                "textTransform": "none",
                "justifyContent": "flex-start",
                "textAlign": "left",
                "fontWeight": 400,
                "color": "text.primary",
                "borderColor": "divider",
                "whiteSpace": "normal",
                "overflowWrap": "anywhere",
                "lineHeight": 1.4,
                "py": "5px",
                # The default colour's hover is a grey of its own, which the label would not read on.
                "&:hover": {
                    "color": "text.primary",
                    "backgroundColor": "action.hover",
                    "borderColor": "text.secondary",
                },
                _PHONE_MAX: {"minHeight": "44px"},
            },
        )
        button.on_click(lambda _event: self.ask(question))
        return button

    def ask(self, question: str):
        """Ask the assistant *question*, as clicking it does."""
        self.clear()
        self.session.chrome.ask(question)

    def clear(self):
        """Stop offering the questions, and stop waiting for them."""
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

    def _on_input(self, event):
        if event.new and self._task is not None:
            self._interrupted = True

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
