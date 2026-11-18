"""The YAML reader's limits, value rules, stable names and restricted connections."""

import json

import param
import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_schema,
    dashboard_to_yaml,
    validate_dashboard,
)
from panel_flowdash.agent.yaml_format import MAX_SIZE, editor_restricted, load_yaml, node_names
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem

from .test_agent_yaml import COMPONENTS, _editor, _errors


class Options(param.Parameterized):
    options = param.Dict(default={})


@register(page=False, component=True, title="Dicty", config_schema=Options, provides=["out"])
def dicty(config):
    return "dicty"


def _dicty_editor(**params):
    return FlowDash(
        components={**COMPONENTS, "T/dicty": dicty},
        include_builtin_components=False,
        notifications=False,
        **params,
    )


def _bomb(levels):
    lines = "a0: &a0 [lol,lol,lol,lol,lol,lol,lol,lol,lol]\n"
    for i in range(1, levels):
        lines += f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 9) + "]\n"
    indented = "".join("        " + line for line in lines.splitlines(True))
    return (
        "flowdash: 1\nnodes:\n  n:\n    component: T/dicty\n    config:\n      options:\n"
        + indented
    )


class TestParser:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (_bomb(7), "anchors and aliases"),
            ("flowdash: 1\nnodes: &r [*r]\n", "anchors and aliases"),
            ("flowdash: 1\ntitle: x\ntitle: y\n", "'title' is defined twice"),
            ("flowdash: 1\n1: x\n", "keys must be strings"),
            ("flowdash: 1\nnodes: !!set {a}\n", "values are not supported"),
            ("flowdash: 1\ntitle: !!binary aGk=\n", "values are not supported"),
            ("flowdash: 1\nx: " + "[" * 50 + "]" * 50 + "\n", "nested deeper than 32"),
        ],
    )
    def test_refuses_what_json_cannot_hold(self, text, message):
        with pytest.raises(DashboardYAMLError, match=message):
            load_yaml(text)

    def test_refuses_large_text(self):
        with pytest.raises(DashboardYAMLError, match="Too large"):
            load_yaml("flowdash: 1\n# " + "x" * MAX_SIZE)

    def test_dates_stay_strings(self):
        assert load_yaml("when: 2024-01-01")["when"] == "2024-01-01"

    def test_every_entry_point_uses_the_limits(self):
        editor = _dicty_editor()
        editor.new_dashboard("mine")
        (issue,) = validate_dashboard(_bomb(7), editor.component_specs)
        assert str(issue).startswith("document: line")
        with pytest.raises(ValueError, match="anchors and aliases"):
            editor.import_dashboard(_bomb(7))
        notes = []
        editor._notify = lambda severity, message, duration=0: notes.append(message)
        editor._on_files_dropped([{"name": "shared.json", "content": _bomb(7)}])
        assert "anchors and aliases" in notes[-1] and editor._tile_items == []
        editor._on_files_dropped([{"name": "deep.json", "content": "[" * 100_000 + "]" * 100_000}])
        assert notes[-1].startswith("Could not load deep.json")

    def test_non_json_values_in_config_name_their_path(self):
        editor = _dicty_editor()
        issues = validate_dashboard(
            "flowdash: 1\nnodes:\n  n:\n    component: T/dicty\n    config:\n"
            "      options: {ratio: .nan}\n",
            editor.component_specs,
        )
        assert [str(i) for i in issues] == [
            "nodes.n.config.options.ratio: must be a finite number, got nan."
        ]

    def test_the_writer_never_writes_aliases(self):
        shared = {"a": 1}
        doc = dashboard_to_yaml(
            {"title": "t", "items": [], "settings": {"x": shared, "y": shared}, "edges": []}
        )
        assert "&" not in doc and "*" not in doc


class TestVersions:
    @pytest.mark.parametrize("version", ["1.0", "true", "'1'"])
    def test_the_version_is_an_integer(self, version):
        with pytest.raises(DashboardYAMLError, match="unsupported format version"):
            dashboard_from_yaml(f"flowdash: {version}\n")

    def test_a_newer_version_says_so(self):
        with pytest.raises(DashboardYAMLError, match="written by a newer FlowDash"):
            dashboard_from_yaml("flowdash: 3\n")

    def test_extension_keys_are_ignored_everywhere(self):
        data = dashboard_from_yaml(
            "flowdash: 1\nx-tool: {a: 1}\nnodes:\n  a: {component: T/number, x-note: hi}\n"
            "  b: {component: T/label}\nedges:\n  - {from: a.value, to: b.value, x-why: z}\n"
            "layout:\n  x-grid: 1\n  tiles:\n    - {node: a, x-colour: red}\n"
        )
        assert data["tile_layout"] == [{"instance_id": "a", "index": 0}]
        assert "x-note" not in json.dumps(data)


