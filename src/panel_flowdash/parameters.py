"""Parameters: the named config fields a dashboard can be run with, and their contracts.

A parameter points at one field of one node's config, e.g. the file a source
node reads or a widget's default. Its default is whatever that field holds;
binding a value writes it into the field before the dashboard computes, so a
saved dashboard reruns on other data or with other inputs, with no template
syntax inside config values.

A component can declare what a value bound to one of its fields must
satisfy with ``register(contract=...)``, a :class:`ParameterContract`. Its
``expects`` records what the dashboard relies on, e.g. the columns and
types the nodes downstream of a data source read, and is stored on the
parameter. Its ``check`` runs whenever the parameter is rebound and refuses
values that break the record, naming the nodes that would fail, before
anything is applied or computed.

In the dashboard YAML they form a top-level ``parameters`` mapping by name,
whose entries look like this::

    orders:
      node: warehouse
      field: ref
      type: source
      description: Order lines, one row per order
      expects: {...}         # written by the component's contract
    since: {node: since, field: value, type: date}
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import typing as t
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from panel_flowdash.value_text import error_text

logger = logging.getLogger("panel_flowdash")

# The types a parameter can have. The scalar ones say how a value given as
# text, e.g. on a command line, is read; "value" takes any JSON. "source",
# "table", "file" and "uri" are text whose meaning the host gives them, e.g.
# a data file a host resolves into a source.
PARAMETER_TYPES = (
    "string",
    "number",
    "integer",
    "boolean",
    "date",
    "datetime",
    "value",
    "source",
    "table",
    "file",
    "uri",
)
TEXT_TYPES = ("string", "source", "table", "file", "uri")

PARAMETER_KEYS = ("node", "field", "type", "description", "expects")

# The component a dashboard file names for a node its user may not use.
RESTRICTED_COMPONENT = "flowdash:restricted"

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*|[0-9]+)$")

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


class ParameterError(ValueError):
    """A parameter, or a value bound to one, is malformed. ``key`` names the offending part."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@dataclass
class DashboardParameter:
    """A named pointer to one config field of one node.

    Attributes
    ----------
    node
        The node: its instance id in a :class:`~panel_flowdash.DashboardModel`,
        its name in the YAML.
    field
        A dotted path into the node's config, e.g. ``ref`` or
        ``spec.tables.orders``; a number indexes a list.
    type
        One of :data:`PARAMETER_TYPES`.
    description
        What the parameter is, for whoever binds it.
    expects
        What the dashboard relies on of a bound value, as the node's
        component's :class:`ParameterContract` recorded it. Opaque to FlowDash.
    """

    node: str
    field: str
    type: str = "value"
    description: str | None = None
    expects: dict[str, t.Any] | None = None

    def to_dict(self) -> dict[str, t.Any]:
        data: dict[str, t.Any] = {"node": self.node, "field": self.field, "type": self.type}
        if self.description is not None:
            data["description"] = self.description
        if self.expects is not None:
            data["expects"] = copy.deepcopy(self.expects)
        return data

    @classmethod
    def from_dict(cls, data: t.Any) -> DashboardParameter:
        """Read a parameter, raising :class:`ParameterError` if it is malformed."""
        if not isinstance(data, Mapping):
            raise ParameterError("parameter", f"must be a mapping, not {type(data).__name__}.")
        unknown = [k for k in data if k not in PARAMETER_KEYS and not str(k).startswith("x-")]
        if unknown:
            raise ParameterError(
                "parameter",
                f"unknown key {', '.join(map(repr, unknown))}; expected {', '.join(PARAMETER_KEYS)}.",
            )
        node = data.get("node")
        if not isinstance(node, str) or not node:
            raise ParameterError("node", "is required and names a node.")
        path = data.get("field")
        field_path(path)
        kind = data.get("type", "value")
        if kind not in PARAMETER_TYPES:
            raise ParameterError(
                "type", f"must be one of {', '.join(PARAMETER_TYPES)}, got {kind!r}."
            )
        description = data.get("description")
        if description is not None and not isinstance(description, str):
            raise ParameterError("description", f"must be a string, got {description!r}.")
        expects = data.get("expects")
        if expects is not None and not isinstance(expects, Mapping):
            raise ParameterError("expects", f"must be a mapping, got {expects!r}.")
        return cls(
            node=node,
            field=path,
            type=kind,
            description=description,
            expects=copy.deepcopy(dict(expects)) if expects is not None else None,
        )


