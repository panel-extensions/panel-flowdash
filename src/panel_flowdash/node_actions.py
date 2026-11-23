"""Actions a component library adds to the menus of a node, and the provenance a node reports.

A library registers a :class:`NodeAction` with :func:`register_node_action`;
the editor offers it in the ⋮ menu of the sidecar's header and of the
node's tile, for each node it ``applies`` to, and calls ``run`` when it is
picked. An action marked ``admin_only`` is only offered to, and only runs
for, a person the editor's ``authorize_admin`` says administers the project.

A component reports where its numbers come from by defining
``__flowdash_provenance__(self)``, returning a :class:`Provenance` (or a
dict of its fields) or None. A view that reads such a node, directly or
through others, shows the nearest one's label: a chip on its tile while the
dashboard is edited (and in view mode when the editor's
``provenance_labels`` is on) and a line in "How was this made?".
"""

from __future__ import annotations

import logging
import typing as t
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROVENANCE_HOOK = "__flowdash_provenance__"

__all__ = [
    "PROVENANCE_HOOK",
    "NodeAction",
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
    run: t.Callable[[t.Any, str], t.Any]
    """Called with the editor and the node's instance id when the action is picked."""
    applies: t.Callable[[t.Any, str], bool] | None = None
    """Whether the action applies to a node, given the editor and its instance id; None applies to all."""
    icon: str | None = None
    admin_only: bool = False
    """Only offer and run the action for an admin of the project, see ``FlowDash.authorize_admin``."""


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


def call_provenance_hook(instance: t.Any) -> Provenance | None:
    """Return what ``instance.__flowdash_provenance__`` reports, or None if it has no hook or it fails."""
    hook = getattr(instance, PROVENANCE_HOOK, None)
    if hook is None or not callable(hook):
        return None
    try:
        value = hook()
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
