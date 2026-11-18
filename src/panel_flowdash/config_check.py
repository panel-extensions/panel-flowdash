"""Run a component's ``check_config`` hook against the config a node is about to get.

A component declares the hook with ``@register(check_config=check)``. It is
called as ``check(config, inputs)``, sync or async, where ``config`` is the
node's whole effective config with the proposed change applied and ``inputs``
holds the node's live input values that are set and not failed. It returns
``None``, a string or an iterable of strings, each non-empty string a problem;
it may also raise ``ValueError`` with the problem as its message. A
:class:`CheckWarning` in place of a string, alone or in the list, is a warning
instead: the change is applied, and the warning shown with it. Any other
result or exception is logged and counts as a failed check.

A sync check runs in a worker thread of this module's own pool, so a check
that hangs past its timeout cannot use up the event loop's default executor.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import copy
import inspect
import logging
import threading
import typing as t
import weakref
from collections.abc import Callable, Iterable, Mapping

from panel_flowdash.util import is_failure

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec
    from panel_flowdash.editor import FlowDash

logger = logging.getLogger("panel_flowdash")

# Seconds a check may take before its config is refused as unchecked.
CHECK_TIMEOUT = 10.0


def default_config(spec: ComponentSpec) -> dict[str, t.Any]:
    """Return the config a newly placed node of *spec* starts with."""
    cls = spec.config_state_class
    if cls is not None:
        return {
            name: copy.deepcopy(p.default)
            for name, p in cls.param.objects("existing").items()
            if name != "name"
        }
    return {f.name: copy.deepcopy(f.default) for f in spec.config}


def effective_config(
    spec: ComponentSpec,
    base: t.Mapping[str, t.Any] | None = None,
    patch: t.Mapping[str, t.Any] | None = None,
) -> dict[str, t.Any]:
    """Merge *patch* over *base* over the defaults, with former field names resolved."""
    config = default_config(spec)
    for overlay in (base or {}, patch or {}):
        for key, value in overlay.items():
            config[spec.resolve_name(key)] = value
    return config


def node_config(editor: FlowDash, instance_id: str, spec: ComponentSpec) -> dict[str, t.Any]:
    """Return the effective config of a placed node."""
    state = editor._dataflow_graph.get_config_state(instance_id)
    if state is None:
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), {})
        return effective_config(spec, item.get("config"))
    return {name: getattr(state, name) for name in state.param if name != "name"}


def node_inputs(editor: FlowDash, instance_id: str, spec: ComponentSpec) -> dict[str, t.Any]:
    """Return a placed node's input values, leaving out unset and failed ones."""
    graph = editor._dataflow_graph
    state = graph.get_state(instance_id)
    if state is None:
        return {}
    failures = graph.get_failures(instance_id)
    failed = failures.failed if failures is not None else {}
    inputs = {}
    for port in spec.inputs:
        if port.name in failed or port.name not in state.param:
            continue
        value = getattr(state, port.name)
        if value is None or is_failure(value):
            continue
        inputs[port.name] = value
    return inputs


def _instantiating():
    # Imported late: the editor imports the inspector, which imports this module.
    from panel_flowdash.editor import _INSTANTIATING

    return _INSTANTIATING


# How many sync checks, and async checks called from sync code, run at once.
# They run on a pool of this many threads of their own, created on the first
# check, so set it before then. A check waits for a free thread at most its
# timeout, and its timeout only starts once it runs.
CHECK_WORKERS = 8

_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=CHECK_WORKERS, thread_name_prefix="flowdash-config-check"
        )
    return _POOL


class CheckWarning(str):
    """A message a config check returns that does not refuse the config.

    Return it in place of a problem string, alone or in the list of problems,
    for something the check could not verify, e.g. a query whose probe timed
    out. The config is applied all the same, and the warning shown with it.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Show it as a warning, not a plain string."""
        return f"CheckWarning({str.__repr__(self)})"