def check_name(name: t.Any) -> str:
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ParameterError(
            "name",
            f"parameter names start with a letter or underscore and contain only letters, "
            f"digits, '_' and '-', at most 64 characters; got {name!r}.",
        )
    return name


def decode_parameters(value: t.Any) -> dict[str, DashboardParameter]:
    """Return stored parameters, which may be missing (None), by name.

    Raises TypeError unless they are a mapping, and :class:`ParameterError`
    for a malformed parameter. Parameters are optional, so the stores catch
    this and drop just the parameters, with a warning.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"parameters must be a mapping, not {type(value).__name__}")
    return {check_name(name): DashboardParameter.from_dict(data) for name, data in value.items()}


def field_path(path: t.Any) -> list[str | int]:
    """Split a parameter's field into its keys, numbers for list items.

    Raises :class:`ParameterError` unless it is a dotted path of names and
    numbers that starts with a name, e.g. ``spec.layers.0.mark``.
    """
    if not isinstance(path, str) or not path:
        raise ParameterError("field", "is required and names a config field, e.g. 'value'.")
    parts = path.split(".")
    if not all(_SEGMENT.match(part) for part in parts) or parts[0].isdigit():
        raise ParameterError(
            "field",
            f"must be a config field or a dotted path into one, e.g. 'spec.tables.orders', "
            f"got {path!r}.",
        )
    return [int(part) if part.isdigit() else part for part in parts]


_MISSING = object()


def get_field(config: Mapping[str, t.Any], path: str) -> t.Any:
    """Return the value at *path* in *config*, or raise :class:`ParameterError`."""
    value: t.Any = config
    for key in field_path(path):
        value = _child(value, key, path)
        if value is _MISSING:
            raise ParameterError("field", f"the config has no {path!r}.")
    return value


def _child(value: t.Any, key: str | int, path: str) -> t.Any:
    if isinstance(key, int):
        if isinstance(value, list) and -len(value) <= key < len(value):
            return value[key]
        return _MISSING
    if isinstance(value, Mapping):
        return value.get(key, _MISSING)
    return _MISSING


def set_field(config: Mapping[str, t.Any], path: str, value: t.Any) -> dict[str, t.Any]:
    """Return a copy of *config* with *value* at *path*.

    Mappings on the way that do not exist yet are created; a list item must exist.
    """
    keys = field_path(path)
    result = copy.deepcopy(dict(config))
    target: t.Any = result
    for i, key in enumerate(keys[:-1]):
        child = _child(target, key, path)
        if child is _MISSING or child is None:
            if isinstance(key, int) or isinstance(keys[i + 1], int):
                raise ParameterError("field", f"the config has no {path!r}.")
            child = {}
            target[key] = child
        elif not isinstance(child, (dict, list)):
            raise ParameterError(
                "field", f"{'.'.join(map(str, keys[: i + 1]))!r} holds no mapping or list."
            )
        target = child
    last = keys[-1]
    if isinstance(last, int):
        if not isinstance(target, list) or not -len(target) <= last < len(target):
            raise ParameterError("field", f"the config has no {path!r}.")
    elif not isinstance(target, dict):
        raise ParameterError("field", f"the config has no {path!r}.")
    target[last] = copy.deepcopy(value)
    return result


def coerce(kind: str, value: t.Any) -> t.Any:
    """Return *value* as a value of parameter type *kind*, reading text as that type.

    Raises :class:`ParameterError` if it is not one.
    """
    if kind in TEXT_TYPES:
        if not isinstance(value, str):
            raise ParameterError("value", f"must be text, got {value!r}.")
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
            return value.strip().lower() in _TRUE
        raise ParameterError("value", f"must be true or false, got {value!r}.")
    if kind in ("number", "integer"):
        number = value
        if isinstance(value, str):
            try:
                number = int(value.strip()) if kind == "integer" else float(value.strip())
            except ValueError:
                if kind == "integer":
                    try:
                        number = float(value.strip())
                    except ValueError:
                        number = None
                else:
                    number = None
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ParameterError("value", f"must be a number, got {value!r}.")
        if not math.isfinite(number):
            raise ParameterError("value", f"must be a finite number, got {value!r}.")
        if kind == "integer":
            if isinstance(number, float) and not number.is_integer():
                raise ParameterError("value", f"must be a whole number, got {value!r}.")
            return int(number)
        return number
    if kind in ("date", "datetime"):
        if isinstance(value, datetime):
            return value.isoformat() if kind == "datetime" else value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            try:
                if kind == "date":
                    return date.fromisoformat(text).isoformat()
                datetime.fromisoformat(text.replace("Z", "+00:00"))
                return text
            except ValueError:
                pass
        example = "2026-11-17" if kind == "date" else "2026-11-17T20:14:03Z"
        raise ParameterError("value", f"must be a {kind} like {example!r}, got {value!r}.")
    if kind == "value":
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        _check_json(value)
        return copy.deepcopy(value)
    raise ParameterError("type", f"must be one of {', '.join(PARAMETER_TYPES)}, got {kind!r}.")


def _check_json(value: t.Any):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise ParameterError("value", "mapping keys must be strings.")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ParameterError("value", f"must be finite, got {item!r}.")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ParameterError("value", f"must be JSON data, got a {type(item).__name__}.")


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractProblem:
    """Something a bound value lacks that the dashboard relies on.

    ``node`` names the node (as in the YAML) that would fail, or is None
    for a problem with the value as a whole, e.g. a source that cannot be
    read. A ``warning`` does not refuse the value, e.g. a node whose query
    could not be analysed, so the contract does not cover it.
    """

    node: str | None
    message: str
    warning: bool = False

    def __str__(self) -> str:
        """Show the problem after the node it names."""
        prefix = f"{self.node}: " if self.node else ""
        return prefix + self.message


@dataclass
class ContractContext:
    """What a :class:`ParameterContract` gets to compute or check a parameter's contract.

    Attributes
    ----------
    name
        The parameter's name.
    parameter
        The parameter, with ``node`` as the node's name in *document*.
    value
        The value the parameter's field holds, the bound one when checking.
    document
        The whole dashboard in the YAML format, with the value bound, as plain data.
    context
        The editor's context (see ``FlowDash.context``), where hosts put
        what nodes may use, e.g. data sources.
    """

    name: str
    parameter: DashboardParameter
    value: t.Any
    document: dict[str, t.Any]
    context: Mapping[str, t.Any] = field(default_factory=dict)

    def downstream(self) -> list[str]:
        """Return the names of the nodes that read the parameter's node, directly or not, nearest first."""
        edges = [
            (str(e.get("from", "")).partition(".")[0], str(e.get("to", "")).partition(".")[0])
            for e in self.document.get("edges") or []
            if isinstance(e, Mapping)
        ]
        seen = [self.parameter.node]
        for name in seen:
            for source, target in edges:
                if source == name and target not in seen:
                    seen.append(target)
        return seen[1:]


