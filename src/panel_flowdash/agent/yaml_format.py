"""The dashboard YAML format: a readable spelling of the dashboard export.

See ``docs/reference/dashboard-yaml.md`` for the specification. The functions
here convert between YAML text and the dict :func:`~panel_flowdash.editor.export_model`
writes and :meth:`~panel_flowdash.editor.FlowDash.import_dashboard` reads, so
loading YAML is importing a dashboard file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import typing as t

import yaml

from panel_flowdash.auto_layout import NodeBox, arrange_graph
from panel_flowdash.dashboard_store import MODEL_VERSION, DashboardModel
from panel_flowdash.parameters import DashboardParameter, ParameterError, check_name
from panel_flowdash.turns import DashboardTurn, TurnError, strip_questions
from panel_flowdash.ui.node_card import CARD_WIDTH

# The version of the format this release writes.
FORMAT_VERSION = 4

# The versions of the format this release reads.
READ_VERSIONS = (1, 2, 3, 4)

TOP_LEVEL_KEYS = (
    "flowdash",
    "title",
    "settings",
    "parameters",
    "turns",
    "nodes",
    "edges",
    "layout",
)
NODE_KEYS = ("component", "origin", "config", "position")
TURN_KEYS = ("id", "actor", "at", "run", "question", "summary", "status")
# Versions 1 to 3 had no parameters, and version 1 neither turns nor node origins.
_V3_TOP_LEVEL_KEYS = tuple(k for k in TOP_LEVEL_KEYS if k != "parameters")
_V1_TOP_LEVEL_KEYS = tuple(k for k in _V3_TOP_LEVEL_KEYS if k != "turns")
_V1_NODE_KEYS = tuple(k for k in NODE_KEYS if k != "origin")
EDGE_KEYS = ("from", "to", "transform")
LAYOUT_KEYS = ("tiles", "reference_width", "breakpoints", "responsive")
TILE_KEYS = ("node", "width", "height", "visible", "framed", "area")
# A tile's frame and area apply at every screen size, so a responsive layout
# cannot set them, and version 2 had neither.
RESPONSIVE_TILE_KEYS = tuple(k for k in TILE_KEYS if k not in ("framed", "area"))
_V2_TILE_KEYS = RESPONSIVE_TILE_KEYS
# Where a node is shown: as a tile on the grid, in the top bar or in the sidebar.
TILE_AREAS = ("grid", "top", "sidebar")
# Keys starting with this are accepted, and ignored, in every mapping of the format
# except config and settings, for tools that annotate dashboard files.
EXTENSION_PREFIX = "x-"

# Limits on the text a dashboard is read from.
MAX_SIZE = 1_000_000
MAX_DEPTH = 32

# The component a file names for a node the exporting user may not use.
RESTRICTED_COMPONENT = "flowdash:restricted"

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# Instance ids the editor generates for placed components: "<type key>_<6 hex>",
# where the type key is the component id with "/" replaced by "__".
_GENERATED_ID = re.compile(r"^.*__(?P<tail>[^_].*_[0-9a-f]{6})$")

# The card height nodes a file gives no position are laid out with.
_CARD_HEIGHT = 160


class DashboardYAMLError(ValueError):
    """Raised when text is not a dashboard in the YAML format.

    The message starts with the offending key, e.g. ``edges[2].from: ...``,
    so it can be fixed without reading the specification.

    Attributes
    ----------
    path
        The offending key, ``document`` for the document as a whole.
    reason
        What is wrong with it.
    """

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _slug(text: str) -> str:
    """Return *text* as a valid node name that contains no ``__``."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-")
    slug = re.sub(r"_{2,}", "_", slug)
    if not slug or not re.match(r"[A-Za-z]", slug):
        slug = f"n_{slug}".rstrip("_")
    return slug


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _own_name(instance_id: str) -> str:
    """Return the name an instance id gives its node, before names are made unique."""
    generated = _GENERATED_ID.match(instance_id)
    if generated:
        tail = generated["tail"]
        return tail if _readable(tail) else _slug(tail)
    if _readable(instance_id):
        return instance_id
    return f"{_slug(instance_id)}_{_hash(instance_id)}"


def _readable(name: str) -> bool:
    """Whether *name* can be a node name a file gives, i.e. one the reader accepts as new."""
    return bool(NAME_PATTERN.match(name)) and "__" not in name


