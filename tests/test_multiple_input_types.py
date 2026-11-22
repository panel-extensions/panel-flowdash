"""A connection into a list input is checked against the type of the items it collects."""

import json

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash, register, register_port_type
from panel_flowdash.agent import DashboardOps, OperationError, validate_dashboard
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.dataflow_engine import DataflowGraph, connection_type
from panel_flowdash.port_types import _ALIASES, _SUPERTYPES
from panel_flowdash.registry import RegistryEntry


@register(page=False, component=True, title="Text", provides=[{"key": "text", "type": "str"}])
def text(config):
    config.text = "a"
    return "text"


@register(page=False, component=True, title="Count", provides=[{"key": "count", "type": "int"}])
def count(config):
    config.count = 1
    return "count"


@register(page=False, component=True, title="Anything", provides=["value"])
def anything(config):
    config.value = 3
    return "anything"


@register(
    page=False,
    component=True,
    title="Join",
    requires=[
        {"key": "texts", "type": "str", "multiple": True, "required": False},
        {"key": "things", "type": "object", "multiple": True, "required": False},
        {"key": "items", "type": "List", "required": False},
        {"key": "loose", "multiple": True, "required": False},
    ],
)
def join(config):
    return "join"


COMPONENTS = {"T/text": text, "T/count": count, "T/anything": anything, "T/join": join}


def _editor(**params):
    return FlowDash(COMPONENTS, include_builtin_components=False, notifications=False, **params)


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Join")
    for name, component in [("t", "T/text"), ("c", "T/count"), ("a", "T/anything")]:
        editor.add_component(component, instance_id=name)
    editor.add_component("T/join", instance_id="j")
    yield editor
    editor.dispose()


class TestConnect:
    async def test_typed_multiple_input_takes_matching_outputs(self, editor):
        assert editor.connect("t", "text", "j", "texts") is True
        assert editor._validate_connection("c", "count", "j", "texts") is not None

    async def test_typed_multiple_input_refuses_other_types(self, editor):
        result = editor.connect("c", "count", "j", "texts")
        assert result == (
            "Type mismatch: output 'count' produces 'int' but input 'texts' expects 'str'."
        )
        assert editor.graph.edges == []

    async def test_refusal_does_not_depend_on_other_edges(self, editor):
        assert editor.connect("t", "text", "j", "texts") is True
        assert "Type mismatch" in editor.connect("c", "count", "j", "texts")
        assert [e["source"] for e in editor.graph.edges] == ["t"]

    @pytest.mark.parametrize("port", ["things", "items", "loose"])
    async def test_untyped_and_object_items_take_anything(self, editor, port):
        assert editor.connect("t", "text", "j", port) is True
        assert editor.connect("c", "count", "j", port) is True
        assert editor.connect("a", "value", "j", port) is True

    async def test_untyped_output_is_checked_by_value(self):
        class Bag(Viewer):
            values = param.List(item_type=str, default=[])

            def __panel__(self):
                return ""

        specs = {
            "t/any": build_component_spec(RegistryEntry.from_app(anything, app_id="t/any")),
            "t/bag": build_component_spec(RegistryEntry.from_app(Bag, app_id="t/bag")),
            "t/text": build_component_spec(RegistryEntry.from_app(text, app_id="t/text")),
        }
        assert connection_type(specs["t/bag"].inputs[0]) == "str"
        graph = DataflowGraph(specs)
        for node, component in [("a", "t/any"), ("b", "t/bag"), ("t", "t/text")]:
            graph.add_node(node, component)
        graph._nodes["a"].value = 3
        assert graph.add_edge("t", "text", "b", "values") is True
        assert graph.validate_connection("a", "value", "b", "values") == (
            "Input 'values' rejects the current value of 'value': its items must be str, not int."
        )


