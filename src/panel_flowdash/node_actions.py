"""Actions a component library adds to the menus of a node, and the provenance a node reports.

A library registers a :class:`NodeAction` with :func:`register_node_action`;
the editor offers it in the ⋮ menu of the sidecar's header and of the
node's tile, for each node it ``applies`` to, in the ``modes`` it declares
(edit mode only by default), and calls ``run`` with a
:class:`NodeActionContext` when it is picked. An action marked
``admin_only`` is only offered to, and only runs for, a person the editor's
``authorize_admin`` says administers the project; it is asked again, with
the project file read afresh, before the action runs.

A component reports where its numbers come from by defining
``__flowdash_provenance__(self, via=())``, returning a :class:`Provenance`
(or a dict of its fields) or None. A view that reads such a node, directly
or through others, shows the nearest one's label: a chip on its tile while
the dashboard is edited (and in view mode when the editor's
``provenance_labels`` is on), under the sidecar's header and first in "How
was this made?".

``via`` holds the edges the numbers travel along from the reporting node
to the view asking, nearest the reporting node first, each ``{"from",
"to", "transform"}`` as :meth:`NodeActionContext.inputs` gives them; it is
empty when a node reports on itself. A hook reads it to tell what the view
shows from what the node holds, e.g. a named metric's rows from their
average taken by an edge's ``aggregate`` step, and returns a label for the
derived number, or None to pass the question further up. The ``via``
argument is optional: a hook that does not take it is called without it.

See the how-to guide "Node Actions and Provenance".
"""

from __future__ import annotations

import inspect
import logging
import typing as t
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType

if t.TYPE_CHECKING:
    from panel_flowdash.agent.ops import DashboardOps
    from panel_flowdash.editor import FlowDash
    from panel_flowdash.project import Project

logger = logging.getLogger(__name__)

PROVENANCE_HOOK = "__flowdash_provenance__"

#: The modes an action may apply in: ``"edit"`` while the dashboard may be
#: edited (editable, not read only, not previewed), ``"view"`` otherwise.
MODES = ("edit", "view")

__all__ = [
    "MODES",
    "PROVENANCE_HOOK",
    "NodeAction",
    "NodeActionContext",
    "NodeActionOps",
    "Provenance",
    "call_provenance_hook",
    "node_actions",
    "register_node_action",
    "unregister_node_action",
]


@dataclass(frozen=True)
class Provenance:
    """Where a node's numbers come from, e.g. a named metric or a query of its own."""

    label: str
    """A short label, e.g. "Metric: capacity" or "Ad hoc"."""
    detail: str = ""
    """A sentence shown on hover, e.g. the metric's description or how the number was computed."""
    kind: str = ""
    """What kind of provenance it is, e.g. "metric" or "ad hoc", for styling and tests."""
    icon: str | None = None
    """A Material icon name for the chip."""


@dataclass(frozen=True)
class NodeAction:
    """An entry a component library adds to a node's ⋮ menus."""

    key: str
    """A unique id, e.g. ``"lumen.save_as_metric"``."""
    label: str
    """The menu label, e.g. "Save as metric…"."""
    run: Callable[[NodeActionContext], t.Any]
    """Called with the :class:`NodeActionContext` of the node when the action is picked."""
    applies: Callable[[NodeActionContext], bool] | None = None
    """Whether the action applies to a node; None applies to all. Called whenever a menu is built."""
    icon: str | None = None
    admin_only: bool = False
    """Only offer and run the action for an admin of the project, see ``FlowDash.authorize_admin``."""
    modes: frozenset[str] = field(default_factory=lambda: frozenset({"edit"}))
    """The modes of :data:`MODES` the action is offered and runs in; edit mode only by default."""

    def __post_init__(self):  # noqa: D105
        modes = frozenset(self.modes)
        if not modes or modes - set(MODES):
            raise ValueError(f"modes must be a non-empty set of {MODES}, got {sorted(modes)}")
        object.__setattr__(self, "modes", modes)


