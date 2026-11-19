"""Groups: nodes framed together on the canvas, which can be collapsed into one node.

A group is a label and a set of nodes. It changes nothing about the dataflow:
the nodes stay ordinary nodes, with their own config, connections and tiles,
and collapsing a group only changes how the canvas draws them. A collapsed
group shows the ports its nodes are connected through, the ones of the
connections that cross its border, so the dataflow stays readable.

In the dashboard YAML groups are a top-level mapping by name, and each node
names its group::

    groups:
      revenue: {label: Paid revenue by region, collapsed: true}
    nodes:
      revenue_sql: {component: Lumen/sql, group: revenue, config: {...}}

A node belongs to at most one group, and groups do not nest.
"""

from __future__ import annotations

import copy
import re
import typing as t
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

# The ids a group can have, the same as the node names of the dashboard YAML.
GROUP_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")

# The longest label a group keeps.
MAX_LABEL_LENGTH = 200

# Canvas node ids of groups are the group id after this prefix. Component
# instance ids never contain a colon, so they cannot collide.
GROUP_NODE_PREFIX = "group:"

# The canvas node type panel-reactflow draws groups with.
GROUP_NODE_TYPE = "group"

# A collapsed group lists its ports in rows below its header, like a node card.
GROUP_HEADER_HEIGHT = 32
GROUP_PORT_ROW_HEIGHT = 22
# The most port rows the canvas stylesheet sizes a collapsed group for.
MAX_GROUP_PORT_ROWS = 12

# Prefixes of a collapsed group's handle ids, so an input and an output of the
# same name never share one.
INPUT_HANDLE_PREFIX = "in:"
OUTPUT_HANDLE_PREFIX = "out:"


class GroupError(ValueError):
    """A group's data is malformed. ``key`` names the offending field."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@dataclass
class DashboardGroup:
    """Nodes framed together on the canvas.

    Attributes
    ----------
    id
        Unique within the dashboard, a name like the dashboard YAML's node names.
    label
        Shown in the group's header; the id is shown when it is None.
    nodes
        Instance ids of the nodes in the group, in the order they were grouped.
    collapsed
        Whether the canvas draws the group as one node.
    """

    id: str
    label: str | None = None
    nodes: list[str] = field(default_factory=list)
    collapsed: bool = False

    @property
    def title(self) -> str:
        return self.label or self.id

    def to_dict(self) -> dict[str, t.Any]:
        data: dict[str, t.Any] = {"id": self.id, "nodes": list(self.nodes)}
        if self.label is not None:
            data["label"] = self.label
        if self.collapsed:
            data["collapsed"] = True
        return data

    @classmethod
    def from_dict(cls, data: t.Any) -> DashboardGroup:
        if not isinstance(data, Mapping):
            raise GroupError("group", f"must be a mapping, got {data!r}.")
        group_id = data.get("id")
        check_group_id(group_id)
        label = data.get("label")
        if label is not None and not isinstance(label, str):
            raise GroupError("label", f"must be a string, got {label!r}.")
        if label is not None and len(label) > MAX_LABEL_LENGTH:
            raise GroupError("label", f"must be at most {MAX_LABEL_LENGTH} characters long.")
        collapsed = data.get("collapsed", False)
        if not isinstance(collapsed, bool):
            raise GroupError("collapsed", f"must be true or false, got {collapsed!r}.")
        nodes = data.get("nodes", [])
        if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
            raise GroupError("nodes", f"must be a list of node ids, got {nodes!r}.")
        return cls(
            id=group_id, label=label or None, nodes=list(dict.fromkeys(nodes)), collapsed=collapsed
        )


def check_group_id(group_id: t.Any):
    """Raise :class:`GroupError` unless *group_id* can name a group."""
    if not isinstance(group_id, str) or not GROUP_ID_PATTERN.match(group_id) or "__" in group_id:
        raise GroupError(
            "id",
            f"must start with a letter or underscore, contain only letters, digits, '_' "
            f"and '-', no '__', and be at most 64 characters long, got {group_id!r}.",
        )


def decode_groups(value: t.Any) -> list[DashboardGroup]:
    """Return the groups a stored dashboard holds, raising ValueError when malformed.

    A node in more than one group stays in the first, and groups left with no
    node are dropped.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise GroupError("groups", f"must be a list, got {value!r}.")
    groups: list[DashboardGroup] = []
    seen_ids: set[str] = set()
    grouped: set[str] = set()
    for entry in value:
        group = DashboardGroup.from_dict(entry)
        if group.id in seen_ids:
            raise GroupError("id", f"repeats the id of an earlier group: {group.id!r}.")
        seen_ids.add(group.id)
        group.nodes = [n for n in group.nodes if n not in grouped]
        grouped.update(group.nodes)
        if group.nodes:
            groups.append(group)
    return groups


def group_node_id(group_id: str) -> str:
    """Return the id of the canvas node that draws group *group_id*."""
    return f"{GROUP_NODE_PREFIX}{group_id}"


def group_id_of(node_id: str) -> str | None:
    """Return the group a canvas node id draws, or None for a component's node."""
    if isinstance(node_id, str) and node_id.startswith(GROUP_NODE_PREFIX):
        return node_id[len(GROUP_NODE_PREFIX) :]
    return None


