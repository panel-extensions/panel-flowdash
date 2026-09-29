"""Tests for the embeddable ``FlowDash`` editor.

These exercise the programmatic API, which is the whole point of the editor
existing separately from ``FlowDashApp``: constructing it from live component
objects, wiring them, and round-tripping through a store, all without a server.
"""

import asyncio
import json

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
import pytest
from panel.tests.util import async_wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.auth import Permission
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    MemoryDashboardStore,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.widgets import BUILTIN_COMPONENTS


@register(page=False, component=True, title="Ticker", provides=[{"key": "ticker", "type": "str"}])
def ticker_select(config):
    return "selector"


@register(page=False, component=True, title="Chart", requires=[{"key": "ticker", "type": "str"}])
def price_chart(config):
    return "chart"


@register(page=False, component=True, requires=[{"key": "tickers", "type": "List"}])
def ticker_list(config):
    return "list"


@register(page=False, component=True, title="Header", singleton=True)
def page_header(config):
    return "header"


class Shouter(Viewer):
    """A Viewer component with a real input param and output method."""

    ticker = param.String(default="")

    @param.output(param.String)
    def shouted(self):
        return self.ticker.upper()

    def __panel__(self):
        return self.ticker


# Registered like the built-in Select: `provides` without a type leaves the output untyped.
@register(page=False, component=True, provides=[{"key": "picked"}])
class Picker(Viewer):
    """A Viewer whose output is untyped, like a generic selection widget."""

    value = param.Parameter(default="A")

    @param.output(param.Parameter)
    @param.depends("value")
    def picked(self):
        return self.value

    def __panel__(self):
        return str(self.value)


class YearRange(Viewer):
    """A Viewer with a typed Integer input."""

    start_year = param.Integer(default=2000, bounds=(1980, 2025))

    def __panel__(self):
        return str(self.start_year)


SELECTOR = "Demo/selector"
CHART = "Demo/chart"
SHOUTER = "Demo/shouter"
SINGLETON = "Demo/header"

# Explicit ids, so the tests do not depend on how ids are derived from modules.
COMPONENTS = {SELECTOR: ticker_select, CHART: price_chart, SHOUTER: Shouter}


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False)


@pytest.fixture
def store_editor():
    return FlowDash(
        COMPONENTS,
        notifications=False,
        store=MemoryDashboardStore(),
        user="alice",
    )


class TestConstruction:
    async def test_specs_built_eagerly_for_live_components(self, editor):
        """Live objects need no import, so the editor is usable immediately."""
        assert editor._components_loaded
        assert set(editor.component_specs) == (
            {SELECTOR, CHART, SHOUTER} | BUILTIN_COMPONENTS.keys()
        )

    async def test_components_positional(self):
        editor = FlowDash({SELECTOR: ticker_select}, notifications=False)
        assert set(editor.component_specs) == ({SELECTOR} | BUILTIN_COMPONENTS.keys())

    async def test_ids_default_to_the_defining_module(self):
        editor = FlowDash(ticker_select, notifications=False)
        assert set(editor.component_specs) == (
            {"test_editor/ticker_select"} | BUILTIN_COMPONENTS.keys()
        )

    async def test_no_components_leaves_the_palette_empty(self):
        editor = FlowDash(notifications=False, include_builtin_components=False)
        assert editor.component_specs == {}
        assert editor._palette.items == []

    async def test_builtins_are_available_without_project_components(self):
        editor = FlowDash(notifications=False)
        assert set(editor.component_specs) == set(BUILTIN_COMPONENTS)
        assert editor._palette.items

    async def test_explicit_component_overrides_builtin_id(self):
        editor = FlowDash({"Widgets/Select": ticker_select}, notifications=False)
        assert editor._component_entries["Widgets/Select"].app is ticker_select

    async def test_directory_components_load_lazily(self, tmp_path):
        _write_project(tmp_path, section="LazySection")
        editor = FlowDash(tmp_path, notifications=False)
        assert not editor._components_loaded
        assert "LazySection/selector" in editor.component_specs
        assert editor._components_loaded

    async def test_directory_components_are_importable(self, tmp_path):
        """A scanned entry must actually import, which needs the dir on sys.path."""
        _write_project(tmp_path, section="ImportableSection")
        editor = FlowDash(tmp_path, notifications=False)
        await editor.ensure_components_loaded_async()

        entry = editor._component_entries["ImportableSection/selector"]
        assert entry.app is not None
        assert editor.add_component("ImportableSection/selector")

    async def test_store_path_is_coerced(self, tmp_path):
        editor = FlowDash(notifications=False, store=tmp_path / "dash.db")
        assert editor.store is not None
        assert editor.new_dashboard("From Path").title == "From Path"

    async def test_dashboard_model_loaded_at_construction(self):
        model = DashboardModel(dashboard_id="d1", user_id="alice", title="Preloaded")
        editor = FlowDash(COMPONENTS, notifications=False, dashboard=model)
        assert editor.dashboard is model

    async def test_dashboard_id_at_construction_needs_store(self):
        with pytest.raises(ValueError, match="without a store"):
            FlowDash(COMPONENTS, notifications=False, dashboard="d1")

    async def test_dashboard_id_resolved_through_store(self):
        store = MemoryDashboardStore()
        saved = store.create_dashboard("alice", "Stored")
        editor = FlowDash(
            COMPONENTS, notifications=False, store=store, dashboard=saved.dashboard_id
        )
        assert editor.dashboard.title == "Stored"


@register(page=False, component=True, provides=[{"key": "values", "type": "List"}])
def option_source(config):
    return None


@register(page=False, component=True, provides=[{"key": "bound", "type": "Number"}])
def bound_source(config):
    return None


