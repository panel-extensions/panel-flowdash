"""Dashboard templates: saved dashboards with slots for the data they show.

A template is a dashboard file in the YAML format (see
``docs/reference/dashboard-yaml.md``) with one more top-level mapping,
``x-template``, which the dashboard reader already accepts and ignores. So a
template is also a dashboard file, and filling one in gives a dashboard
document that is imported like a downloaded file, through the same checks.

``x-template`` names the template, describes it and declares its slots::

    x-template:
      name: KPI overview
      description: Headline numbers, a trend, a breakdown and the rows behind them.
      slots:
        data: {kind: data, label: Data, description: A table with a date and a number.}
        trend: {kind: view, label: Trend chart, of: data}
        date: {kind: column, label: Date, of: data, type: date}
        measure: {kind: column, label: Measure, of: data, type: number}
        title: {kind: text, label: Title, default: Overview}

A ``data`` or ``view`` slot is a node of the template whose component is
``flowdash:slot`` (the node of the slot's name, or the one ``node`` names):
binding it to a component the user may place, optionally with a preset's
config, gives the node that component. Ports of a slot node are written
``table`` in the template's edges and take the bound component's port. A
``column`` or ``text`` slot is written ``[[name]]`` in any string of the
title, settings, node config, edge transforms and filters, and bound to a
column name or text (in node ``inputs`` too).

A slot left unbound does not stop the template from being filled in: every
node that needs it (a slot node, a node whose config names it, or a node fed
by such a node) becomes a Markdown tile saying what to choose, in the same
place, and filters on an unbound column are left out.

Templates come with the package (``package_templates``) and from a project's
``templates/`` folder (``load_template_dir``). A file that is not a valid
template is never offered, not even in part.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_KEY = "x-template"
SLOT_COMPONENT = "flowdash:slot"
SLOT_PORT = "table"
# The component of a tile that needs a slot left unbound.
PLACEHOLDER_COMPONENT = "Text/Markdown"
SLOT_KINDS = ("data", "view", "column", "text")
COLUMN_TYPES = ("any", "number", "date", "category")
TEMPLATE_KEYS = ("name", "description", "slots")
SLOT_KEYS = ("kind", "label", "description", "of", "type", "node", "default", "required")
TEMPLATE_SUFFIXES = (".yaml", ".yml")
PACKAGE_DIR = Path(__file__).parent / "dashboard_templates"

MAX_NAME = 80
MAX_DESCRIPTION = 500
MAX_LABEL = 80
MAX_SLOTS = 20
MAX_TEMPLATES = 100
MAX_BOUND_TEXT = 200
# The size of a dashboard file, checked before a template file is read.
MAX_FILE_SIZE = 1_000_000

_SLOT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_REFERENCE = re.compile(r"\[\[\s*([^\[\]\s:]*)(?::(label))?\s*\]\]")
# A slot's value as a heading word: "t_cap" reads "T cap".
_LABEL_SEPARATORS = re.compile(r"[_\-.]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_BOUND = ("[[", "]]", "{{", "}}")


class TemplateError(ValueError):
    """A file that is not a valid template, or bindings that do not fit one.

    The message starts with the offending key, e.g. ``x-template.slots.date.of: ...``.
    """

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class Slot:
    """Something a template needs from the project: data, a view, a column or text."""

    name: str
    kind: str
    label: str
    description: str = ""
    of: str | None = None
    type: str = "any"
    node: str | None = None
    default: str = ""
    required: bool = True


@dataclass(frozen=True)
class DataBinding:
    """A component a ``data`` or ``view`` slot is bound to, with its config and the port wired."""

    component: str
    config: dict[str, t.Any] = field(default_factory=dict)
    port: str | None = None
    label: str = ""


@dataclass(frozen=True)
class Template:
    """A dashboard template: its document, slots and where it came from."""

    id: str
    name: str
    description: str
    slots: dict[str, Slot]
    document: dict[str, t.Any]
    origin: str = "package"

    def components(self) -> set[str]:
        """Return the components the template places itself, and the Markdown of tiles left unbound."""
        return {PLACEHOLDER_COMPONENT} | {
            node.get("component")
            for node in (self.document.get("nodes") or {}).values()
            if isinstance(node, dict) and node.get("component") != SLOT_COMPONENT
        }

    def describe(self) -> dict[str, t.Any]:
        """Describe the template and its slots in plain data, e.g. for the agent."""
        slots = {}
        for slot in self.slots.values():
            entry: dict[str, t.Any] = {"kind": slot.kind, "label": slot.label}
            if slot.description:
                entry["description"] = slot.description
            if slot.of:
                entry["of"] = slot.of
            if slot.kind == "column":
                entry["type"] = slot.type
            if slot.kind == "text" and slot.default:
                entry["default"] = slot.default
            entry["required"] = slot.required
            slots[slot.name] = entry
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "slots": slots,
        }


def _text(value: t.Any, path: str, limit: int, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise TemplateError(
            path, "must be a non-empty string." if required else "must be a string."
        )
    if len(value) > limit:
        raise TemplateError(path, f"is longer than {limit} characters.")
    if _CONTROL.search(value.replace("\n", "")):
        raise TemplateError(path, "holds control characters.")
    return value.strip()


def _parse_slot(name: t.Any, value: t.Any, path: str) -> Slot:
    if not isinstance(name, str) or not _SLOT_NAME.match(name):
        raise TemplateError(path, f"{name!r} is not a slot name: letters, digits and '_'.")
    path = f"{path}.{name}"
    if not isinstance(value, dict):
        raise TemplateError(path, "must be a mapping.")
    unknown = sorted(set(value) - set(SLOT_KEYS))
    if unknown:
        raise TemplateError(
            f"{path}.{unknown[0]}", f"unknown key; slots have {', '.join(SLOT_KEYS)}."
        )
    kind = value.get("kind")
    if kind not in SLOT_KINDS:
        raise TemplateError(f"{path}.kind", f"must be one of {', '.join(SLOT_KINDS)}.")
    column_type = value.get("type", "any")
    if column_type not in COLUMN_TYPES:
        raise TemplateError(f"{path}.type", f"must be one of {', '.join(COLUMN_TYPES)}.")
    of = value.get("of")
    if of is not None and not isinstance(of, str):
        raise TemplateError(f"{path}.of", "must name a data slot.")
    node = value.get("node")
    if node is not None and not isinstance(node, str):
        raise TemplateError(f"{path}.node", "must name a node.")
    required = value.get("required", True)
    if not isinstance(required, bool):
        raise TemplateError(f"{path}.required", "must be true or false.")
    return Slot(
        name=name,
        kind=kind,
        label=_text(value.get("label") or name, f"{path}.label", MAX_LABEL, required=True),
        description=_text(value.get("description"), f"{path}.description", MAX_DESCRIPTION),
        of=of,
        type=column_type,
        node=(node or name) if kind in ("data", "view") else None,
        default=_text(value.get("default"), f"{path}.default", MAX_BOUND_TEXT),
        required=required,
    )


def _strings(value: t.Any) -> t.Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def references(value: t.Any) -> set[str]:
    """Return the slot names written ``[[name]]`` anywhere in *value*."""
    return {match.group(1) for text in _strings(value) for match in _REFERENCE.finditer(text)}


def _slot_nodes(doc: dict, slots: dict[str, Slot]) -> dict[str, str]:
    """Map each node a data or view slot fills to its slot, checking the template's nodes."""
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        raise TemplateError("nodes", "must be a mapping.")
    filled: dict[str, str] = {}
    for slot in slots.values():
        if slot.kind not in ("data", "view"):
            continue
        node = nodes.get(slot.node)
        if not isinstance(node, dict) or node.get("component") != SLOT_COMPONENT:
            raise TemplateError(
                f"x-template.slots.{slot.name}",
                f"needs a node '{slot.node}' with component {SLOT_COMPONENT}.",
            )
        if slot.node in filled:
            raise TemplateError(f"x-template.slots.{slot.name}.node", "is filled by another slot.")
        filled[slot.node] = slot.name
    for name, node in nodes.items():
        if (
            isinstance(node, dict)
            and node.get("component") == SLOT_COMPONENT
            and name not in filled
        ):
            raise TemplateError(f"nodes.{name}.component", "is a slot node no slot fills.")
    return filled


