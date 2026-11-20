"""The inspector, a persistent panel beside the wiring canvas bound to its selection.

With a component selected it shows a compact one-line header with status dot and
overflow menu, its settings, live input summaries, current outputs and tile
options. With a connection selected it describes the connection and edits its
transform steps (see :meth:`Inspector._edge_view`). With nothing selected it
summarizes the dashboard and lists its issues.
"""

from __future__ import annotations

import copy
import html
import typing as t
import weakref
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.config_check import CheckResult
from panel_flowdash.config_choices import Choices, columns_of, has_choices, node_choices
from panel_flowdash.groups import component_nodes, group_id_of
from panel_flowdash.input_literals import takes_literal
from panel_flowdash.transforms import parse_transform, summarize_transform
from panel_flowdash.ui.choice_widgets import form_widgets, setting_choices, widgets_key
from panel_flowdash.ui.code_field import CodeField
from panel_flowdash.ui.config_draft import ConfigDraft
from panel_flowdash.ui.input_editor import LiteralEditor
from panel_flowdash.ui.node_card import (
    STATUS_TEXT,
    component_icon,
    node_status,
    port_label,
    summarize_value,
)
from panel_flowdash.ui.node_preview import PREVIEW_HOOK, describe_object, format_value
from panel_flowdash.ui.spec_field import SpecEditor
from panel_flowdash.ui.status_bar import (
    CanvasWatcher,
    canvas_issues,
    connection_title,
    select_on_canvas,
)
from panel_flowdash.ui.text import text_alert, text_typography
from panel_flowdash.ui.tile_layout import (
    AREA_NAMES,
    FRAME_DEFAULT,
    GRID,
    area_of,
    frame_choices,
    frame_value,
    move_buttons,
    tile_entry,
    update_tile,
)
from panel_flowdash.util import is_failure

if t.TYPE_CHECKING:
    from panel.viewable import Viewable

    from panel_flowdash.component_spec import ComponentSpec

INSPECTOR_WIDTH = 360
_COLLAPSED_WIDTH = 44

_STATUS_COLOR = {
    "ok": "success",
    "waiting": "warning",
    "failed": "error",
    "unavailable": "default",
    "held": "warning",
    "refused": "error",
}

_WIDTHS = {"Full width": 100.0, "Half": 50.0, "Third": 33.33, "Quarter": 25.0}
_AUTO = "auto"
_HEIGHTS = (200, 300, 400, 600, 800)
_MAX_ISSUES = 20
_MAX_SUMMARY_ITEMS = 3
_MAX_CHIPS = 6
_MAX_CHIP_LEN = 24

_TABLE_CSS = """
:host { overflow-x: auto; }
table.panel-df { font-size: 11px; }
.panel-df th, .panel-df td { white-space: nowrap; padding: 2px 6px; }
"""

_ICON_CSS = """
.fd-avatar { display: flex; align-items: center; justify-content: center; width: 36px;
  height: 36px; border-radius: 8px;
  background: var(--mui-palette-primary-main, #0072b5);
  color: var(--mui-palette-primary-contrastText, #fff); }
.fd-avatar span { font-family: "Material Icons"; font-size: 20px; line-height: 1;
  font-feature-settings: "liga"; }
"""

_HEADER_CSS = """
.fd-insp-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  color: var(--mui-palette-primary-main, #0072b5);
}
.fd-insp-icon span {
  font-family: "Material Icons";
  font-size: 20px;
  line-height: 1;
  font-feature-settings: "liga";
}
.fd-info-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  font-family: "Material Icons";
  font-size: 15px;
  line-height: 1;
  color: var(--mui-palette-text-disabled, #9e9e9e);
  cursor: default;
  font-feature-settings: "liga";
}
.fd-status-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
}
.fd-status-dot {
  display: block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.fd-status-success { background: var(--mui-palette-success-main, #2e7d32); }
.fd-status-warning { background: var(--mui-palette-warning-main, #ed6c02); }
.fd-status-error { background: var(--mui-palette-error-main, #d32f2f); }
.fd-status-default { background: var(--mui-palette-text-disabled, #9e9e9e); }
"""

_INPUT_DATA_CSS = """
.fd-inp-data {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  line-height: 16px;
  color: var(--mui-palette-text-secondary, #666);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-inp-data .fd-mi {
  flex: none;
  font-family: "Material Icons";
  font-size: 14px;
  line-height: 1;
  color: var(--mui-palette-text-disabled, #888);
  font-feature-settings: "liga";
}
.fd-inp-data .fd-inp-text {
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.fd-inp-data code {
  font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--mui-palette-text-primary, inherit);
}
.fd-inp-chips {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  min-width: 0;
}
.fd-inp-chip {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 10px;
  background: var(--mui-palette-action-selected, rgba(128, 128, 128, 0.14));
  color: var(--mui-palette-text-primary, inherit);
  font-size: 11px;
  line-height: 18px;
  font-weight: 500;
  white-space: nowrap;
  max-width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-inp-err { color: var(--mui-palette-error-main, #d32f2f); }
"""

_OVERFLOW_SX = {
    "minWidth": 0,
    "width": "28px",
    "height": "28px",
    "p": 0,
    "color": "text.secondary",
    "flexShrink": 0,
    "& .MuiButton-startIcon": {"m": 0},
    "& .MuiButton-endIcon": {"display": "none"},
}

_HEADER_ICON_SX = {
    "color": "text.secondary",
    "width": "28px",
    "height": "28px",
    "p": 0,
    "flexShrink": 0,
}

# MenuButton items take no colour, so the destructive last entry is styled by position.
_DANGER_LAST_ITEM = """
li.MuiMenuItem-root:last-of-type {
  color: var(--mui-palette-error-main);
}
li.MuiMenuItem-root:last-of-type:not(:first-of-type) {
  border-top: 1px solid var(--mui-palette-divider);
  margin-top: 4px;
  padding-top: 8px;
}
"""

_SECTION_SX = {
    "color": "text.secondary",
    "fontWeight": 500,
    "fontSize": "0.6875rem",
    "letterSpacing": "0.5px",
    "textTransform": "uppercase",
}

_TABS_SX = {
    "& .MuiTabs-root": {
        "minHeight": "36px",
        "px": "6px",
        "borderBottom": 1,
        "borderColor": "divider",
    },
    "& .MuiTab-root": {
        "textTransform": "none",
        "minWidth": 0,
        "minHeight": "36px",
        "py": "6px",
        "px": "10px",
        "fontSize": "0.8125rem",
    },
}


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _caption(text: str, sx: dict | None = None, **kwargs) -> pmui.Typography:
    style = {"color": "text.secondary"}
    if sx:
        style.update(sx)
    return text_typography(text, variant="caption", sx=style, **kwargs)


def _heading(text: str, margin=(16, 0, 4, 0)) -> pmui.Typography:
    return text_typography(text, variant="subtitle2", margin=margin, sx=_SECTION_SX)


def _avatar(icon: str) -> pn.pane.HTML:
    return pn.pane.HTML(
        f'<div class="fd-avatar"><span aria-hidden="true">{_esc(icon)}</span></div>',
        width=36,
        height=36,
        margin=(2, 12, 0, 0),
        stylesheets=[_ICON_CSS],
    )


def _header_icon(icon: str) -> pn.pane.HTML:
    return pn.pane.HTML(
        f'<div class="fd-insp-icon"><span aria-hidden="true">{_esc(icon)}</span></div>',
        width=22,
        height=22,
        margin=0,
        align="center",
        stylesheets=[_HEADER_CSS],
        styles={"flex": "0 0 auto"},
    )


def _code_list(names: t.Sequence[str], limit: int = _MAX_SUMMARY_ITEMS) -> str:
    shown = [f"<code>{_esc(str(n))}</code>" for n in names[:limit]]
    suffix = " …" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def _is_table(value: t.Any) -> bool:
    shape = getattr(value, "shape", None)
    return (
        isinstance(shape, tuple)
        and len(shape) == 2
        and hasattr(value, "columns")
        and hasattr(value, "head")
    )