class TestBuiltinWidgets:
    def test_select_options_port_and_output(self):
        editor = FlowDash({"Test/options": option_source}, notifications=False)
        src = editor.add_component("Test/options")
        dst = editor.add_component("Widgets/Select", config={"default_options": ["A", "B"]})
        widget = editor._tile_objects[-1]

        assert widget.options == ["A", "B"]
        assert editor.graph.get_state(dst).selected == "A"
        widget.value = "B"
        assert editor.graph.get_state(dst).selected == "B"

        editor.graph.get_state(src).values = ["X", "Y"]
        assert editor.connect(src, "values", dst, "options") is True
        assert widget.options == ["X", "Y"]
        assert editor.graph.get_state(dst).options == ["X", "Y"]
        assert editor.graph.get_state(dst).selected == "X"

        editor.disconnect(src, "values", dst, "options")
        assert widget.options == ["A", "B"]
        assert editor.graph.get_state(dst).selected == "A"

    def test_multichoice_filters_selection_when_options_change(self):
        editor = FlowDash({"Test/options": option_source}, notifications=False)
        src = editor.add_component("Test/options")
        dst = editor.add_component("Widgets/MultiChoice")
        widget = editor._tile_objects[-1]
        widget.value = ["A", "B"]
        assert editor.graph.get_state(dst).selected == ["A", "B"]

        editor.graph.get_state(src).values = ["B", "C"]
        assert editor.connect(src, "values", dst, "options") is True
        assert widget.value == ["B"]
        assert editor.graph.get_state(dst).selected == ["B"]
        assert editor.connect(src, "values", dst, "options") == "Connection already exists."

    def test_select_accepts_numeric_options_and_wires_to_string_input(self):
        editor = FlowDash(
            {"Test/options": option_source, "Test/string": Shouter}, notifications=False
        )
        options = editor.add_component("Test/options")
        select = editor.add_component("Widgets/Select")
        shouter = editor.add_component("Test/string")
        editor.graph.get_state(options).values = [1, 2]

        assert editor.connect(options, "values", select, "options") is True
        widget = editor._tile_objects[1]
        assert widget.options == [1, 2]
        assert editor.graph.get_state(select).selected == 1
        # `selected` is untyped, so its current value decides the connection.
        assert "rejects the current value" in editor.connect(select, "selected", shouter, "ticker")

        editor.graph.get_state(options).values = ["X", "Y"]
        assert editor.graph.get_state(select).selected == "X"
        assert editor.connect(select, "selected", shouter, "ticker") is True
        assert editor.graph.get_state(shouter).ticker == "X"

    def test_multichoice_accepts_numeric_options(self):
        editor = FlowDash({"Test/options": option_source}, notifications=False)
        options = editor.add_component("Test/options")
        multi = editor.add_component("Widgets/MultiChoice")
        editor.graph.get_state(options).values = [1, 2]
        assert editor.connect(options, "values", multi, "options") is True
        editor._tile_objects[1].value = [2]
        assert editor.graph.get_state(multi).selected == [2]

    def test_slider_bounds_follow_config_and_ports(self):
        editor = FlowDash({"Test/bound": bound_source}, notifications=False)
        src_start = editor.add_component("Test/bound")
        src_end = editor.add_component("Test/bound")
        dst = editor.add_component(
            "Widgets/Slider", config={"default_start": 10, "default_end": 30, "step": 2}
        )
        widget = editor._tile_objects[-1]
        assert (widget.start, widget.end, widget.step, widget.value) == (10, 30, 2, 10)

        editor.graph.get_state(src_start).bound = 15
        editor.graph.get_state(src_end).bound = 25
        assert editor.connect(src_start, "bound", dst, "start") is True
        assert editor.connect(src_end, "bound", dst, "end") is True
        assert (widget.start, widget.end, widget.value) == (15, 25, 15)
        widget.value = 20
        assert editor.graph.get_state(dst).selected == 20

        editor.graph.get_state(src_start).bound = 26
        assert widget.disabled
        editor.graph.get_config_state(dst).param.update(label="Range", step=0.5)
        assert (widget.label, widget.step) == ("Range", 0.5)
        editor.graph.get_state(src_start).bound = 18
        assert not widget.disabled

        editor.disconnect(src_start, "bound", dst, "start")
        assert widget.start == 10

    @pytest.mark.parametrize(
        "component_id", ["Widgets/Select", "Widgets/MultiChoice", "Widgets/Slider"]
    )
    def test_widgets_have_a_minimum_width_for_the_canvas(self, component_id):
        editor = FlowDash({}, notifications=False)
        instance_id = editor.add_component(component_id)
        widget = editor._tile_objects[-1]
        assert (widget.min_width, widget.sizing_mode) == (240, "stretch_width")

        editor.graph.get_config_state(instance_id).min_widget_width = 320
        assert widget.min_width == 320

    # panel-reactflow's form passes both `name` and `label` to its widgets, which
    # Panel 1.9 warns about; warnings-as-errors would force the JSON fallback.
    @pytest.mark.filterwarnings("ignore:Both 'name' and 'label':PendingDeprecationWarning")
    @pytest.mark.parametrize(
        "component_id", ["Widgets/Select", "Widgets/MultiChoice", "Widgets/Slider"]
    )
    def test_widget_config_renders_a_schema_form(self, component_id):
        editor = FlowDash({}, notifications=False)
        instance_id = editor.add_component(component_id)
        node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
        schema = editor._flow.node_types[node["type"]]["schema"]
        form = pr.SchemaEditor(node["data"], schema)
        # A property the form cannot render makes it fall back to raw JSON.
        assert set(form._form._widgets) == set(schema["properties"])

    def test_select_and_multichoice_config_reaches_the_widget(self):
        editor = FlowDash({}, notifications=False)
        editor.add_component(
            "Widgets/Select",
            config={
                "description": "Pick one",
                "searchable": True,
                "variant": "filled",
                "size": "small",
            },
        )
        select = editor._tile_objects[-1]
        assert (select.description, select.searchable, select.variant, select.size) == (
            "Pick one",
            True,
            "filled",
            "small",
        )

        multi_id = editor.add_component(
            "Widgets/MultiChoice", config={"placeholder": "Any", "max_items": 2}
        )
        multi = editor._tile_objects[-1]
        assert (multi.placeholder, multi.max_items) == ("Any", 2)
        editor.graph.get_config_state(multi_id).max_items = 0
        assert multi.max_items is None

    def test_slider_default_value_and_format(self):
        editor = FlowDash({}, notifications=False)
        instance_id = editor.add_component(
            "Widgets/Slider", config={"default_value": 30, "format": "0.0", "show_value": False}
        )
        widget = editor._tile_objects[-1]
        assert (widget.value, widget.format, widget.show_value) == (30, "0.0", False)
        assert editor.graph.get_state(instance_id).selected == 30

        config = editor.graph.get_config_state(instance_id)
        config.param.update(default_value=200, format="")
        assert widget.value == 100
        assert widget.format == "0"

    def test_slider_number_type_follows_config_and_ports(self):
        editor = FlowDash({"Test/bound": bound_source}, notifications=False)
        src = editor.add_component("Test/bound")
        dst = editor.add_component("Widgets/Slider", config={"default_value": 10})
        widget = editor._tile_objects[-1]
        state = editor.graph.get_state(dst)
        assert (widget.format, widget.step) == ("0", 1)
        assert type(state.selected) is int

        editor.graph.get_state(src).bound = 0.5
        assert editor.connect(src, "bound", dst, "start") is True
        assert widget.format == pmui.FloatSlider.param.format.default
        assert type(state.selected) is float

        editor.graph.get_config_state(dst).number_type = "integer"
        assert (widget.start, widget.step, widget.format) == (1, 1, "0")
        assert state.selected == 10
        assert type(state.selected) is int

    def test_slider_explicit_float_keeps_whole_number_bounds_float(self):
        editor = FlowDash({}, notifications=False)
        dst = editor.add_component("Widgets/Slider", config={"number_type": "float"})
        assert type(editor.graph.get_state(dst).selected) is float
        assert editor._tile_objects[-1].format == pmui.FloatSlider.param.format.default

    def test_builtin_config_roundtrips(self):
        editor = FlowDash({}, notifications=False)
        editor.add_component(
            "Widgets/Select", config={"label": "Region", "default_options": ["US", "EU"]}
        )
        restored = FlowDash({}, notifications=False)
        restored.load_model(editor.to_model(title="Widgets"))

        assert restored._tile_items[0]["component_id"] == "Widgets/Select"
        assert restored._tile_objects[0].label == "Region"
        assert restored._tile_objects[0].options == ["US", "EU"]


class TestAddRemove:
    async def test_add_component_returns_instance_id(self, editor):
        instance_id = editor.add_component(SELECTOR)
        assert instance_id in editor.graph.node_ids
        assert [n["id"] for n in editor._flow.nodes] == [instance_id]
        assert editor.dirty

    async def test_add_component_unknown_id_raises(self, editor):
        with pytest.raises(KeyError, match="Unknown component"):
            editor.add_component("No/Such")

    async def test_add_component_position(self, editor):
        instance_id = editor.add_component(SELECTOR, position=(120, 340))
        node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
        assert node["position"] == {"x": 120, "y": 340}

    async def test_add_component_position_tuple_and_dict_agree(self, editor):
        a = editor.add_component(SELECTOR, position=(10, 20))
        b = editor.add_component(SELECTOR, position={"x": 10, "y": 20})
        nodes = {n["id"]: n["position"] for n in editor._flow.nodes}
        assert nodes[a] == nodes[b]

    async def test_default_positions_do_not_overlap(self, editor):
        ids = [editor.add_component(SELECTOR) for _ in range(4)]
        positions = {n["id"]: tuple(n["position"].values()) for n in editor._flow.nodes}
        assert len({positions[i] for i in ids}) == 4

    async def test_default_position_follows_viewport(self, editor):
        editor._flow.viewport = {"x": -1000, "y": -500, "zoom": 2}
        instance_id = editor.add_component(SELECTOR)
        node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
        assert node["position"] == {"x": 520, "y": 270}

    async def test_default_position_cascades_within_viewport(self, editor):
        editor._flow.viewport = {"x": 0, "y": 0, "zoom": 1}
        ids = [editor.add_component(SELECTOR) for _ in range(3)]
        positions = {n["id"]: n["position"] for n in editor._flow.nodes}
        assert [positions[i] for i in ids] == [
            {"x": 40, "y": 40},
            {"x": 80, "y": 80},
            {"x": 120, "y": 120},
        ]

    async def test_singleton_can_only_be_added_once(self):
        editor = FlowDash({SINGLETON: page_header}, notifications=False)
        editor.add_component(SINGLETON)
        with pytest.raises(ValueError, match="only be placed once"):
            editor.add_component(SINGLETON)

    async def test_remove_component_drops_node_and_tile(self, editor):
        instance_id = editor.add_component(SELECTOR)
        editor.remove_component(instance_id)
        assert editor._flow.nodes == []
        assert editor._tile_items == []
        assert list(editor.graph.node_ids) == []

    async def test_remove_component_drops_its_edges(self, editor):
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(src, "ticker", dst, "ticker")

        editor.remove_component(src)

        assert editor._flow.edges == []
        assert editor._edge_id_map == {}
        assert list(editor.graph.edges) == []

    async def test_clear_removes_everything(self, editor):
        editor.add_component(SELECTOR)
        editor.add_component(CHART)
        editor.clear()
        assert editor._flow.nodes == []
        assert editor._tile_items == []
        assert list(editor.graph.node_ids) == []