def parse_template(text: str | bytes, *, template_id: str, origin: str = "project") -> Template:
    """Read a template file, refusing it whole if anything about it is wrong.

    The document is read under the limits of a dashboard file (size, depth,
    no tags or aliases), its ``x-template`` is checked, every ``[[name]]``
    must name a declared slot, and the document filled in with nothing bound
    must load as a dashboard.

    Raises
    ------
    TemplateError
        Saying which key is wrong and why.
    """
    from panel_flowdash.agent.yaml_format import DashboardYAMLError, parse_yaml

    try:
        doc = parse_yaml(text)
    except DashboardYAMLError as exc:
        raise TemplateError(exc.path, exc.reason) from exc
    meta = doc.pop(TEMPLATE_KEY, None)
    if not isinstance(meta, dict):
        raise TemplateError(TEMPLATE_KEY, "a template needs an x-template mapping with its name.")
    unknown = sorted(set(meta) - set(TEMPLATE_KEYS))
    if unknown:
        raise TemplateError(
            f"{TEMPLATE_KEY}.{unknown[0]}", f"unknown key; it has {', '.join(TEMPLATE_KEYS)}."
        )
    name = _text(meta.get("name"), f"{TEMPLATE_KEY}.name", MAX_NAME, required=True)
    description = _text(meta.get("description"), f"{TEMPLATE_KEY}.description", MAX_DESCRIPTION)
    raw_slots = meta.get("slots") or {}
    if not isinstance(raw_slots, dict):
        raise TemplateError(f"{TEMPLATE_KEY}.slots", "must be a mapping of slot names.")
    if len(raw_slots) > MAX_SLOTS:
        raise TemplateError(f"{TEMPLATE_KEY}.slots", f"has more than {MAX_SLOTS} slots.")
    slots = {
        key: _parse_slot(key, value, f"{TEMPLATE_KEY}.slots") for key, value in raw_slots.items()
    }
    for slot in slots.values():
        path = f"{TEMPLATE_KEY}.slots.{slot.name}.of"
        if slot.kind in ("column", "view"):
            if slot.of is None or slots.get(slot.of) is None or slots[slot.of].kind != "data":
                raise TemplateError(path, "must name a data slot of the template.")
        elif slot.of is not None:
            raise TemplateError(path, f"only column and view slots have 'of', not {slot.kind}.")
    _slot_nodes(doc, slots)
    named = {s.name for s in slots.values() if s.kind in ("column", "text")}
    unknown_refs = sorted(references(doc) - named)
    if unknown_refs:
        raise TemplateError(
            "document", f"[[{unknown_refs[0]}]] names no column or text slot of the template."
        )
    template = Template(
        id=template_id,
        name=name,
        description=description,
        slots=slots,
        document=doc,
        origin=origin,
    )
    try:
        to_dashboard_dict(fill_template(template, {}))
    except ValueError as exc:
        raise TemplateError(
            getattr(exc, "path", "document"), getattr(exc, "reason", str(exc))
        ) from exc
    return template


