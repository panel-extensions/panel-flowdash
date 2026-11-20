"""Compact canvas nodes: a stacked-port card naming a component, its ports and its preview.

The live component is shown only in the tile grid, so the same view is never
rendered twice and a table or map is never squeezed into a node. A card gives
each port its own full-width row (inputs first, then outputs) with port names
only; types are shown as handle colors and hover tooltips. Unconnected optional
inputs fold into a single clickable row when there are two or more, outputs can
be clicked to switch the previewed port on multi-output nodes, and cards can be
collapsed to a 44 px header strip.
"""

from __future__ import annotations

import html
import logging
import threading
import typing as t
from functools import partial

import param
from panel.custom import JSComponent
from panel.pane import HTML
from panel.viewable import Viewer

from panel_flowdash.port_types import port_type_color, port_types_compatible
from panel_flowdash.ui.node_preview import (
    CHART_COLOR,
    PREVIEW_CSS,
    PREVIEW_HOOK,
    Coalescer,
    Preview,
    build_preview,
    call_preview_hook,
    hook_preview,
    preview_priority,
    run_off_loop,
    summarize_value,
)
from panel_flowdash.ui.roles import ROLE_ICONS
from panel_flowdash.util import is_failure, missing_inputs

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec, InputPort, OutputPort
    from panel_flowdash.dataflow_engine import InputFailures
    from panel_flowdash.placeholders import Placeholder

logger = logging.getLogger(__name__)

__all__ = [
    "CARD_WIDTH",
    "NodeCard",
    "card_height",
    "port_offsets",
    "port_tops",
    "select_preview_output",
    "summarize_value",
]

CARD_WIDTH = 260
_HEADER_HEIGHT = 44
_PORTS_PAD = 4
_PORT_ROW_HEIGHT = 22
_SUMMARY_HEIGHT = 34
_FOLD_OPTIONAL_THRESHOLD = 2
_COLLAPSED_HANDLE_SPACING = 12

STATUS_TEXT = {
    "ok": "Up to date",
    "waiting": "Waiting for input",
    "failed": "Failed",
    "unavailable": "Unavailable",
}

_CARD_CSS = (
    """
:host {
  display: block;
  width: 100%;
  height: 100%;
  box-sizing: border-box;
  --fd-text: var(--mui-palette-text-primary, var(--panel-on-background-color, #1f1f1f));
  --fd-muted: var(--mui-palette-text-secondary, rgba(127, 127, 127, 0.95));
  --fd-divider: var(--mui-palette-divider, rgba(127, 127, 127, 0.22));
  --fd-primary: var(--mui-palette-primary-main, var(--panel-primary-color, #0072b5));
  --fd-surface: var(--mui-palette-action-hover, rgba(127, 127, 127, 0.08));
  --fd-hover: var(--mui-palette-action-selected, rgba(127, 127, 127, 0.14));
  --fd-chip-bg: var(--mui-palette-action-hover, rgba(127, 127, 127, 0.12));
  --fd-card-bg: var(--mui-palette-background-paper, var(--panel-background-color, #ffffff));
  --fd-viz-1: """
    + CHART_COLOR
    + """;
  --fd-viz-1-soft: rgba(42, 157, 143, 0.18);
}
.fd-card {
  position: relative;
  width: 100%;
  height: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  color: var(--fd-text);
  font-size: 12px;
  line-height: 16px;
}
.fd-card-action {
  pointer-events: auto;
  cursor: pointer;
}
.fd-ms {
  font-family: "Material Icons";
  font-style: normal;
  font-weight: normal;
  line-height: 1;
  font-feature-settings: "liga";
  -webkit-font-smoothing: antialiased;
  display: inline-block;
  vertical-align: middle;
  user-select: none;
}
.fd-head {
  height: 44px;
  flex: none;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 8px 0 10px;
  border-bottom: 1px solid var(--fd-divider);
}
.fd-card.fd-collapsed .fd-head {
  border-bottom: 0;
}
.fd-icon {
  flex: none;
  width: 18px;
  height: 18px;
  overflow: hidden;
  font-family: "Material Icons";
  font-size: 18px;
  line-height: 1;
  color: var(--fd-primary);
  font-feature-settings: "liga";
  -webkit-font-smoothing: antialiased;
}
.fd-title-block {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.fd-title {
  font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  font-weight: 500;
  line-height: 16px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-subtitle {
  color: var(--fd-muted);
  font-size: 11px;
  line-height: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--mui-palette-success-main, #2e7d32);
}
.fd-waiting .fd-dot { background: var(--mui-palette-warning-main, #ed6c02); }
.fd-failed .fd-dot { background: var(--mui-palette-error-main, #d32f2f); }
.fd-unavailable .fd-dot { background: var(--fd-muted); }
.fd-collapse-btn {
  flex: none;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fd-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  opacity: 0.72;
}
.fd-collapse-btn:hover {
  background: var(--fd-hover);
  color: var(--fd-text);
  opacity: 1;
}
.fd-collapse-btn .fd-ms { font-size: 16px; }
.fd-ports {
  flex: none;
  padding: 4px 0;
  border-bottom: 1px solid var(--fd-divider);
  box-sizing: border-box;
}
.fd-port {
  height: 22px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  font-size: 12px;
  line-height: 22px;
  white-space: nowrap;
  overflow: hidden;
  color: var(--fd-text);
}
.fd-port.fd-out {
  justify-content: flex-end;
}
.fd-port.fd-card-action:hover {
  background: var(--fd-surface);
}
.fd-port.fd-pv-on .fd-name {
  color: var(--fd-primary);
  font-weight: 500;
}
.fd-eye {
  font-size: 14px;
  color: var(--fd-primary);
  flex: none;
}
.fd-name {
  color: var(--fd-text);
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-popt {
  color: var(--fd-muted);
  font-size: 10.5px;
  font-style: italic;
  flex: none;
}
.fd-port-more {
  color: var(--fd-muted);
  font-size: 11.5px;
  gap: 4px;
  padding: 0 10px;
}
.fd-port-more .fd-ms {
  font-size: 15px;
  flex: none;
}
.fd-summary {
  margin: 6px 8px;
  height: 22px;
  line-height: 22px;
  padding: 0 8px;
  border-radius: 6px;
  background: var(--fd-surface);
  color: var(--fd-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  box-sizing: border-box;
}
.fd-failed .fd-summary { color: var(--mui-palette-error-main, #d32f2f); }
"""
    + PREVIEW_CSS
)