class TestToolbar:
    async def test_clear_disabled_on_empty_canvas(self, editor):
        assert editor._clear_button.disabled
        instance_id = editor.add_component(SELECTOR)
        assert not editor._clear_button.disabled
        editor.remove_component(instance_id)
        assert editor._clear_button.disabled

    async def test_clear_button_asks_for_confirmation(self, editor):
        editor.add_component(SELECTOR)
        editor._clear_button.clicks += 1
        assert editor._clear_dialog.open
        assert len(editor._tile_items) == 1

    async def test_clear_confirmation_clears_canvas(self, editor):
        editor.add_component(SELECTOR)
        editor._clear_button.clicks += 1
        confirm = editor._clear_dialog.objects[1][-1]
        confirm.clicks += 1
        assert not editor._clear_dialog.open
        assert editor._tile_items == []

    async def test_clear_cancel_keeps_canvas(self, editor):
        editor.add_component(SELECTOR)
        editor._clear_button.clicks += 1
        cancel = editor._clear_dialog.objects[1][-2]
        cancel.clicks += 1
        assert not editor._clear_dialog.open
        assert len(editor._tile_items) == 1

    async def test_save_button_highlights_unsaved_changes(self, store_editor):
        assert store_editor._save_button.variant == "text"
        store_editor.add_component(SELECTOR)
        assert store_editor._save_button.variant == "contained"
        store_editor.save(title="Demo")
        assert store_editor._save_button.variant == "text"

    async def test_save_button_disabled_when_read_only(self, editor):
        editor.read_only = True
        assert editor._save_button.disabled


class TestConnect:
    async def test_connect_adds_edge_to_graph_and_canvas(self, editor):
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)

        assert editor.connect(src, "ticker", dst, "ticker") is True

        assert len(editor._flow.edges) == 1
        assert len(list(editor.graph.edges)) == 1

    async def test_connect_survives_reactflow_event_echo(self, editor):
        """Regression: ``ReactFlow.add_edge`` emits the event the app handles.

        Without the mute guard the editor's own ``edge_added`` handler re-enters,
        sees the input as already connected, and removes the edge it just made.
        """
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(src, "ticker", dst, "ticker")
        assert len(editor._flow.edges) == 1

    async def test_connect_rejects_occupied_input(self, editor):
        a = editor.add_component(SELECTOR)
        b = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(a, "ticker", dst, "ticker")

        result = editor.connect(b, "ticker", dst, "ticker")

        assert result is not True
        assert isinstance(result, str)
        assert len(editor._flow.edges) == 1

    async def test_connect_rejects_unknown_port(self, editor):
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        assert editor.connect(src, "nope", dst, "ticker") is not True

    async def test_connection_policy_and_handle_capacity(self, editor):
        """Browser policies omit types so scalar outputs may feed List inputs."""
        assert editor._flow.connection_validation == {
            "direction": True,
            "cycles": True,
            "duplicates": True,
            "capacity": True,
        }
        assert editor._flow.has_connection_validators
        chart_type = editor._flow.node_types[CHART.replace("/", "__")]
        assert chart_type["inputs"][0]["maxConnections"] == 1

        list_editor = FlowDash(
            {SELECTOR: ticker_select, "Demo/list": ticker_list}, notifications=False
        )
        list_type = list_editor._flow.node_types["Demo__list"]
        assert "maxConnections" not in list_type["inputs"][0]
        src = list_editor.add_component(SELECTOR)
        dst = list_editor.add_component("Demo/list")
        assert list_editor.graph.validate_connection(src, "ticker", dst, "tickers") is None

    async def test_drag_validation_reports_graph_reasons(self, editor, monkeypatch):
        """ReactFlow drag requests receive graph reasons for each candidate handle."""
        src = editor.add_component(SHOUTER)
        dst = editor.add_component(SHOUTER)
        messages = []
        monkeypatch.setattr(editor._flow, "_send_msg", messages.append)

        editor._flow._handle_msg(
            {
                "type": "connection_validation_requested",
                "request_id": 1,
                "node_id": src,
                "handle_id": "shouted",
                "handle_type": "source",
            }
        )
        results = messages[-1]["results"]
        assert messages[-1]["request_id"] == 1
        assert next(r for r in results if r["node_id"] == dst)["reason"] is None
        assert "cycle" in next(r for r in results if r["node_id"] == src)["reason"]

        assert editor.connect(src, "shouted", dst, "ticker") is True
        editor._flow._handle_msg(
            {
                "type": "connection_validation_requested",
                "request_id": 2,
                "node_id": src,
                "handle_id": "shouted",
                "handle_type": "source",
            }
        )
        assert (
            "already exists"
            in next(r for r in messages[-1]["results"] if r["node_id"] == dst)["reason"]
        )
        assert len(editor.graph.edges) == 1

    async def test_untyped_output_with_rejected_value_is_refused(self, monkeypatch):
        """An untyped output cannot be wired into an Integer input while it holds a string."""
        editor = FlowDash({"Test/picker": Picker, "Test/years": YearRange}, notifications=False)
        picker = editor.add_component("Test/picker")
        years = editor.add_component("Test/years")
        messages = []
        monkeypatch.setattr(editor._flow, "_send_msg", messages.append)

        editor._flow._handle_msg(
            {
                "type": "connection_validation_requested",
                "request_id": 1,
                "node_id": picker,
                "handle_id": "picked",
                "handle_type": "source",
            }
        )
        reason = next(
            r["reason"]
            for r in messages[-1]["results"]
            if r["node_id"] == years and r["handle_id"] == "start_year"
        )
        assert "must be an integer" in reason

        editor._flow.add_edge(
            {
                "id": "bad",
                "source": picker,
                "target": years,
                "sourceHandle": "picked",
                "targetHandle": "start_year",
            }
        )
        assert editor._flow.edges == []
        assert editor.graph.edges == []
        assert editor.graph.get_state(years).start_year == 2000

    async def test_untyped_output_connects_once_its_value_fits(self):
        editor = FlowDash({"Test/picker": Picker, "Test/years": YearRange}, notifications=False)
        picker = editor.add_component("Test/picker")
        years = editor.add_component("Test/years")

        editor.graph.get_state(picker).picked = 2010
        assert editor.connect(picker, "picked", years, "start_year") is True
        assert editor.graph.get_state(years).start_year == 2010

    async def test_stale_edge_event_is_rolled_back(self, editor):
        """Server validation rejects a stale client edge even after drag validation."""
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        assert editor.connect(src, "ticker", dst, "ticker") is True
        editor.dirty = False
        original_edge = editor._flow.edges[0].copy()

        editor._flow.add_edge(
            {
                "id": "stale",
                "source": src,
                "target": dst,
                "sourceHandle": "ticker",
                "targetHandle": "ticker",
            }
        )

        assert editor._flow.edges == [original_edge]
        assert len(editor.graph.edges) == 1
        assert editor._edge_id_map == {original_edge["id"]: (src, "ticker", dst, "ticker")}
        assert not editor.dirty

    async def test_select_output_with_rejected_value_is_refused(self, monkeypatch):
        """Select's untyped output cannot be wired into an Integer input while it holds a string."""
        editor = FlowDash({"Test/years": YearRange}, notifications=False)
        select = editor.add_component("Widgets/Select")
        years = editor.add_component("Test/years")
        messages = []
        monkeypatch.setattr(editor._flow, "_send_msg", messages.append)

        editor._flow._handle_msg(
            {
                "type": "connection_validation_requested",
                "request_id": 1,
                "node_id": select,
                "handle_id": "selected",
                "handle_type": "source",
            }
        )
        reason = next(
            r["reason"]
            for r in messages[-1]["results"]
            if r["node_id"] == years and r["handle_id"] == "start_year"
        )
        assert "must be an integer" in reason

        editor._flow.add_edge(
            {
                "id": "bad",
                "source": select,
                "target": years,
                "sourceHandle": "selected",
                "targetHandle": "start_year",
            }
        )
        assert editor._flow.edges == []
        assert editor.graph.edges == []
        assert editor.graph.get_state(years).start_year == 2000

    async def test_edge_without_handle_is_rolled_back(self, editor):
        """A client edge without declared handles cannot remain only on the canvas."""
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.dirty = False

        editor._flow.add_edge({"id": "missing-port", "source": src, "target": dst})

        assert editor._flow.edges == []
        assert editor.graph.edges == []
        assert not editor.dirty

    async def test_disconnect_removes_edge(self, editor):
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(src, "ticker", dst, "ticker")

        editor.disconnect(src, "ticker", dst, "ticker")

        assert editor._flow.edges == []
        assert editor._edge_id_map == {}
        assert list(editor.graph.edges) == []

    async def test_disconnect_frees_the_input(self, editor):
        a = editor.add_component(SELECTOR)
        b = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(a, "ticker", dst, "ticker")
        editor.disconnect(a, "ticker", dst, "ticker")

        assert editor.connect(b, "ticker", dst, "ticker") is True