def node_names(items: t.Iterable[dict]) -> dict[str, str]:
    """Return a readable, unique name for each exported item, keyed by instance id.

    The name comes from the instance id alone, so it is the same for every
    user, whatever the component is called now, and never changes. An id
    that is already readable is its own name; an id the editor generated,
    like ``Views__kpi_3fa2c1``, is named after its last part (``kpi_3fa2c1``);
    any other id gets a short hash of itself. When two ids would get the same
    name, the one that sorts later gets a hash of its id appended.
    """
    ids = sorted({item["instance_id"] for item in items})
    wanted = {iid: _own_name(iid) for iid in ids}
    # Ids that are their own name keep it, whatever sorts first.
    names = {iid: name for iid, name in wanted.items() if name == iid}
    taken = set(names.values())
    for iid in ids:
        if iid in names:
            continue
        name = wanted[iid]
        while name in taken:
            name = f"{name}_{_hash(iid + name)}"
        names[iid] = name
        taken.add(name)
    if len(set(names.values())) != len(names):
        raise AssertionError("Node names are not unique.")
    return names


def _export_dict(source: t.Any) -> dict:
    if isinstance(source, DashboardModel):
        from panel_flowdash.editor import export_model

        return export_model(source)
    if hasattr(source, "export_dashboard"):
        return source.export_dashboard()
    return source


def editor_restricted_edges(editor) -> list[dict]:
    """Return the edges of an editor's canvas that touch items its user may not use."""
    private = {iid for iid, p in editor._placeholders().items() if p.private}
    return [
        u.edge.to_dict()
        for u in editor._unresolved_edges
        if {u.edge.source, u.edge.target} & private
    ]


def editor_restricted(editor) -> tuple[set[str], list[dict]]:
    """Return the restricted nodes of an editor's canvas by name, and their connections as YAML edges.

    Pass them as ``restricted_nodes`` and ``restricted_edges`` to
    :func:`~panel_flowdash.agent.validate_dashboard`, so YAML meant for this
    canvas is checked against what the canvas holds rather than what the text says.
    """
    data = editor.export_dashboard()
    doc = dashboard_to_dict(data, editor_restricted_edges(editor))
    nodes = doc.get("nodes") or {}
    restricted = {
        name for name, node in nodes.items() if node["component"] == RESTRICTED_COMPONENT
    }
    edges = [
        {"from": e["from"], "to": e["to"]}
        for e in doc.get("edges") or []
        if "." not in e["from"] or "." not in e["to"]
    ]
    return restricted, edges


def _tile(entry: dict, names: dict[str, str], keys: tuple[str, ...]) -> dict | None:
    instance_id = entry.get("instance_id")
    if instance_id not in names:
        return None
    tile = {"node": names[instance_id]}
    for key, value in entry.items():
        if key in keys:
            tile[key] = copy.deepcopy(value)
    return tile


def _tiles(layout: list, names: dict[str, str], keys: tuple[str, ...] = TILE_KEYS) -> list[dict]:
    entries = [e for e in layout or [] if isinstance(e, dict)]
    order = sorted(
        range(len(entries)),
        key=lambda i: (entries[i].get("index") is None, entries[i].get("index") or 0, i),
    )
    return [tile for i in order if (tile := _tile(entries[i], names, keys)) is not None]


def _position(item: dict) -> list:
    return [item.get("x", 0), item.get("y", 0)]