def card_height(n_inputs: int, n_outputs: int, *, extra_rows: int = 0) -> int:
    """Return the height of an expanded card without a preview."""
    rows = n_inputs + extra_rows + n_outputs
    ports_h = (2 * _PORTS_PAD + rows * _PORT_ROW_HEIGHT) if rows else 0
    return _HEADER_HEIGHT + ports_h + _SUMMARY_HEIGHT


def port_offsets(count: int) -> list[float]:
    """Return where *count* handles sit evenly spaced on a card, as percentages of its height."""
    return [(i + 1) * 100 / (count + 1) for i in range(count)]


def port_tops(
    n_inputs: int, n_outputs: int, *, extra_rows: int = 0
) -> tuple[list[float], list[float]]:
    """Return the vertical centres of stacked input and output rows, in px from the card top."""
    start = _HEADER_HEIGHT + _PORTS_PAD
    tops_in = [start + (i + 0.5) * _PORT_ROW_HEIGHT for i in range(n_inputs)]
    out_offset = n_inputs + extra_rows
    tops_out = [start + (out_offset + j + 0.5) * _PORT_ROW_HEIGHT for j in range(n_outputs)]
    return tops_in, tops_out


def _collapsed_top(index: int, count: int) -> float:
    """Return where a handle of a collapsed card sits: spread over the header, or all at its middle.

    Handles closer than a handle's size would overlap, so past that they share one point.
    """
    spacing = _HEADER_HEIGHT / (count + 1)
    if spacing < _COLLAPSED_HANDLE_SPACING:
        return _HEADER_HEIGHT / 2
    return spacing * (index + 1)


def component_role(spec: ComponentSpec) -> str:
    """Classify a component as a data ``source``, a ``transform`` or a ``view``."""
    if not spec.inputs:
        return "source"
    return "transform" if spec.outputs else "view"


def component_icon(spec: ComponentSpec) -> str:
    """Return the component's own icon, or one for its role."""
    return spec.icon or ROLE_ICONS[component_role(spec)]


def port_label(port: InputPort | OutputPort) -> str:
    """Return the name a port is shown under."""
    if port.label:
        return port.label
    parameter = getattr(port, "parameter", None)
    return parameter.label if parameter is not None and parameter.label else port.name


def select_preview_output(
    spec: ComponentSpec,
    state: param.Parameterized,
    preferred: str | None = None,
) -> OutputPort | None:
    """Choose which output port to preview, preferring a DataFrame over opaque values."""
    if not spec.outputs:
        return None
    if preferred:
        match = next((p for p in spec.outputs if p.name == preferred), None)
        if match is not None:
            return match
    best_port = None
    best_rank = 99
    for port in spec.outputs:
        value = getattr(state, port.name, None)
        rank = preview_priority(value)
        if rank == 1 and port.type and port_types_compatible(port.type, "DataFrame"):
            rank = 0
        if rank < best_rank:
            best_rank = rank
            best_port = port
            if rank == 0:
                break
    if best_port is not None and best_rank < 3:
        return best_port
    for port in spec.outputs:
        if port.type and port_types_compatible(port.type, "DataFrame"):
            return port
    return spec.outputs[0]


