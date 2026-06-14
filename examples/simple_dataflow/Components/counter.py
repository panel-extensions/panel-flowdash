"""A counter component that provides a numeric value."""

import panel_material_ui as pmui

from panel_flowdash import register


@register(
    page=False,
    component=True,
    title="Item Counter",
    provides=[{"key": "count", "type": "int"}],
)
def app(config):
    return pmui.IntInput(label="Count", value=10, start=0, end=1000)
