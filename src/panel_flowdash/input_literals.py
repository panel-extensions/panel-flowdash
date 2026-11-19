"""Literal values for a node's unconnected inputs.

An input that nothing is wired to can hold a value set by hand in the
inspector, by the agent or in the dashboard file, under the node's ``inputs``
key. A connection always wins: the literal applies only while the input is
unwired, and comes back when the connection is removed.

Only values a dashboard file can hold are literals: numbers, text, booleans,
lists, tuples and mappings of these, or one of a selector's options. Inputs of
other types, such as DataFrames, can only be wired.
"""

from __future__ import annotations

import inspect
import json
import math
import typing as t

import param

from panel_flowdash.port_types import ANY, normalize_port_type

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import InputPort

_SCALAR_PARAMS: dict[str, type[param.Parameter]] = {
    "number": param.Number,
    "int": param.Integer,
    "str": param.String,
    "bool": param.Boolean,
    "list": param.List,
    "dict": param.Dict,
}

# Param types whose values are plain data, so a literal of them can be stored.
_DATA_PARAMS = (
    param.String,
    param.Number,
    param.Boolean,
    param.List,
    param.Tuple,
    param.Dict,
    param.Color,
)
# Subclasses of those holding values a file cannot.
_OBJECT_PARAMS = (param.Date, param.CalendarDate)


def _copied(parameter: param.Parameter) -> param.Parameter:
    """Return an unbound copy of *parameter* that allows None, for editing a literal."""
    cls = type(parameter)
    names = set(inspect.signature(cls.__init__).parameters) - {"self", "params"}
    kwargs = {name: getattr(parameter, name) for name in names if hasattr(parameter, name)}
    for name in ("doc", "label", "metadata"):
        kwargs[name] = getattr(parameter, name)
    if isinstance(parameter, param.Selector):
        kwargs["objects"] = dict(parameter.names) if parameter.names else list(parameter.objects)
    kwargs["allow_None"] = True
    return cls(**{k: v for k, v in kwargs.items() if v is not param.parameterized.Undefined})


def literal_parameter(port: InputPort) -> param.Parameter | None:
    """Return the Parameter a literal value of *port* is edited and checked with, or None.

    An input introspected from a component's Parameter is edited with a copy
    of that Parameter, so its bounds and options apply. Other inputs follow
    their port type; an untyped input takes any literal, edited as text. An
    input of a type a dashboard file cannot hold, e.g. a DataFrame, takes none.
    """
    parameter = port.parameter
    if parameter is not None:
        if isinstance(parameter, param.Selector):
            return _copied(parameter) if _plain(parameter.objects) else None
        if isinstance(parameter, _OBJECT_PARAMS):
            return None
        if isinstance(parameter, _DATA_PARAMS):
            return _copied(parameter)
        if type(parameter) is param.Parameter:
            return param.Parameter(default=None, allow_None=True)
        return None
    canonical = normalize_port_type(port.type)
    if canonical in (None, ANY, "tuple"):
        return param.Parameter(default=None, allow_None=True)
    cls = _SCALAR_PARAMS.get(canonical)
    if cls is None:
        return None
    return cls(default=None, allow_None=True)


def _plain(objects: t.Any) -> bool:
    values = objects.values() if isinstance(objects, dict) else objects
    try:
        return all(_is_data(value) for value in values)
    except TypeError:
        return False


def _is_data(value: t.Any) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_is_data(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_data(v) for k, v in value.items())
    return False


def takes_literal(port: InputPort) -> bool:
    """Whether *port* can hold a literal value."""
    return literal_parameter(port) is not None


def stored_literal(value: t.Any) -> t.Any:
    """Return *value* as a dashboard file stores it: tuples become lists."""
    if isinstance(value, tuple):
        return [stored_literal(v) for v in value]
    if isinstance(value, list):
        return [stored_literal(v) for v in value]
    if isinstance(value, dict):
        return {k: stored_literal(v) for k, v in value.items()}
    return value


def port_literal(port: InputPort, value: t.Any) -> t.Any:
    """Return a stored literal in the form *port* takes, e.g. a list read from a file as a tuple."""
    parameter = port.parameter
    wants_tuple = isinstance(parameter, param.Tuple) or (
        parameter is None and normalize_port_type(port.type) == "tuple"
    )
    if wants_tuple and isinstance(value, list):
        return tuple(value)
    return value


def _label(port: InputPort) -> str:
    parameter = port.parameter
    return port.label or (parameter.label if parameter is not None else None) or port.name


def check_literal(port: InputPort, value: t.Any) -> str | None:
    """Return why *value* cannot be the literal of *port*, or None if it can."""
    parameter = literal_parameter(port)
    label = _label(port)
    if parameter is None:
        return f"Input '{label}' ({port.type}) cannot hold a value; connect it instead."
    if not _is_data(value):
        return (
            f"Input '{label}' takes numbers, text, booleans, lists or mappings of these, "
            f"not {type(value).__name__}."
        )
    try:
        json.dumps(stored_literal(value))
    except (TypeError, ValueError) as exc:
        return f"Input '{label}' cannot hold {value!r}: {exc}"
    converted = port_literal(port, value)
    try:
        parameter._validate(converted)
    except Exception as exc:
        return str(exc)
    if (reason := port.check_value(converted)) is not None:
        return reason
    canonical = normalize_port_type(port.type)
    if port.parameter is None and canonical == "tuple" and not isinstance(converted, tuple):
        return f"Input '{label}' takes a list of values, not {type(value).__name__}."
    return None


def literal_schema(port: InputPort) -> dict[str, t.Any] | None:
    """Return the JSON Schema of the literal values of *port*, or None if it takes none."""
    parameter = literal_parameter(port)
    if parameter is None:
        return None
    if isinstance(parameter, param.ListSelector):
        return {
            "type": "array",
            "items": {"enum": stored_literal(list(parameter.get_range().values()))},
        }
    if isinstance(parameter, param.Selector):
        return {"enum": stored_literal(list(parameter.get_range().values()))}
    for cls, kind in (
        (param.Boolean, "boolean"),
        (param.Integer, "integer"),
        (param.Number, "number"),
        (param.String, "string"),
        (param.List, "array"),
        (param.Tuple, "array"),
        (param.Dict, "object"),
    ):
        if isinstance(parameter, cls):
            return {"type": kind}
    if normalize_port_type(port.type) == "tuple":
        return {"type": "array"}
    return {}


__all__ = [
    "check_literal",
    "literal_parameter",
    "literal_schema",
    "port_literal",
    "stored_literal",
    "takes_literal",
]
