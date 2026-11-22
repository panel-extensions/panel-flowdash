"""The code panel docked under the canvas, or to its right: a node's spec beside the node's result.

The editor opens it for one node, see
:meth:`~panel_flowdash.editor.FlowDash.open_code_panel`, and while it is open
it follows the node selected on the canvas. It edits the node's spec, what the
node does rather than its settings, which stay in the inspector: a code field,
e.g. a query, as text, or a dict declaring a spec form, e.g. a chart spec, as
a form of its settings, as YAML or as JSON, see
:class:`~panel_flowdash.ui.spec_field.SpecEditor`. The browser remembers the
view picked. A node without a spec shows its result only, beside what defines
it, read only, if its component has a ``__flowdash_definition__`` hook, see
:func:`~panel_flowdash.ui.node_preview.call_definition_hook`.

Format (Shift+Alt+F) lays out a spec's text, or code whose field or
language has a formatter, see :func:`~panel_flowdash.ui.code_field.register_formatter`,
as an edit of the draft that Ctrl+Z undoes.

The edit is a draft until Run (or Ctrl+Enter) applies it as one edit. With
"Run on change" on, the default for a spec, a form edit runs at once, spec
text once it parses and typing pauses, and code after a pause in typing. A
component with a ``check_config`` has the draft checked first, see
:class:`~panel_flowdash.ui.config_draft.ConfigDraft`: a draft that fails stays
in the editor with the problems marked on their lines, and the result keeps
showing the node's last good output. While the result does not match the
draft, or a run is in flight, the result is dimmed under a badge saying so.
Drafts not run are kept per node and field while the panel shows another
node or is closed.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import html
import logging
import time
import typing as t
import weakref
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import Child, JSComponent
from panel.viewable import Viewer
from panel.widgets import CodeEditor
from panel_splitjs import HSplit, VSplit

from panel_flowdash.config_check import CheckResult
from panel_flowdash.config_choices import node_choices, node_completions, spec_fields
from panel_flowdash.dataflow_engine import failure_text
from panel_flowdash.spec_settings import (
    SpecPathError,
    declared_settings,
    delete_path,
    format_spec,
    format_spec_yaml,
    parse_spec,
    parse_spec_yaml,
    set_path,
)
from panel_flowdash.ui.choice_widgets import setting_choices
from panel_flowdash.ui.code_field import (
    ACE_OPTIONS,
    ACE_THEME_CSS,
    AceTools,
    code_formatter,
    code_language,
)
from panel_flowdash.ui.config_draft import ConfigDraft
from panel_flowdash.ui.inspector import hooked_preview, value_preview, wants_preview_hook
from panel_flowdash.ui.node_preview import call_definition_hook, run_off_loop, summarize_value
from panel_flowdash.ui.spec_field import SpecEditor
from panel_flowdash.ui.split_view import GUTTER_CSS
from panel_flowdash.ui.text import field_label, text_alert
from panel_flowdash.util import is_failure
from panel_flowdash.value_text import error_text, safe_frame

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec
    from panel_flowdash.editor import FlowDash

#: Rows of a DataFrame result shown at most.
MAX_ROWS = 500
#: Seconds of no typing after which "Run on change" runs a code draft.
RUN_DELAY = 0.8
#: Seconds a run, or a draft "Run on change" is about to run, lasts before the result is marked stale.
STALE_DELAY = 0.3
#: Whether "Run on change" starts on, by how the field is edited.
RUN_ON_CHANGE = {"spec": True, "code": False}
HEADER_HEIGHT = 48
#: Widths of the code and the result in percent of the panel; a table result wants the room.
SPLIT_SIZES = (40.0, 60.0)
#: Narrowest the code or the result may be dragged, in pixels.
MIN_PANE_WIDTH = 240
#: Heights of the code and the result in percent of the panel docked to the right.
STACKED_SIZES = (50.0, 50.0)
#: Lowest the code or the result may be dragged in the panel docked to the right, in pixels.
MIN_PANE_HEIGHT = 140
FORMAT_SHORTCUT = "Shift+Alt+F"
_EDITOR_CLASS = "fd-code-panel-editor"
_SPEC_TEXT_CLASS = "fd-code-panel-spec-json"
#: The views of a spec, by label.
SPEC_VIEWS = {"Form": "form", "YAML": "yaml", "JSON": "json"}
#: The views of a spec, by label, for a spec without a form.
TEXT_VIEWS = {"YAML": "yaml", "JSON": "json"}
# Where a held draft keeps spec text that was typed but is not a valid spec yet.
_TEXT = object()
# Where a held draft keeps the field's value it was written against.
_BASE = object()
# Where a held draft keeps the syntax its spec text was typed in.
_SYNTAX = object()
_MISSING = object()
logger = logging.getLogger(__name__)

# The result option showing the node's own view rather than one of its outputs.
_VIEW = "__view__"

_HEADER_CSS = """
:host { min-width: 0; }
.fd-cp-title { display: flex; align-items: center; gap: 8px; height: 100%; white-space: nowrap;
  overflow: hidden; }