class CheckResult(list):
    """The problems a config check found, a list of strings, empty when it passes.

    Its :attr:`warnings` are the :class:`CheckWarning` messages the check
    returned, which do not refuse the config.
    """

    def __init__(self, problems: Iterable[str] = (), warnings: Iterable[str] = ()):
        super().__init__(str(p) for p in problems)
        self.warnings: list[str] = [str(w) for w in warnings]

    def __repr__(self) -> str:
        """Show the warnings too, which list equality ignores."""
        return f"CheckResult({list(self)!r}, warnings={self.warnings!r})"


class _TimedOut(Exception):
    """The check did not finish within the time allowed."""


class _NoWorker(_TimedOut):
    """No thread was free to run the check within the time allowed."""


class _BadResult(TypeError):
    """The check returned something other than None, a string or strings."""


def _problems(result: t.Any) -> CheckResult:
    if result is None:
        return CheckResult()
    if isinstance(result, str):
        items = [result]
    elif not isinstance(result, Iterable) or isinstance(result, (Mapping, bytes)):
        raise _BadResult(type(result).__name__)
    else:
        items = list(result)
    if not all(isinstance(item, str) for item in items):
        kinds = sorted({type(item).__name__ for item in items if not isinstance(item, str)})
        raise _BadResult(f"{type(result).__name__} holding {', '.join(kinds)}")
    return CheckResult(
        [item for item in items if item and not isinstance(item, CheckWarning)],
        [item for item in items if item and isinstance(item, CheckWarning)],
    )


def _failed(spec: ComponentSpec, exc: BaseException) -> CheckResult:
    return CheckResult(_failure(spec, exc))


def _failure(spec: ComponentSpec, exc: BaseException) -> list[str]:
    if isinstance(exc, _NoWorker):
        return [
            f"The config check of '{spec.title}' could not start within the time allowed, "
            "since other checks are still running, so the config was not applied."
        ]
    if isinstance(exc, _TimedOut):
        return [
            f"The config check of '{spec.title}' did not finish within the time allowed, "
            "so the config was not applied."
        ]
    if isinstance(exc, _BadResult):
        logger.error(
            "The config check of %s returned a %s; it must return None, a string or a list "
            "of strings",
            spec.component_id,
            exc,
        )
        return [f"The config check of '{spec.title}' failed (it returned a {exc})."]
    if isinstance(exc, ValueError):
        return [str(exc) or f"'{spec.title}' refused the config."]
    logger.error("The config check of %s failed", spec.component_id, exc_info=exc)
    return [f"The config check of '{spec.title}' failed ({type(exc).__name__})."]


def _call(check: Callable, config: dict, inputs: dict, started: t.Callable[[], None]) -> t.Any:
    """Call *check* in this thread, running an async result to completion on its own loop."""
    started()
    result = check(config, inputs)
    if inspect.isawaitable(result):

        async def finish():
            return await result

        result = asyncio.run(finish())
    return result


async def run_check(
    spec: ComponentSpec,
    config: t.Mapping[str, t.Any],
    inputs: t.Mapping[str, t.Any],
    *,
    editor: FlowDash | None = None,
    timeout: float = CHECK_TIMEOUT,
) -> CheckResult:
    """Run the check of *spec* and return its problems, an empty list when it passes.

    An async check runs on the event loop, a sync one in a worker thread.
    Either kind is given *timeout* seconds; one that takes longer counts as a
    problem. While it runs :func:`~panel_flowdash.current_dashboard` returns
    *editor*.
    """
    check = spec.check_config
    if check is None:
        return CheckResult()
    instantiating = _instantiating()
    token = instantiating.set(editor)
    try:
        if inspect.iscoroutinefunction(check):
            future = asyncio.ensure_future(check(dict(config), dict(inputs)))
        else:
            loop = asyncio.get_running_loop()
            running = loop.create_future()

            def started():
                loop.call_soon_threadsafe(lambda: running.done() or running.set_result(None))

            context = contextvars.copy_context()
            future = loop.run_in_executor(
                _executor(), context.run, _call, check, dict(config), dict(inputs), started
            )
            done, _ = await asyncio.wait({running, future}, timeout=timeout)
            if not done:
                future.cancel()
                running.cancel()
                raise _NoWorker()
        done, _ = await asyncio.wait({future}, timeout=timeout)
        if not done:
            future.cancel()
            raise _TimedOut()
        return _problems(future.result())
    except Exception as exc:
        return _failed(spec, exc)
    finally:
        instantiating.reset(token)


