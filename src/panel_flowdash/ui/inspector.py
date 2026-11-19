"""The inspector, a persistent panel beside the wiring canvas bound to its selection.

With a component selected it shows the component's header and status, its
settings (the config form that used to open in a popup above the node), its
current outputs and how it appears on the dashboard, with the inputs wired to
it and a way to remove it. With a connection selected it describes the
connection and edits its transform steps (see :meth:`Inspector._edge_view`).
With nothing selected it summarizes the dashboard and lists its issues.
"""

from __future__ import annotations

import html
import typing as t
import weakref
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.config_check import CheckResult
from panel_flowdash.config_choices import Choices, has_choices, node_choices
from panel_flowdash.groups import component_nodes, group_id_of
from panel_flowdash.transforms import parse_transform, summarize_transform
from panel_flowdash.ui.choice_widgets import form_widgets, widgets_key
from panel_flowdash.ui.config_draft import ConfigDraft
from panel_flowdash.ui.node_card import (
    STATUS_TEXT,
    component_icon,
    node_status,
    port_label,
    summarize_value,
)
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

_SECTION_SX = {"color": "text.secondary", "fontWeight": 500}


def _caption(text: str, **kwargs) -> pmui.Typography:
    return text_typography(text, variant="caption", sx={"color": "text.secondary"}, **kwargs)


def _heading(text: str, margin=(16, 0, 4, 0)) -> pmui.Typography:
    return text_typography(text, variant="subtitle2", margin=margin, sx=_SECTION_SX)


def _avatar(icon: str) -> pn.pane.HTML:
    return pn.pane.HTML(
        f'<div class="fd-avatar"><span aria-hidden="true">{html.escape(icon)}</span></div>',
        width=36,
        height=36,
        margin=(2, 12, 0, 0),
        stylesheets=[_ICON_CSS],
    )


