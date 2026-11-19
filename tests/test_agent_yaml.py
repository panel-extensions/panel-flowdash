"""The dashboard YAML format: round trips, naming, schema and validation."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import (
    FORMAT_VERSION,
    DashboardYAMLError,
    component_schema,
    dashboard_from_yaml,
    dashboard_schema,
    dashboard_to_yaml,
    validate_dashboard,
)
from panel_flowdash.agent.yaml_format import node_names
from panel_flowdash.dashboard_store import MODEL_VERSION, DashboardEdge, DashboardItem
from panel_flowdash.editor import RESTRICTED_COMPONENT_ID
from panel_flowdash.port_types import port_types_compatible

EXAMPLES = Path(__file__).parents[1] / "examples"
WIND = EXAMPLES / "wind_energy"
SEEDS = sorted((WIND / "_dashboards").glob("*.json"))
PROJECTS = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir() and not p.name.startswith("."))
SECTIONS = {"Sources", "Controls", "Transforms", "Views", "Explore", "Components", "Pages"}

pytestmark = pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index")


@pytest.fixture
def wind(tmp_path, monkeypatch):
    """The wind energy project's ``__init__.py``, with its store under *tmp_path*."""
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(WIND), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", WIND / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.store.close()
    for name in list(sys.modules):
        if name.split(".")[0] in SECTIONS:
            del sys.modules[name]


def _project_editor(name, monkeypatch, **params):
    monkeypatch.setattr(sys, "path", [str(EXAMPLES / name), *sys.path])
    return FlowDash(str(EXAMPLES / name), notifications=False, **params)


def _catalog(editor):
    entries = editor._component_entries
    return {
        "specs": editor.component_specs,
        "settings_class": editor.settings_class,
        "controls": [cid for cid, e in entries.items() if e.metadata.sidebar],
        "singletons": [cid for cid, e in entries.items() if e.metadata.singleton],
    }


def _errors(issues):
    return [str(i) for i in issues if i.severity == "error"]


@pytest.mark.parametrize("seed", SEEDS, ids=lambda p: p.stem)
def test_seed_dashboard_round_trips(seed):
    data = json.loads(seed.read_text())
    text = dashboard_to_yaml(data)
    assert dashboard_from_yaml(text) == {
        **data,
        "version": MODEL_VERSION,
        "turns": [],
        "parameters": {},
        "groups": [],
    }
    assert dashboard_to_yaml(dashboard_from_yaml(text)) == text


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("seed", SEEDS, ids=lambda p: p.stem)
def test_seed_dashboard_round_trips_through_the_editor_and_validates(wind, seed, version):
    # The seeds hold no key added after version 1, so they read the same under
    # every header, including the 3 and 4 that development builds wrote.
    store = MemoryDashboardStore()
    wind.seed_dashboards(store, "demo")
    editor = FlowDash(
        str(WIND), store=store, settings_class=wind.settings_class, notifications=False
    )
    try:
        editor.load(seed.stem)
        exported = editor.export_dashboard()
        current = dashboard_to_yaml(exported)
        text = current.replace(f"flowdash: {FORMAT_VERSION}\n", f"flowdash: {version}\n", 1)
        assert text.startswith(f"flowdash: {version}\n")
        assert _errors(validate_dashboard(text, **_catalog(editor))) == []
        assert dashboard_to_yaml(dashboard_from_yaml(text)) == current
        editor.import_dashboard(text)
        assert editor.export_dashboard() == exported
        assert editor.dirty
    finally:
        editor.dispose()


def _wire_everything(editor):
    """Place every component of the editor once and connect each input it can."""
    ids = {}
    for component_id in editor.component_specs:
        ids[component_id] = editor.add_component(component_id)
    for target_id, target in editor.component_specs.items():
        for port in target.inputs:
            for source_id, source in editor.component_specs.items():
                out = next(
                    (o for o in source.outputs if port_types_compatible(o.type, port.type)), None
                )
                if source_id != target_id and out is not None:
                    if editor.connect(ids[source_id], out.name, ids[target_id], port.name) is True:
                        break


@pytest.mark.parametrize("project", PROJECTS)
def test_every_example_project_round_trips(project, monkeypatch):
    editor = _project_editor(project, monkeypatch)
    try:
        editor.new_dashboard("All components")
        _wire_everything(editor)
        exported = editor.export_dashboard()
        text = dashboard_to_yaml(exported)
        assert _errors(validate_dashboard(text, **_catalog(editor))) == []
        editor.import_dashboard(text)
        assert editor.export_dashboard() == exported
    finally:
        editor.dispose()
        for name in list(sys.modules):
            if name.split(".")[0] in SECTIONS:
                del sys.modules[name]