class TestValuePopup:
    """Handle and edge previews look up live values."""

    async def test_hover_is_default_and_popup_has_no_inner_paper(self, editor):
        assert editor.popup_trigger == editor._flow.popup_trigger == "hover"
        popup = editor._build_value_popup("Value", None, "sample")
        assert isinstance(popup, pn.Column)
        assert popup[0].object == "**Value**"

    async def test_hover_unhover_only_closes_matching_inspection(self, editor):
        src = editor.add_component(SELECTOR)
        editor.graph.get_state(src).ticker = "aapl"
        editor._on_handle_hovered(
            {
                "node_id": src,
                "handle_id": "ticker",
                "direction": "output",
                "position": {"x": 1, "y": 2},
            },
            editor._flow,
        )
        editor._inspection_key = ("handle", f"{src}:ticker")
        editor._on_inspection_unhovered({"node_id": "other", "handle_id": "ticker"}, editor._flow)
        assert editor._flow._value_popup is not None

    async def test_handle_click_on_output_shows_current_value(self, editor):
        src = editor.add_component(SELECTOR)
        editor.graph.get_state(src).ticker = "aapl"

        editor._on_handle_clicked(
            {
                "node_id": src,
                "handle_id": "ticker",
                "direction": "output",
                "position": {"x": 1, "y": 2},
            },
            editor._flow,
        )

        assert editor._flow._value_popup is not None
        assert editor._flow._value_popup_position == {"x": 1, "y": 2}

    async def test_custom_value_repr_is_used(self, editor):
        editor.value_repr = lambda value: f"custom: {value}"
        src = editor.add_component(SELECTOR)
        editor.graph.get_state(src).ticker = "aapl"

        preview = editor._value_preview("aapl")

        assert preview.object == "custom: aapl"

    async def test_handle_click_on_input_with_no_value_shows_placeholder(self, editor):
        dst = editor.add_component(CHART)

        editor._on_handle_clicked(
            {
                "node_id": dst,
                "handle_id": "ticker",
                "direction": "input",
                "position": {"x": 1, "y": 2},
            },
            editor._flow,
        )

        assert editor._flow._value_popup is not None

    async def test_handle_click_ignored_without_position(self, editor):
        src = editor.add_component(SELECTOR)

        editor._on_handle_clicked(
            {"node_id": src, "handle_id": "ticker", "direction": "output"}, editor._flow
        )

        assert editor._flow._value_popup is None

    async def test_edge_click_shows_source_value(self, editor):
        src = editor.add_component(SELECTOR)
        dst = editor.add_component(CHART)
        editor.connect(src, "ticker", dst, "ticker")
        editor.graph.get_state(src).ticker = "msft"
        edge_id = editor._flow.edges[0]["id"]

        editor._on_edge_clicked({"edge_id": edge_id, "position": {"x": 3, "y": 4}}, editor._flow)

        assert editor._flow._value_popup is not None
        assert editor._flow._value_popup_position == {"x": 3, "y": 4}

    async def test_edge_click_unknown_edge_is_ignored(self, editor):
        editor._on_edge_clicked({"edge_id": "nope", "position": {"x": 1, "y": 1}}, editor._flow)

        assert editor._flow._value_popup is None

    async def test_port_spec_reports_declared_type(self, editor):
        src = editor.add_component(SELECTOR)

        port = editor._port_spec(src, "ticker", output=True)

        assert port is not None
        assert port.type == "str"


class TestDataflowPropagation:
    async def test_value_flows_along_edge(self, editor):
        src = editor.add_component(SHOUTER)
        dst = editor.add_component(SHOUTER)
        assert editor.connect(src, "shouted", dst, "ticker") is True

        editor.graph.get_state(src).ticker = "aapl"

        assert editor.graph.get_state(dst).ticker == "AAPL"

    async def test_disconnect_resets_the_target_and_stops_propagation(self, editor):
        src = editor.add_component(SHOUTER)
        dst = editor.add_component(SHOUTER)
        editor.connect(src, "shouted", dst, "ticker")
        editor.graph.get_state(src).ticker = "aapl"

        editor.disconnect(src, "shouted", dst, "ticker")

        # Disconnecting restores the input port's declared default...
        assert editor.graph.get_state(dst).ticker == ""
        # ...and further source changes no longer reach it.
        editor.graph.get_state(src).ticker = "msft"
        assert editor.graph.get_state(dst).ticker == ""


