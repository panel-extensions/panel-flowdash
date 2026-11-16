"""Tests for port type normalization, subtype rules and component/port aliases."""

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
    ComponentSpec,
    DataflowGraph,
    InputPort,
    OutputPort,
    normalize_port_type,
    port_types_compatible,
    register,
    register_port_type,
)
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.port_types import _ALIASES, _SUPERTYPES
from panel_flowdash.registry import RegistryEntry, build_registry


@pytest.fixture
def restore_type_registry():
    aliases, supertypes = dict(_ALIASES), {k: set(v) for k, v in _SUPERTYPES.items()}
    yield
    _ALIASES.clear()
    _ALIASES.update(aliases)
    _SUPERTYPES.clear()
    _SUPERTYPES.update(supertypes)


def make_spec(component_id, inputs=None, outputs=None):
    return ComponentSpec(
        component_id=component_id,
        title=component_id,
        description=None,
        icon=None,
        tags=[],
        outputs=outputs or [],
        inputs=inputs or [],
        default_size=None,
    )


def _connect(source_type, target_type):
    graph = DataflowGraph(
        {
            "src": make_spec("src", outputs=[OutputPort(name="out", type=source_type)]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="inp", type=target_type)]),
        }
    )
    graph.add_node("s", "src")
    graph.add_node("t", "tgt")
    return graph.add_edge("s", "out", "t", "inp")


class TestNormalization:
    @pytest.mark.parametrize(
        ("spellings", "canonical"),
        [
            (["str", "String", "STRING", "Color", "Filename"], "str"),
            (["int", "Integer"], "int"),
            (["float", "Number", "Magnitude"], "number"),
            (["bool", "Boolean", "Event"], "bool"),
            (["list", "List", "ListSelector"], "list"),
            (["dict", "Dict"], "dict"),
            (["date", "Date", "CalendarDate", "datetime"], "date"),
            (["DataFrame", "pd.DataFrame", "pandas.DataFrame"], "dataframe"),
            (["Series", "pd.Series", "pandas.Series"], "series"),
            (["object", "Any", "Parameter", "Selector", "ClassSelector"], "any"),
        ],
    )
    async def test_spellings_normalize_to_one_name(self, spellings, canonical):
        assert {normalize_port_type(s) for s in spellings} == {canonical}

    async def test_untyped_is_none(self):
        assert normalize_port_type(None) is None
        assert normalize_port_type("  ") is None

    async def test_unknown_type_normalizes_to_itself(self):
        assert normalize_port_type("Customer") == "customer"


class TestCompatibility:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("String", "str"),
            ("str", "String"),
            ("Integer", "int"),
            ("Integer", "Number"),
            ("int", "float"),
            ("Number", "float"),
            ("List", "list"),
            ("Dict", "dict"),
            ("Boolean", "bool"),
            ("pd.DataFrame", "DataFrame"),
            ("DataFrame", "pandas.DataFrame"),
            ("Customer", "customer"),
            ("str", "Selector"),
            ("int", "object"),
            ("object", "int"),
            (None, "int"),
            ("int", None),
        ],
    )
    async def test_compatible(self, source, target):
        assert port_types_compatible(source, target)
        assert _connect(source, target) is True

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("int", "str"),
            ("Number", "Integer"),
            ("float", "int"),
            ("Boolean", "Integer"),
            ("Boolean", "Number"),
            ("DataFrame", "Series"),
            ("Customer", "Person"),
        ],
    )
    async def test_incompatible(self, source, target):
        assert not port_types_compatible(source, target)
        result = _connect(source, target)
        assert isinstance(result, str) and "Type mismatch" in result

    async def test_viewer_output_connects_to_declared_input(self):
        """Introspected Param names and @register Python names interoperate."""

        class Counter(Viewer):
            name_out = param.String(default="a", metadata={"flowdash": {"output": True}})
            count = param.Integer(default=1, metadata={"flowdash": {"output": True}})

            def __panel__(self):
                return ""

        @register(
            page=False,
            component=True,
            requires=[{"key": "label", "type": "str"}, {"key": "amount", "type": "float"}],
        )
        def show(config):
            return ""

        specs = {
            "counter": build_component_spec(RegistryEntry.from_app(Counter, app_id="t/counter")),
            "show": build_component_spec(RegistryEntry.from_app(show, app_id="t/show")),
        }
        graph = DataflowGraph(specs)
        graph.add_node("c", "counter")
        graph.add_node("s", "show")
        assert graph.add_edge("c", "name_out", "s", "label") is True
        assert graph.add_edge("c", "count", "s", "amount") is True

    async def test_runtime_check_still_rejects_bad_value(self):
        """Statically compatible ports still have each value checked by the input's Parameter."""
        graph = DataflowGraph(
            {
                "src": make_spec("src", outputs=[OutputPort(name="out", type="Selector")]),
                "tgt": make_spec(
                    "tgt",
                    inputs=[
                        InputPort(name="inp", type="Integer", parameter=param.Integer(default=0))
                    ],
                ),
            }
        )
        source = graph.add_node("s", "src")
        source.out = "text"
        graph.add_node("t", "tgt")
        result = graph.add_edge("s", "out", "t", "inp")
        assert isinstance(result, str) and "rejects" in result