class CardBridge(JSComponent):
    """Relays clicks on the actions inside card HTML and styles each card's handles.

    Handle positions are not set here: they are CSS variables in each node's
    style, so React Flow re-measures the handles when they move.
    """

    canvas_class = param.String(default="")

    handles = param.Dict(default={})

    def __init__(self, *, on_action: t.Callable[[dict], None] | None = None, **params):
        self._on_action = on_action
        super().__init__(**params)

    _esm = """
    function findAll(root, selector, acc = []) {
      if (!root) return acc;
      if (root.shadowRoot) findAll(root.shadowRoot, selector, acc);
      if (!root.querySelectorAll) return acc;
      root.querySelectorAll(selector).forEach((el) => acc.push(el));
      root.querySelectorAll("*").forEach((el) => {
        if (el.shadowRoot) findAll(el.shadowRoot, selector, acc);
      });
      return acc;
    }

    function findCanvasRoot(canvasClass) {
      if (!canvasClass) return document;
      const hosts = findAll(document, `.${CSS.escape(canvasClass)}`);
      return hosts[0] || null;
    }

    function syncAll(canvasClass, allHandles) {
      const root = findCanvasRoot(canvasClass);
      if (!root || !allHandles) return false;
      const nodes = findAll(root, ".react-flow__node");
      if (!nodes.length) return false;
      for (const flowNode of nodes) {
        const nid = flowNode.dataset.id;
        const handles = allHandles[nid];
        if (!handles) continue;
        const inHandles = flowNode.querySelectorAll(".react-flow__handle-left");
        const outHandles = flowNode.querySelectorAll(".react-flow__handle-right");
        const applyList = (els, prefix) => {
          els.forEach((h) => {
            const id = h.dataset.handleid;
            const meta = handles[`${prefix}:${id}`];
            if (!meta) return;
            if (meta.color) h.style.setProperty("--fd-hc", meta.color);
            if (meta.dark_color) h.style.setProperty("--fd-hc-dark", meta.dark_color);
            h.classList.toggle("fd-handle-missing", !!meta.missing);
            h.classList.toggle("fd-handle-optional", !!meta.optional);
            h.classList.toggle("fd-handle-hidden", !!meta.hidden);
            if (meta.tooltip) {
              h.dataset.tooltip = meta.tooltip;
              h.setAttribute("aria-label", meta.tooltip);
            }
          });
        };
        applyList(inHandles, "in");
        applyList(outHandles, "out");
      }
      return true;
    }

    const VIEW_ONLY = new Set(["toggle_collapse"]);

    export function render({ el, model }) {
      el.style.display = "none";
      let rafId = null;
      let observer = null;
      let observedNodesEl = null;

      const scheduleSync = () => {
        if (rafId !== null) return;
        rafId = requestAnimationFrame(() => {
          rafId = null;
          syncAll(model.canvas_class, model.handles);
          attachObserver();
        });
      };

      const attachObserver = () => {
        const root = findCanvasRoot(model.canvas_class);
        if (!root) return;
        const nodesEl = findAll(root, ".react-flow__nodes")[0];
        if (!nodesEl || nodesEl === observedNodesEl) return;
        if (observer) observer.disconnect();
        observedNodesEl = nodesEl;
        observer = new MutationObserver(() => scheduleSync());
        observer.observe(nodesEl, { childList: true, subtree: true });
      };

      const pollInitial = (tries = 0) => {
        const ok = syncAll(model.canvas_class, model.handles);
        attachObserver();
        if (!ok && tries < 30) {
          setTimeout(() => pollInitial(tries + 1), 150);
        }
      };

      pollInitial();
      model.on("handles", scheduleSync);

      const locate = (ev) => {
        const path = ev.composedPath ? ev.composedPath() : [ev.target];
        let actionEl = null;
        let flowNode = null;
        let inCanvas = !model.canvas_class;
        for (const item of path) {
          if (!item || !item.classList) continue;
          if (!actionEl && item.dataset && item.dataset.fdAction) {
            actionEl = item;
          }
          if (!flowNode && item.classList.contains("react-flow__node")) {
            flowNode = item;
          }
          if (model.canvas_class && item.classList.contains(model.canvas_class)) {
            inCanvas = true;
          }
        }
        return actionEl && flowNode && inCanvas ? { actionEl, flowNode } : null;
      };

      // Collapsing is view state, so React Flow must not see the press and select or drag the card.
      const isViewOnly = (hit) => hit && VIEW_ONLY.has(hit.actionEl.dataset.fdAction);

      const onPress = (ev) => {
        if (isViewOnly(locate(ev))) ev.stopPropagation();
      };

      const onClick = (ev) => {
        const hit = locate(ev);
        if (!hit) return;
        if (isViewOnly(hit)) ev.stopPropagation();
        model.send_msg({
          node_id: hit.flowNode.dataset.id || "",
          action: hit.actionEl.dataset.fdAction || "",
          port: hit.actionEl.dataset.fdPort || "",
        });
      };

      const presses = ["pointerdown", "mousedown", "dblclick"];
      presses.forEach((type) => window.addEventListener(type, onPress, true));
      window.addEventListener("click", onClick, true);
      return () => {
        presses.forEach((type) => window.removeEventListener(type, onPress, true));
        window.removeEventListener("click", onClick, true);
        if (observer) observer.disconnect();
        if (rafId !== null) cancelAnimationFrame(rafId);
      };
    }
    """

    def _handle_msg(self, data: t.Any) -> None:
        if callable(self._on_action) and isinstance(data, dict):
            self._on_action(data)


