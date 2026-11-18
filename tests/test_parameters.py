"""Parameters: format version 4, binding, contracts and the stores."""

import json
import sqlite3
from contextlib import closing

import param
import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
    dashboard_to_yaml,
)
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.parameters import (
    ContractContext,
    ContractError,
    ContractProblem,
    DashboardParameter,
    ParameterError,
    bind_document,
    coerce,
    decode_parameters,
    get_field,
    parameters_schema,
    set_field,
)


class Columns:
    """The columns of a table node that the pick nodes downstream read."""

    def expects(self, context: ContractContext):
        nodes = context.document["nodes"]
        read = {}
        for name in context.downstream():
            column = (nodes[name].get("config") or {}).get("column")
            if column:
                read.setdefault(column, []).append(name)
        return {"columns": sorted(read)} if context.value is not None else None

    def check(self, context: ContractContext, expects):
        nodes = context.document["nodes"]
        have = set(context.value or [])
        problems = []
        for name in context.downstream():
            column = (nodes[name].get("config") or {}).get("column")
            if column in expects["columns"] and column not in have:
                problems.append(ContractProblem(name, f"The table has no column {column!r}."))
        if not have:
            problems.append(ContractProblem(None, "The table is empty.", warning=True))
        return problems


class TableConfig(param.Parameterized):
    columns = param.List(default=["a", "b"])
    spec = param.Dict(default={})


@register(
    page=False,
    component=True,
    title="Table",
    config_schema=TableConfig,
    contract=Columns(),
    provides=[{"key": "table", "type": "str"}],
)
def table(config, instance_config):
    def update(*_):
        config.table = ",".join(instance_config.columns)

    instance_config.param.watch(update, "columns")
    update()
    return "table"


class PickConfig(param.Parameterized):
    column = param.String(default="a")
    limit = param.Integer(default=1)


@register(
    page=False,
    component=True,
    title="Pick",
    config_schema=PickConfig,
    provides=[{"key": "picked", "type": "str"}],
    requires=[{"key": "table", "type": "str", "required": True}],
)
def pick(config, instance_config):
    def update(*_):
        table = (config.table or "").split(",")
        column = instance_config.column
        config.picked = column if column in table else None

    config.param.watch(update, "table")
    instance_config.param.watch(update, "column")
    update()
    return "pick"


COMPONENTS = {"T/table": table, "T/pick": pick}

DOC = """\
flowdash: 4
title: Picks
parameters:
  cols:
    node: data
    field: columns
    type: value
    description: The table's columns
  which: {node: first, field: column, type: string}
  limit: {node: first, field: limit, type: integer}
nodes:
  data:
    component: T/table
    config: {columns: [a, b, c]}
    position: [0, 0]
  first:
    component: T/pick
    config: {column: a}
    position: [300, 0]
  second:
    component: T/pick
    config: {column: c}
    position: [300, 200]
edges:
  - {from: data.table, to: first.table}
  - {from: data.table, to: second.table}
"""


def _editor(**params):
    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        user="alice",
        history_coalesce=0,
        **params,
    )


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Picks")
    editor.import_dashboard(DOC)
    yield editor
    editor.dispose()


def _output(editor, node, port):
    return DashboardOps(editor).node_output(f"{node}.{port}")


class TestFields:
    def test_get_and_set_a_nested_field(self):
        config = {"spec": {"tables": {"orders": "a.csv"}, "layers": [{"mark": "bar"}]}}
        assert get_field(config, "spec.tables.orders") == "a.csv"
        assert get_field(config, "spec.layers.0.mark") == "bar"
        changed = set_field(config, "spec.layers.0.mark", "line")
        assert changed["spec"]["layers"][0]["mark"] == "line"
        assert config["spec"]["layers"][0]["mark"] == "bar"

    def test_set_creates_missing_mappings_but_not_list_items(self):
        assert set_field({}, "spec.tables.orders", "x") == {"spec": {"tables": {"orders": "x"}}}
        with pytest.raises(ParameterError, match=r"no 'spec\.layers\.3'"):
            set_field({"spec": {"layers": []}}, "spec.layers.3", "x")

    @pytest.mark.parametrize("path", ["", "0.a", "a..b", "a.b c", 3])
    def test_malformed_paths_are_refused(self, path):
        with pytest.raises(ParameterError):
            get_field({}, path)


