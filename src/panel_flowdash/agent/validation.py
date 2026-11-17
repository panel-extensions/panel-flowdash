"""Checking a dashboard in the YAML format against the components an editor offers."""

from __future__ import annotations

import copy
import difflib
import typing as t
from collections import defaultdict
from dataclasses import dataclass

import param

from panel_flowdash.component_spec import ComponentSpec
from panel_flowdash.dataflow_engine import _is_list_port
from panel_flowdash.port_types import port_types_compatible
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.transforms import parse_transform

from .yaml_format import (
    DashboardYAMLError,
    dashboard_from_dict,
    is_dashboard_yaml,
    parse_yaml,
    split_ref,
)

RESTRICTED = "flowdash:restricted"


@dataclass(frozen=True)
class Issue:
    """A problem found in a dashboard.

    Attributes
    ----------
    path
        Where it is, e.g. ``nodes.kpi.config.metric`` or ``edges[2].to``.
    message
        What is wrong and, where there is one, how to fix it.
    severity
        ``"error"`` for a problem that stops the dashboard from loading as
        written, ``"warning"`` for one it loads with, such as an input that is
        required but not connected yet.
    """

    path: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        """Return the issue as ``<path>: <message>``."""
        return f"{self.path}: {self.message}"


def _suggest(name: str, options: t.Iterable[str]) -> str:
    options = sorted(set(options))
    close = difflib.get_close_matches(name, options, n=3, cutoff=0.5)
    if close:
        return f" Did you mean {' or '.join(repr(c) for c in close)}?"
    if options and len(options) <= 12:
        return f" Available: {', '.join(options)}."
    return ""


def _check_value(p: param.Parameter, value) -> str | None:
    try:
        copy.copy(p)._validate(value)
    except Exception as exc:
        return str(exc)
    return None