@register(page=False, component=True, title="Number", provides=[{"key": "value", "type": "int"}])
def number(config):
    return "number"


@register(
    page=False,
    component=True,
    title="Label",
    provides=[{"key": "text", "type": "str"}],
    requires=[{"key": "value", "type": "int", "required": True}],
)
def label(config):
    return "label"


@register(page=False, component=True, title="Restricted", provides=[{"key": "out"}])
def restricted(config):
    return "restricted"


COMPONENTS = {"T/number": number, "T/label": label, "T/restricted": restricted}


def _editor(allowed=True, **params):
    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        user="alice",
        authorize_component=lambda entry: allowed or entry.app_id != "T/restricted",
        **params,
    )


class TestNames:
    def test_generated_ids_are_named_after_their_component(self):
        editor = _editor()
        a = editor.add_component("T/number")
        b = editor.add_component("T/number")
        c = editor.add_component("T/label")
        editor.connect(b, "value", c, "value")
        doc = yaml.safe_load(dashboard_to_yaml(editor))
        assert list(doc["nodes"]) == [f"number_{a[-6:]}", f"number_{b[-6:]}", f"label_{c[-6:]}"]
        assert doc["edges"] == [{"from": f"number_{b[-6:]}.value", "to": f"label_{c[-6:]}.value"}]

    def test_names_never_move_to_another_node(self):
        editor = _editor()
        a = editor.add_component("T/number")
        b = editor.add_component("T/number")
        before = node_names(editor.export_dashboard()["items"])
        editor.remove_component(a)
        c = editor.add_component("T/number")
        after = node_names(editor.export_dashboard()["items"])
        assert after[b] == before[b]
        assert after[c] != before[a]

    def test_names_map_back_to_the_ids_they_were_exported_from(self):
        editor = _editor()
        source = editor.add_component("T/number")
        target = editor.add_component("T/label")
        text = dashboard_to_yaml(editor)
        text = text.replace("nodes:\n", "nodes:\n  extra:\n    component: T/number\n", 1)
        text += (
            f"edges:\n  - from: number_{source[-6:]}.value\n    to: label_{target[-6:]}.value\n"
        )
        editor.import_dashboard(text)
        ids = [item["instance_id"] for item in editor._tile_items]
        assert ids == ["extra", source, target]
        assert editor.graph.edges[0]["source"] == source

    def test_nodes_without_a_position_are_laid_out_by_depth(self):
        data = dashboard_from_yaml(
            "flowdash: 1\nnodes:\n  a: {component: T/number}\n  b: {component: T/label}\n"
            "  c: {component: T/label}\nedges:\n  - {from: a.value, to: b.value}\n"
        )
        positions = {i["instance_id"]: (i["x"], i["y"]) for i in data["items"]}
        assert positions["a"][0] == 0 and positions["b"][0] > 0
        assert positions["c"][0] == 0 and positions["c"][1] > 0


