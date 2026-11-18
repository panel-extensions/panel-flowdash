"""Operations on dashboards, as the person using an editor may perform them.

:class:`DashboardOps` is the surface an AI agent edits dashboards through. It
works on a :class:`~panel_flowdash.editor.FlowDash` editor, so every change
goes through the same calls, component permissions and import checks as the
editor's own UI, and nothing it does can exceed what the editor's user may do.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import typing as t

import panel as pn

from panel_flowdash.config_check import (
    CHECK_TIMEOUT,
    check_node,
    check_node_sync,
    effective_config,
    node_inputs,
    run_check,
    run_check_sync,
)
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import (
    _SESSION_EDITORS,
    FlowDash,
    _ReadOnlyEditorError,
    copy_context,
    export_model,
)
from panel_flowdash.ui import tile_layout

from .preview import node_health, output_failures, preview_editor
from .schema import component_schema
from .validation import Issue, _check_value, _suggest, validate_dashboard
from .yaml_format import (
    RESTRICTED_COMPONENT,
    DashboardYAMLError,
    dashboard_from_dict,
    dashboard_to_dict,
    dashboard_to_yaml,
    editor_restricted,
    node_names,
    parse_yaml,
    split_port,
)


class OperationError(ValueError):
    """An operation was refused because of what it asked for, e.g. a port that does not exist.

    The message says what to change, so an agent can retry with a fix.
    """


def _refuse_config(what: str, problems: list[str]):
    """Raise an :class:`OperationError` listing the problems a config check found."""
    if len(problems) == 1:
        raise OperationError(f"{what}: {problems[0]}")
    if problems:
        raise OperationError(f"{what}:\n" + "\n".join(f"- {p}" for p in problems))


def _incoming(edges: t.Any, name: str) -> list[str]:
    """Return the edges into node *name* of a dashboard document, in a comparable form."""
    if not isinstance(edges, list):
        return []
    return sorted(
        json.dumps(edge, sort_keys=True, default=str)
        for edge in edges
        if isinstance(edge, dict) and str(edge.get("to", "")).split(".", 1)[0] == name
    )


class OutputUnavailable(Exception):
    """A node has no output value to read yet, or it failed, as a preview reports it.

    ``status`` is ``"waiting"`` or ``"failed"``. It is not an
    :class:`OperationError`, since asking again does not help until the
    dashboard changes.
    """

    def __init__(self, name: str, status: str, message: str):
        super().__init__(
            f"'{name}' {'has failed' if status == 'failed' else 'is waiting'}: {message}"
        )
        self.name = name
        self.status = status
        self.message = message


class NotFound(OperationError, KeyError):
    """The dashboard asked for does not exist, or the user may not open it."""

    def __str__(self) -> str:
        """Return the message, without the quotes ``KeyError`` adds."""
        return str(self.args[0]) if self.args else ""


# How long a preview or a bulk replace may take to import and start its components.
DEFAULT_TIMEOUT = 30

_REVISION = re.compile(r"# revision: (?P<token>[0-9a-f]+)\s*")


def _revision(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def without_provenance(doc: t.Any) -> t.Any:
    """Return a copy of a YAML document without its turns and node origins.

    Only the editor records who made which change, so they are dropped from
    YAML an agent or another caller of :class:`DashboardOps` writes.
    """
    if not isinstance(doc, dict):
        return doc
    doc = {key: value for key, value in doc.items() if key != "turns"}
    nodes = doc.get("nodes")
    if isinstance(nodes, dict):
        doc["nodes"] = {
            name: (
                {key: value for key, value in node.items() if key != "origin"}
                if isinstance(node, dict)
                else node
            )
            for name, node in nodes.items()
        }
    return doc


def read_revision(text: str) -> str | None:
    """Return the revision a YAML text from :meth:`DashboardOps.get_dashboard` was read at.

    Only a ``# revision: <token>`` comment on the first line counts.
    """
    first = text.split("\n", 1)[0]
    match = _REVISION.fullmatch(first)
    return match["token"] if match else None


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

    # Seconds a component's config check may take, see ``register(check_config=)``.
    check_timeout: float = CHECK_TIMEOUT

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
            raise NotFound(f"No dashboard '{dashboard_id}'. Call list_dashboards to see yours.")
        return model

    def _can_read_source(self, model: DashboardModel, current: bool = False) -> bool:
        """Whether the user may see the dashboard's components and config, as Download shows them.

        With *current*, *model* is the dashboard in the editor, whose
        ``read_only`` state also applies.
        """
        editable = not (current and self.editor.read_only)
        if editable and self.editor._dashboard_authorized(model, "edit"):
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

    def _source_visible(self) -> bool:
        current = self.editor.dashboard
        return current is None or self._can_read_source(self.editor.to_model(), current=True)

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

    def nodes(self, component: str | None = None) -> list[dict[str, str]]:
        """List the nodes of the editor's dashboard in the order they were placed.

        Parameters
        ----------
        component
            Only list nodes of this component id, or of a former id of it.

        Returns
        -------
        list of dict
            ``name`` and ``component`` of each node. A node of a component
            the user may not use is listed as ``flowdash:restricted``, as
            :meth:`get_dashboard` writes it, and never matches *component*.

        Raises
        ------
        PermissionError
            If the dashboard hides its definition from the user, as Download does.
        """
        self._require_source()
        wanted = None if component is None else self.editor._resolve_component_id(component)
        items = self._export()["items"]
        names = node_names(items)
        live = {i["instance_id"]: i for i in self.editor._tile_items}
        listed = []
        for item in items:
            name = names[item["instance_id"]]
            placeholder = live.get(item["instance_id"], {}).get("placeholder")
            if placeholder is not None and placeholder.private:
                if wanted is None:
                    listed.append({"name": name, "component": RESTRICTED_COMPONENT})
                continue
            resolved = self.editor._resolve_component_id(item["component_id"])
            if wanted is None or resolved == wanted:
                listed.append({"name": name, "component": item["component_id"]})
        return listed

    def node_config(self, name: str) -> dict[str, t.Any]:
        """Return a copy of a node's config.

        Raises
        ------
        OperationError
            If there is no such node, or it could not be loaded.
        PermissionError
            If the dashboard hides its definition from the user, or the node
            is of a component the user may not use.
        """
        self._require_source()
        _, item = self._readable(name)
        return copy.deepcopy(item.get("config") or {})

    def node_output(self, ref: str, port: str | None = None) -> t.Any:
        """Return the current value of an output, as the nodes it feeds receive it.

        The value is the live object, not a copy: a DataFrame or another
        mutable value must not be changed.

        Parameters
        ----------
        ref
            The output as ``"<node>.<port>"``, or the node's name with *port*.
        port
            The output port, when *ref* is only the node's name.

        Raises
        ------
        OutputUnavailable
            If the node has no value to give, as :meth:`preview` reports it:
            ``status`` is ``"waiting"`` while a required input is missing and
            ``"failed"`` when an input failed upstream or the output itself
            failed. Retrying the same call does not help until the dashboard
            changes, so it is not an :class:`OperationError`.
        OperationError
            If there is no such node or output, or the node could not be loaded.
        PermissionError
            If the dashboard hides its definition or its outputs from the
            user, or the node is of a component the user may not use.
        """
        self._require_source()
        if not self._can_sample():
            raise PermissionError(
                "Only people who can inspect this dashboard may read its outputs."
            )
        if port is None:
            name, dot, port = ref.partition(".")
            if not dot:
                raise OperationError(f"Name the output as '{ref}.<port>'.")
        elif "." in ref:
            raise OperationError(
                f"Pass the output as '{ref}' or as a node name and a port, not both."
            )
        else:
            name = ref
        instance_id, item = self._readable(name)
        spec = self.editor.component_specs[item["component_id"]]
        resolved = spec.resolve_name(port)
        if not any(p.name == resolved for p in spec.outputs):
            raise OperationError(
                f"'{name}' ({spec.title}) has no output '{port}'."
                + (
                    _suggest(port, [p.name for p in spec.outputs])
                    if spec.outputs
                    else " It has no outputs."
                )
            )
        status, message = node_health(self.editor, instance_id)
        if status != "ok":
            raise OutputUnavailable(name, status, message)
        if (failed := output_failures(self.editor, instance_id).get(resolved)) is not None:
            raise OutputUnavailable(name, "failed", failed)
        state = self.editor.graph.get_state(instance_id)
        return getattr(state, resolved, None) if state is not None else None

    def _readable(self, name: str) -> tuple[str, dict]:
        """Return the instance id and item of a node whose definition the user may read."""
        instance_id = self._instance(name)
        item = self._item(instance_id)
        placeholder = item.get("placeholder")
        if placeholder is not None:
            if placeholder.private:
                raise PermissionError(f"'{name}' is restricted.")
            raise OperationError(f"'{name}' could not be loaded ({placeholder.message}).")
        return instance_id, item

    def _require_source(self):
        if not self._source_visible():
            raise PermissionError(
                "Only people who can edit this dashboard may read its definition."
            )

    def get_dashboard(self, dashboard_id: str | None = None) -> str:
        """Return a dashboard as YAML.

        The YAML of the editor's dashboard starts with a ``# revision: <token>``
        comment identifying the canvas it was read from; :meth:`replace_yaml`
        refuses text read from a canvas that has changed since.

        Parameters
        ----------
        dashboard_id
            A stored dashboard. Defaults to the dashboard in the editor, with
            its unsaved changes.

        Raises
        ------
        NotFound
            If there is no such dashboard or the user may not open it.
        PermissionError
            If the user may view it but it hides its components from viewers,
            as Download does.
        """
        if dashboard_id is None or (
            self.editor.dashboard is not None
            and dashboard_id == self.editor.dashboard.dashboard_id
        ):
            self._require_source()
            text = dashboard_to_yaml(self.editor)
            return f"# revision: {_revision(text)}\n{text}"
        model = self._stored(dashboard_id)
        if not self._can_read_source(model):
            raise PermissionError(
                "Only people who can edit this dashboard may read its definition."
            )
        restricted = self.editor._restricted_ids(model)
        hidden = [e.to_dict() for e in model.edges if {e.source, e.target} & restricted]
        return dashboard_to_yaml(export_model(model, restricted=restricted), hidden)

    def revision(self) -> str:
        """Return a token identifying the editor's canvas as it is now."""
        return _revision(dashboard_to_yaml(self.editor))

    def open_dashboard(self, dashboard_id: str, *, discard: bool = False) -> str:
        """Load a stored dashboard into the editor and return it as YAML.

        A dashboard the user may only view is opened read-only, and when it
        hides its definition from viewers (see :meth:`get_dashboard`) a note
        saying so is returned instead.

        Raises
        ------
        OperationError
            If the editor has unsaved changes and *discard* is not set.
        """
        model = self._stored(dashboard_id)
        editor = self.editor
        if editor.dirty and not discard:
            raise OperationError(
                "The open dashboard has unsaved changes; save them first or pass discard=True."
            )
        editor.read_only = not editor._dashboard_authorized(model, "edit")
        editor.load_model(model)
        if not self._can_read_source(model):
            return f"Opened '{model.title}' read-only; its definition is hidden from viewers."
        return self.get_dashboard()

    def _parse(self, text: str | dict) -> dict:
        """Read YAML text once, with the format's limits, into a document."""
        if isinstance(text, dict):
            return text
        try:
            return parse_yaml(text)
        except DashboardYAMLError as exc:
            raise OperationError(str(exc)) from exc

    def _provenance_issues(self, doc: dict) -> list[Issue]:
        """Warn that turns and node origins *doc* changes are ignored, see :func:`without_provenance`."""
        if not isinstance(doc, dict):
            return []
        current = dashboard_to_dict(self.editor)
        nodes = doc.get("nodes") if isinstance(doc.get("nodes"), dict) else {}
        current_nodes = current.get("nodes") or {}
        changed = "turns" in doc and doc["turns"] != current.get("turns", [])
        # Only nodes already on the canvas: a renamed or copied node keeping its
        # origin line is a new node, whose origin is ignored as expected.
        for name, node in nodes.items():
            if isinstance(node, dict) and "origin" in node and name in current_nodes:
                changed |= node["origin"] != current_nodes[name].get("origin")
        if not changed:
            return []
        return [
            Issue(
                "turns",
                "changes to turns and node origins are ignored, since FlowDash records them.",
                "warning",
            )
        ]

    def validate(
        self, text: str | dict | None = None, *, standalone: bool = False, checks: bool = True
    ) -> list[Issue]:
        """Check YAML against the components the user may place and the canvas it is meant for.

        Defaults to the editor's dashboard. The canvas's restricted nodes and
        their connections must stay as they are, see
        :func:`~panel_flowdash.agent.validate_dashboard`, unless *standalone*
        is set, for YAML of a new dashboard rather than one for this canvas.

        With *checks*, the config of each node whose component declares a
        ``check_config`` is checked too, against the live inputs of the node
        of the same name on the canvas, and its problems are errors at
        ``nodes.<name>.config``.
        """
        editor = self.editor
        entries = editor._component_entries
        restricted_nodes, restricted_edges = (
            (None, None) if standalone else editor_restricted(editor)
        )
        try:
            doc = dashboard_to_dict(editor) if text is None else self._document(text)
        except OperationError as exc:
            path, _, message = str(exc).partition(": ")
            return [Issue(path, message)]
        issues = validate_dashboard(
            doc,
            self._authorized_specs(),
            settings_class=editor.settings_class,
            aliases=editor._component_aliases(),
            controls=[cid for cid, e in entries.items() if e.metadata.sidebar],
            singletons=[cid for cid, e in entries.items() if e.metadata.singleton],
            restricted_nodes=restricted_nodes,
            restricted_edges=restricted_edges,
        )
        # A node already on the canvas whose component is not offered (any more)
        # is kept as is, like on import, so it does not stop the YAML loading.
        names = self._names()
        unavailable = {
            f"nodes.{names[item['instance_id']]}.component"
            for item in editor._tile_items
            if "placeholder" in item and not item["placeholder"].private
        }
        issues = [
            Issue(i.path, f"{i.message} It is kept on the dashboard as is.", "warning")
            if i.path in unavailable and i.message.startswith("Unknown component")
            else i
            for i in issues
        ]
        if checks:
            for path, spec, config, inputs in self._doc_checks(doc, issues, standalone):
                problems = run_check_sync(
                    spec, config, inputs, editor=editor, timeout=self.check_timeout
                )
                issues += [Issue(path, problem) for problem in problems]
        return issues

    def _doc_checks(
        self, doc: dict, issues: list[Issue], standalone: bool
    ) -> list[tuple[str, t.Any, dict, dict]]:
        """Return what to check for each node of *doc* whose component has a config check.

        A node that already has an error is left out. A node gets the live
        inputs of the canvas node of the same name only when it is of the same
        component and its incoming edges are the same; otherwise it gets none.
        """
        editor = self.editor
        specs = self._authorized_specs()
        canvas_edges = [] if standalone else (dashboard_to_dict(editor).get("edges") or [])
        failing = {".".join(i.path.split(".")[:2]) for i in issues if i.severity == "error"}
        ids = {} if standalone else {n: iid for iid, n in self._names().items()}
        live = {i["instance_id"]: i for i in editor._tile_items}
        checks = []
        for name, node in (doc.get("nodes") or {}).items():
            if not isinstance(node, dict) or f"nodes.{name}" in failing:
                continue
            spec = specs.get(editor._resolve_component_id(str(node.get("component"))))
            if spec is None or spec.check_config is None:
                continue
            inputs = {}
            item = live.get(ids.get(name, ""))
            if (
                item is not None
                and "placeholder" not in item
                and editor._resolve_component_id(item["component_id"]) == spec.component_id
                and _incoming(doc.get("edges"), name) == _incoming(canvas_edges, name)
            ):
                inputs = node_inputs(editor, item["instance_id"], spec)
            config = effective_config(spec, node.get("config"))
            checks.append((f"nodes.{name}.config", spec, config, inputs))
        return checks

    def _canvas_issues(self, problems: dict[str, list[str]]) -> list[Issue]:
        names = self._names()
        return [
            Issue(f"nodes.{names.get(instance_id, instance_id)}.config", problem)
            for instance_id, found in problems.items()
            for problem in found
        ]

    def _checked_nodes(self, editor: FlowDash) -> list[str]:
        return [
            item["instance_id"]
            for item in editor._tile_items
            if "placeholder" not in item
            and getattr(editor._component_specs.get(item["component_id"]), "check_config", None)
        ]

    def _canvas_problems(self, editor: FlowDash) -> dict[str, list[str]]:
        """Check the config of every node of *editor* that has a check, against its live inputs."""
        found = {
            instance_id: check_node_sync(editor, instance_id, timeout=self.check_timeout)
            for instance_id in self._checked_nodes(editor)
        }
        return {instance_id: problems for instance_id, problems in found.items() if problems}

    async def _canvas_problems_async(self, editor: FlowDash) -> dict[str, list[str]]:
        instance_ids = self._checked_nodes(editor)
        results = await asyncio.gather(
            *(check_node(editor, iid, timeout=self.check_timeout) for iid in instance_ids)
        )
        return {
            iid: problems for iid, problems in zip(instance_ids, results, strict=True) if problems
        }

    def preview(self, text: str | None = None, *, rows: int = 5) -> dict[str, t.Any]:
        """Run a dashboard without showing it and report the health of each node.

        In a running server use :meth:`preview_async`.

        Parameters
        ----------
        text
            YAML to run in a separate editor, leaving this one untouched.
            Names map to the nodes of this editor's canvas, as for
            :meth:`replace_yaml`; YAML that names none of them is previewed
            as a new dashboard. Defaults to the editor's dashboard as it is now.
        rows
            How many rows or items each output sample shows, at most 20.

        Returns
        -------
        dict
            See :func:`~panel_flowdash.agent.preview.preview_editor`. Output
            samples are left out for someone the dashboard hides its outputs
            from, and only the overall status is given to someone it hides
            its definition from.
        """
        if text is None:
            return self._preview(self.editor, rows, self._canvas_problems(self.editor))
        data = self._preview_data(text)
        scratch = self._scratch_editor()
        try:
            scratch.import_dashboard(data)
            return self._preview(scratch, rows, self._canvas_problems(scratch))
        finally:
            scratch.dispose()

    async def preview_async(
        self, text: str | None = None, *, rows: int = 5, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, t.Any]:
        """Like :meth:`preview`, importing the components' modules off the event loop.

        *timeout* bounds the imports and async component bodies; creating a
        component synchronously still runs on the event loop.
        """
        if text is None:
            invalid = await self._canvas_problems_async(self.editor)
            return self._preview(self.editor, rows, invalid)
        data = self._preview_data(text)
        scratch = self._scratch_editor()
        try:
            model = scratch._imported_model(data)
            await self._within(scratch.load_model_async(model), timeout)
            invalid = await self._canvas_problems_async(scratch)
            return self._preview(scratch, rows, invalid)
        finally:
            scratch.dispose()

    def _document(self, text: str | dict) -> dict:
        return without_provenance(self._parse(text))

    def _preview_data(self, text: str) -> dict:
        doc = self._document(text)
        canvas = self.editor.export_dashboard()
        # YAML that names none of the canvas's nodes is a new draft, previewed on its own.
        standalone = not set(doc.get("nodes") or {}) & set(node_names(canvas["items"]).values())
        self._refuse_invalid(doc, standalone=standalone)
        # Names map to this canvas, so restricted stand-ins find their stored items.
        return dashboard_from_dict(doc, base=None if standalone else canvas)

    @staticmethod
    async def _within(awaitable, timeout: float):
        try:
            await asyncio.wait_for(awaitable, timeout)
        except TimeoutError as exc:
            raise OperationError(
                f"Loading the dashboard's components took longer than {timeout:g} seconds."
            ) from exc

    def _preview(
        self, editor: FlowDash, rows: int, invalid: dict[str, list[str]] | None = None
    ) -> dict[str, t.Any]:
        names = node_names(editor.export_dashboard()["items"])
        if not self._source_visible():
            report = preview_editor(editor, names, samples=False, invalid=invalid)
            return {
                "status": report["status"],
                "message": "This dashboard hides its definition from viewers.",
            }
        return preview_editor(
            editor, names, samples=self._can_sample(), rows=rows, invalid=invalid
        )

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
            import_unknown_components=editor.import_unknown_components,
            settings_class=editor.settings_class,
            propagate_failures=editor.propagate_failures,
            inspect_outputs=editor.inspect_outputs,
            notifications=False,
            user=editor.user,
            context=copy_context(editor.context),
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

    def _refuse_invalid(self, doc: dict, *, standalone: bool = False):
        issues = self.validate(doc, standalone=standalone, checks=False)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise OperationError(
                "The dashboard has errors:\n" + "\n".join(f"- {issue}" for issue in errors)
            )

    def _check_replace(
        self, text: str, revision: str | None
    ) -> tuple[dict, str, str | None, list[Issue]]:
        """Check a replace may go ahead.

        Returns the document without turns and origins, the revision, the
        dashboard id and the warning that changed turns or origins are ignored.
        """
        self._require_edit()
        revision = revision or read_revision(text)
        if revision is None:
            raise OperationError(
                "Replacing needs the revision the YAML was read at: start from "
                "get_dashboard's output and keep its first line, '# revision: ...'."
            )
        self._check_current(revision, self._dashboard_id())
        raw = self._parse(text)
        doc = without_provenance(raw)
        self._refuse_invalid(doc)
        return doc, revision, self._dashboard_id(), self._provenance_issues(raw)

    def _dashboard_id(self) -> str | None:
        current = self.editor.dashboard
        return current.dashboard_id if current is not None else None

    def _check_current(self, revision: str, dashboard_id: str | None):
        if revision != self.revision() or dashboard_id != self._dashboard_id():
            raise OperationError(
                "The dashboard changed since this YAML was read, e.g. by the user's own "
                "edits. Call get_dashboard again and apply your change to that."
            )

    def replace_yaml(self, text: str, *, revision: str | None = None) -> list[Issue]:
        """Replace the editor's dashboard with YAML, as importing a dashboard file does.

        Node names that are already on the canvas keep their components'
        identity. The dashboard's turns and the origins of its nodes are
        kept as the editor recorded them, and those the YAML gives are
        ignored: nodes already on the canvas keep their origin and new nodes
        get the turn of whoever replaces the dashboard, the agent or the
        person. Restricted nodes and their connections must stay as
        :meth:`get_dashboard` wrote them; a restricted node can only be taken
        off with :meth:`remove_node`. In a running server use
        :meth:`replace_yaml_async`.

        Parameters
        ----------
        text
            The whole dashboard in the YAML format.
        revision
            The revision the text is based on. Defaults to the ``# revision:``
            first line :meth:`get_dashboard` writes; one of the two is
            required. Pass ``revision=ops.revision()`` to replace the canvas
            whatever it holds now.

        Returns
        -------
        list of Issue
            The warnings about the loaded dashboard, e.g. inputs still
            unconnected or turns the YAML changed to no effect, one for each
            node that failed once loaded, and as errors the problems the config
            checks of its components found, see ``register(check_config=)``. A
            node whose check fails is loaded all the same.

        Raises
        ------
        OperationError
            If the YAML has errors, has no revision, or the canvas changed
            since that revision; nothing is changed.
        """
        doc, _, _, ignored = self._check_replace(text, revision)
        title = self._title_issue(doc)
        self.editor.import_dashboard(doc, keep_turns=True)
        checked = self._canvas_issues(self._canvas_problems(self.editor))
        return title + ignored + checked + self._warnings() + self._failures()

    async def replace_yaml_async(
        self, text: str, *, revision: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> list[Issue]:
        """Like :meth:`replace_yaml`, importing the components' modules off the event loop.

        *timeout* bounds the imports. The revision and the open dashboard are
        checked again once they are done, so edits made meanwhile are never lost.
        """
        doc, revision, dashboard_id, ignored = self._check_replace(text, revision)
        editor = self.editor
        components = {
            node.get("component")
            for node in (doc.get("nodes") or {}).values()
            if isinstance(node, dict)
        }
        await self._within(editor.ensure_components_loaded_async(components), timeout)
        self._require_edit()
        self._check_current(revision, dashboard_id)
        title = self._title_issue(doc)
        editor.import_dashboard(doc, keep_turns=True)
        checked = self._canvas_issues(await self._canvas_problems_async(editor))
        return title + ignored + checked + self._warnings() + self._failures()

    def _title_issue(self, doc: dict) -> list[Issue]:
        current = self.editor.dashboard
        title = doc.get("title")
        if current is None or title in (None, current.title):
            return []
        return [
            Issue(
                "title",
                f"is ignored: the dashboard keeps its title {current.title!r}. Only the user "
                "can rename it.",
                "warning",
            )
        ]

    def _warnings(self) -> list[Issue]:
        return [i for i in self.validate(checks=False) if i.severity != "error"]

    def _failures(self) -> list[Issue]:
        """Warn about each node whose own output failed; the nodes it feeds are not repeated."""
        names = self._names()
        issues = []
        for item in self.editor._tile_items:
            if "placeholder" in item:
                continue
            failed = output_failures(self.editor, item["instance_id"])
            if failed:
                reasons = "; ".join(f"output '{p}' failed: {text}" for p, text in failed.items())
                name = names.get(item["instance_id"], item["instance_id"])
                issues.append(Issue(f"nodes.{name}", f"The node failed: {reasons}", "warning"))
        return issues

    def add_node(
        self,
        component: str,
        config: dict[str, t.Any] | None = None,
        position: tuple[float, float] | None = None,
        name: str | None = None,
    ) -> str:
        """Place a component and return the name of its node.

        Parameters
        ----------
        component
            A component id, e.g. ``"Views/kpi"``.
        config
            Values for some of its config fields.
        position
            Canvas position ``(x, y)``; defaults to the next free slot.
        name
            The node's name, e.g. ``"paid_by_region"``. It becomes the node's
            instance id, as for a node a YAML file names, so the YAML, the
            export and the store keep it. It starts with a letter or
            underscore, holds only letters, digits, ``_`` and ``-`` but no
            ``__``, is at most 64 characters long, and no node on the
            dashboard may have it. Defaults to a
            generated name like ``kpi_3fa2c1``.

        Raises
        ------
        OperationError
            If the component is unknown, placed once already when it can only
            be placed once, *config* is invalid or refused by the component's
            config check, or *name* is invalid or taken; for a taken name the
            message suggests a free one.
        PermissionError
            If the user may not use the component, or edit the dashboard.
        """
        spec, resolved = self._new_node(component, config)
        problems = run_check_sync(
            spec,
            effective_config(spec, resolved),
            {},
            editor=self.editor,
            timeout=self.check_timeout,
        )
        _refuse_config(f"'{spec.title}' was not placed", problems)
        return self._place_node(spec, config, position, name)

    async def add_node_async(
        self,
        component: str,
        config: dict[str, t.Any] | None = None,
        position: tuple[float, float] | None = None,
        name: str | None = None,
    ) -> str:
        """Like :meth:`add_node`, running the component's config check off the event loop."""
        spec, resolved = self._new_node(component, config)
        dashboard_id = self._dashboard_id()
        problems = await run_check(
            spec,
            effective_config(spec, resolved),
            {},
            editor=self.editor,
            timeout=self.check_timeout,
        )
        self._check_still_open(dashboard_id)
        _refuse_config(f"'{spec.title}' was not placed", problems)
        self._require_edit()
        return self._place_node(spec, config, position, name)

    def _check_still_open(self, dashboard_id: str | None):
        """Refuse to go on when another dashboard was opened while a check ran."""
        if self._dashboard_id() != dashboard_id:
            raise PermissionError(
                "the user opened another dashboard while the config was being checked, so "
                "nothing was changed. Stop and ask the user what to do with the dashboard "
                "they are looking at now."
            )

    def _new_node(self, component: str, config: dict[str, t.Any] | None) -> tuple[t.Any, dict]:
        self._require_edit()
        specs = self._authorized_specs()
        component_id = self.editor._resolve_component_id(component)
        if component_id not in specs:
            raise OperationError(f"Unknown component '{component}'." + _suggest(component, specs))
        spec = specs[component_id]
        return spec, self._check_config(spec, config or {})

    def _place_node(self, spec, config, position, name) -> str:
        try:
            instance_id = self.editor.add_component(
                spec.component_id, dict(config or {}), position, instance_id=name
            )
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
        """Change some config fields of a node and return its whole config.

        Raises
        ------
        OperationError
            If a field does not exist or takes no such value, or the
            component's config check refuses the result; nothing is changed.
        """
        instance_id, patch = self._config_patch(name, config)
        problems = check_node_sync(self.editor, instance_id, patch, timeout=self.check_timeout)
        _refuse_config(f"The config of '{name}' was not changed", problems)
        return self._apply_config(instance_id, patch)

    async def set_config_async(self, name: str, config: dict[str, t.Any]) -> dict[str, t.Any]:
        """Like :meth:`set_config`, running the component's config check off the event loop."""
        instance_id, patch = self._config_patch(name, config)
        dashboard_id = self._dashboard_id()
        problems = await check_node(self.editor, instance_id, patch, timeout=self.check_timeout)
        self._check_still_open(dashboard_id)
        _refuse_config(f"The config of '{name}' was not changed", problems)
        if self._config_patch(name, config)[0] != instance_id:
            raise OperationError(f"'{name}' was replaced while its config was being checked.")
        return self._apply_config(instance_id, patch)

    def _config_patch(self, name: str, config: dict[str, t.Any]) -> tuple[str, dict[str, t.Any]]:
        self._require_edit()
        instance_id, item = self._changeable(name)
        spec = self.editor.component_specs[item["component_id"]]
        return instance_id, self._check_config(spec, config)

    def _apply_config(self, instance_id: str, patch: dict[str, t.Any]) -> dict[str, t.Any]:
        self.editor._apply_config_patch(instance_id, patch)
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
            tile_layout.move_tile(editor, instance_id, index)
        if patch:
            tile_layout.update_tile(editor, instance_id, **patch)
        return {
            k: v
            for k, v in tile_layout.tile_entry(editor, instance_id).items()
            if k != "instance_id"
        }

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
