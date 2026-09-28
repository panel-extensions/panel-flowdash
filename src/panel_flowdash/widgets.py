"""Built-in controls for FlowDash dashboards."""

import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.registry import register


@register(
    page=False,
    component=True,
    title="Select",
    config=["label", "default_options"],
    provides=[{"key": "selected"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
)
class Select(Viewer):
    """Select a value from configured or wired options."""

    label = param.String(default="Select")
    default_options = param.List(default=["A", "B", "C"])
    options = param.List(default=None, allow_None=True)
    value = param.Parameter(default=None)

    def __init__(self, **params):
        super().__init__(**params)
        self._widget = pmui.Select(label=self.label, sizing_mode="stretch_width")
        self._widget.link(self, value="value", bidirectional=True)
        self._update_options()

    @param.depends("options", "default_options", "label", watch=True)
    def _update_options(self):
        if not hasattr(self, "_widget"):
            return
        choices = self.default_options if self.options is None else self.options
        self._widget.label = self.label
        self._widget.options = choices
        if self.value not in choices:
            self.value = choices[0] if choices else None

    @param.output(param.Parameter)
    @param.depends("value")
    def selected(self):
        return self.value

    def __panel__(self):
        """Return the selector."""
        return self._widget


@register(
    page=False,
    component=True,
    title="MultiChoice",
    config=["label", "default_options"],
    provides=[{"key": "selected", "type": "List"}],
    requires=[{"key": "options", "type": "List", "multiple": False, "required": False}],
)
class MultiChoice(Viewer):
    """Choose multiple values from configured or wired options."""

    label = param.String(default="MultiChoice")
    default_options = param.List(default=["A", "B", "C"])
    options = param.List(default=None, allow_None=True)
    value = param.List(default=[])

    def __init__(self, **params):
        super().__init__(**params)
        self._widget = pmui.MultiChoice(label=self.label, sizing_mode="stretch_width")
        self._widget.link(self, value="value", bidirectional=True)
        self._update_options()

    @param.depends("options", "default_options", "label", watch=True)
    def _update_options(self):
        if not hasattr(self, "_widget"):
            return
        choices = self.default_options if self.options is None else self.options
        self._widget.label = self.label
        self._widget.options = choices
        if any(value not in choices for value in self.value):
            self.value = [value for value in self.value if value in choices]

    @param.output(param.List)
    @param.depends("value")
    def selected(self):
        return self.value

    def __panel__(self):
        """Return the multi-choice control."""
        return self._widget


@register(
    page=False,
    component=True,
    title="Slider",
    config=["label", "default_start", "default_end", "step"],
    provides=[{"key": "selected", "type": "Number"}],
    requires=[
        {"key": "start", "type": "Number", "required": False},
        {"key": "end", "type": "Number", "required": False},
    ],
)
class Slider(Viewer):
    """Select a numeric value within configured or wired bounds."""

    label = param.String(default="Slider")
    default_start = param.Number(default=0)
    default_end = param.Number(default=100)
    step = param.Number(default=1, bounds=(0, None), inclusive_bounds=(False, True))
    start = param.Number(default=None, allow_None=True)
    end = param.Number(default=None, allow_None=True)
    value = param.Number(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        self._widget = pmui.FloatSlider(
            label=self.label,
            start=min(self.default_start, self.default_end),
            end=max(self.default_start, self.default_end),
            value=self.value,
            step=self.step,
            sizing_mode="stretch_width",
            margin=(10, 20),
        )
        self._widget.link(self, value="value", bidirectional=True)
        self._update_bounds()

    @param.depends("start", "end", "default_start", "default_end", "step", "label", watch=True)
    def _update_bounds(self):
        if not hasattr(self, "_widget"):
            return
        start = self.default_start if self.start is None else self.start
        end = self.default_end if self.end is None else self.end
        self._widget.label = self.label
        self._widget.step = self.step
        if start >= end:
            self._widget.disabled = True
            return
        self._widget.disabled = False
        self._widget.param.update(start=start, end=end)
        self.value = min(max(self.value, start), end)

    @param.output(param.Number)
    @param.depends("value")
    def selected(self):
        return self.value

    def __panel__(self):
        """Return the slider."""
        return self._widget


BUILTIN_COMPONENTS = {
    "Widgets/Select": Select,
    "Widgets/MultiChoice": MultiChoice,
    "Widgets/Slider": Slider,
}