class TestModelRoundTrip:
    async def test_to_model_captures_items_and_edges(self, editor):
        src = editor.add_component(SELECTOR, position=(10, 20))
        dst = editor.add_component(CHART, position=(300, 20))
        editor.connect(src, "ticker", dst, "ticker")

        model = editor.to_model(title="Round Trip")

        assert model.title == "Round Trip"
        assert [i.instance_id for i in model.items] == [src, dst]
        assert (model.items[0].x, model.items[0].y) == (10, 20)
        assert len(model.edges) == 1
        assert model.edges[0].source == src

    async def test_to_model_is_detached(self, editor):
        editor.add_component(SELECTOR)
        model = editor.to_model()
        editor.clear()
        assert len(model.items) == 1

    async def test_load_model_rebuilds_canvas(self, editor):
        src = editor.add_component(SELECTOR, position=(10, 20))
        dst = editor.add_component(CHART)
        editor.connect(src, "ticker", dst, "ticker")
        model = editor.to_model(title="Saved")

        fresh = FlowDash(COMPONENTS, notifications=False)
        fresh.load_model(model)

        assert sorted(fresh.graph.node_ids) == sorted([src, dst])
        assert len(fresh._flow.edges) == 1
        assert not fresh.dirty

    async def test_load_model_skips_unknown_components(self, editor):
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Partly Unknown",
            items=[
                DashboardItem(instance_id="n1", component_id=SELECTOR),
                DashboardItem(instance_id="n2", component_id="Gone/missing"),
            ],
        )
        editor.load_model(model)
        assert list(editor.graph.node_ids) == ["n1"]

    async def test_load_model_preserves_tile_layout(self, editor):
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Laid Out",
            items=[DashboardItem(instance_id="n1", component_id=SELECTOR)],
            tile_layout=[{"i": "n1", "x": 2, "y": 0, "w": 4, "h": 3}],
        )
        editor.load_model(model)
        assert editor.layout == [{"i": "n1", "x": 2, "y": 0, "w": 4, "h": 3}]

    async def test_saving_from_wiring_mode_keeps_unrendered_layout(self, editor):
        """A layout loaded but never rendered must survive a save from wiring mode."""
        layout = [{"i": "n1", "x": 2, "y": 0, "w": 4, "h": 3}]
        editor.load_model(
            DashboardModel(
                dashboard_id="d1",
                user_id="alice",
                title="Laid Out",
                items=[DashboardItem(instance_id="n1", component_id=SELECTOR)],
                tile_layout=layout,
            )
        )
        assert editor.mode == "wiring"
        assert editor.to_model().tile_layout == layout

    async def test_reference_width_survives_wiring_and_dashboard_modes(self, editor):
        editor.load_model(
            DashboardModel(
                dashboard_id="d1",
                user_id="alice",
                title="Laid Out",
                items=[DashboardItem(instance_id="n1", component_id=SELECTOR)],
                reference_width=1400,
            )
        )
        assert editor.to_model().reference_width == 1400

        editor.mode = "dashboard"
        assert editor._tile_grid.reference_width == 1400

        editor._tile_grid.reference_width = 1100
        editor.mode = "wiring"
        assert editor.to_model().reference_width == 1100

        editor.mode = "dashboard"
        assert editor._tile_grid.reference_width == 1100

    async def test_loading_dashboard_without_reference_width_clears_it(self, editor):
        items = [DashboardItem(instance_id="n1", component_id=SELECTOR)]
        editor.load_model(
            DashboardModel(
                dashboard_id="d1", user_id="alice", title="A", items=items, reference_width=1400
            )
        )
        editor.mode = "dashboard"
        editor.load_model(
            DashboardModel(dashboard_id="d2", user_id="alice", title="B", items=items)
        )
        editor.mode = "dashboard"
        assert editor._tile_grid.reference_width is None
        assert editor.to_model().reference_width is None

    async def test_loading_dashboard_clears_previous_custom_layouts(self, editor):
        """Stale overrides would stop the next dashboard's layouts from being generated."""
        items = [DashboardItem(instance_id="n1", component_id=SELECTOR)]
        xs = [{"index": 0, "width": 100, "height": 80, "visible": True}]
        editor.load_model(
            DashboardModel(
                dashboard_id="d1",
                user_id="alice",
                title="A",
                items=items,
                breakpoints=[600],
                responsive_layouts={"xs": xs},
            )
        )
        editor.mode = "dashboard"
        assert editor._tile_grid.responsive_layouts == {"xs": xs}
        assert editor._tile_grid.breakpoints == [600]

        editor.load_model(
            DashboardModel(dashboard_id="d2", user_id="alice", title="B", items=items)
        )
        editor.mode = "dashboard"
        assert editor._tile_grid.responsive_layouts == {}
        assert editor._tile_grid.breakpoints == [768, 1200]


class TestPersistence:
    async def test_new_dashboard_persists_and_resets(self, store_editor):
        store_editor.add_component(SELECTOR)
        model = store_editor.new_dashboard("Fresh")

        assert model.title == "Fresh"
        assert model.user_id == "alice"
        assert store_editor.dashboard is model
        assert store_editor._flow.nodes == []
        assert not store_editor.dirty
        assert store_editor.store.find_by_id_or_title("Fresh") is not None

    async def test_new_dashboard_without_store_is_ephemeral(self, editor):
        model = editor.new_dashboard("Ephemeral")
        assert model.title == "Ephemeral"
        assert editor.dashboard is model

    async def test_save_persists_and_clears_dirty(self, store_editor):
        store_editor.new_dashboard("Persisted")
        store_editor.add_component(SELECTOR)
        assert store_editor.dirty

        saved = store_editor.save()

        assert not store_editor.dirty
        reloaded = store_editor.store.load_dashboard("alice", saved.dashboard_id)
        assert len(reloaded.items) == 1

    async def test_save_triggers_saved_event(self, store_editor):
        events = []
        store_editor.param.watch(lambda e: events.append(e), "saved")
        store_editor.save()
        assert len(events) == 1

    async def test_save_without_store_returns_model(self, editor):
        editor.add_component(SELECTOR)
        model = editor.save(title="Unstored")
        assert model.title == "Unstored"
        assert len(model.items) == 1

    async def test_save_refuses_when_read_only(self, store_editor):
        store_editor.new_dashboard("Locked")
        store_editor.read_only = True
        with pytest.raises(RuntimeError, match="read-only"):
            store_editor.save()

    async def test_save_reuses_dashboard_identity(self, store_editor):
        created = store_editor.new_dashboard("Stable")
        saved = store_editor.save()
        assert saved.dashboard_id == created.dashboard_id
        assert len(store_editor.store.list_dashboards("alice")) == 1

    async def test_round_trip_through_store_in_fresh_editor(self, store_editor):
        store_editor.new_dashboard("Shared")
        src = store_editor.add_component(SHOUTER)
        dst = store_editor.add_component(SHOUTER)
        store_editor.connect(src, "shouted", dst, "ticker")
        saved = store_editor.save()

        fresh = FlowDash(COMPONENTS, notifications=False, store=store_editor.store)
        fresh.load(saved.dashboard_id)

        assert sorted(fresh.graph.node_ids) == sorted([src, dst])
        fresh.graph.get_state(src).ticker = "nvda"
        assert fresh.graph.get_state(dst).ticker == "NVDA"

    async def test_load_by_title(self, store_editor):
        store_editor.new_dashboard("By Title")
        store_editor.save()
        store_editor.clear()

        store_editor.load("By Title")

        assert store_editor.dashboard.title == "By Title"

    async def test_load_missing_raises(self, store_editor):
        with pytest.raises(KeyError, match="Dashboard not found"):
            store_editor.load("nope")

    async def test_load_without_store_raises(self, editor):
        with pytest.raises(ValueError, match="without a store"):
            editor.load("anything")


class TestDisplayModes:
    async def test_wiring_mode_floats_the_toolbar_over_the_canvas(self, editor):
        assert editor.mode == "wiring"
        assert editor._workspace_area.objects[0] is editor._flow
        assert editor._flow.top_panel == [editor._controls]
        assert editor._side_panel.visible

    async def test_dashboard_mode_puts_the_toolbar_above_the_grid(self, editor):
        editor.mode = "dashboard"
        assert editor._workspace_area.objects[:2] == [editor._controls, editor._tile_grid]
        assert editor._flow.top_panel == []
        assert not editor._side_panel.visible

    async def test_toolbar_returns_to_the_canvas(self, editor):
        editor.mode = "dashboard"
        editor.mode = "wiring"
        assert editor._controls not in editor._workspace_area.objects
        assert editor._flow.top_panel == [editor._controls]

    async def test_dialogs_stay_mounted_in_both_modes(self, editor):
        dialogs = [editor._clear_dialog, editor._import_dialog]
        assert editor._workspace_area.objects[-2:] == dialogs
        editor.mode = "dashboard"
        assert editor._workspace_area.objects[-2:] == dialogs

    async def test_non_editable_always_shows_the_grid(self, editor):
        editor.editable = False
        assert editor._tile_grid in editor._workspace_area.objects
        assert editor._controls not in editor._workspace_area.objects
        assert not editor._side_panel.visible

    async def test_toolbar_can_be_hidden(self):
        editor = FlowDash(COMPONENTS, notifications=False, toolbar=False)
        assert editor._flow.top_panel == []
        assert editor._side_panel.visible
        editor.mode = "dashboard"
        assert editor._controls not in editor._workspace_area.objects

    async def test_empty_canvas_shows_drop_hint(self, editor):
        assert editor._flow.bottom_panel == [editor._empty_hint]
        instance_id = editor.add_component(SELECTOR)
        assert editor._flow.bottom_panel == []
        editor.remove_component(instance_id)
        assert editor._flow.bottom_panel == [editor._empty_hint]

    async def test_preview_locks_the_grid(self, editor):
        editor.param.update(mode="dashboard", preview=True)
        assert not editor._tile_grid.editable
        assert not editor._tile_grid.card

    async def test_toolbar_extra_is_seated_between_download_and_clear(self):
        button = pmui.Button(label="Share")
        editor = FlowDash(COMPONENTS, notifications=False, toolbar_extra=[button])
        assert editor._actions_row.objects == [
            editor._save_button,
            editor._download_button,
            button,
            editor._clear_button,
        ]

    async def test_toolbar_extra_updates_reactively(self, editor):
        button = pmui.Button(label="Later")
        editor.toolbar_extra = [button]
        assert button in editor._actions_row.objects

    async def test_switching_out_of_dashboard_mode_stashes_layout(self, editor):
        editor.add_component(SELECTOR)
        editor.mode = "dashboard"
        rendered = editor.layout
        editor.mode = "wiring"
        assert editor.layout == rendered


