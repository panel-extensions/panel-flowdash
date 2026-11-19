"""Component specification with typed ports for the dataflow editor."""

from __future__ import annotations

import copy
import logging
import typing as t
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import param
from panel.viewable import Viewer

from panel_flowdash.registry import PanelAppMetadata, RegistryEntry
from panel_flowdash.util import ReactiveError, is_failure

logger = logging.getLogger("panel_flowdash")


@dataclass(frozen=True)
class OutputPort:
    """Describes a single output port on a component node."""

    name: str
    type: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class InputPort:
    """Describes a single input port on a component node."""

    name: str
    type: str | None = None
    label: str | None = None
    required: bool = True
    blocking: bool = True
    default: Any = None
    multiple: bool | None = None
    handles_failures: bool = False
    # The component's own param, when introspected, so a connection can be
    # checked against the value the component would actually accept.
    parameter: param.Parameter | None = field(default=None, compare=False, repr=False)

    def check_value(self, value: Any) -> str | None:
        """Return why *value* would be rejected by this input, or None."""
        if self.parameter is None or value is None or is_failure(value):
            return None
        try:
            self.parameter._validate(value)
        except Exception as exc:
            return str(exc)
        return None


@dataclass(frozen=True)
class ConfigField:
    """Describes a single design-time configuration option on a component."""

    name: str
    type: str | None = None
    label: str | None = None
    default: Any = None
    # Choices the field declares, e.g. {"choices": "columns", "of": "data"}.
    choices: dict[str, Any] | None = None


@dataclass(frozen=True)
class ComponentSpec:
    """Full specification of a component's ports and metadata."""

    component_id: str
    title: str
    description: str | None
    icon: str | None
    tags: list[str]
    outputs: list[OutputPort]
    inputs: list[InputPort]
    default_size: dict[str, Any] | None
    max_size: dict[str, Any] | None = None
    min_size: dict[str, Any] | None = None
    # Whether the component's tile is framed: None follows the dashboard setting.
    framed: bool | None = None
    config: list[ConfigField] = field(default_factory=list)
    config_state_class: type[param.Parameterized] | None = None
    config_editor: Callable | None = None
    check_config: Callable | None = None
    # What a value bound to a parameter on the component's config must satisfy,
    # see panel_flowdash.parameters.ParameterContract.
    contract: Any = None
    config_choices: Callable | None = None
    # Former port and config names mapped to current ones.
    port_aliases: dict[str, str] = field(default_factory=dict)

    def resolve_name(self, name: str) -> str:
        """Return the current name of a port or config field that may have been renamed.

        A name that is still a port or config field is never redirected, so an
        alias cannot shadow a live name.
        """
        current = {item.name for item in (*self.outputs, *self.inputs, *self.config)}
        if name in current:
            return name
        return self.port_aliases.get(name, name)


_BASE_PARAMS: set[str] = set(param.Parameterized.param)
try:
    from panel.viewable import Viewable

    _BASE_PARAMS |= set(Viewable.param)
except Exception:
    pass


def _param_type_name(p: param.Parameter) -> str:
    return type(p).__name__


def _config_from_param_class(
    cls: type[param.Parameterized],
    names: list[str] | None = None,
) -> tuple[type[param.Parameterized], list[ConfigField]]:
    """Build a config state class and field list from a Parameterized subclass.

    When ``names`` is given, only those params are treated as config; otherwise
    every non-base param on the class is used.
    """
    fields: list[ConfigField] = []
    selected: dict[str, param.Parameter] = {}
    for pname, p in cls.param.objects("existing").items():
        if pname in _BASE_PARAMS or pname.startswith("_"):
            continue
        if names is not None and pname not in names:
            continue
        selected[pname] = p
        fields.append(
            ConfigField(
                name=pname,
                type=_param_type_name(p),
                label=p.label or pname,
                default=p.default,
                choices=_declared_choices(getattr(p, "metadata", None)),
            )
        )

    params = {name: copy.copy(p) for name, p in selected.items()}
    state_cls = type(f"ConfigState_{cls.__name__}", (param.Parameterized,), params)
    return state_cls, fields


