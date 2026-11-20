"""The code panel docked under the canvas: a node's code field beside the node's result.

The editor opens it for one code field of one node, see
:meth:`~panel_flowdash.editor.FlowDash.open_code_panel`. The text typed is a
draft until Run (or Ctrl+Enter) applies it, together with the node's other
config fields shown in the panel's header, as one edit. With "Run on change"
on, a pause in typing runs it too. A component with a ``check_config`` has the
draft checked first, see :class:`~panel_flowdash.ui.config_draft.ConfigDraft`:
a draft that fails stays in the editor with the problems marked on their
lines, and the result keeps showing the node's last good output.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import time
import typing as t
import weakref

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewer
from panel.widgets import CodeEditor

from panel_flowdash.config_check import CheckResult
from panel_flowdash.config_choices import code_fields, node_completions
from panel_flowdash.dataflow_engine import failure_text
from panel_flowdash.spec_settings import declared_settings
from panel_flowdash.ui.choice_widgets import form_widgets
from panel_flowdash.ui.code_field import ACE_OPTIONS, AceTools, code_language
from panel_flowdash.ui.config_draft import ConfigDraft
from panel_flowdash.ui.node_preview import summarize_value
from panel_flowdash.ui.text import field_label, text_alert
from panel_flowdash.util import is_failure

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec
    from panel_flowdash.editor import FlowDash

#: Rows of a DataFrame result shown at most.
MAX_ROWS = 500
#: Seconds of no typing after which "Run on change" runs the draft.
RUN_DELAY = 0.8
HEADER_HEIGHT = 48
_EDITOR_CLASS = "fd-code-panel-editor"

_HEADER_CSS = """
:host { min-width: 0; }
.fd-cp-title { display: flex; align-items: center; gap: 8px; height: 100%; white-space: nowrap;
  overflow: hidden; }
.fd-cp-name { overflow: hidden; text-overflow: ellipsis; }
.fd-cp-icon { font-family: "Material Icons"; font-size: 18px; line-height: 1;
  font-feature-settings: "liga"; color: var(--mui-palette-text-secondary); }