class TestCoerce:
    @pytest.mark.parametrize(
        "kind, value, expected",
        [
            ("number", "10", 10.0),
            ("number", 3, 3),
            ("integer", "7", 7),
            ("integer", "7.0", 7),
            ("boolean", "yes", True),
            ("boolean", "False", False),
            ("date", "2026-01-02", "2026-01-02"),
            ("datetime", "2026-01-02T03:04:05Z", "2026-01-02T03:04:05Z"),
            ("value", "[1, 2]", [1, 2]),
            ("value", "plain text", "plain text"),
            ("source", "orders.parquet", "orders.parquet"),
        ],
    )
    def test_text_is_read_as_the_type(self, kind, value, expected):
        assert coerce(kind, value) == expected

    @pytest.mark.parametrize(
        "kind, value",
        [
            ("number", "ten"),
            ("number", "nan"),
            ("integer", "7.5"),
            ("boolean", "maybe"),
            ("date", "02/01/2026"),
            ("string", 3),
            ("value", {1: 2}),
        ],
    )
    def test_values_of_another_type_are_refused(self, kind, value):
        with pytest.raises(ParameterError):
            coerce(kind, value)


class TestFormat:
    def test_round_trip(self, editor):
        doc = dashboard_to_dict(editor)
        assert doc["flowdash"] == 4
        assert doc["parameters"]["which"] == {"node": "first", "field": "column", "type": "string"}
        assert doc["parameters"]["cols"]["description"] == "The table's columns"
        again = _editor()
        try:
            again.import_dashboard(dashboard_to_yaml(editor))
            assert dashboard_to_dict(again)["parameters"] == doc["parameters"]
        finally:
            again.dispose()

    def test_parameters_name_nodes_by_instance_id_in_the_model(self, editor):
        model = editor.to_model()
        assert model.parameters["which"].node == "first"
        assert set(model.parameters) == {"cols", "which", "limit"}

    def test_earlier_versions_have_no_parameters(self):
        with pytest.raises(DashboardYAMLError, match="document: unknown key 'parameters'"):
            dashboard_from_yaml(DOC.replace("flowdash: 4", "flowdash: 3"))

    @pytest.mark.parametrize(
        "change, path",
        [
            (("node: data", "node: nowhere"), "parameters.cols.node"),
            (("field: columns", "field: '0'"), "parameters.cols.field"),
            (("type: value", "type: matrix"), "parameters.cols.type"),
            (("  cols:", "  2cols:"), "parameters.2cols"),
        ],
    )
    def test_malformed_parameters_name_the_key(self, change, path):
        with pytest.raises(DashboardYAMLError) as info:
            dashboard_from_yaml(DOC.replace(*change, 1))
        assert info.value.path == path

    def test_a_parameter_of_a_removed_node_is_dropped(self, editor):
        DashboardOps(editor).remove_node("first")
        assert set(editor.parameters) == {"cols"}
        assert set(dashboard_to_dict(editor)["parameters"]) == {"cols"}

    def test_the_contract_records_what_the_nodes_read(self, editor):
        assert editor.parameters["cols"].expects == {"columns": ["a", "c"]}
        assert editor.parameters["which"].expects is None