def _declared_choices(metadata: Any) -> dict[str, Any] | None:
    """Return the choices a param's or schema property's FlowDash metadata declares."""
    flowdash = metadata.get("flowdash") if isinstance(metadata, dict) else None
    if not isinstance(flowdash, dict) or not flowdash.get("choices"):
        return None
    return {key: flowdash[key] for key in ("choices", "of") if key in flowdash}


_SCHEMA_PARAMS: dict[str, type[param.Parameter]] = {
    "string": param.String,
    "integer": param.Integer,
    "number": param.Number,
    "boolean": param.Boolean,
    "array": param.List,
    "object": param.Dict,
}


def _schema_type(prop: dict[str, Any]) -> tuple[str | None, bool]:
    """Return the JSON type of a schema property and whether it allows null.

    ``{"type": ["integer", "null"]}`` and ``{"anyOf": [{"type": "integer"},
    {"type": "null"}]}`` both give ``("integer", True)``; a union of several
    types gives no type.
    """
    nullable = False
    kinds = prop.get("type")
    if kinds is None and isinstance(prop.get("anyOf"), list):
        kinds = [option.get("type") for option in prop["anyOf"] if isinstance(option, dict)]
    if isinstance(kinds, list):
        nullable = "null" in kinds
        kinds = [k for k in kinds if k != "null"]
        kinds = kinds[0] if len(kinds) == 1 else None
    return (kinds if isinstance(kinds, str) else None), nullable


def _schema_items(prop: dict[str, Any]) -> dict[str, Any]:
    items = prop.get("items")
    if items is None and isinstance(prop.get("anyOf"), list):
        items = next(
            (o.get("items") for o in prop["anyOf"] if isinstance(o, dict) and "items" in o), None
        )
    return items if isinstance(items, dict) else {}


def _param_from_schema(prop: dict[str, Any], default: Any) -> param.Parameter:
    """Build the Parameter a JSON schema property describes, untyped if it cannot be typed."""
    common: dict[str, Any] = {"default": default}
    if prop.get("description"):
        common["doc"] = prop["description"]
    if prop.get("title"):
        common["label"] = prop["title"]
    enum = prop.get("enum")
    if enum is None and "const" in prop:
        enum = [prop["const"]]
    kind, nullable = _schema_type(prop)
    if enum is not None:
        objects = list(enum)
        if default is not None and default not in objects:
            objects.append(default)
        return param.Selector(objects=objects, allow_None=True, **common)
    cls = _SCHEMA_PARAMS.get(kind or "")
    if cls is None:
        return param.Parameter(allow_None=True, **common)
    kwargs = dict(common, allow_None=nullable or default is None)
    if cls in (param.Integer, param.Number):
        bounds = (prop.get("minimum"), prop.get("maximum"))
        if bounds != (None, None):
            kwargs["bounds"] = bounds
    elif cls is param.List and isinstance(default, tuple):
        cls = param.Tuple
        kwargs["length"] = len(default)
    elif cls is param.List:
        item_kind, _ = _schema_type(_schema_items(prop))
        item_type = {"string": str, "integer": int, "boolean": bool}.get(item_kind or "")
        if item_type is not None:
            kwargs["item_type"] = item_type
    try:
        return cls(**kwargs)
    except Exception:
        return param.Parameter(allow_None=True, **common)


