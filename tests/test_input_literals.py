"""Literal values for unconnected inputs: engine, editor, file formats, agent and inspector."""

import dataclasses

import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
    ComponentSpec,
    DataflowGraph,
    FlowDash,
    InputPort,
    MemoryDashboardStore,
    OutputPort,
    register,
)
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.agent.schema import dashboard_schema, inputs_schema
from panel_flowdash.agent.validation import validate_dashboard
from panel_flowdash.agent.yaml_format import dashboard_from_yaml, dashboard_to_yaml
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import export_model
from panel_flowdash.input_literals import check_literal, literal_parameter, takes_literal
from panel_flowdash.ui.choice_widgets import ChoiceSelect
from panel_flowdash.ui.input_editor import LiteralEditor
from panel_flowdash.ui.status_bar import select_on_canvas


@register(page=False, component=True, title="Num", provides=[{"key": "value", "type": "Number"}])
class Num(Viewer):
    v = param.Number(default=5)

    @param.output(param.Number)
    @param.depends("v")
    def value(self):
        return self.v

    def __panel__(self):
        return "num"


def state_choices(config, inputs):
    return {"state": ["TX", "CA"]}


@register(page=False, component=True, title="Show", config_choices=state_choices)
class Show(Viewer):
    value = param.Number(default=None, allow_None=True, bounds=(0, 100))
    span = param.Range(default=(0, 1))
    state = param.String(default="")
    rows = param.DataFrame(default=None, allow_None=True)
    title = param.String(default="", precedence=-1)

    def __panel__(self):
        return "show"


@register(
    page=False,
    component=True,
    title="Count",
    requires=[
        {"key": "limit", "type": "int", "required": True},
        {"key": "items", "type": "list", "required": False},
    ],
)
def count(config):
    return "count"


COMPONENTS = {"D/num": Num, "D/show": Show, "D/count": count}


def _editor(**params):
    params.setdefault("history_coalesce", 0)
    return FlowDash(COMPONENTS, notifications=False, include_builtin_components=False, **params)


@pytest.fixture
def editor():
    editor = _editor(store=MemoryDashboardStore())
    editor.new_dashboard("Mine")
    yield editor
    editor.dispose()


def _placed(editor):
    return editor.add_component("D/num", instance_id="num"), editor.add_component(
        "D/show", instance_id="show"
    )


def _spec(inputs):
    return ComponentSpec(
        component_id="t",
        title="t",
        description=None,
        icon=None,
        tags=[],
        outputs=[],
        inputs=inputs,
        default_size=None,
    )


class TestLiteralTypes:
    @pytest.mark.parametrize(
        ("port", "takes"),
        [
            (InputPort("v", type="Number"), True),
            (InputPort("v", type="int"), True),
            (InputPort("v", type="str"), True),
            (InputPort("v", type="bool"), True),
            (InputPort("v", type="list"), True),
            (InputPort("v", type="tuple"), True),
            (InputPort("v"), True),
            (InputPort("v", type="DataFrame"), False),
            (InputPort("v", type="date"), False),
            (InputPort("v", type="callable"), False),
        ],
    )
    def test_only_values_a_file_holds_are_literals(self, port, takes):
        assert takes_literal(port) is takes

    def test_a_viewer_input_is_edited_with_a_copy_of_its_parameter(self):
        port = next(p for p in _show_spec().inputs if p.name == "span")
        copied = literal_parameter(port)
        assert isinstance(copied, param.Range) and copied is not Show.param.span
        assert copied.allow_None
        assert Show.param.span.allow_None is False

    def test_object_parameters_take_no_literal(self):
        ports = {p.name: p for p in _show_spec().inputs}
        assert not takes_literal(ports["rows"])

    def test_values_are_checked_against_the_input(self):
        ports = {p.name: p for p in _show_spec().inputs}
        assert check_literal(ports["value"], 50) is None
        assert "at most 100" in check_literal(ports["value"], 500)
        assert check_literal(ports["span"], [2, 3]) is None
        assert "Number parameter" in check_literal(InputPort("v", type="number"), "x")
        assert "cannot hold a value" in check_literal(InputPort("v", type="DataFrame"), 1)
        assert "not object" in check_literal(InputPort("v"), object())
        assert "takes a list" in check_literal(InputPort("v", type="tuple"), 3)


