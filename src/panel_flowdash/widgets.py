"""Built-in controls and views for FlowDash dashboards."""

import datetime as dt
import html
import math
import re
import weakref

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.registry import register
from panel_flowdash.text_components import TEXT_COMPONENTS

_COMMON_CONFIG = ["label", "description", "min_widget_width"]
_SLIDER_FORMAT = pmui.FloatSlider.param.format.default
_INT_FORMAT = "0"
_COLORS = ["primary", "secondary", "success", "warning", "danger"]
_ISO_DATE = r"^(\d{4}-\d{2}-\d{2})?$"
# Tiles as tall as the widgets measure in a dashboard, so that a dashboard of
# controls lines up and several fit side by side.
_FIELD_SIZE = {"width": 360, "height": 90}
_SLIDER_SIZE = {"width": 360, "height": 100}
_TOGGLE_SIZE = {"width": 360, "height": 72}


class _ISODate(param.String):
    """A date as a YYYY-MM-DD string, or empty, that must name a real day."""

    def __init__(self, default="", **params):
        super().__init__(default=default, regex=_ISO_DATE, **params)

    def _validate_value(self, val, allow_None):
        super()._validate_value(val, allow_None)
        if val:
            dt.date.fromisoformat(val)

    def schema(self, safe=False, subset=None, mode="json"):
        return self._serializers[mode].param_schema("String", self, safe=safe, subset=subset)


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
    # Params that choose the widget class, so changing one swaps in a new widget.
    _rebuild_config: tuple[str, ...] = ()
    # The param mirroring the widget's value. A widget without inputs keeps it
    # private, since every public param of a Viewer without `requires` is an input.
    _value_param = "value"

    def __init__(self, **params):
        super().__init__(**params)
        self._widget = self._build_widget()
        self._link_widget()
        self.param.watch(self._push_value, self._value_param)
        self.param.watch(
            self._sync_widget, [*_COMMON_CONFIG, *self._widget_config, *self._converted_config]
        )
        self._view = None
        if self._rebuild_config:
            # The view is a stable container, so a swapped widget replaces its
            # content instead of the tile's view.
            self._view = pn.Column(self._widget, margin=0, sizing_mode="stretch_width")
            self.param.watch(self._rebuild_widget, list(self._rebuild_config))
        self._sync_widget()

    def _build_widget(self):
        raise NotImplementedError

    def _link_widget(self):
        self._widget_watcher = self._widget.param.watch(self._pull_value, "value")

    def _pull_value(self, event):
        setattr(self, self._value_param, event.new)

    def _push_value(self, event):
        self._widget.value = event.new

    def _rebuild_widget(self, *events):
        self._widget.param.unwatch(self._widget_watcher)
        self._widget = self._build_widget()
        self._widget.value = getattr(self, self._value_param)
        self._link_widget()
        self._sync_widget()
        self._view.objects = [self._widget]

    def _sync_widget(self, *events):
        self._widget.param.update(
            label=self.label,
            description=self.description,
            min_width=self.min_widget_width,
            **{name: getattr(self, name) for name in self._widget_config},
        )

    def __panel__(self):
        """Return the wrapped widget, or the container that holds it."""
        return self._widget if self._view is None else self._view


class _OptionsWidget(_WidgetComponent):
    """A widget choosing from configured options, or from options wired to its input."""

    default_options = param.List(
        default=["A", "B", "C"], doc="Options offered while no options are wired."
    )
    options = param.List(default=None, allow_None=True)

    def __init__(self, **params):
        super().__init__(**params)
        self._update_options()

    def _rebuild_widget(self, *events):
        super()._rebuild_widget(*events)
        self._update_options()

    @param.depends("options", "default_options", watch=True)
    def _update_options(self):
        if not hasattr(self, "_widget"):
            return
        choices = self.default_options if self.options is None else self.options
        self._widget.options = choices
        self._restrict_value(choices)

    def _restrict_value(self, choices):
        if self.value not in choices:
            self.value = choices[0] if choices else None


