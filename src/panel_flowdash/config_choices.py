"""The values a node's config fields may take, offered as choices in the inspector.

A component offers choices two ways. A config param can declare that it
names a column of one of the node's inputs::

    field = param.String(metadata={"flowdash": {"choices": "columns", "of": "data"}})

and a ``@register(config_choices=hook)`` hook can compute choices for any
field from the node's config and live inputs, e.g. the tables of a wired
database source. The hook is called as ``hook(config, inputs)``, sync or
async, like ``check_config``, and returns ``{field: choices}`` where choices
are a list of values, a ``{label: value}`` dict or a :class:`Choices`. Its
choices replace the declared ones of the same field. Choices under the name
of an input are offered for the literal value it takes while unconnected.

Columns are read from DataFrames directly, and from other values through the
``columns`` reader of their port type, see
:func:`~panel_flowdash.register_port_type`.

Code fields, see :mod:`panel_flowdash.ui.code_field`, are completed the same
way while typing: a ``@register(config_completions=hook)`` hook returns
``{field: words}``, and without one a code field completes the column names
of the node's tabular inputs.
"""

from __future__ import annotations

import logging
import typing as t
from dataclasses import dataclass, field

from panel_flowdash.config_check import CHECK_TIMEOUT, node_config, node_inputs, run_hook
from panel_flowdash.input_literals import takes_literal
from panel_flowdash.port_types import column_reader

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec
    from panel_flowdash.editor import FlowDash

logger = logging.getLogger("panel_flowdash")


@dataclass(frozen=True)
class Choices:
    """The values one config field may take.

    Parameters
    ----------
    options
        The values, or a ``{label: value}`` dict.
    restrict
        Whether only these values are allowed. With ``False`` the field
        suggests them while accepting other text.
    types
        A short type name per value, e.g. ``{"year": "integer"}`` for columns,
        shown beside each value.
    """

    options: list[t.Any] | dict[str, t.Any] = field(default_factory=list)
    restrict: bool = True
    types: dict[t.Any, str] | None = None

    def labelled(self) -> dict[str, t.Any]:
        """Return the options as ``{label: value}``, each label showing the value's type if known."""
        if isinstance(self.options, dict):
            return dict(self.options)
        types = self.types or {}
        return {
            f"{value} ({types[value]})" if types.get(value) else str(value): value
            for value in self.options
        }

    def values(self) -> list[t.Any]:
        """Return the values, without labels."""
        return list(self.options.values() if isinstance(self.options, dict) else self.options)