def run_check_sync(
    spec: ComponentSpec,
    config: t.Mapping[str, t.Any],
    inputs: t.Mapping[str, t.Any],
    *,
    editor: FlowDash | None = None,
    timeout: float = CHECK_TIMEOUT,
) -> CheckResult:
    """Like :func:`run_check`, for callers that cannot await.

    The check runs in a worker thread, an async one on its own event loop
    there, and this waits for it at most *timeout* seconds once it runs,
    blocking the calling thread meanwhile, the event loop too when called on
    it.
    """
    check = spec.check_config
    if check is None:
        return CheckResult()
    instantiating = _instantiating()
    token = instantiating.set(editor)
    try:
        context = contextvars.copy_context()
        running = threading.Event()
        future = _executor().submit(
            context.run, _call, check, dict(config), dict(inputs), running.set
        )
        if not running.wait(timeout) and future.cancel():
            raise _NoWorker()
        try:
            result = future.result(timeout)
        except TimeoutError:
            if not future.done():
                future.cancel()
                raise _TimedOut() from None
            raise
        return _problems(result)
    except Exception as exc:
        return _failed(spec, exc)
    finally:
        instantiating.reset(token)


def same_config(a: t.Mapping[str, t.Any], b: t.Mapping[str, t.Any]) -> bool:
    """Whether two configs hold the same fields and equal values."""
    if a.keys() != b.keys():
        return False
    for key, value in a.items():
        other = b[key]
        if value is other:
            continue
        try:
            if not bool(value == other):
                return False
        except Exception:
            return False
    return True


def _check_args(
    editor: FlowDash, instance_id: str, patch: t.Mapping[str, t.Any] | None = None
) -> tuple[ComponentSpec | None, dict[str, t.Any], dict[str, t.Any]]:
    """Return the spec, effective config with *patch* and live inputs of a placed node."""
    item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
    if item is None or "placeholder" in item:
        return None, {}, {}
    spec = editor._component_specs.get(item["component_id"])
    if spec is None or spec.check_config is None:
        return None, {}, {}
    config = node_config(editor, instance_id, spec)
    for key, value in (patch or {}).items():
        config[spec.resolve_name(key)] = value
    return spec, config, node_inputs(editor, instance_id, spec)


async def check_node(
    editor: FlowDash,
    instance_id: str,
    patch: t.Mapping[str, t.Any] | None = None,
    *,
    timeout: float = CHECK_TIMEOUT,
    attempts: int = 3,
) -> CheckResult:
    """Check a placed node's config with *patch* applied, against its live inputs.

    The node's config may change while the check runs, e.g. by another edit
    of the agent or the user. The check then runs again on the new config, so
    the result holds for the config the caller applies *patch* to, as long as
    the caller applies it without awaiting anything first.
    """
    for _ in range(attempts):
        spec, config, inputs = _check_args(editor, instance_id, patch)
        if spec is None:
            return CheckResult()
        base = node_config(editor, instance_id, spec)
        problems = await run_check(spec, config, inputs, editor=editor, timeout=timeout)
        current, _, _ = _check_args(editor, instance_id)
        if current is None or same_config(base, node_config(editor, instance_id, current)):
            return problems
    return CheckResult(
        [f"The config of '{spec.title}' kept changing while it was checked; try again."]
    )


def check_node_sync(
    editor: FlowDash,
    instance_id: str,
    patch: t.Mapping[str, t.Any] | None = None,
    *,
    timeout: float = CHECK_TIMEOUT,
) -> CheckResult:
    """Like :func:`check_node`, for callers that cannot await."""
    spec, config, inputs = _check_args(editor, instance_id, patch)
    if spec is None:
        return CheckResult()
    return run_check_sync(spec, config, inputs, editor=editor, timeout=timeout)


