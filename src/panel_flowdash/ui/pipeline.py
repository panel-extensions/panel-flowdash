"""A strip summarising the dataflow as source -> transform -> views, with a status dot per stage."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

import panel as pn
import panel_material_ui as pmui
from panel.viewable import Viewer

from panel_flowdash.ui.health import FAILED, OK, WAITING, worst

_DOT_COLORS = {OK: "success.main", WAITING: "warning.main", FAILED: "error.main"}

_STAGE_SX = {
    "display": "inline-flex",
    "alignItems": "center",
    "gap": "6px",
    "height": "28px",
    "px": "10px",
    "border": 1,
    "borderColor": "divider",
    "borderRadius": "8px",
    "whiteSpace": "nowrap",
    "color": "text.primary",
}

# Groups of more nodes than this at one depth collapse into a count.
_MAX_NAMED = 2


@dataclass(frozen=True)
class Stage:
    """One chip of the strip: a label and the worst status of its nodes."""

    label: str
    status: str
    nodes: tuple[str, ...]


def pipeline_stages(
    edges: t.Iterable[dict],
    titles: t.Mapping[str, str],
    status: t.Callable[[str], str],
) -> list[Stage]:
    """Order the wired nodes into stages from the sources to the views.

    Nodes without outgoing edges are the views and are collapsed into one
    "N views" stage at the end. The others are grouped by their longest
    distance from a source, so a chain of transforms keeps its order. A group
    of more than two nodes is collapsed into a count too.
    """
    downstream: dict[str, set[str]] = {}
    nodes: list[str] = []
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source == target:
            continue
        for node in (source, target):
            if node not in nodes:
                nodes.append(node)
        downstream.setdefault(source, set()).add(target)
    if not nodes:
        return []

    upstream: dict[str, set[str]] = {node: set() for node in nodes}
    for source, targets in downstream.items():
        for target in targets:
            upstream[target].add(source)
    depth: dict[str, int] = {}
    pending = [node for node in nodes if not upstream[node]]
    for node in pending:
        depth[node] = 0
    # Longest-path depths in topological order; nodes on a cycle keep depth 0.
    remaining = {node: len(upstream[node]) for node in nodes}
    while pending:
        node = pending.pop(0)
        for target in sorted(downstream.get(node, ()), key=nodes.index):
            depth[target] = max(depth.get(target, 0), depth[node] + 1)
            remaining[target] -= 1
            if remaining[target] == 0:
                pending.append(target)
    views = [node for node in nodes if not downstream.get(node)]
    steps: dict[int, list[str]] = {}
    for node in nodes:
        if node not in views:
            steps.setdefault(depth.get(node, 0), []).append(node)

    stages = []
    for level in sorted(steps):
        group = steps[level]
        if len(group) > _MAX_NAMED:
            noun = "sources" if level == 0 else "steps"
            stages.append(Stage(f"{len(group)} {noun}", worst(map(status, group)), tuple(group)))
        else:
            stages.extend(Stage(titles.get(node, node), status(node), (node,)) for node in group)
    if views:
        label = titles.get(views[0], views[0]) if len(views) == 1 else f"{len(views)} views"
        stages.append(Stage(label, worst(map(status, views)), tuple(views)))
    return stages


def _stage_chip(stage: Stage) -> pmui.Typography:
    return pmui.Typography(
        stage.label,
        variant="caption",
        margin=0,
        sx={
            **_STAGE_SX,
            "&::before": {
                "content": '""',
                "width": "8px",
                "height": "8px",
                "borderRadius": "50%",
                "flexShrink": 0,
                "bgcolor": _DOT_COLORS[stage.status],
            },
        },
        css_classes=[f"fd-stage-{stage.status}"],
    )


def _arrow() -> pmui.Typography:
    return pmui.Typography(
        "→", variant="body2", margin=0, sx={"color": "text.secondary", "px": "2px"}
    )


class PipelineStrip(Viewer):
    """The "Pipeline" strip in the controls bar, hidden while nothing is wired."""

    def __init__(self, **params):
        super().__init__(**params)
        self.stages: list[Stage] = []
        self._label = pmui.Typography(
            "Pipeline",
            variant="caption",
            margin=0,
            sx={"color": "text.secondary", "display": "flex", "alignItems": "center", "mr": "4px"},
        )
        self._strip = pn.FlexBox(
            align_items="center",
            flex_wrap="wrap",
            margin=0,
            sizing_mode=None,
            visible=False,
            gap="6px",
            styles={"margin-left": "auto", "max-width": "100%"},
            css_classes=["fd-pipeline"],
        )

    def update(self, stages: list[Stage]):
        """Show *stages*, or hide the strip when there are none."""
        if stages == self.stages:
            return
        self.stages = stages
        items: list = [self._label]
        for i, stage in enumerate(stages):
            if i:
                items.append(_arrow())
            items.append(_stage_chip(stage))
        with pn.io.hold():
            self._strip[:] = items if stages else []
            self._strip.visible = bool(stages)

    @property
    def visible(self) -> bool:
        """Whether the strip has stages to show."""
        return bool(self.stages)

    def __panel__(self):
        """Render the strip."""
        return self._strip
