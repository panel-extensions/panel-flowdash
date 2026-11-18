"""Compact canvas nodes: a card naming a component, its typed ports and its current value.

The live component is shown only in the tile grid, so the same view is never
rendered twice and a table or map is never squeezed into a node. A card lists
the component's ports with their types, marks required inputs, shows a status
dot and either summarizes the current value in one line, e.g. the rows of a
DataFrame output or a widget's selection, or previews it (see
``ui/node_preview.py``): the first rows of a table, a sparkline, a few chips.

Port rows are spread evenly down a card without a preview, at
``(i + 1) / (n + 1)`` of its height, and the card is tall enough that the
header and summary never meet a label whatever the number of ports. A preview
makes the card taller below the ports without moving them, so each handle is
placed at its row in pixels (see :func:`port_tops`), with or without one.
"""

from __future__ import annotations

import html
import logging
import threading
import typing as t
from functools import partial

import param
from panel.pane import HTML
from panel.viewable import Viewer

from panel_flowdash.ui.node_preview import (
    PREVIEW_CSS,
    PREVIEW_HOOK,
    Coalescer,
    Preview,
    build_preview,
    hook_preview,
    summarize_value,
)
from panel_flowdash.ui.roles import ROLE_ICONS
from panel_flowdash.util import is_failure, missing_inputs

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec, InputPort, OutputPort
    from panel_flowdash.dataflow_engine import InputFailures
    from panel_flowdash.placeholders import Placeholder

logger = logging.getLogger(__name__)

__all__ = ["CARD_WIDTH", "NodeCard", "card_height", "port_offsets", "summarize_value"]

CARD_WIDTH = 240
_HEADER_HEIGHT = 34
_SUMMARY_HEIGHT = 30
_ROW_HEIGHT = 24
# The spacing between port rows, chosen so the first and last rows clear the
# header and summary however many ports there are.
_ROW_PITCH = max(_HEADER_HEIGHT, _SUMMARY_HEIGHT) + _ROW_HEIGHT // 2

STATUS_TEXT = {
    "ok": "Up to date",
    "waiting": "Waiting for input",
    "failed": "Failed",
    "unavailable": "Unavailable",
}

_CARD_CSS = (
    """
:host {
  --fd-text: var(--mui-palette-text-primary, var(--panel-on-background-color, #1f1f1f));
  --fd-muted: var(--mui-palette-text-secondary, rgba(127, 127, 127, 0.95));
  --fd-divider: var(--mui-palette-divider, rgba(127, 127, 127, 0.25));
  --fd-primary: var(--mui-palette-primary-main, var(--panel-primary-color, #0072b5));
  --fd-surface: var(--mui-palette-action-hover, rgba(127, 127, 127, 0.08));
}
.fd-card {
  position: relative;
  width: 100%;
  height: 100%;
  color: var(--fd-text);
  font-size: 12px;
  line-height: 16px;
}
.fd-head {
  position: absolute;
  inset: 0 0 auto 0;
  height: 34px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  border-bottom: 1px solid var(--fd-divider);
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
.fd-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
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
.fd-port {
  position: absolute;
  height: 20px;
  display: flex;
  align-items: center;
  gap: 4px;
  transform: translateY(-50%);
  white-space: nowrap;
  overflow: hidden;
  color: var(--fd-muted);
}
.fd-in { left: 12px; }
.fd-out { right: 12px; justify-content: flex-end; }
.fd-name { color: var(--fd-text); overflow: hidden; text-overflow: ellipsis; }
.fd-type { font-size: 11px; flex: none; }
.fd-required { color: var(--mui-palette-error-main, #d32f2f); flex: none; }
.fd-summary {
  position: absolute;
  inset: auto 8px 6px 8px;
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
}
.fd-failed .fd-summary { color: var(--mui-palette-error-main, #d32f2f); }
"""
    + PREVIEW_CSS
)


def card_height(n_inputs: int, n_outputs: int) -> int:
    """Return the height of a card with this many inputs and outputs."""
    rows = max(n_inputs, n_outputs)
    return (rows + 1) * _ROW_PITCH if rows else _HEADER_HEIGHT + _SUMMARY_HEIGHT + 8