def _port_tooltip(
    port: dict, *, is_input: bool, multi_output: bool = False, active: bool = False
) -> str:
    label = port.get("label") or port.get("name", "")
    typ = port.get("type") or ""
    head = f"{label} ({typ})" if typ else label
    parts = [head]
    if is_input:
        parts.append("required" if port.get("required") else "optional")
        if port.get("source"):
            parts.append(f"from {port['source']}")
        elif port.get("missing"):
            parts.append("missing")
    elif multi_output:
        parts.append("previewing" if active else "click to preview")
    return " · ".join(parts)


class NodeCard(Viewer):
    """The compact view of a placed component on the wiring canvas.

    The card's parameters follow the component straight away, while its HTML
    is rebuilt at most once per burst of changes in a served session (see
    :class:`~panel_flowdash.ui.node_preview.Coalescer`) and only sent when it
    differs from what the page shows.
    """

    active_output = param.String(
        default=None,
        allow_None=True,
        doc="Effective output port currently previewed, used to mark the eye icon.",
    )

    collapsed = param.Boolean(
        default=False, doc="Whether the card is collapsed to a single 44 px header row."
    )

    icon = param.String(default="widgets", doc="Material icon name.")

    inputs = param.List(
        default=[],
        doc="Input ports as dicts with ``name``, ``label``, ``type``, ``required``, ``connected``, ``missing``, ``source``.",
    )

    outputs = param.List(
        default=[], doc="Output ports as dicts with ``name``, ``label``, ``type``."
    )

    preview_output = param.String(
        default=None,
        allow_None=True,
        doc="User-selected output port to preview, or None for automatic selection.",
    )

    preview_source = param.Callable(
        default=None,
        doc="""
        Returns the card's current preview, or None to show the summary. Only
        called while ``show_preview`` is on and the card is expanded.""",
    )

    show_optional = param.Boolean(
        default=False, doc="Whether folded unconnected optional inputs are temporarily expanded."
    )

    show_preview = param.Boolean(
        default=False, doc="Whether to preview the current value below the ports."
    )

    status = param.Selector(default="ok", objects=list(STATUS_TEXT))

    status_detail = param.String(default="", doc="Why the component has its status.")

    subtitle = param.String(default="", doc="Secondary component type label under the title.")

    summary = param.String(default="", doc="One line describing the current value.")

    title = param.String(default="")

    def __init__(self, *, on_layout: t.Callable[[], None] | None = None, **params):
        # Called when the handles move or change style. It is not a parameter,
        # so the card's repr never shows what it calls.
        self._on_layout = on_layout
        super().__init__(**params)
        self._preview: Preview | None = None
        self._pane = HTML(
            width=CARD_WIDTH,
            height=self.base_height,
            margin=0,
            sizing_mode="fixed",
            stylesheets=[_CARD_CSS],
        )
        self._render_lock = threading.RLock()
        self._generation = 0
        self._coalescer = Coalescer(self._render, self._document)
        immediate = {"show_preview", "collapsed", "show_optional", "preview_output"}
        deferred = [p for p in self.param if p not in immediate and p != "name"]
        self.param.watch(self.refresh, deferred)
        self.param.watch(lambda _event: self._render(), list(immediate))
        self._render()

    def _partition_inputs(self) -> tuple[list[dict], list[dict], bool]:
        """Return ``(visible_inputs, folded_inputs, has_fold_row)``."""
        unconnected_opt = [
            p for p in self.inputs if not p.get("required") and not p.get("connected")
        ]
        can_fold = len(unconnected_opt) >= _FOLD_OPTIONAL_THRESHOLD
        if not can_fold:
            return list(self.inputs), [], False
        if self.show_optional:
            return list(self.inputs), [], True
        folded_names = {p["name"] for p in unconnected_opt}
        visible = [p for p in self.inputs if p["name"] not in folded_names]
        return visible, unconnected_opt, True

    def _visible_row_count(self) -> int:
        if self.collapsed:
            return 0
        visible_in, _, has_fold_row = self._partition_inputs()
        return len(visible_in) + (1 if has_fold_row else 0) + len(self.outputs)

    @property
    def base_height(self) -> int:
        """The card's height without a preview, set by its collapsed state and visible ports."""
        if self.collapsed:
            return _HEADER_HEIGHT
        visible_in, _, has_fold_row = self._partition_inputs()
        return card_height(len(visible_in), len(self.outputs), extra_rows=1 if has_fold_row else 0)

    @property
    def height(self) -> int:
        """The card's current height, including the preview shown."""
        if self.collapsed:
            return _HEADER_HEIGHT
        if self._preview is None:
            return self.base_height
        rows = self._visible_row_count()
        ports_h = (2 * _PORTS_PAD + rows * _PORT_ROW_HEIGHT) if rows else 0
        extra = 10 if self._preview.kind == "value" else 0
        return _HEADER_HEIGHT + ports_h + self._preview.height + extra

    @property
    def preview(self) -> Preview | None:
        """The preview the card shows, if any."""
        return self._preview

    def handle_layout(self) -> tuple[dict[str, dict[str, t.Any]], dict[str, str]]:
        """Return handle metadata (for JS sync) and CSS variable overrides (for ``node['style']``)."""
        handles: dict[str, dict[str, t.Any]] = {}
        css_vars: dict[str, str] = {}
        multi_out = len(self.outputs) > 1
        active = self._effective_output()

        if self.collapsed:
            n_in, n_out = len(self.inputs), len(self.outputs)
            for i, port in enumerate(self.inputs):
                top = _collapsed_top(i, n_in)
                css_vars[f"--fd-port-in-{i}"] = f"{top:.2f}px"
                handles[f"in:{port['name']}"] = {
                    "top": round(top, 2),
                    "color": port_type_color(port.get("type")),
                    "dark_color": port_type_color(port.get("type"), dark=True),
                    "missing": bool(port.get("missing")),
                    "optional": not port.get("required"),
                    "hidden": False,
                    "tooltip": _port_tooltip(port, is_input=True),
                }
            for j, port in enumerate(self.outputs):
                top = _collapsed_top(j, n_out)
                css_vars[f"--fd-port-out-{j}"] = f"{top:.2f}px"
                handles[f"out:{port['name']}"] = {
                    "top": round(top, 2),
                    "color": port_type_color(port.get("type")),
                    "dark_color": port_type_color(port.get("type"), dark=True),
                    "missing": False,
                    "optional": False,
                    "hidden": False,
                    "tooltip": _port_tooltip(
                        port, is_input=False, multi_output=multi_out, active=port["name"] == active
                    ),
                }
            return handles, css_vars

        visible_in, folded_in, has_fold_row = self._partition_inputs()
        folded_names = {p["name"] for p in folded_in}
        start = _HEADER_HEIGHT + _PORTS_PAD
        row_index: dict[str, int] = {}
        k = 0
        for port in visible_in:
            row_index[port["name"]] = k
            k += 1
        fold_row_top = start + (k + 0.5) * _PORT_ROW_HEIGHT
        if has_fold_row:
            k += 1
        for i, port in enumerate(self.inputs):
            if port["name"] in folded_names:
                top = fold_row_top
                hidden = True
            else:
                top = start + (row_index[port["name"]] + 0.5) * _PORT_ROW_HEIGHT
                hidden = False
            css_vars[f"--fd-port-in-{i}"] = f"{top:.2f}px"
            handles[f"in:{port['name']}"] = {
                "top": round(top, 2),
                "color": port_type_color(port.get("type")),
                "dark_color": port_type_color(port.get("type"), dark=True),
                "missing": bool(port.get("missing")),
                "optional": not port.get("required"),
                "hidden": hidden,
                "tooltip": _port_tooltip(port, is_input=True),
            }
        for j, port in enumerate(self.outputs):
            top = start + (k + j + 0.5) * _PORT_ROW_HEIGHT
            css_vars[f"--fd-port-out-{j}"] = f"{top:.2f}px"
            handles[f"out:{port['name']}"] = {
                "top": round(top, 2),
                "color": port_type_color(port.get("type")),
                "dark_color": port_type_color(port.get("type"), dark=True),
                "missing": False,
                "optional": False,
                "hidden": False,
                "tooltip": _port_tooltip(
                    port, is_input=False, multi_output=multi_out, active=port["name"] == active
                ),
            }
        return handles, css_vars

    def _effective_output(self) -> str | None:
        if self.preview_output:
            return self.preview_output
        if self.active_output:
            return self.active_output
        return self.outputs[0]["name"] if self.outputs else None

    def refresh(self, *_events):
        """Rebuild the card, e.g. once the value it previews changed."""
        self._coalescer()

    def release(self):
        """Stop updating the card, e.g. once its component has been removed."""
        self._coalescer.cancel()

    def _document(self):
        """Return the served document showing the card, if any."""
        for model, _ in list(self._pane._models.values()):
            doc = model.document
            if doc is not None and doc.session_context is not None:
                return doc
        return None

    def _ports_html(self) -> str:
        visible_in, folded_in, has_fold_row = self._partition_inputs()
        if not visible_in and not has_fold_row and not self.outputs:
            return ""
        rows: list[str] = []
        for port in visible_in:
            name = html.escape(port["label"])
            # The card ignores the pointer outside its actions, so a tooltip here would never show;
            # the port's handle carries it instead.
            opt_tag = '<span class="fd-popt">optional</span>' if not port.get("required") else ""
            rows.append(
                f'<div class="fd-port fd-in" data-port="{html.escape(port["name"], quote=True)}">'
                f'<span class="fd-name">{name}</span>{opt_tag}</div>'
            )
        if has_fold_row:
            if folded_in:
                count = len(folded_in)
                label = f"{count} optional input{'s' if count != 1 else ''} not connected"
                icon = "expand_more"
                tip = "Show unconnected optional inputs"
            else:
                label = "Hide optional inputs"
                icon = "expand_less"
                tip = "Fold unconnected optional inputs"
            rows.append(
                f'<div class="fd-port fd-port-more fd-card-action" '
                f'data-fd-action="toggle_optional" title="{tip}">'
                f'<span class="fd-ms" aria-hidden="true">{icon}</span>'
                f"<span>{html.escape(label)}</span></div>"
            )
        multi_out = len(self.outputs) > 1
        active = self._effective_output()
        for port in self.outputs:
            name = html.escape(port["label"])
            is_active = multi_out and port["name"] == active
            tooltip = html.escape(
                _port_tooltip(port, is_input=False, multi_output=multi_out, active=is_active),
                quote=True,
            )
            eye = (
                '<span class="fd-ms fd-eye" aria-hidden="true">visibility</span>'
                if is_active
                else ""
            )
            if multi_out:
                cls = "fd-port fd-out fd-card-action" + (" fd-pv-on" if is_active else "")
                rows.append(
                    f'<div class="{cls}" data-port="{html.escape(port["name"], quote=True)}" '
                    f'data-fd-action="select_output" data-fd-port="{html.escape(port["name"], quote=True)}" '
                    f'title="{tooltip}">{eye}<span class="fd-name">{name}</span></div>'
                )
            else:
                rows.append(
                    f'<div class="fd-port fd-out" data-port="{html.escape(port["name"], quote=True)}">'
                    f'<span class="fd-name">{name}</span></div>'
                )
        return f'<div class="fd-ports">{"".join(rows)}</div>'

    def _wants_preview(self) -> bool:
        return (
            not self.collapsed
            and self.show_preview
            and self.preview_source is not None
            and self.status != "failed"
        )

    def _build_preview(self) -> Preview | None:
        try:
            return self.preview_source()
        except Exception:
            logger.debug("Previewing %s failed", self.title, exc_info=True)
            return None

    def _render(self):
        """Repaint the card. In a served session its preview is built on a worker thread.

        A component's preview hook may query a database, so it never runs on
        the event loop: the card is repainted once the preview is built, and
        a preview superseded by a later render is dropped. A card without a
        preview, e.g. a collapsed one, is repainted straight away.
        """
        doc = self._document()
        wanted = self._wants_preview()
        with self._render_lock:
            self._generation += 1
            generation = self._generation
            if wanted and doc is not None:
                run_off_loop(self._build_preview, partial(self._show_preview, generation), doc)
                return
            self._preview = self._settle(self._build_preview()) if wanted else None
            self._render_now()

    def _show_preview(self, generation: int, preview: Preview | None):
        with self._render_lock:
            if generation != self._generation or not self._wants_preview():
                return
            self._preview = self._settle(preview)
            self._render_now()

    def _settle(self, preview: Preview | None) -> Preview | None:
        """Keep the card's height while its value stays the same kind, growing but never shrinking.

        Resizing a card moves its edges and can push its neighbours, so a
        filter that keeps changing a short table's row count must not resize it
        each time.
        """
        old = self._preview
        if preview is None or old is None or old.kind != preview.kind:
            return preview
        if preview.height >= old.height:
            return preview
        return preview.with_height(old.height)

    def _render_now(self):
        preview = self._preview
        detail = self.status_detail or STATUS_TEXT[self.status]
        handles, _ = self.handle_layout()
        collapse_icon = "unfold_more" if self.collapsed else "unfold_less"
        collapse_label = "Expand card" if self.collapsed else "Collapse card"
        sub_text = self.summary if self.collapsed else self.subtitle
        subtitle_html = (
            f'<span class="fd-subtitle" title="{html.escape(sub_text, quote=True)}">'
            f"{html.escape(sub_text)}</span>"
            if sub_text
            else ""
        )
        head = (
            '<div class="fd-head">'
            f'<span class="fd-icon" aria-hidden="true">{html.escape(self.icon)}</span>'
            '<span class="fd-title-block">'
            f'<span class="fd-title" title="{html.escape(self.title, quote=True)}">'
            f"{html.escape(self.title)}</span>{subtitle_html}"
            "</span>"
            f'<span class="fd-dot" role="img" aria-label="{html.escape(detail, quote=True)}" '
            f'title="{html.escape(detail, quote=True)}"></span>'
            f'<button class="fd-collapse-btn fd-card-action" type="button" '
            f'data-fd-action="toggle_collapse" aria-label="{collapse_label}" '
            f'title="{collapse_label}">'
            f'<span class="fd-ms" aria-hidden="true">{collapse_icon}</span></button>'
            "</div>"
        )
        if self.collapsed:
            body = ""
        else:
            ports_html = self._ports_html()
            bottom = (
                preview.html
                if preview is not None
                else f'<div class="fd-summary" title="{html.escape(self.summary, quote=True)}">'
                f"{html.escape(self.summary)}</div>"
            )
            body = ports_html + bottom
        cls = f"fd-card fd-{self.status}" + (" fd-collapsed" if self.collapsed else "")
        markup = f'<div class="{cls}">{head}{body}</div>'
        changes = {"object": markup, "height": self.height}
        changes = {k: v for k, v in changes.items() if getattr(self._pane, k) != v}
        if changes:
            self._pane.param.update(**changes)
        if handles != getattr(self, "_last_handles", None):
            self._last_handles = handles
            if self._on_layout is not None:
                self._on_layout()

    def __panel__(self):
        """Return the card's single view component."""
        return self._pane


