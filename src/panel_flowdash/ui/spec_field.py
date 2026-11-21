"""A spec edited as a form over its curated settings and as JSON, kept in sync both ways.

The inspector uses :class:`SpecEditor` for a ``param.Dict`` that declares
settings, see :mod:`panel_flowdash.spec_settings`. Changing a control writes
its path into the spec, and the lines that changed are highlighted in the
JSON. Editing the JSON (applied on blur or Ctrl+Enter, like a code field)
reads every path back into the form. Keys no control covers are kept as
written, marked in the editor's gutter and listed above the form, and while
the text is not valid JSON the form is disabled and the spec keeps its last
valid value.

The parts are exposed separately (:attr:`SpecEditor.banner`,
:attr:`SpecEditor.form`, :attr:`SpecEditor.spec`) for a host laying them out
itself, e.g. beside a live preview.
"""

from __future__ import annotations

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
    parse_spec,
    path_text,
    shown_in_spec,
    unrepresented_paths,
)
from panel_flowdash.ui.code_field import ACE_OPTIONS, ACE_THEME_CSS, AceOptions
from panel_flowdash.ui.text import code_span

if t.TYPE_CHECKING:
    from panel_flowdash.config_choices import Choices

MODES = {"Form": "form", "Form + Spec": "both", "Spec": "spec"}

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


class SpecMarks(JSComponent):
    """Marks lines of the Ace editors beside it: rows only in the spec, and rows just changed.

    :attr:`only` rows get an amber bar in the gutter, :attr:`changed` rows a
    green bar and a green background, until the next change.
    """

    only = param.List(default=[])

    changed = param.List(default=[])

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

    export function render({ el, model }) {
      el.style.display = "none"
      const update = () => setTimeout(() => mark(el, model), 0)
      update()
      model.on(["only", "changed"], update)
    }
    """


def _css_name(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-")


class SpecEditor(CompositeWidget):
    """A spec dict edited through a form of its settings and as JSON text.

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
        Whether the form, the JSON or both are shown.
    layout
        ``"stacked"`` puts the JSON under the form, ``"columns"`` beside it.
    """

    value = param.Dict(default={})

    settings = param.List(default=[], item_type=SpecSetting)

    choices = param.Dict(default={})

    mode = param.Selector(default="both", objects=list(MODES.values()))

    layout = param.Selector(default="stacked", objects=["stacked", "columns"])

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
        self.banner = pn.Column(
            margin=0, sizing_mode="stretch_width", css_classes=["fd-spec-banner"]
        )
        self.form = pn.Column(margin=0, sizing_mode="stretch_width", css_classes=["fd-spec-form"])
        self.editor = CodeEditor(
            value="",
            language="json",
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
        self.form.visible = self.banner.visible = self.mode in ("form", "both") and bool(
            self.settings
        )
        self.spec.visible = self.mode in ("spec", "both") or not self.settings

    def _layout_set(self, *_events):
        self._mode_set()
        if self.layout == "columns":
            body = pn.Row(
                pn.Column(
                    self.banner, self.form, margin=(0, 12, 0, 0), sizing_mode="stretch_width"
                ),
                self.spec,
                margin=0,
                sizing_mode="stretch_width",
            )
            self._composite[:] = [self.header, body]
        else:
            self._composite[:] = [self.header, self.banner, self.form, self.spec]
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
        objects.append(self._notes)
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

    def load(self, value: dict | None):
        """Show *value* as a spec loaded afresh, e.g. another node's, rather than as an edit.

        Unlike setting :attr:`value`, no line is marked as changed, and text
        typed but not applied, valid or not, is dropped.
        """
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

    # The text

    def _value_set(self, event):
        if self._loading:
            return
        changed = changed_paths(event.old or {}, event.new or {})
        self._show_text(event.new, changed)
        self._sync_form()

    def _show_text(self, spec: dict | None, changed: list | None = None):
        text, spans = format_spec(spec or {})
        self._spans = spans
        if self.editor.value != text:
            self._syncing = True
            try:
                self.editor.param.update(value=text, value_input=text)
            finally:
                self._syncing = False
        self.error = None
        self.unsent = False
        self._mark(spec or {}, changed or [])

    def _mark(self, spec: dict, changed: list):
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
        if unknown:
            listed = ", ".join(code_span(path_text(p)) for p in unknown)
            self.banner[:] = [
                pmui.Alert(
                    object=f"Kept as written, no form control: {listed}",
                    severity="warning",
                    margin=(0, 0, 6, 0),
                    sizing_mode="stretch_width",
                    css_classes=["fd-spec-unknown"],
                )
            ]
        else:
            self.banner[:] = []
        self.status.object = ""

    def _edited(self, event):
        if self._syncing:
            return
        try:
            spec = parse_spec(event.new or "")
        except SpecTextError as exc:
            self.error = str(exc)
            self.status.object = f"Not applied. {exc}"
            self.editor.annotations = [
                {"row": exc.line - 1, "column": exc.column - 1, "type": "error", "text": str(exc)}
            ]
            self._sync_form()
            return
        self.error = None
        if spec != self.value or list(spec) != list(self.value or {}):
            self.value = spec
        else:
            self._show_text(spec)
            self._sync_form()

    def _typing(self, *_events):
        unsent = self.editor.value_input != self.editor.value
        if unsent != self.unsent:
            self.unsent = unsent

    def _disabled_set(self, *_events):
        self.editor.readonly = self.disabled
        self._sync_form()


def _rows_of(span: tuple[int, int] | None, first: bool = False) -> range:
    if span is None:
        return range(0)
    start, end = span
    return range(start, start + 1 if first else end + 1)
