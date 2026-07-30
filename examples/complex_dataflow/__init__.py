import panel as pn
import panel_material_ui as pmui

pn.extension("tabulator", "vega", "deckgl", "jsoneditor")


def _assistant_reply(contents, user, instance):
    return f"You said: {contents}"


# Built once at import time so the conversation persists across navigations.
_chat = pmui.ChatInterface(
    callback=_assistant_reply,
    callback_user="Assistant",
    show_rerun=False,
    show_undo=False,
    show_clear=True,
    sizing_mode="stretch_both",
)

_chat_header = pmui.Typography(
    "Ask the assistant about your data and dashboards.",
    variant="body2",
    margin=(8, 16),
    styles={"opacity": "0.7"},
)

_assistant_panel = pn.Column(
    pmui.Typography("Data Assistant", variant="h6", margin=(8, 16, 0, 16)),
    _chat_header,
    _chat,
    sizing_mode="stretch_both",
)


def configure_layout(app, content, route):
    """Show a chat assistant in the contextbar on every page except the launcher."""
    app.contextbar = [_assistant_panel]
    app.contextbar_open = route != "/"