class TestRestricted:
    @pytest.fixture
    def store(self):
        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "Plant")
        model.items = [
            DashboardItem("r1", "T/restricted", config={"query": "SELECT secret"}),
            DashboardItem("n1", "T/number"),
            DashboardItem("l1", "T/label"),
            DashboardItem("l2", "T/label"),
        ]
        model.edges = [
            DashboardEdge("n1", "value", "l1", "value"),
            DashboardEdge("r1", "out", "l2", "value"),
        ]
        store.save_dashboard(model)
        return store, model.dashboard_id

    def _loaded(self, store, allowed):
        store, did = store
        editor = _editor(allowed, store=store)
        editor.load(did)
        return editor, did

    def test_export_writes_a_stand_in_and_marks_its_connections(self, store):
        editor, _ = self._loaded(store, allowed=False)
        text = dashboard_to_yaml(editor)
        assert "SELECT secret" not in text and "r1.out" not in text and "T/restricted" not in text
        doc = yaml.safe_load(text)
        assert doc["nodes"]["r1"] == {"component": RESTRICTED_COMPONENT_ID, "position": [0, 0]}
        assert {"from": "r1", "to": "l2.value"} in doc["edges"]

    def test_an_input_fed_by_a_restricted_node_takes_no_other_edge(self, store):
        editor, _ = self._loaded(store, allowed=False)
        doc = yaml.safe_load(dashboard_to_yaml(editor))
        catalog = {"specs": editor.component_specs}
        issues = validate_dashboard(doc, **catalog)
        assert not [i for i in issues if i.path.startswith("nodes.l2")]
        doc["edges"].append({"from": "n1.value", "to": "l2.value"})
        errors = _errors(validate_dashboard(doc, **catalog))
        assert errors == [
            f"edges[{len(doc['edges']) - 1}]: Input 'value' of 'l2' is already fed by a "
            "restricted node (edges[1]), which you cannot change; an input takes one edge "
            "unless it accepts a list."
        ]

    def test_loading_keeps_the_restricted_connections(self, store):
        editor, did = self._loaded(store, allowed=False)
        editor.import_dashboard(dashboard_to_yaml(editor))
        editor.save()
        saved = editor.store._load_any(did)
        assert sorted((e.source, e.target) for e in saved.edges) == [("n1", "l1"), ("r1", "l2")]

    def test_editing_the_stand_in_cannot_change_the_stored_item(self, store):
        editor, did = self._loaded(store, allowed=False)
        doc = yaml.safe_load(dashboard_to_yaml(editor))
        doc["nodes"]["r1"] = {"component": "T/restricted", "config": {"query": "DROP"}}
        doc["edges"] = [e for e in doc["edges"] if "." in e["from"]]
        doc["edges"].append({"from": "r1.out", "to": "l1.value"})
        editor.import_dashboard(yaml.safe_dump(doc, sort_keys=False))
        editor.save()
        saved = editor.store._load_any(did)
        assert (saved.items[0].instance_id, saved.items[0].config) == (
            "r1",
            {"query": "SELECT secret"},
        )
        assert sorted((e.source, e.target) for e in saved.edges) == [("n1", "l1"), ("r1", "l2")]

    def test_new_restricted_nodes_are_left_out(self):
        editor = _editor(allowed=False)
        editor.new_dashboard("mine")
        editor.import_dashboard("flowdash: 1\nnodes:\n  x: {component: T/restricted}\n")
        assert editor._tile_items == []

    def test_validation_refuses_edges_to_a_stand_in(self):
        issues = validate_dashboard(
            "flowdash: 1\nnodes:\n  r: {component: 'flowdash:restricted'}\n"
            "  l: {component: T/label}\nedges:\n  - {from: r.out, to: l.value}\n",
            _editor().component_specs,
        )
        assert any("restricted" in i.message and i.path == "edges[0]" for i in issues)


def _validate(text):
    return _errors(validate_dashboard(text, _editor().component_specs, singletons=["T/number"]))