def _template_id(path: Path, origin: str) -> str:
    return f"{origin}:{path.stem}"


def load_template_dir(directory: str | Path, origin: str = "project") -> list[Template]:
    """Read the templates in *directory*, skipping and logging each file that is not one.

    Only ``.yaml`` and ``.yml`` files directly in the folder are read, in name
    order, at most ``MAX_TEMPLATES`` of them. A missing folder has none.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []
    templates = []
    files = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in TEMPLATE_SUFFIXES and p.is_file()
    )
    root = folder.resolve()
    for path in files[:MAX_TEMPLATES]:
        try:
            resolved = path.resolve(strict=True)
            if resolved.parent != root:
                raise TemplateError("document", "is a link to a file outside the folder.")
            size = resolved.stat().st_size
            if size > MAX_FILE_SIZE:
                raise TemplateError(
                    "document", f"Too large: {size:,} bytes, at most {MAX_FILE_SIZE:,}."
                )
            text = resolved.read_bytes()
            templates.append(
                parse_template(text, template_id=_template_id(path, origin), origin=origin)
            )
        except (OSError, TemplateError) as exc:
            logger.warning("Skipping the dashboard template %s: %s", path, exc)
    return templates


# The order the package's templates are offered in; others follow by name.
_PACKAGE_ORDER = ("kpi-overview", "trend-and-breakdown", "table-explorer")


def package_templates() -> list[Template]:
    """Return the templates panel-flowdash ships with."""
    templates = load_template_dir(PACKAGE_DIR, origin="package")
    rank = {f"package:{stem}": index for index, stem in enumerate(_PACKAGE_ORDER)}
    return sorted(templates, key=lambda t: rank.get(t.id, len(rank)))


def find_templates(project_dir: str | Path | None = None) -> list[Template]:
    """Return the package's templates, then those in the project's ``templates/`` folder."""
    templates = package_templates()
    if project_dir is not None:
        templates += load_template_dir(Path(project_dir) / "templates", origin="project")
    return templates


def check_bound_text(value: t.Any, path: str) -> str:
    """Refuse bound text that is not a short single-line string or could start a placeholder."""
    if not isinstance(value, str):
        raise TemplateError(path, "must be a string.")
    value = value.strip()
    if not value:
        raise TemplateError(path, "must not be empty.")
    if len(value) > MAX_BOUND_TEXT:
        raise TemplateError(path, f"is longer than {MAX_BOUND_TEXT} characters.")
    if _CONTROL.search(value):
        raise TemplateError(path, "must be a single line without control characters.")
    if any(token in value for token in _UNSAFE_BOUND):
        raise TemplateError(path, "must not hold '[[', ']]', '{{' or '}}'.")
    return value


def label_text(value: str) -> str:
    """Return a column name as a label: separators become spaces and the first letter a capital."""
    words = _LABEL_SEPARATORS.sub(" ", value).strip() or value
    return words[:1].upper() + words[1:]


def check_title(title: t.Any, default: str) -> str:
    """Return a dashboard title from *title*: one line, at most ``MAX_BOUND_TEXT`` characters.

    Control characters become spaces; an empty title gives *default*.
    """
    if title is None:
        return default
    if not isinstance(title, str):
        raise TemplateError("title", "must be a string.")
    title = " ".join(_CONTROL.sub(" ", title).split())
    if len(title) > MAX_BOUND_TEXT:
        raise TemplateError("title", f"is longer than {MAX_BOUND_TEXT} characters.")
    return title or default


def _substitute(value: t.Any, values: dict[str, str], escape: t.Callable[[str], str] | None):
    """Fill ``[[name]]`` and ``[[name:label]]`` in one pass; filled text is never read again."""
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            text = values.get(match.group(1))
            if text is None:
                return match.group(0)
            if match.group(2) and text:
                text = label_text(text)
            return escape(text) if escape is not None else text

        return _REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _substitute(child, values, escape) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute(child, values, escape) for child in value]
    return value


def _placeholder_text(missing: list[Slot]) -> str:
    from panel_flowdash.ui.text import escape_markdown

    labels = [f"**{escape_markdown(slot.label)}**" for slot in missing]
    named = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
    verb = "was" if len(labels) == 1 else "were"
    return (
        f"This tile needs {named}, which {verb} not chosen when the dashboard was made "
        "from a template. Place a component here, or remove the tile."
    )


def _split(ref: t.Any) -> tuple[str, str | None]:
    if not isinstance(ref, str):
        return "", None
    node, _, port = ref.partition(".")
    return node, port or None


def fill_template(template: Template, bindings: t.Mapping[str, t.Any]) -> dict[str, t.Any]:
    """Return the dashboard document *template* gives with *bindings*.

    *bindings* maps slot names to a :class:`DataBinding` for a data or view
    slot, and to a string for a column or text slot. A text slot left out
    takes its default; any other slot left out is unbound, see the module
    docstring. Bindings are checked for their kind but not against the
    project: callers check that a bound component is one the user may place
    and a column one the data has.

    Raises
    ------
    TemplateError
        If a binding names no slot of the template or is of the wrong kind.
    """
    from panel_flowdash.widgets import TEXT_COMPONENTS

    slots = template.slots
    values: dict[str, str] = {}
    components: dict[str, DataBinding] = {}
    for name, value in bindings.items():
        slot = slots.get(name)
        if slot is None:
            raise TemplateError(f"bindings.{name}", "names no slot of the template.")
        if value is None or value == "":
            continue
        if slot.kind in ("data", "view"):
            if not isinstance(value, DataBinding):
                raise TemplateError(
                    f"bindings.{name}", f"a {slot.kind} slot is bound to a component."
                )
            components[name] = value
        else:
            values[name] = check_bound_text(value, f"bindings.{name}")
    for slot in slots.values():
        if slot.kind == "text" and slot.name not in values and slot.default:
            values[slot.name] = slot.default

    doc = copy.deepcopy(template.document)
    nodes: dict[str, dict] = doc.get("nodes") or {}
    filled = _slot_nodes(doc, slots)
    unbound = {name for name in slots if name not in values and name not in components}

    def needs(value: t.Any) -> set[str]:
        return references(value) & unbound

    missing: dict[str, set[str]] = {}
    for name, node in nodes.items():
        slot_name = filled.get(name)
        wanted = needs(node.get("config")) | needs(node.get("inputs"))
        if slot_name is not None and slot_name in unbound:
            wanted.add(slot_name)
        if wanted:
            missing[name] = wanted
    edges = doc.get("edges") or []
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source, _ = _split(edge.get("from"))
            target, _ = _split(edge.get("to"))
            if target not in nodes:
                continue
            wanted = set(missing.get(source, set())) | needs(edge.get("transform"))
            if wanted - missing.get(target, set()):
                missing[target] = missing.get(target, set()) | wanted
                changed = True

    for name, node in nodes.items():
        if name in missing:
            wanted = [slots[s] for s in slots if s in missing[name]]
            nodes[name] = {
                key: value
                for key, value in node.items()
                if key not in ("component", "config", "inputs")
            } | {"component": PLACEHOLDER_COMPONENT, "config": {"text": _placeholder_text(wanted)}}
            continue
        slot_name = filled.get(name)
        if slot_name is not None:
            binding = components[slot_name]
            node["component"] = binding.component
            node["config"] = copy.deepcopy(binding.config)
            node.pop("inputs", None)
            continue
        escape = None
        if node.get("component") in TEXT_COMPONENTS:
            from panel_flowdash.ui.text import escape_markdown

            escape = escape_markdown
        if "config" in node:
            node["config"] = _substitute(node["config"], values, escape)
        if "inputs" in node:
            node["inputs"] = _substitute(node["inputs"], values, None)

    kept_edges = []
    for edge in edges:
        if not isinstance(edge, dict):
            kept_edges.append(edge)
            continue
        source, source_port = _split(edge.get("from"))
        target, target_port = _split(edge.get("to"))
        if source in missing or target in missing:
            continue
        edge = dict(edge)
        if source in filled and source_port == SLOT_PORT:
            port = components[filled[source]].port
            if port:
                edge["from"] = f"{source}.{port}"
        if target in filled and target_port == SLOT_PORT:
            port = components[filled[target]].port
            if port:
                edge["to"] = f"{target}.{port}"
        if "transform" in edge:
            edge["transform"] = _substitute(edge["transform"], values, None)
        kept_edges.append(edge)
    if "edges" in doc:
        doc["edges"] = kept_edges

    filters = doc.get("filters")
    if isinstance(filters, list):
        doc["filters"] = [
            _substitute(entry, values, None) for entry in filters if not needs(entry)
        ]
    # Text an unbound slot leaves in the title or settings is dropped, not shown as [[name]].
    blanks = dict.fromkeys(unbound, "")
    for key in ("title", "settings"):
        if key in doc:
            doc[key] = _substitute(doc[key], {**blanks, **values}, None)
    return doc


def to_dashboard_dict(doc: dict[str, t.Any]) -> dict[str, t.Any]:
    """Convert a filled-in template document to the dict a dashboard export holds."""
    from panel_flowdash.agent.yaml_format import dashboard_from_dict

    return dashboard_from_dict(copy.deepcopy(doc))


# --------------------------------------------------------------------------
# Binding to the components and data of a session
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    """A component, or a preset of one, a data or view slot may be bound to."""

    key: str
    label: str
    binding: DataBinding
    description: str = ""


def _ports(editor: t.Any, component_id: str) -> tuple[list, list]:
    from panel_flowdash.component_spec import _ports_from_metadata

    spec = editor._component_specs.get(component_id)
    if spec is not None:
        return spec.outputs, spec.inputs
    entry = editor._component_entries[component_id]
    return _ports_from_metadata(entry.metadata)


def _role(editor: t.Any, component_id: str) -> str:
    from panel_flowdash.ui.roles import component_role

    entry = editor._component_entries[component_id]
    outputs, inputs = _ports(editor, component_id)
    known = component_id in editor._component_specs or bool(
        entry.metadata.provides or entry.metadata.requires
    )
    return component_role(entry, inputs, outputs, ports_known=known)


# Tags of views that show one value rather than a table.
_NOT_TABLE_VIEWS = ("kpi", "indicator", "metric")


def is_table_type(port_type: str | None) -> bool:
    """Whether a port of *port_type* carries a table: a DataFrame or a type registered as one."""
    from panel_flowdash.port_types import port_types_compatible

    # A port taking any value, e.g. a plain param.Parameter, fits a number as well.
    return (
        port_type is not None
        and port_types_compatible(port_type, "DataFrame")
        and not port_types_compatible(port_type, "Number")
    )


def slot_choices(editor: t.Any, slot: Slot, data: DataBinding | None = None) -> list[Choice]:
    """Return the components, and their presets, *editor*'s user may bind *slot* to.

    Only components the session may place are offered, so a binding can
    never name a component, or a preset's source, the user may not use. A
    data slot offers data sources with an output, by their first output; a
    view slot offers views with an input, by the first input that takes
    *data*'s output type (or the first input, without *data*). Call
    ``editor.ensure_components_loaded`` first, so every component's ports are known.
    """
    from panel_flowdash.port_types import port_types_compatible
    from panel_flowdash.ui.palette import editor_presets
    from panel_flowdash.ui.roles import is_builtin

    if slot.kind not in ("data", "view"):
        return []
    wanted_role = "source" if slot.kind == "data" else "view"
    if data is not None and data.component in editor._component_entries:
        outputs, _ = _ports(editor, data.component)
        data_types = {next((p.type for p in outputs if p.name == data.port), None)}
    elif slot.kind == "view":
        # Before the data is chosen, views that fit any data the user may choose.
        sources = slot_choices(editor, Slot(slot.of or "data", "data", "Data"))
        data_types = {
            next(
                (
                    p.type
                    for p in _ports(editor, c.binding.component)[0]
                    if p.name == c.binding.port
                ),
                None,
            )
            for c in sources
        }
    else:
        data_types = {None}
    choices = []
    for component_id, entry in editor._component_entries.items():
        # Text and the built-in widgets take any value, so they would fit every slot.
        if is_builtin(entry):
            continue
        if (
            not editor._component_offered(component_id)
            or _role(editor, component_id) != wanted_role
        ):
            continue
        outputs, inputs = _ports(editor, component_id)
        if slot.kind == "data":
            tables = [p for p in outputs if is_table_type(p.type)]
            if not tables:
                continue
            port = tables[0].name
        else:
            # KPIs are views of one number; a template places its own.
            if any(str(tag).strip().lower() in _NOT_TABLE_VIEWS for tag in entry.metadata.tags):
                continue
            fitting = [
                p
                for p in inputs
                if is_table_type(p.type)
                and any(port_types_compatible(data_type, p.type) for data_type in data_types)
            ]
            if not fitting:
                continue
            port = fitting[0].name
        description = entry.metadata.description or ""
        choices.append(
            Choice(
                component_id,
                entry.title,
                DataBinding(component_id, {}, port, entry.title),
                description,
            )
        )
        for preset in editor_presets(editor, entry):
            choices.append(
                Choice(
                    f"{component_id}#{preset['key']}",
                    preset["title"],
                    DataBinding(component_id, dict(preset["config"]), port, preset["title"]),
                    preset.get("description") or description,
                )
            )
    return choices


def column_kind(dtype: str) -> str:
    """Whether a pandas dtype name is a ``number``, a ``date`` or a ``category``."""
    name = dtype.lower()
    if "datetime" in name or name.startswith(("date", "period")):
        return "date"
    if name.startswith(("int", "uint", "float", "decimal")) or name in ("int64", "float64"):
        return "number"
    return "category"


_YEAR = re.compile(r"(^|[_\W])(year|yr)s?($|[_\W])", re.IGNORECASE)


def column_fits(dtype: str, column_type: str, name: str = "") -> bool:
    """Whether a column *name* of *dtype* suits a column slot of *column_type*.

    A date slot takes dates and integer columns named like a year; a
    category slot takes text, booleans and categoricals, never numbers such
    as integer ids.
    """
    kind = column_kind(dtype)
    if column_type == "any":
        return True
    if column_type == "number":
        return kind == "number"
    if column_type == "date":
        return kind == "date" or (
            dtype.lower().startswith(("int", "uint")) and bool(_YEAR.search(str(name)))
        )
    return kind == "category"


async def data_columns(
    editor: t.Any, binding: DataBinding, timeout: float = 30
) -> dict[str, str] | None:
    """Return the columns of *binding*'s data by dtype name, run beside *editor*; None if unknown.

    The component runs in a scratch editor with the user's rights, as a
    preview does, so nothing is shown or saved.
    """
    import yaml

    from panel_flowdash.agent.ops import DashboardOps, OperationError

    doc = {
        "flowdash": 1,
        "nodes": {"data": {"component": binding.component, "config": binding.config}},
    }
    try:
        report = await DashboardOps(editor).preview_async(
            yaml.safe_dump(doc, sort_keys=False), rows=1, timeout=timeout
        )
    except (OperationError, PermissionError, ValueError) as exc:
        logger.info("Could not read the columns of %s: %s", binding.component, exc)
        return None
    outputs = (report.get("nodes", {}).get("data") or {}).get("outputs")
    if not isinstance(outputs, dict):
        return None
    sample = outputs.get(binding.port) if binding.port else next(iter(outputs.values()), None)
    if not isinstance(sample, dict) or sample.get("type") != "DataFrame":
        return None
    return dict(sample.get("columns") or {})


def check_bindings(
    template: Template,
    bindings: t.Mapping[str, t.Any],
    choices: t.Mapping[str, t.Sequence[Choice]],
    columns: t.Mapping[str, t.Mapping[str, str] | None],
) -> dict[str, t.Any]:
    r"""Resolve the bindings the agent or a form gives into what :func:`fill_template` takes.

    A data or view slot is bound by the key of one of the slot's *choices*; a
    column slot by a column of its data slot's *columns*, when those are
    known, of a dtype that suits the slot; a text slot by text.

    Raises
    ------
    TemplateError
        Saying which binding is wrong and what it may be.
    """
    resolved: dict[str, t.Any] = {}
    for name, value in bindings.items():
        slot = template.slots.get(name)
        path = f"bindings.{name}"
        if slot is None:
            raise TemplateError(path, f"names no slot; the slots are {', '.join(template.slots)}.")
        if value is None or value == "":
            continue
        if slot.kind in ("data", "view"):
            offered = {choice.key: choice for choice in choices.get(name, ())}
            if value not in offered:
                keys = ", ".join(list(offered)[:30]) or "none"
                raise TemplateError(path, f"{value!r} is not offered; the choices are {keys}.")
            resolved[name] = offered[value].binding
            continue
        text = check_bound_text(value, path)
        if slot.kind == "column":
            known = columns.get(slot.of)
            if known is not None:
                if text not in known:
                    listed = ", ".join(list(known)[:50]) or "none"
                    raise TemplateError(
                        path, f"{text!r} is not a column; the columns are {listed}."
                    )
                if not column_fits(known[text], slot.type, text):
                    raise TemplateError(
                        path,
                        f"{text!r} is {known[text]}, which does not suit a {slot.type} column.",
                    )
        resolved[name] = text
    return resolved


def document_model(doc: dict[str, t.Any], user: str, title: str | None = None):
    """Return a filled-in template document as a new dashboard model of *user*.

    It is read as a downloaded dashboard file is, and still has to pass the
    importing session's checks of the components it places, see
    ``FlowDash._guard_restricted``.
    """
    from panel_flowdash.ui.launcher import parse_dashboard_file

    doc = copy.deepcopy(doc)
    if title:
        doc["title"] = title
    return parse_dashboard_file(json.dumps(to_dashboard_dict(doc)), user)


def template_model(
    template: Template, bindings: t.Mapping[str, t.Any], user: str, title: str | None = None
):
    """Fill *template* in with *bindings* and return it as a new dashboard model of *user*."""
    return document_model(fill_template(template, bindings), user, title)


def offered_templates(editor: t.Any, templates: t.Iterable[Template]) -> list[Template]:
    """Return the *templates* whose own components the session may place, so none fills in half-denied."""
    return [
        template
        for template in templates
        if all(
            component in editor._component_entries and editor._component_offered(component)
            for component in template.components()
        )
    ]