class Inspector(Viewer):
    """A collapsible panel beside the canvas showing what is selected on it."""

    collapsed = param.Boolean(default=False, doc="Whether the panel is folded to a narrow strip.")

    editor = param.Parameter(doc="The FlowDash editor whose canvas selection is inspected.")

    visible = param.Boolean(default=True, doc="Whether the panel is shown.")

    def __init__(self, **params):
        super().__init__(**params)
        self._flow = self.editor._flow
        # Watchers following the shown component, released when it is replaced.
        self._releases: list[t.Callable[[], None]] = []
        self._shown: tuple[str, str] | None = None
        self._status_chip: pmui.Chip | None = None
        self._node_status: tuple[str, str] | None = None
        self._config_status: tuple[str, str] | None = None
        self._data_column: pn.Column | None = None
        self._inputs_column: pn.Column | None = None
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
        self._body = pn.Column(sizing_mode="stretch_width", margin=(0, 16, 16, 16))
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
        # A group just made, shown while nothing is selected on the canvas.
        self._shown_group: str | None = None
        self._flow.param.watch(self._on_selection, "selection")
        self._watcher = CanvasWatcher(self._flow, self._on_canvas_changed)

    # ------------------------------------------------------------------
    # What is shown
    # ------------------------------------------------------------------

    def _selected(self) -> tuple[str, t.Any] | None:
        selection = self._flow.selection or {}
        nodes, edges = selection.get("nodes") or [], selection.get("edges") or []
        components = tuple(n for n in nodes if group_id_of(n) is None)
        if not nodes and not edges and self._shown_group in self.editor._groups:
            return ("group", self._shown_group)
        if len(nodes) == 1 and not edges and (group_id := group_id_of(nodes[0])):
            return ("group", group_id)
        if len(components) > 1 and not edges:
            return ("nodes", components)
        if len(nodes) == 1 and not edges:
            return ("node", nodes[0])
        if len(edges) == 1 and not nodes:
            return ("edge", edges[0])
        return None

    def _on_selection(self, event):
        if (event.new or {}).get("nodes") or (event.new or {}).get("edges"):
            self._shown_group = None
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
        self._status_chip = None
        self._node_status = self._config_status = None
        self._data_column = None
        self._inputs_column = None

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

    def _header(self, icon: str, title: str, description: str | None) -> Viewable:
        lines = [text_typography(title, variant="subtitle1", margin=0, sx={"fontWeight": 500})]
        if description:
            lines.append(_caption(description, margin=(2, 0, 0, 0)))
        self._status_chip = pmui.Chip(size="small", margin=(8, 0, 0, 0))
        lines.append(self._status_chip)
        return pn.Row(
            _avatar(icon),
            pn.Column(*lines, margin=0, sizing_mode="stretch_width"),
            margin=(0, 0, 8, 0),
            sizing_mode="stretch_width",
        )

    def _set_status(self, status: str, detail: str):
        self._node_status = (status, detail)
        self._show_status()

    def _set_config_status(self, draft: ConfigDraft, result: CheckResult, pending: dict):
        """Show in the status that the config check refused the config or held an edit back.

        The outputs are still up to date with the config applied last, but a
        status saying so beside a refused edit reads as if the edit went
        through. A check that finishes after another node was selected is
        ignored, since the chip now shows that node.
        """
        if draft is not self._draft:
            return
        if not result:
            self._config_status = None
        elif pending:
            self._config_status = ("held", "Edit not applied")
        else:
            self._config_status = ("refused", "Config refused")
        self._show_status()

    def _show_status(self):
        node_status = self._node_status or ("ok", "")
        failed = node_status[0] == "failed"
        status, detail = node_status if failed else self._config_status or node_status
        if self._status_chip is not None:
            self._status_chip.param.update(
                label=detail or STATUS_TEXT[status], color=_STATUS_COLOR[status]
            )

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
        # Grouping sends every node to the page again, the grouped ones
        # deselected so the page drops their selection. The inspector then
        # shows the new group until something else is selected.
        for node in self._flow.nodes:
            if node["id"] in instance_ids:
                node["selected"] = False
        group_id = self.editor.group_nodes(instance_ids)
        self._shown_group = group_id
        self.refresh(force=True)

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
        objects.append(ungroup)
        return objects

    def _remove_button(self, instance_id: str) -> pmui.Button:
        button = pmui.Button(
            label="Remove from dashboard",
            icon="delete_outline",
            color="error",
            variant="text",
            margin=(16, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        button.on_click(lambda _event: self.editor.remove_component(instance_id))
        return button

    def _node_view(self, instance_id: str) -> list[Viewable]:
        editor = self.editor
        item = self._item(instance_id)
        if "placeholder" in item:
            placeholder = item["placeholder"]
            header = self._header("extension_off", placeholder.title, placeholder.message)
            self._set_status("unavailable", STATUS_TEXT["unavailable"])
            return [header, self._remove_button(instance_id)]
        spec = editor._component_specs[item["component_id"]]
        graph = editor._dataflow_graph
        state, failures = graph.get_state(instance_id), graph.get_failures(instance_id)
        header = self._header(component_icon(spec), spec.title, spec.description)
        self._data_column = pn.Column(margin=0, sizing_mode="stretch_width")
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
            sx={"& .MuiTab-root": {"textTransform": "none", "minWidth": 0}},
        )
        return [header, tabs, self._remove_button(instance_id)]

    # Settings

    def _settings_tab(self, instance_id: str, item: dict, spec: ComponentSpec) -> Viewable:
        return pn.Column(
            _heading("Configuration"),
            self._config_form(instance_id, spec),
            self._inputs_column,
            margin=(8, 0, 0, 0),
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
            return _caption("This component has no settings.")
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
            self._param_form(edited, names, self._choices.get(instance_id, {})),
            margin=0,
            sizing_mode="stretch_width",
        )
        if has_choices(spec):
            holder.append(
                pmui.LinearProgress(
                    variant="indeterminate",
                    visible=False,
                    sizing_mode="stretch_width",
                    margin=(4, 0, 0, 0),
                    css_classes=["fd-choices-loading"],
                )
            )
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

    def _param_form(
        self, state: param.Parameterized, names: list[str], choices: dict[str, Choices]
    ) -> pn.Param:
        widgets = form_widgets(state, names, choices)
        self._form_key = widgets_key(widgets)
        return pn.Param(
            state,
            parameters=names,
            widgets=widgets or None,
            show_name=False,
            expand_button=False,
            sizing_mode="stretch_width",
            margin=0,
        )

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
                holder[0] = owner._param_form(state, names, choices)

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
        if self._inputs_column is not None and spec.inputs:
            self._inputs_column[:] = self._inputs_section(instance_id, spec)

    def _inputs_section(self, instance_id: str, spec: ComponentSpec) -> list[Viewable]:
        editor = self.editor
        incoming = {
            mapping[3]: (edge_id, mapping)
            for edge_id, mapping in editor._edge_id_map.items()
            if mapping[2] == instance_id
        }
        state = editor._dataflow_graph.get_state(instance_id)
        connected = sum(1 for p in spec.inputs if p.name in incoming)
        objects: list[Viewable] = [
            pn.Row(
                _heading("Inputs"),
                pn.layout.HSpacer(),
                _caption(f"{connected} of {len(spec.inputs)} connected", margin=(18, 0, 0, 0)),
                margin=0,
                sizing_mode="stretch_width",
            )
        ]
        for port in spec.inputs:
            objects.append(self._input_row(port, incoming.get(port.name), state))
        return objects

    def _input_row(self, port, wired, state) -> Viewable:
        editor = self.editor
        name = port_label(port) + (" *" if port.required else "")
        title = text_typography(
            f"{name}  {port.type or ''}".rstrip(),
            variant="body2",
            margin=0,
            sx={"fontWeight": 500},
        )
        if wired is None:
            needed = "Required, not connected" if port.required else "Not connected"
            lines = [title, _caption(needed)]
            return pmui.Paper(
                *lines,
                margin=(0, 0, 6, 0),
                sizing_mode="stretch_width",
                variant="outlined",
                sx={"p": 1},
            )
        edge_id, mapping = wired
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
        edit = pmui.IconButton(
            icon="edit", description="Inspect this connection", size="small", margin=0
        )
        edit.on_click(lambda _event: select_on_canvas(self._flow, edge_id=edge_id))
        return pmui.Paper(
            pn.Row(
                pn.Column(
                    title,
                    _caption(origin),
                    _caption(summarize_value(getattr(state, port.name, None))),
                    margin=0,
                    sizing_mode="stretch_width",
                ),
                edit,
                margin=0,
                sizing_mode="stretch_width",
            ),
            margin=(0, 0, 6, 0),
            sizing_mode="stretch_width",
            variant="outlined",
            sx={"p": 1},
        )

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
        holder = pn.Column(sizing_mode="stretch_width", margin=0)
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
            label="Show on dashboard", value=layout.get("visible", True), margin=(12, 0, 4, 0)
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
        self.collapse_button.visible = self._body.visible = not collapsed
        self.expand_button.visible = collapsed
        self._panel.width = _COLLAPSED_WIDTH if collapsed else INSPECTOR_WIDTH

    def __panel__(self):
        """Render the panel."""
        return self._panel