def _show_spec():
    from panel_flowdash.component_spec import build_component_spec
    from panel_flowdash.registry import RegistryEntry

    return build_component_spec(RegistryEntry.from_app(Show, app_id="D/show"))


class TestEngine:
    def setup_method(self):
        source = dataclasses.replace(
            _spec([]), component_id="src", outputs=[OutputPort("out", "int")]
        )
        target = dataclasses.replace(
            _spec([InputPort("value", type="int", default=1), InputPort("items", type="list")]),
            component_id="dst",
        )
        self.graph = DataflowGraph({"src": source, "dst": target})
        self.src = self.graph.add_node("a", "src")
        self.dst = self.graph.add_node("b", "dst")

    def test_a_literal_applies_while_the_input_is_unwired(self):
        self.graph.set_literal("b", "value", 7)
        assert self.dst.value == 7
        self.src.out = 3
        assert self.graph.add_edge("a", "out", "b", "value") is True
        assert self.dst.value == 3, "the connection wins"
        self.graph.set_literal("b", "value", 9)
        assert self.dst.value == 3, "a literal set on a wired input waits"
        self.graph.remove_edge("a", "out", "b", "value")
        assert self.dst.value == 9, "removing the connection brings the literal back"
        self.graph.clear_literal("b", "value")
        assert self.dst.value == 1, "clearing it goes back to the default"

    def test_removing_the_source_node_brings_the_literal_back(self):
        self.graph.set_literal("b", "value", 7)
        self.src.out = 3
        self.graph.add_edge("a", "out", "b", "value")
        self.graph.remove_node("a")
        assert self.dst.value == 7

    def test_a_list_input_takes_a_literal_list(self):
        self.graph.set_literal("b", "items", [1, 2])
        assert self.dst.items == [1, 2]
        self.src.out = 3
        self.graph.add_edge("a", "out", "b", "items")
        assert self.dst.items == [3]
        self.graph.remove_edge("a", "out", "b", "items")
        assert self.dst.items == [1, 2]
        self.graph.clear_literal("b", "items")
        assert self.dst.items == []

    def test_a_refused_literal_changes_nothing(self):
        with pytest.raises(ValueError, match="has no input"):
            self.graph.set_literal("b", "nope", 1)
        with pytest.raises(ValueError, match="Integer"):
            self.graph.set_literal("b", "value", "x")
        assert self.graph.literals("b") == {}
        assert self.dst.value == 1

    def test_removing_the_node_forgets_its_literals(self):
        self.graph.set_literal("b", "value", 7)
        self.graph.remove_node("b")
        assert self.graph.literals("b") == {}