@register(page=False, component=True, sidebar=True, title="Side")
def side_panel(config):
    return "side"


SIDE = "Demo/side"


@pytest.fixture
def sidebar_editor():
    return FlowDash({**COMPONENTS, SIDE: side_panel}, notifications=False)


class TestSidebarPublishing:
    async def test_sidebar_components_are_published(self, sidebar_editor):
        sidebar_editor.add_component(SIDE)
        assert len(sidebar_editor.sidebar) == 1

    async def test_sidebar_components_are_kept_out_of_the_grid(self, sidebar_editor):
        sidebar_editor.add_component(SIDE)
        sidebar_editor.add_component(SELECTOR)
        sidebar_editor.mode = "dashboard"

        assert len(sidebar_editor._tile_grid.objects) == 1
        assert len(sidebar_editor.sidebar) == 1

    async def test_clear_empties_the_sidebar(self, sidebar_editor):
        sidebar_editor.add_component(SIDE)
        sidebar_editor.clear()
        assert sidebar_editor.sidebar == []


@register(page=False, component=True, title="Async", provides=[{"key": "ticker", "type": "str"}])
async def async_select(config):
    await asyncio.sleep(0)
    return pn.pane.Markdown("async selector")


@register(page=False, component=True, title="Gen", provides=[{"key": "ticker", "type": "str"}])
async def gen_select(config):
    yield pn.pane.Markdown("first")
    yield pn.pane.Markdown("second")


class AsyncShouter(Viewer):
    """A Viewer whose output method and ``__panel__`` are both async."""

    ticker = param.String(default="")

    @param.output(param.String)
    async def shouted(self):
        await asyncio.sleep(0)
        return self.ticker.upper()

    async def __panel__(self):
        return pn.pane.Markdown(self.ticker)


class GenShouter(Viewer):
    """A Viewer whose output method is an async generator."""

    ticker = param.String(default="")

    @param.output(param.String)
    async def shouted(self):
        yield self.ticker.upper()
        yield f"{self.ticker.upper()}!"

    def __panel__(self):
        return self.ticker


ASYNC = "Demo/async"
GEN = "Demo/gen"
ASYNC_SHOUTER = "Demo/async_shouter"
GEN_SHOUTER = "Demo/gen_shouter"

ASYNC_COMPONENTS = {
    ASYNC: async_select,
    GEN: gen_select,
    ASYNC_SHOUTER: AsyncShouter,
    GEN_SHOUTER: GenShouter,
    CHART: price_chart,
}


@pytest.fixture
def async_editor():
    return FlowDash(ASYNC_COMPONENTS, notifications=False)


def _tile_content(view):
    """Resolve the object a tile actually renders, through any deferred pane."""
    return getattr(view, "_pane", view)


class TestAsyncComponents:
    """An ``async def app`` must render as a tile, not leak an un-awaited coroutine.

    A coroutine handed to ``pn.panel`` becomes a ``Str`` pane of its repr and the
    body never runs, so the tile renders blank. These assert the async paths are
    deferred to Panel instead of being called inline.
    """

    async def test_async_component_is_awaited(self, async_editor, recwarn):
        async_editor.add_component(ASYNC)
        view = async_editor._tile_objects[0]
        await asyncio.sleep(0.05)

        assert isinstance(_tile_content(view), pn.pane.Markdown)
        assert _tile_content(view).object == "async selector"
        assert not [w for w in recwarn if "never awaited" in str(w.message)]

    async def test_async_generator_component_is_iterated(self, async_editor):
        async_editor.add_component(GEN)
        view = async_editor._tile_objects[0]
        await asyncio.sleep(0.05)

        assert _tile_content(view).object == "second"

    async def test_async_component_is_not_wrapped_as_a_string(self, async_editor):
        """The exact symptom of the bug: a ``Str`` pane holding a coroutine repr."""
        async_editor.add_component(ASYNC)
        view = async_editor._tile_objects[0]
        await asyncio.sleep(0.05)

        assert "coroutine" not in str(_tile_content(view).object)

    async def test_async_viewer_panel_is_awaited(self, async_editor):
        async_editor.add_component(ASYNC_SHOUTER)
        view = async_editor._tile_objects[0]
        await asyncio.sleep(0.05)

        assert isinstance(_tile_content(view), pn.pane.Markdown)

    async def test_async_output_publishes_a_value_not_a_coroutine(self, async_editor):
        instance_id = async_editor.add_component(ASYNC_SHOUTER)
        async_editor.graph.get_state(instance_id).ticker = "msft"
        await asyncio.sleep(0.05)

        assert async_editor.graph.get_state(instance_id).shouted == "MSFT"

    async def test_async_output_propagates_downstream(self, async_editor):
        src = async_editor.add_component(ASYNC_SHOUTER)
        dst = async_editor.add_component(ASYNC_SHOUTER)
        assert async_editor.connect(src, "shouted", dst, "ticker") is True

        async_editor.graph.get_state(src).ticker = "aapl"
        await asyncio.sleep(0.05)

        assert async_editor.graph.get_state(dst).ticker == "AAPL"

    async def test_async_generator_output_publishes_every_value(self, async_editor):
        instance_id = async_editor.add_component(GEN_SHOUTER)
        state = async_editor.graph.get_state(instance_id)
        seen = []
        state.param.watch(lambda event: seen.append(event.new), "shouted")

        state.ticker = "msft"
        await asyncio.sleep(0.05)

        assert seen[-2:] == ["MSFT", "MSFT!"]


def _write_project(tmp_path, section="Analytics"):
    """Write a one-component project.

    Importing a section caches a module bound to this tmp_path for the rest of
    the session, so each test that imports needs its own section name.
    """
    section = tmp_path / section
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "selector.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, provides=['company'])\n"
        "def app(config):\n"
        "    return 'selector'\n"
    )


def _write_marker_project(tmp_path, section):
    """Write a two-component project where each module records its own import.

    A module touches ``<tmp_path>/<name>.imported`` at import time, so a test can
    assert which components were actually imported rather than merely which
    specs exist.
    """
    directory = tmp_path / section
    directory.mkdir()
    (directory / "__init__.py").write_text("")
    for name in ("used", "unused"):
        (directory / f"{name}.py").write_text(
            "import pathlib\n"
            "from panel_flowdash import register\n\n"
            f"pathlib.Path({str(tmp_path)!r}, '{name}.imported').touch()\n\n"
            "@register(page=False, component=True, provides=['company'])\n"
            "def app(config):\n"
            f"    return '{name}'\n"
        )
    return f"{section}/used", f"{section}/unused"


def _imported(tmp_path, name):
    return (tmp_path / f"{name}.imported").exists()