def _config_from_mapping(
    schema: dict[str, Any],
) -> tuple[type[param.Parameterized], list[ConfigField]]:
    """Build a config state class from a JSON-Schema-like properties dict.

    Properties are typed from their JSON ``type`` (``string``, ``integer``,
    ``number``, ``boolean``, ``array``, ``object``), with ``enum`` giving a
    selector and ``minimum``/``maximum`` bounds; an untyped property accepts
    any value. ``"x-flowdash": {"choices": "columns", "of": "<input>"}``
    declares choices as a param's ``metadata`` does.
    """
    properties = schema.get("properties", schema)
    fields: list[ConfigField] = []
    params: dict[str, param.Parameter] = {}
    for name, prop in properties.items():
        prop = prop if isinstance(prop, dict) else {}
        default = prop.get("default")
        p = _param_from_schema(prop, default)
        params[name] = p
        kind, _ = _schema_type(prop)
        fields.append(
            ConfigField(
                name=name,
                type=kind,
                label=prop.get("title", name),
                default=default,
                choices=_declared_choices({"flowdash": prop.get("x-flowdash")}),
            )
        )
    state_cls = type("ConfigState_schema", (param.Parameterized,), params)
    return state_cls, fields


def _config_from_metadata(
    metadata: PanelAppMetadata,
    viewer_cls: type | None = None,
) -> tuple[type[param.Parameterized] | None, list[ConfigField]]:
    """Resolve config into a state class and field list from component metadata."""
    schema = metadata.config_schema
    if isinstance(schema, type) and issubclass(schema, param.Parameterized):
        return _config_from_param_class(schema)
    if isinstance(schema, type) and _is_pydantic_model(schema):
        return _config_from_mapping(_pydantic_to_properties(schema))
    if isinstance(schema, dict):
        return _config_from_mapping(schema)
    if metadata.config and viewer_cls is not None:
        return _config_from_param_class(viewer_cls, names=metadata.config)
    return None, []


def _is_pydantic_model(cls: type) -> bool:
    return hasattr(cls, "model_fields") and hasattr(cls, "model_validate")


def _pydantic_to_properties(cls: type) -> dict[str, Any]:
    """Return the JSON schema properties of a Pydantic model, with its Python defaults.

    The types come from the model's JSON schema, the defaults from its fields,
    so a tuple default stays a tuple. A model whose JSON schema cannot be
    generated gives untyped properties.
    """
    try:
        schema = cls.model_json_schema()
    except Exception:
        schema = {}
    described = schema.get("properties", {}) if isinstance(schema, dict) else {}
    properties = {}
    for name, fieldinfo in cls.model_fields.items():
        default = getattr(fieldinfo, "default", None)
        if default is ... or repr(default) == "PydanticUndefined":
            default = None
        prop = dict(described.get(fieldinfo.alias or name) or described.get(name) or {})
        prop.setdefault("title", name)
        extra = (
            fieldinfo.json_schema_extra if isinstance(fieldinfo.json_schema_extra, dict) else {}
        )
        if "x-flowdash" in extra:
            prop["x-flowdash"] = extra["x-flowdash"]
        prop["default"] = default
        properties[name] = prop
    return {"properties": properties}


def _ports_from_metadata(
    metadata: PanelAppMetadata,
) -> tuple[list[OutputPort], list[InputPort]]:
    outputs = []
    for item in metadata.provides:
        if isinstance(item, str):
            outputs.append(OutputPort(name=item))
        elif isinstance(item, dict):
            outputs.append(
                OutputPort(
                    name=item["key"],
                    type=item.get("type"),
                    label=item.get("label"),
                )
            )

    inputs = []
    for item in metadata.requires:
        if isinstance(item, str):
            inputs.append(InputPort(name=item))
        elif isinstance(item, dict):
            inputs.append(
                InputPort(
                    name=item.get("key", ""),
                    type=item.get("type"),
                    label=item.get("label"),
                    required=item.get("required", True),
                    blocking=item.get("blocking", True),
                    default=item.get("fallback"),
                    multiple=item.get("multiple"),
                )
            )

    return outputs, inputs


def _type_from_parameter(viewer_cls: type, port: OutputPort) -> OutputPort:
    """Type an untyped output after the Parameter of the same name on the viewer.

    The base ``param.Parameter`` accepts anything, so it leaves the port untyped
    rather than refusing to connect to every typed input.
    """
    p = viewer_cls.param.objects("existing").get(port.name)
    if port.type is not None or p is None or type(p) is param.Parameter:
        return port
    return replace(port, type=type(p).__name__)