class TestBinding:
    def test_bind_document_writes_coerced_values(self):
        doc = yaml.safe_load(DOC)
        bound = bind_document(doc, {"which": "b", "limit": "3", "cols": '["a", "c"]'})
        assert bound["nodes"]["first"]["config"] == {"column": "b", "limit": 3}
        assert bound["nodes"]["data"]["config"]["columns"] == ["a", "c"]
        assert doc["nodes"]["first"]["config"] == {"column": "a"}

    def test_bind_document_refuses_unknown_names_and_bad_values(self):
        doc = yaml.safe_load(DOC)
        with pytest.raises(ParameterError, match="Did you mean 'which'"):
            bind_document(doc, {"which_one": "b"})
        with pytest.raises(ParameterError, match=r"parameters\.limit: value: must be a whole"):
            bind_document(doc, {"limit": "1.5"})

    def test_binding_recomputes_and_is_one_edit(self, editor):
        versions = len(editor._history.versions)
        editor.bind_parameters({"which": "b", "cols": ["a", "b", "c", "d"]})
        assert _output(editor, "first", "picked") == "b"
        assert _output(editor, "data", "table") == "a,b,c,d"
        assert editor.to_model().items[1].config["column"] == "b"
        assert len(editor._history.versions) == versions + 1
        assert editor._history.versions[-1].metadata["label"] == "Bound which, cols"

    def test_binding_undoes_as_one_step(self, editor):
        editor.bind_parameters({"which": "b", "limit": 4})
        assert editor.undo()
        config = {item.instance_id: item.config for item in editor.to_model().items}
        assert config["first"] == {"column": "a"}

    def test_a_contract_failure_names_the_node_and_changes_nothing(self, editor):
        before = dashboard_to_dict(editor)
        with pytest.raises(ContractError) as info:
            editor.bind_parameters({"cols": ["a", "b"], "which": "b"})
        assert info.value.nodes == ["second"]
        assert "second: The table has no column 'c'." in str(info.value)
        assert dashboard_to_dict(editor) == before

    def test_warnings_do_not_refuse(self, editor):
        problems = editor.check_parameters({"cols": []})
        assert [p.warning for p in problems["cols"]] == [False, False, True]
        editor.declare_parameter("cols", "data", "columns")
        assert editor.parameters["cols"].expects == {"columns": ["a", "c"]}

    def test_a_contract_that_raises_refuses_the_value(self, editor, monkeypatch):
        def broken(self, context, expects):
            raise RuntimeError("no connection")

        monkeypatch.setattr(Columns, "check", broken)
        with pytest.raises(ContractError, match="Could not check the value: no connection"):
            editor.bind_parameters({"cols": ["a", "c"]})


class TestDeclaring:
    def test_declare_and_remove(self, editor):
        parameter = editor.declare_parameter("lim2", "second", "limit", "integer", "Rows")
        assert parameter == DashboardParameter("second", "limit", "integer", "Rows")
        assert dashboard_to_dict(editor)["parameters"]["lim2"]["description"] == "Rows"
        assert editor.remove_parameter("lim2")
        assert not editor.remove_parameter("lim2")
        assert "lim2" not in editor.parameters

    @pytest.mark.parametrize(
        "args, match",
        [
            (("x", "nowhere", "limit"), "names no node"),
            (("x", "second", "rows"), "Pick has no config field 'rows'"),
            (("x", "second", "limit", "matrix"), "must be one of"),
            (("1x", "second", "limit"), "parameter names start with"),
        ],
    )
    def test_declare_refuses(self, editor, args, match):
        with pytest.raises(ParameterError, match=match):
            editor.declare_parameter(*args)

    def test_a_replace_cannot_set_what_a_contract_expects(self, editor):
        ops = DashboardOps(editor)
        doc = yaml.safe_load(ops.get_dashboard().split("\n", 1)[1])
        doc["parameters"]["cols"]["expects"] = {"columns": []}
        doc["parameters"]["second"] = {"node": "second", "field": "column", "type": "string"}
        ops.replace_yaml(yaml.safe_dump(doc), revision=ops.revision())
        assert editor.parameters["cols"].expects == {"columns": ["a", "c"]}
        assert editor.parameters["second"].expects is None

    def test_a_replace_that_moves_a_parameter_computes_its_contract_again(self, editor):
        ops = DashboardOps(editor)
        doc = yaml.safe_load(ops.get_dashboard().split("\n", 1)[1])
        doc["nodes"]["second"]["config"]["column"] = "b"
        doc["parameters"]["cols"]["expects"] = {"columns": ["z"]}
        doc["parameters"]["cols"]["field"] = "columns"
        ops.replace_yaml(yaml.safe_dump(doc), revision=ops.revision())
        assert editor.parameters["cols"].expects == {"columns": ["a", "c"]}
        editor.save()
        assert editor.parameters["cols"].expects == {"columns": ["a", "b"]}