def port_offsets(count: int) -> list[float]:
    """Return where *count* port rows sit on a card without a preview, as percentages of its height."""
    return [(i + 1) * 100 / (count + 1) for i in range(count)]


def port_tops(n_inputs: int, n_outputs: int) -> tuple[list[float], list[float]]:
    """Return the centres of a card's input and output rows, in px from its top."""
    height = card_height(n_inputs, n_outputs)
    return (
        [height * p / 100 for p in port_offsets(n_inputs)],
        [height * p / 100 for p in port_offsets(n_outputs)],
    )


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


class NodeCard(Viewer):
    """The compact view of a placed component on the wiring canvas.

    The card's parameters follow the component straight away, while its HTML
    is rebuilt at most once per burst of changes in a served session (see
    :class:`~panel_flowdash.ui.node_preview.Coalescer`) and only sent when it
    differs from what the page shows.
    """

    icon = param.String(default="widgets", doc="Material icon name.")

    inputs = param.List(
        default=[], doc="Input ports as dicts with ``name``, ``label``, ``type`` and ``required``."
    )

    outputs = param.List(
        default=[], doc="Output ports as dicts with ``name``, ``label``, ``type``."
    )

    preview_source = param.Callable(
        default=None,
        doc="""
        Returns the card's current preview, or None to show the summary. Only
        called while ``show_preview`` is on, once per update.""",
    )

    show_preview = param.Boolean(
        default=False, doc="Whether to preview the current value below the ports."
    )

    status = param.Selector(default="ok", objects=list(STATUS_TEXT))

    status_detail = param.String(default="", doc="Why the component has its status.")

    summary = param.String(default="", doc="One line describing the current value.")

    title = param.String(default="")

    def __init__(self, **params):
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
        self._coalescer = Coalescer(self._render, self._document)
        self.param.watch(self.refresh, [p for p in self.param if p != "show_preview"])
        # Turning previews on or off is one change for the whole canvas, sent at once.
        self.param.watch(lambda _event: self._render(), "show_preview")
        self._render()

    @property
    def base_height(self) -> int:
        """The card's height without a preview, set by its number of ports."""
        return card_height(len(self.inputs), len(self.outputs))

    @property
    def height(self) -> int:
        """The card's current height, including the preview shown."""
        if self._preview is None:
            return self.base_height
        return self.base_height - _SUMMARY_HEIGHT + 8 + self._preview.height

    @property
    def preview(self) -> Preview | None:
        """The preview the card shows, if any."""
        return self._preview

    def refresh(self, *_events):
        """Rebuild the card, e.g. once the value it previews changed."""
        self._coalescer()

    def release(self):
        """Stop updating the card, e.g. once its component has been removed."""
        self._coalescer.cancel()

    def _document(self):
        """Return the served document showing the card, if any."""
        # A copy, since the event loop may add or remove the pane's models while another thread asks.
        for model, _ in list(self._pane._models.values()):
            doc = model.document
            if doc is not None and doc.session_context is not None:
                return doc
        return None

    def _ports_html(self, ports: list[dict], tops: list[float], side: str, full: bool) -> str:
        width = "calc(100% - 24px)" if full else "calc(50% - 16px)"
        rows = []
        for port, top in zip(ports, tops, strict=True):
            name = html.escape(port["label"])
            kind = html.escape(port.get("type") or "")
            required = (
                '<span class="fd-required" title="Required">*</span>'
                if port.get("required")
                else ""
            )
            rows.append(
                f'<div class="fd-port fd-{side}" data-port="{html.escape(port["name"])}" '
                f'style="top: {top:.2f}px; max-width: {width}">'
                f'<span class="fd-name">{name}</span>{required}'
                f'<span class="fd-type">{kind}</span></div>'
            )
        return "".join(rows)

    def _build_preview(self) -> Preview | None:
        if not self.show_preview or self.preview_source is None or self.status == "failed":
            return None
        try:
            return self.preview_source()
        except Exception:
            logger.debug("Previewing %s failed", self.title, exc_info=True)
            return None

    def _render(self):
        # Turning previews on or off renders from the calling thread, maybe while the loop renders too.
        with self._render_lock:
            self._render_now()

    def _render_now(self):
        self._preview = preview = self._build_preview()
        detail = self.status_detail or STATUS_TEXT[self.status]
        tops_in, tops_out = port_tops(len(self.inputs), len(self.outputs))
        bottom = (
            preview.html
            if preview is not None
            else f'<div class="fd-summary" title="{html.escape(self.summary)}">'
            f"{html.escape(self.summary)}</div>"
        )
        markup = (
            f'<div class="fd-card fd-{self.status}">'
            '<div class="fd-head">'
            f'<span class="fd-icon" aria-hidden="true">{html.escape(self.icon)}</span>'
            f'<span class="fd-title" title="{html.escape(self.title)}">'
            f"{html.escape(self.title)}</span>"
            f'<span class="fd-dot" role="img" aria-label="{html.escape(detail)}" '
            f'title="{html.escape(detail)}"></span>'
            "</div>"
            f"{self._ports_html(self.inputs, tops_in, 'in', not self.outputs)}"
            f"{self._ports_html(self.outputs, tops_out, 'out', not self.inputs)}"
            f"{bottom}</div>"
        )
        changes = {"object": markup, "height": self.height}
        changes = {k: v for k, v in changes.items() if getattr(self._pane, k) != v}
        if changes:
            self._pane.param.update(**changes)

    def __panel__(self):
        """Return the card's single HTML view."""
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


