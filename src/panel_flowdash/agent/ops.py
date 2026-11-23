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
import yaml

from panel_flowdash.config_check import (
    CHECK_TIMEOUT,
    CheckResult,
    check_node,
    check_node_sync,
    effective_config,
    node_config,
    node_inputs,
    recheck_node,
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
from panel_flowdash.input_literals import check_literal
from panel_flowdash.parameters import ContractError, ParameterError
from panel_flowdash.report import (
    ReportError,
    block_kind,
    insert_block,
    move_block,
    remove_block,
    report_blocks,
    set_title,
    update_block,
)
from panel_flowdash.turns import _agent_source
from panel_flowdash.ui import tile_layout

from .preview import node_health, output_failures, preview_editor
from .schema import component_schema
from .validation import Issue, _check_value, _suggest, validate_dashboard
from .yaml_format import (
    RESTRICTED_COMPONENT,
    DashboardYAMLError,
    _Dumper,
    _report as _report_doc,
    dashboard_from_dict,
    dashboard_to_dict,
    dashboard_to_yaml,
    editor_restricted,
    load_yaml,
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


def _check_issues(path: str, result: CheckResult) -> list[Issue]:
    """Return a config check's problems as errors at *path*, and its warnings as warnings."""
    return [Issue(path, problem) for problem in result] + [
        Issue(path, warning, "warning") for warning in result.warnings
    ]


def _incoming(edges: t.Any, name: str) -> list[str]:
    """Return the edges into node *name* of a dashboard document, in a comparable form."""
    if not isinstance(edges, list):
        return []
    return sorted(
        json.dumps(edge, sort_keys=True, default=str)
        for edge in edges
        if isinstance(edge, dict) and str(edge.get("to", "")).split(".", 1)[0] == name
    )


class NothingChanged(Exception):
    """A request that changes nothing, by design rather than by mistake; its message says why."""


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

# The frame choices of a tile and the layout value each stores; None drops the tile's own.
_FRAMES = {"default": None, "framed": True, "unframed": False}

_REVISION = re.compile(r"# revision: (?P<token>[0-9a-f]+)\s*")


def _revision(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def without_provenance(doc: t.Any, turns: t.Sequence[dict] | None = None) -> t.Any:
    """Return a copy of a YAML document without its turns, node origins and report block origins.

    Only the editor records who made which change, so they are dropped from
    YAML an agent or another caller of :class:`DashboardOps` writes. Pass the
    YAML turns of the dashboard the document is meant for as *turns*: when
    its report has turn blocks, they are checked against those instead.
    """
    if not isinstance(doc, dict):
        return doc
    doc = {key: value for key, value in doc.items() if key != "turns"}
    report = doc.get("report")
    if isinstance(report, dict) and isinstance(report.get("blocks"), list):
        blocks = [
            {key: value for key, value in block.items() if key != "origin"}
            if isinstance(block, dict)
            else block
            for block in report["blocks"]
        ]
        doc["report"] = {**report, "blocks": blocks}
        if turns and any(isinstance(b, dict) and "turn" in b for b in blocks):
            doc["turns"] = copy.deepcopy(list(turns))
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


def _yaml(value: t.Any) -> str:
    return yaml.dump(value, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88)


_REPORT_OPS = ("insert", "update", "move", "remove", "title")


def _report_operation(report: dict, operation: t.Any) -> tuple[dict, str]:
    """Apply one operation of :meth:`DashboardOps.edit_report` to *report*, nodes by name.

    Returns the changed report and a label for the turn's summary.
    """
    if not isinstance(operation, dict):
        raise ReportError("op", f"each operation must be a mapping, got {operation!r}.")
    op = operation.get("op")
    if op not in _REPORT_OPS:
        raise ReportError("op", f"must be one of {', '.join(_REPORT_OPS)}, got {op!r}.")
    allowed = {
        "insert": ("block", "index"),
        "update": ("index", "block"),
        "move": ("index", "to"),
        "remove": ("index",),
        "title": ("title",),
    }[op]
    unknown = [key for key in operation if key not in ("op", *allowed)]
    if unknown:
        raise ReportError(
            unknown[0], f"is not a key of a {op} operation; it takes {', '.join(allowed)}."
        )
    blocks = report.get("blocks") or []
    index = operation.get("index")
    if op == "title":
        return set_title(report, operation.get("title")), "Renamed the report"
    if op == "insert":
        block = operation.get("block")
        report = insert_block(report, index, block)
        return report, _block_label("Added", block)
    if op == "update" and isinstance(operation.get("block"), dict):
        report = update_block(report, index, **operation["block"])
        return report, _block_label("Changed", report["blocks"][index])
    if op == "update":
        raise ReportError("block", "give the keys to change as a mapping.")
    if op == "move":
        return move_block(report, index, operation.get("to")), "Reordered the report"
    removed = blocks[index] if isinstance(index, int) and 0 <= index < len(blocks) else None
    report = remove_block(report, index)
    return report, _block_label("Removed", removed)


def _block_label(verb: str, block: t.Any) -> str:
    """Describe a change of a report block for a turn's summary, naming nodes but no prose."""
    kind = block_kind(block) if isinstance(block, dict) and block else None
    where = "from" if verb == "Removed" else "in" if verb == "Changed" else "to"
    if kind == "node":
        if verb == "Changed":
            return f"Changed how the report shows {block['node']}"
        return f"{verb} {block['node']} {where} the report"
    if kind == "text":
        return {
            "Added": "Wrote a paragraph in the report",
            "Changed": "Rewrote a paragraph in the report",
        }.get(verb, "Removed a paragraph from the report")
    noun = {"heading": "a heading", "group": "a group", "turn": "a question"}.get(kind, "a block")
    return f"{verb} {noun} {where} the report"


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

    #: Whether the edit tools publish the dashboard as a version after each edit.
    versioned: bool = True

    def __init__(self, editor: FlowDash):
        self.editor = editor
        # What the config check found on the latest add_node, set_config,
        # connect or disconnect, warnings included.
        self.last_check: CheckResult = CheckResult()

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
            if self.editor._placement_refusal(cid) is not None:
                continue
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

    def _turns(self) -> list[dict]:
        """Return the editor's turns as the YAML writes them."""
        return dashboard_to_dict(self.editor).get("turns") or []

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
        issues += self._refused_nodes(doc, standalone)
        if checks:
            for path, spec, config, inputs, stored in self._doc_checks(doc, issues, standalone):
                problems = run_check_sync(
                    spec, config, inputs, editor=editor, timeout=self.check_timeout, stored=stored
                )
                issues += _check_issues(path, problems)
        return issues

    def _refused_nodes(self, doc: dict, standalone: bool) -> list[Issue]:
        """Return an error for each node *doc* adds of a component the user may not add.

        A node the canvas already has under that name, of that component, is
        left alone: dashboards keep the components an admin turned off since.
        """
        editor = self.editor
        placed = {}
        if not standalone:
            names = self._names()
            placed = {
                names[item["instance_id"]]: editor._resolve_component_id(item["component_id"])
                for item in editor._tile_items
                if item["instance_id"] in names
            }
        issues = []
        for name, node in (doc.get("nodes") or {}).items():
            if not isinstance(node, dict) or not isinstance(node.get("component"), str):
                continue
            cid = editor._resolve_component_id(node["component"])
            if cid not in editor._component_entries or placed.get(name) == cid:
                continue
            refusal = editor._placement_refusal(cid)
            if refusal is not None:
                title = editor._component_entries[cid].title
                issues.append(
                    Issue(f"nodes.{name}.component", f"'{title}' can't be added: {refusal}.")
                )
        return issues

    def _doc_checks(
        self, doc: dict, issues: list[Issue], standalone: bool
    ) -> list[tuple[str, t.Any, dict, dict, dict | None]]:
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
            inputs, stored = {}, None
            item = live.get(ids.get(name, ""))
            same = (
                item is not None
                and "placeholder" not in item
                and editor._resolve_component_id(item["component_id"]) == spec.component_id
            )
            if same:
                stored = node_config(editor, item["instance_id"], spec)
            if same and _incoming(doc.get("edges"), name) == _incoming(canvas_edges, name):
                inputs = node_inputs(editor, item["instance_id"], spec)
            config = effective_config(spec, node.get("config"))
            checks.append((f"nodes.{name}.config", spec, config, inputs, stored))
        return checks

    def _canvas_issues(self, problems: dict[str, CheckResult]) -> list[Issue]:
        names = self._names()
        return [
            issue
            for instance_id, found in problems.items()
            for issue in _check_issues(
                f"nodes.{names.get(instance_id, instance_id)}.config", found
            )
        ]

    def _checked_nodes(self, editor: FlowDash) -> list[str]:
        return [
            item["instance_id"]
            for item in editor._tile_items
            if "placeholder" not in item
            and getattr(editor._component_specs.get(item["component_id"]), "check_config", None)
        ]

    def _canvas_problems(self, editor: FlowDash) -> dict[str, CheckResult]:
        """Check the config of every node of *editor* that has a check, against its live inputs.

        Returns the results with problems or warnings, by instance id.
        """
        found = {
            instance_id: check_node_sync(editor, instance_id, timeout=self.check_timeout)
            for instance_id in self._checked_nodes(editor)
        }
        return {iid: result for iid, result in found.items() if result or result.warnings}

    async def _canvas_problems_async(self, editor: FlowDash) -> dict[str, CheckResult]:
        instance_ids = self._checked_nodes(editor)
        results = await asyncio.gather(
            *(check_node(editor, iid, timeout=self.check_timeout) for iid in instance_ids)
        )
        return {
            iid: result
            for iid, result in zip(instance_ids, results, strict=True)
            if result or result.warnings
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
        return without_provenance(self._parse(text), self._turns())

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

    def _scratch_editor(self, cls: type | None = None) -> FlowDash:
        """Build an editor with the same components and rights, which nobody sees and nothing saves.

        *cls* is the editor class to build, by default the editor's own.
        """
        editor = self.editor
        doc = pn.state.curdoc
        session_editor = _SESSION_EDITORS.get(doc) if doc is not None else None
        scratch = (cls or type(editor))(
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
            history=False,
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
        doc = without_provenance(raw, self._turns())
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
            Canvas position ``(x, y)``. Leave it out: the editor places the
            node by its dataflow and keeps it in place as it is connected.
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
            message suggests a free one. Warnings of the config check do not
            refuse it; they are left in :attr:`last_check`.
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
        name = self._place_node(spec, config, position, name)
        self.last_check = problems
        return name

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
        name = self._place_node(spec, config, position, name)
        self.last_check = problems
        return name

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
        refusal = self.editor._placement_refusal(component_id)
        if refusal is not None:
            raise OperationError(f"'{specs[component_id].title}' can't be added: {refusal}.")
        spec = specs[component_id]
        return spec, self._check_config(spec, config or {})

    def _place_node(self, spec, config, position, name) -> str:
        try:
            instance_id = self.editor.add_component(
                spec.component_id, dict(config or {}), position, instance_id=name, arrange=True
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

    def set_config(
        self, name: str, config: dict[str, t.Any], inputs: dict[str, t.Any] | None = None
    ) -> dict[str, t.Any]:
        """Change some config fields of a node and return its whole config.

        *inputs* sets literal values of its unconnected inputs in the same
        edit, as :meth:`set_inputs` does.

        Raises
        ------
        OperationError
            If a field does not exist or takes no such value, or the
            component's config check refuses the result; nothing is changed.
            Warnings of the config check do not refuse it; they are left in
            :attr:`last_check`.
        """
        literals = self._inputs_patch(name, inputs) if inputs else {}
        instance_id, patch = self._config_patch(name, config)
        problems = check_node_sync(self.editor, instance_id, patch, timeout=self.check_timeout)
        _refuse_config(f"The config of '{name}' was not changed", problems)
        return self._apply_config(instance_id, patch, problems, literals)

    async def set_config_async(
        self, name: str, config: dict[str, t.Any], inputs: dict[str, t.Any] | None = None
    ) -> dict[str, t.Any]:
        """Like :meth:`set_config`, running the component's config check off the event loop."""
        literals = self._inputs_patch(name, inputs) if inputs else {}
        instance_id, patch = self._config_patch(name, config)
        dashboard_id = self._dashboard_id()
        problems = await check_node(self.editor, instance_id, patch, timeout=self.check_timeout)
        self._check_still_open(dashboard_id)
        _refuse_config(f"The config of '{name}' was not changed", problems)
        if self._config_patch(name, config)[0] != instance_id:
            raise OperationError(f"'{name}' was replaced while its config was being checked.")
        return self._apply_config(instance_id, patch, problems, literals)

    def _config_patch(self, name: str, config: dict[str, t.Any]) -> tuple[str, dict[str, t.Any]]:
        self._require_edit()
        instance_id, item = self._changeable(name)
        spec = self.editor.component_specs[item["component_id"]]
        return instance_id, self._check_config(spec, config)

    def _apply_config(
        self,
        instance_id: str,
        patch: dict[str, t.Any],
        checked: CheckResult,
        inputs: dict[str, t.Any] | None = None,
    ) -> dict[str, t.Any]:
        self.editor._apply_config_patch(instance_id, patch, inputs)
        self.last_check = checked
        return copy.deepcopy(self._item(instance_id).get("config", {}))

    def set_inputs(self, name: str, inputs: dict[str, t.Any]) -> CheckResult:
        """Set literal values on inputs of a node, used while nothing is connected to them.

        A connection always wins over a literal, which applies again once the
        connection is removed. A value of None removes the input's literal.

        Returns
        -------
        CheckResult
            What the config check of the node's component found with the new
            inputs, like :meth:`connect`.

        Raises
        ------
        OperationError
            If the node has no such input, the input cannot hold a literal,
            e.g. a DataFrame, or it refuses the value; nothing is changed.
        """
        instance_id = self._set_inputs(name, inputs)
        return self._rechecked(
            check_node_sync(self.editor, instance_id, timeout=self.check_timeout)
        )

    async def set_inputs_async(self, name: str, inputs: dict[str, t.Any]) -> CheckResult:
        """Like :meth:`set_inputs`, checking the node off the event loop."""
        instance_id = self._set_inputs(name, inputs)
        return self._rechecked(
            await recheck_node(self.editor, instance_id, timeout=self.check_timeout)
        )

    def _set_inputs(self, name: str, inputs: dict[str, t.Any]) -> str:
        resolved = self._inputs_patch(name, inputs)
        instance_id = self._changeable(name)[0]
        self.editor._apply_config_patch(instance_id, {}, resolved)
        return instance_id

    def _inputs_patch(self, name: str, inputs: dict[str, t.Any]) -> dict[str, t.Any]:
        """Return *inputs* by current port name, refusing any the node's inputs would."""
        self._require_edit()
        if not isinstance(inputs, dict):
            raise OperationError(
                f"inputs must be a mapping of input names to values, not {inputs!r}."
            )
        _, item = self._changeable(name)
        spec = self.editor.component_specs[item["component_id"]]
        ports = {p.name: p for p in spec.inputs}
        resolved = {}
        for key, value in inputs.items():
            port = ports.get(spec.resolve_name(key))
            if port is None:
                raise OperationError(
                    f"'{name}' ({spec.title}) has no input '{key}'."
                    + (_suggest(key, list(ports)) if ports else " It has no inputs.")
                )
            if value is not None and (reason := check_literal(port, value)) is not None:
                raise OperationError(f"Input '{key}' of '{name}': {reason}")
            resolved[port.name] = value
        return resolved

    def connect(
        self, source: str, target: str, transform: list[dict[str, t.Any]] | None = None
    ) -> CheckResult:
        """Connect the output *source* to the input *target*, both ``"<node>.<port>"``.

        Returns
        -------
        CheckResult
            What the config check of the target node's component found once
            the node has its new input, see ``register(check_config=)``; empty
            without a check. Problems do not undo the connection: the node is
            reported invalid by :meth:`preview` and :meth:`validate` until its
            config is fixed. It is left in :attr:`last_check` too.

        Raises
        ------
        OperationError
            If a port does not exist, the types do not match, the input is
            taken, the edge would close a cycle or the transform is invalid.
        """
        target_id = self._connect(source, target, transform)
        return self._rechecked(check_node_sync(self.editor, target_id, timeout=self.check_timeout))

    async def connect_async(
        self, source: str, target: str, transform: list[dict[str, t.Any]] | None = None
    ) -> CheckResult:
        """Like :meth:`connect`, checking the target node off the event loop.

        Checks of one node that follow each other quickly, e.g. on several
        connections into it, are coalesced: at most one runs while another
        waits, and the latest state wins.
        """
        target_id = self._connect(source, target, transform)
        return self._rechecked(
            await recheck_node(self.editor, target_id, timeout=self.check_timeout)
        )

    def _connect(self, source: str, target: str, transform) -> str:
        self._require_edit()
        source_ref = self._port(source, "source")
        target_id, target_port = self._port(target, "target")
        result = self.editor.connect(*source_ref, target_id, target_port, transform)
        if result is not True:
            if not transform and (
                kind := self.editor._bridge(*source_ref, target_id, target_port)
            ):
                column = '{"op": "column", "column": "<name>"}, ' if kind == "column" else ""
                result += (
                    f" Connect it with a transform reducing it to one value, e.g. "
                    f'[{column}{{"op": "aggregate", "how": "sum"}}].'
                )
            raise OperationError(result)
        return target_id

    def _rechecked(self, result: CheckResult) -> CheckResult:
        self.last_check = result
        return result

    def _edge(self, source: str, target: str) -> tuple[str, str, str, str]:
        key = (*self._port(source, "source"), *self._port(target, "target"))
        if not any(
            (e["source"], e["source_port"], e["target"], e["target_port"]) == key
            for e in self.editor.graph.edges
        ):
            raise OperationError(f"There is no edge from {source} to {target}.")
        return key

    def disconnect(self, source: str, target: str) -> CheckResult:
        """Remove the edge from the output *source* to the input *target*.

        Returns what the config check of the target node found without the
        input, like :meth:`connect`.
        """
        target_id = self._disconnect(source, target)
        return self._rechecked(check_node_sync(self.editor, target_id, timeout=self.check_timeout))

    async def disconnect_async(self, source: str, target: str) -> CheckResult:
        """Like :meth:`disconnect`, checking the target node off the event loop."""
        target_id = self._disconnect(source, target)
        return self._rechecked(
            await recheck_node(self.editor, target_id, timeout=self.check_timeout)
        )

    def _disconnect(self, source: str, target: str) -> str:
        self._require_edit()
        edge = self._edge(source, target)
        self.editor.disconnect(*edge)
        return edge[2]

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
        frame: t.Literal["default", "framed", "unframed"] | None = None,
        area: t.Literal["grid", "top", "sidebar"] | None = None,
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
            Position among the tiles, 0 for the first, or among the nodes of
            its area when it is beside the grid.
        frame
            "framed" or "unframed" draws the tile as a card in view mode or
            not; "default" follows the component, then the dashboard's
            ``tile_cards`` setting.
        area
            "grid" shows the node as a tile; "top" in the bar above the grid
            and "sidebar" in the panel left of it, which only controls, KPIs
            and text fit.
        """
        self._require_edit()
        instance_id = self._instance(name)
        editor = self.editor
        item = self._item(instance_id)
        if area is not None and area not in tile_layout.AREAS:
            raise OperationError(f"area is one of {', '.join(tile_layout.AREAS)}, got {area!r}.")
        if area not in (None, tile_layout.GRID) and not tile_layout.fits_beside(editor, item):
            raise OperationError(tile_layout.refusal(editor, item, area))
        placed = area or tile_layout.area_of(editor, item)
        sized = any(v is not None for v in (width, height, visible, frame))
        if placed not in (None, tile_layout.GRID) and sized:
            raise OperationError(
                f"'{name}' is in the {tile_layout.AREA_NAMES[placed]}, not a tile on the grid; "
                "width, height, visible and frame apply on the grid (area='grid')."
            )
        if width is not None and not 0 < width <= 100:
            raise OperationError(f"width is a percentage of the row, got {width}.")
        if height is not None and height <= 0:
            raise OperationError(f"height must be a positive number of pixels, got {height}.")
        if frame is not None and frame not in _FRAMES:
            raise OperationError(f"frame is one of {', '.join(_FRAMES)}, got {frame!r}.")
        patch = {
            key: value
            for key, value in (("width", width), ("height", height), ("visible", visible))
            if value is not None
        }
        if frame is not None:
            patch["framed"] = _FRAMES[frame]
        if area is not None or (placed not in (None, tile_layout.GRID) and index is not None):
            tile_layout.move_to_area(editor, instance_id, placed, index=index)
        elif index is not None:
            tile_layout.move_tile(editor, instance_id, index)
        if patch:
            tile_layout.update_tile(editor, instance_id, **patch)
        return {
            k: v
            for k, v in tile_layout.tile_entry(editor, instance_id).items()
            if k != "instance_id"
        }

    def group_nodes(
        self, nodes: list[str], label: str | None = None, name: str | None = None
    ) -> dict[str, t.Any]:
        """Frame nodes together on the canvas as one group, which can be collapsed into one node.

        Returns the group's name and its ports: one input per outside output
        feeding the group, with the inner ports it connects to, and one output
        per inner output read from outside.
        """
        self._require_edit()
        ids = [self._instance(node) for node in nodes]
        try:
            group_id = self.editor.group_nodes(ids, label, group_id=name)
        except ValueError as exc:
            raise OperationError(str(exc)) from exc
        return self.group(group_id)

    def group(self, name: str) -> dict[str, t.Any]:
        """Return a group's label, nodes, whether it is collapsed, and its ports."""
        editor = self.editor
        group = editor.groups.get(name)
        if group is None:
            raise NotFound(f"No group '{name}'." + _suggest(name, list(editor.groups)))
        names = self._names()
        inputs, outputs = editor._group_ports(group)

        def ports(items):
            return [
                {"name": port.name, "connects": [f"{names[n]}.{p}" for n, p in port.ends]}
                for port in items
            ]

        return {
            "group": group.id,
            "label": group.title,
            "nodes": [names[n] for n in group.nodes],
            "collapsed": group.collapsed,
            "inputs": ports(inputs),
            "outputs": ports(outputs),
        }

    def ungroup(self, name: str) -> list[str]:
        """Remove a group's frame, keeping its nodes; returns their names."""
        self._require_edit()
        if name not in self.editor.groups:
            raise NotFound(f"No group '{name}'." + _suggest(name, list(self.editor.groups)))
        names = self._names()
        return [names[n] for n in self.editor.ungroup(name)]

    def arrange_canvas(self) -> None:
        """Lay out every node on the canvas by its dataflow, sources left and views right.

        Nodes added without a position are placed this way already; this
        also moves the nodes people placed, so call it only when asked to
        tidy the canvas.
        """
        self._require_edit()
        self.editor.arrange_canvas()

    def arrange_layout(self) -> None:
        """Lay out every visible tile by kind for each screen size, see :meth:`arrange_tiles`."""
        self.arrange_tiles()

    def arrange_tiles(self, order: t.Sequence[str] | None = None, style: str = "grid") -> None:
        """Lay out every visible tile again in one edit, for each screen size.

        With the ``"grid"`` style controls go first, then KPIs in a row,
        charts two to a row and tables at full width, with headings kept in
        front of the tile they title; ``"stack"`` puts the tiles one to a row
        at full width. *order* names nodes whose tiles go first, in that
        order within their kind; the others follow in their current order.
        This also rearranges the tiles people laid out.
        """
        self._require_edit()
        if style not in ("grid", "stack"):
            raise OperationError(f"Unknown style {style!r}; use 'grid' or 'stack'.")
        ids = [self._instance(name) for name in order or ()]
        self.editor.arrange_layout(order=ids, style=style)

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

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> dict[str, t.Any]:
        """Return the open dashboard's report with nodes by name, as the YAML writes it.

        A dashboard without a report of its own gives the one derived from
        its groups and tile layout, with ``derived`` set; editing it makes it
        the dashboard's own.
        """
        self._require_source()
        editor = self.editor
        resolved = report_blocks(editor)
        names = self._names()
        report: dict[str, t.Any] = {}
        own = editor.report
        if own is not None and own.get("title"):
            report["title"] = own["title"]
        blocks = []
        for block in resolved.blocks:
            entry = copy.deepcopy(dict(block))
            if "node" in entry:
                entry["node"] = names.get(entry["node"], entry["node"])
            blocks.append(entry)
        report["blocks"] = blocks
        if resolved.derived:
            report["derived"] = True
        return report

    def _report_revision(self, report: dict | None = None) -> str:
        """Return a token identifying the report as it is now, nodes by name."""
        report = self.report() if report is None else report
        return _revision(json.dumps(report, sort_keys=True, default=str))

    def report_revision(self) -> str:
        """Return the token :meth:`get_report` writes, which :meth:`edit_report` can check."""
        return self._report_revision()

    def _check_report_revision(self, revision: str | None):
        if revision is not None and revision != self._report_revision():
            raise OperationError(
                "The report changed since it was read, e.g. by the user's own edits, so its "
                "indices may have moved. Nothing was changed; call get_report again and apply "
                "your change to that."
            )

    def get_report(self) -> str:
        """Return the report as YAML, each block preceded by a ``# <index>`` comment.

        The first line is a ``# revision: <token>`` comment, which
        :meth:`edit_report` and :meth:`set_report` check. The indices are
        those :meth:`edit_report` takes. A question not run yet, or one
        answered, has its question in the comment.
        """
        report = self.report()
        turns = {turn.id: turn for turn in self.editor.turns}
        lines = [f"# revision: {self._report_revision(report)}"]
        if report.pop("derived", False):
            lines.append(
                "# derived: the dashboard has no report of its own yet; these blocks follow its "
                "groups and tiles. Editing the report makes them its own."
            )
        if report.get("title"):
            lines.append(_yaml({"title": report["title"]}).rstrip())
        lines.append("blocks:" if report["blocks"] else "blocks: []")
        for index, block in enumerate(report["blocks"]):
            note = ""
            turn = turns.get(block.get("turn"))
            if turn is not None:
                state = "question not run yet" if turn.status == "pending" else "answered"
                question = " ".join((turn.question or "").split())
                if len(question) > 160:
                    question = question[:159] + "…"
                note = f", {state}: {question}" if question else f", {state}"
            lines.append(f"# {index}{note}")
            lines.append(_yaml([block]).rstrip())
        return "\n".join(lines) + "\n"

    def report_text(self) -> str:
        """Return the report as Markdown, as a reader sees it, placeholders filled with current values.

        Nodes are named, with their title, rather than rendered; a group
        lists its shown nodes and an answered question the nodes of its answer.
        """
        from panel_flowdash.report import fill_report_text, group_members, turn_outputs
        from panel_flowdash.ui.tile_layout import tile_title

        self._require_source()
        editor = self.editor
        resolved = report_blocks(editor)
        names = self._names()
        items = {item["instance_id"]: item for item in editor._tile_items}
        turns = {turn.id: turn for turn in editor.turns}

        def node(instance_id: str, block: t.Mapping | None = None) -> str:
            item = items.get(instance_id)
            title = tile_title(editor, item) if item is not None else instance_id
            line = f"[node {names.get(instance_id, instance_id)}: {title}"
            if block is not None and block.get("show") == "table":
                line += ", as a table"
            line += "]"
            if block is not None and block.get("caption"):
                line += f"\n*{block['caption']}*"
            return line

        parts = [f"# {resolved.title}"]
        if resolved.filters:
            parts.append("Filters: " + ", ".join(names.get(iid, iid) for iid in resolved.filters))
        for block in resolved.blocks:
            kind = block_kind(block)
            if kind == "heading":
                parts.append("#" * (block.get("level", 1) + 1) + " " + block["heading"])
            elif kind == "text":
                parts.append(fill_report_text(editor, block["text"]))
            elif kind == "node":
                parts.append(node(block["node"], block))
            elif kind == "group":
                group = editor.groups.get(block["group"])
                label = group.title if group is not None and group.title else block["group"]
                members = group_members(editor, block["group"])
                parts.append(f"## {label}\n\n" + "\n".join(node(iid) for iid in members))
            else:
                turn = turns.get(block["turn"])
                if turn is None:
                    continue
                question = turn.question or "A question"
                if turn.status == "pending":
                    parts.append(f"> Not run yet ({turn.id}): {question}")
                else:
                    answer = [node(iid) for iid in turn_outputs(editor, turn.id)]
                    parts.append(
                        f"### {question}\n\n"
                        + ("\n".join(answer) or "(the nodes of this answer are gone)")
                    )
        return "\n\n".join(parts) + "\n"

    def _report_ids(self) -> dict[str, str]:
        return {name: iid for iid, name in self._names().items()}

    def _report_text_problems(self, report: dict[str, t.Any], known: list[Issue]) -> list[Issue]:
        """Return warnings about text placeholders that read a column or row the current data lacks."""
        from panel_flowdash.report import text_problems

        seen = {(i.path, i.message) for i in known}
        issues = []
        for i, block in enumerate(report.get("blocks") or []):
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            path = f"report.blocks[{i}].text"
            for problem in text_problems(self.editor, block["text"]):
                if (path, problem) not in seen:
                    seen.add((path, problem))
                    issues.append(Issue(path, problem, "warning"))
        return issues

    def _apply_report(self, report: dict[str, t.Any], labels: list[str]) -> list[Issue]:
        """Store *report*, nodes by name, as one edit described by *labels*; return its warnings."""
        editor = self.editor
        try:
            stored = _report_doc(
                report,
                self._report_ids(),
                set(editor.groups),
                {turn.id for turn in editor.turns},
            )
        except DashboardYAMLError as exc:
            raise OperationError(str(exc)) from exc
        doc = dashboard_to_dict(editor)
        doc["report"] = copy.deepcopy(report)
        warnings = [
            issue
            for issue in self.validate(doc, checks=False)
            if issue.path.startswith("report") and issue.severity != "error"
        ]
        warnings += self._report_text_problems(report, warnings)
        try:
            editor.set_report(stored, label=labels, coalesce=False)
        except ValueError as exc:
            raise OperationError(str(exc)) from exc
        return warnings

    def edit_report(
        self, operations: list[dict[str, t.Any]], revision: str | None = None
    ) -> list[Issue]:
        """Change the report's blocks by index, in one edit, and return warnings about the result.

        Each operation is a mapping with an ``op``:

        - ``{"op": "insert", "block": {...}, "index": 2}`` inserts a block
          before the block at ``index``, or last without it;
        - ``{"op": "update", "index": 2, "block": {...}}`` changes keys of a
          block, a null value removing the key;
        - ``{"op": "move", "index": 2, "to": 0}`` moves a block to ``to``;
        - ``{"op": "remove", "index": 2}`` removes a block;
        - ``{"op": "title", "title": "..."}`` renames the report, an empty
          title giving it the dashboard's.

        Operations apply in order, each to the blocks the earlier ones left.
        A dashboard without a report of its own starts from the derived one.
        Nothing changes unless every operation applies and the result names
        only nodes, groups and turns the dashboard has. With *revision*, the
        token :meth:`get_report` gave, nothing changes either if the report
        changed since, as its indices may have moved.

        Raises
        ------
        OperationError
            If an operation is malformed, the result is invalid or the report
            changed since *revision*.
        """
        self._require_edit()
        self._check_report_revision(revision)
        if not isinstance(operations, list) or not operations:
            raise OperationError("operations: give a list of at least one operation.")
        report = self.report()
        report.pop("derived", None)
        labels: list[str] = []
        for i, operation in enumerate(operations):
            try:
                report, label = _report_operation(report, operation)
            except ReportError as exc:
                raise OperationError(f"operations[{i}].{exc.key}: {exc.reason}") from None
            if label not in labels:
                labels.append(label)
        return self._apply_report(report, labels)

    def set_report(self, blocks_yaml: str) -> list[Issue]:
        """Replace the report with YAML: a list of blocks, or a mapping with ``title`` and ``blocks``.

        A list keeps the report's title. Returns warnings about the result.
        YAML starting with the ``# revision:`` line :meth:`get_report`
        writes is refused if the report changed since.

        Raises
        ------
        OperationError
            If the YAML is malformed or names a node, group or turn the dashboard does not have.
        """
        self._require_edit()
        if isinstance(blocks_yaml, str):
            self._check_report_revision(read_revision(blocks_yaml))
        try:
            value = load_yaml(blocks_yaml) if isinstance(blocks_yaml, str) else blocks_yaml
        except DashboardYAMLError as exc:
            raise OperationError(str(exc)) from exc
        current = self.report()
        derived = current.pop("derived", False)
        if isinstance(value, list):
            report = {**current, "blocks": value}
        elif isinstance(value, dict):
            report = dict(value)
        else:
            raise OperationError(
                "blocks_yaml: give a list of blocks, or a mapping with title and blocks."
            )
        label = "Wrote the report" if derived else "Rewrote the report"
        return self._apply_report(report, [label])

    def ask_in_report(self, question: str, after: int | None = None) -> str:
        """Add a question to the report, not run yet, and return its turn id.

        Its block goes after the block at index *after*, or last without it.
        Running it (:meth:`run_pending`, or the Run button in the app) asks
        the agent, whose answer then fills the block.
        """
        self._require_edit()
        editor = self.editor
        blocks = report_blocks(editor).blocks
        if after is not None and (
            isinstance(after, bool) or not isinstance(after, int) or not 0 <= after < len(blocks)
        ):
            raise OperationError(
                f"after: must be the index of a block, 0 to {len(blocks) - 1}, got {after!r}."
                if blocks
                else "after: the report has no blocks yet; leave after out."
            )
        try:
            return editor.ask_in_report(question, None if after is None else after + 1)
        except ValueError as exc:
            raise OperationError(str(exc)) from exc

    def pending_question(self, turn: str) -> str:
        """Return the question of the pending turn *turn*, raising OperationError if there is none."""
        found = next((t for t in self.editor.turns if t.id == turn), None)
        if found is None or found.status != "pending":
            pending = [t.id for t in self.editor.turns if t.status == "pending"]
            raise OperationError(
                f"No question waiting to be run has the id {turn!r}."
                + (f" Those waiting are: {', '.join(pending)}." if pending else "")
            )
        return found.question or ""

    def run_pending(self, turn: str, run_id: str | None = None) -> str:
        """Answer the pending turn *turn* with the agent request *run_id*'s next edits; return its question.

        Defaults to the panel-agents run the call is made in. The request's
        edits so far keep their turn; those it makes from now on are the
        answer, see :meth:`FlowDash.answer_pending`.
        """
        self._require_edit()
        question = self.pending_question(turn)
        if run_id is None:
            source = _agent_source()
            run_id = source.run if source is not None else None
        self.editor.answer_pending(turn, run_id)
        return question

    async def export_report(self, format: str = "html", *, include_pending: bool = False):
        """Export the report as a file, as the Report view's Export does, and return the file.

        Pending questions are never run; with *include_pending* they are
        written as not answered yet. Raises
        :class:`~panel_flowdash.export.ExportUnavailable` when the format needs
        a package that is not installed.
        """
        from panel_flowdash.export import check_format, export_report_async

        try:
            check_format(format)
        except ValueError as exc:
            raise OperationError(str(exc)) from None
        return await export_report_async(self.editor, format, include_pending=include_pending)

    def send_file(self, file) -> bool:
        """Make the user's browser save an exported *file*; False when there is no page to send it to."""
        export = getattr(self.editor, "_report_export", None)
        if export is None or not export.downloader._models:
            return False
        export.send(file)
        return True

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def parameters(self) -> dict[str, dict[str, t.Any]]:
        """Return the dashboard's parameters by name, as the YAML writes them."""
        self._require_source()
        return copy.deepcopy(dashboard_to_dict(self.editor).get("parameters") or {})

    def declare_parameter(
        self,
        name: str,
        node: str,
        field: str,
        *,
        type: str = "value",
        description: str | None = None,
    ) -> dict[str, t.Any]:
        """Declare a parameter on a node's config field, or redeclare one, and return it.

        What its contract expects is computed by the node's component, see
        :meth:`~panel_flowdash.editor.FlowDash.declare_parameter`.

        Raises
        ------
        OperationError
            If the node does not exist or the name, field or type is malformed.
        """
        self._require_edit()
        instance_id, _ = self._changeable(node)
        try:
            self.editor.declare_parameter(name, instance_id, field, type, description)
        except ParameterError as exc:
            raise OperationError(f"Parameter '{name}': {exc}") from None
        return self.parameters()[name]

    def remove_parameter(self, name: str) -> None:
        """Remove a parameter; its node keeps the value the field holds."""
        self._require_edit()
        if not self.editor.remove_parameter(name):
            raise OperationError(
                f"'{name}' is not a parameter." + _suggest(name, self.parameters())
            )

    def bind_parameters(self, values: dict[str, t.Any]) -> list[Issue]:
        """Write values into their parameters' fields as one edit, and return the warnings.

        Values given as text are read as their parameter's type. The
        contract of each parameter checks its value first, then each changed
        node's config check runs; nothing changes unless all pass. Parameters
        on nodes the user may not use are not found.

        Raises
        ------
        OperationError
            If a name is not a parameter, a value is not of its type, a
            contract refuses a value (naming the nodes that would fail) or a
            config check refuses the result.
        """
        self._require_edit()
        editor = self.editor
        try:
            patches, problems, warnings = editor._binding(values, timeout=self.check_timeout)
            editor._apply_binding(values, patches)
        except (ParameterError, ContractError) as exc:
            raise OperationError(str(exc)) from None
        self.last_check = CheckResult(warnings=warnings)
        return [
            Issue(f"parameters.{name}", str(problem), "warning")
            for name, items in problems.items()
            for problem in items
        ]

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
