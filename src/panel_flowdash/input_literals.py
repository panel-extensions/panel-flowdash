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
import re
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
    if (
        isinstance(parameter, param.Range)
        and isinstance(converted, tuple)
        and len(converted) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in converted)
        and converted[0] > converted[1]
    ):
        return f"The start of a range must not be after its end, as in {converted!r}."
    for check in (parameter, port.parameter):
        if check is None:
            continue
        try:
            check._validate(converted)
        except Exception as exc:
            return explain(check, exc)
    canonical = normalize_port_type(port.type)
    if port.parameter is None and canonical == "tuple" and not isinstance(converted, tuple):
        return f"Input '{label}' takes a list of values, not {type(value).__name__}."
    return None


_BOUNDS = (
    (
        re.compile(r"(upper|lower) bound must be in range \[(.+), (.+)\], not (.+?)\.?$"),
        lambda m: f"The {m[1]} end must be between {m[2]} and {m[3]}, not {m[4]}.",
    ),
    (
        re.compile(r"must be in range \[(.+), (.+)\], not (.+?)\.?$"),
        lambda m: f"Must be between {m[1]} and {m[2]}, not {m[3]}.",
    ),
    (
        re.compile(r"must be at (most|least) (.+), not (.+?)\.?$"),
        lambda m: f"Must be at {m[1]} {m[2]}, not {m[3]}.",
    ),
    (
        re.compile(r"must be (less|greater) than (.+), not (.+?)\.?$"),
        lambda m: f"Must be {m[1]} than {m[2]}, not {m[3]}.",
    ),
)
_PARAMETER_NAME = re.compile(r"^(Attribute '\w+' of )?\w+ parameter( '[^']*')? ")
_CLASS = re.compile(r"<class '(?:[\w.]+\.)?(\w+)'>")
# Messages param gives when a value has the wrong type or shape, not when it
# breaks a constraint of the right type.
_WRONG_TYPE = re.compile(
    r"only takes (numeric|a tuple|a string|date)|must be an integer|must be True or False"
    r"|must be a list, not|value must be an instance of|is not of the correct length"
)
_LISTED_OPTIONS = 8


def _options(parameter: param.Selector) -> str:
    names = [str(name) for name in parameter.get_range()]
    shown = ", ".join(names[:_LISTED_OPTIONS])
    more = len(names) - _LISTED_OPTIONS
    return f"{shown} and {more} more" if more > 0 else shown


def _type_words(parameter: param.Parameter) -> str | None:
    if isinstance(parameter, param.DateRange):
        return "Needs two dates."
    if isinstance(parameter, param.Range):
        return "Needs two numbers, like (2010, 2020)."
    if isinstance(parameter, param.Tuple):
        length = parameter.length or 2
        example = ", ".join(str(i + 1) for i in range(length))
        return f"Needs {length} values, like ({example})."
    for cls, words in (
        (param.Boolean, "Needs true or false."),
        (param.Integer, "Needs a whole number."),
        (param.Number, "Needs a number."),
        (param.String, "Needs text."),
        (param.List, "Needs a list, like [1, 2]."),
        (param.Dict, 'Needs a mapping, like {"name": 1}.'),
    ):
        if isinstance(parameter, cls):
            return words
    return None


def explain(parameter: param.Parameter, exc: Exception) -> str:
    """Say in words why *parameter* refuses a value, without param's internal names.

    A value of the wrong type or shape gets what the input needs, e.g. "Needs
    two numbers, like (2010, 2020)." for a range given three numbers; a value
    that breaks a constraint, e.g. a bound or a pattern, gets param's reason
    without the parameter's name.
    """
    message = str(exc)
    for pattern, words in _BOUNDS:
        if match := pattern.search(message):
            return words(match)
    if isinstance(parameter, param.ListSelector):
        return f"Needs some of: {_options(parameter)}."
    if isinstance(parameter, param.Selector):
        return f"Needs one of: {_options(parameter)}."
    if _WRONG_TYPE.search(message) and (words := _type_words(parameter)):
        return words
    cleaned = _CLASS.sub(r"\1", _PARAMETER_NAME.sub("", message))
    return cleaned[:1].upper() + cleaned[1:]


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
    "explain",
    "literal_parameter",
    "literal_schema",
    "port_literal",
    "stored_literal",
    "takes_literal",
]
