"""A spec edited as a form over its curated settings and as JSON or YAML, kept in sync both ways.

The inspector uses :class:`SpecEditor` for a ``param.Dict`` that declares
settings, see :mod:`panel_flowdash.spec_settings`. Changing a control writes
its path into the spec, and the lines that changed are highlighted in the
text. Editing the text (applied on blur or Ctrl+Enter, like a code field, or
with :attr:`SpecEditor.live` once it parses and typing pauses) reads every
path back into the form, and the text stays as typed. Keys no control covers
are kept as written, marked in the editor's gutter and listed at the end of
the form with a link to their lines, and while the text does not parse the
form is disabled and the spec keeps its last valid value.

YAML is read as YAML 1.2, so keys like ``on`` stay strings, and its comments
are kept through edits made in the form.

The parts are exposed separately (:attr:`SpecEditor.form`,
:attr:`SpecEditor.spec`) for a host laying them out itself, e.g. beside a
live preview.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.layout import Column
from panel.widgets import CodeEditor
from panel.widgets.base import CompositeWidget

from panel_flowdash.spec_settings import (
    SpecPathError,
    SpecSetting,
    SpecTextError,
    changed_paths,
    format_spec,
    format_spec_yaml,
    get_path,
    parse_spec,
    parse_spec_yaml,
    path_text,
    shown_in_spec,
    text_spans,
    unrepresented_paths,
)
from panel_flowdash.ui.code_field import ACE_OPTIONS, ACE_THEME_CSS, AceOptions

if t.TYPE_CHECKING:
    from panel_flowdash.config_choices import Choices

MODES = {"Form": "form", "Form + Spec": "both", "Spec": "spec"}

#: The names of the text syntaxes, as people read them.
SYNTAX_LABELS = {"json": "JSON", "yaml": "YAML"}

#: Seconds of no typing after which :attr:`SpecEditor.live` applies text that parses.
APPLY_DELAY = 0.6

_EDITOR_HEIGHT = 300

_LABEL_WIDTH = 84

#: The option that leaves a setting out of the spec.
NOT_SET = "Not set"

#: What a control shows for a value in the spec it cannot show.
SET_IN_SPEC = "Set in the spec"

# Hides a control's own label from sight but not from assistive technology.
_HIDDEN_LABEL_CSS = """
.MuiInputLabel-root, .MuiFormLabel-root {
  position: absolute !important;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
fieldset > legend { display: none !important; }
"""

_RADIO_SX = {
    "& .MuiToggleButton-root, & .MuiButton-root": {
        "textTransform": "none",
        "whiteSpace": "nowrap",
        "px": 0.75,
        "minWidth": 0,
    },
}

_CAPTION_SX = {"color": "text.secondary"}

_GROUP_SX = {
    "color": "text.secondary",
    "textTransform": "uppercase",
    "letterSpacing": "0.06em",
    "fontWeight": 600,
}

_EDITOR_CSS = """
.ace_gutter-cell.fd-spec-only {
  box-shadow: inset 3px 0 0 var(--mui-palette-warning-main, #ed6c02);
}
.ace_gutter-cell.fd-spec-changed-row {
  box-shadow: inset 3px 0 0 var(--mui-palette-success-main, #2e7d32);
}
.fd-spec-changed {
  position: absolute;
  background: rgba(46, 125, 50, 0.16);
}
"""

_UNSHOWN_CSS = """
:host { min-width: 0; }
.fd-spec-path { display: flex; align-items: baseline; gap: 8px; min-width: 0; font-size: 13px;
  line-height: 20px; color: var(--mui-palette-text-primary); }
.fd-spec-path code { font-family: var(--fd-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fd-spec-path span { color: var(--mui-palette-text-secondary); font-size: 12px; white-space: nowrap; }
"""


class SpecMarks(JSComponent):
    """Marks lines of the Ace editors beside it: rows only in the spec, and rows just changed.

    :attr:`only` rows get an amber bar in the gutter, :attr:`changed` rows a
    green bar and a green background, until the next change.
    """

    only = param.List(default=[])

    changed = param.List(default=[])

    goto = param.Dict(
        default={},
        doc="""
        ``{"row": n, "count": k}``: puts the cursor on line *n* (from 0) of the
        editor, scrolled into view, and gives it focus; *count* tells requests
        for the same line apart.""",
    )

    _esm = """
    function editorsUnder(root, found = []) {
      for (const el of root.querySelectorAll("*")) {
        if (el.classList.contains("ace_editor") && el.env && el.env.editor) found.push(el.env.editor)
        if (el.shadowRoot) editorsUnder(el.shadowRoot, found)
      }
      return found
    }

    function siblings(el) {
      const host = el.getRootNode().host
      const root = host && host.parentNode
      return root && root.querySelectorAll ? editorsUnder(root) : []
    }

    function mark(el, model, tries = 0) {
      const editors = siblings(el)
      if (!editors.length) {
        if (tries < 150) setTimeout(() => mark(el, model, tries + 1), 200)
        return
      }
      for (const editor of editors) {
        const session = editor.session
        const state = session.__fdSpecMarks || {only: [], changed: [], markers: []}
        for (const row of state.only) session.removeGutterDecoration(row, "fd-spec-only")
        for (const row of state.changed) session.removeGutterDecoration(row, "fd-spec-changed-row")
        for (const id of state.markers) session.removeMarker(id)
        const markers = []
        for (const row of model.only) session.addGutterDecoration(row, "fd-spec-only")
        for (const row of model.changed) {
          session.addGutterDecoration(row, "fd-spec-changed-row")
          markers.push(session.highlightLines(row, row, "fd-spec-changed", false).id)
        }
        session.__fdSpecMarks = {only: [...model.only], changed: [...model.changed], markers}
      }
    }

    function goto(el, model, tries = 0) {
      const editor = siblings(el).find((e) => e.container.offsetParent !== null)
      if (!editor) {
        if (tries < 25) setTimeout(() => goto(el, model, tries + 1), 100)
        return
      }
      const row = model.goto.row || 0
      editor.resize(true)
      editor.scrollToLine(row, true, false, () => {})
      editor.gotoLine(row + 1, 0, false)
      editor.focus()
    }

    export function render({ el, model }) {
      el.style.display = "none"
      const update = () => setTimeout(() => mark(el, model), 0)
      update()
      model.on(["only", "changed"], update)
      model.on("goto", () => setTimeout(() => goto(el, model), 50))
    }
    """


def _css_name(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-")


class SpecEditor(CompositeWidget):
    """A spec dict edited through a form of its settings and as JSON or YAML text.

    Parameters
    ----------
    value
        The spec.
    settings
        The :class:`~panel_flowdash.spec_settings.SpecSetting` controls.
    choices
        Choices per setting key, e.g. the columns of an input for
        ``encoding.x.field``, overriding the setting's own options.
    mode
        Whether the form, the text or both are shown.
    syntax
        Whether the text is JSON or YAML.
    live
        Whether typed text is applied once it parses and typing pauses for
        :data:`APPLY_DELAY` seconds, as well as on blur.
    layout
        ``"stacked"`` puts the JSON under the form, ``"columns"`` beside it.
    """

    value = param.Dict(default={})

    settings = param.List(default=[], item_type=SpecSetting)

    choices = param.Dict(default={})

    mode = param.Selector(default="both", objects=list(MODES.values()))

    layout = param.Selector(default="stacked", objects=["stacked", "columns"])

    syntax = param.Selector(default="json", objects=list(SYNTAX_LABELS))

    live = param.Boolean(default=False)

    error = param.String(
        default=None,
        allow_None=True,
        doc="""
        Why the text in the editor is not a valid spec, while it is not.""",
    )

    unsent = param.Boolean(
        default=False,
        doc="""
        Whether text typed into the editor has not been applied yet.""",
    )

    unknown = param.List(
        default=[],
        doc="""
        The dotted paths of the spec the form cannot show, kept as written.""",
    )

    _composite_type: t.ClassVar[type] = Column

    def __init__(self, **params):
        super().__init__(**params)
        self._syncing = False
        self._loading = False
        # Set while the spec is taken from the text, which then stays as typed.
        self._from_text = False
        # The text the spec was last shown as or read from.
        self._applied = ""
        # The YAML document last shown or read, whose comments are kept.
        self._doc: t.Any = None
        # Counts the pauses in typing, so an earlier one does not apply newer text.
        self._pauses = 0
        self._unshown_key: tuple | None = None
        # The paths the last edit changed, marked in the text.
        self._changed: list = []
        self._controls: dict[str, tuple[SpecSetting, t.Any]] = {}
        self._spans: dict = {}
        self._label = pmui.Typography(
            self.label or "", variant="caption", sx=_CAPTION_SX, margin=0
        )
        self.mode_toggle = pmui.RadioButtonGroup(
            options=MODES,
            value=self.mode,
            size="small",
            margin=0,
            sx={
                "& .MuiToggleButton-root, & .MuiButton-root": {
                    "textTransform": "none",
                    "whiteSpace": "nowrap",
                    "py": 0.25,
                    "px": 1,
                }
            },
            css_classes=["fd-spec-mode"],
        )
        self.mode_toggle.link(self, value="mode", bidirectional=True)
        # The paths the form cannot show, each with a link to its lines in the text.
        self.unshown = pn.Column(
            margin=(10, 0, 0, 0),
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-spec-unshown"],
        )
        self.form = pn.Column(margin=0, sizing_mode="stretch_width", css_classes=["fd-spec-form"])
        self.editor = CodeEditor(
            value="",
            language=self.syntax,
            height=_EDITOR_HEIGHT,
            on_keyup=False,
            indent=2,
            soft_tabs=True,
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["fd-spec-editor"],
            stylesheets=[ACE_THEME_CSS, _EDITOR_CSS],
            styles={
                "border": "1px solid var(--mui-palette-divider, #ccc)",
                "border-radius": "4px",
            },
        )
        self.status = pmui.Typography(
            variant="caption", margin=(4, 0, 0, 0), css_classes=["fd-spec-status"]
        )
        self._marks = SpecMarks()
        self._labels: dict[str, dict[str, t.Any]] = {}
        self._notes = pmui.Typography(
            variant="caption",
            sx=_CAPTION_SX,
            margin=(6, 0, 0, 0),
            visible=False,
            css_classes=["fd-spec-notes"],
        )
        self.header = pn.Row(
            self._label,
            pn.layout.HSpacer(),
            self.mode_toggle,
            margin=(0, 0, 6, 0),
            sizing_mode="stretch_width",
            align="center",
        )
        self.spec = pn.Column(
            self.editor,
            self.status,
            self._marks,
            AceOptions(options={**ACE_OPTIONS, "wrap": False, "tabSize": 2}),
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-spec-text"],
        )
        self.editor.param.watch(self._edited, "value")
        self.editor.param.watch(self._typing, "value_input")
        self.param.watch(self._value_set, "value")
        self.param.watch(self._rebuild_form, "settings")
        self.param.watch(lambda _event: self._sync_form(), "choices")
        self.param.watch(self._mode_set, "mode")
        self.param.watch(self._syntax_set, "syntax")
        self.param.watch(self._layout_set, "layout")
        self.param.watch(self._disabled_set, "disabled")
        self.param.watch(lambda e: setattr(self._label, "object", e.new or ""), "label")
        self._rebuild_form()
        self._show_text(self.value)
        self._sync_form()
        self._layout_set()

    # Layout

    def _mode_set(self, *_events):
        self.mode_toggle.visible = bool(self.settings)
        self.form.visible = self.mode in ("form", "both") and bool(self.settings)
        self.spec.visible = self.mode in ("spec", "both") or not self.settings

    def _syntax_set(self, *_events):
        """Show the spec in the new syntax; text typed and not applied is dropped, see :meth:`flush`."""
        self._pauses += 1
        self.editor.language = self.syntax
        # The lines just changed stay marked in the other syntax.
        self._show_text(self.value, self._changed)

    def _layout_set(self, *_events):
        self._mode_set()
        if self.layout == "columns":
            body = pn.Row(
                pn.Column(self.form, margin=(0, 12, 0, 0), sizing_mode="stretch_width"),
                self.spec,
                margin=0,
                sizing_mode="stretch_width",
            )
            self._composite[:] = [self.header, body]
        else:
            self._composite[:] = [self.header, self.form, self.spec]
        self._composite.margin = (5, 22, 5, 10)

    # The form

    def _options(self, setting: SpecSetting, value: t.Any = None) -> dict[str, t.Any]:
        """Return the options of a select or radio holding *value*, by label.

        A setting that may be left unset, one without a default, offers that
        as "Not set" in a select, or in a radio while it is unset, and a
        value outside the options is offered too.
        """
        choices: Choices | None = self.choices.get(setting.key)
        options = choices.labelled() if choices is not None else setting.labelled()
        values = list(options.values())
        if value is not None and value not in values:
            options = {str(value): value, **options}
        if setting.default is None and None not in values:
            if setting.widget == "select" or value is None:
                options = {NOT_SET: None, **options}
        return options

    def _control(self, setting: SpecSetting) -> t.Any:
        # choice_widgets offers this field to the inspector's form, so it is imported late.
        from panel_flowdash.ui.choice_widgets import OptionalFloatInput, OptionalIntInput

        # The label beside the control is drawn by the form; the control keeps
        # its own, hidden, so it is named for assistive technology.
        common: dict[str, t.Any] = {
            "margin": 0,
            "label": setting.title,
            "css_classes": ["fd-spec-setting", f"fd-spec-{_css_name(setting.key)}"],
        }
        if setting.widget != "switch":
            common["stylesheets"] = [_HIDDEN_LABEL_CSS]
        if setting.doc:
            common["description"] = setting.doc
        if setting.widget == "switch":
            return pmui.Switch(value=False, size="small", **common)
        if setting.widget == "radio":
            return pmui.RadioButtonGroup(options=[], size="small", sx=_RADIO_SX, **common)
        if setting.widget == "select":
            return pmui.Select(
                options=[],
                size="small",
                sizing_mode="stretch_width",
                **common,
            )
        if setting.widget == "integer":
            widget = OptionalIntInput
        elif setting.widget == "number":
            widget = OptionalFloatInput
        else:
            return pmui.TextInput(size="small", sizing_mode="stretch_width", **common)
        return widget(
            value=None,
            size="small",
            placeholder=NOT_SET,
            sizing_mode="stretch_width",
            **common,
        )

    def _row(self, setting: SpecSetting, control: t.Any) -> pn.Row:
        """Lay a control out beside its label, as one row of the form."""
        label = pmui.Typography(
            "" if setting.widget == "switch" else setting.title,
            variant="body2",
            width=_LABEL_WIDTH,
            margin=(0, 8, 0, 0),
            align="center",
            css_classes=["fd-spec-label"],
        )
        return pn.Row(label, control, margin=(3, 0), sizing_mode="stretch_width", align="center")

    def _rebuild_form(self, *_events):
        groups: dict[str | None, list] = {}
        self._controls = {}
        self._labels = {}
        for setting in self.settings:
            control = self._control(setting)
            control.param.watch(partial(self._control_changed, setting), "value")
            self._controls[setting.key] = (setting, control)
            groups.setdefault(setting.group, []).append(self._row(setting, control))
        objects: list = []
        for group, rows in groups.items():
            if group:
                objects.append(
                    pmui.Typography(
                        group,
                        variant="caption",
                        sx=_GROUP_SX,
                        margin=(10 if objects else 2, 0, 2, 0),
                        css_classes=["fd-spec-group"],
                    )
                )
            objects.extend(rows)
        objects.extend([self._notes, self.unshown])
        self.form[:] = objects
        self._sync_form()
        self._mode_set()

    def _sync_form(self):
        """Show the spec's value of every setting, and disable the controls that cannot show theirs.

        A select or radio holds the label of its value, so that every value,
        None among them, is shown and can be picked.
        """
        spec = self.value or {}
        beyond = {s.key for s in shown_in_spec(spec, self.settings)}
        notes: list[str] = []
        self._syncing = True
        try:
            for key, (setting, control) in self._controls.items():
                value = setting.read(spec)
                reason = setting.disabled_reason(spec)
                if reason and reason not in notes:
                    notes.append(reason)
                editable = key not in beyond and reason is None
                if key in beyond:
                    shown = {"switch": False, "text": ""}.get(setting.widget)
                elif setting.widget == "switch":
                    shown = bool(value)
                elif setting.widget == "text":
                    shown = value or ""
                else:
                    shown = value
                if setting.widget in ("select", "radio"):
                    if key in beyond:
                        options = {SET_IN_SPEC: value}
                    else:
                        options = self._options(setting, shown)
                    self._labels[key] = options
                    labels = list(options)
                    if control.options != labels:
                        control.options = labels
                    shown = next((label for label, v in options.items() if v == value), None)
                control.param.update(
                    value=shown,
                    disabled=self.disabled or self.error is not None or not editable,
                )
                if setting.widget == "text":
                    control.placeholder = SET_IN_SPEC if key in beyond else ""
        finally:
            self._syncing = False
        self._notes.object = " ".join(notes)
        self._notes.visible = bool(notes)

    def _control_changed(self, setting: SpecSetting, event):
        if self._syncing or self.error is not None:
            return
        value = event.new
        if setting.widget in ("select", "radio"):
            value = self._labels.get(setting.key, {}).get(value)
        try:
            new = setting.write(self.value or {}, value)
        except SpecPathError as exc:
            self.status.object = str(exc)
            self._sync_form()
            return
        if new != self.value or list(new) != list(self.value or {}):
            self.value = new

    def load(self, value: dict | None, comments: t.Any = None):
        """Show *value* as a spec loaded afresh, e.g. another node's, rather than as an edit.

        Unlike setting :attr:`value`, no line is marked as changed, and text
        typed but not applied, valid or not, is dropped. *comments* is a YAML
        document, :attr:`yaml_doc` of earlier text of the same spec, whose
        comments YAML text keeps.
        """
        self._pauses += 1
        self._doc = comments
        self._loading = True
        try:
            self.value = value or {}
        finally:
            self._loading = False
        self._syncing = True
        try:
            # Cleared first, so the text is shown however it compares with what was typed.
            self.editor.param.update(value="", value_input="")
        finally:
            self._syncing = False
        self._show_text(self.value)
        self._sync_form()

    @property
    def yaml_doc(self) -> t.Any:
        """The YAML document of the text last shown or read, holding its comments, for :meth:`load`."""
        return self._doc

    @property
    def syntax_label(self) -> str:
        """The name of the text's syntax, "JSON" or "YAML"."""
        return SYNTAX_LABELS[self.syntax]

    def reveal(self, path: tuple[str, ...]):
        """Show the text with the cursor on the first line of *path*."""
        if self.mode == "form":
            self.mode = "spec"
        row = self._spans.get(tuple(path), (0, 0))[0]
        self._marks.goto = {"row": row, "count": self._marks.goto.get("count", 0) + 1}

    def flush(self) -> bool:
        """Apply text typed and not applied yet, returning whether the text is a valid spec."""
        self._pauses += 1
        text = self.editor.value_input
        if text != self._applied or self.error is not None:
            return self._apply_text(text)
        return True

    # The text

    def _value_set(self, event):
        if self._loading:
            return
        changed = changed_paths(event.old or {}, event.new or {})
        if self._from_text:
            # The text holds the spec as typed, so it is not written again.
            self._mark(event.new or {}, changed)
        else:
            self._show_text(event.new, changed)
        self._sync_form()

    def _render(self, spec: dict) -> tuple[str, dict]:
        if self.syntax == "yaml":
            text, spans, self._doc = format_spec_yaml(spec, self._doc)
            return text, spans
        return format_spec(spec)

    def _show_text(self, spec: dict | None, changed: list | None = None):
        text, spans = self._render(spec or {})
        self._spans = spans
        editor = self.editor
        if editor.value != text or editor.value_input != text:
            self._syncing = True
            try:
                if editor.value == text:
                    # The browser only takes a value that changed, and it holds other typed text.
                    editor.param.update(value="", value_input="")
                editor.param.update(value=text, value_input=text)
            finally:
                self._syncing = False
        self._applied = text
        self.error = None
        self.unsent = False
        self._mark(spec or {}, changed or [])

    def _mark(self, spec: dict, changed: list):
        self._changed = changed
        unknown = unrepresented_paths(spec, self.settings) if self.settings else []
        self.unknown = [path_text(p) for p in unknown]
        only = sorted({row for p in unknown for row in _rows_of(self._spans.get(p))})
        rows = sorted({row for p in changed for row in _rows_of(self._spans.get(p), first=True)})
        self._marks.param.update(only=only, changed=rows)
        self.editor.annotations = [
            {
                "row": self._spans[p][0],
                "column": 0,
                "type": "warning",
                "text": f"The form cannot show {path_text(p)}; it is kept as written.",
            }
            for p in unknown
            if p in self._spans
        ]
        self._show_unshown(spec, unknown)
        self.status.object = ""

    def _show_unshown(self, spec: dict, unknown: list):
        """List the paths the form cannot show at its end, each with a link to its lines."""
        key = (self.syntax, tuple(unknown), tuple(_summary(get_path(spec, p)) for p in unknown))
        if key == self._unshown_key:
            return
        self._unshown_key = key
        rows: list = []
        if unknown:
            rows.append(
                pmui.Typography(
                    "Not in the form",
                    variant="caption",
                    sx=_GROUP_SX,
                    margin=(0, 0, 2, 0),
                    css_classes=["fd-spec-group"],
                )
            )
        for path in unknown:
            link = pmui.Button(
                label=f"Edit in {self.syntax_label}",
                icon="edit_note",
                variant="text",
                size="small",
                margin=0,
                align="center",
                description=f"Edit {path_text(path)} in the {self.syntax_label}",
                sx={"textTransform": "none", "whiteSpace": "nowrap", "flex": "none"},
                css_classes=["fd-spec-edit-in-text"],
            )
            link.on_click(partial(self._reveal_clicked, path))
            label = pn.pane.HTML(
                f'<div class="fd-spec-path"><code>{html.escape(path_text(path))}</code>'
                f"<span>{html.escape(_summary(get_path(spec, path)))}</span></div>",
                margin=0,
                align="center",
                sizing_mode="stretch_width",
                stylesheets=[_UNSHOWN_CSS],
            )
            rows.append(
                pn.Row(
                    label,
                    link,
                    margin=(1, 0),
                    sizing_mode="stretch_width",
                    align="center",
                    css_classes=["fd-spec-unshown-row"],
                )
            )
        self.unshown.param.update(objects=rows, visible=bool(rows))

    def _reveal_clicked(self, path: tuple[str, ...], _event):
        self.reveal(path)

    def _parse(self, text: str) -> tuple[dict, t.Any]:
        if self.syntax == "yaml":
            return parse_spec_yaml(text)
        return parse_spec(text), None

    def _apply_text(self, text: str) -> bool:
        """Take the spec *text* holds, keeping the text as typed, or say why it is not a spec."""
        try:
            spec, doc = self._parse(text)
        except SpecTextError as exc:
            self.error = str(exc)
            self.status.object = f"Not applied. {exc}"
            self.editor.annotations = [
                {"row": exc.line - 1, "column": exc.column - 1, "type": "error", "text": str(exc)}
            ]
            self._sync_form()
            self._sync_unsent()
            return False
        # Worked out before any state changes, so the editor is never left half-applied.
        spans = text_spans(text)
        if self.syntax == "yaml":
            self._doc = doc
        self._spans = spans
        self._applied = text
        self.error = None
        if _differs(spec, self.value or {}):
            self._from_text = True
            try:
                self.value = spec
            finally:
                self._from_text = False
        else:
            self._mark(spec, [])
            self._sync_form()
        self._sync_unsent()
        return True

    def _edited(self, event):
        if self._syncing:
            return
        self._pauses += 1
        self._apply_text(event.new or "")

    def _sync_unsent(self):
        unsent = self.editor.value_input != self._applied
        if unsent != self.unsent:
            self.unsent = unsent

    def _typing(self, *_events):
        if self._syncing:
            return
        self._sync_unsent()
        if not self.unsent and self.error is not None:
            # Typed back to the text applied last, which parses.
            self._apply_text(self._applied)
        elif self.unsent and self.live:
            self._apply_later()

    def _apply_later(self):
        """Apply the text once typing pauses, if it parses by then."""
        self._pauses += 1
        pause = self._pauses

        async def later():
            await asyncio.sleep(APPLY_DELAY)
            if pause == self._pauses and self.live and self.editor.value_input != self._applied:
                self._apply_text(self.editor.value_input)

        param.parameterized.async_executor(later)

    def _disabled_set(self, *_events):
        self.editor.readonly = self.disabled
        self._sync_form()


def _differs(a: t.Any, b: t.Any) -> bool:
    """Whether two specs differ, in a value or in the order of keys at any depth."""
    return json.dumps(a, default=str) != json.dumps(b, default=str)


def _summary(value: t.Any) -> str:
    """Describe a value in a few words, e.g. ``3 keys`` or ``-45``."""
    if isinstance(value, dict):
        return f"{len(value)} key{'s' if len(value) != 1 else ''}"
    if isinstance(value, list):
        return f"{len(value)} item{'s' if len(value) != 1 else ''}"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 32 else f"{text[:31]}…"


def _rows_of(span: tuple[int, int] | None, first: bool = False) -> range:
    if span is None:
        return range(0)
    start, end = span
    return range(start, start + 1 if first else end + 1)