class TestNames:
    def test_names_ignore_component_renames(self):
        @register(page=False, component=True, title="Metric", aliases=["Views/kpi"])
        def metric(config):
            return "m"

        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "A")
        model.items = [DashboardItem("Views__kpi_3fa2c1", "Views/kpi")]
        store.save_dashboard(model)
        editor = FlowDash(
            components={"Views/metric": metric},
            include_builtin_components=False,
            notifications=False,
            store=store,
        )
        editor.load(model.dashboard_id)
        assert list(yaml.safe_load(dashboard_to_yaml(editor))["nodes"]) == ["kpi_3fa2c1"]

    def test_restricted_and_allowed_users_see_the_same_names(self):
        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "A")
        model.items = [DashboardItem("T__restricted_a1b2c3", "T/restricted")]
        store.save_dashboard(model)
        names = []
        for allowed in (False, True):
            editor = _editor(allowed, store=store)
            editor.load(model.dashboard_id)
            names.append(list(yaml.safe_load(dashboard_to_yaml(editor))["nodes"]))
        assert names[0] == names[1] == ["restricted_a1b2c3"]

    def test_names_from_a_file_are_kept(self):
        editor = _editor()
        editor.new_dashboard("mine")
        editor.import_dashboard("flowdash: 1\nnodes:\n  revenue_cafe01: {component: T/number}\n")
        assert list(yaml.safe_load(dashboard_to_yaml(editor))["nodes"]) == ["revenue_cafe01"]

    def test_new_names_may_not_contain_double_underscores(self):
        with pytest.raises(DashboardYAMLError, match="may not contain '__'"):
            dashboard_from_yaml("flowdash: 1\nnodes:\n  a__b: {component: T/number}\n")

    def test_colliding_ids_get_distinct_names(self):
        items = [
            {"instance_id": "Views__kpi_3fa2c1", "component_id": "Views/kpi"},
            {"instance_id": "Explore__kpi_3fa2c1", "component_id": "Explore/kpi"},
            {"instance_id": "kpi_3fa2c1", "component_id": "Views/kpi"},
        ]
        names = node_names(items)
        assert names["kpi_3fa2c1"] == "kpi_3fa2c1"
        assert len(set(names.values())) == 3
        assert node_names(reversed(items)) == names
        export = {"title": "t", "items": [{**i, "x": 0, "y": 0, "config": {}} for i in items]}
        assert len(yaml.safe_load(dashboard_to_yaml(export))["nodes"]) == 3


@pytest.fixture
def secret_editor():
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    model.items = [
        DashboardItem("T__restricted_a1b2c3", "T/restricted", config={"q": "SELECT secret"}),
        DashboardItem("n1", "T/number"),
        DashboardItem("l1", "T/label"),
        DashboardItem("l2", "T/label"),
    ]
    model.edges = [DashboardEdge("T__restricted_a1b2c3", "out", "l1", "value")]
    store.save_dashboard(model)
    editor = _editor(False, store=store)
    editor.load(model.dashboard_id)
    return editor


