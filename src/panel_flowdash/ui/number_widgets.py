"""Config widgets for a number's format and its colored thresholds.

A text param holding a Python format spec asks for :class:`FormatInput`, and a
list of ``[limit, color]`` pairs for :class:`ThresholdsInput`, through their
FlowDash metadata::

    format = param.String(metadata={"flowdash": {"widget": "format"}})
    thresholds = param.List(metadata={"flowdash": {"widget": "thresholds"}})

Both store what they always stored, a format spec string and a list of pairs,
so dashboards and the agent see no change.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.widgets.base import CompositeWidget

FORMAT_WIDGET = "format"
THRESHOLDS_WIDGET = "thresholds"

_EXAMPLE = 1234.5678
_FRACTION = 0.1234

#: The formats offered by name, with the spec each stands for.
FORMAT_PRESETS = {
    "Automatic": "",
    "Whole number": ",.0f",
    "One decimal": ",.1f",
    "Two decimals": ",.2f",
    "Percent": ".0%",
    "Percent, one decimal": ".1%",
    "Scientific": ".2e",
}
_CUSTOM = "custom"

#: The theme colors a threshold offers, by label.
THEME_COLORS = {
    "Primary": "primary",
    "Secondary": "secondary",
    "Success": "success",
    "Warning": "warning",
    "Error": "error",
    "Info": "info",
}
_CUSTOM_COLOR = "Custom color"

_LABEL_SX = {"color": "text.secondary"}


def widget_hint(parameter: param.Parameter) -> str | None:
    """Return the ``widget`` a param's FlowDash metadata asks for, or None."""
    flowdash = (parameter.metadata or {}).get("flowdash")
    hint = flowdash.get("widget") if isinstance(flowdash, dict) else None
    return hint if isinstance(hint, str) else None


def format_example(spec: str) -> str:
    """Show how *spec* formats a sample number, the one the KPI shows a value like."""
    sample = _FRACTION if spec.endswith("%") else _EXAMPLE
    if not spec:
        return f"{sample:,.2f}"
    return format(sample, spec)


def format_problem(spec: str) -> str | None:
    """Return why *spec* is no format spec for a number, or None if it is one."""
    try:
        format(_EXAMPLE, spec)
    except (TypeError, ValueError) as exc:
        return f"Not a number format: {exc}."
    return None


def _label(name: str) -> pmui.Typography:
    return pmui.Typography(name, variant="caption", sx=_LABEL_SX, margin=(0, 0, 2, 0))


class FormatInput(CompositeWidget):
    """A dropdown of number formats by name, with a field for a custom format spec."""

    value = param.String(default="", allow_None=True)

    _composite_type: t.ClassVar[type] = pmui.Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        options = {
            f"{label} ({format_example(spec)})": spec for label, spec in FORMAT_PRESETS.items()
        }
        self._preset = pmui.Select(
            label=self.name,
            options={**options, "Custom": _CUSTOM},
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["fd-format-preset"],
        )
        self._custom = pmui.TextInput(
            label="Format spec",
            placeholder="e.g. ,.3f",
            sizing_mode="stretch_width",
            margin=(8, 0, 0, 0),
            css_classes=["fd-format-custom"],
        )
        self._note = pmui.Typography("", variant="caption", margin=(2, 0, 0, 0))
        self._preset.param.watch(self._picked, "value")
        self._custom.param.watch(self._typed, "value")
        self.param.watch(lambda _event: self._show(), "value")
        self.param.watch(lambda event: setattr(self._preset, "label", event.new), "name")
        self._composite[:] = [self._preset, self._custom, self._note]
        # Lined up with the right edge of the text fields of the form.
        self._composite.margin = (5, 22, 5, 10)
        self._show()

    def _show(self):
        spec = self.value or ""
        self._syncing = True
        try:
            preset = spec in FORMAT_PRESETS.values() and self._preset.value != _CUSTOM
            self._preset.value = spec if preset else _CUSTOM
            self._custom.visible = not preset
            if not preset and self._custom.value != spec:
                self._custom.value = spec
        finally:
            self._syncing = False
        self._describe(spec)

    def _describe(self, spec: str):
        problem = format_problem(spec)
        if problem:
            self._note.param.update(
                object=problem, styles={"color": "var(--mui-palette-error-main)"}
            )
        else:
            self._note.param.update(object=f"Shows like {format_example(spec)}", styles={})
        self._note.visible = self._custom.visible

    def _picked(self, event):
        if self._syncing:
            return
        if event.new == _CUSTOM:
            self._custom.visible = True
            self._syncing = True
            self._custom.value = self.value or ""
            self._syncing = False
            self._describe(self.value or "")
            return
        self.value = event.new

    def _typed(self, event):
        if self._syncing:
            return
        spec = event.new.strip()
        if format_problem(spec):
            self._describe(spec)
            return
        self.value = spec
        self._describe(spec)


