"""How a node was made: the agent request that created it, shown in the side sheet."""

from __future__ import annotations

from datetime import datetime

import panel as pn
import panel_material_ui as pmui
from panel.viewable import Viewable
from panel_agents import RunState, SpanView

from panel_flowdash.turns import DashboardTurn
from panel_flowdash.ui.text import text_alert, text_typography

NOT_RECORDED = "The model and tool calls of this request are only kept while the session that made it is open."


def _when(at: str | None) -> str | None:
    if not at:
        return None
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return moment.strftime("%b %d, %Y at %H:%M")


def provenance_objects(
    turn: DashboardTurn, state: RunState | None, *, redacted: bool
) -> tuple[list[Viewable], SpanView | None]:
    """Show the agent request *turn* records: its span when *state* holds the run, else what the turn says.

    *redacted* hides the request's content (the question, arguments, results
    and messages) and keeps its shape, for people who may not edit the dashboard.
    Returns the objects and the span view among them, to release once closed.
    """
    if state is not None and turn.run is not None and turn.run in state.runs:
        span = SpanView(state, run_id=turn.run, redacted=redacted)
        return [span], span
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
    if turn.summary:
        objects.append(text_typography(turn.summary, variant="body2", margin=(0, 0, 8, 0)))
    objects.append(text_alert(NOT_RECORDED, severity="info", margin=0))
    column = pn.Column(
        *objects, margin=0, sizing_mode="stretch_width", css_classes=["flowdash-provenance"]
    )
    return [column], None


__all__ = ["provenance_objects"]
