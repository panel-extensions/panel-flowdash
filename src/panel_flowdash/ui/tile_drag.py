"""Dragging components between the tile grid and the areas beside it, the top bar and the sidebar.

The grid's tiles are dragged by TileGrid (Muuri), which knows nothing of other
drop targets, so a page-level script watches those drags and the handles of the
components beside the grid, highlights where a component can go while it is
dragged, and reports the drop. The editor then moves the component with
:func:`panel_flowdash.ui.tile_layout.move_to_area`, so a drag is one edit.
"""

from __future__ import annotations

import typing as t

import param
from panel.custom import JSComponent

if t.TYPE_CHECKING:
    from collections.abc import Callable

REFUSAL = "Charts and tables stay on the grid"


class TileDrag(JSComponent):
    """Watches drags of tiles and of the components beside the grid, and reports drops.

    ``tiles`` and ``controls`` map the keys the dragged elements carry on the
    page to ``{"id": instance id, "fits": bool}``, ``fits`` saying whether the
    component may leave the grid. A drop calls ``on_move(instance_id, area,
    before)``, *before* being the instance id the component goes in front of
    or None for last; a drop refused because the component does not fit
    calls ``on_refuse(instance_id, area)``.
    """

    enabled = param.Boolean(default=False, doc="Whether drags are watched.")

    grid = param.String(default="", doc="The CSS class of the editor's tile grid.")

    uid = param.String(
        default="", doc="The suffix of the CSS classes of the areas beside the grid."
    )

    areas = param.List(default=[], doc="The areas beside the grid a component can be dropped in.")

    tiles = param.Dict(default={}, doc="The tiles on the grid, by the key class they carry.")

    controls = param.Dict(default={}, doc="The components beside the grid, by their handle's key.")

    labels = param.Dict(
        default={"top": "Top bar", "sidebar": "Sidebar", "grid": "Grid"},
        doc="The names drop targets are labelled with.",
    )

    refusal = param.String(default=REFUSAL, doc="The hint on targets a component may not go to.")

    _esm = "tile_drag.js"

    def __init__(
        self,
        on_move: Callable[[str, str, str | None], None] | None = None,
        on_refuse: Callable[[str, str], None] | None = None,
        **params,
    ):
        params.setdefault("height", 0)
        params.setdefault("width", 0)
        params.setdefault("margin", 0)
        super().__init__(**params)
        self._on_move = on_move
        self._on_refuse = on_refuse

    def _handle_msg(self, msg):
        if not isinstance(msg, dict) or not self.enabled:
            return
        instance_id, area = msg.get("id"), msg.get("area")
        if not isinstance(instance_id, str) or area not in ("grid", *self.areas):
            return
        if msg.get("action") == "move" and self._on_move is not None:
            before = msg.get("before")
            self._on_move(instance_id, area, before if isinstance(before, str) else None)
        elif msg.get("action") == "refuse" and self._on_refuse is not None:
            self._on_refuse(instance_id, area)