class _Checker:
    def __init__(self, specs, settings_class, aliases, singletons):
        self.specs = specs
        self.settings_class = settings_class
        self.aliases = dict(aliases or {})
        self.singletons = set(singletons)
        self.issues: list[Issue] = []

    def error(self, path: str, message: str):
        self.issues.append(Issue(path, message))

    def warn(self, path: str, message: str):
        self.issues.append(Issue(path, message, "warning"))

    def spec_for(self, component_id: str) -> ComponentSpec | None:
        return self.specs.get(self.aliases.get(component_id, component_id)) or self.specs.get(
            component_id
        )

    def check_settings(self, settings: dict):
        params = self.settings_class.param.objects("existing")
        for key, value in settings.items():
            p = params.get(key)
            if p is None or key == "name":
                self.warn(
                    f"settings.{key}",
                    "is not a setting of this app and is kept as is."
                    + _suggest(key, [n for n in params if n != "name"]),
                )
                continue
            reason = _check_value(p, value)
            if reason:
                self.error(f"settings.{key}", reason)

    def check_nodes(self, doc_nodes: dict):
        singletons = defaultdict(list)
        for name, node in doc_nodes.items():
            path = f"nodes.{name}"
            component_id = node["component"]
            if component_id == RESTRICTED:
                if node.get("config"):
                    self.error(
                        f"{path}.config",
                        "A restricted node keeps its stored config; remove 'config'.",
                    )
                continue
            spec = self.spec_for(component_id)
            if spec is None:
                titles = {s.title: cid for cid, s in self.specs.items()}
                hint = _suggest(component_id, self.specs)
                if not hint and component_id in titles:
                    hint = f" Did you mean '{titles[component_id]}'?"
                self.error(f"{path}.component", f"Unknown component '{component_id}'.{hint}")
                continue
            if spec.component_id in self.singletons:
                singletons[spec.component_id].append(name)
            self.check_config(path, spec, node.get("config") or {})
        for component_id, names in singletons.items():
            if len(names) > 1:
                for name in names[1:]:
                    self.error(
                        f"nodes.{name}.component",
                        f"'{self.specs[component_id].title}' can only be placed once per "
                        f"dashboard; it is already placed as '{names[0]}'.",
                    )

    def check_config(self, path: str, spec: ComponentSpec, config: dict):
        fields = {f.name: f for f in spec.config}
        params = (
            spec.config_state_class.param.objects("existing") if spec.config_state_class else {}
        )
        for key, value in config.items():
            name = spec.resolve_name(key)
            if name not in fields:
                if fields:
                    message = f"'{spec.title}' has no config field '{key}'." + _suggest(
                        key, fields
                    )
                else:
                    message = f"'{spec.title}' takes no config; remove '{key}'."
                self.error(f"{path}.config.{key}", message)
                continue
            p = params.get(name)
            if p is not None:
                reason = _check_value(p, value)
                if reason:
                    self.error(f"{path}.config.{key}", reason)

    def check_edges(self, doc: dict):
        nodes = doc.get("nodes") or {}
        components = {name: node["component"] for name, node in nodes.items()}
        incoming = defaultdict(list)
        # Inputs fed by a restricted node, through an edge whose source port is hidden.
        hidden: dict[tuple[str, str], str] = {}
        seen = set()
        graph = defaultdict(set)
        for i, edge in enumerate(doc.get("edges") or []):
            path = f"edges[{i}]"
            source, source_port = split_ref(edge["from"], f"{path}.from")
            target, target_port = split_ref(edge["to"], f"{path}.to")
            key = (source, source_port, target, target_port)
            if key in seen:
                self.error(path, f"Duplicate of an earlier edge {edge['from']} -> {edge['to']}.")
                continue
            seen.add(key)
            if source == target:
                self.error(path, "An edge cannot connect a node to itself.")
                continue
            restricted = [n for n in (source, target) if components.get(n) == RESTRICTED]
            if restricted and source_port is not None and target_port is not None:
                self.error(
                    path,
                    f"'{restricted[0]}' is restricted: its connections are kept as stored "
                    "and cannot be added or changed. Remove this edge.",
                )
                continue
            if restricted:
                # A stored connection with a hidden port: the input it feeds is taken.
                graph[source].add(target)
                if target_port is not None:
                    target_spec = self.spec_for(components[target])
                    port = target_spec.resolve_name(target_port) if target_spec else target_port
                    hidden[(target, port)] = path
                continue
            graph[source].add(target)
            source_spec = self.spec_for(components[source])
            target_spec = self.spec_for(components[target])
            output = input_ = None
            if source_spec is not None:
                output = next(
                    (
                        p
                        for p in source_spec.outputs
                        if p.name == source_spec.resolve_name(source_port)
                    ),
                    None,
                )
                if output is None:
                    hint = _suggest(source_port, [p.name for p in source_spec.outputs])
                    if any(p.name == source_port for p in source_spec.inputs):
                        hint = f" '{source_port}' is an input; 'from' names an output."
                    elif not source_spec.outputs:
                        hint = " It has no outputs."
                    self.error(
                        f"{path}.from",
                        f"'{source}' ({source_spec.title}) has no output '{source_port}'.{hint}",
                    )
            if target_spec is not None:
                input_ = next(
                    (
                        p
                        for p in target_spec.inputs
                        if p.name == target_spec.resolve_name(target_port)
                    ),
                    None,
                )
                if input_ is None:
                    hint = _suggest(target_port, [p.name for p in target_spec.inputs])
                    if any(p.name == target_port for p in target_spec.outputs):
                        hint = f" '{target_port}' is an output; 'to' names an input."
                    elif not target_spec.inputs:
                        hint = " It has no inputs."
                    self.error(
                        f"{path}.to",
                        f"'{target}' ({target_spec.title}) has no input '{target_port}'.{hint}",
                    )
            transform = edge.get("transform") or []
            try:
                parse_transform(transform)
            except ValueError as exc:
                self.error(f"{path}.transform", str(exc))
            if output is None or input_ is None:
                continue
            incoming[(target, input_.name)].append(path)
            if not transform and not port_types_compatible(output.type, input_.type):
                self.error(
                    path,
                    f"Type mismatch: output '{source_port}' of '{source}' produces "
                    f"'{output.type}' but input '{target_port}' of '{target}' expects "
                    f"'{input_.type}'. Connect a matching output or add a transform.",
                )
        for (target, port), paths in incoming.items():
            target_spec = self.spec_for(components[target])
            input_ = next(p for p in target_spec.inputs if p.name == port)
            if _is_list_port(input_):
                continue
            if (target, port) in hidden:
                for extra in paths:
                    self.error(
                        extra,
                        f"Input '{port}' of '{target}' is already fed by a restricted node "
                        f"({hidden[target, port]}), which you cannot change; an input takes "
                        "one edge unless it accepts a list.",
                    )
            elif len(paths) > 1:
                for extra in paths[1:]:
                    self.error(
                        extra,
                        f"Input '{port}' of '{target}' already has a connection "
                        f"({paths[0]}); an input takes one edge unless it accepts a list.",
                    )
        self.check_cycles(graph)
        for name, component_id in components.items():
            spec = self.spec_for(component_id)
            if spec is None:
                continue
            for port in spec.inputs:
                if (
                    port.required
                    and (name, port.name) not in incoming
                    and (name, port.name) not in hidden
                ):
                    self.warn(
                        f"nodes.{name}",
                        f"Required input '{port.name}' is not connected, so '{name}' "
                        "waits for it and shows nothing.",
                    )

    def check_cycles(self, graph: dict[str, set[str]]):
        state: dict[str, int] = {}
        stack: list[str] = []
        reported = set()

        def visit(node: str):
            state[node] = 1
            stack.append(node)
            for nxt in sorted(graph.get(node, ())):
                if state.get(nxt) == 1:
                    cycle = [*stack[stack.index(nxt) :], nxt]
                    key = frozenset(cycle)
                    if key not in reported:
                        reported.add(key)
                        self.error(
                            "edges",
                            f"The edges form a cycle: {' -> '.join(cycle)}. Values must "
                            "flow one way; remove one of these edges.",
                        )
                elif nxt not in state:
                    visit(nxt)
            stack.pop()
            state[node] = 2

        for node in sorted(graph):
            if node not in state:
                visit(node)

    def check_layout(self, doc: dict, controls: t.Collection[str]):
        layout = doc.get("layout") or {}
        nodes = doc.get("nodes") or {}
        bands = [("layout.tiles", layout.get("tiles") or [])]
        bands += [
            (f"layout.responsive.{band}", tiles)
            for band, tiles in (layout.get("responsive") or {}).items()
        ]
        breakpoints = {str(b) for b in layout.get("breakpoints") or []}
        for band in layout.get("responsive") or {}:
            if breakpoints and str(band) not in breakpoints:
                self.warn(
                    f"layout.responsive.{band}",
                    f"is not one of the breakpoints {sorted(breakpoints)} and is never used.",
                )
        for base, tiles in bands:
            first: dict[str, int] = {}
            for i, tile in enumerate(tiles):
                path = f"{base}[{i}]"
                name = tile["node"]
                if name in first:
                    self.error(
                        path,
                        f"'{name}' already has a tile at {base}[{first[name]}]; each node "
                        "has at most one tile.",
                    )
                    continue
                first[name] = i
                component_id = nodes[name]["component"]
                spec = self.spec_for(component_id)
                if spec is not None and spec.component_id in controls:
                    self.warn(
                        path,
                        f"'{name}' is a control, shown in the controls bar, so its tile "
                        "entry is ignored.",
                    )
                width = tile.get("width")
                if width is not None and not 0 < width <= 100:
                    self.error(
                        f"{path}.width",
                        f"is a percentage of the row, between 0 and 100, got {width}.",
                    )
                height = tile.get("height")
                if height is not None and height <= 0:
                    self.error(f"{path}.height", f"must be a positive pixel height, got {height}.")


