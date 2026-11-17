"""Operations on dashboards, as the person using an editor may perform them.

:class:`DashboardOps` is the surface an AI agent edits dashboards through. It
works on a :class:`~panel_flowdash.editor.FlowDash` editor, so every change
goes through the same calls, component permissions and import checks as the
editor's own UI, and nothing it does can exceed what the editor's user may do.
"""

from __future__ import annotations

import copy
import typing as t

import panel as pn

from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import (
    _SESSION_EDITORS,
    FlowDash,
    _ReadOnlyEditorError,
    export_model,
)
from panel_flowdash.ui import tile_layout

from .preview import preview_editor
from .schema import component_schema
from .validation import Issue, _check_value, _suggest, validate_dashboard
from .yaml_format import dashboard_to_yaml, node_names, split_port


class OperationError(ValueError):
    """An operation was refused because of what it asked for, e.g. a port that does not exist.

    The message says what to change, so an agent can retry with a fix.
    """


class DashboardOps:
    """Read and edit dashboards through an editor, with its user's rights.

    Nodes and ports are named as in the dashboard YAML (see
    :func:`~panel_flowdash.agent.dashboard_to_yaml`): a node by its name, a
    port as ``"<node>.<port>"``. Every change applies to the editor's canvas
    and marks it dirty; nothing is stored until :meth:`save`.

    Rights follow the editor: components its ``authorize_component`` refuses
    can neither be listed nor placed, nodes of such components cannot be
    changed, dashboards its ``authorize_dashboard`` does not let the user view
    are reported as missing, and while it is ``read_only`` (the user may not
    edit the dashboard) every change is refused.

    Parameters
    ----------
    editor
        The editor to work through: the one on screen, or one without a page
        built for the purpose, e.g. ``FlowDash(components, store=store,
        authorize_component=..., authorize_dashboard=...)``.
    """

    def __init__(self, editor: FlowDash):
        self.editor = editor

    # ------------------------------------------------------------------
    # Rights
    # ------------------------------------------------------------------

    def _stored(self, dashboard_id: str) -> DashboardModel:
        editor = self.editor
        model = editor.store._load_any(dashboard_id) if editor.store is not None else None
        # A dashboard the user may not view is answered like a missing one.
        if model is None or not editor._dashboard_authorized(model, "view"):
            raise KeyError(f"No dashboard '{dashboard_id}'. Call list_dashboards to see yours.")
        return model

    def _can_read_source(self, model: DashboardModel) -> bool:
        """Whether the user may see the dashboard's components and config, as Download shows them."""
        if self.editor._dashboard_authorized(model, "edit") and not self.editor.read_only:
            return True
        return not self.editor.inspect_outputs or bool(
            model.settings.get("viewers_can_inspect", False)
        )

    def _can_sample(self) -> bool:
        """Whether the user may see output values, as the tiles' Inspect action shows them."""
        editor = self.editor
        return not editor.read_only or (
            editor.inspect_outputs and editor.settings.viewers_can_inspect
        )

    def _require_edit(self):
        editor = self.editor
        if editor.read_only:
            raise PermissionError("You may only view this dashboard, so it cannot be changed.")
        current = editor.dashboard
        if current is not None and editor.store is not None:
            stored = editor.store._load_any(current.dashboard_id)
            if stored is not None and not editor._dashboard_authorized(stored, "edit"):
                editor.read_only = True
                raise PermissionError("You no longer have edit access to this dashboard.")

    # ------------------------------------------------------------------
    # Names
    # ------------------------------------------------------------------

    def _export(self) -> dict:
        return self.editor.export_dashboard()

    def _names(self) -> dict[str, str]:
        return node_names(self._export()["items"])

    def _instance(self, name: str) -> str:
        ids = {n: iid for iid, n in self._names().items()}
        if name not in ids:
            raise OperationError(f"No node '{name}'." + _suggest(name, ids))
        return ids[name]

    def _item(self, instance_id: str) -> dict:
        return next(i for i in self.editor._tile_items if i["instance_id"] == instance_id)

    def _changeable(self, name: str) -> tuple[str, dict]:
        """Return the instance id and item of a node whose component the user may use."""
        instance_id = self._instance(name)
        item = self._item(instance_id)
        placeholder = item.get("placeholder")
        if placeholder is not None:
            if placeholder.private:
                raise PermissionError(f"'{name}' is restricted and cannot be changed.")
            raise OperationError(f"'{name}' could not be loaded ({placeholder.message}).")
        return instance_id, item

    def _port(self, ref: str, direction: str) -> tuple[str, str]:
        try:
            name, port = split_port(ref, direction)
        except ValueError as exc:
            raise OperationError(str(exc)) from exc
        instance_id, item = self._changeable(name)
        spec = self.editor.component_specs[item["component_id"]]
        ports = spec.outputs if direction == "source" else spec.inputs
        resolved = spec.resolve_name(port)
        if not any(p.name == resolved for p in ports):
            kind = "output" if direction == "source" else "input"
            raise OperationError(
                f"'{name}' ({spec.title}) has no {kind} '{port}'."
                + (_suggest(port, [p.name for p in ports]) if ports else f" It has no {kind}s.")
            )
        return instance_id, resolved

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _authorized_specs(self) -> dict:
        editor = self.editor
        return {
            cid: spec
            for cid, spec in editor.component_specs.items()
            if editor._component_authorized(cid)
        }

    def list_components(self, query: str = "") -> list[dict[str, t.Any]]:
        """List the components the user may place, with their ports and config fields.

        Parameters
        ----------
        query
            Only list components whose id, title, description or tags contain
            each of its words, ignoring case.

        Returns
        -------
        list of dict
            One :func:`~panel_flowdash.agent.component_schema` per component.
        """
        words = query.lower().split()
        entries = self.editor._component_entries
        listed = []
        for cid, spec in self._authorized_specs().items():
            text = " ".join([cid, spec.title, spec.description or "", *spec.tags]).lower()
            if all(word in text for word in words):
                listed.append(component_schema(spec, control=entries[cid].metadata.sidebar))
        return listed

    def list_dashboards(self) -> list[dict[str, t.Any]]:
        """List the stored dashboards the user may open, most recently changed first.

        Returns
        -------
        list of dict
            ``id``, ``title``, ``description`` and ``can_edit`` of each dashboard.
        """
        editor = self.editor
        if editor.store is None:
            return []
        return [
            {
                "id": model.dashboard_id,
                "title": model.title,
                "description": model.settings.get("description", ""),
                "can_edit": editor._dashboard_authorized(model, "edit"),
            }
            for model in editor.store._all_dashboards()
            if editor._dashboard_authorized(model, "view")
        ]

    def get_dashboard(self, dashboard_id: str | None = None) -> str:
        """Return a dashboard as YAML.

        Parameters
        ----------
        dashboard_id
            A stored dashboard. Defaults to the dashboard in the editor, with
            its unsaved changes.

        Raises
        ------
        KeyError
            If there is no such dashboard or the user may not open it.
        PermissionError
            If the user may view it but it hides its components from viewers,
            as Download does.
        """
        if dashboard_id is None or (
            self.editor.dashboard is not None
            and dashboard_id == self.editor.dashboard.dashboard_id
        ):
            current = self.editor.dashboard
            if current is not None and not self._can_read_source(self.editor.to_model()):
                raise PermissionError(
                    "Only people who can edit this dashboard may read its definition."
                )
            return dashboard_to_yaml(self._export())
        model = self._stored(dashboard_id)
        if not self._can_read_source(model):
            raise PermissionError(
                "Only people who can edit this dashboard may read its definition."
            )
        return dashboard_to_yaml(
            export_model(model, restricted=self.editor._restricted_ids(model))
        )

    def open_dashboard(self, dashboard_id: str) -> str:
        """Load a stored dashboard into the editor and return it as YAML.

        Unsaved changes in the editor are discarded. A dashboard the user may
        only view is opened read-only, and when it hides its definition from
        viewers (see :meth:`get_dashboard`) a note saying so is returned instead.
        """
        model = self._stored(dashboard_id)
        editor = self.editor
        editor.read_only = not editor._dashboard_authorized(model, "edit")
        editor.load_model(model)
        if not self._can_read_source(model):
            return f"Opened '{model.title}' read-only; its definition is hidden from viewers."
        return self.get_dashboard()

    def validate(self, text: str | None = None) -> list[Issue]:
        """Check YAML against the components the user may place; defaults to the editor's dashboard."""
        editor = self.editor
        entries = editor._component_entries
        return validate_dashboard(
            dashboard_to_yaml(self._export()) if text is None else text,
            self._authorized_specs(),
            settings_class=editor.settings_class,
            aliases=editor._component_aliases(),
            controls=[cid for cid, e in entries.items() if e.metadata.sidebar],
            singletons=[cid for cid, e in entries.items() if e.metadata.singleton],
        )

    def preview(self, text: str | None = None, *, rows: int = 5) -> dict[str, t.Any]:
        """Run a dashboard without showing it and report the health of each node.

        Parameters
        ----------
        text
            YAML to run in a separate editor, leaving this one untouched.
            Defaults to the editor's dashboard as it is now.
        rows
            How many rows or items each output sample shows.

        Returns
        -------
        dict
            See :func:`~panel_flowdash.agent.preview.preview_editor`. Output
            samples are left out for someone the dashboard hides its outputs
            from.
        """
        samples = self._can_sample()
        if text is None:
            return preview_editor(self.editor, self._names(), samples=samples, rows=rows)
        self._refuse_invalid(text)
        scratch = self._scratch_editor()
        try:
            scratch.import_dashboard(text)
            names = node_names(scratch.export_dashboard()["items"])
            return preview_editor(scratch, names, samples=samples, rows=rows)
        finally:
            scratch.dispose()

    def _scratch_editor(self) -> FlowDash:
        """Build an editor with the same components and rights, which nobody sees and nothing saves."""
        editor = self.editor
        doc = pn.state.curdoc
        session_editor = _SESSION_EDITORS.get(doc) if doc is not None else None
        scratch = type(editor)(
            components=editor._registry,
            include_builtin_components=False,
            authorize_component=editor.authorize_component,
            authorize_dashboard=editor.authorize_dashboard,
            settings_class=editor.settings_class,
            propagate_failures=editor.propagate_failures,
            inspect_outputs=editor.inspect_outputs,
            notifications=False,
            user=editor.user,
        )
        # The session's components keep finding the editor on screen, not this one.
        if session_editor is not None:
            _SESSION_EDITORS[doc] = session_editor
        if editor.dashboard is not None:
            scratch.dashboard = copy.deepcopy(editor.dashboard)
        return scratch

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    def _refuse_invalid(self, text: str):
        errors = [i for i in self.validate(text) if i.severity == "error"]
        if errors:
            raise OperationError(
                "The dashboard has errors:\n" + "\n".join(f"- {issue}" for issue in errors)
            )

    def replace_yaml(self, text: str) -> list[Issue]:
        """Replace the editor's dashboard with YAML, as importing a dashboard file does.

        Node names that are already on the canvas keep their components'
        identity. Components the user may not use are kept as stored or left
        out, exactly as for an imported file.

        Returns
        -------
        list of Issue
            The warnings about the loaded dashboard, e.g. inputs still unconnected.

        Raises
        ------
        OperationError
            If the YAML has errors; nothing is changed.
        """
        self._require_edit()
        self._refuse_invalid(text)
        self.editor.import_dashboard(text)
        return [i for i in self.validate() if i.severity != "error"]

    def add_node(
        self,
        component: str,
        config: dict[str, t.Any] | None = None,
        position: tuple[float, float] | None = None,
    ) -> str:
        """Place a component and return the name of its node.

        Raises
        ------
        OperationError
            If the component is unknown, placed once already when it can only
            be placed once, or *config* is invalid.
        PermissionError
            If the user may not use the component, or edit the dashboard.
        """
        self._require_edit()
        editor = self.editor
        specs = self._authorized_specs()
        component_id = editor._resolve_component_id(component)
        if component_id not in specs:
            raise OperationError(f"Unknown component '{component}'." + _suggest(component, specs))
        self._check_config(specs[component_id], config or {})
        try:
            instance_id = editor.add_component(component_id, dict(config or {}), position)
        except (KeyError, ValueError) as exc:
            raise OperationError(str(exc).strip("'\"")) from exc
        return self._names()[instance_id]

    def remove_node(self, name: str) -> None:
        """Remove a node, its edges and its tile."""
        self._require_edit()
        self.editor.remove_component(self._instance(name))

    def _check_config(self, spec, config: dict[str, t.Any]) -> dict[str, t.Any]:
        fields = {f.name for f in spec.config}
        params = (
            spec.config_state_class.param.objects("existing") if spec.config_state_class else {}
        )
        resolved = {}
        for key, value in config.items():
            name = spec.resolve_name(key)
            if name not in fields:
                raise OperationError(
                    f"'{spec.title}' has no config field '{key}'."
                    + (_suggest(key, fields) if fields else " It takes no config.")
                )
            if name in params and (reason := _check_value(params[name], value)):
                raise OperationError(f"Config '{key}': {reason}")
            resolved[name] = value
        return resolved

    def set_config(self, name: str, config: dict[str, t.Any]) -> dict[str, t.Any]:
        """Change some config fields of a node and return its whole config."""
        self._require_edit()
        instance_id, item = self._changeable(name)
        spec = self.editor.component_specs[item["component_id"]]
        self.editor._apply_config_patch(instance_id, self._check_config(spec, config))
        return copy.deepcopy(self._item(instance_id).get("config", {}))

    def connect(
        self, source: str, target: str, transform: list[dict[str, t.Any]] | None = None
    ) -> None:
        """Connect the output *source* to the input *target*, both ``"<node>.<port>"``.

        Raises
        ------
        OperationError
            If a port does not exist, the types do not match, the input is
            taken, the edge would close a cycle or the transform is invalid.
        """
        self._require_edit()
        result = self.editor.connect(
            *self._port(source, "source"), *self._port(target, "target"), transform
        )
        if result is not True:
            raise OperationError(result)

    def _edge(self, source: str, target: str) -> tuple[str, str, str, str]:
        key = (*self._port(source, "source"), *self._port(target, "target"))
        if not any(
            (e["source"], e["source_port"], e["target"], e["target_port"]) == key
            for e in self.editor.graph.edges
        ):
            raise OperationError(f"There is no edge from {source} to {target}.")
        return key

    def disconnect(self, source: str, target: str) -> None:
        """Remove the edge from the output *source* to the input *target*."""
        self._require_edit()
        self.editor.disconnect(*self._edge(source, target))

    def set_transform(
        self, source: str, target: str, transform: list[dict[str, t.Any]] | None
    ) -> None:
        """Replace the transform steps of an edge; an empty list removes them."""
        self._require_edit()
        result = self.editor.set_edge_transform(*self._edge(source, target), transform or [])
        if result is not True:
            raise OperationError(result)

    def place_tile(
        self,
        name: str,
        *,
        width: float | None = None,
        height: float | None = None,
        visible: bool | None = None,
        index: int | None = None,
    ) -> dict[str, t.Any]:
        """Change how a node's tile is laid out and return its layout entry.

        Parameters
        ----------
        width
            Percent of the row, greater than 0 and at most 100.
        height
            Height in pixels.
        visible
            False hides the tile; the component keeps running in the dataflow.
        index
            Position among the tiles, 0 for the first.
        """
        self._require_edit()
        instance_id = self._instance(name)
        editor = self.editor
        entry = editor._component_entries.get(self._item(instance_id)["component_id"])
        if entry is not None and entry.metadata.sidebar:
            raise OperationError(f"'{name}' is a control, shown in the controls bar, not a tile.")
        if width is not None and not 0 < width <= 100:
            raise OperationError(f"width is a percentage of the row, got {width}.")
        if height is not None and height <= 0:
            raise OperationError(f"height must be a positive number of pixels, got {height}.")
        patch = {
            key: value
            for key, value in (("width", width), ("height", height), ("visible", visible))
            if value is not None
        }
        if index is not None:
            self._move_tile(instance_id, index)
        if patch or index is not None:
            tile_layout.update_tile(editor, instance_id, **patch)
        return {
            k: v
            for k, v in tile_layout.tile_entry(editor, instance_id).items()
            if k != "instance_id"
        }

    def _move_tile(self, instance_id: str, index: int):
        editor = self.editor
        layout = [dict(e) for e in tile_layout._layout(editor)]
        tiles = [
            item["instance_id"]
            for item in editor._tile_items
            if not tile_layout._in_sidebar(editor, item)
        ]
        ranked = {e["instance_id"]: e.get("index") for e in layout if "instance_id" in e}
        order = sorted(
            tiles,
            key=lambda iid: (ranked.get(iid) is None, ranked.get(iid) or 0, tiles.index(iid)),
        )
        order.remove(instance_id)
        order.insert(max(0, min(index, len(order))), instance_id)
        by_id = {e["instance_id"]: e for e in layout if "instance_id" in e}
        for position, iid in enumerate(order):
            by_id.setdefault(iid, {"instance_id": iid})["index"] = position
        editor._pending_tile_layout = [
            *by_id.values(),
            *(e for e in layout if "instance_id" not in e),
        ]

    def set_settings(self, settings: dict[str, t.Any]) -> dict[str, t.Any]:
        """Change some dashboard settings and return them all."""
        self._require_edit()
        editor = self.editor
        params = editor.settings.param.objects("existing")
        for key, value in settings.items():
            if key not in editor._settings_names:
                raise OperationError(
                    f"No setting '{key}'." + _suggest(key, editor._settings_names)
                )
            if reason := _check_value(params[key], value):
                raise OperationError(f"Setting '{key}': {reason}")
        editor.settings.param.update(**settings)
        return editor._settings_values()

    def save(self) -> str:
        """Store the editor's dashboard and return its id.

        Raises
        ------
        PermissionError
            If the user may not edit the dashboard.
        DashboardStoreError
            If the store refuses, e.g. because it was saved from another
            session since it was opened.
        """
        self._require_edit()
        try:
            model = self.editor.save()
        except _ReadOnlyEditorError as exc:
            raise PermissionError(str(exc)) from exc
        return model.dashboard_id
