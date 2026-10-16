"""Dataflow wiring engine with runtime validation.

Each node in the graph gets a NodeState (a dynamic Parameterized subclass)
whose parameters correspond to the node's declared input and output ports.
Every edge is a ``param.rx`` expression on its source port, extended by one
pipe per step of the edge's transform, and a connected input is bound to the
expressions feeding it as a param reference, so param itself propagates
values. The binding checks each value against the target first; a rejected
value, like a failing transform step, is reported via the error callback and
skipped, which leaves the target on its last good value.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import param

from panel_flowdash.component_spec import ComponentSpec
from panel_flowdash.transforms import Transform, apply_transform, parse_transform

_RESERVED_PARAMS = set(param.Parameterized.param)

EdgeKey = tuple[str, str, str, str]


@dataclass
class Edge:
    """The transform steps of an edge and the expression after each of them.

    ``stages[0]`` is the source port itself, so ``stages[i]`` is the value
    after ``i`` steps and ``stages[-1]`` is what the target receives.
    """

    steps: list[Transform]
    stages: list[param.rx]

    @property
    def expr(self) -> param.rx:
        return self.stages[-1]


def _is_list_port(port) -> bool:
    """Return True if a port's declared type indicates a list/multi-connection input."""
    if port.multiple is not None:
        return port.multiple
    if port.type is None:
        return False
    return port.type.lower() in ("list", "List")