class NodeActionOps:
    """The part of :class:`~panel_flowdash.agent.DashboardOps` a node action may use.

    It reads the open dashboard and changes its nodes, edges, tiles,
    groups, filters and parameters, each change checked and recorded as the
    full ops do. It leaves out what reaches past the node's dashboard or
    past an edit the person could undo: saving, opening, listing, renaming
    or replacing dashboards, their settings and report, sending files and
    running the assistant's pending questions. A library that needs one of
    those offers its own UI for it rather than doing it from a node's menu.
    It is a fence for well-behaved libraries, not a security boundary: an
    action runs in-process and can reach the editor through Python anyway.
    """

    #: The names of the :class:`~panel_flowdash.agent.DashboardOps` methods it offers.
    METHODS = frozenset(
        {
            "add_filter",
            "add_node",
            "arrange_canvas",
            "arrange_layout",
            "arrange_tiles",
            "bind_parameters",
            "connect",
            "declare_parameter",
            "describe_layout",
            "disconnect",
            "filter_reach",
            "filters",
            "group",
            "group_nodes",
            "hidden_tiles",
            "list_components",
            "node_config",
            "node_output",
            "nodes",
            "parameters",
            "place_tile",
            "preview",
            "remove_filter",
            "remove_node",
            "remove_parameter",
            "revision",
            "set_config",
            "set_inputs",
            "set_transform",
            "ungroup",
            "validate",
        }
    )

    def __init__(self, ops: DashboardOps):
        self._ops = ops

    def get_dashboard(self) -> str:
        """Return the open dashboard as YAML, see ``DashboardOps.get_dashboard``."""
        return self._ops.get_dashboard()

    def __getattr__(self, name: str) -> t.Any:  # noqa: D105
        if name in self.METHODS:
            return getattr(self._ops, name)
        raise AttributeError(f"Node actions cannot use DashboardOps.{name}.")

    def __dir__(self) -> list[str]:  # noqa: D105
        return sorted({*self.METHODS, "get_dashboard"})


class NodeActionContext:
    """What a node action may read and change: the node, the dashboard through its ops, the project.

    Nodes and ports are named as in the dashboard YAML. :attr:`node` is the
    node the action was picked on; the read methods take another node's
    name to read that one instead. Changes go through :attr:`ops`, a
    :class:`NodeActionOps` limited to the open dashboard, with the rights of
    the person using the editor, and :meth:`edit` makes several of them one
    step of the edit history.
    """

    def __init__(self, editor: FlowDash, instance_id: str):
        from panel_flowdash.agent.ops import DashboardOps

        self._editor = editor
        self._instance_id = instance_id
        self._ops = DashboardOps(editor)
        #: The dashboard's :class:`NodeActionOps`, to read and change it.
        self.ops = NodeActionOps(self._ops)
        #: The name of the node the action was picked on.
        self.node: str = editor._node_name(instance_id)

    # Reading

    @property
    def context(self) -> Mapping[str, t.Any]:
        """A read-only view of the resources the host gave the editor, see ``dashboard_context``."""
        return MappingProxyType(self._editor.context)

    @property
    def project(self) -> Project | None:
        """The project file the app reads, e.g. to save a section with ``Project.set``; None without one."""
        return self._editor.project

    @property
    def mode(self) -> str:
        """The mode the editor is in, one of :data:`MODES`."""
        return self._editor._action_mode()

    def _id(self, name: str | None) -> str:
        return self._instance_id if name is None else self._ops._instance(name)

    def nodes(self) -> list[str]:
        """Return the names of the dashboard's nodes."""
        return [node["name"] for node in self.ops.nodes()]

    def component(self, name: str | None = None) -> str | None:
        """Return the component id of a node, or None for a placeholder or a node it does not know."""
        try:
            instance_id = self._id(name)
        except Exception:
            return None
        item = next((i for i in self._editor._tile_items if i["instance_id"] == instance_id), None)
        if item is None or "placeholder" in item:
            return None
        return self._editor._resolve_component_id(item["component_id"])

    def config(self, name: str | None = None) -> dict[str, t.Any]:
        """Return a copy of a node's config."""
        return self.ops.node_config(name or self.node)

    def output(self, port: str, name: str | None = None) -> t.Any:
        """Return the current value of a node's output port, see ``DashboardOps.node_output``."""
        return self.ops.node_output(name or self.node, port)

    def input_value(self, port: str, name: str | None = None) -> t.Any:
        """Return the value a node's input port holds now, after its edge's transform."""
        state = self._editor._dataflow_graph.get_state(self._id(name))
        return getattr(state, port, None) if state is not None and port in state.param else None

    def _edges(self, side: str, name: str | None) -> list[dict[str, t.Any]]:
        instance_id = self._id(name)
        names = self._editor._node_name
        return [
            {
                "from": f"{names(e['source'])}.{e['source_port']}",
                "to": f"{names(e['target'])}.{e['target_port']}",
                "transform": e.get("transform") or [],
            }
            for e in self._editor._dataflow_graph.edges
            if e[side] == instance_id
        ]

    def inputs(self, name: str | None = None) -> list[dict[str, t.Any]]:
        """Return the edges into a node, each ``{"from", "to", "transform"}`` with ``node.port`` ends."""
        return self._edges("target", name)

    def outputs(self, name: str | None = None) -> list[dict[str, t.Any]]:
        """Return the edges out of a node, as :meth:`inputs` does."""
        return self._edges("source", name)

    def tile(self, name: str | None = None) -> dict[str, t.Any] | None:
        """Return where a node's visible tile is, ``{"area", "index", "width", "height"}``, or None."""
        from panel_flowdash.ui import tile_layout

        editor, instance_id = self._editor, self._id(name)
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        area = tile_layout.area_of(editor, item) if item is not None else None
        entry = tile_layout.tile_entry(editor, instance_id)
        if area is None or entry.get("visible") is False:
            return None
        ids = [i["instance_id"] for i in tile_layout.area_items(editor, area)]
        return {
            "area": area,
            "index": ids.index(instance_id),
            "width": entry.get("width"),
            "height": entry.get("height"),
        }

    def is_admin(self) -> bool:
        """Whether the person still administers the project, with the project file read again."""
        return self._editor.is_admin(fresh=True)

    # Changing

    @contextmanager
    def edit(self) -> Iterator[None]:
        """Make the changes inside the block one step of the edit history, which one undo reverts.

        If the block raises, the dashboard goes back to how it was before
        the block, also in an editor without an edit history, and the
        failed changes are neither undone nor redone later.
        """
        with self._editor._history.grouped(atomic=True):
            yield

    def replace_node(
        self,
        name: str,
        component: str,
        config: Mapping[str, t.Any] | None = None,
        *,
        new_name: str | None = None,
        rewire: Callable[[dict[str, t.Any]], tuple[str, list[dict[str, t.Any]] | None]]
        | None = None,
    ) -> str:
        """Replace node *name* by a new node of *component*, wired to what it fed, in its tile slot.

        Each edge out of the old node (as :meth:`outputs` gives it) is passed
        to *rewire*, which returns the new node's output port for it and the
        edge's transform; without *rewire* each edge keeps its port and
        transform. *rewire* may raise ``ValueError`` to refuse an edge. The
        new node takes the old one's tile, or stays hidden when the old one
        had no visible tile, and the old node is removed. One step of the
        edit history; if any step fails nothing is changed. Returns the new
        node's name.
        """
        mapped = []
        for edge in self.outputs(name):
            if rewire is None:
                mapped.append((edge, edge["from"].rsplit(".", 1)[1], edge["transform"]))
            else:
                mapped.append((edge, *rewire(edge)))
        slot = self.tile(name)
        with self.edit():
            created = self.ops.add_node(component, dict(config or {}), name=new_name)
            for edge, port, transform in mapped:
                self.ops.disconnect(edge["from"], edge["to"])
                self.ops.connect(f"{created}.{port}", edge["to"], transform or None)
            if slot is None:
                self.ops.place_tile(created, visible=False)
            elif slot["area"] == "grid":
                self.ops.place_tile(
                    created, index=slot["index"], width=slot["width"], height=slot["height"]
                )
            else:
                self.ops.place_tile(created, area=slot["area"], index=slot["index"])
            self.ops.remove_node(name)
        return created

    # Talking to the person

    def notify(self, kind: str, message: str) -> None:
        """Show a notification; *kind* is ``"success"``, ``"info"``, ``"warning"`` or ``"error"``."""
        self._editor._notify(kind, message)

    def open_dialog(self, title: str, content: t.Any, *, width: str | bool = "sm") -> None:
        """Show *content* in the session's page dialog, see ``panel_flowdash.util.open_dialog``."""
        from panel_flowdash.util import open_dialog

        open_dialog(title, content, width=width, full_width=True)

    def close_dialog(self) -> None:
        """Close the session's page dialog."""
        from panel_flowdash.util import close_dialog

        close_dialog()


