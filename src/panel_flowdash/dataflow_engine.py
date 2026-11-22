"""Dataflow wiring engine with runtime validation.

Each node in the graph gets a NodeState (a dynamic Parameterized subclass)
whose parameters correspond to the node's declared input and output ports.
Every edge is a ``param.rx`` expression on its source port, extended by one
pipe per step of the edge's transform, and a connected input is bound to the
expressions feeding it as a param reference, so param itself propagates
values. All edges leaving a port follow one relay of it, which forwards each
value inside the graph's ``hold`` context, so a change reaches every consumer
before the hold releases anything. The binding checks each value against the
target first; a rejected value, like a failing transform step, is reported via
the error callback and skipped, which leaves the target on its last good value.

With ``propagate_failures`` a failure is carried along the edge instead, as a
``param.ReactiveError`` value. An input that handles failures receives it on
its port; any other input keeps its last good value, records the failure in the
node's :class:`InputFailures`, and its node passes the failure on to every
output so that nothing downstream keeps presenting stale data as current.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import Any

import param

from panel_flowdash.component_spec import ComponentSpec
from panel_flowdash.input_literals import check_literal, port_literal
from panel_flowdash.port_types import (
    is_registered_port_type,
    normalize_port_type,
    port_types_compatible,
)
from panel_flowdash.transforms import Transform, apply_transform, parse_transform
from panel_flowdash.util import ReactiveError, is_failure
from panel_flowdash.value_text import error_text

logger = logging.getLogger("panel_flowdash")

_RESERVED_PARAMS = set(param.Parameterized.param)

EdgeKey = tuple[str, str, str, str]

_NO_LITERAL = object()

# Frames left free below the recursion limit when an update has to stop, for
# the hop already in progress to finish.
_STACK_MARGIN = 100


@dataclass
class Edge:
    """The transform steps of an edge and the expression after each of them.

    ``stages[0]`` is the source port, as forwarded by its relay, and
    ``stages[i]`` is the value after ``i`` steps. ``expr`` is what the target
    receives, which is ``stages[-1]`` unless failures propagate, when
    ``carried`` replaces it with the source node's failure.
    """

    steps: list[Transform]
    stages: list[param.rx]
    carried: param.rx | None = None
    source: tuple[str, str] = ("", "")

    @property
    def expr(self) -> param.rx:
        return self.stages[-1] if self.carried is None else self.carried

    def dispose(self):
        """Detach the edge's expressions from its source port.

        Called once nothing binds to the edge any more. Param releases without
        ``rx.dispose`` only let go of the expressions once they are collected.
        """
        dispose = getattr(self.expr.rx, "dispose", None)
        if dispose is None:
            return
        try:
            dispose(cascade=True)
        except RuntimeError:
            # Something outside the graph still reads the expression.
            pass


def failure_text(error: param.ReactiveError) -> str:
    """Describe a failure as ``Type: message``."""
    return exception_text(error.exception)


def exception_text(exc: BaseException) -> str:
    """Describe an exception as ``Type: message``."""
    message = error_text(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


@dataclass(frozen=True)
class InputFailure:
    """A failure that reached an input which does not handle failures."""

    source_id: str
    source_port: str
    error: param.ReactiveError

    @property
    def text(self) -> str:
        return failure_text(self.error)


class InputFailures(param.Parameterized):
    """The inputs of one node that currently hold a failure, keyed by port name."""

    failed = param.Dict(
        default={},
        doc="""
        Failures that reached inputs which do not handle them, by input port.
        Replaced, never mutated, so a watcher sees every change.""",
    )


def _is_list_port(port) -> bool:
    """Return True if a port's declared type indicates a list/multi-connection input."""
    if port.multiple is not None:
        return port.multiple
    if port.type is None:
        return False
    return port.type.lower() in ("list", "List")


def _item_class(port) -> type | None:
    """Return the class each item of a list input's Parameter must be, or None."""
    item_type = getattr(port.parameter, "item_type", None)
    return item_type if isinstance(item_type, type) else None


def connection_type(port) -> str | None:
    """Return the type one edge into *port* must carry.

    Each edge into a list input delivers one item, so the item type applies:
    the declared type of a ``multiple`` input that is not itself a list, or
    the ``item_type`` of a List Parameter when it names a registered port
    type. A plain ``List`` input says nothing about its items and accepts
    anything, as does an ``item_type`` of a class with no registered port
    type, whose subclasses only the check of each value can tell apart.
    """
    if not _is_list_port(port):
        return port.type
    item_class = _item_class(port)
    if item_class is not None:
        name = item_class.__name__
        return name if is_registered_port_type(name) else None
    if normalize_port_type(port.type) == "list":
        return None
    return port.type