class ThresholdsInput(CompositeWidget):
    """Rows of a limit and a color: a number up to the limit takes the color."""

    value = param.List(default=[], allow_None=True)

    _composite_type: t.ClassVar[type] = pmui.Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self._label = _label(self.name)
        self._rows: list = []
        self._add = pmui.Button(
            label="Add threshold",
            icon="add",
            variant="text",
            size="small",
            margin=(2, 0, 0, 0),
            css_classes=["fd-thresholds-add"],
        )
        self._hint = pmui.Typography(
            "The number takes the color of the first row whose limit it does not exceed.",
            variant="caption",
            sx=_LABEL_SX,
            margin=(2, 0, 0, 0),
        )
        self._add.on_click(self._append)
        self.param.watch(self._on_value, "value")
        self.param.watch(lambda event: setattr(self._label, "object", event.new), "name")
        self._composite.margin = (5, 10)
        self._render()

    def _pairs(self) -> list[list]:
        return [
            list(pair)
            for pair in self.value or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]

    def _on_value(self, _event):
        if not self._syncing:
            self._render()

    def _render(self):
        # The rows sit in the widget's own column rather than a nested one, which
        # the inspector's tabs would not measure as it grows.
        pairs = self._pairs()
        self._rows = [self._row(i, limit, color) for i, (limit, color) in enumerate(pairs)]
        self._hint.visible = bool(pairs)
        cells = [cell for row in self._rows for cell in row]
        self._composite[:] = [self._label, *cells, self._add, self._hint]

    def _set(self, pairs: list[list]):
        self._syncing = True
        try:
            self.value = pairs
        finally:
            self._syncing = False

    def _row(self, index: int, limit: t.Any, color: t.Any) -> tuple[pn.Row, pmui.ColorPicker]:
        number = pmui.FloatInput(
            label="Up to",
            value=float(limit) if isinstance(limit, (int, float)) else None,
            width=130,
            margin=(0, 8, 0, 0),
            css_classes=["fd-threshold-limit"],
        )
        theme = isinstance(color, str) and color.lower() in THEME_COLORS.values()
        options = {**THEME_COLORS, _CUSTOM_COLOR: _CUSTOM}
        picker = pmui.ColorPicker(
            label="Custom color",
            value=color
            if isinstance(color, str) and not theme and color.startswith("#")
            else "#888888",
            sizing_mode="stretch_width",
            margin=(0, 36, 8, 0),
            visible=not theme,
            css_classes=["fd-threshold-picker"],
        )
        select = pmui.Select(
            label="Color",
            options=options,
            value=color.lower() if theme else _CUSTOM,
            sizing_mode="stretch_width",
            margin=(0, 8, 0, 0),
            css_classes=["fd-threshold-color"],
        )
        remove = pmui.IconButton(
            icon="close",
            description="Remove this threshold",
            size="small",
            margin=0,
            align="center",
            css_classes=["fd-threshold-remove"],
        )

        def changed(*_events):
            pairs = self._pairs()
            if index >= len(pairs):
                return
            picker.visible = select.value == _CUSTOM
            shade = picker.value if select.value == _CUSTOM else select.value
            if number.value is None:
                return
            pairs[index] = [number.value, shade]
            self._set(pairs)

        def removed(_event):
            pairs = self._pairs()
            if index < len(pairs):
                del pairs[index]
                self.value = pairs

        number.param.watch(changed, "value")
        select.param.watch(changed, "value")
        picker.param.watch(changed, "value")
        remove.on_click(removed)
        return (
            pn.Row(
                number,
                select,
                remove,
                margin=(0, 0, 8, 0),
                sizing_mode="stretch_width",
                css_classes=["fd-threshold-row"],
            ),
            picker,
        )

    def _append(self, _event):
        pairs = self._pairs()
        limits = [p[0] for p in pairs if isinstance(p[0], (int, float))]
        colors = list(THEME_COLORS.values())
        used = {p[1] for p in pairs}
        color = next((c for c in ("error", "warning", "success") if c not in used), colors[0])
        self.value = [*pairs, [max(limits) * 2 if limits and max(limits) > 0 else 100, color]]