def node_status(
    spec: ComponentSpec, state: param.Parameterized, failures: InputFailures | None
) -> tuple[str, str]:
    """Return a placed component's status and a sentence explaining it."""
    failed = failures.failed if failures is not None else {}
    if failed:
        labels = {p.name: port_label(p) for p in spec.inputs}
        names = ", ".join(labels.get(name, name) for name in failed)
        return "failed", f"An upstream step failed, so {names} is stale"
    broken = [port_label(p) for p in spec.outputs if is_failure(getattr(state, p.name, None))]
    if broken:
        return "failed", f"{', '.join(broken)} failed"
    missing = missing_inputs(state, spec.inputs)
    if missing:
        return "waiting", f"Needs {', '.join(missing)}"
    return "ok", STATUS_TEXT["ok"]


def node_summary(
    spec: ComponentSpec, state: param.Parameterized, preferred_output: str | None = None
) -> str:
    """Summarize the selected output holding a value, or for a view the first input holding one."""
    if spec.outputs:
        selected = select_preview_output(spec, state, preferred_output)
        if selected is not None:
            value = getattr(state, selected.name, None)
            if value is not None:
                return _labelled(selected, value)
        for port in spec.outputs:
            value = getattr(state, port.name, None)
            if value is not None:
                return _labelled(port, value)
        return "No output yet"
    for port in spec.inputs:
        value = getattr(state, port.name, None)
        if value is not None:
            return f"Showing {_labelled(port, value)}"
    return "No input yet" if spec.inputs else ""


