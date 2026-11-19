"""Config widgets for a number's format and its colored thresholds.

A text param holding a Python format spec asks for :class:`FormatInput`, and a
list of ``[limit, color]`` pairs for :class:`ThresholdsInput`, through their
FlowDash metadata::

    format = param.String(metadata={"flowdash": {"widget": "format"}})
    thresholds = param.List(metadata={"flowdash": {"widget": "thresholds"}})

Both store a format spec string and a list of pairs, as a text box and a list
literal did, so existing dashboards load unchanged.
"""

from __future__ import annotations

import math
import re
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
    "Percent": ",.0%",
    "Percent, one decimal": ",.1%",
    "Scientific": ".2e",
}
_CUSTOM = "custom"

#: The theme colors a threshold offers, by label; "danger" is an alias of "error".
THEME_COLORS = {
    "Primary": "primary",
    "Secondary": "secondary",
    "Success": "success",
    "Warning": "warning",
    "Error": "error",
    "Danger": "danger",
    "Info": "info",
}
_CUSTOM_LABEL = "Custom"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# The colors "Add threshold" cycles through.
_NEXT_COLORS = ("error", "warning", "success", "info", "primary", "secondary")
# What the KPI accepts as a color besides a theme name, as widgets._css_color does.
_CSS_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,24}|(rgb|hsl)a?\([0-9\s.,%/+-]{1,40}\))$")

_LABEL_SX = {"color": "text.secondary"}


def _sample(spec: str) -> float | int:
    if spec.endswith("%"):
        return _FRACTION
    # Integer presentation types only format whole numbers.
    return round(_EXAMPLE) if spec[-1:] in ("d", "n", "b", "o", "x", "X", "c") else _EXAMPLE


def format_example(spec: str) -> str:
    """Show how *spec* formats a sample number, the one the KPI shows a value like."""
    sample = _sample(spec)
    if not spec:
        return f"{sample:,.2f}"
    return format(sample, spec)


def format_problem(spec: t.Any) -> str | None:
    """Return why *spec* is no format for a number, in words, or None if it is one."""
    if not isinstance(spec, str):
        return "A number format is text, e.g. ',.2f' or '.1%'."
    try:
        format(_sample(spec), spec)
    except (TypeError, ValueError):
        return f"{spec!r} is not a number format; try e.g. ',.2f', '.1%' or ',d'."
    return None


def color_problem(color: t.Any) -> str | None:
    """Return why *color* is no threshold color, or None if the KPI can show it."""
    if isinstance(color, str):
        name = color.strip()
        if name.lower() in THEME_COLORS.values() or _CSS_COLOR.match(name):
            return None
    return (
        f"{color!r} is not a color; use a theme color (primary, secondary, success, "
        "warning, error, info) or a CSS color like 'red' or '#22aa66'."
    )


def thresholds_problems(thresholds: t.Any) -> list[str]:
    """Return what is wrong with a list of ``[limit, color]`` pairs, empty if nothing is."""
    if not isinstance(thresholds, (list, tuple)):
        return ["Thresholds are a list of [limit, color] pairs."]
    problems = []
    for index, pair in enumerate(thresholds, start=1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            problems.append(f"Threshold {index} is not a [limit, color] pair: {pair!r}.")
            continue
        limit, color = pair
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not math.isfinite(limit)
        ):
            problems.append(f"Threshold {index} has no number as its limit: {limit!r}.")
        if (problem := color_problem(color)) is not None:
            problems.append(f"Threshold {index}: {problem}")
    return problems