class TestScopedLoading:
    """Loading a dashboard must not import components it does not place.

    Importing a module runs its top-level code, so a component doing I/O at
    import time would otherwise tax every dashboard in the project.
    """

    async def test_load_model_imports_only_placed_components(self, tmp_path):
        used, _ = _write_marker_project(tmp_path, "ScopedLoad")
        editor = FlowDash(tmp_path, notifications=False)
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Only used",
            items=[DashboardItem(instance_id="n1", component_id=used)],
        )

        editor.load_model(model)

        assert _imported(tmp_path, "used")
        assert not _imported(tmp_path, "unused")
        assert [n["id"] for n in editor._flow.nodes] == ["n1"]

    async def test_load_model_async_imports_only_placed_components(self, tmp_path):
        used, _ = _write_marker_project(tmp_path, "ScopedLoadAsync")
        editor = FlowDash(tmp_path, notifications=False)
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Only used",
            items=[DashboardItem(instance_id="n1", component_id=used)],
        )

        await editor.load_model_async(model)

        assert _imported(tmp_path, "used")
        assert not _imported(tmp_path, "unused")
        assert [n["id"] for n in editor._flow.nodes] == ["n1"]

    async def test_empty_dashboard_imports_nothing(self, tmp_path):
        _write_marker_project(tmp_path, "ScopedEmpty")
        editor = FlowDash(tmp_path, notifications=False)

        editor.load_model(DashboardModel(dashboard_id="d1", user_id="alice", title="Empty"))

        assert not _imported(tmp_path, "used")
        assert not _imported(tmp_path, "unused")

    async def test_add_component_imports_only_that_component(self, tmp_path):
        used, _ = _write_marker_project(tmp_path, "ScopedAdd")
        editor = FlowDash(tmp_path, notifications=False)

        editor.add_component(used)

        assert _imported(tmp_path, "used")
        assert not _imported(tmp_path, "unused")

    async def test_add_component_unknown_id_imports_nothing(self, tmp_path):
        _write_marker_project(tmp_path, "ScopedUnknown")
        editor = FlowDash(tmp_path, notifications=False)

        with pytest.raises(KeyError, match="Unknown component"):
            editor.add_component("No/Such")

        assert not _imported(tmp_path, "used")
        assert not _imported(tmp_path, "unused")

    async def test_scoped_load_keeps_full_catalog_available(self, tmp_path):
        """A scoped load must not make the unloaded components unreachable."""
        used, unused = _write_marker_project(tmp_path, "ScopedThenAll")
        editor = FlowDash(tmp_path, notifications=False)
        editor.load_model(
            DashboardModel(
                dashboard_id="d1",
                user_id="alice",
                title="Only used",
                items=[DashboardItem(instance_id="n1", component_id=used)],
            )
        )

        instance_id = editor.add_component(unused)

        assert _imported(tmp_path, "unused")
        assert set(editor.component_specs) == ({used, unused} | BUILTIN_COMPONENTS.keys())
        assert instance_id in editor.graph.node_ids
        # The node placed by the scoped load survives the later spec registration.
        assert "n1" in editor.graph.node_ids

    async def test_scoped_load_preserves_edges(self, tmp_path):
        """Registering specs later must not tear down existing wiring."""
        directory = tmp_path / "ScopedEdges"
        directory.mkdir()
        (directory / "__init__.py").write_text("")
        (directory / "src.py").write_text(
            "from panel_flowdash import register\n\n"
            "@register(page=False, component=True, provides=[{'key': 'ticker', 'type': 'str'}])\n"
            "def app(config):\n"
            "    return 'src'\n"
        )
        (directory / "dst.py").write_text(
            "from panel_flowdash import register\n\n"
            "@register(page=False, component=True, requires=[{'key': 'ticker', 'type': 'str'}])\n"
            "def app(config):\n"
            "    return 'dst'\n"
        )
        (directory / "other.py").write_text(
            "from panel_flowdash import register\n\n"
            "@register(page=False, component=True, provides=['company'])\n"
            "def app(config):\n"
            "    return 'other'\n"
        )
        editor = FlowDash(tmp_path, notifications=False)
        editor.load_model(
            DashboardModel(
                dashboard_id="d1",
                user_id="alice",
                title="Wired",
                items=[
                    DashboardItem(instance_id="a", component_id="ScopedEdges/src"),
                    DashboardItem(instance_id="b", component_id="ScopedEdges/dst"),
                ],
                edges=[
                    DashboardEdge(
                        source="a", source_port="ticker", target="b", target_port="ticker"
                    )
                ],
            )
        )
        assert len(list(editor.graph.edges)) == 1

        editor.add_component("ScopedEdges/other")

        assert len(list(editor.graph.edges)) == 1
        assert set(editor.graph.node_ids) >= {"a", "b"}


class TestSpecCaching:
    async def test_viewer_specs_do_not_construct_the_component(self):
        """Spec introspection reads the class, so a Viewer's __init__ never runs.

        Constructing every registered Viewer to read its ports would run each
        component's __init__ on every session.
        """
        constructed = []

        class Exploding(Viewer):
            ticker = param.String(default="")

            def __init__(self, **params):
                constructed.append(1)
                super().__init__(**params)

            @param.output(param.String)
            def shouted(self):
                return self.ticker.upper()

            def __panel__(self):
                return self.ticker

        editor = FlowDash({"Demo/exploding": Exploding}, notifications=False)
        spec = editor.component_specs["Demo/exploding"]

        assert not constructed
        assert [p.name for p in spec.outputs] == ["shouted"]
        assert "ticker" in [p.name for p in spec.inputs]

    async def test_spec_is_cached_on_the_registry_entry(self, tmp_path):
        """Entries are shared between sessions, so specs are built once per process."""
        _write_project(tmp_path, section="CachedSpecs")
        registry = normalize_components(tmp_path)

        first = FlowDash(registry, notifications=False)
        spec = first.component_specs["CachedSpecs/selector"]

        second = FlowDash(registry, notifications=False)

        assert second.component_specs["CachedSpecs/selector"] is spec


def _palette_path(editor, component_id):
    for i, section in enumerate(editor._palette.items):
        for j, item in enumerate(section["items"]):
            if item["component_id"] == component_id:
                return [i, j]
    raise AssertionError(f"{component_id} not in palette")


def _drop(editor, component_id=None, *, path=None, position=(10, 20), target=None):
    editor._flow._handle_msg(
        {
            "type": "drop",
            "drop_type": "application/x-flowdash-component",
            "data": {"path": path or _palette_path(editor, component_id), "label": ""},
            "position": {"x": position[0], "y": position[1]},
            "target": target,
        }
    )


class TestPalette:
    async def test_palette_groups_components_by_section(self, editor):
        sections = {s["label"]: s for s in editor._palette.items}
        assert {"Demo", "Widgets"} <= set(sections)
        demo = sections["Demo"]
        assert demo["draggable"] is False
        assert [i["component_id"] for i in demo["items"]] == [SELECTOR, CHART, SHOUTER]
        assert all(i["draggable"] for i in demo["items"])
        assert editor._palette.drag_type in editor._flow.drop_types

    async def test_placed_singleton_is_not_draggable(self):
        editor = FlowDash({SINGLETON: page_header}, notifications=False)
        editor.add_component(SINGLETON)
        (item,) = next(s for s in editor._palette.items if s["label"] == "Demo")["items"]
        assert item["draggable"] is False

    async def test_clicking_placed_singleton_does_not_add_it_again(self):
        editor = FlowDash({SINGLETON: page_header}, notifications=False)
        editor.add_component(SINGLETON)
        path = _palette_path(editor, SINGLETON)
        item = editor._palette.items[path[0]]["items"][path[1]]
        editor._palette._process_click({}, tuple(path), item)
        assert len(editor._tile_items) == 1

    async def test_clicking_palette_item_adds_component(self, editor):
        path = _palette_path(editor, CHART)
        item = editor._palette.items[path[0]]["items"][path[1]]
        editor._palette._process_click({}, tuple(path), item)
        assert [i["component_id"] for i in editor._tile_items] == [CHART]

    async def test_drop_on_canvas_adds_component_at_position(self, editor):
        _drop(editor, CHART, position=(120, 80))
        (node,) = editor._flow.nodes
        assert editor._tile_items[0]["component_id"] == CHART
        assert node["position"] == {"x": 120, "y": 80}
        assert editor.graph.edges == []

    async def test_drop_on_input_wires_new_output(self, editor):
        chart = editor.add_component(CHART, position=(500, 100))
        _drop(
            editor,
            SELECTOR,
            target={"node_id": chart, "handle_id": "ticker", "direction": "input"},
        )
        selector = editor._tile_items[1]["instance_id"]
        assert [(e["source"], e["target"]) for e in editor.graph.edges] == [(selector, chart)]
        node = next(n for n in editor._flow.nodes if n["id"] == selector)
        assert node["position"] == {"x": 140, "y": 100}

    async def test_drop_on_output_wires_new_input(self, editor):
        selector = editor.add_component(SELECTOR, position=(0, 0))
        _drop(
            editor,
            CHART,
            target={"node_id": selector, "handle_id": "ticker", "direction": "output"},
        )
        chart = editor._tile_items[1]["instance_id"]
        assert [(e["source"], e["target"]) for e in editor.graph.edges] == [(selector, chart)]
        node = next(n for n in editor._flow.nodes if n["id"] == chart)
        assert node["position"] == {"x": 360, "y": 0}

    async def test_drop_on_incompatible_port_keeps_node_unwired(self):
        @register(page=False, component=True, requires=[{"key": "count", "type": "int"}])
        def counter(config):
            return "counter"

        editor = FlowDash({SELECTOR: ticker_select, "Demo/counter": counter}, notifications=False)
        count = editor.add_component("Demo/counter", position=(500, 0))
        _drop(
            editor, SELECTOR, target={"node_id": count, "handle_id": "count", "direction": "input"}
        )
        assert len(editor._tile_items) == 2
        assert editor.graph.edges == []

    async def test_drop_on_node_body_adds_without_wiring(self, editor):
        chart = editor.add_component(CHART, position=(500, 100))
        _drop(
            editor,
            SELECTOR,
            position=(510, 110),
            target={"node_id": chart, "handle_id": None, "direction": None},
        )
        assert len(editor._tile_items) == 2
        assert editor.graph.edges == []

    async def test_drop_of_section_header_is_ignored(self, editor):
        _drop(editor, path=[0])
        assert editor._tile_items == []

    async def test_dropped_singleton_is_refused_once_placed(self):
        editor = FlowDash({SINGLETON: page_header}, notifications=False)
        editor.add_component(SINGLETON)
        _drop(editor, SINGLETON)
        assert len(editor._tile_items) == 1