def as_choices(value: t.Any) -> Choices | None:
    """Return *value*, a list, ``{label: value}`` dict or :class:`Choices`, as Choices, or None."""
    if isinstance(value, Choices):
        return value
    if isinstance(value, dict):
        return Choices(dict(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return Choices(
            sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        )
    return None


def _dtype_name(dtype: t.Any) -> str:
    kind = getattr(dtype, "kind", None)
    if kind == "b":
        return "boolean"
    if kind in ("i", "u"):
        return "integer"
    if kind in ("f", "c"):
        return "number"
    if kind in ("M", "m"):
        return "datetime"
    return "string" if kind in ("O", "U", "S") or str(dtype) == "string" else str(dtype)


def columns_of(value: t.Any, port_type: str | None = None) -> list[tuple[str, str | None]] | None:
    """Return the ``(name, type)`` columns of *value*, or None if it is not tabular.

    The ``columns`` reader of *port_type* is used when one is registered;
    otherwise a DataFrame's columns are listed with their dtypes as type names
    (``integer``, ``number``, ``boolean``, ``datetime``, ``string``).
    """
    if value is None:
        return None
    reader = column_reader(port_type)
    if reader is not None:
        listed = reader(value)
        if listed is None:
            return None
        if isinstance(listed, dict):
            return [(str(name), kind) for name, kind in listed.items()]
        return [
            (str(item[0]), item[1]) if isinstance(item, (tuple, list)) else (str(item), None)
            for item in listed
        ]
    columns, dtypes = getattr(value, "columns", None), getattr(value, "dtypes", None)
    if columns is None or dtypes is None:
        return None
    return [(str(name), _dtype_name(dtype)) for name, dtype in zip(columns, dtypes, strict=False)]


def column_choices(value: t.Any, port_type: str | None = None) -> Choices | None:
    """Return the columns of *value* as :class:`Choices`, or None if it is not tabular."""
    columns = columns_of(value, port_type)
    if columns is None:
        return None
    return Choices([name for name, _ in columns], types={n: k for n, k in columns if k})


def declared_choices(spec: ComponentSpec, inputs: t.Mapping[str, t.Any]) -> dict[str, Choices]:
    """Return the choices the config fields of *spec* declare, for a node with *inputs*."""
    ports = {port.name: port for port in spec.inputs}
    result: dict[str, Choices] = {}
    for config_field in spec.config:
        declared = config_field.choices
        if not declared or declared.get("choices") != "columns":
            continue
        name = declared.get("of")
        names = [name] if name else [p for p in ports if p in inputs]
        for port_name in names:
            port = ports.get(port_name)
            choices = column_choices(inputs.get(port_name), port.type if port else None)
            if choices is not None:
                result[config_field.name] = choices
                break
    return result


def has_choices(spec: ComponentSpec) -> bool:
    """Whether nodes of *spec* may offer choices for their config."""
    return spec.config_choices is not None or any(f.choices for f in spec.config)


async def compute_choices(
    spec: ComponentSpec,
    config: t.Mapping[str, t.Any],
    inputs: t.Mapping[str, t.Any],
    *,
    editor: FlowDash | None = None,
    timeout: float = CHECK_TIMEOUT,
    hook: bool = True,
    declared: t.Mapping[str, Choices] | None = None,
) -> dict[str, Choices]:
    """Return the choices of each config field of a node of *spec* with *config* and *inputs*.

    With *hook* off only the declared choices are returned, which needs no
    call of the component's ``config_choices``. *declared* passes those
    computed before, so they are not read again. A hook that fails, takes
    longer than *timeout* or returns something else than a mapping is logged
    and offers nothing; the declared choices stay.
    """
    if declared is not None:
        result = dict(declared)
    else:
        try:
            result = declared_choices(spec, inputs)
        except Exception:
            logger.exception("Reading the columns of an input of %s failed", spec.component_id)
            result = {}
    choices_hook = spec.config_choices if hook else None
    if choices_hook is None:
        return result
    try:
        computed = await run_hook(choices_hook, config, inputs, editor=editor, timeout=timeout)
    except Exception:
        logger.exception("The config choices of %s failed", spec.component_id)
        return result
    if computed is None:
        return result
    if not isinstance(computed, t.Mapping):
        logger.error(
            "The config choices of %s returned a %s; it must return a dict of choices per field",
            spec.component_id,
            type(computed).__name__,
        )
        return result
    fields = {f.name for f in spec.config} | {p.name for p in spec.inputs if takes_literal(p)}
    for name, value in computed.items():
        name = spec.resolve_name(name)
        choices = as_choices(value)
        if name in fields and choices is not None:
            result[name] = choices
    return result


async def node_choices(
    editor: FlowDash,
    instance_id: str,
    *,
    timeout: float = CHECK_TIMEOUT,
    hook: bool = True,
    declared: t.Mapping[str, Choices] | None = None,
) -> dict[str, Choices]:
    """Return the choices of each config field of a placed node, from its config and live inputs.

    *hook* and *declared* are passed on to :func:`compute_choices`.
    """
    item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
    if item is None or "placeholder" in item:
        return {}
    spec = editor._component_specs.get(item["component_id"])
    if spec is None or not has_choices(spec):
        return {}
    return await compute_choices(
        spec,
        node_config(editor, instance_id, spec),
        node_inputs(editor, instance_id, spec),
        editor=editor,
        timeout=timeout,
        hook=hook,
        declared=declared,
    )


def code_fields(spec: ComponentSpec) -> list[str]:
    """Return the names of the config fields of *spec* edited as code."""
    from panel_flowdash.ui.code_field import code_language

    cls = spec.config_state_class
    if cls is None:
        return []
    return [
        name
        for name, parameter in cls.param.objects("existing").items()
        if name != "name" and code_language(parameter) is not None
    ]


def input_columns(spec: ComponentSpec, inputs: t.Mapping[str, t.Any]) -> Choices:
    """Return the columns of the tabular inputs of a node of *spec*, each once, as Choices."""
    names: list[str] = []
    types: dict[str, str] = {}
    for port in spec.inputs:
        try:
            columns = columns_of(inputs.get(port.name), port.type)
        except Exception:
            logger.exception(
                "Reading the columns of input %s of %s failed", port.name, spec.component_id
            )
            continue
        for name, kind in columns or []:
            if name not in types and name not in names:
                names.append(name)
                if kind:
                    types[name] = kind
    return Choices(names, restrict=False, types=types)


async def compute_completions(
    spec: ComponentSpec,
    config: t.Mapping[str, t.Any],
    inputs: t.Mapping[str, t.Any],
    *,
    editor: FlowDash | None = None,
    timeout: float = CHECK_TIMEOUT,
) -> dict[str, Choices]:
    """Return the words each code field of a node of *spec* completes while typing.

    The component's ``config_completions`` hook is asked when it has one; its
    words replace the default, the columns of the node's tabular inputs, for
    the fields it names. A hook that fails, takes longer than *timeout* or
    returns something else than a mapping is logged and the default stays.
    """
    fields = code_fields(spec)
    if not fields:
        return {}
    columns = input_columns(spec, inputs)
    result = {name: columns for name in fields} if columns.options else {}
    hook = spec.config_completions
    if hook is None:
        return result
    try:
        computed = await run_hook(hook, config, inputs, editor=editor, timeout=timeout)
    except Exception:
        logger.exception("The config completions of %s failed", spec.component_id)
        return result
    if computed is None:
        return result
    if not isinstance(computed, t.Mapping):
        logger.error(
            "The config completions of %s returned a %s; it must return a dict of words per field",
            spec.component_id,
            type(computed).__name__,
        )
        return result
    for name, value in computed.items():
        name = spec.resolve_name(name)
        words = as_choices(value)
        if name in fields and words is not None:
            result[name] = words
    return result


async def node_completions(
    editor: FlowDash, instance_id: str, *, timeout: float = CHECK_TIMEOUT
) -> dict[str, Choices]:
    """Return the words each code field of a placed node completes, from its config and live inputs."""
    item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
    if item is None or "placeholder" in item:
        return {}
    spec = editor._component_specs.get(item["component_id"])
    if spec is None:
        return {}
    return await compute_completions(
        spec,
        node_config(editor, instance_id, spec),
        node_inputs(editor, instance_id, spec),
        editor=editor,
        timeout=timeout,
    )