class TestEditor:
    def test_a_literal_reaches_the_component_and_the_edge_wins(self, editor):
        num, show = _placed(editor)
        component = editor._component_instances[show]
        editor.set_input_value(show, "value", 42)
        assert component.value == 42
        editor.connect(num, "value", show, "value")
        assert component.value == 5
        editor.disconnect(num, "value", show, "value")
        assert component.value == 42
        assert editor.input_values(show) == {"value": 42}

    def test_none_removes_the_literal(self, editor):
        _, show = _placed(editor)
        editor.set_input_value(show, "value", 42)
        editor.set_input_value(show, "value", None)
        assert editor.input_values(show) == {}
        assert editor.graph.get_state(show).value is None

    def test_refused_values_change_nothing(self, editor):
        _, show = _placed(editor)
        with pytest.raises(ValueError, match="at most 100"):
            editor.set_input_value(show, "value", 500)
        with pytest.raises(ValueError, match="has no input"):
            editor.set_input_value(show, "nope", 1)
        with pytest.raises(ValueError, match="cannot hold a value"):
            editor.set_input_value(show, "rows", 1)
        assert editor.input_values(show) == {}

    def test_each_change_is_one_version_and_undo_restores_it(self, editor):
        _, show = _placed(editor)
        editor.set_input_value(show, "value", 42)
        editor.set_input_value(show, "span", (2, 3))
        versions = editor.edit_history.versions
        assert [v.metadata["op"] for v in versions[-2:]] == ["set_input", "set_input"]
        assert versions[-1].metadata["label"] == "Set Span of show"
        state = editor.graph.get_state(show)
        assert (
            editor.undo() and state.span == (0, 1) and editor.input_values(show) == {"value": 42}
        )
        assert editor.undo() and state.value is None and editor.input_values(show) == {}
        assert editor.redo() and editor.redo()
        assert state.value == 42 and state.span == (2, 3)

    def test_the_model_stores_inputs_and_reloads_them(self, editor):
        _, show = _placed(editor)
        editor.set_input_value(show, "span", (2, 3))
        model = editor.to_model()
        item = next(i for i in model.items if i.instance_id == show)
        assert item.inputs == {"span": [2, 3]}
        assert item.to_dict()["inputs"] == {"span": [2, 3]}
        restored = DashboardModel.from_dict(model.to_dict())
        other = _editor()
        other.load_model(restored)
        assert other.graph.get_state(show).span == (2, 3)
        assert other.input_values(show) == {"span": [2, 3]}

    def test_an_item_without_inputs_writes_no_key(self):
        assert "inputs" not in DashboardItem("a", "D/show").to_dict()

    def test_malformed_stored_inputs_are_refused(self):
        with pytest.raises(TypeError):
            DashboardItem.from_dict({"instance_id": "a", "component_id": "D/show", "inputs": [1]})

    def test_a_stored_literal_the_input_refuses_is_kept_for_the_next_save(self):
        model = DashboardModel("d", "alice", "D")
        model.items = [DashboardItem("show", "D/show", inputs={"value": 500, "gone": 1})]
        editor = _editor()
        editor.load_model(model)
        assert editor.graph.get_state("show").value is None
        assert editor.to_model().items[0].inputs == {"value": 500, "gone": 1}

    def test_a_restricted_export_leaves_inputs_out(self, editor):
        _, show = _placed(editor)
        editor.set_input_value(show, "value", 4)
        data = export_model(editor.to_model(), restricted={show})
        item = next(i for i in data["items"] if i["instance_id"] == show)
        assert "inputs" not in item


class TestFile:
    def test_the_yaml_round_trips_inputs(self, editor):
        _, show = _placed(editor)
        editor.set_input_value(show, "value", 4)
        text = dashboard_to_yaml(editor)
        assert "inputs:\n      value: 4" in text
        data = dashboard_from_yaml(text)
        (item,) = [i for i in data["items"] if i["component_id"] == "D/show"]
        assert item["inputs"] == {"value": 4}

    def test_validation_checks_inputs(self, editor):
        text = (
            "flowdash: 4\nnodes:\n"
            "  show: {component: D/show, inputs: {value: 500, nope: 1, rows: 1}}\n"
            "  count: {component: D/count, inputs: {limit: 3}}\n"
        )
        issues = validate_dashboard(text, editor.component_specs)
        errors = {i.path: i.message for i in issues if i.severity == "error"}
        assert "at most 100" in errors["nodes.show.inputs.value"]
        assert "has no input 'nope'" in errors["nodes.show.inputs.nope"]
        assert "cannot hold a value" in errors["nodes.show.inputs.rows"]
        assert not any("limit" in i.message for i in issues), (
            "a required input with a literal is not reported as missing"
        )

    def test_the_schema_types_each_literal(self, editor):
        spec = editor.component_specs["D/show"]
        schema = inputs_schema(spec)
        assert schema["properties"]["value"]["type"] == "number"
        assert schema["properties"]["span"]["type"] == "array"
        assert "rows" not in schema["properties"]
        node = dashboard_schema(editor.component_specs)["$defs"]["node"]["oneOf"]
        assert any("inputs" in variant["properties"] for variant in node)


