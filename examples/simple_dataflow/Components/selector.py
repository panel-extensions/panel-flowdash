"""A selector component that outputs a chosen category."""

import panel_material_ui as pmui

from panel_flowdash import register


@register(
    page=False,
    component=True,
    title="Category Selector",
    provides=[{"key": "category", "type": "str"}],
)
def app(config):
    return pmui.Select(
        label="Category",
        options=["Electronics", "Clothing", "Food", "Books"],
        value="Electronics",
    )
