"""Dataflow wiring engine with runtime validation.

Each node in the graph gets a NodeState (a dynamic Parameterized subclass)
whose parameters correspond to the node's declared input and output ports.
Edges are wired via param.watch: when a source port changes, the value is
assigned to the target port inside a try/except so that runtime type errors
(e.g. param validation failures) are caught and reported via an error callback.
"""

from __future__ import annotations

from collections.abc import Callable

import param

from panel_flowdash.component_spec import ComponentSpec

_RESERVED_PARAMS = set(param.Parameterized.param)


def build_node_state_class(spec: ComponentSpec) -> type[param.Parameterized]:
    """Create a Parameterized subclass with one param per port (inputs + outputs)."""
    params: dict[str, param.Parameter] = {}

    for port in spec.inputs:
        if port.name in _RESERVED_PARAMS:
            continue
        params[port.name] = param.Parameter(default=port.default, allow_None=True, allow_refs=True)

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
        self._specs = specs
        self._state_classes: dict[str, type[param.Parameterized]] = {}
        self._nodes: dict[str, param.Parameterized] = {}
        self._node_specs: dict[str, ComponentSpec] = {}
        self._edges: list[dict[str, str]] = []
        self._watchers: dict[tuple, param.parameterized.Watcher] = {}
        self._on_error = on_error

        for comp_id, spec in specs.items():
            self._state_classes[comp_id] = build_node_state_class(spec)

    def add_node(self, instance_id: str, component_id: str) -> param.Parameterized:
        """Create a new node state instance."""
        spec = self._specs[component_id]
        cls = self._state_classes[component_id]
        state = cls(name=instance_id)
        self._nodes[instance_id] = state
        self._node_specs[instance_id] = spec
        return state

    def remove_node(self, instance_id: str):
        """Remove a node and any edges connected to it."""
        keys_to_remove = [k for k in self._watchers if k[0] == instance_id or k[2] == instance_id]
        for key in keys_to_remove:
            watcher = self._watchers.pop(key)
            src = self._nodes.get(key[0])
            if src is not None:
                src.param.unwatch(watcher)

        self._edges = [
            e for e in self._edges if e["source"] != instance_id and e["target"] != instance_id
        ]
        self._nodes.pop(instance_id, None)
        self._node_specs.pop(instance_id, None)

    def add_edge(
        self,
        source_id: str,
        source_port: str,
        target_id: str,
        target_port: str,
    ) -> bool | str:
        """Wire an edge between two ports.

        Returns True on success, or an error message string on failure.
        """
        source_state = self._nodes.get(source_id)
        target_state = self._nodes.get(target_id)
        if source_state is None or target_state is None:
            return "Source or target node not found."
        if not hasattr(source_state.param, source_port):
            return f"Output port '{source_port}' does not exist on source node."
        if not hasattr(target_state.param, target_port):
            return f"Input port '{target_port}' does not exist on target node."

        for e in self._edges:
            if e["target"] == target_id and e["target_port"] == target_port:
                return f"Input '{target_port}' already has a connection. Disconnect it first."

        if self._would_create_cycle(source_id, target_id):
            return "Connection rejected: would create a cycle."

        source_spec = self._node_specs.get(source_id)
        target_spec = self._node_specs.get(target_id)
        if source_spec and target_spec:
            error = self._check_type_compatibility(
                source_spec, source_port, target_spec, target_port
            )
            if error:
                return error

        def _propagate(
            event,
            _src_id=source_id,
            _src_port=source_port,
            _tgt_id=target_id,
            _tgt_port=target_port,
            _target=target_state,
        ):
            try:
                setattr(_target, _tgt_port, event.new)
            except Exception as exc:
                if self._on_error:
                    self._on_error(_src_id, _src_port, _tgt_id, _tgt_port, exc)

        watcher = source_state.param.watch(_propagate, source_port)
        edge_key = (source_id, source_port, target_id, target_port)
        self._watchers[edge_key] = watcher

        current = getattr(source_state, source_port)
        if current is not None:
            try:
                setattr(target_state, target_port, current)
            except Exception as exc:
                if self._on_error:
                    self._on_error(source_id, source_port, target_id, target_port, exc)

        self._edges.append(
            {
                "source": source_id,
                "source_port": source_port,
                "target": target_id,
                "target_port": target_port,
            }
        )
        return True

    def _would_create_cycle(self, source_id: str, target_id: str) -> bool:
        """Return True if adding an edge from source to target would create a cycle."""
        if source_id == target_id:
            return True
        visited = set()
        queue = [target_id]
        while queue:
            node = queue.pop(0)
            if node == source_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            for e in self._edges:
                if e["source"] == node:
                    queue.append(e["target"])
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
        """Remove an edge, unsubscribe the watcher, and reset target to default."""
        self._edges = [
            e
            for e in self._edges
            if not (
                e["source"] == source_id
                and e["source_port"] == source_port
                and e["target"] == target_id
                and e["target_port"] == target_port
            )
        ]

        edge_key = (source_id, source_port, target_id, target_port)
        watcher = self._watchers.pop(edge_key, None)
        if watcher is not None:
            source_state = self._nodes.get(source_id)
            if source_state is not None:
                source_state.param.unwatch(watcher)

        target_state = self._nodes.get(target_id)
        if target_state is not None and hasattr(target_state, target_port):
            spec = self._node_specs.get(target_id)
            default = None
            if spec:
                for port in spec.inputs:
                    if port.name == target_port:
                        default = port.default
                        break
            setattr(target_state, target_port, default)

    def get_state(self, instance_id: str) -> param.Parameterized | None:
        """Get the state instance for a node."""
        return self._nodes.get(instance_id)

    @property
    def edges(self) -> list[dict[str, str]]:
        """All current edges."""
        return list(self._edges)

    @property
    def node_ids(self) -> list[str]:
        """All current node instance IDs."""
        return list(self._nodes.keys())
