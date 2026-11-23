"""How a node was made: the agent request that created it, and the turns that changed it later."""

from __future__ import annotations

import typing as t
from collections.abc import Callable, Sequence
from datetime import datetime

import panel as pn
import panel_material_ui as pmui
from panel.viewable import Viewable
from panel_agents import RunState, SpanView

from panel_flowdash.node_actions import Provenance
from panel_flowdash.turns import DashboardTurn
from panel_flowdash.ui.text import text_alert, text_typography

NOT_RECORDED = "The model and tool calls of this request are only kept while the session that made it is open."


def _when(at: str | None, fmt: str = "%b %d, %Y at %H:%M") -> str | None:
    if not at:
        return None
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return moment.strftime(fmt)


def recorded(state: RunState | None, run_id: str | None) -> bool:
    """Whether *state* holds the steps of run *run_id*, not just its versions as a stored history does."""
    if state is None or run_id is None or run_id not in state.runs:
        return False
    return bool(state.span(run_id).steps)


def _fallback(turn: DashboardTurn, redacted: bool) -> Viewable:
    objects: list[Viewable] = []
    if turn.question and not redacted:
        objects.append(
            text_typography(turn.question, variant="subtitle1", margin=0, sx={"fontWeight": 500})
        )
    when = _when(turn.at)
    meta = "Made by the assistant" + (f" on {when}" if when else "")
    objects.append(
        pmui.Typography(
            meta, variant="caption", margin=(2, 0, 8, 0), sx={"color": "text.secondary"}
        )
    )
    if turn.summary and not redacted:
        objects.append(text_typography(turn.summary, variant="body2", margin=(0, 0, 8, 0)))
    objects.append(text_alert(NOT_RECORDED, severity="info", margin=0))
    return pn.Column(
        *objects, margin=0, sizing_mode="stretch_width", css_classes=["flowdash-provenance"]
    )


def later_summary(later: Sequence[DashboardTurn], more: bool = False) -> str:
    """Say how many requests and manual edits changed a node after it was made, e.g. "Changed later by 2 requests"."""
    requests = sum(t.actor == "agent" for t in later)
    edits = len(later) - requests
    parts = []
    if requests:
        parts.append(f"{requests} request{'s' * (requests != 1)}")
    if edits:
        parts.append(f"{edits} manual edit{'s' * (edits != 1)}")
    return f"Changed later by {'at least ' if more else ''}{' and '.join(parts)}"


def _later(
    later: Sequence[DashboardTurn],
    on_open: Callable[[DashboardTurn], None] | None,
    openable: Callable[[DashboardTurn], bool],
    more: bool,
) -> Viewable:
    requests = [t for t in later if t.actor == "agent"]
    objects: list[Viewable] = [
        pmui.Typography(
            later_summary(later, more),
            variant="body2",
            margin=0,
            sx={"color": "text.secondary"},
        )
    ]
    for turn in requests:
        if on_open is None or not openable(turn):
            continue
        button = pmui.Button(
            label=f"Request on {_when(turn.at, '%b %d at %H:%M') or 'an earlier date'}",
            icon="manage_search",
            variant="text",
            size="small",
            margin=0,
            sx={"textTransform": "none"},
        )
        button.on_click(lambda _event, turn=turn: on_open(turn))
        objects.append(button)
    return pn.Column(
        *objects,
        margin=(12, 0, 0, 0),
        sizing_mode="stretch_width",
        css_classes=["flowdash-provenance-later"],
    )


def provenance_objects(
    turn: DashboardTurn,
    state: RunState | None,
    *,
    redacted: bool,
    later: Sequence[DashboardTurn] = (),
    on_open: Callable[[DashboardTurn], None] | None = None,
    more: bool = False,
) -> tuple[list[Viewable], SpanView | None]:
    """Show the agent request *turn* records and the turns that changed the node since.

    The request is its span when *state* holds the run's steps, and what the
    turn says otherwise, e.g. after a reload, when a stored history holds the
    run's versions but none of its model or tool calls. *redacted* hides the
    request's content (the question, summary, arguments, results and
    messages) and keeps its shape. *later* lists the turns that changed the
    node after it was made, and *more* says more may have, since a summary
    names at most eight changes; *on_open* is offered for those whose steps
    *state* holds. Returns the objects and the span view among them, to
    release once closed.
    """
    span = None
    if recorded(state, turn.run):
        span = SpanView(state, run_id=turn.run, redacted=redacted)
        objects: list[Viewable] = [span]
    else:
        objects = [_fallback(turn, redacted)]
    if later:
        objects.append(_later(later, on_open, lambda t: recorded(state, t.run), more))
    return objects, span


__all__ = ["later_summary", "provenance_objects", "recorded"]


def provenance_chip(provenance: Provenance, **params: t.Any) -> pmui.Chip:
    """Return a quiet chip with a node's provenance label, e.g. "Metric: capacity", and its detail on hover."""
    icon = provenance.icon or ("functions" if provenance.kind == "metric" else "calculate")
    params.setdefault("margin", 0)
    return pmui.Chip(
        label=provenance.label,
        icon=icon,
        color="default",
        size="small",
        variant="outlined",
        description=provenance.detail or None,
        css_classes=[
            "flowdash-provenance-chip",
            f"flowdash-provenance-{(provenance.kind or 'other').replace(' ', '-')}",
        ],
        **params,
    )


def provenance_line(provenance: Provenance) -> Viewable:
    """Return the provenance of a node's numbers as a chip with its detail, for "How was this made?"."""
    objects: list[Viewable] = [provenance_chip(provenance)]
    if provenance.detail:
        objects.append(
            pmui.Typography(
                provenance.detail,
                variant="body2",
                margin=(4, 0, 0, 0),
                sx={"color": "text.secondary"},
            )
        )
    return pn.Column(
        *objects,
        margin=(0, 0, 12, 0),
        sizing_mode="stretch_width",
        css_classes=["flowdash-provenance-source"],
    )
