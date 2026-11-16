"""A tile overlay that explains, over the stale content, why a tile could not update."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import panel as pn
import panel_material_ui as pmui

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

    from panel_flowdash.dataflow_engine import InputFailure, InputFailures

# The scrim fades stale content without taking part in layout, so a tile is
# sized by its content as it is without failure handling. It is derived from the
# theme to read in both light and dark mode, and lets the stale content show
# through so it is clear the message is about what is behind it.
_SCRIM_STYLES = {
    "position": "absolute",
    "inset": "0",
    "z-index": "10",
    "background": (
        "color-mix(in srgb, var(--mui-palette-background-paper, "
        "var(--panel-background-color, #fff)) 82%, transparent)"
    ),
    "overflow": "auto",
}

_ICON_SX = {
    "alignItems": "center",
    "bgcolor": "color-mix(in srgb, currentColor 14%, transparent)",
    "borderRadius": "50%",
    "color": "error.main",
    "display": "flex",
    "height": 40,
    "justifyContent": "center",
    "width": 40,
}

_SIZING_PARAMS = (
    "sizing_mode",
    "width",
    "height",
    "min_width",
    "min_height",
    "max_width",
    "max_height",
)


@dataclass(frozen=True)
class FailureMessage:
    """What a failure overlay says about the failed inputs of one tile.

    ``sources`` names the upstream components whose failure stopped the tile,
    in the words the user sees; ``details`` holds the technical explanation
    shown behind the Details disclosure.
    """

    sources: list[str] = field(default_factory=list)
    details: str = ""


def _sentence(sources: list[str]) -> str:
    if not sources:
        return "An upstream step failed, so this shows its last result."
    names = [f"**{name}**" for name in sources]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{joined} stopped the data, so this shows its last result."


def icon_badge(icon: str, color: str = "error.main") -> pmui.Typography:
    """Return a Material icon in a tinted circle, to lead a centred state message."""
    return pmui.Typography(
        f'<span class="material-icons" aria-hidden="true">{icon}</span>',
        align="center",
        margin=(0, 0, 8, 0),
        sx={**_ICON_SX, "color": color},
    )


class FailureOverlay(pn.Column):
    """A view with a message that covers it while any of its inputs is failed.

    ``objects`` stays ``[view, overlay]``; the overlay is a sibling that is
    only visible while the failures are non-empty. ``actions`` is a row of
    extra buttons shown next to the Details toggle, e.g. a way to the editor.
    """

    def __init__(
        self,
        view: Viewable,
        failures: InputFailures,
        describe: Callable[[dict[str, InputFailure]], FailureMessage | str],
        **params,
    ):
        message = failures.param.failed.rx.pipe(
            lambda failed: _as_message(describe(failed)) if failed else FailureMessage()
        )
        self.summary = pmui.Typography(
            message.rx.pipe(lambda m: _sentence(m.sources)),
            variant="body2",
            align="center",
            margin=(0, 16),
            max_width=560,
            sx={"color": "text.secondary", "textAlign": "center"},
        )
        self.details = pmui.Typography(
            message.rx.pipe(lambda m: m.details),
            css_classes=["flowdash-failure-details"],
            variant="caption",
            align="center",
            margin=(4, 16, 0, 16),
            max_width=560,
            visible=False,
            sx={
                "color": "text.secondary",
                "fontFamily": "monospace",
                "overflowWrap": "anywhere",
                "textAlign": "left",
                "whiteSpace": "pre-line",
            },
        )
        self.details_toggle = pmui.Button(
            label="Details",
            icon="expand_more",
            size="small",
            variant="text",
            margin=(0, 4),
        )
        self.details_toggle.on_click(self._toggle_details)
        self.actions = pn.Row(margin=0, align="center")
        overlay = pn.Column(
            pn.layout.VSpacer(),
            icon_badge("error_outline"),
            pmui.Typography(
                "This tile could not update",
                variant="subtitle1",
                align="center",
                margin=(0, 16, 4, 16),
                sx={"fontWeight": 500, "textAlign": "center"},
            ),
            self.summary,
            pn.Row(self.actions, self.details_toggle, align="center", margin=(8, 0, 0, 0)),
            self.details,
            pn.layout.VSpacer(),
            css_classes=["flowdash-failure"],
            margin=0,
            sizing_mode="stretch_both",
            styles=_SCRIM_STYLES,
            visible=failures.param.failed.rx.len() > 0,
        )
        sizing = {name: getattr(view, name) for name in _SIZING_PARAMS}
        super().__init__(
            view, overlay, margin=0, styles={"position": "relative"}, **{**sizing, **params}
        )

    def _toggle_details(self, _event=None):
        shown = not self.details.visible
        self.details.visible = shown
        self.details_toggle.param.update(
            label="Hide details" if shown else "Details",
            icon="expand_less" if shown else "expand_more",
        )


def _as_message(described: FailureMessage | str) -> FailureMessage:
    if isinstance(described, FailureMessage):
        return described
    return FailureMessage(details=described)


def with_failure_overlay(
    view: Viewable,
    failures: InputFailures,
    describe: Callable[[dict[str, InputFailure]], FailureMessage | str],
) -> FailureOverlay:
    """Wrap *view* so that it is covered by a message while any of its inputs is failed.

    The wrapper takes over the view's sizing so that it lays out the same way in
    a tile or on the canvas. The message says in plain language that the tile
    could not update and which upstream components stopped it; the technical
    explanation from *describe* sits behind a Details disclosure.
    """
    return FailureOverlay(view, failures, describe)