class TestRestrictedConnections:
    def _check(self, editor, doc):
        nodes, edges = editor_restricted(editor)
        return _errors(
            validate_dashboard(
                doc, editor.component_specs, restricted_nodes=nodes, restricted_edges=edges
            )
        )

    def test_dropping_the_bare_edge_is_refused_and_the_input_stays_taken(self, secret_editor):
        doc = yaml.safe_load(dashboard_to_yaml(secret_editor))
        doc["edges"] = [{"from": "n1.value", "to": "l1.value"}]
        errors = self._check(secret_editor, doc)
        assert any("keep {from: restricted_a1b2c3, to: l1.value}" in e for e in errors)
        assert any("already fed by a restricted node" in e for e in errors)

    def test_retargeting_the_bare_edge_is_refused(self, secret_editor):
        doc = yaml.safe_load(dashboard_to_yaml(secret_editor))
        doc["edges"] = [{"from": "restricted_a1b2c3", "to": "l2.value"}]
        errors = self._check(secret_editor, doc)
        assert any("is not a stored connection of a restricted node" in e for e in errors)
        assert any("keep {from: restricted_a1b2c3, to: l1.value}" in e for e in errors)

    def test_restricted_nodes_cannot_be_replaced_dropped_or_invented(self, secret_editor):
        doc = yaml.safe_load(dashboard_to_yaml(secret_editor))
        doc["nodes"]["restricted_a1b2c3"] = {"component": "T/number"}
        doc["edges"] = [e for e in doc["edges"] if "restricted" not in e["from"]]
        assert any("cannot be replaced" in e for e in self._check(secret_editor, doc))
        del doc["nodes"]["restricted_a1b2c3"]
        assert any("must stay in the YAML" in e for e in self._check(secret_editor, doc))
        doc["nodes"]["ghost"] = {"component": "flowdash:restricted"}
        assert any("names no restricted node" in e for e in self._check(secret_editor, doc))

    def test_the_export_validates_against_its_canvas_and_the_schema(self, secret_editor):
        jsonschema = pytest.importorskip("jsonschema")
        text = dashboard_to_yaml(secret_editor)
        assert self._check(secret_editor, text) == []
        schema = dashboard_schema(secret_editor.component_specs)
        assert (
            list(jsonschema.Draft202012Validator(schema).iter_errors(yaml.safe_load(text))) == []
        )
        bad = yaml.safe_load(text)
        bad.setdefault("layout", {})["tiles"] = [{"node": "n1", "colour": "red"}]
        assert list(jsonschema.Draft202012Validator(schema).iter_errors(bad))


class TestRoundTrip:
    def test_empty_titles_and_line_separators_survive(self):
        export = {
            "title": "",
            "items": [
                {
                    "instance_id": "a",
                    "component_id": "T/number",
                    "x": 0,
                    "y": 0,
                    "config": {"s": "a\x85b\u2028c"},
                }
            ],
            "edges": [],
        }
        data = dashboard_from_yaml(dashboard_to_yaml(export))
        assert data["title"] == ""
        assert data["items"][0]["config"] == {"s": "a\x85b\u2028c"}


def test_long_chains_validate_without_recursion():
    nodes = "".join(f"  n{i}: {{component: T/label}}\n" for i in range(3000))
    edges = "".join(f"  - {{from: n{i}.text, to: n{i + 1}.value}}\n" for i in range(2999))
    issues = validate_dashboard(
        f"flowdash: 1\nnodes:\n{nodes}edges:\n{edges}", _editor().component_specs
    )
    assert all(i.path != "document" for i in issues)


class TestWrittenNamesLoad:
    IDS = (
        "Views__3d_plot_4ceaca",
        "Views__kpi_3fa2c1",
        "Views__kpi",
        "my__thing",
        "a___b",
        "9lives",
        "__private",
        "x y",
        "-dash",
        "Ünïcode__ok_1a2b3c",
        "plain",
        "kpi_3fa2c1",
        "Explore__kpi_3fa2c1",
    )

    @pytest.mark.parametrize("instance_id", IDS)
    def test_every_written_name_loads_as_a_new_name(self, instance_id):
        name = node_names([{"instance_id": instance_id}])[instance_id]
        data = dashboard_from_yaml(f"flowdash: 1\nnodes:\n  {name}: {{component: T/number}}\n")
        assert [i["instance_id"] for i in data["items"]] == [name]

    def test_a_whole_export_of_odd_ids_loads_into_a_fresh_dashboard(self):
        items = [
            {"instance_id": iid, "component_id": "T/number", "x": 0, "y": 0, "config": {}}
            for iid in self.IDS
        ]
        text = dashboard_to_yaml({"title": "t", "items": items, "edges": []})
        assert len(dashboard_from_yaml(text)["items"]) == len(self.IDS)

    def test_new_nodes_never_take_an_existing_name(self, monkeypatch):
        import uuid as uuid_module

        editor = _editor()
        first = editor.add_component("T/number")
        suffix = first[-6:]
        real = uuid_module.uuid4

        class Fixed:
            def __init__(self, value):
                self.hex = value + "0" * 26

        draws_left = [suffix, suffix]

        def uuid4():
            return Fixed(draws_left.pop()) if draws_left else real()

        monkeypatch.setattr(uuid_module, "uuid4", uuid4)
        second = editor.add_component("T/number")
        names = node_names(editor.export_dashboard()["items"])
        assert second != first and names[first] == f"number_{suffix}"
        assert names[second] != names[first]