@t.runtime_checkable
class ParameterContract(t.Protocol):
    """What a component requires of a value bound to a parameter on its config.

    Pass one to ``register(contract=...)``. Both methods are synchronous and
    should not fetch data beyond metadata.
    """

    def expects(self, context: ContractContext) -> dict[str, t.Any] | None:
        """Return what the dashboard relies on of the parameter's current value, as JSON data.

        None when it cannot tell, e.g. without access to the data; the
        parameter then keeps what it recorded before.
        """

    def check(
        self, context: ContractContext, expects: dict[str, t.Any]
    ) -> Iterable[ContractProblem | str]:
        """Return the problems of the value in *context* against *expects*, none when it fits."""


class ContractError(ValueError):
    """A bound value breaks what the dashboard relies on; nothing was applied.

    ``problems`` maps each failing parameter's name to its problems.
    """

    def __init__(self, problems: Mapping[str, list[ContractProblem]]):
        self.problems = {name: list(items) for name, items in problems.items()}
        lines = []
        for name, items in self.problems.items():
            for problem in items:
                if not problem.warning:
                    lines.append(f"- {name}: {problem}")
        super().__init__("The values break the dashboard's contract:\n" + "\n".join(lines))

    @property
    def nodes(self) -> list[str]:
        """The nodes the problems name, in order, each once."""
        names: list[str] = []
        for items in self.problems.values():
            for problem in items:
                if problem.node and not problem.warning and problem.node not in names:
                    names.append(problem.node)
        return names


