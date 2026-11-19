"""JSON Schema of the dashboard YAML format, generated from the component specs."""

from __future__ import annotations

import copy
import typing as t
from collections import defaultdict

import param

from panel_flowdash.component_spec import ComponentSpec
from panel_flowdash.parameters import NAME_PATTERN as PARAMETER_NAME, PARAMETER_TYPES
from panel_flowdash.port_types import ANY, normalize_port_type
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.transforms import TRANSFORMS
from panel_flowdash.turns import TURN_ACTORS, TURN_ID_PATTERN, TURN_STATUSES

from .yaml_format import FORMAT_VERSION, NAME_PATTERN

SCHEMA_ID = (
    f"https://panel-extensions.github.io/panel-flowdash/schema/dashboard-{FORMAT_VERSION}.json"
)

# "<node>.<port>", or a bare "<node>" for the hidden end of a restricted node's connection.
_PORT_REF = r"^[A-Za-z_][A-Za-z0-9_-]*(\.[^.]+)?$"
# Keys starting with "x-" are accepted, and ignored, in the format's own mappings.
_EXTENSIONS = {"^x-": {}}


_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def _clean(schema: t.Any) -> t.Any:
    """Drop the parts of a schema param writes that are not JSON Schema, e.g. ``type: parameter``."""
    if isinstance(schema, list):
        return [_clean(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str) and value not in _JSON_TYPES:
            continue
        cleaned[key] = _clean(value)
    for key in ("anyOf", "oneOf"):
        if key in cleaned:
            options = [s for s in cleaned[key] if s]
            if len(options) < len(cleaned[key]):
                # An option that accepts anything makes the whole choice accept anything.
                del cleaned[key]
    return cleaned


def _parameter_schema(p: param.Parameter) -> dict[str, t.Any]:
    """Return the JSON Schema of one Parameter's values, or an empty schema if param has none."""
    try:
        schema = _clean(copy.deepcopy(p.schema()))
    except Exception:
        schema = {}
    # param writes an anyOf of duplicate types for a Selector; the enum says it all.
    if "enum" in schema:
        schema.pop("anyOf", None)
    if p.doc:
        schema["description"] = " ".join(p.doc.split())
    if p.label:
        schema["title"] = p.label
    default = p.default
    if default is None or isinstance(default, (str, int, float, bool, list, dict)):
        schema["default"] = copy.deepcopy(default)
    schema["x-flowdash-type"] = type(p).__name__
    return schema


def _class_properties(cls: type[param.Parameterized]) -> dict[str, dict[str, t.Any]]:
    return {
        name: _parameter_schema(p)
        for name, p in cls.param.objects("existing").items()
        if name != "name"
    }


def config_schema(spec: ComponentSpec) -> dict[str, t.Any]:
    """Return the JSON Schema of a component's ``config`` mapping."""
    properties: dict[str, dict[str, t.Any]] = {}
    state = spec.config_state_class
    for field in spec.config:
        p = state.param.objects("existing").get(field.name) if state is not None else None
        if p is not None:
            properties[field.name] = _parameter_schema(p)
        else:
            entry: dict[str, t.Any] = {"title": field.label or field.name}
            if field.default is not None:
                entry["default"] = copy.deepcopy(field.default)
            properties[field.name] = entry
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _port(port, kind: str) -> dict[str, t.Any]:
    canonical = normalize_port_type(port.type)
    entry: dict[str, t.Any] = {
        "name": port.name,
        "type": port.type if canonical not in (None, ANY) else ANY,
    }
    if port.label:
        entry["label"] = port.label
    if kind == "input":
        entry["required"] = port.required
        if port.multiple:
            entry["multiple"] = True
    return entry


def component_schema(spec: ComponentSpec, *, control: bool = False) -> dict[str, t.Any]:
    """Describe a component for someone writing a dashboard: what it is, its ports and config.

    Parameters
    ----------
    spec
        The component's spec.
    control
        Whether the component is a control, shown in the controls bar instead of a tile.

    Returns
    -------
    dict
        ``component`` (its id), ``title``, ``description``, ``tags``,
        ``control``, ``inputs`` and ``outputs`` (each a list of ``name``,
        ``type`` and, for inputs, ``required``) and ``config``, the JSON Schema
        of its config mapping.
    """
    return {
        "component": spec.component_id,
        "title": spec.title,
        "description": spec.description or "",
        "tags": list(spec.tags or []),
        "control": control,
        "inputs": [_port(p, "input") for p in spec.inputs],
        "outputs": [_port(p, "output") for p in spec.outputs],
        "config": config_schema(spec),
    }


def _tile_schema(names: dict[str, t.Any], *, framed: bool = True) -> dict[str, t.Any]:
    properties = {
        "node": names,
        "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
        "height": {"type": "number", "exclusiveMinimum": 0},
        "visible": {"type": "boolean"},
    }
    if framed:
        properties["framed"] = {"type": ["boolean", "null"]}
        properties["area"] = {"enum": ["grid", "top", "sidebar"]}
    return {
        "type": "object",
        "required": ["node"],
        "properties": properties,
        "patternProperties": _EXTENSIONS,
        "additionalProperties": False,
    }


def _transform_schema() -> dict[str, t.Any]:
    steps = []
    for op, cls in sorted(TRANSFORMS.items()):
        properties = {"op": {"const": op}, **_class_properties(cls)}
        steps.append(
            {
                "title": cls.label or op,
                "description": " ".join((cls.__doc__ or "").split()),
                "type": "object",
                "required": ["op"],
                "properties": properties,
                "additionalProperties": False,
            }
        )
    return {"type": "array", "items": {"oneOf": steps}}


# A parameter's field: config field names and list indexes joined by dots, starting with a name.
FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|[0-9]+))*$"


