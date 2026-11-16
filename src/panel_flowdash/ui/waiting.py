"""The placeholder a tile shows while a required input has no value yet."""

from __future__ import annotations

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.failure_overlay import icon_badge


def join_labels(labels: list[str]) -> str:
    """Join labels the way a sentence lists them: "A", "A and B", "A, B and C"."""
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


class WaitingPlaceholder(pn.Column):
    """A centred icon, what the tile needs and how to provide it."""

    def __init__(self, **params):
        self.headline = pmui.Typography(
            variant="subtitle1",
            align="center",
            margin=(0, 12, 4, 12),
            sx={"fontWeight": 500, "textAlign": "center"},
        )
        self.hint = pmui.Typography(
            variant="body2",
            align="center",
            margin=(0, 12),
            sx={"color": "text.secondary", "textAlign": "center"},
        )
        params.setdefault("css_classes", ["flowdash-waiting"])
        super().__init__(
            pn.layout.VSpacer(),
            icon_badge("input", color="text.secondary"),
            self.headline,
            self.hint,
            pn.layout.VSpacer(),
            margin=0,
            sizing_mode="stretch_both",
            # Canvas nodes shrink to their content, so a stretching placeholder
            # needs its own minimum size to leave the node any room.
            min_height=96,
            min_width=200,
            **params,
        )

    def show(self, missing: list[str], message: str | None = None):
        """Say that the tile needs the *missing* inputs, led by a custom *message* if given."""
        needs = f"This tile needs {join_labels(missing)}"
        how = (
            "It appears once a connected component provides a value, "
            "or once one is connected in the editor."
        )
        if message:
            self.headline.object = message
            self.hint.object = f"{needs}. {how}"
        else:
            self.headline.object = needs
            self.hint.object = how