def dashboard_to_dict(
    source: t.Any, restricted_edges: t.Iterable[dict] = (), *, questions: bool = True
) -> dict:
    """Return the YAML document for a dashboard export, as plain data.

    Parameters
    ----------
    source
        A :class:`~panel_flowdash.editor.FlowDash` editor, whose canvas is
        exported with its user's rights, a dashboard export, as written by
        :meth:`~panel_flowdash.editor.FlowDash.export_dashboard`, or a
        :class:`~panel_flowdash.dashboard_store.DashboardModel`, which is
        exported as is.
    restricted_edges
        The stored edges (export dicts) touching restricted items, which the
        export leaves out. They are written with only the restricted node's
        name on that end, so the file shows which inputs are taken without
        revealing the hidden ports.
    questions
        False leaves out the questions the turns record.
    """
    data = _export_dict(source)
    if hasattr(source, "export_dashboard") and not restricted_edges:
        restricted_edges = editor_restricted_edges(source)
    items = list(data.get("items") or [])
    names = node_names(items)
    doc: dict[str, t.Any] = {"flowdash": FORMAT_VERSION}
    if isinstance(data.get("title"), str):
        doc["title"] = data["title"]
    if data.get("settings"):
        doc["settings"] = copy.deepcopy(data["settings"])
    parameters = {}
    for name, parameter in (data.get("parameters") or {}).items():
        entry = copy.deepcopy(parameter)
        if entry.get("node") not in names:
            continue
        entry["node"] = names[entry["node"]]
        parameters[name] = entry
    if parameters:
        doc["parameters"] = parameters
    turns = [
        {key: turn[key] for key in TURN_KEYS if turn.get(key) is not None}
        for turn in data.get("turns") or []
    ]
    if not questions:
        turns = strip_questions(turns)
    if turns:
        doc["turns"] = turns
    turn_ids = {turn["id"] for turn in turns}
    nodes = {}
    assert len(set(names.values())) == len(items), "two items share an instance id"
    for item in items:
        node: dict[str, t.Any] = {"component": item["component_id"]}
        if item.get("origin") in turn_ids:
            node["origin"] = item["origin"]
        if item.get("config"):
            node["config"] = copy.deepcopy(item["config"])
        node["position"] = _position(item)
        nodes[names[item["instance_id"]]] = node
    if nodes:
        doc["nodes"] = nodes
    edges = []
    for edge in data.get("edges") or []:
        if edge["source"] not in names or edge["target"] not in names:
            continue
        entry: dict[str, t.Any] = {
            "from": f"{names[edge['source']]}.{edge['source_port']}",
            "to": f"{names[edge['target']]}.{edge['target_port']}",
        }
        if edge.get("transform"):
            entry["transform"] = copy.deepcopy(edge["transform"])
        edges.append(entry)
    restricted = {
        item["instance_id"] for item in items if item["component_id"] == RESTRICTED_COMPONENT
    }
    for edge in restricted_edges:
        ends = []
        for node, port in (
            (edge["source"], edge["source_port"]),
            (edge["target"], edge["target_port"]),
        ):
            if node not in names:
                break
            ends.append(names[node] if node in restricted else f"{names[node]}.{port}")
        else:
            edges.append({"from": ends[0], "to": ends[1]})
    if edges:
        doc["edges"] = edges
    layout: dict[str, t.Any] = {}
    if tiles := _tiles(data.get("tile_layout"), names):
        layout["tiles"] = tiles
    if data.get("reference_width") is not None:
        layout["reference_width"] = data["reference_width"]
    if data.get("breakpoints"):
        layout["breakpoints"] = list(data["breakpoints"])
    responsive = {
        str(band): _tiles(entries, names, RESPONSIVE_TILE_KEYS)
        for band, entries in (data.get("responsive_layouts") or {}).items()
    }
    if responsive:
        layout["responsive"] = responsive
    if layout:
        doc["layout"] = layout
    return doc


class _Dumper(yaml.SafeDumper):
    """Writes block style with indented lists, but short lists of scalars such as positions inline.

    It never writes anchors and aliases, which the reader refuses.
    """

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

    def ignore_aliases(self, data):
        return True


def _represent_list(dumper, value):
    flow = len(value) <= 4 and all(isinstance(v, (int, float, bool)) or v is None for v in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flow)


# Characters YAML reads as line breaks; only a double-quoted string keeps them.
_BREAKS = ("\x85", "\u2028", "\u2029")


def _represent_str(dumper, value):
    if any(c in value for c in _BREAKS):
        style = '"'
    else:
        style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(list, _represent_list)
_Dumper.add_representer(str, _represent_str)


