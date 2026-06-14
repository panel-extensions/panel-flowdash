"""Component specification with typed ports for the dataflow editor."""

from __future__ import annotations

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


_BASE_PARAMS: set[str] = set(param.Parameterized.param)
try:
    from panel.viewable import Viewable
    _BASE_PARAMS |= set(Viewable.param)
except Exception:
    pass


def _ports_from_metadata(
    metadata: PanelAppMetadata,
) -> tuple[list[OutputPort], list[InputPort]]:
    outputs = []
    for item in metadata.provides:
        if isinstance(item, str):
            outputs.append(OutputPort(name=item))
        elif isinstance(item, dict):
            outputs.append(OutputPort(
                name=item["key"],
                type=item.get("type"),
                label=item.get("label"),
            ))

    inputs = []
    for item in metadata.requires:
        if isinstance(item, str):
            inputs.append(InputPort(name=item))
        elif isinstance(item, dict):
            inputs.append(InputPort(
                name=item.get("key", ""),
                type=item.get("type"),
                label=item.get("label"),
                required=item.get("required", True),
                blocking=item.get("blocking", True),
                default=item.get("fallback"),
            ))

    return outputs, inputs


def _ports_from_viewer_class(
    viewer_cls: type,
) -> tuple[list[OutputPort], list[InputPort]]:
    instance = viewer_cls()
    output_info = instance.param.outputs()

    outputs = []
    for name, (ptype, _method, _index) in output_info.items():
        if ptype is None:
            type_str = None
        elif isinstance(ptype, type):
            type_str = ptype.__name__
        else:
            type_str = type(ptype).__name__
        outputs.append(OutputPort(name=name, type=type_str))

    inputs = []
    for pname, p in viewer_cls.param.objects("existing").items():
        if pname in _BASE_PARAMS or pname.startswith("_"):
            continue
        type_str = type(p).__name__ if p else None
        inputs.append(InputPort(
            name=pname,
            type=type_str,
            required=False,
            blocking=False,
        ))

    return outputs, inputs


def build_component_spec(entry: RegistryEntry) -> ComponentSpec:
    """Build a ComponentSpec from a registry entry."""
    app = entry.app
    metadata = entry.metadata

    if isinstance(app, type) and issubclass(app, Viewer):
        outputs, inputs = _ports_from_viewer_class(app)
        dec_outputs, dec_inputs = _ports_from_metadata(metadata)
        if dec_outputs:
            outputs = dec_outputs
        if dec_inputs:
            inputs = dec_inputs
    else:
        outputs, inputs = _ports_from_metadata(metadata)

    return ComponentSpec(
        component_id=entry.app_id,
        title=entry.title,
        description=metadata.description,
        icon=metadata.icon,
        tags=metadata.tags,
        outputs=outputs,
        inputs=inputs,
        default_size=metadata.default_size,
    )


def build_component_specs(
    registry: dict[str, RegistryEntry],
) -> dict[str, ComponentSpec]:
    """Build specs for all component-enabled entries in a registry."""
    specs = {}
    for app_id, entry in registry.items():
        if entry.metadata.component:
            specs[app_id] = build_component_spec(entry)
    return specs