_ACTIONS: dict[str, NodeAction] = {}


def register_node_action(action: NodeAction) -> NodeAction:
    """Offer *action* in the node menus of every editor, replacing one of the same key."""
    _ACTIONS[action.key] = action
    return action


def unregister_node_action(key: str) -> None:
    """Stop offering the action registered under *key*."""
    _ACTIONS.pop(key, None)


def node_actions() -> list[NodeAction]:
    """Return the registered actions, in the order they were registered."""
    return list(_ACTIONS.values())


def _takes_via(hook: Callable[..., t.Any]) -> bool:
    try:
        parameters = inspect.signature(hook).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind is p.VAR_KEYWORD
        or (p.name == "via" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY))
        for p in parameters
    )


def call_provenance_hook(
    instance: t.Any, via: t.Sequence[Mapping[str, t.Any]] = ()
) -> Provenance | None:
    """Return what ``instance.__flowdash_provenance__`` reports, or None if it has no hook or it fails.

    *via* is passed on to a hook that takes it, see :mod:`panel_flowdash.node_actions`.
    """
    hook = getattr(instance, PROVENANCE_HOOK, None)
    if hook is None or not callable(hook):
        return None
    try:
        value = hook(via=tuple(via)) if _takes_via(hook) else hook()
    except Exception:
        logger.exception("The provenance hook of %s failed", type(instance).__name__)
        return None
    if value is None or isinstance(value, Provenance):
        return value
    if isinstance(value, dict) and isinstance(value.get("label"), str):
        return Provenance(
            label=value["label"],
            detail=str(value.get("detail") or ""),
            kind=str(value.get("kind") or ""),
            icon=value.get("icon"),
        )
    logger.warning("The provenance hook of %s returned %r", type(instance).__name__, value)
    return None
