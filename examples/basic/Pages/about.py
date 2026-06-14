"""An about page with reactive content."""

import panel as pn
import panel_material_ui as pmui

from panel_flowdash import register


@register(page=True, title="About")
def app():
    counter = pmui.IntInput(label="Visit count", value=0, start=0)
    message = pn.pane.Markdown()

    def _update(event):
        message.object = f"You've clicked **{event.new}** times."

    counter.param.watch(_update, "value")
    message.object = "You've clicked **0** times."

    return pn.Column(
        "## About This App",
        "Built with **panel-flowdash**, a dataflow dashboard framework for Panel.",
        counter,
        message,
    )