ContractLookup = Callable[[str], t.Any]


def _problems(result: t.Any) -> list[ContractProblem]:
    if result is None:
        return []
    if isinstance(result, (str, ContractProblem)):
        result = [result]
    problems = []
    for item in result:
        if isinstance(item, ContractProblem):
            problems.append(item)
        elif isinstance(item, str) and item:
            problems.append(ContractProblem(None, item))
        else:
            raise TypeError(f"a contract check returned {item!r}")
    return problems


def _contexts(
    doc: Mapping[str, t.Any],
    names: Iterable[str],
    lookup: ContractLookup,
    context: Mapping[str, t.Any] | None,
) -> Iterable[tuple[str, t.Any, ContractContext]]:
    parameters = doc.get("parameters") or {}
    nodes = doc.get("nodes") or {}
    for name in names:
        parameter = DashboardParameter.from_dict(parameters[name])
        node = nodes.get(parameter.node)
        if not isinstance(node, Mapping):
            continue
        hook = lookup(node.get("component"))
        if hook is None:
            continue
        try:
            value = get_field(node.get("config") or {}, parameter.field)
        except ParameterError:
            value = None
        yield (
            name,
            hook,
            ContractContext(name, parameter, value, copy.deepcopy(dict(doc)), context or {}),
        )


def check_contracts(
    doc: Mapping[str, t.Any],
    names: Iterable[str],
    lookup: ContractLookup,
    context: Mapping[str, t.Any] | None = None,
) -> dict[str, list[ContractProblem]]:
    """Check the parameters *names* of the YAML document *doc*, values bound, against their contracts.

    *lookup* returns the :class:`ParameterContract` of a component id, or
    None. A parameter without a recorded ``expects`` or without a contract
    is not checked. A contract that raises fails its parameter with the
    error as the problem. Returns the problems, warnings included, of the
    parameters that have any.
    """
    found: dict[str, list[ContractProblem]] = {}
    for name, hook, ctx in _contexts(doc, names, lookup, context):
        if ctx.parameter.expects is None:
            continue
        try:
            problems = _problems(hook.check(ctx, copy.deepcopy(ctx.parameter.expects)))
        except Exception as exc:
            logger.warning("The contract check of parameter %r failed", name, exc_info=True)
            problems = [ContractProblem(None, f"Could not check the value: {error_text(exc)}")]
        if problems:
            found[name] = problems
    return found


def compute_expects(
    doc: Mapping[str, t.Any],
    names: Iterable[str],
    lookup: ContractLookup,
    context: Mapping[str, t.Any] | None = None,
) -> dict[str, dict[str, t.Any]]:
    """Return what the contracts of the parameters *names* of *doc* record now.

    Parameters whose contract returns None, or raises, are left out, so
    they keep what they recorded before.
    """
    found = {}
    for name, hook, ctx in _contexts(doc, names, lookup, context):
        try:
            expects = hook.expects(ctx)
        except Exception:
            logger.warning("The contract of parameter %r failed", name, exc_info=True)
            continue
        if expects is None:
            continue
        try:
            if not isinstance(expects, Mapping):
                raise ParameterError(
                    "expects", f"must be a mapping, got {type(expects).__name__}."
                )
            _check_json(dict(expects))
        except ParameterError as exc:
            logger.warning("The contract of parameter %r returned unusable data: %s", name, exc)
            continue
        found[name] = copy.deepcopy(dict(expects))
    return found