def _check_item(port, value) -> str | None:
    """Return why a list input would reject *value* as one of its items, or None."""
    item_class = _item_class(port)
    if item_class is None or value is None or is_failure(value) or isinstance(value, item_class):
        return None
    return f"its items must be {item_class.__name__}, not {type(value).__name__}."


def _stack_deeper_than(depth: int) -> bool:
    try:
        sys._getframe(depth)
    except ValueError:
        return False
    return True


_iterative_updates = getattr(param, "batch", nullcontext)


# holoviz/param#1232 makes param drop these watchers itself when a reference is
# overwritten with a plain value.
_PARAM_RELEASES_REF_WATCHERS = hasattr(param.parameterized.Parameters, "_remove_ref")


def _prune_ref_watchers(state: param.Parameterized):
    """Remove the watchers a state keeps on the sources of references it no longer follows."""
    # Param releases without holoviz/param#1232 leave them in place when a
    # reference is overwritten with a plain value, so every source a state was
    # once bound to keeps it alive and keeps calling into it. This reads param
    # internals, so if their shape changes it gives up rather than break
    # removing nodes and edges; the cost is the leak it was working around.
    if _PARAM_RELEASES_REF_WATCHERS:
        return
    try:
        private = state._param__private
        refs = private.refs
        kept, stale = [], []
        for entry in private.ref_watchers:
            names, watcher = entry
            if any(name in refs for name in names):
                kept.append(entry)
            else:
                owner = watcher.cls if watcher.inst is None else watcher.inst
                stale.append((owner, watcher))
    except (AttributeError, TypeError, ValueError):
        return
    for owner, watcher in stale:
        try:
            owner.param.unwatch(watcher)
        except (AttributeError, TypeError, ValueError):
            pass
    private.ref_watchers = kept


def _unbind(state: param.Parameterized, name: str, value):
    """Set *name* to a plain value, releasing any reference it followed."""
    setattr(state, name, value)
    _prune_ref_watchers(state)


def _release_refs(state: param.Parameterized):
    """Replace every reference a state follows with the value it currently resolves to."""
    # A bound input would otherwise keep firing its guard for a node the graph
    # no longer knows, and an output would keep following its component.
    with param.discard_events(state):
        for name in state.param:
            if name != "name":
                setattr(state, name, getattr(state, name))
    _prune_ref_watchers(state)


class _Relay(param.Parameterized):
    """The value of one output port, as followed by every edge leaving it."""

    value = param.Parameter()


def build_node_state_class(spec: ComponentSpec) -> type[param.Parameterized]:
    """Create a Parameterized subclass with one param per port (inputs + outputs)."""
    params: dict[str, param.Parameter] = {}

    for port in spec.inputs:
        if port.name in _RESERVED_PARAMS:
            continue
        if (port.type or "").lower() == "list" and port.handles_failures:
            # A List param would reject the failure this port is meant to receive.
            params[port.name] = param.Parameter(
                default=port.default
                if port.default is not None
                else ([] if _is_list_port(port) else None),
                allow_None=True,
                allow_refs=True,
                instantiate=True,
            )
        elif (port.type or "").lower() == "list":
            params[port.name] = param.List(
                default=port.default
                if port.default is not None
                else ([] if _is_list_port(port) else None),
                allow_None=not _is_list_port(port),
                allow_refs=True,
            )
        else:
            # Copied per node, so a mutable fallback is not shared between nodes.
            params[port.name] = param.Parameter(
                default=port.default, allow_None=True, allow_refs=True, instantiate=True
            )

    for port in spec.outputs:
        if port.name in _RESERVED_PARAMS:
            continue
        if port.name not in params:
            params[port.name] = param.Parameter(
                default=None,
                allow_None=True,
                allow_refs=True,
            )

    class_name = f"NodeState_{spec.component_id.replace('/', '_')}"
    return type(class_name, (param.Parameterized,), params)