def _labelled(port, value) -> str:
    text = summarize_value(value)
    if (
        getattr(value, "shape", None) is not None
        or isinstance(value, (list, tuple, dict))
        or text == type(value).__name__
    ):
        return text
    return f"{port_label(port)}: {text}"


def _port_dicts(ports, inputs: bool, state: param.Parameterized | None = None) -> list[dict]:
    result = []
    for port in ports:
        entry: dict[str, t.Any] = {
            "name": port.name,
            "label": port_label(port),
            "type": port.type or "",
        }
        if inputs:
            entry["required"] = port.required
            entry["connected"] = False
            entry["missing"] = bool(
                port.required and state is not None and getattr(state, port.name, None) is None
            )
            entry["source"] = ""
        result.append(entry)
    return result


def node_preview(
    spec: ComponentSpec,
    state: param.Parameterized,
    instance: t.Any = None,
    output: str | None = None,
) -> Preview | None:
    """Preview a placed component: its own preview, its selected output, or a view's input."""
    selected = select_preview_output(spec, state, output) if spec.outputs else None
    out_name = selected.name if selected is not None else output
    if instance is not None and (preview := hook_preview(instance, output=out_name)) is not None:
        return preview
    if selected is not None:
        value = getattr(state, selected.name, None)
        if value is not None:
            return build_preview(value, port_label(selected), showing=False)
    ports = spec.outputs or spec.inputs
    for port in ports:
        value = getattr(state, port.name, None)
        if value is not None:
            return build_preview(value, port_label(port), showing=not spec.outputs)
    return None