def build_node_state_class(spec: ComponentSpec) -> type[param.Parameterized]:
    """Create a Parameterized subclass with one param per port (inputs + outputs)."""
    params: dict[str, param.Parameter] = {}

    for port in spec.inputs:
        if port.name in _RESERVED_PARAMS:
            continue
        if (port.type or "").lower() == "list":
            params[port.name] = param.List(
                default=port.default
                if port.default is not None
                else ([] if _is_list_port(port) else None),
                allow_None=not _is_list_port(port),
                allow_refs=True,
            )
        else:
            params[port.name] = param.Parameter(
                default=port.default, allow_None=True, allow_refs=True
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
    ):
        """Initialize the dataflow graph.

        Parameters
        ----------
        specs
            Component specs keyed by component_id.
        on_error
            Callback invoked when a runtime value assignment fails.
            Signature: (source_id, source_port, target_id, target_port, exception)
        """
        self._specs = dict(specs)
        self._state_classes: dict[str, type[param.Parameterized]] = {}
        self._nodes: dict[str, param.Parameterized] = {}
        self._config_states: dict[str, param.Parameterized] = {}
        self._node_specs: dict[str, ComponentSpec] = {}
        self._edges: dict[EdgeKey, Edge] = {}
        self._on_error = on_error

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
        if spec.config_state_class is not None:
            self._config_states[instance_id] = spec.config_state_class(
                name=f"{instance_id}_config"
            )
        return state

    def remove_node(self, instance_id: str):
        """Remove a node and any edges connected to it."""
        removed = [key for key in self._edges if instance_id in (key[0], key[2])]
        for key in removed:
            del self._edges[key]
        for target_id, target_port in {key[2:] for key in removed}:
            if target_id == instance_id:
                continue
            if self._is_list_input(target_id, target_port):
                self._bind_input(target_id, target_port)
            else:
                # Releases the binding to the removed node but keeps its last value.
                target = self._nodes[target_id]
                setattr(target, target_port, getattr(target, target_port))

        self._nodes.pop(instance_id, None)
        self._config_states.pop(instance_id, None)
        self._node_specs.pop(instance_id, None)

    def _input_port(self, target_id: str, target_port: str):
        spec = self._node_specs.get(target_id)
        if not spec:
            return None
        return next((port for port in spec.inputs if port.name == target_port), None)

    def _is_list_input(self, target_id: str, target_port: str) -> bool:
        """Return True if the target port is a List-typed multi-connection input."""
        port = self._input_port(target_id, target_port)
        return port is not None and _is_list_port(port)

    def _check_target_value(self, target_id: str, target_port: str, value):
        """Raise if the target state or the component behind it would reject *value*."""
        if value is None:
            return
        self._nodes[target_id].param[target_port]._validate(value)
        port = self._input_port(target_id, target_port)
        if port is not None and port.parameter is not None:
            port.parameter._validate(value)

    def _incoming(self, target_id: str, target_port: str) -> dict[EdgeKey, Edge]:
        return {
            key: edge for key, edge in self._edges.items() if key[2:] == (target_id, target_port)
        }

    def _build_edge(self, key: EdgeKey, steps: list[Transform]) -> Edge:
        stage = self._nodes[key[0]].param[key[1]].rx()
        stages = [stage]
        for step in steps:
            stage = stage.rx.pipe(self._run_step, step, key)
            stages.append(stage)
        return Edge(steps, stages)

    def _run_step(self, value, step: Transform, key: EdgeKey):
        try:
            return step(value)
        except Exception as exc:
            if self._on_error:
                self._on_error(*key, exc)
            raise param.Skip from exc

    @staticmethod
    def _parse(transform) -> tuple[list[Transform], str | None]:
        try:
            return parse_transform(transform), None
        except ValueError as exc:
            return [], f"Invalid transform: {exc}"

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
            setattr(target, target_port, [])
            return

        error_source = ("", "") if is_list else next(iter(incoming))[:2]
        first_call = [skip_initial_none]

        def _guard(*values):
            value = [v for v in values if v is not None] if is_list else values[0]
            if first_call and first_call.pop() and value is None:
                raise param.Skip
            try:
                self._check_target_value(target_id, target_port, value)
            except Exception as exc:
                if self._on_error:
                    self._on_error(*error_source, target_id, target_port, exc)
                raise param.Skip from exc
            return value

        # Set on the instance's own copy of the param, so states this graph did
        # not build can be wired too.
        target.param[target_port].allow_refs = True
        setattr(target, target_port, param.bind(_guard, *(e.expr for e in incoming.values())))

    def validate_connection(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
        transform: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Return a rejection reason without changing graph state, or None if allowed."""
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
        if is_list:
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
            return f"Transform fails on the current value of '{source_port}': {exc}"
        port = next(p for p in target_spec.inputs if p.name == target_port)
        reason = port.check_value(value)
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
            # A connection whose first value is rejected would sit wired but
            # broken, so refuse it instead.
            try:
                self._check_target_value(target_id, target_port, self._edge_value(key, steps))
            except Exception as exc:
                return f"Connection rejected: {exc}"

        self._edges[key] = self._build_edge(key, steps)
        # An output nothing has set yet should not wipe the input's default.
        self._bind_input(target_id, target_port, skip_initial_none=not is_list)
        return True

    def _would_create_cycle(self, source_id: str, target_id: str) -> bool:
        """Return True if adding an edge from source to target would create a cycle."""
        if source_id == target_id:
            return True
        successors = {}
        for src, _, tgt, _ in self._edges:
            successors.setdefault(src, []).append(tgt)
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
                target_type = port.type
                break

        if source_type is None or target_type is None:
            return None

        if source_type.lower() == target_type.lower():
            return None

        return (
            f"Type mismatch: output '{source_port}' produces '{source_type}' "
            f"but input '{target_port}' expects '{target_type}'."
        )

    def remove_edge(self, source_id: str, source_port: str, target_id: str, target_port: str):
        """Remove an edge and reset the target input to its default."""
        self._edges.pop((source_id, source_port, target_id, target_port), None)
        target_state = self._nodes.get(target_id)
        if target_state is None or not hasattr(target_state, target_port):
            return
        if self._is_list_input(target_id, target_port):
            self._bind_input(target_id, target_port)
        else:
            port = self._input_port(target_id, target_port)
            setattr(target_state, target_port, port.default if port else None)

    def get_state(self, instance_id: str) -> param.Parameterized | None:
        """Get the state instance for a node."""
        return self._nodes.get(instance_id)

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
            Number of transform steps applied: 0 is the source port's own value,
            the default -1 is what the target receives.
        """
        edge = self._edges.get((source_id, source_port, target_id, target_port))
        return None if edge is None else edge.stages[stage]

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
