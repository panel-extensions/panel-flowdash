"""Component specification with typed ports for the dataflow editor."""

from __future__ import annotations

import copy
import typing as t
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import param
from panel.viewable import Viewer

from panel_flowdash.registry import PanelAppMetadata, RegistryEntry


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


@dataclass(frozen=True)
class ConfigField:
    """Describes a single design-time configuration option on a component."""

    name: str
    type: str | None = None
    label: str | None = None
    default: Any = None


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
    config: list[ConfigField] = field(default_factory=list)
    config_state_class: type[param.Parameterized] | None = None
    config_editor: Callable | None = None


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
            )
        )

    params = {name: copy.copy(p) for name, p in selected.items()}
    state_cls = type(f"ConfigState_{cls.__name__}", (param.Parameterized,), params)
    return state_cls, fields


def _config_from_mapping(
    schema: dict[str, Any],
) -> tuple[type[param.Parameterized], list[ConfigField]]:
    """Build a config state class from a JSON-Schema-like properties dict."""
    properties = schema.get("properties", schema)
    fields: list[ConfigField] = []
    params: dict[str, param.Parameter] = {}
    for name, prop in properties.items():
        prop = prop if isinstance(prop, dict) else {}
        default = prop.get("default")
        enum = prop.get("enum")
        if enum is not None:
            p = param.Selector(default=default, objects=list(enum))
        else:
            p = param.Parameter(default=default, allow_None=True)
        params[name] = p
        fields.append(
            ConfigField(
                name=name,
                type=prop.get("type"),
                label=prop.get("title", name),
                default=default,
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
    properties = {}
    for name, fieldinfo in cls.model_fields.items():
        default = getattr(fieldinfo, "default", None)
        if default is ... or repr(default) == "PydanticUndefined":
            default = None
        properties[name] = {"default": default, "title": name}
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
                )
            )

    return outputs, inputs


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

    inputs = []
    for pname, p in viewer_cls.param.objects("existing").items():
        if pname in _BASE_PARAMS or pname.startswith("_") or pname in exclude:
            continue
        type_str = type(p).__name__ if p else None
        inputs.append(
            InputPort(
                name=pname,
                type=type_str,
                required=False,
                blocking=False,
                # Carried through so that disconnecting an edge resets the port
                # to a value the target param will actually accept.
                default=p.default,
            )
        )

    return outputs, inputs


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
            outputs = dec_outputs
        if dec_inputs:
            inputs = [p for p in dec_inputs if p.name not in config_names]
    else:
        outputs, inputs = _ports_from_metadata(metadata)
        inputs = [p for p in inputs if p.name not in config_names]

    spec = ComponentSpec(
        component_id=entry.app_id,
        title=entry.title,
        description=metadata.description,
        icon=metadata.icon,
        tags=metadata.tags,
        outputs=outputs,
        inputs=inputs,
        default_size=metadata.default_size,
        config=config,
        config_state_class=config_state_class,
        config_editor=metadata.config_editor,
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