def node_wiring(editor: FlowDash, instance_id: str) -> tuple[tuple[t.Any, ...], ...]:
    """Return the edges into a node, with their transforms, in a comparable form."""
    return tuple(
        sorted(
            (e["target_port"], e["source"], e["source_port"], repr(e.get("transform")))
            for e in editor._dataflow_graph.edges
            if e["target"] == instance_id
        )
    )


class _Slot:
    """The check of one node running now, and the one queued to run after it."""

    __slots__ = ("loop", "patch", "queued", "running", "task")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.running: asyncio.Future | None = None
        self.queued: asyncio.Future | None = None
        self.patch: dict[str, t.Any] = {}
        self.task: asyncio.Task | None = None


_SLOTS: weakref.WeakKeyDictionary[t.Any, dict[t.Hashable, _Slot]] = weakref.WeakKeyDictionary()


def _snapshot(editor: FlowDash, instance_id: str, patch: dict[str, t.Any]) -> tuple | None:
    spec, config, _ = _check_args(editor, instance_id, patch)
    if spec is None:
        return None
    return config, node_wiring(editor, instance_id)


def _unchanged(before: tuple | None, after: tuple | None) -> bool:
    if before is None or after is None:
        return False
    return same_config(before[0], after[0]) and before[1] == after[1]


async def _drive(
    editor, instance_id: str, slots: dict, key: t.Hashable, slot: _Slot, patch: dict, timeout
):
    future = slot.running
    result: CheckResult | None = None
    read: tuple | None = None
    try:
        while True:
            state = _snapshot(editor, instance_id, patch)
            if result is None or not _unchanged(read, state):
                read = state
                try:
                    result = await check_node(editor, instance_id, patch, timeout=timeout)
                except Exception as exc:
                    logger.error("The config check of %s failed", instance_id, exc_info=exc)
                    result = CheckResult([f"The config check failed ({type(exc).__name__})."])
            if not future.done():
                future.set_result(result)
            if slot.queued is None:
                return
            future, patch = slot.queued, slot.patch
            slot.queued, slot.running = None, future
    finally:
        for pending in (slot.running, slot.queued):
            if pending is not None and not pending.done():
                pending.cancel()
        slot.running = slot.queued = None
        if slots.get(key) is slot:
            del slots[key]


async def recheck_node(
    editor: FlowDash,
    instance_id: str,
    patch: t.Mapping[str, t.Any] | None = None,
    *,
    timeout: float = CHECK_TIMEOUT,
) -> CheckResult:
    """Like :func:`check_node`, running at most one check of the node at a time.

    Meant for checks that follow a change, e.g. of the node's wiring, where
    only the latest state matters. While a check of the node runs, further
    requests wait for a single check queued after it, which uses the *patch*
    of the latest request and reads the node's config and inputs once it
    starts; all of them get its result. When the config and wiring it reads
    are those the running check read, the running check's result is reused
    instead. Checks with and without a *patch* are queued apart, each kind
    with at most one check running and one queued per node. A sync check
    that timed out holds its pool thread until it returns all the same.
    """
    loop = asyncio.get_running_loop()
    slots = _SLOTS.setdefault(editor, {})
    key = (instance_id, bool(patch))
    slot = slots.get(key)
    if slot is None or slot.loop is not loop:
        slot = slots[key] = _Slot(loop)
    patch = dict(patch or {})
    if slot.running is None:
        slot.running = future = loop.create_future()
        slot.task = loop.create_task(_drive(editor, instance_id, slots, key, slot, patch, timeout))
    else:
        slot.patch = patch
        if slot.queued is None:
            slot.queued = loop.create_future()
        future = slot.queued
    return await asyncio.shield(future)


__all__ = [
    "CHECK_TIMEOUT",
    "CheckResult",
    "CheckWarning",
    "check_node",
    "check_node_sync",
    "effective_config",
    "node_inputs",
    "node_wiring",
    "recheck_node",
    "run_check",
    "run_check_sync",
]
