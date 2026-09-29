"""Built-in controls for FlowDash dashboards."""

import math

import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.registry import register

_COMMON_CONFIG = ["label", "description", "min_widget_width"]
_SLIDER_FORMAT = pmui.FloatSlider.param.format.default
_INT_FORMAT = "0"


class _WidgetComponent(Viewer):
    """Shared config for the built-in widgets, applied live to the wrapped widget."""

    label = param.String(default="")

    description = param.String(default="", doc="Tooltip shown next to the label.")

    # Config names that clash with Viewable params are dropped by the spec builder.
    min_widget_width = param.Integer(
        default=240,
        bounds=(80, 1200),
        label="Minimum width",
        doc="""
        Canvas nodes shrink to their content, which gives a stretching widget no
        room, so this sets how wide the widget is there. Tiles still stretch it.""",
    )

    # Params copied onto the widget unchanged, and ones `_sync_widget` translates.
    _widget_config: tuple[str, ...] = ()
    _converted_config: tuple[str, ...] = ()

    def __init__(self, **params):
        super().__init__(**params)
        self._widget = self._build_widget()
        self._widget.link(self, value="value", bidirectional=True)
        self.param.watch(
            self._sync_widget, [*_COMMON_CONFIG, *self._widget_config, *self._converted_config]
        )
        self._sync_widget()

    def _build_widget(self):
        raise NotImplementedError

    def _sync_widget(self, *events):
        self._widget.param.update(
            label=self.label,
            description=self.description,
            min_width=self.min_widget_width,
            **{name: getattr(self, name) for name in self._widget_config},
        )

    def __panel__(self):
        """Return the wrapped widget."""
        return self._widget