class TestScalars:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("12:30", "12:30"),
            ("017", "017"),
            ("yes", "yes"),
            ("no", "no"),
            ("1_000", "1_000"),
            ("2024-01-01", "2024-01-01"),
            ("0o17", 15),
            ("0x1f", 31),
            ("1e3", 1000.0),
            ("-3", -3),
            ("True", True),
            ("~", None),
        ],
    )
    def test_core_schema_typing(self, text, value):
        assert load_yaml(f"a: {text}")["a"] == value

    @pytest.mark.parametrize("value", ["12:30", "017", "yes", "0o17", "1e3", "true", "~", ""])
    def test_strings_that_look_typed_are_quoted(self, value):
        export = {"title": "t", "items": [], "edges": [], "settings": {"s": value}}
        assert load_yaml(dashboard_to_yaml(export))["settings"]["s"] == value

    @pytest.mark.parametrize("text", ["!!int abc", "!!float abc", "!!bool maybe", "9" * 5000])
    def test_bad_scalars_are_issues(self, text):
        (issue,) = validate_dashboard(f"flowdash: 1\ntitle: {text}\n", _editor().component_specs)
        assert issue.path == "document"

    @pytest.mark.parametrize(
        "doc",
        [
            "nodes:\n  a: {component: T/number, position: [.nan, 0]}\n",
            "layout:\n  reference_width: .inf\n",
            "layout:\n  breakpoints: [.nan]\n",
            "nodes:\n  a: {component: T/number}\nlayout:\n  tiles:\n    - {node: a, height: .inf}\n",
        ],
    )
    def test_numbers_must_be_finite(self, doc):
        with pytest.raises(DashboardYAMLError, match="finite number"):
            dashboard_from_yaml("flowdash: 1\n" + doc)

    def test_the_size_limit_counts_bytes_for_bytes(self):
        with pytest.raises(DashboardYAMLError, match="bytes"):
            load_yaml(b"# " + b"x" * MAX_SIZE)


def test_a_transform_on_a_restricted_connection_is_refused():
    with pytest.raises(DashboardYAMLError, match="keeps its stored transform"):
        dashboard_from_yaml(
            "flowdash: 1\nnodes:\n  r: {component: 'flowdash:restricted'}\n"
            "  l: {component: T/label}\nedges:\n"
            "  - {from: r, to: l.value, transform: [{op: head, n: 1}]}\n"
        )


class TestJsonSpelling:
    def test_json_with_a_version_follows_the_yaml_rules(self):
        editor = _editor()
        editor.new_dashboard("mine")
        with pytest.raises(ValueError, match="'title' is defined twice"):
            editor.import_dashboard('{"flowdash": 1, "title": "a", "title": "b"}')

    def test_flow_style_yaml_is_read_as_yaml(self):
        editor = _editor()
        editor.new_dashboard("mine")
        editor.import_dashboard("{flowdash: 1, nodes: {a: {component: T/number}}}")
        assert [i["instance_id"] for i in editor._tile_items] == ["a"]

    def test_broken_json_still_says_json(self):
        with pytest.raises(ValueError, match="Not valid JSON"):
            _editor().import_dashboard('{"items": [}')


class TestUnicode:
    def test_json_with_escaped_emoji_keeps_it(self):
        editor = _editor()
        text = json.dumps({"flowdash": 1, "title": "Wind 🌬", "settings": {"description": "🌬"}})
        from panel_flowdash.agent.yaml_format import read_dashboard_text

        assert read_dashboard_text(text)["title"] == "Wind 🌬"
        editor.new_dashboard("mine")
        editor.import_dashboard(text)
        assert editor.settings.description == "🌬"
        json.dumps(editor.export_dashboard(), ensure_ascii=False).encode("utf-8")

    @pytest.mark.parametrize(
        "text",
        ['flowdash: 1\ntitle: "\\ud800"\n', '{"flowdash": 1, "title": "\\ud800"}'],
    )
    def test_lone_surrogates_are_refused(self, text):
        editor = _editor()
        with pytest.raises(ValueError, match="not valid Unicode"):
            editor.import_dashboard(text)

    def test_json_spelled_dashboards_follow_the_depth_limit(self):
        nested = "[" * 40 + "]" * 40
        with pytest.raises(ValueError, match="nested deeper than 32"):
            _editor().import_dashboard(f'{{"flowdash": 1, "x-deep": {nested}}}')


@pytest.mark.parametrize("value", ["-0o17", "+0x1f", "+0o7", "-0x1F"])
def test_signed_octal_and_hex_strings_are_quoted(value):
    export = {"title": "t", "items": [], "edges": [], "settings": {"s": value}}
    text = dashboard_to_yaml(export)
    assert f"s: '{value}'" in text
    assert load_yaml(text)["settings"]["s"] == value