class TestRegisterPortType:
    async def test_custom_aliases_and_subtypes(self, restore_type_registry):
        register_port_type("Person")
        register_port_type("Customer", aliases=["crm.Customer"], subtype_of=["Person"])
        register_port_type("VIP", subtype_of=["Customer"])
        assert port_types_compatible("crm.Customer", "Customer")
        assert port_types_compatible("Customer", "Person")
        assert port_types_compatible("VIP", "Person")
        assert not port_types_compatible("Person", "Customer")
        assert _connect("VIP", "person") is True

    async def test_alias_of_another_type_is_refused(self, restore_type_registry):
        with pytest.raises(ValueError, match="already names"):
            register_port_type("Label", aliases=["String"])

    async def test_any_cannot_be_a_subtype(self, restore_type_registry):
        with pytest.raises(ValueError, match="any"):
            register_port_type("object", subtype_of=["str"])

    async def test_extending_a_builtin(self, restore_type_registry):
        register_port_type("Ticker", subtype_of=["str"])
        assert port_types_compatible("Ticker", "String")
        assert not port_types_compatible("String", "Ticker")


# Component and port aliases ------------------------------------------------


@register(
    page=False,
    component=True,
    provides=[{"key": "symbol", "type": "str"}],
    aliases=["Old/ticker"],
)
def ticker(config):
    return "ticker"


@register(
    page=False,
    component=True,
    requires=[{"key": "symbol", "type": "str"}],
    port_aliases={"ticker": "symbol"},
)
def chart(config):
    return "chart"


class Report(Viewer):
    title = param.String(default="", metadata={"flowdash": {"aliases": ["heading"]}})
    symbol = param.String(default="", metadata={"flowdash": {"aliases": ["ticker"]}})

    def __panel__(self):
        return self.title


register(page=False, component=True, aliases=["Old/report"], config=["title"])(Report)


def _editor():
    return FlowDash(
        {"New/ticker": ticker, "New/chart": chart, "New/report": Report},
        notifications=False,
        include_builtin_components=False,
    )


def _old_model():
    return DashboardModel(
        dashboard_id="d",
        user_id="u",
        title="Old names",
        items=[
            DashboardItem(instance_id="t1", component_id="Old/ticker"),
            DashboardItem(instance_id="c1", component_id="New/chart"),
            DashboardItem(
                instance_id="r1", component_id="Old/report", config={"heading": "Prices"}
            ),
        ],
        edges=[
            DashboardEdge(source="t1", source_port="symbol", target="c1", target_port="ticker"),
            DashboardEdge(source="t1", source_port="symbol", target="r1", target_port="ticker"),
        ],
    )


class TestAliases:
    async def test_saved_dashboard_with_old_names_loads(self):
        editor = _editor()
        editor.load_model(_old_model())

        saved = editor.to_model()
        assert [(i.instance_id, i.component_id) for i in saved.items] == [
            ("t1", "New/ticker"),
            ("c1", "New/chart"),
            ("r1", "New/report"),
        ]
        assert [(e.source_port, e.target, e.target_port) for e in saved.edges] == [
            ("symbol", "c1", "symbol"),
            ("symbol", "r1", "symbol"),
        ]
        assert saved.items[2].config == {"title": "Prices"}

    async def test_alias_never_shadows_a_registered_id(self):
        @register(page=False, component=True, aliases=["New/chart"])
        def other(config):
            return "other"

        editor = FlowDash(
            {"New/chart": chart, "New/other": other},
            notifications=False,
            include_builtin_components=False,
        )
        assert editor._resolve_component_id("New/chart") == "New/chart"

    async def test_live_port_name_wins_over_alias(self):
        spec = build_component_spec(
            RegistryEntry.from_app(
                register(
                    page=False,
                    component=True,
                    requires=["a", "b"],
                    port_aliases={"a": "b"},
                )(lambda config: ""),
                app_id="t/x",
            )
        )
        assert spec.resolve_name("a") == "a"

    async def test_load_imports_component_by_alias(self, tmp_path):
        section = tmp_path / "Renamed"
        section.mkdir()
        (section / "picker.py").write_text(
            "from panel_flowdash import register\n"
            "@register(page=False, component=True, aliases=['Legacy/picker'],\n"
            "          provides=[{'key': 'value', 'type': 'str'}])\n"
            "def app(config):\n"
            "    return 'picker'\n"
        )
        registry = build_registry(tmp_path)
        assert registry["Renamed/picker"].metadata.aliases == ["Legacy/picker"]
        editor = FlowDash(tmp_path, notifications=False, include_builtin_components=False)
        editor.load_model(
            DashboardModel(
                dashboard_id="d",
                user_id="u",
                title="t",
                items=[DashboardItem(instance_id="p1", component_id="Legacy/picker")],
            )
        )
        assert [i.component_id for i in editor.to_model().items] == ["Renamed/picker"]

    async def test_malformed_scanned_port_aliases_are_ignored(self, tmp_path):
        section = tmp_path / "Bad"
        section.mkdir()
        (section / "thing.py").write_text(
            "from panel_flowdash import register\n"
            "@register(page=False, component=True, port_aliases=['a'])\n"
            "def app(config):\n"
            "    return ''\n"
        )
        assert build_registry(tmp_path)["Bad/thing"].metadata.port_aliases == {}

    async def test_register_rejects_malformed_port_aliases(self):
        with pytest.raises(TypeError, match="port_aliases"):
            register(component=True, port_aliases=["a"])