.fd-cp-name { font-family: var(--fd-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-weight: 600; font-size: 14px; color: var(--mui-palette-text-primary); }
.fd-cp-kind { font-size: 12px; color: var(--mui-palette-text-secondary); }
.fd-cp-dot { width: 8px; height: 8px; border-radius: 50%; flex: none;
  background: var(--mui-palette-success-main, #2e7d32); }
.fd-cp-dot.error { background: var(--mui-palette-error-main, #d32f2f); }
.fd-cp-dot.pending { background: var(--mui-palette-warning-main, #ed6c02); }
"""

_BAR_CSS = """
:host { min-width: 0; }
.fd-cp-bar { display: flex; align-items: center; gap: 10px; height: 100%;
  font-size: 12px; color: var(--mui-palette-text-secondary); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.fd-cp-bar b { font-weight: 600; color: var(--mui-palette-text-primary); }
.fd-cp-ok { color: var(--mui-palette-success-main, #2e7d32); }
.fd-cp-error { color: var(--mui-palette-error-main, #d32f2f); }
.fd-cp-warn { color: var(--mui-palette-warning-main, #ed6c02); }
.fd-cp-icon { font-family: "Material Icons"; font-size: 15px; line-height: 1;
  font-feature-settings: "liga"; vertical-align: -3px; margin-right: 4px; }
"""

# A strip of controls, centred on one line, with a rule under it.
_STRIP_CSS = """
:host { align-items: center; flex-wrap: nowrap; padding: 0 0 0 16px; box-sizing: border-box;
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  background: var(--mui-palette-background-paper, #fff); }
:host { flex: none !important; }
:host(.fd-code-panel-header) { height: 48px !important; min-height: 48px;
  border-top: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
/* The controls on the right keep their size; the title and the fields give way. */
:host(.fd-code-panel-header) > * { flex: none; }
:host(.fd-code-panel-header) > .bk-Spacer { flex: 1 1 0; min-width: 0; width: auto !important; }
:host(.fd-code-panel-header) > .fd-cp-title-pane { flex: 0 1 auto; min-width: 60px; overflow: hidden; }
:host(.fd-code-panel-header) > .fd-code-panel-fields { flex: 0 1 auto; min-width: 0;
  overflow-x: clip; overflow-y: visible; }
:host(.fd-cp-footer) { border-bottom: none; padding: 0 12px;
  border-top: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
:host(.fd-cp-subheader) { padding: 0 12px; }
"""

_TABLE_CSS = """
:host { overflow: auto; height: 100%; }
table.panel-df { border-collapse: collapse; width: 100%; font-size: 12px; border: none !important;
  border-radius: 0 !important;
  font-variant-numeric: tabular-nums; color: var(--mui-palette-text-primary); }
.panel-df thead th { position: sticky; top: 0; z-index: 1; text-align: right; font-weight: 600;
  text-transform: none !important; letter-spacing: normal !important;
  color: var(--mui-palette-text-primary) !important;
  background: var(--mui-palette-background-paper, #fff);
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
.panel-df th, .panel-df td { white-space: nowrap; padding: 4px 12px !important; text-align: right;
  height: auto !important; line-height: 18px !important;
  border: none; border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.08)); }
.panel-df tbody th { color: var(--mui-palette-text-secondary); font-weight: 400; }
"""


def _bar(*parts: str) -> str:
    return f'<div class="fd-cp-bar">{"".join(parts)}</div>'


def _icon(name: str) -> str:
    return f'<span class="fd-cp-icon" aria-hidden="true">{name}</span>'


def _rows(frame: t.Any) -> int | None:
    try:
        return len(frame)
    except Exception:
        return None


def _utf16(line: str, column: int) -> int:
    """Return the 0-based *column* of *line*, counted in code points, in UTF-16 units as Ace counts."""
    return len(line[:column].encode("utf-16-le")) // 2


def _is_frame(value: t.Any) -> bool:
    return hasattr(value, "columns") and hasattr(value, "head") and hasattr(value, "shape")


class DockHandle(JSComponent):
    """A bar dragged to resize the layout holding it, which reports its new :attr:`height`.

    While dragging, the layout's height follows the pointer in the page;
    :attr:`height` is set once the pointer is released. Arrow keys resize it
    by 24 pixels when the bar has focus.
    """

    size = param.Integer(
        default=None, allow_None=True, doc="The height the bar resized its layout to."
    )

    min_size = param.Integer(default=160)

    max_ratio = param.Number(
        default=0.8, doc="The largest share of the parent's height it may take."
    )

    _stylesheets: t.ClassVar[list[str]] = [
        """
        :host { cursor: row-resize; }
        .fd-dock-handle { height: 7px; display: flex; align-items: center; justify-content: center;
          touch-action: none; outline: none; }
        .fd-dock-handle::after { content: ""; width: 32px; height: 3px; border-radius: 2px;
          background: var(--mui-palette-divider, rgba(0, 0, 0, 0.2)); }
        .fd-dock-handle:hover::after, .fd-dock-handle:focus-visible::after, .fd-dock-handle.dragging::after {
          background: var(--mui-palette-primary-main, #0072b5); }
        """
    ]

    _esm = """
    export function render({ el, model }) {
      const bar = document.createElement("div")
      bar.className = "fd-dock-handle"
      bar.tabIndex = 0
      bar.setAttribute("role", "separator")
      bar.setAttribute("aria-orientation", "horizontal")
      bar.setAttribute("aria-label", "Resize the code panel")
      el.appendChild(bar)
      function dock() {
        const host = el.getRootNode().host
        return host && host.getRootNode().host
      }
      function clamp(height, target) {
        const parent = target.parentElement || (target.getRootNode() && target.getRootNode().host)
        const room = parent ? parent.getBoundingClientRect().height : window.innerHeight
        return Math.round(Math.max(model.min_size, Math.min(height, room * model.max_ratio)))
      }
      let start = null
      bar.addEventListener("pointerdown", (event) => {
        const target = dock()
        if (!target) return
        start = {y: event.clientY, height: target.getBoundingClientRect().height, target}
        bar.setPointerCapture(event.pointerId)
        bar.classList.add("dragging")
        event.preventDefault()
      })
      bar.addEventListener("pointermove", (event) => {
        if (!start) return
        const height = clamp(start.height + start.y - event.clientY, start.target)
        start.target.style.height = `${height}px`
        start.last = height
      })
      function end() {
        if (!start) return
        bar.classList.remove("dragging")
        if (start.last != null) model.size = start.last
        start = null
      }
      bar.addEventListener("pointerup", end)
      bar.addEventListener("pointercancel", end)
      bar.addEventListener("keydown", (event) => {
        const step = {ArrowUp: 24, ArrowDown: -24}[event.key]
        const target = dock()
        if (!step || !target) return
        event.preventDefault()
        const height = clamp(target.getBoundingClientRect().height + step, target)
        target.style.height = `${height}px`
        model.size = height
      })
    }
    """


class CodePanel(Viewer):
    """Edits one code field of one node beside the node's result, applying the draft on Run."""

    editor = param.Parameter(doc="The FlowDash editor holding the node.")

    instance_id = param.String(default=None, allow_None=True, doc="The node edited.")

    field = param.String(default=None, allow_None=True, doc="The code field edited.")

    run_on_change = param.Boolean(
        default=False, doc="Whether a pause in typing runs the draft, without pressing Run."
    )

    collapsed = param.Boolean(default=False, doc="Whether only the header is shown.")

    dirty = param.Boolean(default=False, doc="Whether the draft differs from the node's config.")

    on_close = param.Callable(default=None, doc="Called when the panel's close button is pressed.")

    def __init__(self, **params):
        super().__init__(**params)
        self._editor_ref = weakref.ref(self.editor)
        self._releases: list[t.Callable[[], None]] = []
        self._draft: ConfigDraft | None = None
        self._fields_state: param.Parameterized | None = None
        self._others: list[str] = []
        self._runs = 0
        self._typing = 0
        self._started: float | None = None
        self._ran_at: dt.datetime | None = None
        self._took: float | None = None
        self._result = CheckResult()
        self._failed = False
        # The last output value that was not a failure, per output, shown dimmed after a failed run.
        self._good: dict[str, t.Any] = {}
        self._syncing = False
        # Drafts not run yet, per node and field, kept when the panel shows another field or closes.
        self._held: dict[tuple[str, str], dict[str, t.Any]] = {}
        self._running = False
        self._run_again = False

        self._title = pn.pane.HTML(
            "",
            margin=0,
            align="center",
            stylesheets=[_HEADER_CSS],
            css_classes=["fd-cp-title-pane"],
        )
        self._fields = pn.Row(
            margin=(0, 0, 0, 16), align="center", css_classes=["fd-code-panel-fields"]
        )
        self.run_switch = pmui.Switch(
            label="Run on change",
            value=self.param.run_on_change,
            margin=(0, 8),
            size="small",
            align="center",
            sx={"& .MuiFormControlLabel-label": {"fontSize": "0.8125rem"}},
            css_classes=["fd-code-run-on-change"],
        )
        self.run_switch.link(self, value="run_on_change", bidirectional=True)
        self.run_button = pmui.Button(
            label="Run",
            icon="play_arrow",
            variant="contained",
            size="small",
            margin=(0, 4),
            align="center",
            description="Run the draft (Ctrl+Enter)",
            css_classes=["fd-code-run"],
            sx={"textTransform": "none"},
        )
        self.run_button.on_click(lambda _event: self.run())
        self.discard_button = pmui.IconButton(
            icon="undo",
            description="Discard the draft",
            size="small",
            margin=(0, 2),
            align="center",
            visible=False,
            css_classes=["fd-code-discard"],
        )
        self.discard_button.on_click(lambda _event: self.discard())
        self.collapse_button = pmui.IconButton(
            icon="expand_more",
            description="Collapse the code panel",
            size="small",
            margin=(0, 2),
            align="center",
            css_classes=["fd-code-collapse"],
        )
        self.collapse_button.on_click(lambda _event: setattr(self, "collapsed", True))
        self.expand_button = pmui.IconButton(
            icon="expand_less",
            description="Expand the code panel",
            size="small",
            margin=(0, 2),
            align="center",
            visible=False,
            css_classes=["fd-code-expand-panel"],
        )
        self.expand_button.on_click(lambda _event: setattr(self, "collapsed", False))
        self.close_button = pmui.IconButton(
            icon="close",
            description="Close the code panel",
            size="small",
            margin=(0, 8, 0, 2),
            align="center",
            css_classes=["fd-code-close"],
        )
        self.close_button.on_click(lambda _event: self.on_close and self.on_close())
        self._header = pn.Row(
            self._title,
            self._fields,
            pn.layout.HSpacer(),
            self.discard_button,
            self.run_switch,
            self.run_button,
            self.collapse_button,
            self.expand_button,
            self.close_button,
            height=HEADER_HEIGHT,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-code-panel-header"],
        )

        self.code = CodeEditor(
            value="",
            value_input="",
            language="text",
            on_keyup=False,
            sizing_mode="stretch_both",
            min_height=80,
            margin=0,
            css_classes=[_EDITOR_CLASS],
        )
        self.code.param.watch(self._typed, "value_input")
        self.tools = AceTools(
            target_class=_EDITOR_CLASS, options=ACE_OPTIONS, on_run=self.run, margin=0
        )
        self._problems = pn.Column(margin=0, sizing_mode="stretch_width")
        self._code_status = pn.pane.HTML(
            "", margin=0, sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        self._language = pn.pane.HTML("", margin=(0, 12, 0, 12), stylesheets=[_BAR_CSS])
        code_footer = pn.Row(
            self._code_status,
            self.tools,
            self._language,
            height=32,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-footer", "fd-code-footer-bar"],
        )
        self._code_header = pn.pane.HTML(
            "", margin=0, sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        code_header = pn.Row(
            self._code_header,
            height=36,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-subheader"],
        )
        self._code_column = pn.Column(
            code_header,
            self._problems,
            self.code,
            code_footer,
            margin=0,
            sizing_mode="stretch_both",
            styles={
                "border-right": "1px solid var(--mui-palette-divider)",
                "min-width": "0",
                "overflow": "hidden",
            },
            css_classes=["fd-code-panel-code"],
        )

        self.outputs = pmui.RadioButtonGroup(
            options=[], size="small", margin=(0, 8, 0, 0), css_classes=["fd-code-outputs"]
        )
        self.outputs.param.watch(lambda _event: self._show_result(), "value")
        self._summary = pn.pane.HTML(
            "", margin=0, sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        result_header = pn.Row(
            self.outputs,
            self._summary,
            height=36,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-subheader"],
        )
        self._result_body = pn.Column(
            margin=0, sizing_mode="stretch_both", scroll="y-auto", css_classes=["fd-code-result"]
        )
        self._result_footer = pn.pane.HTML(
            "", margin=0, sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        result_footer = pn.Row(
            self._result_footer,
            height=32,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-footer"],
        )
        self._result_column = pn.Column(
            result_header,
            self._result_body,
            result_footer,
            margin=0,
            sizing_mode="stretch_both",
            styles={"min-width": "0", "overflow": "hidden"},
            css_classes=["fd-code-panel-result"],
        )
        self._body = pn.Row(
            self._code_column,
            self._result_column,
            margin=0,
            sizing_mode="stretch_both",
            visible=not self.collapsed,
            css_classes=["fd-code-panel-body"],
        )
        self._layout = pmui.Column(
            self._header,
            self._body,
            margin=0,
            sizing_mode="stretch_both",
            sx={"bgcolor": "background.paper"},
            css_classes=["fd-code-panel"],
        )
        self.param.watch(self._sync_collapsed, "collapsed")
        self.param.watch(self._sync_run_button, ["dirty", "run_on_change"])
        self._sync_collapsed()
        self._sync_run_button()

    # ------------------------------------------------------------------
    # Opening and closing
    # ------------------------------------------------------------------

    @property
    def _flowdash(self) -> FlowDash | None:
        return self._editor_ref()

    def _spec(self) -> ComponentSpec | None:
        editor = self._flowdash
        if editor is None or self.instance_id is None:
            return None
        item = next((i for i in editor._tile_items if i["instance_id"] == self.instance_id), None)
        return editor._component_specs.get(item["component_id"]) if item else None

    def can_edit(self, instance_id: str, field: str) -> bool:
        """Whether *field* of the node *instance_id* is a code field the panel can edit."""
        editor = self._flowdash
        if editor is None:
            return False
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        if item is None or "placeholder" in item:
            return False
        spec = editor._component_specs.get(item["component_id"])
        state = editor._dataflow_graph.get_config_state(instance_id)
        return spec is not None and state is not None and field in code_fields(spec)

    @pn.io.hold()
    def open(self, instance_id: str, field: str):
        """Edit *field* of the node *instance_id*, keeping the draft if it is already shown."""
        if (instance_id, field) == (self.instance_id, self.field):
            self.collapsed = False
            self.tools.focus += 1
            return
        self.release()
        self.param.update(instance_id=instance_id, field=field, collapsed=False)
        editor = self._flowdash
        spec = self._spec()
        state = editor._dataflow_graph.get_config_state(instance_id)
        self._good = {}
        self._result = CheckResult()
        self._failed = False
        self._ran_at = self._took = None
        parameter = state.param[field]
        language = code_language(parameter) or "text"
        text = getattr(state, field) or ""
        self._syncing = True
        try:
            self.code.param.update(language=language, value=text, value_input=text)
        finally:
            self._syncing = False
        self._language.object = _bar(html.escape(language.upper()))
        self._code_header.object = self._code_header_html(spec, parameter)
        self._build_fields(state, spec)
        held = self._held.pop((instance_id, field), None)
        if held:
            self._restore(held)
        watcher = state.param.watch(self._config_changed, [field, *self._others])
        self._releases.append(lambda: state.param.unwatch(watcher))
        node = editor._dataflow_graph.get_state(instance_id)
        outputs = [p.name for p in spec.outputs if node is not None and p.name in node.param]
        if node is not None and outputs:
            out_watcher = node.param.watch(lambda *_e: self._show_result(), outputs)
            self._releases.append(lambda: node.param.unwatch(out_watcher))
        inputs = [p.name for p in spec.inputs if node is not None and p.name in node.param]
        if node is not None and inputs:
            in_watcher = node.param.watch(lambda *_e: self._complete(), inputs)
            self._releases.append(lambda: node.param.unwatch(in_watcher))
        self.outputs.param.update(options=outputs, value=self._default_output(node, outputs))
        self.outputs.visible = len(outputs) > 1
        self._title.object = self._title_html(spec)
        if spec.check_config is not None:
            inspector = editor._inspector
            draft = ConfigDraft(
                editor, instance_id, inspector._check_runs, inspector._check_results
            )
            draft.on_result = self._checked
            self._draft = draft
            draft.show_current()
        self._sync_dirty()
        self._show_status()
        self._show_result()
        self._complete()
        self.tools.focus += 1

    def _default_output(self, node: param.Parameterized | None, outputs: list[str]) -> str | None:
        """Return the output to show first: a table before a lazy value, e.g. a query's pipeline."""
        for name in outputs:
            if _is_frame(getattr(node, name, None)):
                return name
        return outputs[0] if outputs else None

    def _restore(self, held: dict[str, t.Any]):
        self._syncing = True
        try:
            if self.field in held:
                text = held[self.field]
                self.code.param.update(value=text, value_input=text)
            if self._fields_state is not None:
                self._fields_state.param.update(
                    {k: v for k, v in held.items() if k in self._others}
                )
        finally:
            self._syncing = False

    @property
    def has_drafts(self) -> bool:
        """Whether an edit typed in the panel has not been run, shown or held for later."""
        return self.dirty or bool(self._held)

    def drop_all_drafts(self):
        """Forget every draft, the one shown included."""
        self._held.clear()
        if self.instance_id is not None:
            self.discard()
        self.param.trigger("dirty")

    def drop_drafts(self, instance_id: str):
        """Forget the drafts held for the node *instance_id*, e.g. once it is removed."""
        for key in [k for k in self._held if k[0] == instance_id]:
            del self._held[key]
        self.param.trigger("dirty")

    def discard(self):
        """Put the node's config back in the editor and the header, dropping the draft."""
        editor = self._flowdash
        if editor is None or self.instance_id is None:
            return
        state = editor._dataflow_graph.get_config_state(self.instance_id)
        self._restore(
            {self.field: getattr(state, self.field) or ""}
            | {n: getattr(state, n) for n in self._others}
        )
        self._typing += 1
        self._sync_dirty()

    def release(self):
        """Stop following the node shown, e.g. before closing the panel, holding an unrun draft."""
        if self.instance_id is not None and self.field is not None:
            patch = self._patch()
            if patch:
                self._held[(self.instance_id, self.field)] = patch
        self._typing += 1
        for release in self._releases:
            release()
        self._releases = []
        if self._draft is not None:
            self._draft.on_result = None
        self._draft = None
        self._runs += 1
        self.param.update(instance_id=None, field=None, dirty=False)
        self.param.trigger("dirty")
        self.tools.param.update(diagnostics=[], completions=[])
        self.code.annotations = []

    def _build_fields(self, state: param.Parameterized, spec: ComponentSpec):
        """Show the node's other config fields in the header, as a draft applied on Run."""
        codes = set(code_fields(spec))
        # Spec settings get an editor of their own, too large for the header.
        names = [
            n
            for n in state.param
            if n != "name" and n not in codes and declared_settings(state.param[n]) is None
        ]
        self._others = names
        if not names:
            self._fields_state = None
            self._fields[:] = []
            return
        self._fields_state = draft = type(state)(**{n: getattr(state, n) for n in names})
        editor = self._flowdash
        choices = editor._inspector._choices.get(self.instance_id, {}) if editor else {}
        widgets = form_widgets(draft, names, choices)
        form = pn.Param(
            draft,
            parameters=names,
            widgets=widgets or None,
            show_name=False,
            expand_button=False,
            default_layout=pn.Row,
            margin=0,
        )
        for widget in getattr(form, "_widgets", {}).values():
            update = {
                "width": 150,
                "sizing_mode": "fixed",
                "margin": (0, 8, 0, 0),
                "align": "center",
            }
            if "size" in widget.param:
                update["size"] = "small"
            if "sx" in widget.param:
                # As tall as the Run button, so the label clears the top of the strip.
                update["sx"] = {
                    **(widget.sx or {}),
                    "& .MuiInputBase-input": {"py": "5px"},
                    "& .MuiSelect-select": {"py": "5px"},
                }
            widget.param.update(update)
        self._fields[:] = [form]
        watcher = draft.param.watch(self._field_edited, names)
        self._releases.append(lambda: draft.param.unwatch(watcher))

    def _code_header_html(self, spec: ComponentSpec, parameter: param.Parameter) -> str:
        """Name the field and where the node's inputs come from."""
        editor = self._flowdash
        sources = []
        for edge in editor._dataflow_graph.edges if editor else []:
            if edge["target"] == self.instance_id:
                sources.append(f"{editor._node_name(edge['source'])}.{edge['source_port']}")
        label = field_label(parameter) or self.field
        origin = f"from {', '.join(sources)}" if sources else ""
        return _bar(f"<b>{html.escape(label)}</b>", html.escape(origin))

    def _title_html(self, spec: ComponentSpec) -> str:
        editor = self._flowdash
        name = editor._node_name(self.instance_id) if editor else self.instance_id
        icon = html.escape(spec.icon or "code") if spec.icon and " " not in spec.icon else "code"
        status = "error" if self._failed or self._result else ""
        return (
            '<div class="fd-cp-title">'
            f'<span class="fd-cp-icon" aria-hidden="true">{icon}</span>'
            f'<span class="fd-cp-name">{html.escape(name)}</span>'
            f'<span class="fd-cp-kind">{html.escape(spec.title)}</span>'
            f'<span class="fd-cp-dot {status}" title="{"Failing" if status else "Up to date"}"></span>'
            "</div>"
        )

    def _sync_collapsed(self, *_events):
        self._body.visible = not self.collapsed
        self.collapse_button.visible = not self.collapsed
        self.expand_button.visible = self.collapsed

    def _sync_run_button(self, *_events):
        self.run_button.variant = (
            "contained" if self.dirty or not self.run_on_change else "outlined"
        )
        self.discard_button.visible = self.dirty

    # ------------------------------------------------------------------
    # The draft
    # ------------------------------------------------------------------

    def _patch(self) -> dict[str, t.Any]:
        """Return the draft's changes to the node's config."""
        editor = self._flowdash
        if editor is None or self.instance_id is None:
            return {}
        state = editor._dataflow_graph.get_config_state(self.instance_id)
        if state is None:
            return {}
        patch: dict[str, t.Any] = {}
        text = self.code.value_input
        if text != (getattr(state, self.field) or ""):
            patch[self.field] = text
        if self._fields_state is not None:
            for name in self._others:
                value = getattr(self._fields_state, name)
                try:
                    same = bool(value == getattr(state, name))
                except Exception:
                    same = False
                if not same:
                    patch[name] = value
        return patch

    def _sync_dirty(self):
        self.dirty = bool(self._patch())
        self._show_result_footer()

    def _typed(self, _event):
        if self._syncing or self.instance_id is None:
            return
        self._sync_dirty()
        self._run_later()

    def _field_edited(self, *_events):
        if self._syncing:
            return
        self._sync_dirty()
        self._run_later()

    def _run_later(self):
        """With "Run on change", run the draft once typing pauses, or after a run in flight."""
        if not (self.run_on_change and self.dirty):
            return
        self._typing += 1
        typing = self._typing

        async def later():
            await asyncio.sleep(RUN_DELAY)
            if typing != self._typing:
                return
            if self._running:
                self._run_again = True
            else:
                self.run()

        param.parameterized.async_executor(later)

    def _config_changed(self, *events):
        """Follow a change of the node's config made elsewhere, unless the draft holds an edit of it."""
        state = events[0].obj
        for event in events:
            if event.name == self.field:
                pending = self.code.value_input != (event.old or "")
                if not pending:
                    self._syncing = True
                    try:
                        text = event.new or ""
                        self.code.param.update(value=text, value_input=text)
                    finally:
                        self._syncing = False
            elif self._fields_state is not None and event.name in self._others:
                try:
                    pending = not bool(getattr(self._fields_state, event.name) == event.old)
                except Exception:
                    pending = True
                if not pending:
                    self._fields_state.param.update({event.name: getattr(state, event.name)})
        self._sync_dirty()

    def run(self, text: str | None = None):
        """Apply the draft to the node, after its check passes if the component has one.

        *text* is the editor's text as sent with Ctrl+Enter, which may be
        ahead of what the server has.
        """
        editor = self._flowdash
        if editor is None or self.instance_id is None or not editor.editable:
            return
        if text is not None and text != self.code.value_input:
            self._syncing = True
            try:
                self.code.value_input = text
            finally:
                self._syncing = False
        self._typing += 1
        self._close_completions()
        patch = self._patch()
        self._runs += 1
        self._running = True
        self._run_again = False
        self._started = time.perf_counter()
        self._show_result_footer(running=True)
        if self._draft is not None:
            self._draft.propose(patch)
            return
        instance_id = self.instance_id

        async def apply():
            # A turn of the loop first, so "Running…" reaches the page before a slow refresh.
            await asyncio.sleep(0)
            if patch and self.instance_id == instance_id:
                editor._apply_config_patch(instance_id, patch)
            self._ran(CheckResult())

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if patch:
                editor._apply_config_patch(instance_id, patch)
            self._ran(CheckResult())
            return
        param.parameterized.async_executor(apply)

    def _close_completions(self):
        self.tools.closing += 1

    def _checked(self, result: CheckResult, pending: dict[str, t.Any]):
        """Show what the check of the draft, or of the node's config on opening, found."""
        if self._started is None:
            self._result = result
            self._show_status()
            return
        if result:
            self._ran(result)
            return
        # The draft calls this before it applies a passing patch, so show the result after.
        try:
            asyncio.get_running_loop().call_soon(self._ran, result)
        except RuntimeError:
            self._ran(result)

    def _ran(self, result: CheckResult):
        took = time.perf_counter() - self._started if self._started is not None else None
        self._started = None
        self._running = False
        self._result = result
        if not result:
            self._ran_at = dt.datetime.now()
            self._took = took
        self._sync_dirty()
        self._show_status()
        self._show_result()
        if self._run_again:
            self._run_again = False
            self._run_later()

    # ------------------------------------------------------------------
    # Problems and completions
    # ------------------------------------------------------------------

    def _show_status(self):
        result = self._result
        positioned = result.diagnostics(self.field) if self.field else []
        text_lines = self.code.value_input.splitlines() or [""]
        diagnostics = []
        for d in positioned:
            if not d.line:
                continue
            row = min(d.line, len(text_lines)) - 1
            line = text_lines[row]
            diagnostics.append(
                {
                    "row": row,
                    "column": _utf16(line, d.column - 1) if d.column else None,
                    "end_column": _utf16(line, d.end_column - 1) if d.end_column else None,
                    "text": str(d),
                    "suggestion": d.suggestion,
                }
            )
        self.tools.diagnostics = diagnostics
        self.code.annotations = [
            {"row": d["row"], "column": d["column"] or 0, "text": d["text"], "type": "error"}
            for d in diagnostics
        ]
        shown = {id(d) for d in positioned if d.line}
        others = [p for p in result if id(p) not in shown]
        alerts = []
        if others:
            alerts.append(
                text_alert(
                    "\n\n".join(others),
                    severity="error",
                    margin=(8, 12),
                    sizing_mode="stretch_width",
                    css_classes=["fd-code-problems"],
                )
            )
        if result.warnings:
            alerts.append(
                text_alert(
                    "\n\n".join(result.warnings),
                    severity="warning",
                    margin=(8, 12),
                    sizing_mode="stretch_width",
                    css_classes=["fd-code-warnings"],
                )
            )
        self._problems[:] = alerts
        if result:
            count = len(result)
            status = (
                f'<span class="fd-cp-error">{_icon("error")}{count} '
                f"problem{'s' if count != 1 else ''}</span>"
            )
        elif result.warnings:
            status = f'<span class="fd-cp-warn">{_icon("warning")}Not verified</span>'
        elif self._draft is not None:
            status = f'<span class="fd-cp-ok">{_icon("check_circle")}Check passed</span>'
        else:
            status = ""
        self._code_status.object = _bar(status)
        spec = self._spec()
        if spec is not None:
            self._title.object = self._title_html(spec)

    def _complete(self):
        editor = self._flowdash
        instance_id, field = self.instance_id, self.field
        if editor is None or instance_id is None:
            return

        async def fetch():
            try:
                words = await node_completions(editor, instance_id)
            except Exception:
                return
            if (self.instance_id, self.field) != (instance_id, field):
                return
            choices = words.get(field)
            if choices is None:
                self.tools.completions = []
                return
            types = choices.types or {}
            self.tools.completions = [
                {"value": str(value), "meta": types.get(value, "")} for value in choices.values()
            ]

        param.parameterized.async_executor(fetch)

    # ------------------------------------------------------------------
    # The result
    # ------------------------------------------------------------------

    def _output_value(self) -> tuple[str | None, t.Any]:
        editor = self._flowdash
        if editor is None or self.instance_id is None:
            return None, None
        node = editor._dataflow_graph.get_state(self.instance_id)
        name = self.outputs.value
        if node is None or name is None:
            return name, None
        return name, getattr(node, name, None)

    def _show_result(self):
        editor = self._flowdash
        if editor is None or self.instance_id is None:
            self._result_body[:] = []
            return
        spec = self._spec()
        if spec is not None and not spec.outputs:
            self._show_view(editor)
            return
        name, value = self._output_value()
        error = editor._output_failures.get(self.instance_id, {}).get(name)
        failed = is_failure(value) or error is not None
        self._failed = failed
        if not is_failure(value) and value is not None:
            self._good[name] = value
        shown = self._good.get(name) if failed else value
        objects = []
        if failed:
            objects.append(
                text_alert(
                    failure_text(value)
                    if is_failure(value)
                    else f"{type(error).__name__}: {error}",
                    severity="error",
                    title="The last run failed, showing the last good result",
                    margin=(8, 12),
                    sizing_mode="stretch_width",
                    css_classes=["fd-code-run-error"],
                )
            )
        view = self._value_view(editor, shown)
        if failed or self._result:
            view.styles = {**(view.styles or {}), "opacity": "0.55"}
            view.css_classes = [*view.css_classes, "fd-code-result-stale"]
        objects.append(view)
        self._result_body[:] = objects
        self._summary.object = _bar(self._summary_text(shown))
        self._show_result_footer()
        if spec is not None:
            self._title.object = self._title_html(spec)

    def _show_view(self, editor: FlowDash):
        """Show a node without outputs, a view, unless the tile grid shows it already."""
        instance = editor._component_instances.get(self.instance_id)
        if instance is None:
            self._result_body[:] = []
        elif editor._grid_on_screen:
            self._result_body[:] = [
                pn.pane.HTML(
                    _bar("The view is shown in the dashboard pane."),
                    margin=(8, 12),
                    stylesheets=[_BAR_CSS],
                )
            ]
        else:
            self._result_body[:] = [instance]
        self._summary.object = _bar(self._ran_text())
        self._show_result_footer()

    def _value_view(self, editor: FlowDash, value: t.Any) -> pn.viewable.Viewable:
        if value is None:
            return pn.pane.HTML(_bar("No result yet."), margin=(8, 12), stylesheets=[_BAR_CSS])
        if _is_frame(value):
            return pn.pane.DataFrame(
                value.head(MAX_ROWS),
                sizing_mode="stretch_both",
                margin=0,
                stylesheets=[_TABLE_CSS],
                css_classes=["fd-code-table"],
            )
        return pn.Column(editor._value_preview(value), margin=(8, 12), sizing_mode="stretch_width")

    def _ran_text(self) -> str:
        parts = []
        if self._took is not None:
            parts.append(f"{self._took * 1000:,.0f} ms")
        if self._ran_at is not None:
            parts.append(f"ran {self._ran_at:%H:%M}")
        return " · ".join(parts)

    def _summary_text(self, value: t.Any) -> str:
        parts = []
        if value is not None:
            if _is_frame(value):
                rows, columns = value.shape[0], value.shape[1]
                parts.append(
                    f"{rows:,} row{'s' if rows != 1 else ''} &times; {columns} column{'s' if columns != 1 else ''}"
                )
            else:
                parts.append(html.escape(summarize_value(value)))
        if ran := self._ran_text():
            parts.append(ran)
        return " · ".join(parts)

    def _show_result_footer(self, running: bool = False):
        if running:
            text = "Running…"
        elif self._result:
            text = '<span class="fd-cp-error">Not run, the check failed</span>'
        elif self.dirty:
            text = "Edited since the last run · Ctrl ↵ to run"
        else:
            _name, value = self._output_value()
            rows = _rows(value) if _is_frame(value) else None
            text = f"First {MAX_ROWS:,} of {rows:,} rows" if rows and rows > MAX_ROWS else ""
        self._result_footer.object = _bar(text)

    def __panel__(self):
        """Render the panel."""
        return self._layout
