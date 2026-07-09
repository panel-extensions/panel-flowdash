"""A counter component that provides a numeric value."""

import panel_material_ui as pmui
import param

from panel_flowdash import register


class CounterConfig(param.Parameterized):
    """Design-time configuration for the counter input."""

    label = param.String(default="Count", doc="Label shown on the input.")
    start = param.Integer(default=0, doc="Minimum value.")
    end = param.Integer(default=1000, doc="Maximum value.")
    step = param.Integer(default=1, bounds=(1, 100), doc="Increment step.")


@register(
    page=False,
    component=True,
    title="Item Counter",
    provides=[{"key": "count", "type": "int"}],
    config_schema=CounterConfig,
)
def app(config, instance_config):
    widget = pmui.IntInput(
        label=instance_config.param.label,
        value=10,
        start=instance_config.param.start,
        end=instance_config.param.end,
        step=instance_config.param.step,
    )
    widget.link(config, value="count")
    config.count = widget.value
    return widget