def _is_raw_repr(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def _source_tables(value: t.Any) -> list[str] | None:
    getter = getattr(value, "get_tables", None)
    if callable(getter):
        try:
            tables = getter()
            if isinstance(tables, (list, tuple, set)):
                return [str(t_) for t_ in tables]
            if isinstance(tables, dict):
                return [str(k) for k in tables]
        except Exception:
            return None
    tables = getattr(value, "tables", None)
    if isinstance(tables, dict) and tables:
        return [str(k) for k in tables]
    if isinstance(tables, (list, tuple)) and tables and all(isinstance(x, str) for x in tables):
        return list(tables)
    return None


def _table_summary_html(rows: int | None, cols: t.Sequence[str]) -> str:
    parts: list[str] = []
    if rows is not None:
        parts.append(f"{rows:,} rows \u00d7 {len(cols)}")
    else:
        parts.append(f"{len(cols):,} {'column' if len(cols) == 1 else 'columns'}")
    if cols:
        parts.append(_code_list(cols))
    body = " · ".join(parts)
    return (
        '<div class="fd-inp-data">'
        '<span class="fd-mi" aria-hidden="true">table_chart</span>'
        f'<span class="fd-inp-text">{body}</span></div>'
    )


def _chips_summary_html(items: t.Sequence[t.Any]) -> str:
    if not items:
        return (
            '<div class="fd-inp-data">'
            '<span class="fd-mi" aria-hidden="true">checklist</span>'
            '<span class="fd-inp-text">Empty</span></div>'
        )
    formatted = [format_value(item, _MAX_CHIP_LEN) for item in items[:_MAX_CHIPS]]
    chips = "".join(
        f'<span class="fd-inp-chip" title="{_esc(text)}">{_esc(text)}</span>' for text in formatted
    )
    if len(items) > _MAX_CHIPS:
        chips += f'<span class="fd-inp-chip">+{len(items) - _MAX_CHIPS}</span>'
    return (
        '<div class="fd-inp-data">'
        '<span class="fd-mi" aria-hidden="true">checklist</span>'
        f'<span class="fd-inp-chips">{chips}</span></div>'
    )


def summarize_input_html(
    value: t.Any,
    port_type: str | None = None,
    source_instance: t.Any = None,
) -> str:
    """Return a compact escaped HTML summary line for a connected input value."""
    if value is None:
        return '<div class="fd-inp-data"><span class="fd-inp-text">No value yet</span></div>'
    if is_failure(value):
        return (
            '<div class="fd-inp-data fd-inp-err">'
            '<span class="fd-mi" aria-hidden="true">error</span>'
            '<span class="fd-inp-text">Failed</span></div>'
        )
    if _is_table(value):
        cols = [str(c) for c in value.columns]
        return _table_summary_html(int(value.shape[0]), cols)

    try:
        cols_info = columns_of(value, port_type)
    except Exception:
        cols_info = None
    if cols_info:
        cols = [name for name, _ in cols_info]
        shape = getattr(value, "shape", None)
        rows = (
            int(shape[0])
            if isinstance(shape, tuple) and len(shape) == 2 and isinstance(shape[0], int)
            else None
        )
        return _table_summary_html(rows, cols)

    tables = _source_tables(value)
    if tables:
        kind = _esc(type(value).__name__)
        return (
            '<div class="fd-inp-data">'
            '<span class="fd-mi" aria-hidden="true">storage</span>'
            f'<span class="fd-inp-text">{kind} · {_code_list(tables)}</span></div>'
        )

    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        if not seq or all(
            len(format_value(x, _MAX_CHIP_LEN)) <= _MAX_CHIP_LEN for x in seq[:_MAX_CHIPS]
        ):
            return _chips_summary_html(seq)

    if isinstance(value, dict):
        count = len(value)
        label = f"{count:,} {'entry' if count == 1 else 'entries'}"
        keys = _code_list([str(k) for k in list(value)[:_MAX_SUMMARY_ITEMS]]) if count else ""
        body = f"{label} · {keys}" if keys else label
        return (
            '<div class="fd-inp-data">'
            '<span class="fd-mi" aria-hidden="true">data_object</span>'
            f'<span class="fd-inp-text">{body}</span></div>'
        )

    described = describe_object(value)
    if described is not None:
        return f'<div class="fd-inp-data"><span class="fd-inp-text">{_esc(described)}</span></div>'

    preview_hook = getattr(value, PREVIEW_HOOK, None)
    if callable(preview_hook):
        try:
            preview_val = preview_hook()
        except Exception:
            preview_val = None
        if preview_val is not None and preview_val is not value:
            if isinstance(preview_val, str) and preview_val.strip():
                return (
                    '<div class="fd-inp-data">'
                    f'<span class="fd-inp-text">{_esc(preview_val.strip())}</span></div>'
                )
            if _is_table(preview_val):
                cols = [str(c) for c in preview_val.columns]
                return _table_summary_html(None, cols)

    text = summarize_value(value)
    if _is_raw_repr(text) and source_instance is not None:
        src_hook = getattr(source_instance, PREVIEW_HOOK, None)
        if callable(src_hook):
            try:
                src_preview = src_hook()
            except Exception:
                src_preview = None
            if isinstance(src_preview, str) and src_preview.strip():
                return (
                    '<div class="fd-inp-data">'
                    f'<span class="fd-inp-text">{_esc(src_preview.strip())}</span></div>'
                )
            if _is_table(src_preview):
                cols = [str(c) for c in src_preview.columns]
                return _table_summary_html(None, cols)
    if _is_raw_repr(text):
        text = type(value).__name__

    return (
        '<div class="fd-inp-data">'
        f'<span class="fd-inp-text" title="{_esc(text)}">{_esc(text)}</span></div>'
    )


class Inspector(Viewer):
    """A collapsible panel beside the canvas showing what is selected on it."""

    collapsed = param.Boolean(default=False, doc="Whether the panel is folded to a narrow strip.")

    editor = param.Parameter(doc="The FlowDash editor whose canvas selection is inspected.")

    visible = param.Boolean(default=True, doc="Whether the panel is shown.")

    on_edit_code = param.Callable(
        default=None,
        doc="""
        Optional callback invoked as ``on_edit_code(instance_id, field_name, field)``
        when Edit is clicked on a code field in the inspector.""",
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._flow = self.editor._flow
        # Watchers following the shown component, released when it is replaced.
        self._releases: list[t.Callable[[], None]] = []
        self._shown: tuple[str, str] | None = None
        self._status_dot: pmui.Tooltip | None = None
        self._status_dot_pane: pn.pane.HTML | None = None
        self._status_info: tuple[str, str] = ("Up to date", "success")
        self._node_status: tuple[str, str] | None = None
        self._config_status: tuple[str, str] | None = None
        self._last_check_status: tuple[str, str] | None = None
        self._code_fields: list[CodeField] = []
        self._has_inline_close = False
        self._data_column: pn.Column | None = None
        self._inputs_column: pn.Column | None = None
        # What the shown input rows depend on, and their parts updated in place.
        self._inputs_key: dict[str, tuple] = {}
        self._connected_caption: pmui.Typography | None = None
        self._literal_editors: dict[str, LiteralEditor] = {}
        self._literal_captions: dict[str, pmui.Typography] = {}
        self._value_panes: dict[str, pn.pane.HTML] = {}
        # The Move to buttons of the shown Tile tab.
        self._tile_move_buttons: list = []
        self._refresh_inputs: t.Callable[[], None] = lambda: None
        # The latest config check started per node, see ConfigDraft.
        self._check_runs: dict[str, int] = {}
        self._check_results: dict[str, tuple[dict, tuple, CheckResult]] = {}
        self._draft: ConfigDraft | None = None
        # The choices last offered per node, so a form shown again starts with them.
        self._choices: dict[str, dict[str, Choices]] = {}
        self._choice_holder: pn.Column | None = None
        # The form whose choices are being asked for, and whether to ask again after.
        self._choices_busy: pn.Column | None = None
        self._choices_again = False
        self._form_key: tuple = ()

        self.collapse_button = pmui.IconButton(
            icon="keyboard_double_arrow_right",
            description="Hide inspector",
            size="small",
            margin=(8, 4, 0, 0),
            align="end",
        )
        self.expand_button = pmui.IconButton(
            icon="keyboard_double_arrow_left",
            description="Show inspector",
            size="small",
            margin=(8, 4),
        )
        self.collapse_button.on_click(lambda _event: setattr(self, "collapsed", True))
        self.expand_button.on_click(lambda _event: setattr(self, "collapsed", False))
        # A Material UI column follows its contents' height as a form grows, e.g. by
        # a row of thresholds, so what follows the tabs moves down with it.
        self._body = pmui.Column(sizing_mode="stretch_width", margin=(0, 16, 16, 16))
        self._panel = pmui.Column(
            self.collapse_button,
            self._body,
            self.expand_button,
            width=INSPECTOR_WIDTH,
            sizing_mode="stretch_height",
            scroll="y-auto",
            margin=0,
            visible=self.param.visible,
            sx={"borderLeft": 1, "borderColor": "divider", "bgcolor": "background.paper"},
        )
        self._sync_collapsed()
        self._flow.param.watch(self._on_selection, "selection")
        self._watcher = CanvasWatcher(self._flow, self._on_canvas_changed)

    # ------------------------------------------------------------------
    # What is shown
    # ------------------------------------------------------------------

    def _selected(self) -> tuple[str, t.Any] | None:
        selection = self._flow.selection or {}
        nodes, edges = selection.get("nodes") or [], selection.get("edges") or []
        components = tuple(n for n in nodes if group_id_of(n) is None)
        if len(nodes) == 1 and not edges and (group_id := group_id_of(nodes[0])):
            return ("group", group_id)
        if len(components) > 1 and not edges:
            return ("nodes", components)
        if len(nodes) == 1 and not edges:
            return ("node", nodes[0])
        if len(edges) == 1 and not nodes:
            return ("edge", edges[0])
        return None

    def _on_selection(self, _event):
        if self.visible:
            self.refresh()

    @param.depends("visible", watch=True)
    def _sync_visible(self):
        # Hidden, it holds nothing, so it neither follows the canvas nor
        # duplicates text that is on screen elsewhere.
        if self.visible:
            self.refresh(force=True)
        else:
            self._release()
            self._shown = None
            self._body[:] = []

    def _on_canvas_changed(self):
        # A summary is rebuilt on any change; a component's view follows its own
        # state, so a form being typed into is not rebuilt.
        if not self.visible:
            return
        if self._shown is None or self._shown != self._selected() or not self._exists():
            self.refresh(force=True)
        else:
            if self._inputs_column is not None:
                self._refresh_inputs()
            if self._draft is not None:
                self._draft.rewired()

    def _exists(self) -> bool:
        kind, item_id = self._shown or ("", "")
        placed = {item["instance_id"] for item in self.editor._tile_items}
        if kind == "node":
            return item_id in placed
        if kind == "nodes":
            return all(i in placed for i in item_id)
        if kind == "group":
            return item_id in self.editor._groups
        return item_id in self.editor._edge_id_map

    @pn.io.hold()
    def refresh(self, force: bool = False):
        """Show the selected component or connection, or the dashboard summary."""
        selected = self._selected()
        if selected == self._shown and not force and selected is not None:
            return
        self._release()
        self._shown = selected
        if selected is not None:
            self._shown = selected if self._exists() else None
        inline_close = self._shown is not None and self._shown[0] == "node"
        self._has_inline_close = inline_close
        self._body.margin = 0 if inline_close else (0, 16, 16, 16)
        self._sync_collapsed()
        if self._shown is None:
            objects = self._summary_view()
        elif self._shown[0] == "node":
            objects = self._node_view(self._shown[1])
        elif self._shown[0] == "nodes":
            objects = self._selection_view(self._shown[1])
        elif self._shown[0] == "group":
            objects = self._group_view(self._shown[1])
        else:
            objects = self._edge_view(self._shown[1])
        self._body[:] = objects
        self._sync_save()

    def show_extra(
        self,
        caption: str,
        title: str,
        objects: list[Viewable],
        release: t.Callable[[], None] | None = None,
    ):
        """Show *objects* in place of the selection's view until the selection changes or Back is pressed.

        *release* is called when they are replaced.
        """
        self._release()
        self._has_inline_close = False
        self._body.margin = (0, 16, 16, 16)
        self._sync_collapsed()
        if release is not None:
            self._releases.append(release)
        # Back sits on a row of its own, so the caption and title start at
        # the content's left edge, as in the side sheet.
        back = pmui.Button(
            label="Back",
            icon="arrow_back",
            variant="text",
            size="small",
            margin=(0, 0, 4, 0),
            sx={"minWidth": 0, "px": 0, "textTransform": "none"},
        )
        back.on_click(lambda _event: self.refresh(force=True))
        header = pn.Column(
            back,
            _caption(caption, margin=0),
            text_typography(title, variant="subtitle1", margin=0, sx={"fontWeight": 500}),
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
            css_classes=["flowdash-inspector-extra"],
        )
        self._body[:] = [header, *objects]

    def refresh_summary(self):
        """Rebuild the dashboard summary if it is shown, e.g. after the issues changed."""
        if self.visible and self._shown is None:
            self.refresh(force=True)

    def _release(self):
        for release in self._releases:
            release()
        self._releases = []
        self._draft = None
        self._choice_holder = None
        self._status_dot = None
        self._status_dot_pane = None
        self._node_status = self._config_status = None
        self._last_check_status = None
        self._code_fields = []
        self._data_column = None
        self._inputs_column = None
        self._inputs_key = {}
        self._literal_editors = {}
        self._value_panes = {}

    # ------------------------------------------------------------------
    # Dashboard summary, shown without a selection
    # ------------------------------------------------------------------

    def _summary_view(self) -> list[Viewable]:
        editor = self.editor
        dashboard = editor.dashboard
        nodes, edges = len(component_nodes(editor._flow.nodes)), len(editor._flow.edges)
        objects: list[Viewable] = [
            text_typography(
                dashboard.title if dashboard is not None else "Untitled dashboard",
                variant="h6",
                margin=(0, 0, 4, 0),
            ),
            _caption(
                f"{nodes} component{'' if nodes == 1 else 's'}, "
                f"{edges} connection{'' if edges == 1 else 's'}"
            ),
        ]
        issues = canvas_issues(editor)
        objects.append(_heading(f"Issues ({len(issues)})" if issues else "Issues"))
        if not issues:
            objects.append(
                pmui.Alert(
                    object="All outputs up to date.",
                    severity="success",
                    margin=0,
                    sizing_mode="stretch_width",
                    sx={"py": 0, "fontSize": "0.875rem"},
                )
            )
        for issue in issues[:_MAX_ISSUES]:
            objects.append(self._issue_row(issue))
        if len(issues) > _MAX_ISSUES:
            objects.append(_caption(f"and {len(issues) - _MAX_ISSUES} more"))
        objects.append(
            _caption(
                "Select a component to see its settings, its data and how it appears "
                "on the dashboard. Select a connection to edit its transform.",
                margin=(16, 0, 0, 0),
            )
        )
        return objects

    def _issue_row(self, issue) -> Viewable:
        severity = "error" if issue.status == "failed" else "warning"
        if issue.status == "unavailable":
            severity = "info"
        label = f"{issue.title}: {issue.detail}"
        if issue.instance_id is None and issue.edge_id is None:
            return text_alert(label, severity=severity, margin=(0, 0, 6, 0))
        button = pmui.Button(
            label=label,
            icon={"error": "error_outline", "warning": "hourglass_empty"}.get(severity, "info"),
            color=severity,
            variant="outlined",
            size="small",
            sizing_mode="stretch_width",
            margin=(0, 0, 6, 0),
            sx={"justifyContent": "flex-start", "textAlign": "left", "textTransform": "none"},
        )
        button.on_click(
            lambda _event: select_on_canvas(
                self._flow, node_id=issue.instance_id, edge_id=issue.edge_id
            )
        )
        return button

    # ------------------------------------------------------------------
    # A selected component
    # ------------------------------------------------------------------

    def _item(self, instance_id: str) -> dict | None:
        return next((i for i in self.editor._tile_items if i["instance_id"] == instance_id), None)

    def _header(
        self,
        icon: str,
        title: str,
        description: str | None,
        *,
        subtitle: str | None = None,
        menu_items: list[dict] | None = None,
        on_menu: t.Callable[[t.Any], None] | None = None,
        show_close: bool = True,
    ) -> Viewable:
        name_text = text_typography(
            title,
            variant="subtitle2",
            margin=0,
            sx={
                "fontFamily": '"Roboto Mono", ui-monospace, monospace',
                "fontSize": "0.84375rem",
                "fontWeight": 500,
                "lineHeight": "18px",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
                "whiteSpace": "nowrap",
            },
            css_classes=["fd-inspector-title"],
        )
        stack_items: list[Viewable] = [name_text]
        sub_parts: list[Viewable] = []
        if subtitle:
            sub_parts.append(
                text_typography(
                    subtitle,
                    variant="caption",
                    margin=0,
                    sx={
                        "color": "text.secondary",
                        "fontSize": "0.75rem",
                        "lineHeight": "16px",
                        "overflow": "hidden",
                        "textOverflow": "ellipsis",
                        "whiteSpace": "nowrap",
                    },
                    css_classes=["fd-inspector-subtitle"],
                )
            )
        if description:
            info_pane = pn.pane.HTML(
                '<span class="fd-info-icon" role="img" aria-label="Component description">'
                "info_outline</span>",
                width=16,
                height=16,
                margin=0,
                stylesheets=[_HEADER_CSS],
            )
            sub_parts.append(
                pmui.Tooltip(
                    object=info_pane,
                    title=description,
                    arrow=True,
                    margin=0,
                    css_classes=["fd-inspector-info"],
                )
            )
        if sub_parts:
            stack_items.append(
                pn.FlexBox(
                    *sub_parts,
                    align_items="center",
                    flex_wrap="nowrap",
                    gap="4px",
                    margin=0,
                    sizing_mode="stretch_width",
                    styles={"min-width": "0"},
                )
            )
        title_stack = pn.Column(
            *stack_items,
            margin=0,
            align="center",
            sizing_mode="stretch_width",
            styles={"min-width": "0", "flex": "1 1 0%", "overflow": "hidden"},
        )
        self._status_dot_pane = pn.pane.HTML(
            '<div class="fd-status-wrap">'
            '<span class="fd-status-dot fd-status-success" role="status" '
            'aria-label="Up to date"></span></div>',
            width=18,
            height=18,
            margin=0,
            stylesheets=[_HEADER_CSS],
        )
        self._status_dot = pmui.Tooltip(
            object=self._status_dot_pane,
            title="Up to date",
            arrow=True,
            margin=0,
            align="center",
            css_classes=["fd-inspector-status"],
        )
        row_items: list[Viewable] = [_header_icon(icon), title_stack, self._status_dot]
        if menu_items:
            menu = pmui.MenuButton(
                label="",
                icon="more_vert",
                icon_size="18px",
                description="More actions",
                items=menu_items,
                size="small",
                variant="text",
                color="default",
                margin=0,
                align="center",
                sx=_OVERFLOW_SX,
                stylesheets=[_DANGER_LAST_ITEM],
                css_classes=["fd-inspector-menu"],
            )
            if on_menu is not None:
                menu.on_click(on_menu)
            row_items.append(menu)
        if show_close:
            close_btn = pmui.IconButton(
                icon="close",
                icon_size="18px",
                description="Hide inspector",
                size="small",
                margin=0,
                align="center",
                sx=_HEADER_ICON_SX,
                css_classes=["fd-inspector-close"],
            )
            close_btn.on_click(lambda _event: setattr(self, "collapsed", True))
            row_items.append(close_btn)
        return pmui.Row(
            *row_items,
            sizing_mode="stretch_width",
            margin=0,
            sx={
                "alignItems": "center",
                "flexWrap": "nowrap",
                "minHeight": "54px",
                "pl": "14px",
                "pr": "8px",
                "gap": "4px",
            },
            css_classes=["fd-inspector-header"],
        )

    def _set_status(self, status: str, detail: str):
        self._node_status = (status, detail)
        self._show_status()

    def _set_config_status(self, draft: ConfigDraft, result: CheckResult, pending: dict):
        """Show in the status that the config check refused the config or held an edit back.

        The outputs are still up to date with the config applied last, but a
        status saying so beside a refused edit reads as if the edit went
        through. A check that finishes after another node was selected is
        ignored, since the header now shows that node.
        """
        if draft is not self._draft:
            return
        if not result:
            self._config_status = None
            if result.warnings:
                count = len(result.warnings)
                self._last_check_status = (
                    "warning",
                    f"{count} warning" if count == 1 else f"{count} warnings",
                )
            else:
                self._last_check_status = ("ok", "Check passed")
        else:
            count = len(result)
            self._last_check_status = (
                "error",
                f"{count} problem" if count == 1 else f"{count} problems",
            )
            if pending:
                self._config_status = ("held", "Edit not applied")
            else:
                self._config_status = ("refused", "Config refused")
        for field in self._code_fields:
            field.check_status = self._last_check_status
        self._show_status()

    def _show_status(self):
        node_status = self._node_status or ("ok", "")
        failed = node_status[0] == "failed"
        status, detail = node_status if failed else self._config_status or node_status
        label = detail or STATUS_TEXT[status]
        color = _STATUS_COLOR[status]
        self._status_info = (label, color)
        if self._status_dot is not None:
            self._status_dot.title = label
        if self._status_dot_pane is not None:
            self._status_dot_pane.object = (
                '<div class="fd-status-wrap">'
                f'<span class="fd-status-dot fd-status-{_esc(color)}" role="status" '
                f'aria-label="{_esc(label)}"></span></div>'
            )

    def _duplicate_component(self, instance_id: str):
        editor = self.editor
        item = self._item(instance_id)
        if item is None or "placeholder" in item:
            return
        config = copy.deepcopy(item.get("config") or {})
        node = next((n for n in self._flow.nodes if n["id"] == instance_id), None)
        pos = node.get("position") if node else None
        new_pos = {"x": pos["x"] + 32, "y": pos["y"] + 32} if isinstance(pos, dict) else None
        try:
            new_id = editor.add_component(
                item["component_id"], config=config or None, position=new_pos
            )
        except PermissionError:
            return
        for port_name, val in editor.input_values(instance_id).items():
            try:
                editor.set_input_value(new_id, port_name, copy.deepcopy(val))
            except Exception:
                pass
        select_on_canvas(self._flow, node_id=new_id)

    def _on_node_menu(self, instance_id: str, event):
        item = getattr(event, "new", event)
        action = item.get("action") if isinstance(item, dict) else None
        if action == "how_made":
            self.editor.show_how_made(instance_id)
        elif action == "duplicate":
            self._duplicate_component(instance_id)
        elif action == "remove":
            self.editor.remove_component(instance_id)

    # ------------------------------------------------------------------
    # Several selected components, and a selected group
    # ------------------------------------------------------------------

    def _selection_view(self, instance_ids: tuple[str, ...]) -> list[Viewable]:
        editor = self.editor
        count = len(instance_ids)
        grouped = [i for i in instance_ids if editor.group_of(i) is not None]
        objects: list[Viewable] = [
            pn.Row(
                _avatar("select_all"),
                text_typography(
                    f"{count} components selected",
                    variant="subtitle1",
                    margin=0,
                    sx={"fontWeight": 500},
                ),
                margin=(0, 0, 8, 0),
                sizing_mode="stretch_width",
            )
        ]
        button = pmui.Button(
            label="Group",
            icon="workspaces",
            variant="outlined",
            disabled=bool(grouped),
            margin=(8, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        button.on_click(lambda _event: self._group(instance_ids))
        objects.append(button)
        objects.append(
            _caption(
                f"{len(grouped)} of them already belong to a group. Ungroup them first."
                if grouped
                else "Frames the components together on the canvas. A group can be "
                "collapsed into one node showing the connections that cross it.",
                margin=(8, 0, 0, 0),
            )
        )
        return objects

    def _group(self, instance_ids: tuple[str, ...]):
        group_id = self.editor.group_nodes(instance_ids)
        select_on_canvas(self._flow, node_id=self.editor._group_node_id(group_id))

    def _group_view(self, group_id: str) -> list[Viewable]:
        editor = self.editor
        group = editor._groups[group_id]
        names = {i["instance_id"]: editor._node_name(i["instance_id"]) for i in editor._tile_items}
        inputs, outputs = editor._group_ports(group)
        label = pmui.TextInput(
            label="Label",
            value=group.title,
            sizing_mode="stretch_width",
            margin=(0, 0, 4, 0),
        )
        label.param.watch(lambda event: editor.rename_group(group_id, event.new), "value")
        objects: list[Viewable] = [
            pn.Row(
                _avatar("workspaces"),
                pn.Column(
                    text_typography(
                        group.title, variant="subtitle1", margin=0, sx={"fontWeight": 500}
                    ),
                    _caption(
                        f"{len(group.nodes)} component{'' if len(group.nodes) == 1 else 's'}"
                        + (", collapsed" if group.collapsed else ""),
                        margin=(2, 0, 0, 0),
                    ),
                    margin=0,
                    sizing_mode="stretch_width",
                ),
                margin=(0, 0, 8, 0),
                sizing_mode="stretch_width",
            ),
            label,
        ]
        for heading, ports in (("Inputs", inputs), ("Outputs", outputs)):
            objects.append(_heading(heading))
            if not ports:
                objects.append(
                    _caption(
                        f"No connection {'into' if heading == 'Inputs' else 'out of'} the group.",
                        margin=(0, 0, 2, 0),
                    )
                )
            for port in ports:
                ends = ", ".join(f"{names.get(n, n)}.{p}" for n, p in port.ends)
                objects.append(_caption(f"{port.label}: {ends}", margin=(0, 0, 2, 0)))
        objects.append(_heading("Components"))
        objects.append(_caption(", ".join(names.get(n, n) for n in group.nodes), margin=0))
        ungroup = pmui.Button(
            label="Ungroup",
            icon="layers_clear",
            variant="text",
            margin=(16, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        ungroup.on_click(lambda _event: editor.ungroup(group_id))
        count = len(group.nodes)
        delete = pmui.Button(
            label=f"Delete group and its {count} component{'' if count == 1 else 's'}",
            icon="delete_outline",
            color="error",
            variant="text",
            margin=(4, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        delete.on_click(lambda _event: editor.delete_group(group_id))
        objects.extend([ungroup, delete])
        return objects

    def _node_view(self, instance_id: str) -> list[Viewable]:
        editor = self.editor
        item = self._item(instance_id)
        if "placeholder" in item:
            placeholder = item["placeholder"]
            menu_items = [
                {"label": "Remove from dashboard", "icon": "delete_outline", "action": "remove"}
            ]
            header = self._header(
                "extension_off",
                placeholder.title,
                placeholder.message,
                subtitle="Unavailable",
                menu_items=menu_items,
                on_menu=partial(self._on_node_menu, instance_id),
            )
            self._set_status("unavailable", STATUS_TEXT["unavailable"])
            return [header, _caption(placeholder.message, margin=(12, 14, 16, 14))]
        spec = editor._component_specs[item["component_id"]]
        graph = editor._dataflow_graph
        state, failures = graph.get_state(instance_id), graph.get_failures(instance_id)
        menu_items: list[dict] = []
        if editor.made_by_agent(instance_id):
            menu_items.append(
                {"label": "How was this made?", "icon": "manage_search", "action": "how_made"}
            )
        menu_items.append({"label": "Duplicate", "icon": "content_copy", "action": "duplicate"})
        menu_items.append(
            {"label": "Remove from dashboard", "icon": "delete_outline", "action": "remove"}
        )
        header = self._header(
            component_icon(spec),
            editor._node_name(instance_id),
            spec.description,
            subtitle=spec.title,
            menu_items=menu_items,
            on_menu=partial(self._on_node_menu, instance_id),
        )
        self._data_column = pn.Column(margin=(12, 14, 16, 14), sizing_mode="stretch_width")
        self._inputs_column = pn.Column(margin=0, sizing_mode="stretch_width")
        self._refresh_inputs = partial(self._fill_inputs, instance_id, spec)

        inspector = weakref.ref(self)

        def update(*_events):
            if (owner := inspector()) is None or owner._data_column is None:
                return
            owner._set_status(*node_status(spec, state, failures))
            owner._data_column[:] = owner._data_objects(instance_id, spec)
            owner._refresh_inputs()

        names = [p.name for p in (*spec.outputs, *spec.inputs) if p.name in state.param]
        if names:
            watcher = state.param.watch(update, names)
            self._releases.append(partial(state.param.unwatch, watcher))
        if failures is not None:
            watcher = failures.param.watch(update, "failed")
            self._releases.append(partial(failures.param.unwatch, watcher))
        update()

        tabs = pmui.Tabs(
            ("Settings", self._settings_tab(instance_id, item, spec)),
            ("Data", self._data_column),
            ("Tile", self._tile_tab(instance_id, item, spec)),
            dynamic=True,
            sizing_mode="stretch_width",
            margin=0,
            sx=_TABS_SX,
        )
        return [header, tabs]

    # Settings

    def _settings_tab(self, instance_id: str, item: dict, spec: ComponentSpec) -> Viewable:
        return pn.Column(
            self._config_form(instance_id, spec),
            self._inputs_column,
            margin=(12, 14, 16, 14),
            sizing_mode="stretch_width",
        )

    def _config_form(self, instance_id: str, spec: ComponentSpec) -> Viewable:
        """Build the config form: the component's own `config_editor`, or one from its params.

        With a `check_config` declared, the form edits a draft that is applied
        once the check passes, see `ConfigDraft`.
        """
        editor = self.editor
        config_state = editor._dataflow_graph.get_config_state(instance_id)
        names = [n for n in (config_state.param if config_state else []) if n != "name"]
        if not names:
            caption = (
                _caption("This component has no settings.")
                if not spec.inputs
                else pn.Column(margin=0)
            )
            if not has_choices(spec) or not any(takes_literal(p) for p in spec.inputs):
                return caption
            # The choices are offered for the values of the unconnected inputs.
            self._form_key = ()
            holder = pn.Column(pn.Column(margin=0), self._choices_progress(), margin=0)
            self._watch_choices(instance_id, spec, None, None, [], holder)
            return pn.Column(caption, holder, margin=0, sizing_mode="stretch_width")
        draft = None
        if spec.check_config is not None:
            draft = ConfigDraft(editor, instance_id, self._check_runs, self._check_results)
            draft.on_result = partial(self._set_config_status, draft)
            self._draft = draft
            # A node placed from the palette or gallery has not been checked yet.
            draft.show_current()
        if spec.config_editor is not None:
            type_key = spec.component_id.replace("/", "__")
            node_type = editor._flow.node_types.get(type_key) or {}
            custom = spec.config_editor(
                {name: getattr(config_state, name) for name in names},
                node_type.get("schema"),
                id=instance_id,
                type=type_key,
                on_patch=partial(editor._apply_config_patch, instance_id)
                if draft is None
                else draft.propose,
            )
            form = custom.__panel__() if hasattr(custom, "__panel__") else pn.panel(custom)
            if draft is None:
                return form
            return pn.Column(form, draft.alert, margin=0, sizing_mode="stretch_width")

        if draft is not None:
            self._releases += draft.bind(config_state, names)
            edited = draft.state
        else:

            def record(*events):
                editor._apply_config_patch(instance_id, {e.name: e.new for e in events})

            watcher = config_state.param.watch(record, names)
            self._releases.append(partial(config_state.param.unwatch, watcher))
            edited = config_state
        holder = pn.Column(
            self._param_form(instance_id, edited, names, self._choices.get(instance_id, {})),
            margin=0,
            sizing_mode="stretch_width",
        )
        if has_choices(spec):
            holder.append(self._choices_progress())
            self._watch_choices(instance_id, spec, config_state, edited, names, holder)
        objects = [holder] if draft is None else [holder, draft.alert]
        if self.editor._rejected_config.get(instance_id):
            rejected = pn.Column(margin=0, sizing_mode="stretch_width")

            def show_rejected(*_events):
                alert = self._rejected_alert(instance_id, config_state)
                rejected[:] = [] if alert is None else [alert]

            show_rejected()
            watcher = config_state.param.watch(show_rejected, names)
            self._releases.append(partial(config_state.param.unwatch, watcher))
            objects.insert(0, rejected)
        if len(objects) == 1:
            return holder
        return pn.Column(*objects, margin=0, sizing_mode="stretch_width")

    @staticmethod
    def _choices_progress() -> pmui.LinearProgress:
        return pmui.LinearProgress(
            variant="indeterminate",
            visible=False,
            sizing_mode="stretch_width",
            margin=(4, 0, 0, 0),
            css_classes=["fd-choices-loading"],
        )

    def _rejected_alert(self, instance_id: str, config_state) -> Viewable | None:
        """Say which saved config values the node refused on load, and what it uses instead."""
        rejected = self.editor._rejected_config.get(instance_id)
        if not rejected:
            return None
        lines = [
            f"{config_state.param[key].label or key}: {value!r} is not available, "
            f"using {getattr(config_state, key)!r}."
            for key, value in rejected.items()
            if key in config_state.param
        ]
        if not lines:
            return None
        return text_alert(
            "\n\n".join(lines),
            severity="warning",
            title="Saved settings not applied",
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
            css_classes=["fd-config-rejected"],
        )

    def _edit_code_field(self, instance_id: str, field_name: str, field: CodeField):
        if not self.editor.open_code_panel(instance_id, field_name):
            field.open_dialog()

    def _param_form(
        self,
        instance_id: str,
        state: param.Parameterized,
        names: list[str],
        choices: dict[str, Choices],
    ) -> pn.Param:
        widgets = form_widgets(state, names, choices)
        self._form_key = widgets_key(widgets)
        form = pn.Param(
            state,
            parameters=names,
            widgets=widgets or None,
            show_name=False,
            expand_button=False,
            sizing_mode="stretch_width",
            margin=0,
        )
        editor = weakref.ref(self.editor)
        widget_names = {
            widget: name
            for name, widget in getattr(form, "_widgets", {}).items()
            if name != "_title"
        }
        for widget in widget_names:
            if not isinstance(widget, (CodeField, SpecEditor)):
                widget.margin = (6, 0)
                if not isinstance(widget, (pmui.Checkbox, pmui.Switch)):
                    widget.param.update(width=None, sizing_mode="stretch_width")
        code_fields: list = []
        for field in form.select(CodeField):
            field_name = widget_names.get(field, field.name)
            field.on_edit = partial(self._edit_code_field, instance_id, field_name)
            if self._last_check_status is not None:
                field.check_status = self._last_check_status
        for field in (*form.select(CodeField), *form.select(SpecEditor)):
            # Typed text is applied on blur, so Save is offered while some waits.
            field.param.watch(
                lambda _event: (e := editor()) is not None and e._sync_toolbar_state(), "unsent"
            )
            code_fields.append(field)
        self._code_fields = code_fields
        return form

    def _sync_save(self):
        """Offer Save while a code field shown holds unapplied text, and stop once it is gone."""
        if getattr(self.editor, "_dashboard_bar", None) is not None:
            self.editor._sync_toolbar_state()

    def has_unsent_text(self) -> bool:
        """Whether a code field shown holds typed text it has not applied yet."""
        body = pn.Column(*self._body)
        return any(field.unsent for field in (*body.select(CodeField), *body.select(SpecEditor)))

    async def settle(self) -> None:
        """Wait for the config check of an edit in the form shown, so a save includes it."""
        if self._draft is not None:
            await self._draft.settle()

    def _watch_choices(self, instance_id, spec, config_state, edited, names, holder):
        """Offer the node's config choices in *holder*'s form, asking again as its inputs or config change."""
        self._choice_holder = holder
        refresh = partial(self._update_choices, instance_id, spec, edited, names, holder)
        state = self.editor._dataflow_graph.get_state(instance_id)
        for owner, watched in (
            (config_state, names),
            (state, [p.name for p in spec.inputs if state is not None and p.name in state.param]),
        ):
            if owner is not None and watched:
                watcher = owner.param.watch(lambda *_events: refresh(), watched)
                self._releases.append(partial(owner.param.unwatch, watcher))
        refresh()

    def _update_choices(self, instance_id, spec, state, names, holder):
        """Ask for the node's choices and rebuild its form if they change its widgets.

        The declared column choices are shown first, then the component's
        ``config_choices`` is asked, with a progress bar under the form
        meanwhile. One request runs at a time per form; changes meanwhile ask
        once more after it, so a fast-changing input does not pile up calls
        of a slow ``config_choices``.
        """
        if self._choices_busy is holder:
            self._choices_again = True
            return
        self._choices_busy, self._choices_again = holder, False
        inspector = weakref.ref(self)
        declared_names = {f.name for f in spec.config if f.choices}
        loading = holder[1]

        def show(owner, choices):
            owner._choices[instance_id] = choices
            if widgets_key(form_widgets(state, names, choices)) != owner._form_key:
                holder[0] = owner._param_form(instance_id, state, names, choices)
                owner._sync_save()
            for name, widget in getattr(holder[0], "_widgets", {}).items():
                if isinstance(widget, SpecEditor):
                    widget.choices = setting_choices(choices, name)
            owner._refresh_inputs()

        async def update():
            try:
                again = True
                while again:
                    owner = inspector()
                    if owner is None or owner._choice_holder is not holder:
                        return
                    owner._choices_again = False
                    declared = await node_choices(owner.editor, instance_id, hook=False)
                    if owner._choice_holder is not holder:
                        return
                    earlier = owner._choices.get(instance_id, {})
                    kept = {k: v for k, v in earlier.items() if k not in declared_names}
                    show(owner, {**kept, **declared})
                    if spec.config_choices is not None:
                        loading.visible = True
                        try:
                            choices = await node_choices(
                                owner.editor, instance_id, declared=declared
                            )
                        finally:
                            loading.visible = False
                        if owner._choice_holder is not holder:
                            return
                        show(owner, choices)
                    again = owner._choices_again
            finally:
                owner = inspector()
                if owner is not None and owner._choices_busy is holder:
                    owner._choices_busy = None

        param.parameterized.async_executor(update)

    def _fill_inputs(self, instance_id: str, spec: ComponentSpec):
        """Show the node's inputs, rebuilding only the rows whose wiring changed.

        The other rows are updated in place, and new choices for an input are
        offered in its field, so a value being typed into one input's field is
        not replaced while another input changes or choices arrive.
        """
        if self._inputs_column is None or not spec.inputs:
            return
        rows = self._input_rows(instance_id, spec)
        column = self._inputs_column
        if not column.objects or list(rows) != list(self._inputs_key):
            self._inputs_key = {port: row[0] for port, row in rows.items()}
            self._literal_editors = {}
            column[:] = self._inputs_section(instance_id, spec)
            return
        incoming = self._incoming(instance_id)
        state = self.editor._dataflow_graph.get_state(instance_id)
        literals = self.editor.input_values(instance_id)
        ports = {port.name: port for port in spec.inputs}
        for index, (name, (key, update)) in enumerate(rows.items(), start=1):
            if key == self._inputs_key.get(name):
                update()
                continue
            self._inputs_key[name] = key
            self._literal_editors.pop(name, None)
            column[index] = self._input_row(
                instance_id, ports[name], incoming.get(name), state, literals
            )
        connected = sum(1 for name in ports if name in incoming)
        self._connected_caption.object = f"{connected} of {len(ports)} connected"

    def _input_rows(self, instance_id: str, spec: ComponentSpec) -> dict[str, tuple]:
        """Return, per input, what its row shows that needs a rebuild, and how to update the rest."""
        editor = self.editor
        incoming = self._incoming(instance_id)
        literals = editor.input_values(instance_id)
        choices = self._choices.get(instance_id, {})
        state = editor._dataflow_graph.get_state(instance_id)
        rows = {}
        for port in spec.inputs:
            wired = incoming.get(port.name)
            if wired is not None:
                source_id = wired[1][0]
                key = (
                    "wired",
                    wired[0],
                    self._origin_text(wired[1]),
                    repr(literals.get(port.name)),
                )

                def update(name=port.name, port_type=port.type, src_id=source_id):
                    pane = self._value_panes.get(name)
                    if pane is not None:
                        src_inst = editor._component_instances.get(src_id)
                        pane.object = summarize_input_html(
                            getattr(state, name, None), port_type, src_inst
                        )

            else:
                key = ("free", takes_literal(port))

                def update(name=port.name, port=port):
                    literal = self._literal_editors.get(name)
                    if literal is not None:
                        literal.set_choices(choices.get(name))
                        literal.sync(literals.get(name), name in literals)
                        self._literal_caption(name, name in literals, port)

            rows[port.name] = (key, update)
        return rows

    def _incoming(self, instance_id: str) -> dict[str, tuple[str, tuple]]:
        return {
            mapping[3]: (edge_id, mapping)
            for edge_id, mapping in self.editor._edge_id_map.items()
            if mapping[2] == instance_id
        }

    def _inputs_section(self, instance_id: str, spec: ComponentSpec) -> list[Viewable]:
        editor = self.editor
        incoming = self._incoming(instance_id)
        state = editor._dataflow_graph.get_state(instance_id)
        literals = editor.input_values(instance_id)
        connected = sum(1 for p in spec.inputs if p.name in incoming)
        self._connected_caption = _caption(
            f"{connected} of {len(spec.inputs)} connected",
            margin=0,
            sx={"whiteSpace": "nowrap", "flexShrink": 0},
        )
        objects: list[Viewable] = [
            pn.FlexBox(
                _heading("Inputs", margin=0),
                self._connected_caption,
                align_items="center",
                justify_content="space-between",
                flex_wrap="nowrap",
                margin=(16, 0, 8, 0),
                sizing_mode="stretch_width",
            )
        ]
        self._value_panes = {}
        self._literal_captions = {}
        for port in spec.inputs:
            objects.append(
                self._input_row(instance_id, port, incoming.get(port.name), state, literals)
            )
        return objects

    def _origin_text(self, mapping: tuple) -> str:
        editor = self.editor
        source_id, source_port = mapping[0], mapping[1]
        source_component = editor._component_id_for_instance(source_id)
        source_spec = editor._component_specs.get(source_component or "")
        source_title = source_spec.title if source_spec else source_id
        source_port_spec = editor._port_spec(source_id, source_port, output=True)
        origin = f"from {source_title}, {port_label(source_port_spec) if source_port_spec else source_port}"
        steps = parse_transform(editor._dataflow_graph.get_edge_transform(*mapping))
        transform = summarize_transform(steps)
        if transform:
            origin += f", with transform {transform}"
        return origin

    def _input_row(self, instance_id, port, wired, state, literals) -> Viewable:
        name = port_label(port)
        title = text_typography(
            name,
            variant="body2",
            margin=0,
            sx={"fontWeight": 500, "fontSize": "0.78125rem", "flexShrink": 0},
        )
        if wired is None:
            head_items: list[Viewable] = [title]
            if not port.required:
                head_items.append(
                    _caption(
                        "optional", margin=0, sx={"color": "text.secondary", "fontSize": "0.75rem"}
                    )
                )
            if takes_literal(port):
                literal = LiteralEditor(
                    port,
                    literals.get(port.name),
                    port.name in literals,
                    partial(self.editor.set_input_value, instance_id, port.name),
                    choices=self._choices.get(instance_id, {}).get(port.name),
                )
                self._literal_editors[port.name] = literal
                caption = _caption("")
                self._literal_captions[port.name] = caption
                self._literal_caption(port.name, port.name in literals, port)
                head_items.extend([pn.layout.HSpacer(), literal.clear_button])
                lines: list[Viewable] = [
                    pn.FlexBox(
                        *head_items,
                        align_items="center",
                        flex_wrap="nowrap",
                        gap="6px",
                        margin=0,
                        sizing_mode="stretch_width",
                    ),
                    literal,
                    caption,
                ]
            else:
                lines = [
                    pn.FlexBox(
                        *head_items,
                        align_items="center",
                        flex_wrap="nowrap",
                        gap="6px",
                        margin=0,
                        sizing_mode="stretch_width",
                    ),
                    _caption(
                        "Required, not connected" if port.required else "Not connected",
                        margin=(3, 0, 0, 0),
                    ),
                ]
            return pmui.Paper(
                *lines,
                margin=(0, 0, 8, 0),
                sizing_mode="stretch_width",
                variant="outlined",
                sx={"p": "7px 8px 7px 10px", "borderRadius": "6px", "boxShadow": "none"},
                css_classes=["fd-input-row", f"fd-input-{port.name}"],
            )

        edge_id, mapping = wired
        source_instance = self.editor._component_instances.get(mapping[0])
        value_pane = pn.pane.HTML(
            summarize_input_html(getattr(state, port.name, None), port.type, source_instance),
            margin=(3, 0, 0, 0),
            sizing_mode="stretch_width",
            stylesheets=[_INPUT_DATA_CSS],
            css_classes=["fd-input-summary"],
        )
        self._value_panes[port.name] = value_pane
        origin_text = self._origin_text(mapping)
        origin = text_typography(
            origin_text,
            variant="caption",
            margin=0,
            sizing_mode="stretch_width",
            sx={
                "color": "text.secondary",
                "fontSize": "0.75rem",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
                "whiteSpace": "nowrap",
                "minWidth": 0,
            },
        )
        edit = pmui.IconButton(
            icon="edit",
            icon_size="16px",
            description="Inspect this connection",
            size="small",
            margin=0,
            align="center",
            sx={"p": "2px", "color": "text.secondary", "flexShrink": 0},
        )
        edit.on_click(lambda _event: select_on_canvas(self._flow, edge_id=edge_id))
        top_items: list[Viewable] = [title]
        if not port.required:
            top_items.append(
                text_typography(
                    "optional ·",
                    variant="caption",
                    margin=0,
                    sx={
                        "color": "text.secondary",
                        "fontSize": "0.75rem",
                        "whiteSpace": "nowrap",
                        "flexShrink": 0,
                    },
                )
            )
        top_items.extend([origin, edit])
        lines = [
            pmui.Row(
                *top_items,
                margin=0,
                sizing_mode="stretch_width",
                sx={"alignItems": "center", "flexWrap": "nowrap", "gap": "6px"},
            ),
            value_pane,
        ]
        if port.name in literals:
            lines.append(
                _caption(
                    f"The value set here ({summarize_value(literals[port.name])}) "
                    "applies once disconnected.",
                    margin=(4, 0, 0, 0),
                    css_classes=["fd-literal-overridden"],
                )
            )
        return pmui.Paper(
            *lines,
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
            variant="outlined",
            sx={"p": "7px 8px 7px 10px", "borderRadius": "6px", "boxShadow": "none"},
            css_classes=["fd-input-row", f"fd-input-{port.name}"],
        )

    def _literal_caption(self, name: str, is_set: bool, port) -> None:
        caption = self._literal_captions.get(name)
        if caption is None:
            return
        if is_set:
            caption.object = "Set here; a connection would replace it."
        elif port.required:
            caption.object = "Required: set a value or connect it."
        else:
            caption.object = "Not connected; set a value or connect it."

    # Data

    def _data_objects(self, instance_id: str, spec: ComponentSpec) -> list[Viewable]:
        editor = self.editor
        state = editor._dataflow_graph.get_state(instance_id)
        ports, kind = (spec.outputs, "Output") if spec.outputs else (spec.inputs, "Input")
        if not ports:
            return [_caption("This component has no outputs or inputs.", margin=(8, 0, 0, 0))]
        objects: list[Viewable] = []
        for port in ports:
            value = getattr(state, port.name, None)
            objects += [
                _heading(f"{kind}: {port_label(port)}  {port.type or ''}".rstrip()),
                _caption(summarize_value(value), margin=(0, 0, 4, 0)),
                self._preview(value),
            ]
        return objects

    def _preview(self, value) -> Viewable:
        """Show the first rows and the columns of a table, or the editor's own value preview."""
        columns = getattr(value, "columns", None)
        if columns is None or not hasattr(value, "head"):
            return self.editor._value_preview(value)
        names = [str(c) for c in columns]
        listed = ", ".join(names[:12]) + (
            f" and {len(names) - 12} more" if len(names) > 12 else ""
        )
        return pn.Column(
            _caption(f"Columns: {listed}", margin=(0, 0, 4, 0)),
            pn.pane.DataFrame(
                value.head(5), sizing_mode="stretch_width", margin=0, stylesheets=[_TABLE_CSS]
            ),
            margin=0,
            sizing_mode="stretch_width",
        )

    # Tile

    def _tile_tab(self, instance_id: str, item: dict, spec: ComponentSpec) -> Viewable:
        """Return the tile's options, rebuilt when the component moves to or from the grid."""
        editor = self.editor
        holder = pn.Column(sizing_mode="stretch_width", margin=(12, 14, 16, 14))
        releases: list = []
        shown = None

        def fill(*_events):
            nonlocal shown
            area = area_of(editor, item)
            if area == shown:
                return
            shown = area
            for release in releases:
                release()
            releases.clear()
            holder[:] = self._tile_options(instance_id, item, spec, area, releases)

        watcher = editor._view_controls.param.watch(fill, "placed")
        self._releases.append(partial(editor._view_controls.param.unwatch, watcher))
        self._releases.append(lambda: [release() for release in releases])
        fill()
        return holder

    def _tile_options(
        self, instance_id: str, item: dict, spec: ComponentSpec, area: str | None, releases: list
    ) -> list[Viewable]:
        editor = self.editor

        def refocus():
            # The pressed button went with the rebuilt tab; the first move left takes the focus.
            button = next(iter(self._tile_move_buttons), None)
            if button is not None:
                button.focus()

        moves = move_buttons(editor, item, then=refocus, margin=(12, 8, 0, 0))
        self._tile_move_buttons = moves
        if area != GRID:
            where = AREA_NAMES.get(area, "sidebar")
            return [
                _caption(
                    f"This component is shown in the {where} of the dashboard rather than "
                    "as a tile.",
                    margin=(12, 0, 0, 0),
                ),
                pn.FlexBox(*moves, margin=0, gap="0px"),
            ]
        layout = tile_entry(editor, instance_id)
        default = spec.default_size or {}
        show = pmui.Switch(
            label="Show on dashboard", value=layout.get("visible", True), margin=(4, 0, 4, 0)
        )
        width = layout.get("width")
        options = {"Automatic": _AUTO, **_WIDTHS}
        if isinstance(width, (int, float)) and width not in options.values():
            options[f"{width:g}%"] = width
        width_select = pmui.Select(
            label="Width",
            options=options,
            value=width if isinstance(width, (int, float)) else _AUTO,
            sizing_mode="stretch_width",
            margin=(8, 0),
        )
        current = layout.get("height", default.get("height"))
        heights = {"Automatic": _AUTO, **{f"{h} px": h for h in _HEIGHTS}}
        if isinstance(current, (int, float)) and current not in heights.values():
            heights[f"{current:g} px"] = current
        height = pmui.Select(
            label="Height",
            options=heights,
            value=current if isinstance(current, (int, float)) else _AUTO,
            sizing_mode="stretch_width",
            margin=(8, 0),
        )
        frame = pmui.Select(
            label="Frame",
            options=frame_choices(editor, instance_id),
            value=frame_value(layout),
            description="Whether the tile is drawn as a card in view mode",
            sizing_mode="stretch_width",
            margin=(12, 0, 8, 0),
        )
        syncing = False

        def set_frame(event):
            if not syncing:
                update_tile(
                    editor, instance_id, framed=None if event.new == FRAME_DEFAULT else event.new
                )

        def sync_frame(*_events):
            # Follows the dashboard's Frame tiles setting and frames set elsewhere, e.g. by the agent.
            nonlocal syncing
            syncing = True
            try:
                frame.param.update(
                    options=frame_choices(editor, instance_id),
                    value=frame_value(tile_entry(editor, instance_id)),
                )
                frame.param.trigger("value")
            finally:
                syncing = False

        for owner, names in ((editor.settings, ["tile_cards"]), (editor._tile_grid, ["objects"])):
            watcher = owner.param.watch(sync_frame, names)
            releases.append(partial(owner.param.unwatch, watcher))
        show.param.watch(lambda e: update_tile(editor, instance_id, visible=e.new), "value")
        frame.param.watch(set_frame, "value")
        width_select.param.watch(
            lambda e: update_tile(editor, instance_id, width=None if e.new == _AUTO else e.new),
            "value",
        )
        height.param.watch(
            lambda e: update_tile(editor, instance_id, height=None if e.new == _AUTO else e.new),
            "value",
        )
        objects: list[Viewable] = [show, width_select, height]
        hints = self._size_hints(spec)
        if hints:
            objects.append(_caption(hints))
        objects.append(frame)
        arrange = pmui.Button(
            label="Arrange tiles",
            icon="dashboard",
            variant="text",
            size="small",
            margin=(12, 0, 0, 0),
        )
        arrange.on_click(lambda _event: setattr(editor, "mode", "dashboard"))
        objects.append(arrange)
        if moves:
            objects.append(pn.FlexBox(*moves, margin=0, gap="0px"))
        return objects

    @staticmethod
    def _size_hints(spec: ComponentSpec) -> str:
        def size(hint):
            return " x ".join(f"{hint[k]} px" for k in ("width", "height") if k in hint)

        parts = [
            f"{label} {size(hint)}"
            for label, hint in (
                ("Starts at", spec.default_size),
                ("at least", spec.min_size),
                ("at most", spec.max_size),
            )
            if hint
        ]
        return ("Size hints: " + ", ".join(parts) + ".") if parts else ""

    # ------------------------------------------------------------------
    # A selected connection
    # ------------------------------------------------------------------

    def _edge_view(self, edge_id: str) -> list[Viewable]:
        """Show a selected connection: its ends and the editor of its transform steps.

        It receives the canvas edge id; the wired ports are
        ``editor._edge_id_map[edge_id]``. The transform editor is the edge's
        own, kept by the editor, so a draft survives the edge being deselected.
        """
        editor = self.editor
        mapping = editor._edge_id_map[edge_id]
        source_id, source_port, target_id, target_port = mapping

        def end(instance_id, port, output):
            port_spec = editor._port_spec(instance_id, port, output=output)
            label = port_label(port_spec) if port_spec else port
            port_type = port_spec.type if port_spec else None
            return f"{label} ({port_type})" if port_type else label

        disconnect = pmui.IconButton(
            icon="link_off",
            description="Remove connection",
            size="small",
            color="error",
            margin=(4, 0, 0, 0),
        )
        disconnect.on_click(lambda _event: editor.disconnect(*mapping))
        header = pn.Row(
            _avatar("functions"),
            pn.Column(
                _caption("Connection", margin=0),
                text_typography(
                    connection_title(editor, mapping),
                    variant="subtitle1",
                    margin=(2, 0, 0, 0),
                    sx={"fontWeight": 500, "lineHeight": 1.3},
                ),
                _caption(
                    f"{end(source_id, source_port, True)} into "
                    f"{end(target_id, target_port, False)}",
                    margin=(2, 0, 0, 0),
                ),
                margin=0,
                sizing_mode="stretch_width",
            ),
            disconnect,
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
        )
        steps = pn.Row(
            _heading("Transform steps"),
            pn.layout.HSpacer(),
            _caption("Applied in order", margin=(18, 0, 0, 0)),
            margin=0,
            sizing_mode="stretch_width",
        )
        transform = editor._transform_editor(edge_id)
        transform.show()
        self._releases.append(transform.hide)
        return [header, steps, transform]

    # ------------------------------------------------------------------
    # Collapsing
    # ------------------------------------------------------------------

    @param.depends("collapsed", watch=True)
    def _sync_collapsed(self):
        collapsed = self.collapsed
        self.collapse_button.visible = not collapsed and not self._has_inline_close
        self._body.visible = not collapsed
        self.expand_button.visible = collapsed
        self._panel.width = _COLLAPSED_WIDTH if collapsed else INSPECTOR_WIDTH

    def __panel__(self):
        """Render the panel."""
        return self._panel
