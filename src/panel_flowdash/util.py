"""Shared helpers for the app shell and the embeddable editor."""

from __future__ import annotations

import inspect
import logging
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash.session_state import check_requirements

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

    from panel_flowdash.component_spec import InputPort

logger = logging.getLogger("panel_flowdash")

_LOG_LEVELS = {
    "success": logging.INFO,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# param.ReactiveError, rx(error_mode=...) and bind(process_failures=...) arrived
# together in param 2.5, so failures can only be carried along edges from there.
ReactiveError: type[Exception] | None = getattr(param, "ReactiveError", None)


def is_failure(value: t.Any) -> bool:
    """Whether *value* is a failure carried along an edge as a ``param.ReactiveError``."""
    return ReactiveError is not None and isinstance(value, ReactiveError)


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


def _input_label(port: InputPort) -> str:
    if port.label:
        return port.label
    return port.parameter.label if port.parameter is not None else port.name


def _blocking_ports(state: param.Parameterized, inputs: t.Iterable[InputPort]) -> list[InputPort]:
    return [p for p in inputs if p.required and p.blocking and p.name in state.param]


def missing_inputs(state: param.Parameterized, inputs: t.Iterable[InputPort]) -> list[str]:
    """Return the labels of the blocking inputs of *state* that are still None."""
    ports = _blocking_ports(state, inputs)
    requires = [{"key": p.name, "required": True, "blocking": True} for p in ports]
    labels = {p.name: _input_label(p) for p in ports}
    return [labels[u["key"]] for u in check_requirements(state, requires)]


def gate_view(
    view: Viewable,
    state: param.Parameterized,
    inputs: t.Iterable[InputPort],
    message: str | None = None,
) -> tuple[Viewable, Callable[[], None] | None]:
    """Show a placeholder instead of *view* while a blocking input of *state* is None.

    Blocking follows :func:`~panel_flowdash.session_state.check_requirements`.
    Returns the view to display and a callable that stops it following
    *state*, or *view* itself and ``None`` when no input can block. Otherwise
    *view* is built once and only its container is hidden, so toggling neither
    re-renders the view nor recreates its models.
    """
    ports = _blocking_ports(state, inputs)
    if not ports:
        return view, None

    placeholder = pmui.Typography(
        variant="body2",
        sizing_mode="stretch_both",
        # Canvas nodes shrink to their content, so a stretching placeholder
        # needs its own minimum size to leave the node any room.
        min_height=64,
        min_width=200,
        sx={
            "alignItems": "center",
            "color": "text.secondary",
            "display": "flex",
            "justifyContent": "center",
            "p": 1,
            "textAlign": "center",
        },
    )
    # The view is wrapped rather than toggled itself, since a component may
    # manage the `visible` of the objects it returns.
    body = pn.Column(view, margin=0, sizing_mode="stretch_both")

    def update(*_):
        missing = missing_inputs(state, ports)
        with pn.io.hold():
            placeholder.param.update(
                object=message or f"Waiting for {', '.join(missing)}",
                visible=bool(missing),
            )
            body.visible = not missing

    update()
    # One plain watcher rather than bound refs, so the editor can release it
    # with the rest of the node's wiring when the node is removed.
    watcher = state.param.watch(update, [p.name for p in ports])
    gated = pn.Column(placeholder, body, margin=0, sizing_mode="stretch_both")
    return gated, partial(state.param.unwatch, watcher)


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