def dashboard_to_yaml(
    source: t.Any, restricted_edges: t.Iterable[dict] = (), *, questions: bool = True
) -> str:
    """Write a dashboard export as YAML text.

    Parameters
    ----------
    source
        A :class:`~panel_flowdash.editor.FlowDash` editor, a dashboard export
        or a :class:`~panel_flowdash.dashboard_store.DashboardModel`, see
        :func:`dashboard_to_dict`. Pass the editor when the dashboard may hold
        components the user may not use, so they are written as restricted
        stand-ins with their connections marked.
    restricted_edges
        The stored edges touching restricted items, see :func:`dashboard_to_dict`.
    questions
        False leaves out the questions the turns record.
    """
    return yaml.dump(
        dashboard_to_dict(source, restricted_edges, questions=questions),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


class _Loader(yaml.SafeLoader):
    """A loader for JSON-compatible YAML.

    It refuses anchors and aliases, nesting deeper than ``MAX_DEPTH``,
    duplicate and non-string keys and every tag beyond strings, numbers,
    booleans, null, lists and mappings, and reads dates as strings.
    """

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _LoadError(
                f"line {event.start_mark.line + 1}: anchors and aliases (& and *) are not supported."
            )
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise _LoadError(
                f"line {event.start_mark.line + 1}: nested deeper than {MAX_DEPTH} levels."
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise _LoadError(
                    f"line {key_node.start_mark.line + 1}: keys must be strings, got {key!r}."
                )
            if key in seen:
                raise _LoadError(f"line {key_node.start_mark.line + 1}: {key!r} is defined twice.")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class _LoadError(yaml.YAMLError):
    pass


def _refuse_tag(loader, node):
    raise _LoadError(
        f"line {node.start_mark.line + 1}: {node.tag!r} values are not supported; "
        "use strings, numbers, true/false, null, lists and mappings."
    )


# Plain scalars are typed by the YAML 1.2 core schema, with integers in plain decimal
# (no leading zeros), 0o octal or 0x hex. Anything else, such as yes/no, 12:30, 017
# or 2024-01-01, is a string.
_CORE_SCALARS = {
    "tag:yaml.org,2002:null": (r"^(?:~|null|Null|NULL|)$", list("~nN")),
    "tag:yaml.org,2002:bool": (r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    "tag:yaml.org,2002:int": (
        r"^(?:[-+]?(?:0|[1-9][0-9]*)|0o[0-7]+|0x[0-9a-fA-F]+)$",
        list("-+0123456789"),
    ),
    "tag:yaml.org,2002:float": (
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)(?:[eE][-+]?[0-9]+)?|[-+]?[0-9]+[eE][-+]?[0-9]+"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
}
_Loader.yaml_implicit_resolvers = {}
for _tag, (_pattern, _first) in _CORE_SCALARS.items():
    _Loader.add_implicit_resolver(_tag, re.compile(_pattern), _first)
# The writer quotes every string either YAML 1.1 or the core schema would read as
# another type, so files stay unambiguous to other YAML readers too.
for _tag, (_pattern, _first) in _CORE_SCALARS.items():
    _Dumper.add_implicit_resolver(_tag, re.compile(_pattern), _first)
# Other readers also take signed octal and hex as integers.
_Dumper.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^[-+](?:0o[0-7]+|0x[0-9a-fA-F]+)$"), list("-+")
)
# A plain "" (empty) scalar has no first character to index the resolver by.
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:null", re.compile(_CORE_SCALARS["tag:yaml.org,2002:null"][0]), [None]
)
_Loader.yaml_constructors = {
    tag: (
        _refuse_tag
        if tag is None
        or tag.rsplit(":", 1)[-1] not in ("null", "bool", "int", "float", "str", "seq", "map")
        else constructor
    )
    for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
}
_Loader.yaml_constructors[None] = _refuse_tag


def _construct_int(loader, node):
    value = loader.construct_scalar(node)
    sign, digits = (-1, value[1:]) if value[:1] == "-" else (1, value.lstrip("+"))
    base = {"0o": 8, "0x": 16}.get(digits[:2], 10)
    return sign * int(digits[2:] if base != 10 else digits, base)


def _construct_float(loader, node):
    value = loader.construct_scalar(node).lower()
    if value.endswith(".nan"):
        return math.nan
    if value.endswith(".inf"):
        return -math.inf if value.startswith("-") else math.inf
    return float(value)


def _construct_bool(loader, node):
    value = loader.construct_scalar(node).lower()
    if value not in ("true", "false"):
        raise _LoadError(f"line {node.start_mark.line + 1}: {value!r} is not true or false.")
    return value == "true"


_Loader.yaml_constructors["tag:yaml.org,2002:int"] = _construct_int
_Loader.yaml_constructors["tag:yaml.org,2002:float"] = _construct_float
_Loader.yaml_constructors["tag:yaml.org,2002:bool"] = _construct_bool


def load_yaml(text: str | bytes) -> t.Any:
    """Parse YAML text into JSON-compatible data, within the format's limits.

    Raises
    ------
    DashboardYAMLError
        If the text is larger than ``MAX_SIZE``, is not valid YAML, or uses
        anything JSON cannot hold: anchors and aliases, tags such as sets or
        binary, non-string or duplicate keys, or nesting deeper than
        ``MAX_DEPTH``. Dates are read as strings.
    """
    _check_size(text)
    try:
        data = yaml.load(text, Loader=_Loader)
    except _LoadError as exc:
        raise DashboardYAMLError("document", str(exc)) from exc
    except yaml.YAMLError as exc:
        raise DashboardYAMLError("document", f"Not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise DashboardYAMLError("document", "Nested too deeply.") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise DashboardYAMLError("document", f"Not valid YAML: {exc}") from exc
    _check_document(data)
    return data


def parse_yaml(text: str | bytes) -> dict:
    """Parse YAML text into a dashboard document, checking only that it is one, see :func:`load_yaml`."""
    doc = load_yaml(text)
    if not isinstance(doc, dict) or "flowdash" not in doc:
        raise DashboardYAMLError(
            "document",
            f"Not a FlowDash dashboard: the file must start with 'flowdash: {FORMAT_VERSION}'.",
        )
    return doc


def read_dashboard_text(text: str | bytes) -> t.Any:
    """Read the text of a dashboard file: a JSON export, or a dashboard in the YAML format.

    A JSON document with a ``flowdash`` key is a YAML-format dashboard written
    as JSON and is read under the same limits and rules as :func:`load_yaml`.

    Raises
    ------
    ValueError
        With "Not valid JSON" when the text is neither JSON nor a YAML-format
        dashboard, or the :class:`DashboardYAMLError` of a YAML-format dashboard.
    """
    try:
        data = json.loads(text)
    except RecursionError as exc:
        raise ValueError("Not a dashboard file: nested too deeply.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        try:
            return parse_yaml(text)
        except DashboardYAMLError as yaml_exc:
            plain = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
            looks_like_json = plain.lstrip().startswith(("{", "["))
            if yaml_exc.reason.startswith("Not a FlowDash dashboard") or (
                looks_like_json and yaml_exc.reason.startswith("Not valid YAML")
            ):
                raise ValueError(f"Not valid JSON: {exc}") from exc
            raise
    if not is_dashboard_yaml(data):
        return data
    # A YAML-format dashboard spelled as JSON keeps json's decoding (which joins
    # surrogate-pair escapes) and gets the reader's other rules.
    _check_size(text)

    def refuse_duplicates(pairs):
        keys = [key for key, _ in pairs]
        for key in keys:
            if keys.count(key) > 1:
                raise DashboardYAMLError("document", f"{key!r} is defined twice.")
        return dict(pairs)

    data = json.loads(text, object_pairs_hook=refuse_duplicates)
    _check_document(data)
    return data


def _check_size(text: str | bytes):
    if len(text) > MAX_SIZE:
        unit = "bytes" if isinstance(text, bytes) else "characters"
        raise DashboardYAMLError(
            "document", f"Too large: {len(text):,} {unit}, at most {MAX_SIZE:,}."
        )


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= c <= "\udfff" for c in text)


def _check_document(data: t.Any):
    """Refuse nesting deeper than ``MAX_DEPTH`` and strings that are not valid Unicode."""
    stack = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise DashboardYAMLError("document", f"nested deeper than {MAX_DEPTH} levels.")
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and _has_surrogate(key):
                    raise DashboardYAMLError("document", f"key {key!r} is not valid Unicode.")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and _has_surrogate(item):
            raise DashboardYAMLError(
                "document", f"{item[:40]!r} is not valid Unicode: it holds a lone surrogate."
            )


def is_dashboard_yaml(data: t.Any) -> bool:
    """Whether *data*, a parsed document, is in the YAML format rather than the JSON export."""
    return isinstance(data, dict) and "flowdash" in data


def _fail(path: str, message: str) -> t.NoReturn:
    raise DashboardYAMLError(path, message)


def _mapping(value, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(path, f"must be a mapping, not {type(value).__name__}.")
    return value


def _sequence(value, path: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(path, f"must be a list, not {type(value).__name__}.")
    return value


def _unknown_keys(value: dict, allowed: t.Sequence[str], path: str):
    unknown = [k for k in value if k not in allowed and not str(k).startswith(EXTENSION_PREFIX)]
    if unknown:
        _fail(
            path,
            f"unknown key {', '.join(repr(k) for k in unknown)}; expected {', '.join(allowed)}.",
        )


def _json_value(value: t.Any, path: str):
    """Fail unless *value* is JSON: strings, finite numbers, booleans, null, lists and mappings."""
    stack = [(value, path)]
    while stack:
        item, where = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail(where, f"keys must be strings, got {key!r}.")
                stack.append((child, f"{where}.{key}"))
        elif isinstance(item, list):
            stack.extend((child, f"{where}[{i}]") for i, child in enumerate(item))
        elif isinstance(item, float) and not math.isfinite(item):
            _fail(where, f"must be a finite number, got {item!r}.")
        elif isinstance(item, str) and _has_surrogate(item):
            _fail(where, "is not valid Unicode: it holds a lone surrogate.")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            _fail(where, f"must be JSON data, got a {type(item).__name__}.")


def split_port(ref: t.Any, path: str) -> tuple[str, str]:
    """Split a ``<node>.<port>`` reference, raising DashboardYAMLError if it is not one."""
    node, port = split_ref(ref, path)
    if port is None:
        _fail(path, f"must be '<node>.<port>', got {ref!r}.")
    return node, port


def split_ref(ref: t.Any, path: str) -> tuple[str, str | None]:
    """Split an edge end into node and port; the port is None for a bare node name.

    A bare name is how a file marks the hidden port of a restricted node.
    """
    if not isinstance(ref, str) or not ref:
        _fail(path, f"must be '<node>.<port>', got {ref!r}.")
    node, _, port = ref.partition(".")
    if not node or (not port and "." in ref):
        _fail(path, f"must be '<node>.<port>', got {ref!r}.")
    return node, port or None


def _number(value, path: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(path, f"must be a finite number, got {value!r}.")
    return value


def _auto_positions(names: list[str], edges: list[tuple[str, str]]) -> dict[str, list]:
    """Lay nodes out by their dataflow, sources on the left, see :mod:`panel_flowdash.auto_layout`.

    Their components are not known here, so every card gets the same size;
    the editor lays out the nodes a file gives no position again once it
    has loaded them.
    """
    boxes = [NodeBox(name, CARD_WIDTH, _CARD_HEIGHT) for name in names]
    return {name: [x, y] for name, (x, y) in arrange_graph(boxes, edges).items()}


def unplaced_nodes(doc: dict, data: dict) -> set[str]:
    """Return the instance ids of the nodes *doc* gives no position.

    *data* is what :func:`dashboard_from_dict` made of *doc*, whose items
    follow the order of the document's nodes.
    """
    nodes = doc.get("nodes") or {}
    return {
        item["instance_id"]
        for node, item in zip(nodes.values(), data.get("items") or [], strict=False)
        if isinstance(node, dict) and node.get("position") is None
    }


def _component(nodes: dict, name: str) -> t.Any:
    node = nodes.get(name)
    return node.get("component") if isinstance(node, dict) else None


def _layout_entries(
    tiles: list, path: str, ids: dict[str, str], keys: tuple[str, ...] = TILE_KEYS
) -> list[dict]:
    entries = []
    for i, tile in enumerate(_sequence(tiles, path)):
        tile_path = f"{path}[{i}]"
        tile = _mapping(tile, tile_path)
        name = tile.get("node")
        if not isinstance(name, str) or name not in ids:
            _fail(f"{tile_path}.node", f"names no node: {name!r}.")
        _unknown_keys(tile, keys, tile_path)
        entry = {k: copy.deepcopy(v) for k, v in tile.items() if k in keys and k != "node"}
        for key in ("width", "height"):
            if key in entry and entry[key] is not None:
                _number(entry[key], f"{tile_path}.{key}")
        if entry.get("framed", False) is None:
            del entry["framed"]
        for key in ("visible", "framed"):
            if key in entry and not isinstance(entry[key], bool):
                _fail(f"{tile_path}.{key}", f"must be true or false, got {entry[key]!r}.")
        if "area" in entry and entry["area"] not in TILE_AREAS:
            _fail(
                f"{tile_path}.area",
                f"must be one of {', '.join(TILE_AREAS)}, got {entry['area']!r}.",
            )
        entries.append({"instance_id": ids[name], "index": i, **entry})
    return entries


def dashboard_from_dict(doc: dict, *, base: DashboardModel | dict | None = None) -> dict:
    """Convert a parsed YAML document into a dashboard export dict.

    Raises :class:`DashboardYAMLError` if the document is not structurally a
    dashboard; whether its components, ports and config exist is checked by
    :func:`~panel_flowdash.agent.validation.validate_dashboard` and, on load,
    by the editor.

    Parameters
    ----------
    doc
        The parsed document.
    base
        The dashboard (model or export) the document was exported from. Node
        names that :func:`node_names` gives its items map back to their
        instance ids, so loading an edited export keeps every node's identity.
    """
    doc = _mapping(doc, "document")
    version = doc.get("flowdash")
    if type(version) is not int or version not in READ_VERSIONS:
        newer = type(version) is int and version > FORMAT_VERSION
        _fail(
            "flowdash",
            f"unsupported format version {version!r}; this release reads "
            f"{', '.join(map(str, READ_VERSIONS[:-1]))} and {READ_VERSIONS[-1]}."
            + (" The file was written by a newer FlowDash." if newer else ""),
        )
    allowed = (
        TOP_LEVEL_KEYS
        if version >= 4
        else _V3_TOP_LEVEL_KEYS
        if version >= 2
        else _V1_TOP_LEVEL_KEYS
    )
    _unknown_keys(doc, allowed, "document")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        _fail("title", f"must be a string, got {title!r}.")
    settings = _mapping(doc.get("settings"), "settings")
    _json_value(settings, "settings")
    turns = _turns(doc.get("turns"))
    turn_ids = {turn["id"] for turn in turns}

    base_items = []
    if base is not None:
        base_items = (
            [item.to_dict() for item in base.items]
            if isinstance(base, DashboardModel)
            else list(base.get("items") or [])
        )
    known = {name: iid for iid, name in node_names(base_items).items()}

    nodes = _mapping(doc.get("nodes"), "nodes")
    ids: dict[str, str] = {}
    used: set[str] = set()
    for name in nodes:
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            _fail(
                f"nodes.{name}",
                "node names start with a letter or underscore and contain only "
                "letters, digits, '_' and '-'.",
            )
        if name not in known and "__" in name:
            _fail(
                f"nodes.{name}",
                "new node names may not contain '__', which marks ids the editor generates.",
            )
        instance_id = known.get(name, name)
        while instance_id in used:
            instance_id = f"{instance_id}_"
        ids[name] = instance_id
        used.add(instance_id)

    edges = []
    wiring = []
    for i, edge in enumerate(_sequence(doc.get("edges"), "edges")):
        path = f"edges[{i}]"
        edge = _mapping(edge, path)
        _unknown_keys(edge, EDGE_KEYS, path)
        for key in ("from", "to"):
            if key not in edge:
                _fail(f"{path}.{key}", "is required.")
        source, source_port = split_ref(edge["from"], f"{path}.from")
        target, target_port = split_ref(edge["to"], f"{path}.to")
        for name, port, key in ((source, source_port, "from"), (target, target_port, "to")):
            if name not in ids:
                _fail(f"{path}.{key}", f"names no node: {name!r}.")
            if port is None and _component(nodes, name) != RESTRICTED_COMPONENT:
                _fail(f"{path}.{key}", f"must be '<node>.<port>', got {name!r}.")
        transform = _sequence(edge.get("transform"), f"{path}.transform")
        for j, step in enumerate(transform):
            if not isinstance(step, dict) or "op" not in step:
                _fail(f"{path}.transform[{j}]", "must be a mapping with an 'op'.")
        wiring.append((source, target))
        if (source_port is None or target_port is None) and transform:
            _fail(
                f"{path}.transform",
                "a restricted node's connection keeps its stored transform; remove 'transform'.",
            )
        if source_port is None or target_port is None:
            # A stored connection of a restricted node: kept as stored on import.
            continue
        entry = {
            "source": ids[source],
            "source_port": source_port,
            "target": ids[target],
            "target_port": target_port,
        }
        if transform:
            entry["transform"] = copy.deepcopy(transform)
        edges.append(entry)

    unplaced = [
        n for n, node in nodes.items() if _mapping(node, f"nodes.{n}").get("position") is None
    ]
    auto = _auto_positions(list(nodes), wiring) if unplaced else {}
    items = []
    for name, node in nodes.items():
        path = f"nodes.{name}"
        node = _mapping(node, path)
        _unknown_keys(node, NODE_KEYS if version >= 2 else _V1_NODE_KEYS, path)
        component = node.get("component")
        if not isinstance(component, str) or not component:
            _fail(f"{path}.component", "is required and names a component, e.g. 'Views/kpi'.")
        config = _mapping(node.get("config"), f"{path}.config")
        _json_value(config, f"{path}.config")
        position = node.get("position")
        if position is None:
            position = auto[name]
        elif not isinstance(position, list) or len(position) != 2:
            _fail(f"{path}.position", f"must be [x, y], got {position!r}.")
        x = _number(position[0], f"{path}.position")
        y = _number(position[1], f"{path}.position")
        item = {
            "instance_id": ids[name],
            "component_id": component,
            "x": x,
            "y": y,
            "config": copy.deepcopy(config),
        }
        origin = node.get("origin")
        if origin is not None:
            if origin not in turn_ids:
                _fail(f"{path}.origin", f"names no turn: {origin!r}.")
            item["origin"] = origin
        items.append(item)

    parameters = _parameters(doc.get("parameters"), ids)

    layout = _mapping(doc.get("layout"), "layout")
    _unknown_keys(layout, LAYOUT_KEYS, "layout")
    reference_width = layout.get("reference_width")
    if reference_width is not None and _number(reference_width, "layout.reference_width") <= 0:
        _fail(
            "layout.reference_width",
            f"must be a positive number of pixels, got {reference_width}.",
        )
    breakpoints = _sequence(layout.get("breakpoints"), "layout.breakpoints")
    for i, value in enumerate(breakpoints):
        _number(value, f"layout.breakpoints[{i}]")
    responsive = {
        str(band): _layout_entries(entries, f"layout.responsive.{band}", ids, RESPONSIVE_TILE_KEYS)
        for band, entries in _mapping(layout.get("responsive"), "layout.responsive").items()
    }
    return {
        "version": MODEL_VERSION,
        "title": "Untitled" if title is None else title,
        "items": items,
        "edges": edges,
        "tile_layout": _layout_entries(
            layout.get("tiles"), "layout.tiles", ids, TILE_KEYS if version >= 3 else _V2_TILE_KEYS
        ),
        "breakpoints": list(breakpoints),
        "responsive_layouts": responsive,
        "reference_width": reference_width,
        "settings": copy.deepcopy(settings),
        "turns": turns,
        "parameters": parameters,
    }


def _parameters(value: t.Any, ids: dict[str, str]) -> dict[str, dict]:
    """Check the parameters of a document and return them as export dicts, nodes by instance id."""
    parameters = {}
    for name, parameter in _mapping(value, "parameters").items():
        path = f"parameters.{name}"
        try:
            check_name(name)
        except ParameterError as exc:
            _fail(path, exc.reason)
        _json_value(parameter, path)
        try:
            parsed = DashboardParameter.from_dict(parameter)
        except ParameterError as exc:
            _fail(f"{path}.{exc.key}" if exc.key != "parameter" else path, exc.reason)
        if parsed.node not in ids:
            _fail(f"{path}.node", f"names no node: {parsed.node!r}.")
        parsed.node = ids[parsed.node]
        parameters[name] = parsed.to_dict()
    return parameters


def _turns(value: t.Any) -> list[dict]:
    """Check the turns of a document and return them as export dicts."""
    turns = []
    seen: set[str] = set()
    for i, turn in enumerate(_sequence(value, "turns")):
        path = f"turns[{i}]"
        turn = _mapping(turn, path)
        _unknown_keys(turn, TURN_KEYS, path)
        try:
            parsed = DashboardTurn.from_dict(turn)
        except TurnError as exc:
            _fail(f"{path}.{exc.key}" if exc.key != "turn" else path, exc.reason)
        if parsed.id in seen:
            _fail(f"{path}.id", f"{parsed.id!r} is the id of an earlier turn.")
        seen.add(parsed.id)
        turns.append(parsed.to_dict())
    return turns


def dashboard_from_yaml(text: str | bytes, *, base: DashboardModel | dict | None = None) -> dict:
    """Read YAML text into the dashboard export dict that importing a file takes.

    Pass the result to :meth:`~panel_flowdash.editor.FlowDash.import_dashboard`,
    which also accepts the YAML text directly.

    Parameters
    ----------
    text
        The YAML document.
    base
        The dashboard the text was exported from, so node names map back to its
        instance ids, see :func:`dashboard_from_dict`.

    Raises
    ------
    DashboardYAMLError
        If *text* is not a dashboard in the YAML format. The message names the
        offending key.
    """
    return dashboard_from_dict(parse_yaml(text), base=base)


__all__ = [
    "FORMAT_VERSION",
    "READ_VERSIONS",
    "DashboardYAMLError",
    "dashboard_from_yaml",
    "dashboard_to_yaml",
]