@register(
    page=False,
    component=True,
    title="Select",
    icon="list",
    description=(
        "Dropdown that picks one value from a list of options. The options come from the "
        "default_options config, or from a List wired to the options input."
    ),
    config=[*_COMMON_CONFIG, "default_options", "searchable", "variant", "size"],
    provides=[{"key": "selected"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
    default_size=_FIELD_SIZE,
)
class Select(_OptionsWidget):
    """Select a value from configured or wired options."""

    label = param.String(default="Select")
    searchable = param.Boolean(default=False, doc="Filter the options by typing.")
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    value = param.Parameter(default=None)

    _widget_config = ("searchable", "variant", "size")

    def _build_widget(self):
        return pmui.Select(sizing_mode="stretch_width")

    @param.output(param.Parameter)
    @param.depends("value")
    def selected(self):
        return self.value


@register(
    page=False,
    component=True,
    title="MultiChoice",
    icon="checklist",
    description=(
        "Multi-select dropdown, filtered by typing, that picks any number of values from a list of options, "
        "configured or wired to the options input. Outputs the selected values as a List."
    ),
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
    default_size=_FIELD_SIZE,
)
class MultiChoice(_OptionsWidget):
    """Choose multiple values from configured or wired options."""

    label = param.String(default="MultiChoice")
    placeholder = param.String(default="", doc="Text shown while nothing is selected.")
    # panel-reactflow's schema form cannot render allow_None (anyOf) fields, so 0 means no limit.
    max_items = param.Integer(
        default=0, bounds=(0, None), doc="Maximum selections, 0 for no limit."
    )
    searchable = param.Boolean(default=True, doc="Filter the options by typing.")
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    value = param.List(default=[])

    _widget_config = ("placeholder", "searchable", "variant")
    _converted_config = ("max_items",)

    def _build_widget(self):
        return pmui.MultiChoice(sizing_mode="stretch_width")

    def _sync_widget(self, *events):
        super()._sync_widget(*events)
        self._widget.max_items = self.max_items or None

    def _restrict_value(self, choices):
        if any(value not in choices for value in self.value):
            self.value = [value for value in self.value if value in choices]

    @param.output(param.List)
    @param.depends("value")
    def selected(self):
        return self.value


@register(
    page=False,
    component=True,
    title="Radio Group",
    icon="radio_button_checked",
    description=(
        "Picks one value from a short list of options, all visible at once as radio buttons "
        "or as a row of toggle buttons. The options come from default_options or from a "
        "List wired to the options input. Prefer Select for long lists."
    ),
    config=[
        *_COMMON_CONFIG,
        "default_options",
        "style",
        "orientation",
        "color",
        "size",
    ],
    provides=[{"key": "selected"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
    default_size=_SLIDER_SIZE,
)
class RadioGroup(_OptionsWidget):
    """Pick one of a few options shown side by side."""

    label = param.String(default="Radio Group")
    style = param.Selector(
        default="radio",
        objects=["radio", "buttons"],
        doc="Radio buttons with labels, or a joined row of toggle buttons.",
    )
    orientation = param.Selector(
        default="horizontal",
        objects=["horizontal", "vertical"],
        doc="Lay the options out in a row or a column.",
    )
    color = param.Selector(default="primary", objects=_COLORS)
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    value = param.Parameter(default=None)

    _widget_config = ("color", "size")
    _converted_config = ("orientation",)
    _rebuild_config = ("style",)

    def _build_widget(self):
        if self.style == "buttons":
            return pmui.RadioButtonGroup(sizing_mode="stretch_width")
        return pmui.RadioBoxGroup(sizing_mode="stretch_width")

    def _sync_widget(self, *events):
        super()._sync_widget(*events)
        if isinstance(self._widget, pmui.RadioButtonGroup):
            self._widget.orientation = self.orientation
        else:
            self._widget.inline = self.orientation == "horizontal"

    @param.output(param.Parameter)
    @param.depends("value")
    def selected(self):
        return self.value


class _NumberWidget(_WidgetComponent):
    """Shared handling of whole and fractional numbers for the numeric widgets."""

    number_type = param.Selector(
        default="auto",
        objects=["auto", "integer", "float"],
        doc="""
        Whether the widget selects integers or floats. Auto picks integers when
        the bounds, step and default value are all whole numbers.""",
    )
    step = param.Number(
        default=1,
        bounds=(0, None),
        inclusive_bounds=(False, True),
        doc="Increment between selectable values, e.g. 1 or 0.5.",
    )
    format = param.String(
        default="",
        doc="Numeral.js format of the value, e.g. '0.0' or '0%'. Empty picks one for the number type.",
    )
    start = param.Number(default=None, allow_None=True)
    end = param.Number(default=None, allow_None=True)

    def _whole_numbers(self) -> tuple:
        return (self.step,)

    @property
    def is_integer(self) -> bool:
        """Whether the widget currently selects integers."""
        if self.number_type != "auto":
            return self.number_type == "integer"
        return all(float(n).is_integer() for n in self._whole_numbers() if n is not None)

    def _number(self, value):
        if value is None:
            return None
        return round(value) if self.is_integer else float(value)


class _BoundedSlider(_NumberWidget):
    """A slider over configured bounds, which wired start and end inputs override."""

    default_start = param.Number(default=0, doc="Lower bound while no start is wired.")
    default_end = param.Number(default=100, doc="Upper bound while no end is wired.")
    show_value = param.Boolean(default=True, doc="Show the current value next to the label.")
    color = param.Selector(
        default="primary", objects=_COLORS, doc="Theme color of the track and handles."
    )

    _widget_config = ("show_value", "color")

    def _bounds(self) -> tuple[float, float]:
        start = self.default_start if self.start is None else self.start
        end = self.default_end if self.end is None else self.end
        return start, end

    def _whole_numbers(self) -> tuple:
        return (*self._bounds(), self.step)

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
        previous = (self._widget.start, self._widget.end)
        self._widget.param.update(disabled=False, start=start, end=end)
        self._clamp_value(start, end, previous)

    def _clamp_value(self, start, end, previous):
        raise NotImplementedError


@register(
    page=False,
    component=True,
    title="Slider",
    icon="linear_scale",
    description=(
        "Slider that picks one number between a start and an end, from config or from "
        "Numbers wired to the start and end inputs. Outputs an integer when the bounds, step "
        "and default value are whole numbers, otherwise a float."
    ),
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
    default_size=_SLIDER_SIZE,
)
class Slider(_BoundedSlider):
    """Select a numeric value within configured or wired bounds."""

    label = param.String(default="Slider")
    default_value = param.Number(default=0, doc="Value selected when the slider is placed.")
    value = param.Number(default=0)

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

    def _whole_numbers(self) -> tuple:
        return (*super()._whole_numbers(), self.default_value)

    @param.depends("default_value", watch=True)
    def _apply_default_value(self):
        self.value = self.default_value
        self._update_bounds()

    def _clamp_value(self, start, end, previous):
        self.value = self._number(min(max(self.value, start), end))

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
        return self._number(self.value)


@register(
    page=False,
    component=True,
    title="Range Slider",
    icon="tune",
    description=(
        "Slider with two handles that picks a (low, high) range of numbers between a start "
        "and an end, from config or from Numbers wired to the start and end inputs. Starts "
        "with the whole range selected and keeps it whole when the bounds change. Outputs "
        "integers when the bounds and step are whole numbers, otherwise floats."
    ),
    config=[
        *_COMMON_CONFIG,
        "number_type",
        "default_start",
        "default_end",
        "step",
        "show_value",
        "format",
        "color",
    ],
    provides=[{"key": "selected", "type": "Range"}],
    requires=[
        {"key": "start", "type": "Number", "required": False},
        {"key": "end", "type": "Number", "required": False},
    ],
    default_size=_SLIDER_SIZE,
)
class RangeSlider(_BoundedSlider):
    """Select a numeric range within configured or wired bounds."""

    label = param.String(default="Range Slider")
    value = param.Range(default=(0, 100))

    def _build_widget(self):
        return pmui.RangeSlider(
            start=0, end=100, value=(0, 100), sizing_mode="stretch_width", margin=(10, 20)
        )

    def __init__(self, **params):
        super().__init__(**params)
        self._update_bounds()

    def _clamp_value(self, start, end, previous):
        low, high = self.value
        if (low, high) == tuple(previous):
            low, high = start, end
        else:
            low, high = (min(max(v, start), end) for v in (low, high))
        self.value = (self._number(low), self._number(high))

    @param.output(param.Range)
    @param.depends("value", "number_type", "start", "end", "default_start", "default_end", "step")
    def selected(self):
        low, high = self.value
        return (self._number(low), self._number(high))


@register(
    page=False,
    component=True,
    title="Number Input",
    icon="pin",
    description=(
        "Field for typing a number, with arrow buttons that step it up and down. Optional "
        "start and end inputs bound the value. Outputs an integer when the step and default "
        "value are whole numbers, otherwise a float, and None while the field is empty."
    ),
    config=[
        *_COMMON_CONFIG,
        "number_type",
        "default_value",
        "step",
        "format",
        "variant",
        "size",
    ],
    provides=[{"key": "selected", "type": "Number"}],
    requires=[
        {"key": "start", "type": "Number", "required": False},
        {"key": "end", "type": "Number", "required": False},
    ],
    default_size=_FIELD_SIZE,
)
class NumberInput(_NumberWidget):
    """Type a number, optionally within wired bounds."""

    label = param.String(default="Number Input")
    default_value = param.Number(default=0, doc="Value shown when the input is placed.")
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    value = param.Number(default=0, allow_None=True)

    _widget_config = ("variant", "size")

    def _build_widget(self):
        return pmui.FloatInput(value=self.default_value, step=1, sizing_mode="stretch_width")

    def __init__(self, **params):
        params.setdefault("value", params.get("default_value", 0))
        super().__init__(**params)
        self._update_input()

    def _whole_numbers(self) -> tuple:
        return (self.step, self.default_value)

    def _pull_value(self, event):
        self.value = self._number(event.new)
        # Rounding to an integer may leave the value unchanged, which would not
        # update the field, so the rounded number is written back explicitly.
        if self._widget.value != self.value:
            self._widget.value = self.value

    @param.depends("default_value", watch=True)
    def _apply_default_value(self):
        self.value = self.default_value
        self._update_input()

    @param.depends("start", "end", "step", "number_type", "format", watch=True)
    def _update_input(self):
        if not hasattr(self, "_widget"):
            return
        integer = self.is_integer
        step = max(1, round(self.step)) if integer else self.step
        self._widget.param.update(
            start=self.start,
            end=self.end,
            step=step,
            format=self.format or ("0,0" if integer else "0,0.0[000]"),
        )
        if self.value is None:
            return
        value = self.value
        if self.start is not None:
            value = max(value, self.start)
        if self.end is not None:
            value = min(value, self.end)
        self.value = self._number(value)

    @param.output(param.Number)
    @param.depends("value", "number_type", "step", "default_value")
    def selected(self):
        return self._number(self.value)


@register(
    page=False,
    component=True,
    title="Toggle",
    icon="toggle_on",
    description=(
        "Boolean control, drawn as a switch or a checkbox, that outputs True or False. Use it "
        "to enable or disable an option of a downstream component."
    ),
    config=[*_COMMON_CONFIG, "default_value", "style", "color", "size"],
    provides=[{"key": "checked", "type": "Boolean"}],
    default_size=_TOGGLE_SIZE,
)
class Toggle(_WidgetComponent):
    """Turn an option on or off."""

    label = param.String(default="Toggle")
    default_value = param.Boolean(default=False, doc="Whether it is on when placed.")
    style = param.Selector(default="switch", objects=["switch", "checkbox"])
    color = param.Selector(default="primary", objects=_COLORS)
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    _value = param.Boolean(default=False)

    _widget_config = ("color", "size")
    _rebuild_config = ("style",)
    _value_param = "_value"

    def __init__(self, **params):
        params.setdefault("_value", params.get("default_value", False))
        super().__init__(**params)

    def _build_widget(self):
        widget = pmui.Checkbox if self.style == "checkbox" else pmui.Switch
        return widget(value=self._value, sizing_mode="stretch_width")

    @param.depends("default_value", watch=True)
    def _apply_default_value(self):
        self._value = self.default_value

    @param.output(param.Boolean)
    @param.depends("_value")
    def checked(self):
        return self._value


@register(
    page=False,
    component=True,
    title="Text Input",
    icon="search",
    description=(
        "Single-line text field, e.g. a query or a free-text filter, that outputs the entered text as a "
        "string. By default the text is sent when Enter is pressed or the field loses "
        "focus; set update_on to 'typing' to send it on every keystroke."
    ),
    config=[
        *_COMMON_CONFIG,
        "placeholder",
        "default_value",
        "update_on",
        "variant",
        "size",
    ],
    provides=[{"key": "text", "type": "String"}],
    default_size=_FIELD_SIZE,
)
class TextInput(_WidgetComponent):
    """Enter a line of text."""

    label = param.String(default="Text Input")
    placeholder = param.String(default="", doc="Hint shown while the field is empty.")
    default_value = param.String(default="", doc="Text in the field when it is placed.")
    update_on = param.Selector(
        default="enter",
        objects=["enter", "typing"],
        doc="""
        When the text is sent downstream: on Enter or when the field loses
        focus, or on every keystroke, which suits cheap filters.""",
    )
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    _value = param.String(default="")

    _widget_config = ("placeholder", "variant", "size")
    _value_param = "_value"

    def __init__(self, **params):
        params.setdefault("_value", params.get("default_value", ""))
        super().__init__(**params)

    def _build_widget(self):
        return pmui.TextInput(value=self._value, sizing_mode="stretch_width")

    def _link_widget(self):
        super()._link_widget()
        self._widget.param.watch(self._on_typing, "value_input")

    def _push_value(self, event):
        # Text typed into the field is already there, so it is not sent back to it.
        if event.new != self._widget.value_input:
            super()._push_value(event)

    def _on_typing(self, event):
        if self.update_on == "typing":
            self._value = event.new or ""

    @param.depends("default_value", watch=True)
    def _apply_default_value(self):
        self._value = self.default_value

    @param.output(param.String)
    @param.depends("_value")
    def text(self):
        return self._value


_PERIODS = {
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
    "last_365_days": 365,
}


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _as_date(value) -> dt.date | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    elif type(value).__name__ == "datetime64":
        value = value.astype("datetime64[D]").item()
    elif isinstance(value, str):
        value = _parse_date(value[:10])
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


@register(
    page=False,
    component=True,
    title="Date Range",
    icon="date_range",
    description=(
        "Calendar picker for a (start, end) range of dates, output as a tuple of two "
        "datetime.date values. It starts on a period relative to today, e.g. the last 30 "
        "days, the whole span between its bounds, or fixed dates. Dates in config are ISO "
        "strings (YYYY-MM-DD); Dates wired to the start and end inputs override the bounds."
    ),
    config=[
        *_COMMON_CONFIG,
        "default_period",
        "custom_start",
        "custom_end",
        "default_start",
        "default_end",
        "variant",
        "size",
    ],
    provides=[{"key": "selected", "type": "DateRange"}],
    requires=[
        {"key": "start", "type": "Date", "required": False},
        {"key": "end", "type": "Date", "required": False},
    ],
    # Narrower than this the picker cuts off the end date.
    default_size={"width": 400, "height": 90},
    min_size={"width": 320},
)
class DateRange(_WidgetComponent):
    """Pick a range of dates."""

    label = param.String(default="Date Range")
    default_period = param.Selector(
        default="last_30_days",
        objects=[*_PERIODS, "month_to_date", "year_to_date", "all", "custom"],
        label="Default period",
        doc="""
        Range selected when the dashboard opens. Relative periods end today, 'all'
        spans the bounds (no selection without bounds), and 'custom' uses
        custom_start and custom_end. A period outside the bounds selects
        all of them, or just the nearest bound when only one is set.""",
    )
    custom_start = _ISODate(doc="First date of the 'custom' period, as YYYY-MM-DD.")
    custom_end = _ISODate(doc="Last date of the 'custom' period, as YYYY-MM-DD.")
    default_start = _ISODate(
        label="Earliest date",
        doc="Earliest selectable date as YYYY-MM-DD while no start is wired; empty for none.",
    )
    default_end = _ISODate(
        label="Latest date",
        doc="Latest selectable date as YYYY-MM-DD while no end is wired; empty for none.",
    )
    variant = param.Selector(default="outlined", objects=["outlined", "filled", "standard"])
    size = param.Selector(default="medium", objects=["small", "medium", "large"])
    start = param.Date(default=None, allow_None=True)
    end = param.Date(default=None, allow_None=True)
    value = param.DateRange(default=None, allow_None=True)

    _widget_config = ("variant", "size")

    def _build_widget(self):
        return pmui.DateRangePicker(sizing_mode="stretch_width")

    def __init__(self, **params):
        super().__init__(**params)
        self._apply_period()

    def _bounds(self) -> tuple[dt.date | None, dt.date | None]:
        start = _parse_date(self.default_start) if self.start is None else _as_date(self.start)
        end = _parse_date(self.default_end) if self.end is None else _as_date(self.end)
        return start, end

    def _period(self, start, end) -> tuple[dt.date, dt.date] | None:
        today = dt.date.today()
        period = self.default_period
        if period in _PERIODS:
            return today - dt.timedelta(days=_PERIODS[period] - 1), today
        if period == "month_to_date":
            return today.replace(day=1), today
        if period == "year_to_date":
            return today.replace(month=1, day=1), today
        if period == "custom":
            low, high = _parse_date(self.custom_start), _parse_date(self.custom_end)
            if low is not None and high is not None:
                return min(low, high), max(low, high)
            return None
        if start is not None and end is not None:
            return start, end
        return None

    @param.depends(
        "default_period", "custom_start", "custom_end", "default_start", "default_end", watch=True
    )
    def _apply_period(self):
        if hasattr(self, "_widget"):
            self._update_range(self._period(*self._apply_bounds()))

    @param.depends("start", "end", watch=True)
    def _follow_bounds(self):
        # New bounds from upstream keep the user's selection where they still allow it.
        if hasattr(self, "_widget"):
            start, end = self._apply_bounds()
            self._update_range(self.value or self._period(start, end))

    def _apply_bounds(self) -> tuple[dt.date | None, dt.date | None]:
        start, end = self._bounds()
        if start is not None and end is not None and start > end:
            start, end = end, start
        self._widget.param.update(start=start, end=end)
        return start, end

    def _update_range(self, value):
        start, end = self._widget.start, self._widget.end
        if value is not None:
            low, high = (_as_date(v) for v in value)
            if start is not None and high < start:
                # The period lies before the bounds, so select all of them.
                low, high = start, end if end is not None else start
            elif end is not None and low > end:
                low, high = start if start is not None else end, end
            else:
                low = low if start is None else max(low, start)
                high = high if end is None else min(high, end)
            value = (low, high)
        self.value = value

    @param.output(param.DateRange)
    @param.depends("value")
    def selected(self):
        if self.value is None:
            return None
        low, high = self.value
        return (_as_date(low), _as_date(high))


_THEME_COLORS = {
    "primary": ("primary", "#1976d2"),
    "secondary": ("secondary", "#9c27b0"),
    "success": ("success", "#2e9d4a"),
    "warning": ("warning", "#ed6c02"),
    "error": ("error", "#d84a3a"),
    "danger": ("error", "#d84a3a"),
    "info": ("info", "#0288d1"),
}
_CSS_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,24}|(rgb|hsl)a?\([0-9\s.,%/+-]{1,40}\))$")


def _css_color(color) -> str | None:
    """Return a CSS color for a theme color name or a plain CSS color, or None if unsafe."""
    if not isinstance(color, str):
        return None
    color = color.strip()
    if color.lower() in _THEME_COLORS:
        name, fallback = _THEME_COLORS[color.lower()]
        return f"var(--mui-palette-{name}-main, {fallback})"
    return color if _CSS_COLOR.match(color) else None


def _format_number(value: float, spec: str) -> str:
    if spec:
        try:
            return format(value, spec)
        except (TypeError, ValueError):
            pass
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


# Material UI defines its palette variables only where its theme is applied,
# so each has a fallback that reads well on light and dark backgrounds.
_KPI_CSS = """
:host {
  container-type: inline-size;
  --fd-kpi-muted: var(--mui-palette-text-secondary, color-mix(in srgb, currentColor 65%, transparent));
}
.fd-kpi {
  box-sizing: border-box;
  height: 100%;
  padding: 8px 14px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  min-width: 0;
  font-family: var(--mui-font-family, inherit);
  color: var(--mui-palette-text-primary, inherit);
}
.fd-kpi-label {
  font-size: 0.75rem;
  font-weight: 500;
  letter-spacing: 0.08em;
  line-height: 1.6;
  text-transform: uppercase;
  color: var(--fd-kpi-muted);
}
.fd-kpi-label, .fd-kpi-value, .fd-kpi-delta {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-kpi-value {
  font-size: clamp(1.5rem, 13cqi, 3rem);
  font-weight: 500;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.fd-kpi-empty {
  color: var(--fd-kpi-muted);
}
.fd-kpi-delta {
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--fd-kpi-muted);
}
.fd-kpi-change {
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.fd-kpi-good {
  color: var(--mui-palette-success-main, #2e9d4a);
}
.fd-kpi-bad {
  color: var(--mui-palette-error-main, #d84a3a);
}
"""


@register(
    page=False,
    component=True,
    title="KPI",
    icon="speed",
    description=(
        "Card showing one headline number under a short label. Wire a Number to the value "
        "input; wire a second Number to reference to show the change from it, e.g. last "
        "period's value. Thresholds color the number, e.g. red below a target."
    ),
    tags=["view"],
    config=[
        "label",
        "format",
        "prefix",
        "suffix",
        "delta",
        "increase_is_good",
        "caption",
        "thresholds",
    ],
    requires=[
        {"key": "value", "type": "Number", "required": False},
        {"key": "reference", "type": "Number", "required": False},
    ],
    default_size={"width": 280, "height": 130},
    min_size={"width": 160, "height": 90},
)
class KPI(Viewer):
    """A headline number, with its change from a reference value."""

    label = param.String(default="KPI", doc="Heading shown above the number.")
    format = param.String(
        default="",
        doc="""
        Python format spec of the number, e.g. ',.0f', '.1%' or '.3s'. Empty
        shows whole numbers with thousands separators and others with two decimals.""",
    )
    prefix = param.String(default="", doc="Text before the number, e.g. '$'.")
    suffix = param.String(default="", doc="Text after the number, e.g. ' kWh'.")
    delta = param.Selector(
        default="percent",
        objects=["percent", "absolute", "none"],
        doc="How the change from a wired reference is shown.",
    )
    increase_is_good = param.Boolean(
        default=True,
        label="Increase is good",
        doc="Color a rise green and a fall red; off reverses the colors, e.g. for costs.",
    )
    caption = param.String(default="", doc="Muted text after the change, e.g. 'vs last month'.")
    thresholds = param.List(
        default=[],
        doc="""
        Ascending [limit, color] pairs that color the number: it takes the color
        of the first pair whose limit it does not exceed, e.g. [[50, "error"],
        [80, "warning"], [100, "success"]]. Colors are theme names (primary,
        secondary, success, warning, error, info) or CSS colors.""",
    )
    value = param.Number(default=None, allow_None=True)
    reference = param.Number(default=None, allow_None=True)

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.HTML(
            self._html(), stylesheets=[_KPI_CSS], sizing_mode="stretch_both", margin=0
        )

    def _color(self, value) -> str | None:
        thresholds = []
        for pair in self.thresholds:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    thresholds.append((float(pair[0]), pair[1]))
                except (TypeError, ValueError):
                    continue
        for limit, color in sorted(thresholds, key=lambda pair: pair[0]):
            if value <= limit:
                return _css_color(color)
        return None

    def _delta_html(self, value) -> str:
        reference = self.reference
        if self.delta == "none" or reference is None or not math.isfinite(reference):
            return ""
        change = value - reference
        if self.delta == "percent":
            if reference == 0:
                return ""
            amount = f"{abs(change) / abs(reference):.1%}"
        else:
            amount = _format_number(abs(change), self.format)
        arrow = "▲ " if change > 0 else "▼ " if change < 0 else ""
        tone = ""
        if change:
            good = (change > 0) == self.increase_is_good
            tone = " fd-kpi-good" if good else " fd-kpi-bad"
        return f'<span class="fd-kpi-change{tone}">{arrow}{html.escape(amount)}</span>'

    def _html(self) -> str:
        label = f'<div class="fd-kpi-label">{html.escape(self.label)}</div>' if self.label else ""
        value = self.value
        if value is None or not math.isfinite(value):
            number = '<div class="fd-kpi-value fd-kpi-empty">&ndash;</div>'
            delta = ""
        else:
            text = self.prefix + _format_number(value, self.format) + self.suffix
            color = self._color(value)
            style = f' style="color: {html.escape(color)}"' if color else ""
            number = f'<div class="fd-kpi-value"{style}>{html.escape(text)}</div>'
            delta = self._delta_html(value)
        caption = html.escape(self.caption)
        if delta or caption:
            delta = f'<div class="fd-kpi-delta">{delta}{" " if delta and caption else ""}{caption}</div>'
        return f'<div class="fd-kpi">{label}{number}{delta}</div>'

    @param.depends(
        "label",
        "format",
        "prefix",
        "suffix",
        "delta",
        "increase_is_good",
        "caption",
        "thresholds",
        "value",
        "reference",
        watch=True,
    )
    def _update(self):
        self._pane.object = self._html()

    def __panel__(self):
        """Return the card."""
        return self._pane


_SAFE_URL = re.compile(
    r"^(https?://|/|\./|\.\./|data:image/(png|jpe?g|gif|webp|svg\+xml|avif);base64,)"
    r"|^[A-Za-z0-9_][A-Za-z0-9_./%-]*([?#][^\s\"'<>]*)?$",
    re.IGNORECASE,
)

_IMAGE_CSS = """
:host {
  min-height: 60px;
}
.fd-image {
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.fd-image img {
  display: block;
  width: 100%;
  height: 100%;
}
.fd-image-empty {
  font-family: var(--mui-font-family, inherit);
  font-size: 0.875rem;
  color: var(--mui-palette-text-secondary, color-mix(in srgb, currentColor 65%, transparent));
}
"""


def safe_image_url(url: str) -> str | None:
    """Return *url* when it is an http(s), relative or base64 image data URL, else None."""
    url = (url or "").strip()
    return url if url and _SAFE_URL.match(url) else None


@register(
    page=False,
    component=True,
    title="Image",
    icon="image",
    description=(
        "Shows an image, e.g. a logo or a diagram, from the url config or from a String URL "
        "wired to the url input. Takes http(s) URLs, paths relative to the server and "
        "base64 image data URLs."
    ),
    tags=["view"],
    config=["default_url", "alt", "fit"],
    requires=[{"key": "url", "type": "String", "required": False}],
    default_size={"width": 360, "height": 240},
)
class Image(Viewer):
    """An image from a URL."""

    default_url = param.String(
        default="", label="URL", doc="Image URL shown while no url is wired."
    )
    alt = param.String(default="", label="Alt text", doc="Text describing the image.")
    fit = param.Selector(
        default="contain",
        objects=["contain", "cover", "fill", "scale-down", "none"],
        doc="""
        How the image fills its tile: 'contain' shows all of it, 'cover' fills
        the tile and crops, 'fill' stretches it.""",
    )
    url = param.String(default=None, allow_None=True)

    _editing = param.Boolean(default=True)

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.HTML(
            self._html(), stylesheets=[_IMAGE_CSS], sizing_mode="stretch_both", margin=0
        )
        self._follow_editor()

    def _follow_editor(self):
        from panel_flowdash.editor import current_dashboard

        editor = current_dashboard()
        if editor is None:
            return
        self._editing = editor.editable
        # Weak, so that the editor's watcher does not keep a removed image alive.
        image = weakref.ref(self)

        def follow(event):
            if (target := image()) is None:
                editor.param.unwatch(watcher)
            else:
                target._editing = event.new

        watcher = editor.param.watch(follow, "editable")

    def _html(self) -> str:
        url = safe_image_url(self.default_url if self.url is None else self.url)
        if url is None:
            # The placeholder only helps while the dashboard is being edited.
            empty = '<span class="fd-image-empty">No image</span>' if self._editing else ""
            return f'<div class="fd-image">{empty}</div>'
        return (
            f'<div class="fd-image"><img src="{html.escape(url, quote=True)}" '
            f'alt="{html.escape(self.alt, quote=True)}" style="object-fit: {self.fit}"></div>'
        )

    @param.depends("default_url", "alt", "fit", "url", "_editing", watch=True)
    def _update(self):
        self._pane.object = self._html()

    def __panel__(self):
        """Return the image."""
        return self._pane


BUILTIN_COMPONENTS = {
    "Widgets/Select": Select,
    "Widgets/MultiChoice": MultiChoice,
    "Widgets/RadioGroup": RadioGroup,
    "Widgets/Slider": Slider,
    "Widgets/RangeSlider": RangeSlider,
    "Widgets/NumberInput": NumberInput,
    "Widgets/DateRange": DateRange,
    "Widgets/Toggle": Toggle,
    "Widgets/TextInput": TextInput,
    "Widgets/KPI": KPI,
    "Widgets/Image": Image,
    **TEXT_COMPONENTS,
}