def node_preview_value(
    spec: ComponentSpec,
    state: param.Parameterized,
    instance: t.Any = None,
    output: str | None = None,
) -> tuple[str | None, t.Any]:
    """Return ``(port_name, raw_value)`` for the node's currently previewed output (for pop-out)."""
    selected = select_preview_output(spec, state, output) if spec.outputs else None
    out_name = selected.name if selected is not None else output
    if instance is not None:
        hooked = call_preview_hook(instance, output=out_name)
        if hooked is not None and not (isinstance(hooked, str) and not hooked.strip()):
            return out_name, hooked
    if selected is not None:
        value = getattr(state, selected.name, None)
        if value is not None:
            return selected.name, value
    for port in spec.outputs or spec.inputs:
        value = getattr(state, port.name, None)
        if value is not None:
            return port.name, value
    return out_name, None


def _display_titles(spec: ComponentSpec, instance_id: str | None) -> tuple[str, str]:
    """Return ``(primary_title, subtitle)`` for a card header.

    A node with a name of its own, e.g. ``turbines_by_year``, is titled by it
    above the component's title; one with an id the editor generated is
    titled by its component.
    """
    from panel_flowdash.agent.yaml_format import own_name

    if instance_id and own_name(instance_id) == instance_id:
        subtitle = spec.title if spec.title != instance_id else ""
        return instance_id, subtitle
    return spec.title, ""