def _file_drop(editor, *files):
    editor._flow._handle_msg(
        {
            "type": "drop",
            "drop_type": "Files",
            "data": [
                {
                    "name": name,
                    "type": "application/json",
                    "size": len(content),
                    "content": content,
                }
                for name, content in files
            ],
            "position": {"x": 0, "y": 0},
            "target": None,
        }
    )


def _wired_export():
    source = FlowDash(COMPONENTS, notifications=False)
    src = source.add_component(SELECTOR, position=(10, 20))
    dst = source.add_component(CHART, position=(400, 20))
    source.connect(src, "ticker", dst, "ticker")
    data = source.export_dashboard()
    data["title"] = "Exported"
    data["reference_width"] = 1300
    return data, src, dst


class TestExportImport:
    async def test_export_omits_identity_and_keeps_contents(self, store_editor):
        store_editor.new_dashboard("Mine")
        src = store_editor.add_component(SELECTOR)
        dst = store_editor.add_component(CHART)
        store_editor.connect(src, "ticker", dst, "ticker")

        data = store_editor.export_dashboard()

        assert not {"dashboard_id", "user_id", "permission"} & data.keys()
        assert data["title"] == "Mine"
        assert [i["instance_id"] for i in data["items"]] == [src, dst]
        assert data["edges"] == [
            {"source": src, "source_port": "ticker", "target": dst, "target_port": "ticker"}
        ]
        json.dumps(data)

    async def test_import_recreates_canvas(self, editor):
        data, src, dst = _wired_export()
        editor.add_component(SHOUTER)

        editor.import_dashboard(json.dumps(data))

        assert [i["instance_id"] for i in editor._tile_items] == [src, dst]
        assert [(e["source"], e["target"]) for e in editor.graph.edges] == [(src, dst)]
        assert {n["id"]: n["position"] for n in editor._flow.nodes}[dst] == {"x": 400, "y": 20}
        assert editor.to_model().reference_width == 1300
        assert editor.dirty

    async def test_import_keeps_loaded_dashboard_identity(self, store_editor):
        target = store_editor.new_dashboard("Target")
        store_editor.dashboard.permission = Permission.from_spec(allow_users=["bob"])
        data, src, _dst = _wired_export()
        data.update(dashboard_id="other", user_id="mallory", permission={"allow_users": ["x"]})

        model = store_editor.import_dashboard(data)
        store_editor.save()

        assert (model.dashboard_id, model.user_id, model.title) == (
            target.dashboard_id,
            "alice",
            "Target",
        )
        saved = store_editor.store.load_dashboard("alice", target.dashboard_id)
        assert saved.items[0].instance_id == src
        assert saved.permission.allow_users == frozenset({"bob"})
        assert store_editor.store.load_dashboard("mallory", "other") is None

    async def test_import_without_dashboard_uses_file_title(self, editor):
        data, _src, _dst = _wired_export()
        model = editor.import_dashboard(data)
        assert (model.title, model.user_id) == ("Exported", editor.user)

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            ("{not json", "Not valid JSON"),
            ('["items"]', "Not a FlowDash dashboard"),
            ({"title": "No items"}, "Not a FlowDash dashboard"),
            ({"items": [{"component_id": SELECTOR}]}, "Malformed"),
        ],
    )
    async def test_import_rejects_invalid_data(self, editor, data, match):
        instance = editor.add_component(SELECTOR)
        with pytest.raises(ValueError, match=match):
            editor.import_dashboard(data)
        assert [i["instance_id"] for i in editor._tile_items] == [instance]

    async def test_download_writes_export_under_dashboard_title(self, editor):
        editor.new_dashboard("Sales / Q3 report")
        editor.add_component(SELECTOR)

        content = json.loads(editor._download_callback().read())

        assert content == editor.export_dashboard()
        assert editor._download_button.filename == "Sales_Q3_report.json"


class TestFileDrop:
    async def test_canvas_accepts_file_drops(self, editor):
        assert "Files" in editor._flow.drop_types

    async def test_drop_on_empty_canvas_loads_file(self, editor):
        data, src, dst = _wired_export()
        _file_drop(editor, ("notes.txt", "hi"), ("dash.json", json.dumps(data)))
        await async_wait_until(lambda: len(editor._tile_items) == 2)
        assert [(e["source"], e["target"]) for e in editor.graph.edges] == [(src, dst)]
        assert editor.dirty
        assert not editor._import_dialog.open

    async def test_drop_on_populated_canvas_asks_first(self, editor):
        existing = editor.add_component(SHOUTER)
        data, src, _dst = _wired_export()
        _file_drop(editor, ("dash.json", json.dumps(data)))
        await asyncio.sleep(0.05)

        assert editor._import_dialog.open
        assert "dash.json" in editor._import_message.object
        assert [i["instance_id"] for i in editor._tile_items] == [existing]

        confirm = editor._import_dialog.objects[1][-1]
        confirm.clicks += 1
        await async_wait_until(lambda: editor._tile_items[0]["instance_id"] == src)
        assert not editor._import_dialog.open

    async def test_cancelled_drop_keeps_canvas(self, editor):
        existing = editor.add_component(SHOUTER)
        data, _src, _dst = _wired_export()
        _file_drop(editor, ("dash.json", json.dumps(data)))
        cancel = editor._import_dialog.objects[1][-2]
        cancel.clicks += 1
        await asyncio.sleep(0.05)
        assert not editor._import_dialog.open
        assert editor._pending_import is None
        assert [i["instance_id"] for i in editor._tile_items] == [existing]

    @pytest.mark.parametrize("files", [[("notes.txt", "{}")], [("dash.json", "{broken")]])
    async def test_unusable_drop_is_ignored(self, editor, files):
        existing = editor.add_component(SHOUTER)
        _file_drop(editor, *files)
        await asyncio.sleep(0.05)
        assert not editor._import_dialog.open
        assert [i["instance_id"] for i in editor._tile_items] == [existing]

    async def test_drop_skips_components_the_editor_lacks(self):
        data, src, _dst = _wired_export()
        editor = FlowDash({SELECTOR: ticker_select}, notifications=False)
        _file_drop(editor, ("dash.json", json.dumps(data)))
        await async_wait_until(lambda: len(editor._tile_items) == 1)
        assert editor._tile_items[0]["instance_id"] == src
        assert editor.graph.edges == []
