"""A simple home page."""

import panel as pn

from panel_flowdash import register


@register(page=True, title="Home")
def app():
    return pn.pane.Markdown(
        "# Welcome to FlowDash\n\n"
        "This is a basic example showing page-based navigation.\n\n"
        "Use the sidebar to switch between pages."
    )