def _flowdash_metadata(p: param.Parameter) -> dict[str, Any]:
    """Return the ``"flowdash"`` namespace of a Parameter's metadata, or an empty dict."""
    namespace = (p.metadata or {}).get("flowdash")
    return namespace if isinstance(namespace, dict) else {}


def _parameter_outputs(viewer_cls: type) -> list[OutputPort]:
    """List the Parameters a viewer class declares as outputs through metadata."""
    outputs = []
    for pname, p in viewer_cls.param.objects("existing").items():
        declared = _flowdash_metadata(p)
        if not declared.get("output"):
            continue
        if pname in _BASE_PARAMS or pname.startswith("_"):
            raise ValueError(
                f"Parameter {pname!r} on {viewer_cls.__name__} is declared as a flowdash "
                "output but cannot be a port: base Viewable parameters and names "
                "starting with an underscore are not ports. Rename the Parameter."
            )
        outputs.append(
            _type_from_parameter(viewer_cls, OutputPort(pname, label=declared.get("label")))
        )
    return outputs


def _parameter_aliases(viewer_cls: type) -> dict[str, str]:
    """Map the former names a viewer's Parameters declare in metadata to their current names."""
    aliases = {}
    for pname, p in viewer_cls.param.objects("existing").items():
        former = _flowdash_metadata(p).get("aliases") or []
        if isinstance(former, str):
            former = [former]
        for old in former:
            aliases[old] = pname
    return aliases


def _ports_from_viewer_class(
    viewer_cls: type,
    exclude: set[str] | None = None,
) -> tuple[list[OutputPort], list[InputPort]]:
    exclude = exclude or set()
    # Introspected off the class rather than an instance: constructing every
    # registered Viewer just to read its ports would run each component's
    # __init__ on every session, so one component doing work there would slow
    # down dashboards that never place it.
    output_info = viewer_cls.param.outputs()
    mro_dicts = [cls.__dict__ for cls in viewer_cls.__mro__]

    outputs = []
    for name, (ptype, _method, _index) in output_info.items():
        if not any(name in d for d in mro_dicts):
            continue
        if ptype is None:
            type_str = None
        elif isinstance(ptype, type):
            type_str = ptype.__name__
        else:
            type_str = type(ptype).__name__
        outputs.append(OutputPort(name=name, type=type_str))

    outputs += _parameter_outputs(viewer_cls)
    output_names = {port.name for port in outputs}

    inputs = []
    for pname, p in viewer_cls.param.objects("existing").items():
        if (
            pname in _BASE_PARAMS
            or pname.startswith("_")
            or pname in exclude
            or pname in output_names
        ):
            continue
        type_str = type(p).__name__ if p else None
        flowdash = _flowdash_metadata(p)
        required = bool(flowdash.get("required", False))
        inputs.append(
            InputPort(
                name=pname,
                type=type_str,
                required=required,
                blocking=bool(flowdash.get("blocking", required)),
                # Carried through so that disconnecting an edge resets the port
                # to a value the target param will actually accept.
                default=p.default,
                parameter=p,
            )
        )

    return outputs, inputs


def _mark_failure_handlers(
    component_id: str, inputs: list[InputPort], names: list[str]
) -> list[InputPort]:
    """Flag the inputs a component declares it can receive a failure on."""
    unknown = set(names) - {port.name for port in inputs}
    if unknown:
        raise ValueError(
            f"'{component_id}' declares handles_failures for unknown inputs: "
            f"{', '.join(sorted(unknown))}."
        )
    marked = []
    for port in inputs:
        if port.name in names:
            # Without ReactiveError failures cannot propagate, so there is nothing to probe.
            if port.parameter is not None and ReactiveError is not None:
                try:
                    port.parameter._validate(ReactiveError(Exception()))
                except Exception as exc:
                    raise ValueError(
                        f"Input '{port.name}' of '{component_id}' declares handles_failures, "
                        f"but its parameter would reject a failure: {exc}"
                    ) from exc
            port = replace(port, handles_failures=True)
        marked.append(port)
    return marked


