"""A compact code field for config parameters holding code, SQL, JSON or Markdown.

A text param declares the field with its FlowDash metadata::

    sql = param.String(metadata={"flowdash": {"widget": "code", "language": "sql"}})

and a JSON-schema property with ``"x-flowdash": {"widget": "code", "language": "sql"}``.
The inspector shows a read-only snippet of the first few lines with its line
count, check status and an Edit button opening the full text in the editor's
code panel under the canvas when it has room for one, see
:class:`~panel_flowdash.ui.code_panel.CodePanel`, or in a dialog.
"""

from __future__ import annotations

import html
import re
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

_SNIPPET_LINES = 5
_EXPANDED_HEIGHT = 480

_SQL_KEYWORDS = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "GROUP",
        "BY",
        "ORDER",
        "HAVING",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "OUTER",
        "FULL",
        "CROSS",
        "ON",
        "AS",
        "AND",
        "OR",
        "NOT",
        "IS",
        "NULL",
        "IN",
        "LIMIT",
        "OFFSET",
        "DISTINCT",
        "UNION",
        "ALL",
        "WITH",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "ASC",
        "DESC",
        "LIKE",
        "ILIKE",
        "BETWEEN",
        "CAST",
        "OVER",
        "PARTITION",
        "CREATE",
        "DROP",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TABLE",
        "VIEW",
        "TRUE",
        "FALSE",
    }
)

_PY_KEYWORDS = frozenset(
    {
        "def",
        "class",
        "return",
        "if",
        "elif",
        "else",
        "for",
        "while",
        "in",
        "not",
        "and",
        "or",
        "is",
        "None",
        "True",
        "False",
        "import",
        "from",
        "as",
        "with",
        "try",
        "except",
        "finally",
        "raise",
        "yield",
        "lambda",
        "pass",
        "break",
        "continue",
        "async",
        "await",
    }
)

_SQL_TOKEN = re.compile(
    r"(?P<comment>--.*$)"
    r"|(?P<string>'(?:''|[^'])*')"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)"
)

_JSON_TOKEN = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])*"(?=\s*:))'
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<number>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
    r"|(?P<literal>\b(?:true|false|null)\b)"
)

_PY_TOKEN = re.compile(
    r"(?P<comment>#.*$)"
    r"|(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)"
)

_YAML_TOKEN = re.compile(
    r"(?P<comment>#.*$)"
    r"|(?P<key>^[ \t]*[A-Za-z0-9_.-]+(?=\s*:))"
    r"|(?P<string>'(?:''|[^'])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<literal>\b(?:true|false|null)\b)"
)

_SNIPPET_CSS = """
.fd-snip-wrap {
  padding: 6px 0 6px;
  overflow: hidden;
}
.fd-snip-label {
  font-family: Roboto, sans-serif;
  font-size: 11px;
  font-weight: 500;
  line-height: 14px;
  color: var(--mui-palette-text-secondary, #666);
  padding: 0 10px 4px;
}
.fd-snip-lines {
  font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 19px;
  overflow: hidden;
  user-select: text;
}
.fd-snip-ln {
  display: flex;
  min-height: 19px;
}
.fd-snip-no {
  width: 30px;
  flex: none;
  text-align: right;
  padding-right: 10px;
  color: var(--mui-palette-text-disabled, #9e9e9e);
  user-select: none;
}
.fd-snip-tx {
  white-space: pre;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
  padding-right: 10px;
  color: var(--mui-palette-text-primary, inherit);
}
.fd-snip-empty {
  color: var(--mui-palette-text-disabled, #9e9e9e);
  font-style: italic;
}
.fd-hk { color: var(--mui-palette-primary-main, #0072b5); font-weight: 500; }
.fd-hn { color: var(--mui-palette-warning-main, #b35c00); }
.fd-hs { color: var(--mui-palette-success-main, #2e7d32); }
.fd-hc { color: var(--mui-palette-text-disabled, #9e9e9e); font-style: italic; }
"""

