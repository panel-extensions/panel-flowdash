"""A compact code editor for config fields holding code, SQL or Markdown.

A text param declares the editor with its FlowDash metadata::

    sql = param.String(metadata={"flowdash": {"widget": "code", "language": "sql"}})

and a JSON-schema property with ``"x-flowdash": {"widget": "code", "language": "sql"}``.
The inspector shows a few lines with highlighting for the language, and a
button opening the same text in a large editor: the editor's code panel under
the canvas when it has room for one, see :class:`~panel_flowdash.ui.code_panel.CodePanel`,
or a dialog.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.layout import Column
from panel.widgets import CodeEditor
from panel.widgets.base import CompositeWidget

from panel_flowdash.component_spec import widget_hint

#: The ``widget`` metadata value that asks for a code editor.
CODE_WIDGET = "code"

_COMPACT_HEIGHT = 140
_EXPANDED_HEIGHT = 480

_LABEL_SX = {"color": "text.secondary"}


def code_language(parameter: param.Parameter) -> str | None:
    """Return the language a text param is edited as code in, or None if it is plain text."""
    if not isinstance(parameter, param.String) or widget_hint(parameter) != CODE_WIDGET:
        return None
    language = (parameter.metadata or {})["flowdash"].get("language")
    return language if isinstance(language, str) and language else "text"


class AceOptions(JSComponent):
    """Sets Ace options Panel's ``CodeEditor`` does not expose on the editors beside it.

    It looks for the Ace editors among its siblings in the layout it is
    placed in and applies :attr:`options`, e.g. ``enableKeyboardAccessibility``
    so Tab moves on rather than trapping the keyboard (Enter edits, Escape
    leaves the text), and ``wrap`` for soft-wrapped lines. Bumping
    :attr:`focus` focuses the editor with the :attr:`focus_class` CSS class.
    """

    options = param.Dict(default={})

    focus = param.Integer(default=0)

    focus_class = param.String(default="")

    _esm = """
    function editorsUnder(root, found = []) {
      for (const el of root.querySelectorAll("*")) {
        if (el.classList.contains("ace_editor") && el.env && el.env.editor) {
          found.push([root.host, el.env.editor])
        }
        if (el.shadowRoot) editorsUnder(el.shadowRoot, found)
      }
      return found
    }

    function siblings(el) {
      const host = el.getRootNode().host
      const root = host && host.parentNode
      return root && root.querySelectorAll ? editorsUnder(root) : []
    }

    function apply(el, model, tries = 0) {
      const found = siblings(el)
      for (const [, editor] of found) {
        try { editor.setOptions(model.options) } catch (e) { console.warn(e) }
      }
      // Ace renders after this component when the page is slow, so keep looking.
      if (!found.length && tries < 150) {
        setTimeout(() => apply(el, model, tries + 1), 200)
      }
    }

    // The large editor renders in a dialog, outside this layout, once opened.
    function focus(model, tries = 0) {
      const target = editorsUnder(document).find(
        ([host]) => host && host.classList.contains(model.focus_class) && host.getClientRects().length
      )
      if (target) {
        try { target[1].setOptions(model.options) } catch (e) { console.warn(e) }
        target[1].navigateFileEnd()
        target[1].focus()
      } else if (tries < 150) {
        setTimeout(() => focus(model, tries + 1), 100)
      }
    }

    export function render({ el, model }) {
      el.style.display = "none"
      apply(el, model)
      model.on("options", () => apply(el, model))
      model.on("focus", () => focus(model))
    }
    """


#: The Ace options every code field uses, see :class:`AceOptions`.
ACE_OPTIONS = {"enableKeyboardAccessibility": True, "wrap": True}


class AceTools(JSComponent):
    """Adds diagnostics, completions and a Run key to the Ace editor with :attr:`target_class`.

    Each diagnostic, ``{"row", "column", "end_column", "text", "suggestion"}``
    with 0-based rows and columns, gets an error marker in the gutter, a
    squiggle under its text and its message in a line under it, with a button
    applying the suggestion if it has one. :attr:`completions`,
    ``{"value", "meta"}`` each, are offered while typing. Ctrl+Enter
    (Cmd+Enter on macOS) sends a ``run`` message with the editor's text.
    The component itself shows the cursor position, e.g. ``Ln 5, Col 19``.
    """

    target_class = param.String(doc="The CSS class of the CodeEditor to extend.")

    options = param.Dict(default={})

    diagnostics = param.List(default=[])

    completions = param.List(default=[])

    focus = param.Integer(default=0)

    _stylesheets: t.ClassVar[list[str]] = [
        """
        :host { color: var(--mui-palette-text-secondary); font-size: 12px; white-space: nowrap; }
        """
    ]

    _esm = r"""
    const STYLE = `
    .fd-ace-error { position: absolute; border-bottom: 2px wavy var(--mui-palette-error-main, #d32f2f); }
    .fd-ace-widget {
      display: flex; align-items: flex-start; gap: 8px; margin: 2px 8px 4px 0;
      padding: 6px 10px; border-left: 3px solid var(--mui-palette-error-main, #d32f2f);
      background: rgba(211, 47, 47, 0.08); border-radius: 0 4px 4px 0;
      font: 12px/1.45 var(--mui-typography-fontFamily, sans-serif); white-space: pre-wrap;
      color: var(--mui-palette-text-primary, inherit);
    }
    .fd-ace-widget span { flex: 1; }
    .fd-ace-widget button {
      border: none; background: none; cursor: pointer; padding: 0; font: inherit;
      color: var(--mui-palette-primary-main, #1976d2); white-space: nowrap;
    }
    `

    function editorsUnder(root, found = []) {
      for (const el of root.querySelectorAll("*")) {
        if (el.classList.contains("ace_editor") && el.env && el.env.editor) {
          found.push([root.host, el.env.editor, el])
        }
        if (el.shadowRoot) editorsUnder(el.shadowRoot, found)
      }
      return found
    }

    function find(model) {
      return editorsUnder(document).find(
        ([host]) => host && host.classList.contains(model.target_class)
      )
    }

    export function render({ el, model }) {
      let editor = null
      let markers = []
      let widgets = []

      function position() {
        const pos = editor.getCursorPosition()
        el.textContent = `Ln ${pos.row + 1}, Col ${pos.column + 1}`
      }

      function clear() {
        const session = editor.session
        for (const id of markers) session.removeMarker(id)
        if (session.widgetManager) {
          for (const w of widgets) session.widgetManager.removeLineWidget(w)
        }
        markers = []
        widgets = []
      }

      function diagnose() {
        clear()
        const session = editor.session
        const Range = window.ace.require("ace/range").Range
        if (!session.widgetManager) {
          const LineWidgets = window.ace.require("ace/line_widgets").LineWidgets
          session.widgetManager = new LineWidgets(session)
          session.widgetManager.attach(editor)
        }
        const rows = session.getLength()
        for (const d of model.diagnostics) {
          const row = Math.min(Math.max(d.row || 0, 0), rows - 1)
          const line = session.getLine(row)
          if (d.column != null) {
            const start = Math.min(d.column, line.length)
            let end = d.end_column != null ? d.end_column : start + 1
            if (end <= start) { end = start + 1 }
            const range = new Range(row, start, row, Math.min(end, Math.max(line.length, start + 1)))
            markers.push(session.addMarker(range, "fd-ace-error", "text", true))
          }
          const box = document.createElement("div")
          box.className = "fd-ace-widget fd-code-diagnostic"
          const text = document.createElement("span")
          text.textContent = d.text
          box.appendChild(text)
          if (d.suggestion && d.column != null) {
            const fix = document.createElement("button")
            fix.className = "fd-code-fix"
            fix.textContent = `Use ${d.suggestion}`
            fix.addEventListener("click", () => {
              const end = d.end_column != null ? d.end_column : d.column + 1
              session.replace(new Range(row, d.column, row, end), d.suggestion)
              editor.focus()
            })
            box.appendChild(fix)
          }
          const widget = {row, fixedWidth: true, coverGutter: false, el: box}
          session.widgetManager.addLineWidget(widget)
          widgets.push(widget)
        }
      }

      function complete() {
        const tools = window.ace.require("ace/ext/language_tools")
        const words = {
          identifierRegexps: [/[a-zA-Z_0-9$\u00A2-\u2000\u2070-\uFFFF]/],
          getCompletions: (_ed, _session, _pos, _prefix, callback) => {
            callback(null, model.completions.map(c => ({
              caption: c.value, value: c.value, meta: c.meta || "", score: 1000
            })))
          }
        }
        editor.completers = [words, tools.keyWordCompleter, tools.textCompleter].filter(c => c)
      }

      function attach(tries = 0) {
        const found = find(model)
        if (!found) {
          if (tries < 150) { setTimeout(() => attach(tries + 1), 100) }
          return
        }
        editor = found[1]
        const root = found[2].getRootNode()
        if (root && !root.querySelector("style.fd-ace-tools")) {
          const style = document.createElement("style")
          style.className = "fd-ace-tools"
          style.textContent = STYLE
          ;(root.head || root).appendChild(style)
        }
        try { editor.setOptions({...model.options, enableLiveAutocompletion: true}) } catch (e) { console.warn(e) }
        editor.commands.addCommand({
          name: "flowdashRun",
          bindKey: {win: "Ctrl-Enter", mac: "Command-Enter"},
          exec: (ed) => { model.send_msg({type: "run", text: ed.getValue()}) },
        })
        editor.selection.on("changeCursor", position)
        position()
        complete()
        diagnose()
      }

      el.textContent = ""
      attach()
      model.on("diagnostics", () => editor && diagnose())
      model.on("completions", () => editor && complete())
      model.on("options", () => editor && editor.setOptions(model.options))
      model.on("focus", () => editor && (editor.focus(), editor.navigateFileEnd()))
      model.on("remove", () => editor && clear())
    }
    """

    def __init__(self, on_run: t.Callable[[str], None] | None = None, **params):
        super().__init__(**params)
        self.on_run = on_run

    def _handle_msg(self, msg):
        if isinstance(msg, dict) and msg.get("type") == "run" and self.on_run is not None:
            text = msg.get("text")
            self.on_run(text if isinstance(text, str) else None)


class CodeField(CompositeWidget):
    """A labelled code editor a few lines high, with a button opening it in a dialog.

    The text is applied when the editor loses focus, not on every key, so a
    config check runs once per edit rather than per character. :attr:`unsent`
    is True while the editor holds text it has not applied yet. Tab moves to
    the next field after Escape; lines wrap.
    """

    value = param.String(default="", allow_None=True)

    language = param.String(default="text")

    unsent = param.Boolean(
        default=False,
        doc="""
        Whether text typed into the editor has not been applied yet.""",
    )

    on_edit = param.Callable(
        default=None,
        doc="""
        Optional callback invoked as ``on_edit(field)`` when the expand button
        is clicked, replacing the default dialog.""",
    )

    _composite_type: t.ClassVar[type] = Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self._label = pmui.Typography(
            self.label or "", variant="caption", sx=_LABEL_SX, margin=(0, 0, 2, 0)
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
        self._ace = AceOptions(options=ACE_OPTIONS, focus_class="fd-code-field-large")
        done = pmui.Button(label="Done", variant="text")
        self._dialog = pmui.Dialog(
            objects=[
                self._large,
                pn.Row(pn.layout.HSpacer(), done, sizing_mode="stretch_width"),
            ],
            title=self.label or "",
            open=False,
            show_close_button=True,
            close_on_click=True,
            width_option="lg",
            css_classes=["fd-code-dialog"],
        )
        self.expand_button.on_click(lambda _event: self._open())
        done.on_click(lambda _event: self._dialog.param.update(open=False))
        self.param.watch(self._sync_label, "label")
        self.param.watch(self._value_set, "value")
        self.param.watch(self._language_set, "language")
        self.param.watch(self._disabled_set, "disabled")
        self._disabled_set()
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
            self._ace,
        ]
        # Lined up with the right edge of the text fields of the form.
        self._composite.margin = (5, 22, 5, 10)

    def _make_editor(self, height: int, css_classes: list[str]) -> CodeEditor:
        editor = CodeEditor(
            value=self.value or "",
            value_input=self.value or "",
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
        editor.param.watch(self._edited, "value")
        editor.param.watch(self._typing, "value_input")
        return editor

    def open_dialog(self):
        """Open the large editor in a dialog and focus it."""
        self._dialog.open = True
        self._ace.focus += 1

    def _open(self):
        if self.disabled:
            return
        if self.on_edit is not None:
            self.on_edit(self)
        else:
            self.open_dialog()

    def _value_set(self, event):
        # The editors hold text, so an unset optional field shows as empty.
        text = event.new or ""
        self._syncing = True
        try:
            for editor in (self._editor, self._large):
                editor.param.update(value=text, value_input=text)
        finally:
            self._syncing = False
        self._typing()

    def _edited(self, event):
        if self._syncing:
            return
        if event.new == (self.value or ""):
            return
        self.value = event.new
        self._typing()

    def _typing(self, *_events):
        text = self.value or ""
        unsent = any(editor.value_input != text for editor in (self._editor, self._large))
        if unsent != self.unsent:
            self.unsent = unsent

    def _language_set(self, event):
        for editor in (self._editor, self._large):
            editor.language = event.new

    def _disabled_set(self, *_events):
        for editor in (self._editor, self._large):
            editor.readonly = self.disabled
        self.expand_button.disabled = self.disabled

    def _sync_label(self, event):
        self._label.object = event.new or ""
        self._dialog.title = event.new or ""