def node_card(
    spec: ComponentSpec,
    state: param.Parameterized,
    failures: InputFailures | None,
    wiring: list[t.Callable[[], None]],
    instance: t.Any = None,
    show_preview: bool = False,
    *,
    instance_id: str | None = None,
    preview_output: str | None = None,
    collapsed: bool = False,
    on_layout: t.Callable[[], None] | None = None,
) -> NodeCard:
    """Build the card of a placed component, following its state until *wiring* is released."""
    title, subtitle = _display_titles(spec, instance_id)
    selected = select_preview_output(spec, state, preview_output) if spec.outputs else None

    card = NodeCard(
        title=title,
        subtitle=subtitle,
        icon=component_icon(spec),
        inputs=_port_dicts(spec.inputs, True, state),
        outputs=_port_dicts(spec.outputs, False),
        show_preview=show_preview,
        preview_output=preview_output,
        active_output=selected.name if selected is not None else None,
        collapsed=collapsed,
        on_layout=on_layout,
    )

    def build_current_preview() -> Preview | None:
        return node_preview(spec, state, instance, output=card.preview_output)

    card.preview_source = build_current_preview

    def update(*_events):
        status, detail = node_status(spec, state, failures)
        chosen = select_preview_output(spec, state, card.preview_output) if spec.outputs else None
        updated_inputs = [
            {
                **p,
                "missing": bool(p.get("required") and getattr(state, p["name"], None) is None),
            }
            for p in card.inputs
        ]
        updates: dict[str, t.Any] = {
            "status": status,
            "status_detail": detail,
            "summary": node_summary(spec, state, card.preview_output),
            "active_output": chosen.name if chosen is not None else None,
        }
        if updated_inputs != card.inputs:
            updates["inputs"] = updated_inputs
        card.param.update(**updates)
        # The value may have changed without its summary, e.g. a table's cells.
        card.refresh()

    names = [p.name for p in (*spec.outputs, *spec.inputs) if p.name in state.param]
    if names:
        watcher = state.param.watch(update, names)
        wiring.append(partial(state.param.unwatch, watcher))
    if failures is not None:
        watcher = failures.param.watch(update, "failed")
        wiring.append(partial(failures.param.unwatch, watcher))
    if instance is not None and callable(getattr(instance, PREVIEW_HOOK, None)):
        # The ports are followed through the node state already.
        ports = {p.name for p in (*spec.inputs, *spec.outputs)}
        own = [name for name in instance.param if name != "name" and name not in ports]
        if own:
            watcher = instance.param.watch(card.refresh, own)
            wiring.append(partial(instance.param.unwatch, watcher))
    watcher = card.param.watch(lambda _e: update(), "preview_output")
    wiring.append(partial(card.param.unwatch, watcher))
    wiring.append(card.release)
    update()
    return card


def placeholder_card(placeholder: Placeholder) -> NodeCard:
    """Build the card of a saved item that could not be placed."""
    return NodeCard(
        title=placeholder.title,
        icon="lock" if placeholder.private else "extension_off",
        status="unavailable",
        status_detail=placeholder.message,
        summary=placeholder.message,
    )