class TestAgent:
    async def test_connect_tool_reports_the_mismatch(self, editor):
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match="Type mismatch: output 'count' produces 'int'"):
            ops.connect("c.count", "j.texts")
        ops.connect("t.text", "j.texts")

    async def test_yaml_validation_checks_item_types(self, editor):
        text = (
            "flowdash: 1\nnodes:\n"
            "  t: {component: T/text}\n  c: {component: T/count}\n  j: {component: T/join}\n"
            "edges:\n"
            "  - {from: t.text, to: j.texts}\n"
            "  - {from: c.count, to: j.texts}\n"
            "  - {from: c.count, to: j.items}\n"
        )
        issues = validate_dashboard(text, editor.component_specs)
        errors = [str(i) for i in issues if i.severity == "error"]
        assert len(errors) == 1
        assert errors[0].startswith(
            "edges[1]: Type mismatch: output 'count' of 'c' produces 'int' but input "
            "'texts' of 'j' expects 'str'."
        )


class TestLoad:
    def _model(self):
        return DashboardModel(
            dashboard_id="d",
            user_id="alice",
            title="Saved before the check",
            items=[
                DashboardItem(instance_id="t", component_id="T/text"),
                DashboardItem(instance_id="c", component_id="T/count"),
                DashboardItem(instance_id="j", component_id="T/join"),
            ],
            edges=[
                DashboardEdge(source="t", source_port="text", target="j", target_port="texts"),
                DashboardEdge(source="c", source_port="count", target="j", target_port="texts"),
            ],
        )

    async def test_saved_invalid_connection_is_kept_and_listed(self):
        editor = _editor()
        model = self._model()
        editor.load_model(model)
        assert [(e["source"], e["target"]) for e in editor.graph.edges] == [("t", "j")]
        (unresolved,) = editor._unresolved_edges
        assert unresolved.edge.source == "c"
        assert "Type mismatch" in unresolved.reason
        assert "Type mismatch" in editor._flow.bottom_panel[-1][0].objects[0].object
        key = lambda e: json.dumps(e.to_dict(), sort_keys=True)
        assert sorted(map(key, editor.to_model().edges)) == sorted(map(key, model.edges))
        editor.dispose()


class Base:
    pass


class Sub(Base):
    pass


class TestSubclassItems:
    @pytest.fixture
    def restore_type_registry(self):
        aliases, supertypes = dict(_ALIASES), {k: set(v) for k, v in _SUPERTYPES.items()}
        yield
        _ALIASES.clear()
        _ALIASES.update(aliases)
        _SUPERTYPES.clear()
        _SUPERTYPES.update(supertypes)

    def _graph(self, target):
        class Maker(Viewer):
            @param.output(Sub)
            def made(self):
                return Sub()

            def __panel__(self):
                return ""

        specs = {
            "t/maker": build_component_spec(RegistryEntry.from_app(Maker, app_id="t/maker")),
            "t/target": build_component_spec(RegistryEntry.from_app(target, app_id="t/target")),
        }
        graph = DataflowGraph(specs)
        graph.add_node("m", "t/maker")
        graph.add_node("x", "t/target")
        return graph

    async def test_subclass_output_feeds_a_list_of_its_base(self):
        class Bag(Viewer):
            values = param.List(item_type=Base, default=[])

            def __panel__(self):
                return ""

        assert (
            connection_type(
                build_component_spec(RegistryEntry.from_app(Bag, app_id="t/bag")).inputs[0]
            )
            is None
        )
        graph = self._graph(Bag)
        assert graph.add_edge("m", "made", "x", "values") is True

    async def test_subclass_output_feeds_a_multiple_input_of_its_base(self, restore_type_registry):
        register_port_type("Sub", subtype_of=["Base"])

        @register(
            page=False,
            component=True,
            requires=[{"key": "bases", "type": "Base", "multiple": True, "required": False}],
        )
        def bases(config):
            return "bases"

        graph = self._graph(bases)
        assert graph.add_edge("m", "made", "x", "bases") is True


async def test_mismatch_behind_a_transform_is_refused(editor):
    """A step that cannot take the output's kind of value does not slip a mismatch through."""
    steps = [{"op": "column", "column": "x"}]
    assert "Type mismatch" in editor.connect("c", "count", "j", "texts")
    assert editor.connect("c", "count", "j", "texts", steps) == (
        "Transform fails on the current value of 'count': Column cannot be applied to a int."
    )
    assert editor.graph.edges == []