@register(
    page=False,
    component=True,
    title="Select",
    config=[*_COMMON_CONFIG, "default_options", "searchable", "variant", "size"],
    provides=[{"key": "selected"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
)
class Select(_WidgetComponent):
    """Select a value from configured or wired options."""

    label = param.String(default="Select")
    default_options = param.List(default=["A", "B", "C"])
    searchable = param.Boolean(default=False, doc="Filter the options by typing.")
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    options = param.List(default=None, allow_None=True)
    value = param.Parameter(default=None)

    _widget_config = ("searchable", "variant", "size")

    def _build_widget(self):
        return pmui.Select(sizing_mode="stretch_width")

    def __init__(self, **params):
        super().__init__(**params)
        self._update_options()

    @param.depends("options", "default_options", watch=True)
    def _update_options(self):
        if not hasattr(self, "_widget"):
            return
        choices = self.default_options if self.options is None else self.options
        self._widget.options = choices
        if self.value not in choices:
            self.value = choices[0] if choices else None

    @param.output(param.Parameter)
    @param.depends("value")
    def selected(self):
        return self.value


@register(
    page=False,
    component=True,
    title="MultiChoice",
    config=[
        *_COMMON_CONFIG,
        "default_options",
        "placeholder",
        "max_items",
        "searchable",
        "variant",
    ],
    provides=[{"key": "selected", "type": "List"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
)
class MultiChoice(_WidgetComponent):
    """Choose multiple values from configured or wired options."""

    label = param.String(default="MultiChoice")
    default_options = param.List(default=["A", "B", "C"])
    placeholder = param.String(default="")
    # panel-reactflow's schema form cannot render allow_None (anyOf) fields, so 0 means no limit.
    max_items = param.Integer(
        default=0, bounds=(0, None), doc="Maximum selections, 0 for no limit."
    )
    searchable = param.Boolean(default=True, doc="Filter the options by typing.")
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    options = param.List(default=None, allow_None=True)
    value = param.List(default=[])

    _widget_config = ("placeholder", "searchable", "variant")
    _converted_config = ("max_items",)

    def _build_widget(self):
        return pmui.MultiChoice(sizing_mode="stretch_width")

    def _sync_widget(self, *events):
        super()._sync_widget(*events)
        self._widget.max_items = self.max_items or None

    def __init__(self, **params):
        super().__init__(**params)
        self._update_options()

    @param.depends("options", "default_options", watch=True)
    def _update_options(self):
        if not hasattr(self, "_widget"):
            return
        choices = self.default_options if self.options is None else self.options
        self._widget.options = choices
        if any(value not in choices for value in self.value):
            self.value = [value for value in self.value if value in choices]

    @param.output(param.List)
    @param.depends("value")
    def selected(self):
        return self.value


@register(
    page=False,
    component=True,
    title="Slider",
    config=[
        *_COMMON_CONFIG,
        "number_type",
        "default_start",
        "default_end",
        "step",
        "default_value",
        "show_value",
        "format",
        "color",
    ],
    provides=[{"key": "selected", "type": "Number"}],
    requires=[
        {"key": "start", "type": "Number", "required": False},
        {"key": "end", "type": "Number", "required": False},
    ],
)
class Slider(_WidgetComponent):
    """Select a numeric value within configured or wired bounds."""

    label = param.String(default="Slider")
    number_type = param.Selector(
        default="auto",
        objects=["auto", "integer", "float"],
        doc="""
        Whether the slider selects integers or floats. Auto picks integers when
        the bounds, step and default value are all whole numbers.""",
    )
    default_start = param.Number(default=0)
    default_end = param.Number(default=100)
    step = param.Number(default=1, bounds=(0, None), inclusive_bounds=(False, True))
    default_value = param.Number(default=0, doc="Value selected when the slider is placed.")
    show_value = param.Boolean(default=True, doc="Show the current value next to the label.")
    format = param.String(
        default="",
        doc="Numeral.js format of the value, e.g. '0.0' or '0%'. Empty picks one for the number type.",
    )
    color = param.Selector(
        default="primary", objects=["primary", "secondary", "success", "warning", "danger"]
    )
    start = param.Number(default=None, allow_None=True)
    end = param.Number(default=None, allow_None=True)
    value = param.Number(default=0)

    _widget_config = ("show_value", "color")

    def _build_widget(self):
        return pmui.FloatSlider(
            start=min(self.default_start, self.default_end),
            end=max(self.default_start, self.default_end),
            value=self.default_value,
            sizing_mode="stretch_width",
            margin=(10, 20),
        )

    def __init__(self, **params):
        params.setdefault("value", params.get("default_value", 0))
        super().__init__(**params)
        self._update_bounds()

    def _bounds(self) -> tuple[float, float]:
        start = self.default_start if self.start is None else self.start
        end = self.default_end if self.end is None else self.end
        return start, end

    @property
    def is_integer(self) -> bool:
        """Whether the slider currently selects integers."""
        if self.number_type != "auto":
            return self.number_type == "integer"
        numbers = (*self._bounds(), self.step, self.default_value)
        return all(float(n).is_integer() for n in numbers)

    @param.depends("default_value", watch=True)
    def _apply_default_value(self):
        self.value = self.default_value
        self._update_bounds()

    @param.depends(
        "start", "end", "default_start", "default_end", "step", "number_type", "format", watch=True
    )
    def _update_bounds(self):
        if not hasattr(self, "_widget"):
            return
        start, end = self._bounds()
        step = self.step
        # A FloatSlider rather than swapping in an IntSlider keeps the node view stable.
        if self.is_integer:
            start, end, step = math.ceil(start), math.floor(end), max(1, round(step))
        default_format = _INT_FORMAT if self.is_integer else _SLIDER_FORMAT
        self._widget.param.update(step=step, format=self.format or default_format)
        if start >= end:
            self._widget.disabled = True
            return
        self._widget.disabled = False
        self._widget.param.update(start=start, end=end)
        value = min(max(self.value, start), end)
        self.value = round(value) if self.is_integer else value

    @param.output(param.Number)
    @param.depends(
        "value",
        "number_type",
        "start",
        "end",
        "default_start",
        "default_end",
        "step",
        "default_value",
    )
    def selected(self):
        return round(self.value) if self.is_integer else float(self.value)


BUILTIN_COMPONENTS = {
    "Widgets/Select": Select,
    "Widgets/MultiChoice": MultiChoice,
    "Widgets/Slider": Slider,
}
