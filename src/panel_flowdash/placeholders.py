"""Stand-ins for the saved items and edges a dashboard cannot restore.

A dashboard may reference a component that is no longer installed, one that
fails to build, or an edge whose ports no longer exist or no longer fit. Rather
than dropping them, the editor keeps the saved data untouched, so it is written
back unchanged on the next save, and shows a placeholder in their place.
"""

from __future__ import annotations

from dataclasses import dataclass

import panel as pn
import panel_material_ui as pmui
from panel.viewable import Viewable

from panel_flowdash.dashboard_store import DashboardEdge
from panel_flowdash.ui.text import escape_markdown, markdown_pane, text_alert

# Node type of placeholder nodes on the canvas. Component node types are derived
# from component ids, which never contain a colon.
PLACEHOLDER_NODE_TYPE = "flowdash:placeholder"


@dataclass(frozen=True)
class Placeholder:
    """Why a saved item is shown as a placeholder, and what the placeholder says.

    Parameters
    ----------
    reason
        A short machine-readable cause, e.g. ``"missing"`` (the component is not
        offered) or ``"failed"`` (it raised while being placed).
    title
        Label of the placeholder node and heading of its tile.
    message
        Text shown on the node and the tile.
    private
        Whether the saved item must not be described at all. Its id, config and
        port names are then kept out of the page; edges touching it are only
        counted, never listed.
    """

    reason: str
    title: str
    message: str
    private: bool = False


@dataclass(frozen=True)
class UnresolvedEdge:
    """A saved edge that could not be wired, kept to be saved unchanged."""

    edge: DashboardEdge
    reason: str


def placeholder_view(placeholder: Placeholder) -> Viewable:
    """Build what a placeholder node and tile display."""
    return text_alert(
        placeholder.message,
        title=placeholder.title,
        severity="warning",
        sizing_mode="stretch_width",
        max_width=360,
        margin=0,
    )


def unresolved_summary(items: int, edges: int) -> str | None:
    """Phrase how many saved items and edges were kept unresolved, or None if none were."""
    parts = []
    if items:
        parts.append(f"{items} component{'s' if items != 1 else ''}")
    if edges:
        parts.append(f"{edges} connection{'s' if edges != 1 else ''}")
    if not parts:
        return None
    return (
        f"{' and '.join(parts)} could not be restored. They are kept and saved "
        "unchanged; remove them to drop them from the dashboard."
    )


def broken_edges_panel(lines: list[str], hidden: int, on_discard) -> Viewable:
    """Build the listing of saved edges that could not be wired, with a discard action."""
    count = len(lines) + hidden
    text = "\n".join(f"- {escape_markdown(line)}" for line in lines)
    if hidden:
        text += f"\n- {hidden} connection{'s' if hidden != 1 else ''} to unavailable components"
    button = pmui.Button(
        label="Discard", variant="text", size="small", margin=0, description="Discard them"
    )
    button.on_click(lambda _event: on_discard())
    return pn.Column(
        pmui.Alert(
            markdown_pane(text, margin=0),
            title=(
                f"{count} saved connection{'s' if count != 1 else ''} could not be "
                "restored and will be saved unchanged"
            ),
            severity="warning",
            margin=0,
        ),
        button,
        margin=(0, 8),
        max_width=640,
    )
