"""The role a component plays in a dataflow: data source, transform, view or widget."""

from __future__ import annotations

from collections.abc import Sequence

from panel_flowdash.registry import RegistryEntry
from panel_flowdash.widgets import BUILTIN_COMPONENTS

# "other" holds components whose ports are unknown until they are imported.
ROLES = ("source", "transform", "view", "widget", "other")

ROLE_ICONS = {
    "source": "storage",
    "transform": "filter_alt",
    "view": "insert_chart",
    "widget": "tune",
    "other": "widgets",
}

# Tags that decide a component's role, whatever its ports say.
ROLE_TAGS = {
    "source": "source",
    "data source": "source",
    "transform": "transform",
    "view": "view",
    "widget": "widget",
}


def is_builtin(entry: RegistryEntry) -> bool:
    """Whether *entry* is one of the widgets FlowDash ships with."""
    return entry.app is not None and BUILTIN_COMPONENTS.get(entry.app_id) is entry.app


def component_role(
    entry: RegistryEntry,
    inputs: Sequence,
    outputs: Sequence,
    *,
    ports_known: bool = True,
) -> str:
    """Return the role of a component, one of :data:`ROLES`.

    A ``source``, ``transform``, ``view`` or ``widget`` tag decides it.
    Otherwise built-in widgets are widgets, a component without outputs is a
    view, one with outputs but no inputs a data source, and the rest are
    transforms. A component whose ports are not known yet is ``other``.
    """
    for tag in entry.metadata.tags:
        role = ROLE_TAGS.get(str(tag).strip().lower())
        if role is not None:
            return role
    if is_builtin(entry):
        return "widget"
    if not ports_known:
        return "other"
    if not outputs:
        return "view"
    if not inputs:
        return "source"
    return "transform"