def _size_hint(component_id: str, name: str, hint: Any) -> dict[str, float] | None:
    """Keep the positive ``width`` and ``height`` pixel values of a tile size hint."""
    if hint is None:
        return None
    if not isinstance(hint, dict):
        logger.warning("Ignoring %s of '%s': expected a dict, got %r.", name, component_id, hint)
        return None
    size = {}
    for key, value in hint.items():
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        if key in ("width", "height") and valid:
            size[key] = value
        else:
            logger.warning(
                "Ignoring %s[%r] of '%s': size hints take positive 'width' and "
                "'height' values in pixels.",
                name,
                key,
                component_id,
            )
    return size or None


def build_component_spec(entry: RegistryEntry) -> ComponentSpec:
    """Build a ComponentSpec from a registry entry, caching it on the entry.

    A spec is derived purely from the component's class/metadata, so it does not
    vary between sessions. Registry entries are shared across sessions, which
    makes the cache process-wide.
    """
    if entry.spec is not None:
        return entry.spec

    app = entry.app
    metadata = entry.metadata

    is_viewer = isinstance(app, type) and issubclass(app, Viewer)
    config_state_class, config = _config_from_metadata(
        metadata, viewer_cls=app if is_viewer else None
    )
    config_names = {f.name for f in config}

    if is_viewer:
        outputs, inputs = _ports_from_viewer_class(app, exclude=config_names)
        dec_outputs, dec_inputs = _ports_from_metadata(metadata)
        if dec_outputs:
            outputs = [_type_from_parameter(app, port) for port in dec_outputs]
            # A node has one state param per port name, so a Parameter that is
            # produced as an output cannot also be consumed as an input.
            output_names = {port.name for port in outputs}
            inputs = [port for port in inputs if port.name not in output_names]
        if dec_inputs:
            inputs = [p for p in dec_inputs if p.name not in config_names]
    else:
        outputs, inputs = _ports_from_metadata(metadata)
        inputs = [p for p in inputs if p.name not in config_names]

    if metadata.handles_failures:
        inputs = _mark_failure_handlers(entry.app_id, inputs, metadata.handles_failures)

    port_aliases = {**(_parameter_aliases(app) if is_viewer else {}), **metadata.port_aliases}

    spec = ComponentSpec(
        component_id=entry.app_id,
        title=entry.title,
        description=metadata.description,
        icon=metadata.icon,
        tags=metadata.tags,
        outputs=outputs,
        inputs=inputs,
        default_size=_size_hint(entry.app_id, "default_size", metadata.default_size),
        max_size=_size_hint(entry.app_id, "max_size", metadata.max_size),
        min_size=_size_hint(entry.app_id, "min_size", metadata.min_size),
        framed=metadata.framed,
        config=config,
        config_state_class=config_state_class,
        config_editor=metadata.config_editor,
        check_config=metadata.check_config,
        contract=metadata.contract,
        config_choices=metadata.config_choices,
        port_aliases=port_aliases,
    )
    entry.spec = spec
    return spec


def build_component_specs(
    registry: dict[str, RegistryEntry],
    component_ids: t.Iterable[str] | None = None,
) -> dict[str, ComponentSpec]:
    """Build specs for component-enabled entries in a registry.

    Parameters
    ----------
    registry
        Registry entries keyed by component id.
    component_ids
        Restrict spec building to these ids. Entries outside the set are skipped
        without being introspected, so an unloaded component costs nothing.
    """
    wanted = None if component_ids is None else set(component_ids)
    specs = {}
    for app_id, entry in registry.items():
        if not entry.metadata.component:
            continue
        if wanted is not None and app_id not in wanted:
            continue
        specs[app_id] = build_component_spec(entry)
    return specs