class TestOps:
    @pytest.fixture
    def ops(self, editor):
        return DashboardOps(editor)

    def test_the_agent_sets_and_removes_literals(self, ops):
        ops.editor.add_component("D/show", instance_id="show")
        ops.set_inputs("show", {"value": 12, "span": [1, 2]})
        assert ops.editor.input_values("show") == {"value": 12, "span": [1, 2]}
        assert "value: 12" in ops.get_dashboard()
        ops.set_inputs("show", {"value": None})
        assert ops.editor.input_values("show") == {"span": [1, 2]}

    def test_a_refused_value_changes_nothing(self, ops):
        ops.editor.add_component("D/show", instance_id="show")
        with pytest.raises(OperationError, match="at most 100"):
            ops.set_inputs("show", {"span": [1, 2], "value": 500})
        with pytest.raises(OperationError, match=r"has no input 'vale'\. Did you mean"):
            ops.set_inputs("show", {"vale": 1})
        assert ops.editor.input_values("show") == {}


def _inspect(editor, node_id):
    select_on_canvas(editor._flow, node_id=node_id)
    return editor._inspector


def _literal_editor(inspector, port):
    return inspector._literal_editors[port]


class TestInspector:
    def test_an_unwired_input_gets_a_typed_editor(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        literal = _literal_editor(inspector, "value")
        assert isinstance(literal, LiteralEditor)
        assert "rows" not in inspector._literal_editors
        literal._holder.value = 30
        assert editor.input_values(show) == {"value": 30}
        assert editor.graph.get_state(show).value == 30
        assert literal.clear_button.visible
        assert inspector._literal_captions["value"].object.startswith("Set here")

    def test_typing_a_value_does_not_rebuild_the_row(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        literal = _literal_editor(inspector, "value")
        literal._holder.value = 30
        literal._holder.value = 31
        assert _literal_editor(inspector, "value") is literal

    def test_a_refused_value_is_shown_under_the_field(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        literal = _literal_editor(inspector, "state")

        def refuse(value):
            raise ValueError("No such state")

        literal._on_change = refuse
        literal._holder.value = "XX"
        assert literal._error.visible and literal._error.object == "No such state"

    def test_clearing_goes_back_to_the_default(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        literal = _literal_editor(inspector, "span")
        literal._holder.value = (3, 4)
        literal.clear_button.clicks += 1
        assert editor.input_values(show) == {}
        assert literal._holder.value == (0, 1)
        assert not literal.clear_button.visible

    def test_undo_updates_the_field(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        literal = _literal_editor(inspector, "value")
        literal._holder.value = 30
        editor.undo()
        literal = _literal_editor(inspector, "value")
        assert literal._holder.value is None
        assert not literal.clear_button.visible

    def test_a_wired_input_says_its_literal_waits(self, editor):
        num, show = _placed(editor)
        editor.set_input_value(show, "value", 30)
        editor.connect(num, "value", show, "value")
        inspector = _inspect(editor, show)
        assert "value" not in inspector._literal_editors
        texts = [
            str(c.object)
            for c in inspector._inputs_column.select(pmui.Typography)
            if "applies once disconnected" in str(c.object)
        ]
        assert texts and "30" in texts[0]

    async def test_choices_for_an_input_fill_its_dropdown(self, editor):
        _, show = _placed(editor)
        inspector = _inspect(editor, show)
        from .test_config_choices import _settle

        await _settle(0.5)
        literal = _literal_editor(inspector, "state")
        widgets = list(literal._form.select(ChoiceSelect))
        assert widgets and set(widgets[0].options.values()) >= {"TX", "CA"}