def node_summary(spec: ComponentSpec, state: param.Parameterized) -> str:
    """Summarize the first output holding a value, or for a view the first input holding one."""
    for port in spec.outputs:
        value = getattr(state, port.name, None)
        if value is not None:
            return _labelled(port, value)
    if spec.outputs:
        return "No output yet"
    for port in spec.inputs:
        value = getattr(state, port.name, None)
        if value is not None:
            return f"Showing {_labelled(port, value)}"
    return "No input yet" if spec.inputs else ""


def _labelled(port, value) -> str:
    text = summarize_value(value)
    if getattr(value, "shape", None) is not None or isinstance(value, (list, tuple, dict)):
        return text
    return f"{port_label(port)}: {text}"


def _port_dicts(ports, inputs: bool) -> list[dict]:
    return [
        {
            "name": port.name,
            "label": port_label(port),
            "type": port.type or "",
            **({"required": port.required} if inputs else {}),
        }
        for port in ports
    ]


def node_preview(
    spec: ComponentSpec, state: param.Parameterized, instance: t.Any = None
) -> Preview | None:
    """Preview a placed component: its own preview, its first output, or a view's input.

    The first output holding a value is previewed, or for a view the first
    input holding one, as the summary describes. A component defining
    ``__flowdash_preview__`` supplies its own (see
    :func:`~panel_flowdash.ui.node_preview.hook_preview`).
    """
    if instance is not None and (preview := hook_preview(instance)) is not None:
        return preview
    ports = spec.outputs or spec.inputs
    for port in ports:
        value = getattr(state, port.name, None)
        if value is not None:
            return build_preview(value, port_label(port), showing=not spec.outputs)
    return None


def node_card(
    spec: ComponentSpec,
    state: param.Parameterized,
    failures: InputFailures | None,
    wiring: list[t.Callable[[], None]],
    instance: t.Any = None,
    show_preview: bool = False,
) -> NodeCard:
    """Build the card of a placed component, following its state until *wiring* is released.

    Parameters
    ----------
    spec
        The component's spec, for its title, icon and ports.
    state
        The node state holding the values of its ports.
    failures
        The failures held by its inputs, if failures are carried along edges.
    wiring
        Releases run when the component leaves the editor; the card's
        watchers are appended to it.
    instance
        The live component, whose ``__flowdash_preview__`` hook, if it has
        one, supplies the preview. Any change to its parameters refreshes it.
    show_preview
        Whether the card starts out previewing the current value.
    """
    card = NodeCard(
        title=spec.title,
        icon=component_icon(spec),
        inputs=_port_dicts(spec.inputs, True),
        outputs=_port_dicts(spec.outputs, False),
        preview_source=partial(node_preview, spec, state, instance),
        show_preview=show_preview,
    )

    def update(*_events):
        status, detail = node_status(spec, state, failures)
        card.param.update(status=status, status_detail=detail, summary=node_summary(spec, state))
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