def failures(problems: Mapping[str, list[ContractProblem]]) -> dict[str, list[ContractProblem]]:
    """Return the problems that refuse a value, leaving out warnings."""
    refused = {name: [p for p in items if not p.warning] for name, items in problems.items()}
    return {name: items for name, items in refused.items() if items}


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------


def suggest_name(name: str, names: Iterable[str]) -> str:
    import difflib

    names = sorted(names)
    close = difflib.get_close_matches(name, names, n=1)
    if close:
        return f" Did you mean {close[0]!r}?"
    if names:
        return f" The dashboard's parameters are: {', '.join(names)}."
    return " The dashboard declares no parameters."


def bind_document(doc: Mapping[str, t.Any], values: Mapping[str, t.Any]) -> dict[str, t.Any]:
    """Return a copy of the YAML document *doc* with *values* written into their parameters' fields.

    Each value is read as its parameter's type first (see :func:`coerce`),
    so values given as text, e.g. on a command line, can be passed as is.
    Nothing is computed and no contract is checked; see :func:`check_contracts`.

    Raises
    ------
    ParameterError
        If a name is not a parameter of *doc*, a value is not of its
        parameter's type, or a parameter's node or field does not exist. The
        key is ``parameters.<name>``.
    """
    result = copy.deepcopy(dict(doc))
    parameters = result.get("parameters") or {}
    nodes = result.get("nodes") or {}
    for name, value in values.items():
        if name not in parameters:
            raise ParameterError(
                f"parameters.{name}", "is not a parameter." + suggest_name(name, parameters)
            )
        key = f"parameters.{name}"
        try:
            parameter = DashboardParameter.from_dict(parameters[name])
            node = nodes.get(parameter.node)
            if not isinstance(node, dict):
                raise ParameterError("node", f"names no node: {parameter.node!r}.")
            if node.get("component") == RESTRICTED_COMPONENT:
                raise ParameterError("node", f"{parameter.node!r} is a node you may not change.")
            node["config"] = set_field(
                node.get("config") or {}, parameter.field, coerce(parameter.type, value)
            )
        except ParameterError as exc:
            raise ParameterError(key, f"{exc.key}: {exc.reason}") from None
    return result


def parameters_schema(
    parameters: Mapping[str, DashboardParameter], values: Mapping[str, t.Any] | None = None
) -> dict[str, t.Any]:
    """Return the JSON Schema of a form that binds *parameters*, e.g. for panel-agents' ``SchemaForm``.

    Each parameter is a property titled by its name, described by its
    description, with the value it holds in *values* as its default.
    """
    properties: dict[str, t.Any] = {}
    for name, parameter in parameters.items():
        schema: dict[str, t.Any] = {"title": name}
        if parameter.type in ("number", "integer", "boolean"):
            schema["type"] = parameter.type
        elif parameter.type == "date":
            schema.update(type="string", format="date")
        elif parameter.type == "datetime":
            schema.update(type="string", format="date-time")
        elif parameter.type in TEXT_TYPES:
            schema["type"] = "string"
        if parameter.description:
            schema["description"] = parameter.description
        if values is not None and name in values and values[name] is not None:
            schema["default"] = copy.deepcopy(values[name])
        properties[name] = schema
    return {"type": "object", "properties": properties}


__all__ = [
    "PARAMETER_TYPES",
    "ContractContext",
    "ContractError",
    "ContractProblem",
    "DashboardParameter",
    "ParameterContract",
    "ParameterError",
    "bind_document",
    "check_contracts",
    "coerce",
    "compute_expects",
    "decode_parameters",
    "get_field",
    "parameters_schema",
    "set_field",
]