def is_group_node(node: Mapping) -> bool:
    """Whether a canvas node draws a group rather than a component."""
    return node.get("type") == GROUP_NODE_TYPE


def component_nodes(nodes: Iterable[Mapping]) -> list[Mapping]:
    """Return the canvas nodes that are components, leaving out group frames."""
    return [node for node in nodes if not is_group_node(node)]


def unique_group_id(base: str, taken: Iterable[str]) -> str:
    """Return a group id made from *base* that is not in *taken*."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", base or "").strip("_-")
    slug = re.sub(r"_{2,}", "_", slug)[:56] or "group"
    if not re.match(r"[A-Za-z_]", slug):
        slug = f"group_{slug}"
    taken = set(taken)
    candidate, n = slug, 2
    while candidate in taken:
        candidate = f"{slug}_{n}"
        n += 1
    return candidate


@dataclass(frozen=True)
class GroupPort:
    """A port of a group: the inner ports one connection across its border reaches.

    An input is named after the inner port it feeds and reaches every inner
    port fed by the same outside output; an output is one inner output read
    from outside the group.
    """

    name: str
    direction: str
    ends: tuple[tuple[str, str], ...]
    label: str
    type: str | None = None

    @property
    def handle(self) -> str:
        prefix = INPUT_HANDLE_PREFIX if self.direction == "input" else OUTPUT_HANDLE_PREFIX
        return f"{prefix}{self.name}"


PortInfo = Callable[[str, str, str], tuple[str, str | None]]


def group_ports(
    members: Iterable[str],
    edges: Iterable[tuple[str, str, str, str]],
    port_info: PortInfo | None = None,
) -> tuple[list[GroupPort], list[GroupPort]]:
    """Return the inputs and outputs of a group from the connections crossing its border.

    *edges* are ``(source, source_port, target, target_port)`` tuples by
    instance id. *port_info(instance_id, port, direction)* returns the label
    and type of an inner port, which the group's port takes over.
    """
    members = set(members)
    inputs: dict[tuple[str, str], list[tuple[str, str]]] = {}
    outputs: dict[tuple[str, str], None] = {}
    for source, source_port, target, target_port in edges:
        inside_source, inside_target = source in members, target in members
        if inside_target and not inside_source:
            ends = inputs.setdefault((source, source_port), [])
            if (target, target_port) not in ends:
                ends.append((target, target_port))
        elif inside_source and not inside_target:
            outputs.setdefault((source, source_port), None)

    def info(node: str, port: str, direction: str) -> tuple[str, str | None]:
        return port_info(node, port, direction) if port_info else (port, None)

    def named(items: list[tuple[str, tuple[tuple[str, str], ...], str]]) -> list[GroupPort]:
        ports, taken = [], set()
        for direction, ends, port in items:
            name, n = port, 2
            while name in taken:
                name = f"{port}_{n}"
                n += 1
            taken.add(name)
            label, port_type = info(ends[0][0], ends[0][1], direction)
            ports.append(
                GroupPort(name, direction, ends, label if name == port else name, port_type)
            )
        return ports

    return (
        named([("input", tuple(ends), ends[0][1]) for ends in inputs.values()]),
        named([("output", (end,), end[1]) for end in outputs]),
    )


def group_node_data(
    group: DashboardGroup, inputs: list[GroupPort], outputs: list[GroupPort]
) -> dict[str, t.Any]:
    """Return the canvas node data that draws *group* with its ports.

    ``handle_map`` ties each inner port to the group handle that stands in for
    it while the group is collapsed, so connections are drawn to it and a
    connection made to it reaches every inner port it maps.
    """

    def handle(port: GroupPort, row: int) -> dict[str, t.Any]:
        top = GROUP_HEADER_HEIGHT + 4 + GROUP_PORT_ROW_HEIGHT * row + GROUP_PORT_ROW_HEIGHT / 2
        spec: dict[str, t.Any] = {"id": port.handle, "label": port.label, "top": top}
        if port.type:
            spec["type"] = port.type
        return spec

    return {
        "collapsed": group.collapsed,
        "inputs": [handle(port, row) for row, port in enumerate(inputs)],
        "outputs": [handle(port, row) for row, port in enumerate(outputs)],
        "handle_map": [
            {
                "node": node,
                "handle": port_name,
                "group_handle": port.handle,
                "direction": port.direction,
            }
            for port in [*inputs, *outputs]
            for node, port_name in port.ends
        ],
    }


def group_class(inputs: list[GroupPort], outputs: list[GroupPort]) -> str:
    """Return the CSS class that sizes a collapsed group's frame for its port rows."""
    return f"fd-group-rows-{min(max(len(inputs), len(outputs), 1), MAX_GROUP_PORT_ROWS)}"


def copy_groups(groups: Mapping[str, DashboardGroup]) -> dict[str, DashboardGroup]:
    """Return *groups* as copies, so changing them changes no group of an editor."""
    return {group_id: copy.deepcopy(group) for group_id, group in groups.items()}
