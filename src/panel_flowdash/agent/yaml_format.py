"""The dashboard YAML format: a readable spelling of the dashboard export.

See ``docs/reference/dashboard-yaml.md`` for the specification. The functions
here convert between YAML text and the dict :func:`~panel_flowdash.editor.export_model`
writes and :meth:`~panel_flowdash.editor.FlowDash.import_dashboard` reads, so
loading YAML is importing a dashboard file.
"""

from __future__ import annotations

import copy
import re
import typing as t
from collections import defaultdict

import yaml

from panel_flowdash.dashboard_store import DashboardModel

FORMAT_VERSION = 1

# The dashboard model version the export dicts produced here carry.
_MODEL_VERSION = 3

TOP_LEVEL_KEYS = ("flowdash", "title", "settings", "nodes", "edges", "layout")
NODE_KEYS = ("component", "config", "position")
EDGE_KEYS = ("from", "to", "transform")
LAYOUT_KEYS = ("tiles", "reference_width", "breakpoints", "responsive")

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# Instance ids the editor generates for placed components: "<type key>_<6 hex>".
_GENERATED_ID = re.compile(r"^.+_[0-9a-f]{6}$")

# Canvas spacing of the nodes that are laid out because the file gives no position.
_COLUMN_SPACING = 360
_ROW_SPACING = 220


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
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-").lower()
    if not slug or not re.match(r"[A-Za-z_]", slug):
        slug = f"n_{slug}" if slug else "node"
    return slug


def node_names(items: t.Iterable[dict]) -> dict[str, str]:
    """Return a readable, unique name for each exported item, keyed by instance id.

    An instance id that is already readable is its own name. Ids the editor
    generated, like ``Views__kpi_3fa2c1``, are named after their component,
    numbered from the second one on (``kpi``, ``kpi_2``).
    """
    items = list(items)
    names: dict[str, str] = {}
    taken: set[str] = set()
    pending = []
    for item in items:
        instance_id = item["instance_id"]
        if NAME_PATTERN.match(instance_id) and not _GENERATED_ID.match(instance_id):
            names[instance_id] = instance_id
            taken.add(instance_id)
        else:
            pending.append(item)
    for item in pending:
        base = _slug(str(item.get("component_id", "node")).rsplit("/", 1)[-1].split(":")[-1])
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}_{n}"
        names[item["instance_id"]] = name
        taken.add(name)
    return names


def _export_dict(source: dict | DashboardModel) -> dict:
    if isinstance(source, DashboardModel):
        from panel_flowdash.editor import export_model

        return export_model(source)
    return source


def _tile(entry: dict, names: dict[str, str]) -> dict | None:
    instance_id = entry.get("instance_id")
    if instance_id not in names:
        return None
    tile = {"node": names[instance_id]}
    for key, value in entry.items():
        if key not in ("instance_id", "index"):
            tile[key] = copy.deepcopy(value)
    return tile


def _tiles(layout: list, names: dict[str, str]) -> list[dict]:
    entries = [e for e in layout or [] if isinstance(e, dict)]
    order = sorted(
        range(len(entries)),
        key=lambda i: (entries[i].get("index") is None, entries[i].get("index") or 0, i),
    )
    return [tile for i in order if (tile := _tile(entries[i], names)) is not None]


def _position(item: dict) -> list:
    return [item.get("x", 0), item.get("y", 0)]


def dashboard_to_dict(source: dict | DashboardModel) -> dict:
    """Return the YAML document for a dashboard export, as plain data.

    Parameters
    ----------
    source
        A dashboard export, as written by
        :meth:`~panel_flowdash.editor.FlowDash.export_dashboard`, or a
        :class:`~panel_flowdash.dashboard_store.DashboardModel`, which is
        exported first.
    """
    data = _export_dict(source)
    items = list(data.get("items") or [])
    names = node_names(items)
    doc: dict[str, t.Any] = {"flowdash": FORMAT_VERSION}
    if data.get("title"):
        doc["title"] = data["title"]
    if data.get("settings"):
        doc["settings"] = copy.deepcopy(data["settings"])
    nodes = {}
    for item in items:
        node: dict[str, t.Any] = {"component": item["component_id"]}
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
        str(band): _tiles(entries, names)
        for band, entries in (data.get("responsive_layouts") or {}).items()
    }
    if responsive:
        layout["responsive"] = responsive
    if layout:
        doc["layout"] = layout
    return doc


class _Dumper(yaml.SafeDumper):
    """Writes block style with indented lists, but short lists of scalars such as positions inline."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _represent_list(dumper, value):
    flow = len(value) <= 4 and all(isinstance(v, (int, float, bool)) or v is None for v in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flow)


def _represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(list, _represent_list)
_Dumper.add_representer(str, _represent_str)


def dashboard_to_yaml(source: dict | DashboardModel) -> str:
    """Write a dashboard export as YAML text.

    Parameters
    ----------
    source
        A dashboard export, as written by
        :meth:`~panel_flowdash.editor.FlowDash.export_dashboard`, or a
        :class:`~panel_flowdash.dashboard_store.DashboardModel`. Export through
        the editor when the dashboard may hold components the user may not use,
        so they are written as restricted stand-ins.
    """
    return yaml.dump(
        dashboard_to_dict(source),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def parse_yaml(text: str | bytes) -> dict:
    """Parse YAML text into a dashboard document, checking only that it is one."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DashboardYAMLError("document", f"Not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or "flowdash" not in doc:
        raise DashboardYAMLError(
            "document", "Not a FlowDash dashboard: the file must start with 'flowdash: 1'."
        )
    return doc


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
    unknown = [k for k in value if k not in allowed]
    if unknown:
        _fail(
            path,
            f"unknown key {', '.join(repr(k) for k in unknown)}; expected {', '.join(allowed)}.",
        )