def validate_dashboard(
    data: str | bytes | dict,
    specs: t.Mapping[str, ComponentSpec],
    *,
    settings_class: type[DashboardSettings] = DashboardSettings,
    aliases: t.Mapping[str, str] | None = None,
    controls: t.Collection[str] = (),
    singletons: t.Collection[str] = (),
) -> list[Issue]:
    """Check a dashboard in the YAML format and return the problems found.

    Checks the document's structure, that every component exists, every config
    field and setting exists and takes its value, every edge joins an existing
    output to an existing input of a compatible type and carries a valid
    transform, that inputs have one edge unless they accept a list, that the
    edges form no cycle and that every node has at most one tile of a valid
    size. Each message says what is wrong and how to fix it.

    Parameters
    ----------
    data
        The YAML text, or the parsed document.
    specs
        The components the dashboard may place, keyed by id. A component
        missing from it is reported as unknown, so pass only the ones the user
        may use.
    settings_class
        The class of the dashboard settings.
    aliases
        Former component ids mapped to current ones.
    controls
        The ids of the components shown in the controls bar rather than as tiles.
    singletons
        The ids of the components that can be placed once per dashboard.

    Returns
    -------
    list of Issue
        Empty when the dashboard is valid. Only issues with severity
        ``"error"`` stop it from loading as written.
    """
    checker = _Checker(specs, settings_class, aliases, singletons)
    try:
        doc = parse_yaml(data) if isinstance(data, (str, bytes)) else data
        if not is_dashboard_yaml(doc):
            raise DashboardYAMLError(
                "document", "must be a mapping that starts with 'flowdash: 1'."
            )
        dashboard_from_dict(doc)
    except DashboardYAMLError as exc:
        return [Issue(exc.path, exc.reason)]
    checker.check_settings(doc.get("settings") or {})
    checker.check_nodes(doc.get("nodes") or {})
    checker.check_edges(doc)
    checker.check_layout(doc, set(controls))
    return checker.issues
