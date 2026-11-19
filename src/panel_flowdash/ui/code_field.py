"""A compact code editor for config fields holding code, SQL or Markdown.

A text param declares the editor with its FlowDash metadata::

    sql = param.String(metadata={"flowdash": {"widget": "code", "language": "sql"}})

and a JSON-schema property with ``"x-flowdash": {"widget": "code", "language": "sql"}``.
The inspector shows a few lines with highlighting for the language, and a
button opening the same text in a large editor.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.layout import Column
from panel.widgets import CodeEditor
from panel.widgets.base import CompositeWidget

#: The ``widget`` metadata value that asks for a code editor.
CODE_WIDGET = "code"

_COMPACT_HEIGHT = 140
_EXPANDED_HEIGHT = 480

_LABEL_SX = {"color": "text.secondary"}


def code_language(parameter: param.Parameter) -> str | None:
    """Return the language a text param is edited as code in, or None if it is plain text."""
    if not isinstance(parameter, param.String):
        return None
    flowdash = (parameter.metadata or {}).get("flowdash")
    if not isinstance(flowdash, dict) or flowdash.get("widget") != CODE_WIDGET:
        return None
    language = flowdash.get("language")
    return language if isinstance(language, str) and language else "text"


class CodeField(CompositeWidget):
    """A labelled code editor a few lines high, with a button opening it in a dialog.

    The text is applied when the editor loses focus, not on every key, so a
    config check runs once per edit rather than per character.
    """

    value = param.String(default="", allow_None=True)

    language = param.String(default="text")

    _composite_type: t.ClassVar[type] = Column

    def __init__(self, **params):
        super().__init__(**params)
        self._label = pmui.Typography(
            self.name, variant="caption", sx=_LABEL_SX, margin=(0, 0, 2, 0)
        )
        self.expand_button = pmui.IconButton(
            icon="open_in_full",
            description="Edit in a larger editor",
            size="small",
            margin=0,
            css_classes=["fd-code-expand"],
        )
        self._editor = self._make_editor(_COMPACT_HEIGHT, ["fd-code-field"])
        self._large = self._make_editor(_EXPANDED_HEIGHT, ["fd-code-field-large"])
        done = pmui.Button(label="Done", variant="text")
        self._dialog = pmui.Dialog(
            objects=[
                self._large,
                pn.Row(pn.layout.HSpacer(), done, sizing_mode="stretch_width"),
            ],
            title=self.name,
            open=False,
            width_option="lg",
            css_classes=["fd-code-dialog"],
        )
        self.expand_button.on_click(lambda _event: self._dialog.param.update(open=True))
        done.on_click(lambda _event: self._dialog.param.update(open=False))
        self.param.watch(self._sync_label, "name")
        self._composite[:] = [
            pn.Row(
                self._label,
                pn.layout.HSpacer(),
                self.expand_button,
                margin=0,
                sizing_mode="stretch_width",
            ),
            self._editor,
            self._dialog,
        ]
        # Lined up with the right edge of the text fields of the form.
        self._composite.margin = (5, 22, 5, 10)

    def _make_editor(self, height: int, css_classes: list[str]) -> CodeEditor:
        editor = CodeEditor(
            value=self.value or "",
            language=self.language,
            height=height,
            on_keyup=False,
            sizing_mode="stretch_width",
            margin=0,
            css_classes=css_classes,
            styles={
                "border": "1px solid var(--mui-palette-divider, #ccc)",
                "border-radius": "4px",
            },
        )
        self.link(editor, value="value", language="language", bidirectional=True)
        return editor

    def _sync_label(self, event):
        self._label.object = event.new
        self._dialog.title = event.new