_FOOTER_CSS = """
.fd-code-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  line-height: 16px;
  color: var(--mui-palette-text-secondary, #666);
  white-space: nowrap;
  overflow: hidden;
}
.fd-code-st {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 500;
}
.fd-st-ok { color: var(--mui-palette-success-main, #2e7d32); }
.fd-st-err { color: var(--mui-palette-error-main, #d32f2f); }
.fd-st-warn { color: var(--mui-palette-warning-main, #ed6c02); }
.fd-mi {
  font-family: "Material Icons";
  font-size: 15px;
  line-height: 1;
  font-feature-settings: "liga";
}
.fd-code-unsent {
  color: var(--mui-palette-warning-main, #ed6c02);
  font-weight: 500;
}
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _highlight_line(line: str, language: str) -> str:
    lang = (language or "text").lower()
    if lang == "sql":
        pattern = _SQL_TOKEN
    elif lang == "json":
        pattern = _JSON_TOKEN
    elif lang in ("python", "py"):
        pattern = _PY_TOKEN
    elif lang in ("yaml", "yml"):
        pattern = _YAML_TOKEN
    else:
        return _esc(line)

    out: list[str] = []
    pos = 0
    for match in pattern.finditer(line):
        start, end = match.span()
        if start > pos:
            out.append(_esc(line[pos:start]))
        kind = match.lastgroup
        token = match.group(0)
        escaped = _esc(token)
        if kind == "comment":
            out.append(f'<span class="fd-hc">{escaped}</span>')
        elif kind == "string":
            out.append(f'<span class="fd-hs">{escaped}</span>')
        elif kind in ("number", "literal"):
            out.append(f'<span class="fd-hn">{escaped}</span>')
        elif kind == "key":
            out.append(f'<span class="fd-hk">{escaped}</span>')
        elif kind == "word":
            if (lang == "sql" and token.upper() in _SQL_KEYWORDS) or (
                lang in ("python", "py") and token in _PY_KEYWORDS
            ):
                out.append(f'<span class="fd-hk">{escaped}</span>')
            else:
                out.append(escaped)
        else:
            out.append(escaped)
        pos = end
    if pos < len(line):
        out.append(_esc(line[pos:]))
    return "".join(out)


def _render_snippet(value: str | None, language: str, label: str = "") -> str:
    text = value or ""
    head = f'<div class="fd-snip-label">{_esc(label)}</div>' if label else ""
    if not text:
        body = (
            '<div class="fd-snip-ln"><span class="fd-snip-no">1</span>'
            '<span class="fd-snip-tx fd-snip-empty">Empty</span></div>'
        )
    else:
        lines = text.splitlines()[:_SNIPPET_LINES]
        body = "".join(
            f'<div class="fd-snip-ln"><span class="fd-snip-no">{idx}</span>'
            f'<span class="fd-snip-tx">{_highlight_line(line, language)}</span></div>'
            for idx, line in enumerate(lines, start=1)
        )
    return (
        f'<div class="fd-snip-wrap">{head}'
        f'<div class="fd-snip-lines" data-language="{_esc(language)}">{body}</div></div>'
    )


def _line_count_text(value: str | None) -> str:
    text = value or ""
    if not text:
        return "Empty"
    count = len(text.splitlines())
    return f"{count} line" if count == 1 else f"{count} lines"


def _render_footer_status(
    value: str | None,
    check_status: tuple[str, str] | None,
    unsent: bool,
) -> str:
    parts: list[str] = []
    if check_status is not None:
        kind, label = check_status
        cls, icon = {
            "ok": ("fd-st-ok", "check_circle"),
            "error": ("fd-st-err", "error"),
            "warning": ("fd-st-warn", "warning"),
        }.get(kind, ("fd-st-ok", "check_circle"))
        parts.append(
            f'<span class="fd-code-st {cls}">'
            f'<span class="fd-mi" aria-hidden="true">{icon}</span>{_esc(label)}</span>'
        )
    parts.append(f"<span>{_esc(_line_count_text(value))}</span>")
    if unsent:
        parts.append('<span class="fd-code-unsent">Unsaved</span>')
    return f'<div class="fd-code-meta">{"".join(parts)}</div>'


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


#: Ace's GitHub themes highlight the cursor line in a saturated blue; follow the page's palette.
ACE_THEME_CSS = """
.ace_editor .ace_marker-layer .ace_active-line,
.ace_editor .ace_gutter-active-line {
  background: var(--mui-palette-action-hover, rgba(0, 0, 0, 0.04)) !important;
}
.ace_editor .ace_marker-layer .ace_selection {
  background: rgba(var(--mui-palette-primary-mainChannel, 0 114 181) / 0.3) !important;
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

    closing = param.Integer(default=0, doc="Bumped to close the completion popup.")

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
        // Not the text completer, which offers every number and word typed so far.
        editor.completers = [words, tools.keyWordCompleter].filter(c => c)
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
          exec: (ed) => {
            if (ed.completer) { ed.completer.detach() }
            model.send_msg({type: "run", text: ed.getValue()})
          },
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
      model.on("closing", () => editor && editor.completer && editor.completer.detach())
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
    """A read-only code snippet with line count, check status and an Edit button.

    Clicking Edit calls :attr:`on_edit` when set, otherwise opens a large Ace
    dialog where edits are applied when the editor loses focus. :attr:`unsent`
    is True while the editor holds text it has not applied yet.
    """

    value = param.String(default="", allow_None=True)

    language = param.String(default="text")

    unsent = param.Boolean(
        default=False,
        doc="""
        Whether text typed into the editor has not been applied yet.""",
    )

    check_status = param.Parameter(
        default=None,
        doc="""
        Optional ``(kind, label)`` pair shown in the snippet footer, where
        *kind* is ``"ok"``, ``"error"`` or ``"warning"``.""",
    )

    on_edit = param.Callable(
        default=None,
        doc="""
        Optional callback invoked as ``on_edit(field)`` when Edit is clicked,
        replacing the default dialog.""",
    )

    _composite_type: t.ClassVar[type] = Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self._snippet = pn.pane.HTML(
            _render_snippet(self.value, self.language, self.label or ""),
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_SNIPPET_CSS],
            css_classes=["fd-code-field", "fd-code-snippet"],
        )
        self._status_html = pn.pane.HTML(
            _render_footer_status(self.value, self.check_status, self.unsent),
            sizing_mode="stretch_width",
            margin=0,
            stylesheets=[_FOOTER_CSS],
            css_classes=["fd-code-status"],
        )
        self.edit_button = pmui.Button(
            label="Edit",
            icon="edit",
            variant="text",
            size="small",
            margin=0,
            sx={
                "minWidth": 0,
                "textTransform": "none",
                "py": "2px",
                "px": "8px",
                "fontSize": "0.75rem",
                "flexShrink": 0,
            },
            css_classes=["fd-code-expand", "fd-code-edit"],
        )
        # Alias kept for existing callers and tests.
        self.expand_button = self.edit_button
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
        footer = pmui.Row(
            self._status_html,
            self.edit_button,
            sizing_mode="stretch_width",
            margin=0,
            sx={
                "alignItems": "center",
                "borderTop": 1,
                "borderColor": "divider",
                "pl": "10px",
                "pr": "4px",
                "minHeight": "32px",
                "gap": "6px",
            },
            css_classes=["fd-code-footer"],
        )
        self._card = pmui.Column(
            self._snippet,
            footer,
            sizing_mode="stretch_width",
            margin=0,
            sx={
                "border": 1,
                "borderColor": "divider",
                "borderRadius": "4px",
                "bgcolor": "background.paper",
                "overflow": "hidden",
            },
            css_classes=["fd-code-card"],
        )
        self.edit_button.on_click(lambda _event: self._open())
        done.on_click(lambda _event: self._dialog.param.update(open=False))
        self.param.watch(self._sync_label, "label")
        self.param.watch(self._value_set, "value")
        self.param.watch(self._language_set, "language")
        self.param.watch(self._disabled_set, "disabled")
        self.param.watch(self._status_set, ["check_status", "unsent"])
        self._disabled_set()
        self._composite[:] = [self._card, self._dialog, self._ace]
        self._composite.margin = (8, 0, 6, 0)

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
            stylesheets=[ACE_THEME_CSS],
            styles={
                "border": "1px solid var(--mui-palette-divider, #ccc)",
                "border-radius": "4px",
            },
        )
        editor.param.watch(self._edited, "value")
        editor.param.watch(self._typing, "value_input")
        return editor

    def open_dialog(self):
        """Open the full-size code dialog and focus its editor."""
        self._dialog.open = True
        self._ace.focus += 1

    def _open(self):
        if self.disabled:
            # A constant field opens read-only, so its whole text can be read.
            self.open_dialog()
        elif self.on_edit is not None:
            self.on_edit(self)
        else:
            self.open_dialog()

    def _refresh_snippet(self):
        self._snippet.object = _render_snippet(self.value, self.language, self.label or "")
        self._status_html.object = _render_footer_status(
            self.value, self.check_status, self.unsent
        )

    def _value_set(self, event):
        text = event.new or ""
        self._syncing = True
        try:
            self._large.param.update(value=text, value_input=text)
        finally:
            self._syncing = False
        self._typing()
        self._refresh_snippet()

    def _edited(self, event):
        if self._syncing:
            return
        if event.new == (self.value or ""):
            return
        self.value = event.new
        self._typing()

    def _typing(self, *_events):
        text = self.value or ""
        unsent = self._large.value_input != text
        if unsent != self.unsent:
            self.unsent = unsent

    def _language_set(self, event):
        self._large.language = event.new
        self._refresh_snippet()

    def _status_set(self, *_events):
        self._status_html.object = _render_footer_status(
            self.value, self.check_status, self.unsent
        )

    def _disabled_set(self, *_events):
        self._large.readonly = self.disabled
        self.edit_button.param.update(
            label="View" if self.disabled else "Edit",
            icon="visibility" if self.disabled else "edit",
        )

    def _sync_label(self, event):
        label = event.new or ""
        self._dialog.title = label
        self._refresh_snippet()