class TestOps:
    def test_ops_declare_bind_and_list(self, editor):
        ops = DashboardOps(editor)
        ops.declare_parameter("lim2", "second", "limit", type="integer")
        assert ops.parameters()["lim2"] == {"node": "second", "field": "limit", "type": "integer"}
        ops.bind_parameters({"lim2": "5"})
        assert ops.node_config("second")["limit"] == 5
        ops.remove_parameter("lim2")
        assert "lim2" not in ops.parameters()

    def test_ops_errors_are_operation_errors(self, editor):
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match="second: The table has no column 'c'"):
            ops.bind_parameters({"cols": ["a"]})
        with pytest.raises(OperationError, match="No node 'nowhere'"):
            ops.declare_parameter("x", "nowhere", "limit")
        with pytest.raises(OperationError, match="Pick has no config field 'rows'"):
            ops.declare_parameter("x", "second", "rows")
        with pytest.raises(OperationError, match="'x' is not a parameter"):
            ops.remove_parameter("x")

    def test_ops_bind_runs_the_config_check(self, editor, monkeypatch):
        from panel_flowdash.agent import ops as ops_module

        monkeypatch.setattr(ops_module, "check_node_sync", lambda *a, **k: ["no"])
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match="no"):
            ops.bind_parameters({"which": "b"})
        assert ops.node_config("first")["column"] == "a"

    def test_read_only_editors_refuse(self, editor):
        editor.read_only = True
        with pytest.raises(PermissionError):
            DashboardOps(editor).bind_parameters({"which": "b"})


class TestSchema:
    def test_schema_types_and_defaults(self):
        parameters = decode_parameters(yaml.safe_load(DOC)["parameters"])
        schema = parameters_schema(parameters, {"which": "a", "limit": 2})
        assert schema["properties"]["which"] == {
            "title": "which",
            "type": "string",
            "default": "a",
        }
        assert schema["properties"]["limit"]["type"] == "integer"
        assert schema["properties"]["cols"] == {
            "title": "cols",
            "description": "The table's columns",
        }


class TestStores:
    def _model(self, store):
        model = store.create_dashboard("alice", "Picks")
        model.parameters = {"p": DashboardParameter("x", "value", "number", "P", {"k": [1]})}
        store.save_dashboard(model)
        return model

    def test_sqlite(self, tmp_path):
        store = DashboardStore(str(tmp_path / "d.db"))
        model = self._model(store)
        assert store.load_dashboard("alice", model.dashboard_id).parameters == model.parameters

    def test_sqlite_drops_malformed_parameters(self, tmp_path, caplog):
        path = tmp_path / "d.db"
        store = DashboardStore(str(path))
        model = self._model(store)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('UPDATE dashboards SET parameters_json = \'{"p": {"node": 1}}\'')
        loaded = store.load_dashboard("alice", model.dashboard_id)
        assert loaded.parameters == {}
        assert "Ignoring malformed parameters" in caplog.text

    def test_json_folder(self, tmp_path, caplog):
        store = JSONFolderDashboardStore(tmp_path)
        model = self._model(store)
        assert store.load_dashboard("alice", model.dashboard_id).parameters == model.parameters
        path = next(tmp_path.glob("*.json"))
        data = json.loads(path.read_text())
        data["parameters"] = {"p": {"node": "x", "field": "value", "type": "matrix"}}
        path.write_text(json.dumps(data))
        assert store.load_dashboard("alice", model.dashboard_id).parameters == {}
        assert "Ignoring malformed parameters" in caplog.text

    def test_memory_round_trip(self):
        store = MemoryDashboardStore()
        model = self._model(store)
        again = DashboardModel.from_dict(model.to_dict())
        assert again.parameters == model.parameters


class TestValidation:
    def test_a_parameter_on_a_missing_config_field_is_an_error(self, editor):
        ops = DashboardOps(editor)
        doc = yaml.safe_load(DOC)
        doc["parameters"]["which"]["field"] = "columns_"
        issues = ops.validate(yaml.safe_dump(doc))
        assert [(i.path, i.severity) for i in issues if i.path.startswith("parameters")] == [
            ("parameters.which.field", "error")
        ]
        assert "Did you mean 'column'" in str(issues[0])