def split_port(ref: t.Any, path: str) -> tuple[str, str]:
    """Split a ``<node>.<port>`` reference, raising DashboardYAMLError if it is not one."""
    if not isinstance(ref, str) or "." not in ref:
        _fail(path, f"must be '<node>.<port>', got {ref!r}.")
    node, port = ref.split(".", 1)
    if not node or not port:
        _fail(path, f"must be '<node>.<port>', got {ref!r}.")
    return node, port


def _number(value, path: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, f"must be a number, got {value!r}.")
    return value


def _auto_positions(names: list[str], edges: list[tuple[str, str]]) -> dict[str, list]:
    """Lay nodes out in columns by their depth in the dataflow, sources on the left."""
    upstream = defaultdict(set)
    for source, target in edges:
        if source != target:
            upstream[target].add(source)
    depth: dict[str, int] = {}

    def depth_of(name: str, seen: frozenset = frozenset()) -> int:
        if name in depth:
            return depth[name]
        if name in seen:
            return 0
        parents = upstream.get(name, ())
        value = 1 + max((depth_of(p, seen | {name}) for p in parents), default=-1)
        depth[name] = value
        return value

    rows: dict[int, int] = defaultdict(int)
    positions = {}
    for name in names:
        column = depth_of(name)
        positions[name] = [column * _COLUMN_SPACING, rows[column] * _ROW_SPACING]
        rows[column] += 1
    return positions


def _layout_entries(tiles: list, path: str, ids: dict[str, str]) -> list[dict]:
    entries = []
    for i, tile in enumerate(_sequence(tiles, path)):
        tile_path = f"{path}[{i}]"
        tile = _mapping(tile, tile_path)
        name = tile.get("node")
        if not isinstance(name, str) or name not in ids:
            _fail(f"{tile_path}.node", f"names no node: {name!r}.")
        entry = {k: copy.deepcopy(v) for k, v in tile.items() if k != "node"}
        for key in ("width", "height"):
            if key in entry and entry[key] is not None:
                _number(entry[key], f"{tile_path}.{key}")
        if "visible" in entry and not isinstance(entry["visible"], bool):
            _fail(f"{tile_path}.visible", f"must be true or false, got {entry['visible']!r}.")
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
    _unknown_keys(doc, TOP_LEVEL_KEYS, "document")
    version = doc.get("flowdash")
    if version != FORMAT_VERSION:
        _fail("flowdash", f"unsupported format version {version!r}; this release reads 1.")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        _fail("title", f"must be a string, got {title!r}.")
    settings = _mapping(doc.get("settings"), "settings")
    if not all(isinstance(k, str) for k in settings):
        _fail("settings", "keys must be setting names.")

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
        source, source_port = split_port(edge["from"], f"{path}.from")
        target, target_port = split_port(edge["to"], f"{path}.to")
        for name, key in ((source, "from"), (target, "to")):
            if name not in ids:
                _fail(f"{path}.{key}", f"names no node: {name!r}.")
        transform = _sequence(edge.get("transform"), f"{path}.transform")
        for j, step in enumerate(transform):
            if not isinstance(step, dict) or "op" not in step:
                _fail(f"{path}.transform[{j}]", "must be a mapping with an 'op'.")
        entry = {
            "source": ids[source],
            "source_port": source_port,
            "target": ids[target],
            "target_port": target_port,
        }
        if transform:
            entry["transform"] = copy.deepcopy(transform)
        edges.append(entry)
        wiring.append((source, target))

    unplaced = [
        n for n, node in nodes.items() if _mapping(node, f"nodes.{n}").get("position") is None
    ]
    auto = _auto_positions(list(nodes), wiring) if unplaced else {}
    items = []
    for name, node in nodes.items():
        path = f"nodes.{name}"
        node = _mapping(node, path)
        _unknown_keys(node, NODE_KEYS, path)
        component = node.get("component")
        if not isinstance(component, str) or not component:
            _fail(f"{path}.component", "is required and names a component, e.g. 'Views/kpi'.")
        config = _mapping(node.get("config"), f"{path}.config")
        position = node.get("position")
        if position is None:
            position = auto[name]
        elif not isinstance(position, list) or len(position) != 2:
            _fail(f"{path}.position", f"must be [x, y], got {position!r}.")
        x = _number(position[0], f"{path}.position")
        y = _number(position[1], f"{path}.position")
        items.append(
            {
                "instance_id": ids[name],
                "component_id": component,
                "x": x,
                "y": y,
                "config": copy.deepcopy(config),
            }
        )

    layout = _mapping(doc.get("layout"), "layout")
    _unknown_keys(layout, LAYOUT_KEYS, "layout")
    reference_width = layout.get("reference_width")
    if reference_width is not None:
        _number(reference_width, "layout.reference_width")
    breakpoints = _sequence(layout.get("breakpoints"), "layout.breakpoints")
    for i, value in enumerate(breakpoints):
        _number(value, f"layout.breakpoints[{i}]")
    responsive = {
        str(band): _layout_entries(entries, f"layout.responsive.{band}", ids)
        for band, entries in _mapping(layout.get("responsive"), "layout.responsive").items()
    }
    return {
        "version": _MODEL_VERSION,
        "title": title or "Untitled",
        "items": items,
        "edges": edges,
        "tile_layout": _layout_entries(layout.get("tiles"), "layout.tiles", ids),
        "breakpoints": list(breakpoints),
        "responsive_layouts": responsive,
        "reference_width": reference_width,
        "settings": copy.deepcopy(settings),
    }


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
