"""A display component that receives and shows a category."""

import panel as pn

from panel_flowdash import register
from panel_flowdash.ui.text import escape_markdown


@register(
    page=False,
    component=True,
    title="Category Display",
    requires=[{"key": "category", "type": "str"}],
)
def app(config):
    category = getattr(config, "category", None) or "nothing selected"
    # The value comes from another component, so it is shown as text, not Markdown.
    return pn.pane.Markdown(f"## Selected: {escape_markdown(category)}")