/* The component's name gives way before the node's. */
.fd-cp-name { overflow: hidden; text-overflow: ellipsis; flex: 0 0.01 auto; min-width: 48px; }
.fd-cp-kind { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
  font-size: 12px; line-height: 18px; color: var(--mui-palette-text-secondary); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.fd-cp-bar b { font-weight: 600; color: var(--mui-palette-text-primary); }
.fd-cp-ok { color: var(--mui-palette-success-main, #2e7d32); }
.fd-cp-error { color: var(--mui-palette-error-main, #d32f2f); }
.fd-cp-warn { color: var(--mui-palette-warning-main, #ed6c02); }
.fd-cp-ok, .fd-cp-error, .fd-cp-warn { display: inline-flex; align-items: center; gap: 4px; }
.fd-cp-icon { font-family: "Material Icons"; font-size: 16px; line-height: 1; width: 16px;
  font-feature-settings: "liga"; flex: none; }
.fd-cp-empty { display: flex; flex-direction: column; gap: 4px; padding: 16px; font-size: 13px;
  line-height: 20px; color: var(--mui-palette-text-secondary); white-space: normal; }
.fd-cp-empty b { font-weight: 600; color: var(--mui-palette-text-primary); }
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
/* In a narrow panel, e.g. docked to the right, the node's name keeps its room. */
:host(.fd-code-panel-header) > .fd-cp-title-pane { flex: 0 1 auto; min-width: min(150px, 35%); overflow: hidden; }
:host(.fd-cp-footer) { border-bottom: none; padding: 0 12px;
  border-top: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
:host(.fd-cp-subheader) { padding: 0 12px; }
"""

# The strip over a result that does not match the draft.
_STALE_CSS = """
:host(.fd-code-stale-badge) { padding: 0 12px; background: color-mix(in srgb, var(--mui-palette-warning-main, #ed6c02) 12%,
  var(--mui-palette-background-paper, #fff)); }
"""

_TABLE_CSS = """
:host { overflow: auto; height: 100%; }
/* Columns as wide as their content, scrolling sideways once they outgrow the pane. */
:host table.panel-df { border-collapse: collapse; width: max-content; min-width: 0; font-size: 12px; border: none !important;
  border-radius: 0 !important;
  font-variant-numeric: tabular-nums; color: var(--mui-palette-text-primary); }
.panel-df thead th { position: sticky; top: 0; z-index: 1; text-align: right; font-weight: 600;
  text-transform: none !important; letter-spacing: normal !important;
  color: var(--mui-palette-text-primary) !important;
  background: var(--mui-palette-background-paper, #fff);
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
.panel-df th, .panel-df td { white-space: nowrap; padding: 4px 12px !important; text-align: right;
  max-width: 360px; overflow: hidden; text-overflow: ellipsis;
  height: auto !important; line-height: 18px !important;
  border: none; border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.08)); }
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


def _shows_index(frame: t.Any) -> bool:
    """Whether the table shows *frame*'s index: not when it only numbers the rows.

    An unnamed integer index, the default one or what is left of it after
    filtering, carries no data; a named one, a MultiIndex or e.g. a
    DatetimeIndex holds group or time labels.
    """
    index = getattr(frame, "index", None)
    if index is None or getattr(index, "nlevels", 1) > 1 or index.name is not None:
        return index is not None
    return getattr(index.dtype, "kind", "") not in "iu"


def _output_label(port: t.Any) -> str:
    """Name an output in the toggle: its own label, or its name with the first letter raised."""
    label = port.label if port.label and port.label != port.name else port.name.replace("_", " ")
    return label[:1].upper() + label[1:]


class DockHandle(JSComponent):
    """A bar dragged to resize the layout holding it, which reports its new :attr:`size`.

    A horizontal bar sits on top of a layout docked under the page and sets
    its height; a vertical one sits on the left of a layout docked to the
    right and sets its width. While dragging, the layout follows the pointer
    in the page; :attr:`size` is set once the pointer is released. Arrow keys
    resize it by 24 pixels when the bar has focus.
    """

    size = param.Integer(
        default=None, allow_None=True, doc="The height or width the bar resized its layout to."
    )

    min_size = param.Integer(default=160)

    max_ratio = param.Number(
        default=0.8, doc="The largest share of the parent's height or width it may take."
    )

    orientation = param.Selector(
        default="horizontal",
        objects=["horizontal", "vertical"],
        doc="A horizontal bar resizes the height, a vertical one the width.",
    )

    _stylesheets: t.ClassVar[list[str]] = [
        """
        :host { cursor: row-resize; }
        :host(.fd-dock-vertical) { cursor: col-resize; }
        .fd-dock-handle { height: 7px; display: flex; align-items: center; justify-content: center;
          touch-action: none; outline: none; }
        .fd-dock-handle.vertical { height: 100%; width: 7px; }
        .fd-dock-handle::after { content: ""; width: 32px; height: 3px; border-radius: 2px;
          background: var(--mui-palette-divider, rgba(0, 0, 0, 0.2)); }
        .fd-dock-handle.vertical::after { width: 3px; height: 32px; }
        .fd-dock-handle:hover::after, .fd-dock-handle:focus-visible::after, .fd-dock-handle.dragging::after {
          background: var(--mui-palette-primary-main, #0072b5); }
        """
    ]

    _esm = """
    export function render({ el, model }) {
      const vertical = model.orientation === "vertical"
      const bar = document.createElement("div")
      bar.className = vertical ? "fd-dock-handle vertical" : "fd-dock-handle"
      bar.tabIndex = 0
      bar.setAttribute("role", "separator")
      // A separator's orientation is that of the line it draws, across the resize.
      bar.setAttribute("aria-orientation", vertical ? "vertical" : "horizontal")
      bar.setAttribute("aria-label", "Resize the code panel")
      el.appendChild(bar)
      function dock() {
        const host = el.getRootNode().host
        return host && host.getRootNode().host
      }
      function extent(rect) {
        return vertical ? rect.width : rect.height
      }
      function clamp(size, target) {
        const parent = target.parentElement || (target.getRootNode() && target.getRootNode().host)
        const room = parent ? extent(parent.getBoundingClientRect()) : (vertical ? window.innerWidth : window.innerHeight)
        return Math.round(Math.max(model.min_size, Math.min(size, room * model.max_ratio)))
      }
      function resize(target, size) {
        if (vertical) {
          target.style.width = `${size}px`
          target.style.minWidth = `${size}px`
        } else {
          target.style.height = `${size}px`
        }
      }
      let start = null
      bar.addEventListener("pointerdown", (event) => {
        const target = dock()
        if (!target) return
        start = {
          at: vertical ? event.clientX : event.clientY,
          size: extent(target.getBoundingClientRect()),
          target,
        }
        bar.setPointerCapture(event.pointerId)
        bar.classList.add("dragging")
        event.preventDefault()
      })
      bar.addEventListener("pointermove", (event) => {
        if (!start) return
        const at = vertical ? event.clientX : event.clientY
        const size = clamp(start.size + start.at - at, start.target)
        resize(start.target, size)
        start.last = size
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
        const keys = vertical ? {ArrowLeft: 24, ArrowRight: -24} : {ArrowUp: 24, ArrowDown: -24}
        const step = keys[event.key]
        const target = dock()
        if (!step || !target) return
        event.preventDefault()
        const size = clamp(extent(target.getBoundingClientRect()) + step, target)
        resize(target, size)
        model.size = size
      })
    }
    """

    def __init__(self, **params):
        super().__init__(**params)
        if self.orientation == "vertical":
            self.css_classes = [*self.css_classes, "fd-dock-vertical"]


class ViewFrame(JSComponent):
    """Holds a node's own view and tells it the size it gets.

    A chart sized to its container, e.g. a Vega-Lite spec with
    ``"container"`` dimensions, measures the container when it is embedded
    and again only on a window resize. Embedded before the code panel's
    result side is laid out, it keeps a height of zero, so the frame
    dispatches a window resize whenever its own size changes, as the tile
    grid and the splitter do.
    """

    object = Child()

    _esm = """
    export function render({ el, model }) {
      el.append(model.get_child("object"))
      let last = null
      let frame = null
      const observer = new ResizeObserver(([entry]) => {
        const { width, height } = entry.contentRect
        const size = `${Math.round(width)}x${Math.round(height)}`
        if (!width || !height || size === last) return
        last = size
        cancelAnimationFrame(frame)
        frame = requestAnimationFrame(() => window.dispatchEvent(new Event("resize")))
      })
      observer.observe(el)
      model.on("remove", () => {
        observer.disconnect()
        cancelAnimationFrame(frame)
      })
    }
    """


class CodePanel(Viewer):
    """Edits the spec of one node beside the node's result, applying the draft on Run."""

    editor = param.Parameter(doc="The FlowDash editor holding the node.")

    instance_id = param.String(default=None, allow_None=True, doc="The node shown.")

    field = param.String(
        default=None,
        allow_None=True,
        doc="The spec field edited, a code field or a spec dict; None for a node without one.",
    )

    run_on_change = param.Boolean(
        default=False,
        doc="""
        Whether an edit runs the draft without pressing Run, for the field
        shown. It starts on for a spec and off for code, see
        :data:`RUN_ON_CHANGE`, and is remembered for each while the panel lives.""",
    )

    running = param.Boolean(default=False, doc="Whether a run of the draft is in flight.")

    stale = param.Selector(
        default=None,
        objects=[None, "draft", "running"],
        doc="""
        Whether the result shown does not match the draft: a ``"draft"`` not
        run, or ``"running"``.""",
    )

    spec_view = param.Selector(
        default="form",
        objects=list(SPEC_VIEWS.values()),
        doc="The view of a spec: its form, or its text as YAML or JSON.",
    )

    spec_text = param.Selector(
        default="yaml",
        objects=list(TEXT_VIEWS.values()),
        doc="The syntax of a spec's text, the one last picked; a spec without a form shows it.",
    )

    collapsed = param.Boolean(default=False, doc="Whether only the header is shown.")

    dirty = param.Boolean(default=False, doc="Whether the draft differs from the node's config.")

    on_close = param.Callable(default=None, doc="Called when the panel's close button is pressed.")

    dock = param.Selector(
        default="bottom",
        objects=["bottom", "right"],
        doc="Where the panel is docked: under the canvas, or in a column to its right.",
    )

    can_dock_right = param.Boolean(
        default=True, doc="Whether the window is wide enough to offer docking to the right."
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._editor_ref = weakref.ref(self.editor)
        self._releases: list[t.Callable[[], None]] = []
        self._draft: ConfigDraft | None = None
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
        # The YAML documents of specs shown before, per node and field, whose comments are kept.
        self._yaml_docs: dict[tuple[str, str], t.Any] = {}
        self._run_on_change = dict(RUN_ON_CHANGE)
        self._run_again = False
        # Counts the stale states worked out, so a delayed one is not shown once outdated.
        self._stale_shown = 0
        self._format_error: str | None = None
        # Set while the panel edits the spec itself, e.g. formatting it, which "Run on change" does not run.
        self._hold_run = False
        # Counts the results shown, so a preview still loading for an earlier one is dropped.
        self._shown_results = 0

        self._title = pn.pane.HTML(
            "",
            margin=0,
            align="center",
            stylesheets=[_HEADER_CSS],
            css_classes=["fd-cp-title-pane"],
        )
        self.run_switch = pmui.Switch(
            label="Run on change",
            value=self.param.run_on_change,
            margin=(0, 8),
            size="small",
            align="center",
            # The sx styles the switch itself, so its label is styled here.
            stylesheets=[
                ".MuiFormControlLabel-label { font-size: 0.8125rem; white-space: nowrap; }"
            ],
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
        self.dock_button = pmui.IconButton(
            icon="view_sidebar",
            description="Dock to the right",
            size="small",
            margin=(0, 2),
            align="center",
            css_classes=["fd-code-dock-toggle"],
        )
        self.dock_button.on_click(lambda _event: self.toggle_dock())
        self._header = pn.Row(
            self._title,
            pn.layout.HSpacer(),
            self.discard_button,
            self.run_switch,
            self.run_button,
            self.dock_button,
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
            stylesheets=[ACE_THEME_CSS],
        )
        self.code.param.watch(self._typed, "value_input")
        self.tools = AceTools(
            target_class=_EDITOR_CLASS,
            options=ACE_OPTIONS,
            on_run=self.run,
            on_format=self.format,
            scroll_margin=8,
            margin=0,
            align="center",
        )
        # A spec dict is edited by a SpecEditor whose parts the panel lays out itself.
        self.spec_editor = SpecEditor(mode="form", syntax=self.spec_text, margin=0)
        self.spec_editor.editor.param.update(
            height=None,
            min_height=80,
            sizing_mode="stretch_both",
            styles={},
            css_classes=[*self.spec_editor.editor.css_classes, _SPEC_TEXT_CLASS],
        )
        self.spec_editor.spec.param.update(sizing_mode="stretch_both", styles={"min-height": "0"})
        self.spec_editor.status.margin = (4, 12)
        self.spec_editor.param.watch(self._spec_edited, "value")
        self.spec_editor.param.watch(self._spec_typed, ["unsent", "error"])
        self.spec_editor.param.watch(self._spec_mode_set, "mode")
        self.spec_tools = AceTools(
            target_class=_SPEC_TEXT_CLASS,
            options={**ACE_OPTIONS, "wrap": False, "tabSize": 2},
            on_run=self.run,
            on_format=self.format,
            scroll_margin=8,
            margin=0,
            align="center",
        )
        self.view_toggle = pmui.RadioButtonGroup(
            options=SPEC_VIEWS,
            value=self.spec_view,
            size="small",
            margin=(0, 12, 0, 0),
            align="center",
            sx={
                "& .MuiToggleButton-root": {
                    "py": "1px",
                    "px": "10px",
                    "fontSize": "0.75rem",
                    "lineHeight": "20px",
                    "textTransform": "none",
                }
            },
            css_classes=["fd-code-spec-view"],
        )
        self.view_toggle.param.watch(self._view_picked, "value")
        self._spec_form = pn.Column(
            self.spec_editor.form,
            margin=0,
            styles={"padding": "8px 16px", "box-sizing": "border-box", "min-height": "0"},
            sizing_mode="stretch_both",
            scroll="y-auto",
            css_classes=["fd-code-spec-form"],
        )
        self._spec_text = pn.Column(
            self.spec_editor.spec,
            margin=0,
            sizing_mode="stretch_both",
            styles={"min-height": "0"},
            css_classes=["fd-code-spec-text"],
        )
        self._empty = pn.pane.HTML(
            "",
            margin=0,
            sizing_mode="stretch_both",
            stylesheets=[_BAR_CSS],
            css_classes=["fd-code-empty"],
        )
        # What defines a node without code or a spec to edit, e.g. a source's spec, read only.
        self.definition = CodeEditor(
            value="",
            language="text",
            readonly=True,
            sizing_mode="stretch_both",
            min_height=80,
            margin=0,
            visible=False,
            css_classes=["fd-code-definition"],
            stylesheets=[ACE_THEME_CSS],
        )
        self._problems = pn.Column(margin=0, sizing_mode="stretch_width")
        # Why the form is disabled while the text does not parse, shown in the Form view.
        self.fix_json_button = pmui.Button(
            label="Fix in YAML",
            variant="text",
            size="small",
            margin=(0, 12, 4, 12),
            sx={"textTransform": "none"},
            css_classes=["fd-code-fix-json"],
        )
        self.fix_json_button.on_click(lambda _event: setattr(self, "spec_view", self.spec_text))
        self._json_error = pn.Column(
            margin=0,
            sizing_mode="stretch_width",
            visible=False,
            css_classes=["fd-code-json-error"],
        )
        # A held draft written against a value of the field that has changed since.
        self._stale: dict | None = None
        self.keep_draft_button = pmui.Button(
            label="Keep draft",
            variant="text",
            size="small",
            margin=(0, 4, 4, 12),
            sx={"textTransform": "none"},
            css_classes=["fd-code-keep-draft"],
        )
        self.keep_draft_button.on_click(lambda _event: self.keep_stale_draft())
        self.discard_stale_button = pmui.Button(
            label="Discard",
            variant="text",
            size="small",
            margin=(0, 4, 4, 0),
            sx={"textTransform": "none"},
            css_classes=["fd-code-discard-stale"],
        )
        self.discard_stale_button.on_click(lambda _event: self.discard_stale_draft())
        self._stale_notice = pn.Column(
            margin=0, sizing_mode="stretch_width", visible=False, css_classes=["fd-code-stale"]
        )
        # Picks the spec field shown, for a node with several.
        self.field_toggle = pmui.RadioButtonGroup(
            options={},
            size="small",
            margin=(0, 12, 0, 0),
            align="center",
            visible=False,
            # Many fields scroll sideways rather than push the header's other controls out.
            styles={"min-width": "0", "overflow-x": "auto", "flex": "0 1 auto"},
            sx={
                "flexWrap": "nowrap",
                "& .MuiToggleButton-root": {
                    "py": "1px",
                    "px": "10px",
                    "fontSize": "0.75rem",
                    "lineHeight": "20px",
                    "textTransform": "none",
                    "whiteSpace": "nowrap",
                },
            },
            css_classes=["fd-code-field-switch"],
        )
        self.field_toggle.param.watch(self._field_picked, "value")
        self._code_status = pn.pane.HTML(
            "", margin=0, align="center", sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        self._language = pn.pane.HTML(
            "", margin=(0, 0, 0, 12), align="center", stylesheets=[_BAR_CSS]
        )
        code_footer = pn.Row(
            self._code_status,
            self.tools,
            self.spec_tools,
            self._language,
            height=32,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-footer", "fd-code-footer-bar"],
        )
        self._code_header = pn.pane.HTML(
            "", margin=0, align="center", sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        self.format_button = pmui.Button(
            label="Format",
            icon="format_align_left",
            variant="text",
            size="small",
            margin=(0, 8, 0, 0),
            align="center",
            visible=False,
            description=f"Format the code ({FORMAT_SHORTCUT})",
            sx={"textTransform": "none", "py": "1px", "fontSize": "0.75rem"},
            css_classes=["fd-code-format"],
        )
        self.format_button.on_click(lambda _event: self.format())
        code_header = pn.Row(
            self._code_header,
            self.field_toggle,
            self.view_toggle,
            self.format_button,
            height=36,
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS],
            css_classes=["fd-cp-subheader"],
        )
        self._code_column = pn.Column(
            code_header,
            self._problems,
            self._stale_notice,
            self._json_error,
            self.code,
            self._spec_form,
            self._spec_text,
            self._empty,
            self.definition,
            code_footer,
            margin=0,
            sizing_mode="stretch_both",
            styles={"min-width": "0", "overflow": "hidden"},
            css_classes=["fd-code-panel-code"],
        )

        self.outputs = pmui.RadioButtonGroup(
            options=[],
            size="small",
            margin=(0, 12, 0, 0),
            align="center",
            # Small enough for the strip, so the summary sits beside it on the same line.
            sx={
                "& .MuiToggleButton-root": {
                    "py": "1px",
                    "px": "10px",
                    "fontSize": "0.75rem",
                    "lineHeight": "20px",
                    "textTransform": "none",
                }
            },
            css_classes=["fd-code-outputs"],
        )
        self.outputs.param.watch(lambda _event: self._show_result(), "value")
        self._summary = pn.pane.HTML(
            "", margin=0, align="center", sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
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
        # Says the result does not match the draft, or that a run is in flight.
        self._stale_text = pn.pane.HTML(
            "", margin=0, align="center", sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
        )
        self.stale_run_button = pmui.Button(
            label="Run",
            icon="play_arrow",
            variant="text",
            size="small",
            margin=(0, 4, 0, 0),
            align="center",
            description="Run the draft (Ctrl+Enter)",
            sx={"textTransform": "none", "py": "1px", "fontSize": "0.75rem"},
            css_classes=["fd-code-stale-run"],
        )
        self.stale_run_button.on_click(lambda _event: self.run())
        self._stale_badge = pn.Row(
            self._stale_text,
            self.stale_run_button,
            height=32,
            margin=0,
            visible=False,
            sizing_mode="stretch_width",
            stylesheets=[_STRIP_CSS, _STALE_CSS],
            css_classes=["fd-code-stale-badge"],
        )
        self._result_footer = pn.pane.HTML(
            "", margin=0, align="center", sizing_mode="stretch_width", stylesheets=[_BAR_CSS]
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
            self._stale_badge,
            self._result_body,
            result_footer,
            margin=0,
            sizing_mode="stretch_both",
            styles={"min-width": "0", "overflow": "hidden"},
            css_classes=["fd-code-panel-result"],
        )
        self._body = self._make_body()
        self._layout = pmui.Column(
            self._header,
            self._body,
            margin=0,
            sizing_mode="stretch_both",
            sx={"bgcolor": "background.paper"},
            css_classes=["fd-code-panel"],
        )
        self.param.watch(self._sync_collapsed, "collapsed")
        self.param.watch(self._sync_run_button, ["dirty", "run_on_change", "field"])
        self.param.watch(self._run_on_change_set, "run_on_change")
        self.param.watch(lambda *_events: self._sync_stale(), ["running", "dirty", "instance_id"])
        self.param.watch(self._spec_view_set, ["spec_view", "spec_text"])
        self.param.watch(self._sync_dock, ["dock", "can_dock_right"])
        self._sync_dock()
        self._sync_collapsed()
        self._sync_kind()
        self._sync_run_button()

    def _make_body(self) -> HSplit | VSplit:
        """Lay out the code and the result side by side, or one above the other when docked right."""
        if self.dock == "right":
            split, sizes, least = VSplit, STACKED_SIZES, MIN_PANE_HEIGHT
        else:
            split, sizes, least = HSplit, SPLIT_SIZES, MIN_PANE_WIDTH
        return split(
            self._code_column,
            self._result_column,
            sizes=sizes,
            expanded_sizes=sizes,
            min_size=(least, least),
            snap_size=0,
            gutter_size=8,
            margin=0,
            sizing_mode="stretch_both",
            visible=not self.collapsed,
            stylesheets=[GUTTER_CSS],
            css_classes=["fd-code-panel-body"],
        )

    def toggle_dock(self):
        """Dock the panel to the right of the canvas, or back under it."""
        self.dock = "bottom" if self.dock == "right" else "right"

    @pn.io.hold()
    def _sync_dock(self, *events):
        right = self.dock == "right"
        self.dock_button.param.update(
            icon="horizontal_split" if right else "view_sidebar",
            description="Dock under the canvas" if right else "Dock to the right",
            visible=right or self.can_dock_right,
        )
        if right and self.collapsed:
            self.collapsed = False
        if any(e.name == "dock" for e in events):
            self._keep_text()
            self._body.objects = []
            self._body = self._make_body()
            self._layout[:] = [self._header, self._body]
        self._sync_collapsed()

    def _keep_text(self):
        """Make the text typed in the editors what they show when rendered again, e.g. once moved.

        A browser renders an editor from its applied text, so spec text typed
        but not applied is applied as leaving the editor would: a valid spec
        updates the draft and the form, invalid text stays with its problem
        marked. Ace's undo history starts afresh in the new editor.
        """
        self._syncing = True
        try:
            if self.code.value != self.code.value_input:
                self.code.value = self.code.value_input
        finally:
            self._syncing = False
        text = self.spec_editor.editor
        if self._kind() == "spec" and text.value != text.value_input:
            # A move is not an edit, so "Run on change" does not run it.
            self._hold_run = True
            try:
                text.value = text.value_input
            finally:
                self._hold_run = False
            self._sync_dirty()
            self._show_status()

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

    def _kind(self) -> str | None:
        """Return how the field shown is edited: ``"code"``, ``"spec"`` or None without one."""
        editor = self._flowdash
        if editor is None or self.instance_id is None or self.field is None:
            return None
        state = editor._dataflow_graph.get_config_state(self.instance_id)
        if state is None or self.field not in state.param:
            return None
        return "spec" if declared_settings(state.param[self.field]) is not None else "code"

    def can_edit(self, instance_id: str, field: str | None = None) -> bool:
        """Whether the panel can show the node *instance_id*, editing its spec field *field*.

        Without a *field* it shows the node's result only.
        """
        editor = self._flowdash
        if editor is None:
            return False
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        if item is None or "placeholder" in item:
            return False
        spec = editor._component_specs.get(item["component_id"])
        if spec is None:
            return False
        if field is None:
            return True
        state = editor._dataflow_graph.get_config_state(instance_id)
        return state is not None and field in spec_fields(spec)

    @pn.io.hold()
    def open(self, instance_id: str, field: str | None = None, focus: bool = True):
        """Show the node *instance_id*, editing its spec field *field*, keeping the draft if it is already shown.

        Another node's draft not run yet is held until it is shown again.
        With *focus*, as when asked for rather than following the selection,
        a collapsed panel expands and the editor takes the keyboard.
        """
        if (instance_id, field) == (self.instance_id, self.field):
            if focus:
                self.collapsed = False
                self._focus()
            return
        self.release()
        self.param.update(instance_id=instance_id, field=field)
        if focus:
            self.collapsed = False
        editor = self._flowdash
        spec = self._spec()
        state = editor._dataflow_graph.get_config_state(instance_id)
        self._good = {}
        self._format_error = None
        self._result = CheckResult()
        self._failed = False
        self._ran_at = self._took = None
        kind = self._kind()
        parameter = state.param[field] if kind is not None else None
        self._syncing = True
        try:
            if kind == "code":
                language = code_language(parameter) or "text"
                text = getattr(state, field) or ""
                self.code.param.update(language=language, value=text, value_input=text)
                self._language.object = _bar(html.escape(language.upper()))
            elif kind == "spec":
                choices = editor._inspector._choices.get(instance_id, {})
                self.spec_editor.param.update(
                    settings=declared_settings(parameter) or [],
                    choices=setting_choices(choices, field),
                )
                self.spec_editor.load(
                    copy.deepcopy(getattr(state, field) or {}),
                    comments=self._yaml_docs.get((instance_id, field)),
                )
            else:
                self._language.object = _bar("")
            self.run_on_change = self._run_on_change.get(kind, False)
        finally:
            self._syncing = False
        self._code_header.object = self._code_header_html(spec, parameter)
        self._empty.object = self._empty_html(spec) if kind is None else ""
        self._show_definition()
        self._sync_kind()
        held = self._held.pop((instance_id, field), None) if field else None
        if held:
            self._restore_held(held, getattr(state, field))
        self._sync_field_toggle(spec)
        if kind is not None:
            watcher = state.param.watch(self._config_changed, [field])
            self._releases.append(lambda: state.param.unwatch(watcher))
        node = editor._dataflow_graph.get_state(instance_id)
        ports = [p for p in spec.outputs if node is not None and p.name in node.param]
        outputs = [p.name for p in ports]
        if node is not None and outputs:
            out_watcher = node.param.watch(lambda *_e: self._show_result(), outputs)
            self._releases.append(lambda: node.param.unwatch(out_watcher))
            if kind is None:
                # A new output, e.g. of a source picked by its ref, may come from a new definition.
                def_watcher = node.param.watch(lambda *_e: self._show_definition(), outputs)
                self._releases.append(lambda: node.param.unwatch(def_watcher))
        inputs = [p.name for p in spec.inputs if node is not None and p.name in node.param]
        if node is not None and inputs and kind is not None:
            in_watcher = node.param.watch(lambda *_e: self._complete(), inputs)
            self._releases.append(lambda: node.param.unwatch(in_watcher))
        options = {_output_label(p): p.name for p in ports}
        if kind == "spec" and instance_id in editor._component_instances:
            # A spec, e.g. a chart's, is best judged by what it draws.
            options = {"View": _VIEW, **options}
        self.outputs.param.update(
            options=options,
            value=_VIEW if _VIEW in options.values() else self._default_output(node, outputs),
        )
        self.outputs.visible = len(options) > 1
        self._title.object = self._title_html(spec)
        if spec.check_config is not None and kind is not None:
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
        if focus:
            self._focus()

    def _focus(self):
        kind = self._kind()
        if kind == "code":
            self.tools.focus += 1
        elif kind == "spec" and self._spec_text.visible:
            self.spec_tools.focus += 1

    def _sync_kind(self):
        """Show the editor for the field shown: code, the spec's form or text, or the empty state."""
        kind = self._kind()
        has_form = kind == "spec" and bool(self.spec_editor.settings)
        form = has_form and self.spec_editor.mode == "form"
        self.code.visible = self.tools.visible = kind == "code"
        self._spec_form.visible = form
        self._spec_text.visible = self.spec_tools.visible = kind == "spec" and not form
        options = SPEC_VIEWS if has_form else TEXT_VIEWS
        view = self.spec_view if has_form else self.spec_text
        syncing, self._syncing = self._syncing, True
        try:
            self.view_toggle.param.update(options=options, value=view, visible=kind == "spec")
        finally:
            self._syncing = syncing
        self._empty.visible = kind is None and not self.definition.visible
        formatter = self._formatter()
        label = self.spec_editor.syntax_label
        self.format_button.param.update(
            visible=formatter is not None,
            description=f"Format the {label if kind == 'spec' else 'code'} ({FORMAT_SHORTCUT})",
        )
        self.tools.can_format = kind == "code" and formatter is not None
        self.spec_tools.can_format = kind == "spec" and formatter is not None
        self.fix_json_button.label = f"Fix in {label}"
        self._sync_json_error()
        if kind == "spec":
            self._language.object = _bar("" if form else label)

    def _view_picked(self, event):
        """Show the spec's view picked, once text typed in the view left is applied.

        Text that does not parse cannot be shown in the other syntax, so it
        stays, saying why; the form says why it is disabled.
        """
        if self._syncing or event.new is None:
            return
        if self._kind() == "spec" and self._spec_text.visible and not self.spec_editor.flush():
            if event.new != "form":
                self._syncing = True
                try:
                    self.view_toggle.value = event.old
                finally:
                    self._syncing = False
                self.spec_editor.status.object = (
                    f"Not switched to {event.new.upper()}: the "
                    f"{self.spec_editor.syntax_label} is not valid. {self.spec_editor.error}"
                )
                self._sync_dirty()
                self._show_status()
                return
        self._sync_dirty()
        self._show_status()
        if event.new == "form":
            self.spec_view = "form"
        elif self.spec_editor.settings or self.spec_view != "form":
            self.param.update(spec_view=event.new, spec_text=event.new)
        else:
            # A spec without a form keeps the view picked for those with one.
            self.spec_text = event.new

    def _spec_view_set(self, *_events):
        if self.spec_view != "form" and self.spec_text != self.spec_view:
            self.spec_text = self.spec_view
            return
        self.spec_editor.param.update(
            mode="form" if self.spec_view == "form" else "spec", syntax=self.spec_text
        )
        self._sync_kind()

    def _spec_mode_set(self, event):
        """Follow the spec editor showing its text, e.g. for a link from the form to a path."""
        if event.new == "spec" and self.spec_view == "form":
            self.spec_view = self.spec_text
        else:
            self._sync_kind()

    def _run_on_change_set(self, event):
        kind = self._kind()
        if not self._syncing and kind is not None:
            self._run_on_change[kind] = event.new
        self.spec_editor.live = event.new and kind == "spec"

    def _show_definition(self):
        """Show what defines a node without code or a spec, read only, if its component says."""
        editor = self._flowdash
        empty = editor is not None and self.instance_id is not None and self._kind() is None
        instance = editor._component_instances.get(self.instance_id) if empty else None
        definition = call_definition_hook(instance) if instance is not None else None
        if definition is None:
            if self.definition.visible:
                self.definition.visible = False
                self._empty.visible = empty
                if empty:
                    self._code_header.object = self._code_header_html(self._spec(), None)
                    self._language.object = _bar("")
            return
        language, text = definition
        self.definition.param.update(language=language or "text", value=text, visible=True)
        self._empty.visible = False
        self._code_header.object = _bar("<b>Definition</b>", "Read only")
        self._language.object = _bar(html.escape((language or "text").upper()))

    def _default_output(self, node: param.Parameterized | None, outputs: list[str]) -> str | None:
        """Return the output to show first: a table before a lazy value, e.g. a query's pipeline."""
        for name in outputs:
            if _is_frame(getattr(node, name, None)):
                return name
        return outputs[0] if outputs else None

    def _restore_held(self, held: dict[t.Any, t.Any], current: t.Any):
        """Show a held draft again, on the field's current value if that changed since it was held.

        A spec draft is rebased: the paths it changed are applied to the
        current spec. Text, a code field's or spec text that does not parse,
        is not merged; a notice offers to keep the draft or discard it.
        """
        base = held.get(_BASE, current)
        if _same_spec(base, current):
            self._restore(held)
            return
        if self._kind() == "spec" and _TEXT not in held and self.field in held:
            rebased = _rebase(base, held[self.field], current)
            if rebased is not None:
                self._restore({self.field: rebased})
                return
        self._stale = held
        self._show_stale()

    def _show_stale(self):
        held = self._stale
        if held is None:
            self._stale_notice.param.update(objects=[], visible=False)
            return
        self._stale_notice.param.update(
            objects=[
                text_alert(
                    "This draft was written against an older version, which has changed "
                    "since, e.g. by an undo or the assistant. Keep it to replace the current "
                    "version when run, or discard it.",
                    severity="warning",
                    title="Draft not run",
                    margin=(8, 12, 4, 12),
                    sizing_mode="stretch_width",
                ),
                pn.Row(self.keep_draft_button, self.discard_stale_button, margin=0, align="start"),
            ],
            visible=True,
        )

    def keep_stale_draft(self):
        """Show the held draft written against an older version of the field."""
        held, self._stale = self._stale, None
        if held is not None:
            self._restore(held)
        self._show_stale()
        self._sync_dirty()

    def discard_stale_draft(self):
        """Drop the held draft written against an older version of the field."""
        self._stale = None
        self._show_stale()
        self._sync_dirty()

    def drafted_fields(self, instance_id: str) -> list[str]:
        """Return the spec fields of the node *instance_id* holding an edit that was not run."""
        fields = [k[1] for k in self._held if k[0] == instance_id]
        if self.instance_id == instance_id and self.field is not None:
            if (self.dirty or self._stale is not None) and self.field not in fields:
                fields.append(self.field)
        return fields

    def _sync_field_toggle(self, spec: ComponentSpec | None):
        editor = self._flowdash
        fields = spec_fields(spec) if spec is not None and self.field is not None else []
        state = editor._dataflow_graph.get_config_state(self.instance_id) if editor else None
        drafted = set(self.drafted_fields(self.instance_id)) if self.instance_id else set()
        options = {}
        for name in fields:
            label = field_label(state.param[name]) or name if state is not None else name
            draft = name in drafted and name != self.field
            options[f"{label} (draft not run)" if draft else label] = name
        self._syncing = True
        try:
            self.field_toggle.param.update(
                options=options, value=self.field if options else None, visible=len(options) > 1
            )
        finally:
            self._syncing = False

    def _field_picked(self, event):
        if self._syncing or event.new is None or self.instance_id is None:
            return
        if event.new != self.field:
            self.open(self.instance_id, event.new)

    def _restore(self, held: dict[t.Any, t.Any]):
        kind = self._kind()
        self._syncing = True
        try:
            if kind == "code" and self.field in held:
                text = held[self.field]
                self.code.param.update(value=text, value_input=text)
            elif kind == "spec":
                if self.field in held:
                    self.spec_editor.load(
                        copy.deepcopy(held[self.field]), comments=self.spec_editor.yaml_doc
                    )
                if _TEXT in held:
                    syntax = held.get(_SYNTAX, self.spec_text)
                    if syntax != self.spec_text:
                        # Shown in the syntax it was typed in.
                        views = {} if self.spec_view == "form" else {"spec_view": syntax}
                        self.param.update(spec_text=syntax, **views)
                    # Applied as typed, so text that is not a valid spec shows why again.
                    self.spec_editor.editor.value = held[_TEXT]
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
        for key in [k for k in self._yaml_docs if k[0] == instance_id]:
            del self._yaml_docs[key]
        self.param.trigger("dirty")

    def discard(self):
        """Put the node's spec back in the editor, dropping the draft."""
        editor = self._flowdash
        kind = self._kind()
        if editor is None or kind is None:
            return
        state = editor._dataflow_graph.get_config_state(self.instance_id)
        value = getattr(state, self.field)
        self._stale = None
        self._show_stale()
        self._restore({self.field: value or ("" if kind == "code" else {})})
        self._typing += 1
        self._sync_dirty()

    def release(self):
        """Stop following the node shown, e.g. before closing the panel, holding an unrun draft."""
        if self.instance_id is not None and self.field is not None:
            if self._stale is not None:
                # Not decided on, so it stays held against the version it was written for.
                self._held[(self.instance_id, self.field)] = self._stale
            else:
                draft = self._held_draft()
                if draft:
                    self._held[(self.instance_id, self.field)] = draft
            if self._kind() == "spec" and self.spec_editor.yaml_doc is not None:
                self._yaml_docs[(self.instance_id, self.field)] = self.spec_editor.yaml_doc
        self._stale = None
        self._show_stale()
        self._typing += 1
        for release in self._releases:
            release()
        self._releases = []
        if self._draft is not None:
            self._draft.on_result = None
        self._draft = None
        self._runs += 1
        # A run still in flight is no longer followed.
        self.param.update(instance_id=None, field=None, dirty=False, running=False)
        self.param.trigger("dirty")
        self.tools.param.update(diagnostics=[], completions=[])
        self.code.annotations = []

    def _code_header_html(self, spec: ComponentSpec, parameter: param.Parameter | None) -> str:
        """Name the field and where the node's inputs come from."""
        editor = self._flowdash
        sources = []
        for edge in editor._dataflow_graph.edges if editor else []:
            if edge["target"] == self.instance_id:
                sources.append(f"{editor._node_name(edge['source'])}.{edge['source_port']}")
        label = (
            (field_label(parameter) or self.field) if parameter is not None else "Nothing to edit"
        )
        origin = f"from {', '.join(sources)}" if sources else ""
        return _bar(f"<b>{html.escape(label)}</b>", html.escape(origin))

    @staticmethod
    def _empty_html(spec: ComponentSpec) -> str:
        return (
            '<div class="fd-cp-empty">'
            f"<b>{html.escape(spec.title)} has no code or spec to edit.</b>"
            "<span>Its settings are in the inspector. Select a node with a query or a spec "
            "to edit it here.</span></div>"
        )

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
        # Docked to the right the panel has the window's height, so it is not folded to its header.
        foldable = self.dock == "bottom"
        self._body.visible = not self.collapsed
        self.collapse_button.visible = foldable and not self.collapsed
        self.expand_button.visible = self.collapsed

    def _sync_run_button(self, *_events):
        editing = self.field is not None
        self.run_button.param.update(
            variant="contained" if self.dirty or not self.run_on_change else "outlined",
            visible=editing,
        )
        self.run_switch.visible = editing
        self.discard_button.visible = self.dirty

    # ------------------------------------------------------------------
    # The draft
    # ------------------------------------------------------------------

    def _patch(self) -> dict[str, t.Any]:
        """Return the draft's change to the node's config."""
        editor = self._flowdash
        kind = self._kind()
        if editor is None or kind is None:
            return {}
        state = editor._dataflow_graph.get_config_state(self.instance_id)
        current = getattr(state, self.field)
        if kind == "code":
            text = self.code.value_input
            return {self.field: text} if text != (current or "") else {}
        value = self.spec_editor.value or {}
        if _same_spec(value, current or {}):
            return {}
        return {self.field: copy.deepcopy(value)}

    def _held_draft(self) -> dict[t.Any, t.Any]:
        """Return the draft to hold while another node is shown: the patch, and spec text not applied."""
        draft: dict[t.Any, t.Any] = self._patch()
        if self._kind() == "spec" and self._spec_pending():
            draft[_TEXT] = self.spec_editor.editor.value_input
            draft[_SYNTAX] = self.spec_editor.syntax
        if draft:
            editor = self._flowdash
            state = editor._dataflow_graph.get_config_state(self.instance_id)
            draft[_BASE] = copy.deepcopy(getattr(state, self.field))
        return draft

    def _spec_pending(self) -> bool:
        """Whether the spec's text holds an edit the form and the draft do not have yet."""
        return self.spec_editor.unsent or self.spec_editor.error is not None

    def _sync_dirty(self):
        self.dirty = (
            bool(self._patch())
            or (self._kind() == "spec" and self._spec_pending())
            or self._stale is not None
        )
        self._show_result_footer()
        self._sync_stale()

    def _sync_stale(self):
        """Dim the result under a badge while it does not match the draft, or a run is in flight.

        A run, and a draft that "Run on change" is about to run, are shown
        only once they last :data:`STALE_DELAY` seconds, so quick runs do not
        flash.
        """
        if self.instance_id is None:
            state = None
        elif self.running:
            state = "running"
        elif self.dirty:
            state = "draft"
        else:
            state = None
        self._stale_shown += 1
        wait = state == "running" or (
            state == "draft" and self.stale is None and self.run_on_change
        )
        if not wait:
            self._show_stale_state(state)
            return
        shown = self._stale_shown

        async def later():
            await asyncio.sleep(STALE_DELAY)
            if shown == self._stale_shown:
                self._show_stale_state(state)

        param.parameterized.async_executor(later)

    def _show_stale_state(self, state: str | None):
        if state == "running":
            text = f'<span class="fd-cp-warn">{_icon("hourglass_top")}<span>Running…</span></span>'
        else:
            text = (
                f'<span class="fd-cp-warn">{_icon("edit_note")}'
                "<span><b>Not run</b>: showing the previous version</span></span>"
            )
        self._stale_text.object = _bar(text)
        self.stale_run_button.visible = state != "running"
        self._stale_badge.visible = state is not None
        classes = [c for c in self._result_body.css_classes if c != "fd-code-result-dimmed"]
        # A result a failed check dims already is not dimmed twice.
        if state is not None and not self._result:
            classes.append("fd-code-result-dimmed")
        if classes != self._result_body.css_classes:
            self._result_body.param.update(
                css_classes=classes,
                styles={"opacity": "0.55"} if "fd-code-result-dimmed" in classes else {},
            )
        self.stale = state

    def stale_states(self) -> dict[str, str]:
        """Return the nodes whose result does not match their spec: ``"draft"`` or ``"running"``.

        A node has a draft not run while it is shown with one, or holds one
        for later.
        """
        states = {instance_id: "draft" for instance_id, _field in self._held}
        if self.instance_id is not None and self.stale is not None:
            states[self.instance_id] = self.stale
        return states

    def _typed(self, _event):
        if self._syncing or self._kind() != "code":
            return
        if self._format_error is not None:
            self._format_error = None
            self._show_status()
        self._sync_dirty()
        self._run_later()

    def _spec_edited(self, _event):
        if self._syncing or self._kind() != "spec":
            return
        self._sync_dirty()
        if not self._hold_run:
            # The text waited for a pause in typing already, and a form edit runs at once.
            self._run_later(delay=0)

    def _spec_typed(self, _event):
        self._sync_json_error()
        if self._syncing or self._kind() != "spec":
            return
        self._sync_dirty()
        self._show_status()

    def _sync_json_error(self):
        """Say in the Form view why the form is disabled while the text does not parse."""
        error = self.spec_editor.error if self._kind() == "spec" else None
        shown = error is not None and self._spec_form.visible
        if shown:
            self._json_error.objects = [
                text_alert(
                    f"The {self.spec_editor.syntax_label} is not a valid spec, so the form is "
                    f"disabled. {error}",
                    severity="error",
                    margin=(8, 12, 4, 12),
                    sizing_mode="stretch_width",
                ),
                self.fix_json_button,
            ]
        elif self._json_error.objects:
            self._json_error.objects = []
        self._json_error.visible = shown

    def _run_later(self, delay: float | None = None):
        """With "Run on change", run the draft after *delay* seconds of no edits, or after a run in flight."""
        if not (self.run_on_change and self.dirty):
            return
        delay = RUN_DELAY if delay is None else delay
        self._typing += 1
        typing = self._typing

        async def later():
            await asyncio.sleep(delay)
            if typing != self._typing:
                return
            if self.running:
                self._run_again = True
            else:
                self.run()

        param.parameterized.async_executor(later)

    def _config_changed(self, event):
        """Follow a change of the node's spec made elsewhere, unless the draft holds an edit of it."""
        kind = self._kind()
        self._syncing = True
        try:
            if kind == "code" and self.code.value_input == (event.old or ""):
                text = event.new or ""
                self.code.param.update(value=text, value_input=text)
            elif (
                kind == "spec"
                and not self._spec_pending()
                and _same_spec(self.spec_editor.value or {}, event.old or {})
            ):
                self.spec_editor.load(
                    copy.deepcopy(event.new or {}), comments=self.spec_editor.yaml_doc
                )
        finally:
            self._syncing = False
        self._sync_dirty()

    def run(self, text: str | None = None):
        """Apply the draft to the node, after its check passes if the component has one.

        *text* is the editor's text as sent with Ctrl+Enter, which may be
        ahead of what the server has. Spec text that is not a valid spec is
        not run; the problem is shown under it.
        """
        editor = self._flowdash
        kind = self._kind()
        if editor is None or kind is None or not editor.editable:
            return
        if kind == "code" and text is not None and text != self.code.value_input:
            self._syncing = True
            try:
                self.code.value_input = text
            finally:
                self._syncing = False
        elif kind == "spec":
            pending = text if text is not None else self.spec_editor.editor.value_input
            if self._spec_text.visible and pending != self.spec_editor.editor.value:
                self._syncing = True
                try:
                    self.spec_editor.editor.value = pending
                finally:
                    self._syncing = False
            if self.spec_editor.error is not None:
                self._sync_dirty()
                self._show_status()
                return
        if self._stale is not None:
            # What is run replaces the older draft the notice offered.
            self._stale = None
            self._show_stale()
        self._typing += 1
        self._close_completions()
        patch = self._patch()
        self._runs += 1
        self._run_again = False
        self._started = time.perf_counter()
        self.running = True
        self._show_result_footer(running=True)
        if self._draft is not None:
            self._draft.propose(patch)
            return
        instance_id = self.instance_id

        def finish(applies: bool):
            # The run always finishes, so a failing apply does not leave it running.
            result = CheckResult()
            try:
                if applies:
                    editor._apply_config_patch(instance_id, patch)
            except Exception as exc:
                logger.exception("Applying the draft of %s failed", instance_id)
                result = CheckResult(
                    [f"Could not apply the draft: {type(exc).__name__}: {error_text(exc)}"]
                )
            finally:
                self._ran(result)

        async def apply():
            # A turn of the loop first, so "Running…" reaches the page before a slow refresh.
            await asyncio.sleep(0)
            finish(bool(patch) and self.instance_id == instance_id)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            finish(bool(patch))
            return
        param.parameterized.async_executor(apply)

    def _formatter(self) -> t.Callable[[str], str] | None:
        """Return what formats the text shown: the code field's formatter, or the spec text's syntax's."""
        kind = self._kind()
        if kind == "spec":
            if not self._spec_text.visible:
                return None
            return _format_spec_yaml if self.spec_editor.syntax == "yaml" else _format_spec_text
        if kind != "code":
            return None
        state = self._flowdash._dataflow_graph.get_config_state(self.instance_id)
        return code_formatter(state.param[self.field])

    def format(self, text: str | None = None):
        """Lay out the text in the editor, as an edit of the draft that Ctrl+Z undoes.

        *text* is the editor's text as sent with Shift+Alt+F, which may be
        ahead of what the server has. Text that does not parse is left as
        it is, with the problem said under it.
        """
        formatter = self._formatter()
        kind = self._kind()
        editor = self._flowdash
        if formatter is None or editor is None or not editor.editable:
            return
        source = self.code if kind == "code" else self.spec_editor.editor
        tools = self.tools if kind == "code" else self.spec_tools
        if text is None:
            text = source.value_input
        try:
            formatted = formatter(text)
        except Exception as exc:
            self._format_failed(kind, text, exc)
            return
        if not isinstance(formatted, str):
            return
        self._format_error = None
        shown = source.value
        if kind == "code":
            self._syncing = True
            try:
                self.code.param.update(value=formatted, value_input=formatted)
            finally:
                self._syncing = False
            self._show_status()
            self._sync_dirty()
        else:
            # Applied as if typed, so the spec and the form follow the text.
            self._hold_run = True
            try:
                self.spec_editor.editor.param.update(value_input=formatted)
                self.spec_editor.editor.value = formatted
            finally:
                self._hold_run = False
        if source.value == shown:
            # The browser updates the editor when its value changes; unchanged, e.g. after an
            # undo there, the text is sent. Sending both could redo the edit after an undo.
            tools.replace(source.value_input)

    def _format_failed(self, kind: str | None, text: str, error: Exception):
        if kind == "spec":
            # Applied as typed, so the editor marks where the text breaks, as Run would.
            self.spec_editor.editor.value = text
            return
        self._format_error = f"Could not format: {error}"
        self._show_status()

    def _close_completions(self):
        self.tools.closing += 1
        self.spec_tools.closing += 1

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
        self._result = result
        if not result:
            self._ran_at = dt.datetime.now()
            self._took = took
        self.running = False
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
        # Problems in a spec are shown above it; its JSON is laid out afresh, so lines would not hold.
        positioned = result.diagnostics(self.field) if self._kind() == "code" else []
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
        invalid = self._kind() == "spec" and self.spec_editor.error is not None
        if invalid:
            label = self.spec_editor.syntax_label
            status = f'<span class="fd-cp-error">{_icon("error")}Not run, the {label} is not valid</span>'
        elif result:
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
        if self._format_error and self._kind() == "code":
            status = f'<span class="fd-cp-warn">{_icon("warning")}{html.escape(self._format_error)}</span>'

        self._code_status.object = _bar(status)
        spec = self._spec()
        if spec is not None:
            self._title.object = self._title_html(spec)

    def _complete(self):
        """Offer the words a code field completes, or the choices of a spec form's settings."""
        editor = self._flowdash
        instance_id, field, kind = self.instance_id, self.field, self._kind()
        if editor is None or kind is None:
            return

        async def fetch_choices():
            try:
                choices = await node_choices(editor, instance_id)
            except Exception:
                return
            if (self.instance_id, self.field) == (instance_id, field):
                self.spec_editor.choices = setting_choices(choices, field)

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

        param.parameterized.async_executor(fetch_choices if kind == "spec" else fetch)

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
        if spec is not None and (not spec.outputs or self.outputs.value == _VIEW):
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
                    else f"{type(error).__name__}: {error_text(error)}",
                    severity="error",
                    title="The last run failed, showing the last good result",
                    margin=(8, 12),
                    sizing_mode="stretch_width",
                    css_classes=["fd-code-run-error"],
                )
            )
        self._shown_results += 1
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
            self._result_body[:] = [
                ViewFrame(
                    object=instance,
                    margin=0,
                    sizing_mode="stretch_both",
                    css_classes=["fd-code-view"],
                )
            ]
        self._summary.object = _bar(self._ran_text())
        self._show_result_footer()

    def _value_view(self, editor: FlowDash, value: t.Any) -> pn.viewable.Viewable:
        """Show a DataFrame as a table, and any other output as the inspector's Data tab does.

        The component's own preview of the output comes first; it may query,
        so in a session it is asked for on a worker thread.
        """
        if value is None:
            return pn.pane.HTML(_bar("No result yet."), margin=(8, 12), stylesheets=[_BAR_CSS])
        if _is_pandas(value):
            return pn.pane.DataFrame(
                safe_frame(value.head(MAX_ROWS)),
                index=_shows_index(value),
                sizing_mode="stretch_both",
                margin=0,
                stylesheets=[_TABLE_CSS],
                css_classes=["fd-code-table"],
            )
        holder = pn.Column(
            margin=(8, 12), sizing_mode="stretch_width", css_classes=["fd-code-preview"]
        )
        instance = editor._component_instances.get(self.instance_id)
        name = self.outputs.value
        if not wants_preview_hook(value, instance):
            holder[:] = _previewed(editor, value)
            return holder
        work = partial(hooked_preview, value, instance, name)
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            holder[:] = _previewed(editor, *work())
            return holder
        holder[:] = [pn.pane.HTML(_bar("Loading the preview…"), margin=0, stylesheets=[_BAR_CSS])]
        generation = self._shown_results

        def show(result):
            # A later result or another output supersedes this preview.
            if generation == self._shown_results:
                holder[:] = _previewed(editor, *result)

        run_off_loop(work, show, doc)
        return holder

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
        elif self._kind() == "spec" and self.spec_editor.error is not None:
            label = self.spec_editor.syntax_label
            text = f'<span class="fd-cp-error">Not run, the {label} is not valid</span>'
        elif self._stale is not None:
            text = "A draft written against an older version is waiting"
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


def _is_pandas(value: t.Any) -> bool:
    try:
        import pandas as pd
    except ImportError:
        return False
    return isinstance(value, pd.DataFrame)


def _previewed(editor: FlowDash, value: t.Any, hooked: bool = False) -> list:
    """Return the inspector's preview of *value*, or a caption naming it where that fails."""
    try:
        return value_preview(editor, value, hooked)
    except Exception:
        logger.exception("Previewing a %s failed", type(value).__name__)
        return [pn.pane.HTML(_bar(html.escape(summarize_value(value))), stylesheets=[_BAR_CSS])]


def _format_spec_yaml(text: str) -> str:
    """Return spec YAML *text* laid out as the editor shows a spec, keeping its comments."""
    spec, doc = parse_spec_yaml(text)
    return format_spec_yaml(spec, doc)[0]


def _format_spec_text(text: str) -> str:
    """Return spec JSON *text* laid out as the editor shows a spec, keys in the order written."""
    return format_spec(parse_spec(text))[0]


def _same_spec(a: t.Any, b: t.Any) -> bool:
    """Whether two specs are equal, keys in the same order too, as the order is kept when saved."""
    try:
        return bool(a == b) and _key_order(a) == _key_order(b)
    except Exception:
        return False


def _key_order(value: t.Any) -> t.Any:
    if isinstance(value, dict):
        return [(k, _key_order(v)) for k, v in value.items()]
    if isinstance(value, list):
        return [_key_order(v) for v in value]
    return None


def _differences(base: t.Any, draft: t.Any, prefix: tuple = ()) -> list[tuple[tuple, t.Any]]:
    """Return the paths where *draft* differs from *base*, with the draft's value or ``_MISSING``."""
    if not isinstance(base, dict) or not isinstance(draft, dict):
        return [] if _same_spec(base, draft) else [(prefix, draft)]
    found = []
    for key, value in draft.items():
        path = (*prefix, key)
        if key not in base:
            found.append((path, value))
        else:
            found.extend(_differences(base[key], value, path))
    found.extend(((*prefix, key), _MISSING) for key in base if key not in draft)
    return found


def _rebase(base: t.Any, draft: t.Any, current: t.Any) -> dict | None:
    """Return *current* with the changes *draft* made to *base*, or None where they conflict.

    The draft is rebased only where the changes made since touch other
    paths than the draft's, none inside another, a list counting as one
    value; otherwise the person decides.
    """
    if not isinstance(base, dict) or not isinstance(current, dict):
        return None
    drafted = _differences(base, draft or {})
    outside = [path for path, _value in _differences(base, current)]
    for path, _value in drafted:
        for other in outside:
            shared = min(len(path), len(other))
            if path[:shared] == other[:shared]:
                return None
    rebased = copy.deepcopy(current)
    for path, value in drafted:
        if not path:
            return None
        try:
            if value is _MISSING:
                delete_path(rebased, list(path))
            else:
                set_path(rebased, list(path), copy.deepcopy(value))
        except SpecPathError:
            return None
    return rebased