def _number(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _next_limit(limits: list[float]) -> float | int:
    if len(limits) >= 2:
        return _number(limits[-1] + (limits[-1] - limits[-2]))
    if limits:
        return _number(limits[0] * 2 if limits[0] > 0 else limits[0] + 10)
    return 100


def _label(text: str, description: str | None) -> pn.Row:
    tooltip = pn.widgets.TooltipIcon(
        value=description or "", margin=(0, 0, 0, 4), visible=bool(description)
    )
    return pn.Row(
        pmui.Typography(text, variant="caption", sx=_LABEL_SX, margin=(0, 0, 2, 0)),
        tooltip,
        margin=0,
    )


class FormatInput(CompositeWidget):
    """A dropdown of number formats by name, with a field for a custom format spec."""

    value = param.String(default="", allow_None=True)

    description = param.String(
        default=None,
        allow_None=True,
        doc="""
        The field's doc, shown as a tooltip beside its name.""",
    )

    _composite_type: t.ClassVar[type] = pmui.Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        options = {
            f"{label} ({format_example(spec)})": spec for label, spec in FORMAT_PRESETS.items()
        }
        self._preset = pmui.Select(
            label=self.label or "",
            description=self.description,
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
        self.param.watch(lambda event: setattr(self._preset, "label", event.new or ""), "label")
        self.param.watch(
            lambda event: setattr(self._preset, "description", event.new), "description"
        )
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
        # Spaces are part of a spec: " .2f" pads positive numbers.
        spec = event.new
        if format_problem(spec):
            self._describe(spec)
            return
        self.value = spec
        self._describe(spec)


class ThresholdsInput(CompositeWidget):
    """Rows of a limit and a color: a number takes the color of the lowest limit it reaches.

    Pairs it cannot read, e.g. ``[2]``, and colors it does not offer, e.g.
    ``"rgb(0, 0, 0)"``, are kept as they are: the rows show what they can and
    an edit changes only its own pair.
    """

    value = param.List(default=[], allow_None=True)

    description = param.String(
        default=None,
        allow_None=True,
        doc="""
        The field's doc, shown as a tooltip beside its name.""",
    )

    _composite_type: t.ClassVar[type] = pmui.Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self._label = _label(self.label or "", self.description)
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
            "The number takes the color of the lowest limit it does not exceed.",
            variant="caption",
            sx=_LABEL_SX,
            margin=(2, 0, 0, 0),
        )
        self._add.on_click(self._append)
        self.param.watch(self._on_value, "value")
        self.param.watch(lambda event: setattr(self._label[0], "object", event.new or ""), "label")
        self._composite.margin = (5, 10)
        self._render()

    def _entries(self) -> list:
        return list(self.value or [])

    @staticmethod
    def _readable(pair: t.Any) -> bool:
        return (
            isinstance(pair, (list, tuple))
            and len(pair) == 2
            and isinstance(pair[0], (int, float))
            and not isinstance(pair[0], bool)
        )

    def _on_value(self, _event):
        if not self._syncing:
            self._render()

    def _render(self):
        # The rows sit in the widget's own column rather than a nested one, which
        # the inspector's tabs would not measure as it grows.
        entries = self._entries()
        self._rows = [
            self._row(index, pair[0], pair[1])
            for index, pair in enumerate(entries)
            if self._readable(pair)
        ]
        self._hint.visible = bool(self._rows)
        cells = [cell for row in self._rows for cell in row]
        self._composite[:] = [self._label, *cells, self._add, self._hint]

    def _set(self, index: int, pair: list):
        entries = self._entries()
        if index >= len(entries):
            return
        entries[index] = pair
        self._syncing = True
        try:
            self.value = entries
        finally:
            self._syncing = False

    def _row(self, index: int, limit: t.Any, color: t.Any) -> tuple[pn.Row, pmui.ColorPicker]:
        number = pmui.FloatInput(
            label="Up to",
            value=float(limit),
            width=120,
            margin=(0, 8, 0, 0),
            css_classes=["fd-threshold-limit"],
        )
        options = dict(THEME_COLORS)
        if color not in options.values():
            # A color the dropdown does not offer stays as it is until changed.
            options = {str(color): color, **options}
        options[_CUSTOM_LABEL] = _CUSTOM
        select = pmui.Select(
            label="Color",
            options=options,
            value=color,
            sizing_mode="stretch_width",
            margin=(0, 8, 0, 0),
            css_classes=["fd-threshold-color"],
        )
        picker = pmui.ColorPicker(
            label="Custom color",
            value=color if isinstance(color, str) and _HEX.match(color) else "#888888",
            sizing_mode="stretch_width",
            margin=(0, 36, 8, 128),
            visible=False,
            css_classes=["fd-threshold-picker"],
        )
        remove = pmui.IconButton(
            icon="close",
            description="Remove this threshold",
            size="small",
            margin=0,
            align="center",
            css_classes=["fd-threshold-remove"],
        )
        current = {"color": color}

        def limit_changed(event):
            if event.new is not None:
                self._set(index, [_number(event.new), current["color"]])

        def color_picked(event):
            # A custom color is stored once one is picked, not when the picker opens.
            picker.visible = event.new == _CUSTOM
            if event.new != _CUSTOM:
                current["color"] = event.new
                self._set(index, [_number(number.value), event.new])

        def custom_picked(event):
            current["color"] = event.new
            self._set(index, [_number(number.value), event.new])

        def removed(_event):
            entries = self._entries()
            if index < len(entries):
                del entries[index]
                self.value = entries

        number.param.watch(limit_changed, "value")
        select.param.watch(color_picked, "value")
        picker.param.watch(custom_picked, "value")
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
        entries = self._entries()
        pairs = [pair for pair in entries if self._readable(pair)]
        limits = sorted(pair[0] for pair in pairs)
        color = _NEXT_COLORS[len(pairs) % len(_NEXT_COLORS)]
        self.value = [*entries, [_next_limit(limits), color]]
