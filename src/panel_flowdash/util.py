"""Shared helpers for the app shell and the embeddable editor."""

from __future__ import annotations

import inspect
import logging
import typing as t
import weakref

import panel as pn
import panel_material_ui as pmui

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from bokeh.document import Document
    from panel.viewable import Viewable

logger = logging.getLogger("panel_flowdash")

_LOG_LEVELS = {
    "success": logging.INFO,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# One dialog per served session. Once rendered, the dialog's models reference their
# document, which would keep a weak key alive forever, so the entry is also dropped
# explicitly when the session is destroyed.
_dialogs: weakref.WeakKeyDictionary[Document, pmui.Dialog] = weakref.WeakKeyDictionary()


def is_async_gen(obj: t.Any) -> bool:
    """Whether *obj* is an async generator function, seen through decorators.

    ``param.output`` and ``functools.wraps`` return sync wrappers around async
    functions, which the plain :mod:`inspect` predicates report as sync, so
    unwrap before asking.
    """
    return inspect.isasyncgenfunction(inspect.unwrap(obj))


def is_coroutine(obj: t.Any) -> bool:
    """Whether *obj* is a coroutine function, seen through decorators."""
    return inspect.iscoroutinefunction(inspect.unwrap(obj))


def is_async(obj: t.Any) -> bool:
    """Whether *obj* is a coroutine or async generator function."""
    return is_coroutine(obj) or is_async_gen(obj)


def panel_call(app: Callable, /, **kwargs) -> Viewable:
    """Call a component callable and return a renderable view of the result.

    Sync callables are called immediately. Async ones must not be: calling them
    here would only produce an un-awaited coroutine, which ``pn.panel`` wraps as
    a string. Instead they are deferred to a zero-argument closure that Panel's
    ``ParamFunction`` awaits (or iterates, for async generators) on the event
    loop when the view is rendered.
    """
    if is_async_gen(app):

        async def view():
            async for obj in app(**kwargs):
                yield obj

        return pn.panel(view)

    if is_coroutine(app):

        async def view():
            return await app(**kwargs)

        return pn.panel(view)

    result = app(**kwargs)
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        # A sync callable that returns an awaitable, so the predicates above
        # could not have caught it. Rendering it directly would leak it
        # un-awaited, so hand it to Panel to resolve on the event loop.
        return pn.panel(_as_deferred(result))
    return pn.panel(result)


def _as_deferred(awaitable) -> Callable:
    """Wrap an already-created awaitable or async generator for ``ParamFunction``."""
    if inspect.isasyncgen(awaitable):

        async def view():
            async for obj in awaitable:
                yield obj

    else:

        async def view():
            return await awaitable

    return view


def panel_viewer(instance) -> Viewable:
    """Return a renderable view of a ``Viewer``, awaiting an async ``__panel__``.

    ``pn.panel`` calls ``__panel__`` synchronously, so an ``async def __panel__``
    would be wrapped un-awaited. ``ParamMethod`` handles both, and additionally
    re-renders when the method declares ``param.depends``.
    """
    if is_async(instance.__panel__):
        return pn.pane.ParamMethod(instance.__panel__)
    return pn.panel(instance)


def notify(
    severity: str,
    message: str,
    *,
    duration: int = 3000,
    enabled: bool = True,
) -> None:
    """Emit a Panel notification, falling back to the logger.

    ``pn.state.notifications`` is ``None`` outside a served session (a plain
    script, a test, or a notebook without the notifications extension), so
    calling it unguarded raises. Embedders can also opt out entirely by passing
    ``enabled=False``.
    """
    if not enabled:
        return
    notifications = pn.state.notifications
    if notifications is None:
        logger.log(_LOG_LEVELS.get(severity, logging.INFO), message)
        return
    getattr(notifications, severity)(message, duration=duration)


def page_dialog() -> pmui.Dialog | None:
    """Return the dialog shared by every component of the current session.

    The dialog is created on first use and released with the session. It must
    be laid out once to be visible: ``FlowDashApp`` places it in the page header
    and ``FlowDash`` in its workspace, so only code that builds its own page
    around a ``FlowDash(include_page_dialog=False)`` needs to call this.

    Returns ``None`` outside a served session, where there is no document to
    own it.
    """
    doc = pn.state.curdoc
    if doc is None:
        return None
    dialog = _dialogs.get(doc)
    if dialog is None:
        dialog = _dialogs[doc] = pmui.Dialog(open=False)
        pn.state.on_session_destroyed(lambda _context: _dialogs.pop(doc, None))
    return dialog


def _session_dialog(title: str) -> pmui.Dialog | None:
    dialog = page_dialog()
    if dialog is None:
        logger.warning("Cannot show the dialog %r outside a served session.", title)
    return dialog


def open_dialog(title: str, content: t.Any, *, width: str | bool = "sm") -> None:
    """Show *content* in the session's page dialog, replacing whatever it shows.

    The user can dismiss the dialog with its close button, the Escape key or a
    click outside it. Use :func:`close_dialog` to dismiss it from code.

    Outside a served session there is nowhere to show a dialog, so a warning is
    logged and nothing happens, as with :func:`notify`.

    Parameters
    ----------
    title
        Heading of the dialog.
    content
        Anything Panel can render.
    width
        Maximum width of the dialog, one of ``"xs"``, ``"sm"``, ``"md"``,
        ``"lg"``, ``"xl"``, or ``False`` for no limit.
    """
    dialog = _session_dialog(title)
    if dialog is None:
        return
    dialog.param.update(
        title=title,
        objects=[content],
        open=True,
        close_on_click=True,
        show_close_button=True,
        width_option=width,
    )


def close_dialog() -> None:
    """Close the session's page dialog. Does nothing when it is not open."""
    doc = pn.state.curdoc
    dialog = None if doc is None else _dialogs.get(doc)
    if dialog is not None:
        dialog.open = False


def confirm(
    title: str,
    content: t.Any = None,
    *,
    on_confirm: Callable[[t.Any], t.Any],
    on_cancel: Callable[[t.Any], t.Any] | None = None,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    width: str | bool = "sm",
) -> None:
    """Ask the user to confirm an action in the session's page dialog.

    Opening the dialog never runs anything. Clicking *Confirm* closes the
    dialog and then calls ``on_confirm(event)``; clicking *Cancel* closes it and
    calls ``on_cancel(event)`` if one is given. Either handler may be a
    coroutine function, which Panel schedules. Only the first click counts, so
    a double click cannot run the action twice. The buttons are the only way
    out: a confirmation cannot be dismissed with Escape or a click outside.

    Replacing the dialog (another :func:`open_dialog` or :func:`confirm`) before
    the user answers drops the question without calling either handler.

    Outside a served session a warning is logged and neither handler runs.

    Parameters
    ----------
    title
        Heading of the dialog, usually the question.
    content
        Anything Panel can render, such as a diff of what will change.
    on_confirm
        Called with the click event after the dialog has closed.
    on_cancel
        Called with the click event after the dialog has closed.
    confirm_label, cancel_label
        Button labels.
    width
        See :func:`open_dialog`.
    """
    dialog = _session_dialog(title)
    if dialog is None:
        return
    cancel = pmui.Button(label=cancel_label, variant="text")
    accept = pmui.Button(label=confirm_label, color="primary")
    watchers = []

    def dismiss(_event):
        dialog.open = False
        for button, watcher in watchers:
            button.param.unwatch(watcher)

    # The handler is registered on the button itself, behind the dismissal, because
    # Panel only schedules a coroutine function when param is given the function.
    for button, handler in ((cancel, on_cancel), (accept, on_confirm)):
        watchers.append((button, button.on_click(dismiss)))
        if handler is not None:
            watchers.append((button, button.on_click(handler)))

    dialog.param.update(
        title=title,
        objects=[
            *([] if content is None else [content]),
            pn.Row(pn.layout.HSpacer(), cancel, accept, sizing_mode="stretch_width"),
        ],
        open=True,
        close_on_click=False,
        show_close_button=False,
        width_option=width,
    )