class TestValidate:
    def test_structural_errors_name_the_key(self):
        assert _validate("flowdash: 1\nnodes:\n  a: {component: T/number, colour: red}\n") == [
            "nodes.a: unknown key 'colour'; expected component, origin, group, config, inputs, position."
        ]
        assert _validate("flowdash: 5\n") == [
            "flowdash: unsupported format version 5; this release reads versions 1 to 4. "
            "The file was written by a newer FlowDash."
        ]
        assert _validate("nodes: {}\n")[0].startswith("document:")
        assert _validate(
            "flowdash: 1\nnodes:\n  a: {component: T/number}\n  b: {component: T/label}\n"
            "edges:\n  - {from: a, to: b.value}\n"
        ) == ["edges[0].from: must be '<node>.<port>', got 'a'."]
        assert _validate(
            "flowdash: 1\nnodes:\n  a: {component: T/number}\n"
            "layout:\n  tiles:\n    - {node: a, colour: red}\n"
        ) == [
            "layout.tiles[0]: unknown key 'colour'; expected node, width, height, visible, framed, area."
        ]

    def test_unknown_component_suggests_a_close_match(self):
        (error,) = _validate("flowdash: 1\nnodes:\n  a: {component: T/numbers}\n")
        assert (
            error
            == "nodes.a.component: Unknown component 'T/numbers'. Did you mean 'T/number' or 'T/label'?"
        )

    def test_unknown_port_says_which_direction(self):
        errors = _validate(
            "flowdash: 1\nnodes:\n  a: {component: T/number}\n  b: {component: T/label}\n"
            "edges:\n  - {from: b.value, to: a.value}\n"
        )
        assert "edges[0].from: 'b' (Label) has no output 'value'. 'value' is an input; " in (
            errors[0] + " "
        )
        assert errors[1] == (
            "edges[0].to: 'a' (Number) has no input 'value'. 'value' is an output; "
            "'to' names an input."
        )

    def test_type_mismatch_cycle_and_double_input(self):
        errors = _validate(
            "flowdash: 1\nnodes:\n"
            "  a: {component: T/label}\n  b: {component: T/label}\n"
            "  n: {component: T/number}\n"
            "edges:\n"
            "  - {from: a.text, to: b.value}\n"
            "  - {from: n.value, to: b.value}\n"
        )
        assert errors[0].startswith("edges[0]: Type mismatch: output 'text' of 'a' produces 'str'")
        assert errors[1].startswith("edges[1]: Input 'value' of 'b' already has a connection")
        cycle = _validate(
            "flowdash: 1\nnodes:\n  a: {component: T/label}\n  b: {component: T/label}\n"
            "edges:\n"
            "  - {from: a.text, to: b.value, transform: [{op: length}]}\n"
            "  - {from: b.text, to: a.value, transform: [{op: length}]}\n"
        )
        assert [e for e in cycle if "cycle" in e] == [
            "edges: The edges form a cycle: a -> b -> a. Values must flow one way; "
            "remove one of these edges."
        ]

    def test_config_settings_transform_and_layout(self):
        specs = _editor().component_specs
        issues = validate_dashboard(
            "flowdash: 1\nsettings: {refresh_period: -5, colour: red}\n"
            "nodes:\n  a: {component: T/number, config: {size: 3}}\n"
            "  b: {component: T/number}\n  l: {component: T/label}\n"
            "edges:\n  - {from: a.value, to: l.value, transform: [{op: nope}]}\n"
            "layout:\n  tiles:\n    - {node: a, width: 120}\n    - {node: a}\n",
            specs,
            singletons=["T/number"],
        )
        messages = {i.path: (i.severity, i.message) for i in issues}
        assert messages["settings.refresh_period"][0] == "error"
        assert messages["settings.colour"][0] == "warning"
        assert messages["nodes.a.config.size"] == (
            "error",
            "'Number' takes no config; remove 'size'.",
        )
        assert messages["nodes.b.component"][1].startswith("'Number' can only be placed once")
        assert messages["edges[0].transform"] == ("error", "Unknown transform 'nope'.")
        assert messages["layout.tiles[0].width"][0] == "error"
        assert messages["layout.tiles[1]"][1].startswith("'a' already has a tile")

    def test_unconnected_required_input_is_a_warning(self):
        issues = validate_dashboard(
            "flowdash: 1\nnodes:\n  l: {component: T/label}\n", _editor().component_specs
        )
        assert [(i.path, i.severity) for i in issues] == [("nodes.l", "warning")]

    def test_validating_does_not_change_the_document(self):
        doc = {"flowdash": 1, "nodes": {"a": {"component": "T/number"}}}
        before = copy.deepcopy(doc)
        validate_dashboard(doc, _editor().component_specs)
        assert doc == before

    def test_reading_invalid_yaml_raises(self):
        with pytest.raises(DashboardYAMLError, match="Not valid YAML"):
            dashboard_from_yaml("flowdash: [1")


class TestSchema:
    def test_schema_names_the_written_version(self):
        schema = dashboard_schema({})
        assert schema["properties"]["flowdash"] == {"const": FORMAT_VERSION}
        assert schema["$id"].endswith(f"/dashboard-{FORMAT_VERSION}.json")

    def test_component_schema_lists_ports_and_config(self, wind):
        editor = FlowDash(str(WIND), notifications=False)
        schema = component_schema(editor.component_specs["Views/kpi"])
        assert schema["inputs"] == [{"name": "turbines", "type": "DataFrame", "required": False}]
        metric = schema["config"]["properties"]["metric"]
        assert "turbines" in metric["enum"] and metric["default"] == "turbines"
        editor.dispose()

    def test_seed_dashboards_satisfy_the_schema(self, wind):
        jsonschema = pytest.importorskip("jsonschema")
        editor = FlowDash(str(WIND), settings_class=wind.settings_class, notifications=False)
        schema = dashboard_schema(editor.component_specs, wind.settings_class)
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        for seed in SEEDS:
            doc = yaml.safe_load(dashboard_to_yaml(json.loads(seed.read_text())))
            assert list(validator.iter_errors(doc)) == [], seed.stem
        doc["nodes"]["map"]["config"] = {"color_by": "rainbow"}
        assert list(validator.iter_errors(doc))
        editor.dispose()


def test_yaml_errors_are_reported_as_yaml():
    editor = _editor()
    with pytest.raises(ValueError, match="Not valid YAML"):
        editor.import_dashboard("flowdash: 1\nnodes: [unclosed\n")
    with pytest.raises(ValueError, match="Not valid JSON"):
        editor.import_dashboard("{not json")
