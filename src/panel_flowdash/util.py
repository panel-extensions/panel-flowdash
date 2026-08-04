"""Shared helpers for the app shell and the embeddable editor."""

from __future__ import annotations

import inspect
import logging
import typing as t

import panel as pn

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

logger = logging.getLogger("panel_flowdash")

_LOG_LEVELS = {
    "success": logging.INFO,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


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
