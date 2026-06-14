"""A display component that receives and shows a category."""

import panel as pn

from panel_flowdash import register


@register(
    page=False,
    component=True,
    title="Category Display",
    requires=[{"key": "category", "type": "str"}],
)
def app(config):
    category = getattr(config, "category", None) or "nothing selected"
    return pn.pane.Markdown(f"## Selected: {category}")