class DataflowGraph:
    """Manages node state instances and wires edges with runtime validation."""

    def __init__(
        self,
        specs: dict[str, ComponentSpec],
        on_error: Callable[[str, str, str, str, Exception], None] | None = None,
        propagate_failures: bool = False,
        hold: Callable[[], AbstractContextManager] = nullcontext,
    ):
        """Initialize the dataflow graph.

        Parameters
        ----------
        specs
            Component specs keyed by component_id.
        on_error
            Callback invoked when a runtime value assignment fails.
            Signature: (source_id, source_port, target_id, target_port, exception)
        propagate_failures
            Carry a failing output or edge transform downstream as a
            ``param.ReactiveError`` instead of skipping it. Applies to edges
            wired after construction, so set it here rather than later.
        hold
            Context manager every change of an output port is forwarded to
            its edges in. FlowDash holds the document with it, so a change
            fanning out to several consumers reaches the browser as one
            message.
        """
        self._specs = dict(specs)
        self._state_classes: dict[str, type[param.Parameterized]] = {}
        self._nodes: dict[str, param.Parameterized] = {}
        self._config_states: dict[str, param.Parameterized] = {}
        self._node_specs: dict[str, ComponentSpec] = {}
        self._edges: dict[EdgeKey, Edge] = {}
        self._hold = hold
        self._relays: dict[tuple[str, str], tuple[_Relay, Callable[[], None]]] = {}
        # Indexes over `_edges`, kept in step by `_store_edge` and `_drop_edge`.
        self._incoming_edges: dict[tuple[str, str], dict[EdgeKey, Edge]] = {}
        self._successors: dict[str, dict[str, int]] = {}
        self._on_error = on_error
        self._batch_depth = 0
        self._pending_lists: dict[tuple[str, str], None] = {}
        # Edges replaced inside batch() whose target is rebound only when the
        # pending list inputs are flushed, so they are released after that.
        self._pending_disposals: list[Edge] = []
        self._deferred: dict[tuple[str, str], None] = {}
        self._drain_scheduled = False
        if propagate_failures and ReactiveError is None:
            raise RuntimeError(
                f"propagate_failures requires param >= 2.5, found {param.__version__}."
            )
        self._failures: dict[str, InputFailures] = {}
        # Values set on unconnected inputs, by node and port, see `set_literal`.
        self._literals: dict[str, dict[str, Any]] = {}
        self.propagate_failures = propagate_failures

        for comp_id, spec in specs.items():
            self._state_classes[comp_id] = build_node_state_class(spec)

    def register_specs(self, specs: dict[str, ComponentSpec]):
        """Add component specs to the graph, keeping existing nodes intact.

        Components are imported on demand, so specs arrive after the graph is
        constructed. Registering them incrementally avoids rebuilding the graph
        and losing the nodes and edges already placed on it.
        """
        for comp_id, spec in specs.items():
            if comp_id in self._state_classes:
                continue
            self._specs[comp_id] = spec
            self._state_classes[comp_id] = build_node_state_class(spec)

    def add_node(self, instance_id: str, component_id: str) -> param.Parameterized:
        """Create a new node state instance."""
        spec = self._specs[component_id]
        cls = self._state_classes[component_id]
        state = cls(name=instance_id)
        self._nodes[instance_id] = state
        self._node_specs[instance_id] = spec
        self._failures[instance_id] = InputFailures()
        if spec.config_state_class is not None:
            self._config_states[instance_id] = spec.config_state_class(
                name=f"{instance_id}_config"
            )
        return state

    def remove_node(self, instance_id: str):
        """Remove a node and any edges connected to it.

        The node's state is detached from everything it was bound to, both the
        edges feeding its inputs and any references its outputs follow, so it
        stops receiving values but keeps the ones it last held.
        """
        removed = [key for key in self._edges if instance_id in (key[0], key[2])]
        edges = [edge for edge in map(self._drop_edge, removed) if edge is not None]
        for target_id, target_port in {key[2:] for key in removed}:
            if target_id == instance_id:
                continue
            self._set_failure(target_id, target_port, None)
            if self._is_list_input(target_id, target_port):
                self._bind_input(target_id, target_port)
            elif target_port in self._literals.get(target_id, {}):
                self._unbind_to_literal(target_id, target_port)
            else:
                # Releases the binding to the removed node but keeps its last value.
                target = self._nodes[target_id]
                _unbind(target, target_port, getattr(target, target_port))

        state = self._nodes.pop(instance_id, None)
        self._failures.pop(instance_id, None)
        self._literals.pop(instance_id, None)
        self._config_states.pop(instance_id, None)
        self._node_specs.pop(instance_id, None)
        if state is not None:
            _release_refs(state)
        self._dispose_edges(edges)
        # An edge awaiting disposal keeps its source's relay, which would
        # follow the old state if a node is added again under this id.
        for source in [key for key in self._relays if key[0] == instance_id]:
            self._release_relay(source)

    def _input_port(self, target_id: str, target_port: str):
        spec = self._node_specs.get(target_id)
        if not spec:
            return None
        return next((port for port in spec.inputs if port.name == target_port), None)

    def _is_list_input(self, target_id: str, target_port: str) -> bool:
        """Return True if the target port is a List-typed multi-connection input."""
        port = self._input_port(target_id, target_port)
        return port is not None and _is_list_port(port)

    def _handles_failures(self, target_id: str, target_port: str) -> bool:
        port = self._input_port(target_id, target_port)
        return port is not None and port.handles_failures

    def _set_failure(self, node_id: str, port: str, failure: InputFailure | None):
        """Record or clear the failure held by a node's input."""
        holder = self._failures.get(node_id)
        if holder is None:
            return
        if failure is not None:
            holder.failed = {**holder.failed, port: failure}
        elif port in holder.failed:
            holder.failed = {k: v for k, v in holder.failed.items() if k != port}

    def _check_target_value(self, target_id: str, target_port: str, value):
        """Raise if the target state or the component behind it would reject *value*."""
        if value is None or is_failure(value):
            return
        self._nodes[target_id].param[target_port]._validate(value)
        port = self._input_port(target_id, target_port)
        if port is not None and port.parameter is not None:
            port.parameter._validate(value)

    def _incoming(self, target_id: str, target_port: str) -> dict[EdgeKey, Edge]:
        return self._incoming_edges.get((target_id, target_port)) or {}

    def _store_edge(self, key: EdgeKey, edge: Edge):
        if key not in self._edges:
            targets = self._successors.setdefault(key[0], {})
            targets[key[2]] = targets.get(key[2], 0) + 1
        self._edges[key] = edge
        self._incoming_edges.setdefault(key[2:], {})[key] = edge

    def _drop_edge(self, key: EdgeKey) -> Edge | None:
        """Remove *key* from the indexes and return its edge, or None if it was not wired.

        The edge is returned rather than released here: its target may still
        be bound to it, so callers release it once the target is rebound or
        unbound.
        """
        edge = self._edges.pop(key, None)
        if edge is None:
            return None
        incoming = self._incoming_edges[key[2:]]
        del incoming[key]
        if not incoming:
            del self._incoming_edges[key[2:]]
        targets = self._successors[key[0]]
        targets[key[2]] -= 1
        if not targets[key[2]]:
            del targets[key[2]]
            if not targets:
                del self._successors[key[0]]
        return edge

    def _dispose_edges(self, edges: list[Edge]):
        """Release edges that no input is bound to any more.

        Called only after the target input was rebound or unbound, since
        disposing an expression an input still follows would cut it off.
        """
        for edge in edges:
            edge.dispose()
        for source in {edge.source for edge in edges}:
            if not any(
                e.source == source for e in (*self._edges.values(), *self._pending_disposals)
            ):
                self._release_relay(source)

    def _relay(self, node_id: str, port: str) -> _Relay:
        """Return the relay the edges leaving *port* follow, creating it on first use.

        Each edge watching the port itself would receive a change in its own
        watcher call, so a hold could only cover one consumer at a time.
        Forwarding through one watcher lets the hold cover all of them.
        """
        entry = self._relays.get((node_id, port))
        if entry is not None:
            return entry[0]
        state = self._nodes[node_id]
        relay = _Relay(name=f"{node_id}.{port}", value=getattr(state, port))
        hold = self._hold

        def _forward(event):
            with hold():
                if event.type == "triggered":
                    # A trigger, e.g. after mutating a list in place, carries a
                    # value equal to the relay's, which assigning would not forward.
                    with param.discard_events(relay):
                        relay.value = event.new
                    relay.param.trigger("value")
                else:
                    relay.value = event.new

        watcher = state.param.watch(_forward, port)
        self._relays[(node_id, port)] = (relay, partial(state.param.unwatch, watcher))
        return relay

    def _release_relay(self, source: tuple[str, str]):
        entry = self._relays.pop(source, None)
        if entry is not None:
            entry[1]()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Defer binding list inputs until the block exits.

        Every edge into a list input rebinds it to all of its edges, so wiring
        N edges into one list input one by one costs O(N^2). Inside this block
        each list input is bound once, when the block exits, with all of its
        edges. Wiring an edge into any other input from a node downstream of a
        pending list input binds the pending inputs first, since validating
        the edge reads the value that node produces.
        """
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if not self._batch_depth:
                self._flush_pending_lists()

    def _flush_pending_lists(self, before: str | None = None):
        """Bind the pending list inputs, or only if node *before* is downstream of one."""
        if not self._pending_lists:
            return
        if before is not None:
            seen = set()
            queue = deque(target_id for target_id, _ in self._pending_lists)
            while queue:
                node = queue.popleft()
                if node not in seen:
                    seen.add(node)
                    queue.extend(self._successors.get(node, ()))
            if before not in seen:
                return
        while self._pending_lists:
            target_id, target_port = next(iter(self._pending_lists))
            del self._pending_lists[(target_id, target_port)]
            if target_id in self._nodes:
                self._bind_input(target_id, target_port)
        disposals, self._pending_disposals = self._pending_disposals, []
        self._dispose_edges(disposals)

    def _defer_binding(
        self, target_id: str, target_port: str, error_source: tuple[str, str]
    ) -> bool:
        """Return True if an input deep down the stack should not be updated right now.

        Each hop of a chain of nodes adds a dozen or more frames to the stack of
        the update that started it, so a long enough chain would raise a
        RecursionError out of whatever set the first value and leave the rest
        of the chain stale. With an event loop running, the update continues
        from a shallow stack on the next tick instead, by re-binding the input.
        Without one it runs as deep as it safely can, then stops and is reported.
        """
        limit = sys.getrecursionlimit()
        if not _stack_deeper_than(limit // 2):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if not _stack_deeper_than(limit - _STACK_MARGIN):
                return False
            if self._on_error:
                exc = RecursionError(
                    "The chain of connected nodes is too deep to update synchronously."
                )
                self._on_error(*error_source, target_id, target_port, exc)
            return True
        self._deferred[(target_id, target_port)] = None
        if not self._drain_scheduled:
            self._drain_scheduled = True
            param.parameterized.async_executor(self._drain_deferred)
        return True

    async def _drain_deferred(self):
        self._drain_scheduled = False
        deferred, self._deferred = list(self._deferred), {}
        # param.batch (param >= 2.5) runs the watchers a cascade triggers one
        # after another instead of nested, so the rest of the chain cannot
        # run out of stack again.
        with _iterative_updates():
            for target_id, target_port in deferred:
                if target_id not in self._nodes:
                    continue
                incoming = self._incoming(target_id, target_port)
                is_list = self._is_list_input(target_id, target_port)
                if not (incoming or is_list):
                    continue
                # Nothing awaits this task, so an error raised here would only
                # surface as an unretrieved task exception.
                try:
                    self._bind_input(target_id, target_port)
                except Exception as exc:
                    if self._on_error:
                        source = ("", "") if is_list or not incoming else next(iter(incoming))[:2]
                        self._on_error(*source, target_id, target_port, exc)
                    else:
                        logger.exception("Failed to update input %s.%s", target_id, target_port)

    def _build_edge(self, key: EdgeKey, steps: list[Transform]) -> Edge:
        source = self._relay(key[0], key[1]).param.value
        stage = (
            param.rx(source, error_mode="propagate") if self.propagate_failures else source.rx()
        )
        stages = [stage]
        for step in steps:
            stage = stage.rx.pipe(self._run_step, step, key)
            stages.append(stage)
        carried = None
        if self.propagate_failures:
            carried = stage.rx.pipe(self._carry_failure, self._failures[key[0]].param.failed)
        return Edge(steps, stages, carried, source=(key[0], key[1]))

    def _run_step(self, value, step: Transform, key: EdgeKey):
        try:
            return step(value)
        except Exception as exc:
            if self._on_error:
                self._on_error(*key, exc)
            if self.propagate_failures:
                # The rx node turns the exception into the edge's failure.
                raise
            raise param.Skip from exc

    @staticmethod
    def _carry_failure(value, failed: dict[str, InputFailure]):
        """Replace *value* with the node's failure while one of its inputs holds one.

        The node presents stale data for as long as an input is failed, and every
        output is derived from its inputs, so no output can be trusted.
        """
        return next(iter(failed.values())).error if failed else value

    @staticmethod
    def _parse(transform) -> tuple[list[Transform], str | None]:
        try:
            return parse_transform(transform), None
        except ValueError as exc:
            return [], f"Invalid transform: {error_text(exc)}"

    def _edge_value(self, key: EdgeKey, steps: list[Transform]):
        """Return the source's current value run through *steps*, raising if a step fails."""
        return apply_transform(steps, getattr(self._nodes[key[0]], key[1], None))

    def _bind_input(self, target_id: str, target_port: str, skip_initial_none: bool = False):
        """Bind an input port to the expressions of every edge feeding it.

        A list input collects the non-None values of all its edges; any other
        input follows its single edge. A list input left without edges is reset
        to empty, which also releases its previous binding.
        """
        target = self._nodes[target_id]
        incoming = self._incoming(target_id, target_port)
        is_list = self._is_list_input(target_id, target_port)
        if is_list and not incoming:
            if target_port in self._literals.get(target_id, {}):
                self._unbind_to_literal(target_id, target_port)
            else:
                _unbind(target, target_port, [])
            return

        error_source = ("", "") if is_list else next(iter(incoming))[:2]
        first_call = [skip_initial_none]
        keys = list(incoming)

        def _guard(*values):
            initial = bool(first_call) and first_call.pop()
            failure = next(
                ((key, v) for key, v in zip(keys, values, strict=True) if is_failure(v)),
                None,
            )
            value = [v for v in values if v is not None] if is_list else values[0]
            if failure is None and initial and value is None:
                raise param.Skip
            # A failure travels down a chain like any value, so it is deferred too.
            if self._defer_binding(target_id, target_port, error_source):
                raise param.Skip
            if failure is not None:
                return self._receive_failure(target_id, target_port, *failure)
            try:
                self._check_target_value(target_id, target_port, value)
            except Exception as exc:
                if self._on_error:
                    self._on_error(*error_source, target_id, target_port, exc)
                raise param.Skip from exc
            self._set_failure(target_id, target_port, None)
            return value

        # Set on the instance's own copy of the param, so states this graph did
        # not build can be wired too.
        target.param[target_port].allow_refs = True
        exprs = (e.expr for e in incoming.values())
        # Without process_failures param skips the call instead of handing the
        # guard a failure; it is only passed when set, as older param lacks it.
        options = {"process_failures": True} if self.propagate_failures else {}
        setattr(target, target_port, param.bind(_guard, *exprs, **options))

    def _receive_failure(self, target_id: str, target_port: str, key: EdgeKey, error):
        """Hand a failure to an input that handles it, or hold it for the node otherwise.

        A list input fails as a whole: showing the members that still work would
        pass off a partial result as the full one.
        """
        if self._handles_failures(target_id, target_port):
            return error
        self._set_failure(target_id, target_port, InputFailure(key[0], key[1], error))
        raise param.Skip

    def validate_connection(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, Any]] | None = None,
        *,
        types: bool = True,
    ) -> str | None:
        """Return a rejection reason without changing graph state, or None if allowed.

        With *types* False the ports' types and the value the edge would carry
        are not checked, only the wiring rules: ports that exist, a free
        input, no duplicate and no cycle.
        """
        source_spec = self._node_specs.get(source_id)
        target_spec = self._node_specs.get(target_id)
        if source_id not in self._nodes or target_id not in self._nodes:
            return "Source or target node not found."
        if not source_spec or not any(port.name == source_port for port in source_spec.outputs):
            return f"Output port '{source_port}' does not exist on source node."
        if not target_spec or not any(port.name == target_port for port in target_spec.inputs):
            return f"Input port '{target_port}' does not exist on target node."

        if (source_id, source_port, target_id, target_port) in self._edges:
            return "Connection already exists."
        is_list = self._is_list_input(target_id, target_port)
        if not is_list and self._incoming(target_id, target_port):
            return f"Input '{target_port}' already has a connection. Disconnect it first."

        if self._would_create_cycle(source_id, target_id):
            return "Connection rejected: would create a cycle."

        steps, error = self._parse(transform)
        if error:
            return error
        if not types:
            return None
        # A transform changes what the edge carries, so the declared output type
        # no longer applies and only the transformed value can be checked.
        if not steps:
            error = self._check_type_compatibility(
                source_spec, source_port, target_spec, target_port
            )
            if error:
                return error
        # Untyped outputs pass the static check, so test what they hold now.
        try:
            value = self._edge_value((source_id, source_port, target_id, target_port), steps)
        except Exception as exc:
            return f"Transform fails on the current value of '{source_port}': {error_text(exc)}"
        port = next(p for p in target_spec.inputs if p.name == target_port)
        reason = _check_item(port, value) if is_list else port.check_value(value)
        if reason:
            return f"Input '{target_port}' rejects the current value of '{source_port}': {reason}"
        return None

    def add_edge(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, Any]] | None = None,
    ) -> bool | str:
        """Wire an edge, returning True on success or a rejection reason.

        Parameters
        ----------
        transform
            Steps applied to every value carried along the edge, each a dict
            naming an ``op`` from :data:`panel_flowdash.transforms.TRANSFORMS`
            plus its arguments.
        """
        if not self._is_list_input(target_id, target_port):
            self._flush_pending_lists(source_id)
        error = self.validate_connection(source_id, source_port, target_id, target_port, transform)
        if error is not None:
            return error
        key = (source_id, source_port, target_id, target_port)
        return self._wire(key, parse_transform(transform))

    def set_edge_transform(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, Any]] | None,
    ) -> bool | str:
        """Replace the transform of an existing edge, returning True or a rejection reason.

        A rejected transform leaves the edge on its previous one.
        """
        key = (source_id, source_port, target_id, target_port)
        if key not in self._edges:
            return "Edge not found."
        steps, error = self._parse(transform)
        if error:
            return error
        return self._wire(key, steps)

    def _wire(self, key: EdgeKey, steps: list[Transform]) -> bool | str:
        _, _, target_id, target_port = key
        is_list = self._is_list_input(target_id, target_port)
        if not is_list:
            self._flush_pending_lists(key[0])
            # A connection whose first value is rejected would sit wired but
            # broken, so refuse it instead.
            try:
                self._check_target_value(target_id, target_port, self._edge_value(key, steps))
            except Exception as exc:
                return f"Connection rejected: {error_text(exc)}"

        previous = self._edges.get(key)
        self._store_edge(key, self._build_edge(key, steps))
        if is_list and self._batch_depth:
            self._pending_lists[(target_id, target_port)] = None
            if previous is not None:
                self._pending_disposals.append(previous)
            return True
        # An output nothing has set yet should not wipe the input's default.
        self._bind_input(target_id, target_port, skip_initial_none=not is_list)
        if previous is not None:
            self._dispose_edges([previous])
        return True

    def _would_create_cycle(self, source_id: str, target_id: str) -> bool:
        """Return True if adding an edge from source to target would create a cycle."""
        if source_id == target_id:
            return True
        successors = self._successors
        visited = set()
        queue = deque([target_id])
        while queue:
            node = queue.popleft()
            if node == source_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            queue.extend(successors.get(node, []))
        return False

    def _check_type_compatibility(
        self,
        source_spec: ComponentSpec,
        source_port: str,
        target_spec: ComponentSpec,
        target_port: str,
    ) -> str | None:
        """Return an error message if types are incompatible, None if OK."""
        source_type = None
        for port in source_spec.outputs:
            if port.name == source_port:
                source_type = port.type
                break

        target_type = None
        for port in target_spec.inputs:
            if port.name == target_port:
                target_type = connection_type(port)
                break

        if port_types_compatible(source_type, target_type):
            return None

        return (
            f"Type mismatch: output '{source_port}' produces '{source_type}' "
            f"but input '{target_port}' expects '{target_type}'."
        )

    def remove_edge(self, source_id: str, source_port: str, target_id: str, target_port: str):
        """Remove an edge and reset the target input to its literal or its default, see `_default`."""
        edge = self._drop_edge((source_id, source_port, target_id, target_port))
        self._set_failure(target_id, target_port, None)
        target_state = self._nodes.get(target_id)
        if target_state is not None and hasattr(target_state, target_port):
            if self._is_list_input(target_id, target_port):
                self._bind_input(target_id, target_port)
            elif target_port in self._literals.get(target_id, {}):
                self._unbind_to_literal(target_id, target_port)
            else:
                _unbind(target_state, target_port, self._default(target_id, target_port))
        if edge is not None:
            self._dispose_edges([edge])

    def _unbind_to_literal(self, instance_id: str, port: str):
        _unbind(self._nodes[instance_id], port, copy.deepcopy(self._literals[instance_id][port]))

    def is_wired(self, instance_id: str, port: str) -> bool:
        """Whether an edge feeds the input *port* of a node."""
        return bool(self._incoming(instance_id, port))

    def literals(self, instance_id: str) -> dict[str, Any]:
        """Return a copy of the literal values set on a node's inputs, by port."""
        return copy.deepcopy(self._literals.get(instance_id, {}))

    def set_literal(self, instance_id: str, port: str, value: Any):
        """Give the input *port* of a node a literal value, used while no edge feeds it.

        The value is checked against the input first; a wired input keeps the
        value of its edge and takes the literal once the edge is removed.

        Raises
        ------
        ValueError
            If the input does not exist, cannot hold a literal or rejects *value*.
        """
        input_port = self._input_port(instance_id, port)
        if input_port is None or instance_id not in self._nodes:
            raise ValueError(f"'{instance_id}' has no input '{port}'.")
        if (reason := check_literal(input_port, value)) is not None:
            raise ValueError(reason)
        value = port_literal(input_port, copy.deepcopy(value))
        self._literals.setdefault(instance_id, {})[port] = value
        if not self._incoming(instance_id, port):
            self._unbind_to_literal(instance_id, port)

    def clear_literal(self, instance_id: str, port: str):
        """Remove the literal value of an input; an unwired input goes back to its default.

        That is the port's default, or for a port without one the default of
        the component's Parameter, the value the component had before.
        """
        literals = self._literals.get(instance_id, {})
        if literals.pop(port, _NO_LITERAL) is _NO_LITERAL:
            return
        if not literals:
            self._literals.pop(instance_id, None)
        if self._incoming(instance_id, port):
            return
        if self._is_list_input(instance_id, port):
            self._bind_input(instance_id, port)
            return
        _unbind(self._nodes[instance_id], port, self._default(instance_id, port))

    def _default(self, instance_id: str, port: str) -> Any:
        """Return a copy of the value an unwired input without a literal goes back to.

        That is the port's default, or for a port without one the default of
        the component's Parameter, the value the component had before.
        """
        input_port = self._input_port(instance_id, port)
        if input_port is None:
            return None
        default = input_port.default
        if default is None and input_port.parameter is not None:
            default = input_port.parameter.default
        return copy.deepcopy(default)

    def get_state(self, instance_id: str) -> param.Parameterized | None:
        """Get the state instance for a node."""
        return self._nodes.get(instance_id)

    def get_failures(self, instance_id: str) -> InputFailures | None:
        """Get the failures held by a node's inputs, if the node exists."""
        return self._failures.get(instance_id)

    def get_config_state(self, instance_id: str) -> param.Parameterized | None:
        """Get the config state instance for a node, if it has config."""
        return self._config_states.get(instance_id)

    def get_edge_expression(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        stage: int = -1,
    ) -> param.rx | None:
        """Return the reactive expression carrying values along an edge, if it is wired.

        Parameters
        ----------
        stage
            Number of transform steps applied: 0 is the source port's own value.
            The default -1 is what the target receives, which with
            ``propagate_failures`` is the failure of the source node while one
            of its inputs holds one.
        """
        edge = self._edges.get((source_id, source_port, target_id, target_port))
        if edge is None:
            return None
        return edge.expr if stage == -1 else edge.stages[stage]

    def get_edge_transform(
        self, source_id: str, source_port: str, target_id: str, target_port: str
    ) -> list[dict[str, Any]] | None:
        """Return the transform steps of an edge as stored, or None if it is not wired."""
        edge = self._edges.get((source_id, source_port, target_id, target_port))
        return None if edge is None else [step.to_dict() for step in edge.steps]

    @property
    def edges(self) -> list[dict[str, Any]]:
        """All current edges."""
        return [
            {
                "source": src,
                "source_port": src_port,
                "target": tgt,
                "target_port": tgt_port,
                "transform": [step.to_dict() for step in edge.steps],
            }
            for (src, src_port, tgt, tgt_port), edge in self._edges.items()
        ]

    @property
    def node_ids(self) -> list[str]:
        """All current node instance IDs."""
        return list(self._nodes.keys())