def dashboard_schema(
    specs: t.Mapping[str, ComponentSpec],
    settings_class: type[DashboardSettings] = DashboardSettings,
    *,
    controls: t.Collection[str] = (),
    aliases: t.Mapping[str, str] | None = None,
) -> dict[str, t.Any]:
    """Return the JSON Schema (draft 2020-12) of dashboards built from *specs*.

    Each node's ``config`` is checked against its component's config fields.
    Port names and types and the dataflow itself are beyond JSON Schema; the
    ports of each component are listed under ``x-flowdash-components`` and
    :func:`~panel_flowdash.agent.validation.validate_dashboard` checks them.

    Parameters
    ----------
    specs
        The components a dashboard may place, keyed by component id.
    settings_class
        The class of the dashboard settings.
    controls
        The ids of the components shown in the controls bar.
    aliases
        Former component ids mapped to current ones, accepted like the current ids.
    """
    former = defaultdict(list)
    for old, new in (aliases or {}).items():
        former[new].append(old)
    node_variants = [
        {
            "type": "object",
            "required": ["component"],
            "properties": {
                "component": {"enum": [component_id, *sorted(former[component_id])]},
                "origin": {"$ref": "#/$defs/turn_id"},
                "config": config_schema(spec),
                "position": {"$ref": "#/$defs/position"},
            },
            "patternProperties": _EXTENSIONS,
            "additionalProperties": False,
        }
        for component_id, spec in sorted(specs.items())
    ]
    node_variants.append(
        {
            "description": "A component the exporting user may not use, kept as stored.",
            "type": "object",
            "required": ["component"],
            "properties": {
                "component": {"const": "flowdash:restricted"},
                "origin": {"$ref": "#/$defs/turn_id"},
                "position": {"$ref": "#/$defs/position"},
            },
            "patternProperties": _EXTENSIONS,
            "additionalProperties": False,
        }
    )
    names = {"type": "string", "pattern": NAME_PATTERN.pattern}
    tile = _tile_schema(names)
    settings = _class_properties(settings_class)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "FlowDash dashboard",
        "type": "object",
        "required": ["flowdash"],
        "patternProperties": _EXTENSIONS,
        "additionalProperties": False,
        "properties": {
            "flowdash": {"const": FORMAT_VERSION},
            "title": {"type": "string"},
            "settings": {"type": "object", "properties": settings},
            "parameters": {
                "type": "object",
                "propertyNames": {"type": "string", "pattern": PARAMETER_NAME.pattern},
                "additionalProperties": {"$ref": "#/$defs/parameter"},
            },
            "turns": {"type": "array", "items": {"$ref": "#/$defs/turn"}},
            "nodes": {
                "type": "object",
                "propertyNames": names,
                "additionalProperties": {"$ref": "#/$defs/node"},
            },
            "edges": {"type": "array", "items": {"$ref": "#/$defs/edge"}},
            "layout": {
                "type": "object",
                "patternProperties": _EXTENSIONS,
                "additionalProperties": False,
                "properties": {
                    "tiles": {"type": "array", "items": tile},
                    "reference_width": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "breakpoints": {"type": "array", "items": {"type": "number"}},
                    "responsive": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": _tile_schema(names, framed=False),
                        },
                    },
                },
            },
        },
        "$defs": {
            "position": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "node": {"oneOf": node_variants},
            "turn_id": {"type": "string", "pattern": TURN_ID_PATTERN.pattern},
            "parameter": {
                "type": "object",
                "required": ["node", "field"],
                "patternProperties": _EXTENSIONS,
                "additionalProperties": False,
                "properties": {
                    "node": names,
                    "field": {"type": "string", "pattern": FIELD_PATTERN},
                    "type": {"enum": list(PARAMETER_TYPES)},
                    "description": {"type": "string"},
                    "expects": {"type": "object"},
                },
            },
            "turn": {
                "type": "object",
                "required": ["id"],
                "patternProperties": _EXTENSIONS,
                "additionalProperties": False,
                "properties": {
                    "id": {"$ref": "#/$defs/turn_id"},
                    "actor": {"enum": list(TURN_ACTORS)},
                    "at": {"type": "string", "format": "date-time"},
                    "run": {"type": "string"},
                    "question": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"enum": list(TURN_STATUSES)},
                },
                "if": {"not": {"required": ["status"]}},
                "then": {"required": ["id", "actor", "at"]},
                "else": {"required": ["id", "question"]},
            },
            "edge": {
                "type": "object",
                "required": ["from", "to"],
                "patternProperties": _EXTENSIONS,
                "additionalProperties": False,
                "properties": {
                    "from": {"type": "string", "pattern": _PORT_REF},
                    "to": {"type": "string", "pattern": _PORT_REF},
                    "transform": _transform_schema(),
                },
            },
        },
        "x-flowdash-components": {
            component_id: {
                key: value
                for key, value in component_schema(spec, control=component_id in controls).items()
                if key != "config"
            }
            for component_id, spec in sorted(specs.items())
        },
    }


__all__ = ["SCHEMA_ID", "component_schema", "dashboard_schema"]
