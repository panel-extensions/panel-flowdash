"""Tests for the embeddable ``FlowDash`` editor.

These exercise the programmatic API, which is the whole point of the editor
existing separately from ``FlowDashApp``: constructing it from live component
objects, wiring them, and round-tripping through a store, all without a server.
"""

import asyncio
import gc
import itertools
import json
import logging
import re
import weakref
from types import SimpleNamespace
from typing import ClassVar

import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc
from panel.tests.util import async_wait_until
from panel.viewable import Viewer

from panel_flowdash import DashboardSettings, current_dashboard, register
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


class Disposable(Viewer):
    """A Viewer that counts how often its dispose hook runs."""

    ticker = param.String(default="")

    def __init__(self, **params):
        super().__init__(**params)
        self.disposals = 0

    @param.output(param.String)
    def shouted(self):
        return self.ticker.upper()

    def dispose(self):
        self.disposals += 1
        self.ticker = "disposed"

    def __panel__(self):
        return self.ticker


class FailingDispose(Disposable):
    def dispose(self):
        super().dispose()
        raise RuntimeError("teardown broke")


class AsyncDispose(Disposable):
    async def dispose(self):
        await asyncio.sleep(0)
        super().dispose()


class BrokenView(Disposable):
    disposed: ClassVar[list] = []

    def dispose(self):
        super().dispose()
        self.disposed.append(self)

    def __panel__(self):
        raise RuntimeError("cannot render")


DISPOSABLE = "Demo/disposable"


@pytest.fixture
def disposal_editor():
    return FlowDash(
        {
            **COMPONENTS,
            DISPOSABLE: Disposable,
            "Demo/failing": FailingDispose,
            "Demo/async": AsyncDispose,
            "Demo/broken": BrokenView,
        },
        notifications=False,
    )


def _place_disposable(editor, component_id=DISPOSABLE):
    instance_id = editor.add_component(component_id)
    return instance_id, editor._component_instances[instance_id]


class TestComponentDisposal:
    async def test_remove_component_disposes_once(self, disposal_editor):
        instance_id, instance = _place_disposable(disposal_editor)

        disposal_editor.remove_component(instance_id)

        assert instance.disposals == 1
        assert disposal_editor._component_instances == {}

    async def test_deleting_node_on_canvas_disposes_it(self, disposal_editor):
        instance_id, instance = _place_disposable(disposal_editor)

        disposal_editor._flow._handle_msg({"type": "node_deleted", "node_id": instance_id})

        assert instance.disposals == 1
        assert disposal_editor._tile_items == []

    async def test_hook_runs_after_the_node_left_the_graph(self, disposal_editor):
        src, source = _place_disposable(disposal_editor)
        dst = disposal_editor.add_component(SHOUTER)
        disposal_editor.connect(src, "shouted", dst, "ticker")
        source.ticker = "aapl"

        disposal_editor.remove_component(src)

        assert source.ticker == "disposed"
        assert disposal_editor.graph.get_state(dst).ticker == "AAPL"

    async def test_removed_component_stops_receiving_inputs(self, disposal_editor):
        src = disposal_editor.add_component(SHOUTER)
        dst, target = _place_disposable(disposal_editor)
        disposal_editor.connect(src, "shouted", dst, "ticker")
        disposal_editor.graph.get_state(src).ticker = "aapl"

        disposal_editor.remove_component(dst)
        disposal_editor.graph.get_state(src).ticker = "msft"

        assert target.ticker == "disposed"

    async def test_clear_disposes_every_component(self, disposal_editor):
        placed = [_place_disposable(disposal_editor)[1] for _ in range(3)]
        disposal_editor.add_component(SELECTOR)

        disposal_editor.clear()

        assert [p.disposals for p in placed] == [1, 1, 1]

    async def test_new_dashboard_disposes_previous_components(self, disposal_editor):
        _, instance = _place_disposable(disposal_editor)

        disposal_editor.new_dashboard("Fresh")

        assert instance.disposals == 1

    async def test_loading_a_dashboard_disposes_previous_components(self, disposal_editor):
        _, instance = _place_disposable(disposal_editor)
        model = disposal_editor.to_model()

        disposal_editor.load_model(model)

        assert instance.disposals == 1
        reloaded = next(iter(disposal_editor._component_instances.values()))
        assert reloaded is not instance
        assert reloaded.disposals == 0

    async def test_import_disposes_previous_components(self, disposal_editor):
        _, instance = _place_disposable(disposal_editor)
        data, _src, _dst = _wired_export()

        disposal_editor.import_dashboard(data)

        assert instance.disposals == 1

    async def test_config_change_does_not_dispose(self):
        editor = FlowDash({}, notifications=False)
        instance_id = editor.add_component("Widgets/Slider")
        calls = []
        editor._component_instances[instance_id].dispose = lambda: calls.append(instance_id)

        editor._apply_config_patch(instance_id, {"label": "Range"})
        assert calls == []

        editor.remove_component(instance_id)
        assert calls == [instance_id]

    async def test_disposed_once_across_removal_paths(self, disposal_editor):
        _, instance = _place_disposable(disposal_editor)

        disposal_editor.clear()
        disposal_editor.clear()
        disposal_editor.dispose()

        assert instance.disposals == 1

    async def test_failing_hook_is_logged_and_removal_completes(self, disposal_editor, caplog):
        instance_id, instance = _place_disposable(disposal_editor, "Demo/failing")
        _, other = _place_disposable(disposal_editor)

        disposal_editor.remove_component(instance_id)

        assert instance.disposals == 1
        assert f"dispose() of component '{instance_id}' failed" in caplog.text
        assert "teardown broke" in caplog.text
        assert instance_id not in [i["instance_id"] for i in disposal_editor._tile_items]
        assert instance_id not in disposal_editor.graph.node_ids
        assert other.disposals == 0

    async def test_failing_hook_does_not_stop_clear(self, disposal_editor, caplog):
        _, failing = _place_disposable(disposal_editor, "Demo/failing")
        _, other = _place_disposable(disposal_editor)

        disposal_editor.clear()

        assert (failing.disposals, other.disposals) == (1, 1)
        assert disposal_editor._tile_items == []

    async def test_async_hook_is_awaited(self, disposal_editor):
        instance_id, instance = _place_disposable(disposal_editor, "Demo/async")

        disposal_editor.remove_component(instance_id)

        await async_wait_until(lambda: instance.disposals == 1)

    async def test_components_without_hook_are_removed(self, disposal_editor):
        ids = [disposal_editor.add_component(c) for c in (SELECTOR, SHOUTER)]

        for instance_id in ids:
            disposal_editor.remove_component(instance_id)

        assert disposal_editor._tile_items == []
        assert disposal_editor._component_instances == {}

    async def test_failed_placement_disposes_the_half_built_component(
        self, disposal_editor, monkeypatch
    ):
        monkeypatch.setattr(BrokenView, "disposed", [])

        with pytest.raises(RuntimeError, match="cannot render"):
            disposal_editor.add_component("Demo/broken")

        (instance,) = BrokenView.disposed
        assert instance.disposals == 1
        assert disposal_editor._component_instances == {}

    async def test_hook_runs_after_the_view_left_the_canvas(self, disposal_editor):
        instance_id, instance = _place_disposable(disposal_editor)
        seen = []
        instance.dispose = lambda: seen.append(_placed_ids(disposal_editor))

        disposal_editor.remove_component(instance_id)

        assert seen == [([], [])]

    async def test_reset_disposes_after_the_canvas_is_cleared(self, disposal_editor):
        placed = [_place_disposable(disposal_editor) for _ in range(2)]
        seen = []
        for _, instance in placed:
            instance.dispose = lambda: seen.append(_placed_ids(disposal_editor))

        disposal_editor.clear()

        assert seen == [([], [])] * 2

    async def test_dispose_tears_down_the_editor(self, disposal_editor):
        src, source = _place_disposable(disposal_editor)
        dst = disposal_editor.add_component(SHOUTER)
        disposal_editor.connect(src, "shouted", dst, "ticker")

        disposal_editor.dispose()

        assert source.disposals == 1
        assert list(disposal_editor.graph.node_ids) == []
        assert _placed_ids(disposal_editor) == ([], [])
        assert disposal_editor._tile_objects == []
        assert disposal_editor._flow.edges == []
        assert disposal_editor._component_instances == {}

        _, again = _place_disposable(disposal_editor)
        assert [n["id"] for n in disposal_editor._flow.nodes] == list(
            disposal_editor._component_instances
        )
        assert again.disposals == 0

    async def test_replacing_an_instance_id_disposes_the_previous_component(self, disposal_editor):
        instance_id, previous = _place_disposable(disposal_editor)

        disposal_editor._place(DISPOSABLE, instance_id, {})

        assert previous.disposals == 1
        assert disposal_editor._component_instances[instance_id] is not previous

    async def test_session_end_disposes_components(self):
        doc, session_context = _session_doc()
        with set_curdoc(doc):
            editor = FlowDash({DISPOSABLE: Disposable}, notifications=False)
            _, instance = _place_disposable(editor)

        for callback in doc.session_destroyed_callbacks:
            callback(session_context)

        assert instance.disposals == 1
        assert list(editor.graph.node_ids) == []

    async def test_session_callback_does_not_keep_the_editor_alive(self):
        doc, session_context = _session_doc()
        with set_curdoc(doc):
            editor = FlowDash({DISPOSABLE: Disposable}, notifications=False)
            _, instance = _place_disposable(editor)
        ref = weakref.ref(editor)

        del editor
        gc.collect()

        assert ref() is None
        for callback in doc.session_destroyed_callbacks:
            callback(session_context)
        assert instance.disposals == 0

    async def test_editor_outside_a_session_registers_no_session_callback(self):
        editor = FlowDash({DISPOSABLE: Disposable}, notifications=False)

        assert not any(_disposes(cb, editor) for cb in pn.state._on_session_destroyed)


def _placed_ids(editor):
    return (
        [item["instance_id"] for item in editor._tile_items],
        [node["id"] for node in editor._flow.nodes],
    )


def _session_doc():
    doc = Document()
    session_context = SimpleNamespace(id="session", request=SimpleNamespace(arguments={}))
    doc._session_context = lambda: session_context
    return doc, session_context


def _disposes(callback, editor):
    args = getattr(callback, "args", ())
    return bool(args) and isinstance(args[0], weakref.ref) and args[0]() is editor


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

    async def test_load_model_assigns_canvas_once(self, editor):
        ids = [editor.add_component(SHOUTER, position=(i * 10, 0)) for i in range(5)]
        for src, dst in itertools.pairwise(ids):
            editor.connect(src, "shouted", dst, "ticker")
        model = editor.to_model()

        fresh = FlowDash(COMPONENTS, notifications=False)
        assignments = []
        fresh._flow.param.watch(
            lambda *events: assignments.extend(e.name for e in events), ["nodes", "edges"]
        )
        fresh.load_model(model)

        # One reset and one fill per list, instead of one assignment per item.
        assert assignments.count("nodes") <= 2
        assert assignments.count("edges") <= 2
        assert [n["id"] for n in fresh._flow.nodes] == ids
        assert all(n["view"] is not None and n["deletable"] for n in fresh._flow.nodes)
        assert len(fresh._flow.edges) == 4

        state = fresh.graph.get_state(ids[0])
        state.ticker = "abc"
        assert fresh.graph.get_state(ids[-1]).ticker == "ABC"

    async def test_load_model_skips_node_with_invalid_data(self, editor, caplog):
        # panel-reactflow only validates node data when jsonschema is installed.
        pytest.importorskip("jsonschema")
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Bad data",
            items=[DashboardItem(instance_id="n1", component_id=SELECTOR)],
        )
        editor._flow.node_types = {
            "Demo__selector": {
                "type": "Demo__selector",
                "schema": {"type": "object", "required": ["x"]},
            }
        }
        editor.load_model(model)
        assert editor._flow.nodes == []

    async def test_load_model_warns_once_without_node_validator(self, editor, monkeypatch, caplog):
        import panel_flowdash.editor as editor_module

        monkeypatch.setattr(editor_module, "_validate_node_data", None)
        editor_module._warn_validation_unavailable.cache_clear()
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Unvalidated",
            items=[
                DashboardItem(instance_id="n1", component_id=SELECTOR),
                DashboardItem(instance_id="n2", component_id=SELECTOR),
            ],
        )
        with caplog.at_level("WARNING", logger="panel_flowdash"):
            editor.load_model(model)
            editor.load_model(model)
        editor_module._warn_validation_unavailable.cache_clear()
        warnings = [r for r in caplog.records if "_validate_data" in r.getMessage()]
        assert len(warnings) == 1
        assert [n["id"] for n in editor._flow.nodes] == ["n1", "n2"]

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
        assert editor.layout == [{"i": "n1", "x": 2, "y": 0, "w": 4, "h": 3, "instance_id": "n1"}]

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
        assert editor.to_model().tile_layout == [{**layout[0], "instance_id": "n1"}]

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


def _spec(index, width, visible=True):
    return {"index": index, "width": width, "height": 200, "visible": visible}


def _layout_model(items, tile_layout, **kwargs):
    return DashboardModel(
        dashboard_id="d1",
        user_id="alice",
        title="Laid Out",
        items=[DashboardItem(instance_id=i, component_id=c) for i, c in items],
        tile_layout=tile_layout,
        **kwargs,
    )


class TestTileLayoutKeys:
    """The tile layout follows the tiles by instance id rather than by position."""

    def _three_tiles(self, editor):
        ids = [editor.add_component(SHOUTER) for _ in range(3)]
        editor.mode = "dashboard"
        editor._tile_grid.layout = [_spec(0, 100), _spec(1, 50), _spec(2, 50, visible=False)]
        return ids

    async def test_removing_a_tile_in_wiring_mode_keeps_the_others_layout(self, editor):
        ids = self._three_tiles(editor)
        editor.mode = "wiring"
        editor.remove_component(ids[0])
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [_spec(0, 50), _spec(1, 50, visible=False)]

    async def test_removing_a_tile_in_dashboard_mode_keeps_the_others_layout(self, editor):
        ids = self._three_tiles(editor)
        editor.remove_component(ids[0])
        editor.mode = "wiring"
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [_spec(0, 50), _spec(1, 50, visible=False)]

    async def test_saved_layout_is_keyed_and_drops_removed_tiles(self, editor):
        ids = self._three_tiles(editor)
        editor.mode = "wiring"
        editor.remove_component(ids[1])

        assert editor.to_model().tile_layout == [
            {**_spec(0, 100), "instance_id": ids[0]},
            {**_spec(2, 50, visible=False), "instance_id": ids[2]},
        ]

    async def test_keyed_layout_survives_a_component_that_no_longer_loads(self, editor):
        model = _layout_model(
            [("gone", "Gone/missing"), ("k1", SHOUTER), ("k2", SHOUTER)],
            [
                {**_spec(0, 100), "instance_id": "gone"},
                {**_spec(1, 50), "instance_id": "k1"},
                {**_spec(2, 30, visible=False), "instance_id": "k2"},
            ],
        )
        editor.load_model(model)
        editor.mode = "dashboard"

        # The missing component keeps its slot as a placeholder tile.
        assert editor._tile_grid.layout == [
            _spec(0, 100),
            _spec(1, 50),
            _spec(2, 30, visible=False),
        ]

    async def test_untagged_legacy_layout_maps_by_saved_position(self, editor):
        """Layouts saved by older versions line up with the items as they were saved."""
        model = _layout_model(
            [("gone", "Gone/missing"), ("k1", SHOUTER), ("k2", SHOUTER)],
            [_spec(0, 100), _spec(1, 50), _spec(2, 30, visible=False)],
        )
        editor.load_model(model)

        assert editor.layout == [
            {**_spec(0, 100), "instance_id": "gone"},
            {**_spec(1, 50), "instance_id": "k1"},
            {**_spec(2, 30, visible=False), "instance_id": "k2"},
        ]
        editor.mode = "dashboard"
        assert editor._tile_grid.layout == [
            _spec(0, 100),
            _spec(1, 50),
            _spec(2, 30, visible=False),
        ]

    async def test_tiles_without_an_entry_get_an_empty_one(self, editor):
        model = _layout_model(
            [("k1", SHOUTER), ("k2", SHOUTER), ("k3", SHOUTER)],
            [{**_spec(0, 40), "instance_id": "k1"}, {**_spec(1, 60), "instance_id": "k3"}],
        )
        editor.load_model(model)
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [_spec(0, 40), {}, _spec(1, 60)]

    async def test_responsive_layouts_follow_the_tiles(self, editor):
        ids = [editor.add_component(SHOUTER) for _ in range(2)]
        editor.mode = "dashboard"
        editor._tile_grid.responsive_layouts = {"xs": [_spec(0, 100), _spec(1, 80)]}
        editor.mode = "wiring"
        editor.remove_component(ids[0])
        editor.mode = "dashboard"

        assert editor._tile_grid.responsive_layouts == {"xs": [_spec(0, 80)]}
        assert editor.to_model().responsive_layouts == {
            "xs": [{**_spec(0, 80), "instance_id": ids[1]}]
        }


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
        dialogs = [editor._clear_dialog, editor._import_dialog, editor._settings_dialog]
        assert editor._workspace_area.objects[-3:] == dialogs
        editor.mode = "dashboard"
        assert editor._workspace_area.objects[-3:] == dialogs

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


class Polling(Viewer):
    """A Viewer whose output is an endless async generator, like a live feed."""

    ticks: ClassVar[list] = []

    @param.output(param.Integer)
    async def tick(self):
        i = 0
        while True:
            Polling.ticks.append(i)
            yield i
            i += 1
            await asyncio.sleep(0.01)

    def __panel__(self):
        return "polling"


class Counter(Viewer):
    """A Viewer publishing an Integer."""

    value = param.Integer(default=0)

    @param.output(param.Integer)
    @param.depends("value")
    def count(self):
        return self.value

    def __panel__(self):
        return str(self.value)


class Summing(Viewer):
    """A Viewer with an async output that records each time it starts."""

    a = param.Integer(default=0)
    b = param.Integer(default=0)
    runs: ClassVar[list] = []

    @param.output(param.Integer)
    @param.depends("a", "b")
    async def total(self):
        Summing.runs.append((self.a, self.b))
        await asyncio.sleep(0)
        return self.a + self.b

    def __panel__(self):
        return "summing"


class Exploding(Viewer):
    """A Viewer whose own watcher raises on one particular input."""

    ticker = param.String(default="")

    @param.depends("ticker", watch=True)
    def _react(self):
        if self.ticker == "BOOM":
            raise ValueError("watcher failed")

    def __panel__(self):
        return self.ticker


class TestComponentWiring:
    @pytest.fixture
    def editor(self):
        Polling.ticks = []
        Summing.runs = []
        return FlowDash(
            {
                SHOUTER: Shouter,
                "Demo/counter": Counter,
                "Demo/polling": Polling,
                "Demo/summing": Summing,
                "Demo/exploding": Exploding,
            },
            notifications=False,
        )

    async def test_removed_async_generator_output_stops(self, editor):
        instance_id = editor.add_component("Demo/polling")
        await async_wait_until(lambda: len(Polling.ticks) >= 3)

        editor.remove_component(instance_id)
        await asyncio.sleep(0)
        count = len(Polling.ticks)
        await asyncio.sleep(0.05)

        assert len(Polling.ticks) == count

    async def test_cleared_async_generator_output_stops(self, editor):
        editor.add_component("Demo/polling")
        await async_wait_until(lambda: len(Polling.ticks) >= 3)

        editor.clear()
        await asyncio.sleep(0)
        count = len(Polling.ticks)
        await asyncio.sleep(0.05)

        assert len(Polling.ticks) == count

    async def test_removed_component_is_released(self, editor):
        src = editor.add_component(SHOUTER)
        dst = editor.add_component(SHOUTER)
        editor.connect(src, "shouted", dst, "ticker")
        state = editor.graph.get_state(dst)
        instance = weakref.ref(editor._component_instances[dst])

        editor.remove_component(dst)
        gc.collect()

        assert instance() is None
        assert not any(w for by_type in state.param.watchers.values() for w in by_type.values())

    async def test_failing_consumer_does_not_stop_its_siblings(self, editor, caplog):
        src = editor.add_component(SHOUTER)
        bad = editor.add_component("Demo/exploding")
        good = editor.add_component(SHOUTER)
        editor.connect(src, "shouted", bad, "ticker")
        editor.connect(src, "shouted", good, "ticker")

        editor.graph.get_state(src).ticker = "boom"

        assert editor.graph.get_state(good).ticker == "BOOM"
        assert editor._component_instances[good].ticker == "BOOM"
        assert "watcher failed" in caplog.text

    async def test_async_output_runs_once_per_input_change(self, editor):
        a = editor.add_component("Demo/counter")
        b = editor.add_component("Demo/counter")
        total = editor.add_component("Demo/summing")
        assert editor.connect(a, "count", total, "a") is True
        assert editor.connect(b, "count", total, "b") is True
        await asyncio.sleep(0.01)
        Summing.runs.clear()

        editor._component_instances[a].value = 2
        await async_wait_until(lambda: editor.graph.get_state(total).total == 2)
        await asyncio.sleep(0.01)

        assert Summing.runs == [(2, 0)]


class Inverse(Viewer):
    """A Viewer whose output raises for some inputs."""

    value = param.Number(default=1)

    @param.output(param.Number)
    @param.depends("value")
    def inverse(self):
        return 1 / self.value

    def __panel__(self):
        return str(self.value)


@register(page=False, component=True, provides=["pick", "doubled"])
class Collector(Viewer):
    """A Viewer exposing a Parameter and a method as outputs."""

    instances: ClassVar[list] = []

    pick = param.String(default="seed")
    scale = param.Integer(default=2)

    def __init__(self, **params):
        super().__init__(**params)
        Collector.instances.append(self)

    @param.output(param.Integer)
    @param.depends("pick", "scale")
    def doubled(self):
        return len(self.pick) * self.scale

    def __panel__(self):
        return self.pick


class Receiver(Viewer):
    """A Viewer with a String input."""

    text = param.String(default="")

    def __panel__(self):
        return self.text


class TestParameterOutputs:
    @pytest.fixture
    def editor(self):
        Collector.instances.clear()
        return FlowDash(
            {"Demo/collector": Collector, "Demo/receiver": Receiver}, notifications=False
        )

    async def test_output_publishes_the_parameter(self, editor):
        source = editor.add_component("Demo/collector")
        (collector,) = Collector.instances

        collector.pick = "ab"

        assert editor.graph.get_state(source).pick == "ab"

    async def test_output_starts_from_the_parameters_value(self, editor):
        source = editor.add_component("Demo/collector")
        assert editor.graph.get_state(source).pick == "seed"

    async def test_output_feeds_a_connected_input(self, editor):
        source = editor.add_component("Demo/collector")
        target = editor.add_component("Demo/receiver")
        (collector,) = Collector.instances
        assert editor.connect(source, "pick", target, "text") is True

        collector.pick = "ab"

        assert editor.graph.get_state(target).text == "ab"

    async def test_output_cannot_be_written_from_the_canvas(self, editor):
        source = editor.add_component("Demo/collector")
        (collector,) = Collector.instances
        assert "pick" not in [p.name for p in editor.component_specs["Demo/collector"].inputs]

        editor.graph.get_state(source).pick = "x"

        assert collector.pick == "seed"

    async def test_method_output_still_works_beside_a_parameter_output(self, editor):
        source = editor.add_component("Demo/collector")
        (collector,) = Collector.instances

        collector.pick = "abc"

        assert editor.graph.get_state(source).doubled == 6

    async def test_disconnecting_stops_publishing(self, editor):
        source = editor.add_component("Demo/collector")
        target = editor.add_component("Demo/receiver")
        (collector,) = Collector.instances
        editor.connect(source, "pick", target, "text")
        editor.disconnect(source, "pick", target, "text")

        collector.pick = "a"

        assert editor.graph.get_state(target).text == ""


class MetadataSource(Viewer):
    """A Viewer publishing Parameters through metadata, one set during ``__init__``."""

    instances: ClassVar[list] = []

    company = param.String(default="seed", metadata={"flowdash": {"output": True}})
    count = param.Integer(default=0, metadata={"flowdash": {"output": True, "label": "Count"}})
    scale = param.Integer(default=2)

    def __init__(self, **params):
        super().__init__(**params)
        self.company = "from-init"
        MetadataSource.instances.append(self)

    @param.output(param.Integer)
    @param.depends("company", "scale")
    def size(self):
        return len(self.company) * self.scale

    def __panel__(self):
        return self.company


class CountReceiver(Viewer):
    """A Viewer with an Integer input."""

    amount = param.Integer(default=0)

    def __panel__(self):
        return str(self.amount)


class TestMetadataOutputs:
    @pytest.fixture
    def editor(self):
        MetadataSource.instances.clear()
        return FlowDash(
            {
                "Demo/source": MetadataSource,
                "Demo/text": Receiver,
                "Demo/count": CountReceiver,
            },
            notifications=False,
        )

    async def test_value_set_in_init_reaches_a_connected_input(self, editor):
        source = editor.add_component("Demo/source")
        target = editor.add_component("Demo/text")

        assert editor.connect(source, "company", target, "text") is True

        assert editor.graph.get_state(target).text == "from-init"

    async def test_later_change_reaches_a_connected_input(self, editor):
        source = editor.add_component("Demo/source")
        target = editor.add_component("Demo/text")
        (instance,) = MetadataSource.instances
        editor.connect(source, "company", target, "text")

        instance.company = "changed"

        assert editor.graph.get_state(target).text == "changed"

    async def test_node_state_follows_the_parameter_before_any_connection(self, editor):
        source = editor.add_component("Demo/source")
        (instance,) = MetadataSource.instances
        assert editor.graph.get_state(source).company == "from-init"

        instance.company = "later"

        assert editor.graph.get_state(source).company == "later"

    async def test_typed_output_connects_to_a_matching_input_only(self, editor):
        source = editor.add_component("Demo/source")
        count_target = editor.add_component("Demo/count")
        text_target = editor.add_component("Demo/text")
        (instance,) = MetadataSource.instances

        assert "produces 'Integer'" in editor.connect(source, "count", text_target, "text")
        assert editor.connect(source, "count", count_target, "amount") is True
        instance.count = 3

        assert editor.graph.get_state(count_target).amount == 3

    async def test_method_output_works_beside_metadata_outputs(self, editor):
        source = editor.add_component("Demo/source")
        (instance,) = MetadataSource.instances
        assert editor.graph.get_state(source).size == len("from-init") * 2

        instance.company = "abc"

        assert editor.graph.get_state(source).size == 6

    async def test_output_is_not_an_input_and_cannot_be_written_from_the_canvas(self, editor):
        source = editor.add_component("Demo/source")
        (instance,) = MetadataSource.instances
        assert [p.name for p in editor.component_specs["Demo/source"].inputs] == ["scale"]

        editor.graph.get_state(source).company = "x"

        assert instance.company == "from-init"

    async def test_class_factory_output_is_wired(self):
        instances = []

        def init(self, **params):
            Viewer.__init__(self, **params)
            self.company = "factory"
            instances.append(self)

        Factory = type(
            "Factory",
            (Viewer,),
            {
                "company": param.String(default="", metadata={"flowdash": {"output": True}}),
                "__init__": init,
                "__panel__": lambda self: self.company,
            },
        )
        editor = FlowDash({"Demo/factory": Factory, "Demo/text": Receiver}, notifications=False)
        source = editor.add_component("Demo/factory")
        target = editor.add_component("Demo/text")
        editor.connect(source, "company", target, "text")
        assert editor.graph.get_state(target).text == "factory"

        instances[0].company = "updated"

        assert editor.graph.get_state(target).text == "updated"

    async def test_provides_replaces_metadata_outputs_in_the_editor(self):
        @register(page=False, component=True, provides=["scale"])
        class Replaced(Viewer):
            company = param.String(default="seed", metadata={"flowdash": {"output": True}})
            scale = param.Integer(default=2)

        editor = FlowDash({"Demo/replaced": Replaced}, notifications=False)
        spec = editor.component_specs["Demo/replaced"]

        assert [o.name for o in spec.outputs] == ["scale"]
        assert spec.inputs == []


@register(page=False, component=True, provides=[{"key": "company"}])
def company_source(config):
    return "source"


@register(
    page=False,
    component=True,
    requires=[
        {"key": "company", "label": "Company"},
        {"key": "region", "label": "Region"},
        {"key": "note", "required": False},
        {"key": "hint", "blocking": False},
    ],
)
def report_function(config):
    return pn.pane.Markdown("function report")


@register(page=False, component=True, requires=[{"key": "company", "fallback": "ACME"}])
def report_with_fallback(config):
    return "fallback report"


class RequiredReport(Viewer):
    """A Viewer that cannot render without a company."""

    instances: ClassVar[list] = []

    company = param.String(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )
    notes = param.String(default="")

    def __init__(self, **params):
        super().__init__(**params)
        RequiredReport.instances.append(self)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind("report for {}".format, self.param.company))


@register(page=False, component=True, placeholder="Pick a company to start")
class MessageReport(RequiredReport):
    pass


REPORT = "Demo/report"


class TestRequiredInputPlaceholder:
    @pytest.fixture
    def editor(self):
        RequiredReport.instances.clear()
        return FlowDash(
            {
                "Demo/source": company_source,
                "Demo/viewer": RequiredReport,
                "Demo/message": MessageReport,
                "Demo/function": report_function,
                "Demo/fallback": report_with_fallback,
                "Demo/receiver": Receiver,
                "Demo/picker": Picker,
            },
            notifications=False,
            include_builtin_components=False,
        )

    @staticmethod
    def _shown(view):
        placeholder, body = view
        return placeholder.visible, body.visible

    @pytest.mark.parametrize("component", ["Demo/viewer", "Demo/function"])
    async def test_placeholder_swaps_with_view_as_input_changes(self, editor, component):
        source = editor.add_component("Demo/source")
        target = editor.add_component(component)
        view = editor._tile_objects[1]
        assert editor.connect(source, "company", target, "company") is True
        state = editor.graph.get_state(source)
        if component == "Demo/function":
            editor.graph.get_state(target).region = "EU"

        assert self._shown(view) == (True, False)

        state.company = "ACME"
        assert self._shown(view) == (False, True)

        state.company = None
        assert self._shown(view) == (True, False)

    async def test_disconnecting_returns_the_placeholder(self, editor):
        source = editor.add_component("Demo/source")
        target = editor.add_component("Demo/viewer")
        view = editor._tile_objects[1]
        editor.connect(source, "company", target, "company")
        editor.graph.get_state(source).company = "ACME"
        assert self._shown(view) == (False, True)

        editor.disconnect(source, "company", target, "company")

        assert self._shown(view) == (True, False)

    async def test_setting_an_unconnected_input_directly_shows_the_view(self, editor):
        target = editor.add_component("Demo/viewer")
        view = editor._tile_objects[0]
        assert self._shown(view) == (True, False)

        editor.graph.get_state(target).company = "ACME"

        assert self._shown(view) == (False, True)

    async def test_default_message_names_only_the_missing_inputs(self, editor):
        target = editor.add_component("Demo/function")
        view = editor._tile_objects[0]
        placeholder, _ = view
        assert placeholder.object == "Waiting for Company, Region"

        editor.graph.get_state(target).company = "ACME"

        assert placeholder.object == "Waiting for Region"

    async def test_viewer_message_uses_the_param_label(self, editor):
        editor.add_component("Demo/viewer")
        placeholder, _ = editor._tile_objects[0]
        assert placeholder.object == "Waiting for Company"

    async def test_registered_message_replaces_the_default(self, editor):
        target = editor.add_component("Demo/message")
        view = editor._tile_objects[0]
        placeholder, _ = view
        assert placeholder.object == "Pick a company to start"

        editor.graph.get_state(target).company = "ACME"
        assert self._shown(view) == (False, True)
        assert placeholder.object == "Pick a company to start"

    async def test_component_is_instantiated_while_blocked(self, editor):
        editor.add_component("Demo/viewer")
        assert len(RequiredReport.instances) == 1

    async def test_view_is_not_rebuilt_when_toggling(self, editor):
        source = editor.add_component("Demo/source")
        target = editor.add_component("Demo/viewer")
        editor.connect(source, "company", target, "company")
        view = editor._tile_objects[1]
        _, body = view
        (inner,) = body
        root = view.get_root()
        model = inner._models[root.ref["id"]][0]
        state = editor.graph.get_state(source)

        for value in ("ACME", None, "Globex", None):
            state.company = value

        assert editor._tile_objects[1] is view
        assert list(body) == [inner]
        assert inner._models[root.ref["id"]][0] is model
        assert len(RequiredReport.instances) == 1

    @pytest.mark.parametrize(
        "component", ["Demo/receiver", "Demo/source"], ids=["optional_param", "no_inputs"]
    )
    async def test_components_without_blocking_inputs_are_not_gated(self, editor, component):
        editor.add_component(component)
        view = editor._tile_objects[0]
        assert not isinstance(view, pn.Column)

    async def test_required_but_non_blocking_inputs_do_not_gate(self, editor):
        editor.add_component("Demo/function")
        placeholder, _ = editor._tile_objects[0]
        assert placeholder.object == "Waiting for Company, Region"

    async def test_fallback_satisfies_a_blocking_input(self, editor):
        target = editor.add_component("Demo/fallback")
        view = editor._tile_objects[0]
        assert editor.graph.get_state(target).company == "ACME"
        assert self._shown(view) == (False, True)

    async def test_metadata_output_marked_required_is_not_gated(self):
        class Source(Viewer):
            company = param.String(
                default=None,
                allow_None=True,
                metadata={"flowdash": {"output": True, "required": True}},
            )

            def __panel__(self):
                return "source"

        editor = FlowDash({"Demo/source": Source}, notifications=False)
        editor.add_component("Demo/source")

        assert not isinstance(editor._tile_objects[0], pn.Column)

    async def test_builtin_widgets_are_not_gated(self):
        editor = FlowDash({}, notifications=False)
        for component_id in BUILTIN_COMPONENTS:
            editor.add_component(component_id)

        assert not any(isinstance(view, pn.Column) for view in editor._tile_objects)

    async def test_gated_view_reaches_the_tile_grid(self, editor):
        editor.add_component("Demo/viewer")
        editor.mode = "dashboard"
        assert editor._tile_grid[0] is editor._tile_objects[0]

    async def test_saved_dashboard_loads_gated(self, editor):
        source = editor.add_component("Demo/source")
        target = editor.add_component("Demo/viewer")
        editor.connect(source, "company", target, "company")
        model = editor.to_model("Gated")

        editor.load_model(model)

        view = editor._tile_objects[1]
        assert self._shown(view) == (True, False)
        editor.graph.get_state(source).company = "ACME"
        assert self._shown(view) == (False, True)

    async def test_batched_load_delivers_upstream_values_to_the_gate(self, editor):
        editor.add_component("Demo/picker")
        editor.add_component("Demo/viewer")
        picker, target = (item["instance_id"] for item in editor._tile_items)
        editor.connect(picker, "picked", target, "company")
        model = editor.to_model("Gated")

        editor.load_model(model)

        assert editor.graph.get_state(target).company == "A"
        assert self._shown(editor._tile_objects[1]) == (False, True)

    @pytest.mark.parametrize("component", ["Demo/viewer", "Demo/function"])
    async def test_removing_a_component_releases_its_placeholder(self, editor, component):
        target = editor.add_component(component)
        view = editor._tile_objects[0]
        state = editor.graph.get_state(target)

        editor.remove_component(target)
        state.company = "ACME"
        state.region = "EU" if component == "Demo/function" else None

        assert self._shown(view) == (True, False)
        assert target not in editor._component_wiring

    @pytest.mark.parametrize("teardown", ["clear", "dispose"])
    async def test_teardown_releases_function_component_placeholders(self, editor, teardown):
        target = editor.add_component("Demo/function")
        view = editor._tile_objects[0]
        state = editor.graph.get_state(target)

        getattr(editor, teardown)()
        state.param.update(company="ACME", region="EU")

        assert self._shown(view) == (True, False)
        assert editor._component_wiring == {}


@register(page=False, component=True, title="Explained Totals")
class Explained(Viewer):
    """A Viewer that explains one of its two outputs through the inspect hook."""

    value = param.Integer(default=2)

    @param.output(param.Integer)
    @param.depends("value")
    def doubled(self):
        return self.value * 2

    @param.output(param.String)
    def label(self):
        return "fixed"

    def __flowdash_inspect__(self, port):
        if port == "doubled":
            return f"value * 2, with value = {self.value}"
        return None

    def __panel__(self):
        return str(self.value)


class BrokenExplanation(Explained):
    def __flowdash_inspect__(self, port):
        raise RuntimeError("no explanation today")


class GatedTotals(Viewer):
    """A Viewer whose tile waits for a company and that publishes its length."""

    company = param.String(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    @param.output(param.Integer)
    @param.depends("company")
    def length(self):
        return None if self.company is None else len(self.company)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind("report for {}".format, self.param.company))


EXPLAINED = "Demo/explained"
BROKEN = "Demo/broken"
GATED = "Demo/gated"


def _texts(dialog):
    """The text of every pane in the dialog, in document order."""
    return [str(pane.object) for pane in dialog.select(pn.pane.base.PaneBase)]


def _tile(editor, index=0):
    """The grid's view of a tile, as an inspect wrapper when it has one."""
    editor.editable = False
    return editor._tile_grid.objects[index]


class TestInspectOutputs:
    @pytest.fixture
    def editor(self):
        return FlowDash(
            {
                **COMPONENTS,
                EXPLAINED: Explained,
                BROKEN: BrokenExplanation,
                GATED: GatedTotals,
            },
            notifications=False,
            inspect_outputs=True,
        )

    def _click(self, editor, instance_id):
        editor._inspect_tiles[instance_id][1].clicks += 1

    async def test_off_by_default_leaves_tiles_untouched(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        editor.add_component(SELECTOR)
        editor.editable = False

        assert editor._tile_grid.objects == editor._tile_objects
        assert editor._inspect_dialog not in editor._workspace_area.objects

    async def test_button_only_on_components_with_outputs(self, editor):
        editor.add_component(SELECTOR)
        editor.add_component(CHART)
        editor.editable = False

        with_outputs, without = editor._tile_grid.objects
        assert isinstance(with_outputs, pn.Column)
        assert isinstance(with_outputs[1], pmui.IconButton)
        assert without is editor._tile_objects[1]

    async def test_button_is_hidden_while_arranging_tiles(self, editor):
        instance_id = editor.add_component(SELECTOR)
        editor.mode = "dashboard"
        button = editor._inspect_tiles[instance_id][1]
        assert not button.visible

        editor.preview = True
        assert button.visible

        editor.param.update(preview=False, editable=False)
        assert button.visible

    async def test_tiles_are_not_rebuilt_across_mode_switches(self, editor):
        editor.add_component(SELECTOR)
        first = _tile(editor)
        editor.editable = True
        editor.mode = "wiring"
        editor.mode = "dashboard"
        editor.editable = False

        assert editor._tile_grid.objects[0] is first

    async def test_enabling_at_runtime_wraps_the_tiles(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        instance_id = editor.add_component(SELECTOR)
        editor.editable = False
        assert editor._tile_grid.objects == editor._tile_objects

        editor.inspect_outputs = True

        assert editor._tile_grid.objects == [editor._inspect_tiles[instance_id]]
        assert editor._inspect_dialog in editor._workspace_area.objects

    async def test_dialog_is_mounted_for_both_modes(self, editor):
        assert editor._inspect_dialog in editor._workspace_area.objects
        editor.mode = "dashboard"
        assert editor._inspect_dialog in editor._workspace_area.objects

    async def test_dialog_lists_outputs_with_current_values(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        editor.graph.get_state(instance_id).value = 5
        _tile(editor)

        self._click(editor, instance_id)

        dialog = editor._inspect_dialog
        assert dialog.open
        assert dialog.title == "Explained Totals"
        texts = _texts(dialog)
        assert "**doubled**  \n*Integer*" in texts
        assert "**label**  \n*String*" in texts
        assert 10 in [pane.object for pane in dialog.select(pn.pane.JSON)]
        assert "fixed" in [pane.object for pane in dialog.select(pn.pane.JSON)]

    async def test_unset_output_shows_the_placeholder(self, editor):
        instance_id = editor.add_component(SELECTOR)
        _tile(editor)

        self._click(editor, instance_id)

        assert "*No value yet*" in _texts(editor._inspect_dialog)

    async def test_value_repr_renders_every_value(self, editor):
        editor.value_repr = lambda value: f"custom: {value}"
        instance_id = editor.add_component(EXPLAINED)
        _tile(editor)

        self._click(editor, instance_id)

        texts = _texts(editor._inspect_dialog)
        assert "custom: 4" in texts
        assert "custom: fixed" in texts

    async def test_dialog_is_a_snapshot_taken_at_click_time(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        state = editor.graph.get_state(instance_id)
        _tile(editor)

        self._click(editor, instance_id)
        shown = list(editor._inspect_dialog.objects)
        state.value = 50

        assert [p.object for p in shown[0].select(pn.pane.JSON)] == [4]
        self._click(editor, instance_id)
        assert 100 in [p.object for p in editor._inspect_dialog.select(pn.pane.JSON)]

    async def test_one_dialog_serves_every_tile(self, editor):
        a = editor.add_component(EXPLAINED)
        b = editor.add_component(SELECTOR)
        _tile(editor)
        dialog = editor._inspect_dialog

        self._click(editor, a)
        assert dialog.title == "Explained Totals"
        self._click(editor, b)

        assert dialog.title == "Ticker"
        assert editor._workspace_area.objects.count(dialog) == 1

    async def test_hook_content_follows_the_default_rendering(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        _tile(editor)

        self._click(editor, instance_id)

        section = editor._inspect_dialog.objects[0]
        assert isinstance(section[-1], pn.pane.Markdown)
        assert section[-1].object == "value * 2, with value = 2"
        assert isinstance(section[1], pn.pane.JSON)

    async def test_hook_returning_none_keeps_only_the_default(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        _tile(editor)

        self._click(editor, instance_id)

        label_section = editor._inspect_dialog.objects[1]
        assert len(label_section) == 2

    async def test_hook_error_is_logged_and_shown_without_propagating(self, editor, caplog):
        instance_id = editor.add_component(BROKEN)
        _tile(editor)

        with caplog.at_level("ERROR", logger="panel_flowdash"):
            self._click(editor, instance_id)

        dialog = editor._inspect_dialog
        assert dialog.open
        alerts = dialog.select(pmui.Alert)
        assert len(alerts) == 2
        assert "no explanation today" in alerts[0].objects[0].object
        assert alerts[0].severity == "error"
        assert "Inspecting output 'doubled'" in caplog.text
        assert any(isinstance(p, pn.pane.JSON) for p in dialog.select())

    async def test_function_components_have_no_hook_and_still_inspect(self, editor):
        instance_id = editor.add_component(SELECTOR)
        _tile(editor)

        self._click(editor, instance_id)

        assert editor._inspect_dialog.open
        assert len(editor._inspect_dialog.objects[0]) == 2

    async def test_removing_a_component_forgets_its_button(self, editor):
        instance_id = editor.add_component(SELECTOR)
        _tile(editor)
        assert instance_id in editor._inspect_tiles

        editor.remove_component(instance_id)

        assert editor._inspect_tiles == {}

    async def test_hook_runs_on_the_instance_the_editor_disposes(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        instance = editor._component_instances[instance_id]
        instance.value = 7
        _tile(editor)

        self._click(editor, instance_id)

        assert editor._inspect_dialog.objects[0][-1].object == "value * 2, with value = 7"

    async def test_dispose_forgets_every_button(self, editor):
        editor.add_component(EXPLAINED)
        _tile(editor)

        editor.dispose()

        assert editor._inspect_tiles == {}
        assert editor._component_instances == {}

    async def test_session_end_forgets_every_button(self, editor):
        editor.add_component(EXPLAINED)
        _tile(editor)

        editor._dispose_components()

        assert editor._inspect_tiles == {}

    async def test_click_from_a_replaced_dashboard_is_ignored(self, editor):
        def model(title):
            return DashboardModel(
                dashboard_id=title,
                user_id="u",
                title=title,
                items=[DashboardItem(instance_id="g1", component_id=EXPLAINED)],
            )

        editor.load_model(model("A"))
        old_button = _tile(editor)[1]
        editor.load_model(model("B"))
        _tile(editor)

        old_button.clicks += 1

        assert not editor._inspect_dialog.open
        editor._inspect_tiles["g1"][1].clicks += 1
        assert editor._inspect_dialog.open

    async def test_loading_another_dashboard_closes_the_dialog(self, editor):
        instance_id = editor.add_component(EXPLAINED)
        _tile(editor)
        self._click(editor, instance_id)

        editor.load_model(
            DashboardModel(dashboard_id="other", user_id="u", title="Other", items=[])
        )

        assert not editor._inspect_dialog.open
        assert editor._inspect_dialog.objects == []

    async def test_removing_the_inspected_component_closes_the_dialog(self, editor):
        inspected = editor.add_component(EXPLAINED)
        other = editor.add_component(SELECTOR)
        _tile(editor)
        self._click(editor, inspected)

        editor.remove_component(other)
        assert editor._inspect_dialog.open

        editor.remove_component(inspected)
        assert not editor._inspect_dialog.open

    async def test_gated_tile_keeps_its_button_and_says_what_it_waits_for(self, editor):
        instance_id = editor.add_component(GATED)
        tile = _tile(editor)
        gated = editor._tile_objects[0]
        assert tile[0] is gated
        placeholder, body = gated
        assert placeholder.visible and not body.visible
        assert tile[1].visible

        self._click(editor, instance_id)

        dialog = editor._inspect_dialog
        notice = dialog.objects[0]
        assert isinstance(notice, pmui.Alert)
        assert notice.severity == "info"
        assert "Waiting for Company" in notice.objects[0].object
        assert "*No value yet*" in _texts(dialog)

        editor.graph.get_state(instance_id).company = "ACME"
        self._click(editor, instance_id)

        assert not dialog.select(pmui.Alert)
        assert 4 in [pane.object for pane in dialog.select(pn.pane.JSON)]

    async def test_app_passes_the_switch_to_its_editor(self):
        from panel_flowdash.app import FlowDashApp
        from panel_flowdash.dashboard_store import MemoryDashboardStore

        registry = normalize_components(COMPONENTS)
        on = FlowDashApp(
            registry=registry,
            store=MemoryDashboardStore(),
            editor_options={"inspect_outputs": True},
        )
        off = FlowDashApp(registry=registry, store=MemoryDashboardStore())

        assert on._editor.inspect_outputs
        assert not off._editor.inspect_outputs


class TestOutputFailures:
    async def test_failing_output_keeps_last_value_downstream(self, caplog):
        editor = FlowDash({"Demo/inverse": Inverse}, notifications=False)
        src = editor.add_component("Demo/inverse")
        dst = editor.add_component("Demo/inverse")
        assert editor.connect(src, "inverse", dst, "value") is True

        editor.graph.get_state(src).value = 4
        assert editor.graph.get_state(dst).value == 0.25

        editor.graph.get_state(src).value = 0
        assert editor.graph.get_state(src).inverse == 0.25
        assert editor.graph.get_state(dst).value == 0.25
        assert "Output 'inverse' failed" in caplog.text


class AsyncInverse(Viewer):
    """A Viewer whose async output raises for some inputs."""

    value = param.Number(default=1)

    @param.output(param.Number)
    async def inverse(self):
        return 1 / self.value

    def __panel__(self):
        return str(self.value)


class GenInverse(Viewer):
    """A Viewer whose async generator output raises after its first value."""

    value = param.Number(default=1)

    @param.output(param.Number)
    async def inverse(self):
        yield self.value
        yield 1 / self.value

    def __panel__(self):
        return str(self.value)


@register(
    page=False,
    component=True,
    requires=["value"],
    provides=["echo"],
    handles_failures=["value"],
)
class Tolerant(Viewer):
    """A Viewer that takes failures on an untyped input and forwards its value."""

    value = param.Parameter(default=None)

    @param.output(param.Parameter)
    @param.depends("value")
    def echo(self):
        return self.value

    def __panel__(self):
        return str(self.value)


class WaitingInverse(Inverse):
    """An Inverse that shows a placeholder until its value is set."""

    value = param.Number(default=None, allow_None=True, metadata={"flowdash": {"required": True}})


@register(page=False, component=True, requires=["value"], handles_failures=["value"])
class WaitingTolerant(Viewer):
    """A Viewer that waits for a value and presents a failure on it itself."""

    value = param.Parameter(default=None)

    def __panel__(self):
        return str(self.value)


FAILING_COMPONENTS = {
    "Demo/waiting_inverse": WaitingInverse,
    "Demo/waiting_tolerant": WaitingTolerant,
    "Demo/inverse": Inverse,
    "Demo/async_inverse": AsyncInverse,
    "Demo/gen_inverse": GenInverse,
    "Demo/tolerant": Tolerant,
    "Demo/list": ticker_list,
}


def _overlay_text(view):
    """The message of a tile's failure overlay and whether it is showing."""
    overlay = next(obj for obj in view.select(pn.Column) if "flowdash-failure" in obj.css_classes)
    (message,) = overlay.select(pmui.Typography)
    return message.object, overlay.visible


def _edge_style(editor, edge_id):
    edge = next(e for e in editor._flow.edges if e["id"] == edge_id)
    return edge.get("style")


@pytest.mark.failures
class TestFailurePropagation:
    @pytest.fixture
    def editor(self):
        return FlowDash(FAILING_COMPONENTS, notifications=False, propagate_failures=True)

    def _chain(self, editor, source="Demo/inverse", target="Demo/inverse"):
        src = editor.add_component(source)
        dst = editor.add_component(target)
        assert editor.connect(src, "inverse", dst, "value") is True
        editor.graph.get_state(src).value = 4
        return src, dst

    async def test_default_is_off_and_adds_no_overlay(self):
        editor = FlowDash({"Demo/inverse": Inverse}, notifications=False)
        assert editor.propagate_failures is False
        assert editor.graph.propagate_failures is False

        editor.add_component("Demo/inverse")

        assert isinstance(editor._tile_objects[0], pn.pane.Markdown)

    async def test_failing_output_reaches_downstream_and_logs_once(self, editor, caplog):
        src, dst = self._chain(editor)
        assert editor.graph.get_state(dst).value == 0.25

        editor.graph.get_state(src).value = 0

        assert isinstance(editor.graph.get_state(src).inverse, param.ReactiveError)
        failed = editor.graph.get_failures(dst).failed
        assert failed["value"].text == "ZeroDivisionError: division by zero"
        assert caplog.text.count("Output 'inverse' failed") == 1

    async def test_typed_input_keeps_its_last_value(self, editor):
        src, dst = self._chain(editor)

        editor.graph.get_state(src).value = 0

        assert editor.graph.get_state(dst).value == 0.25
        assert editor._tile_items and dst in editor.graph.node_ids

    async def test_recovery_replaces_the_failure(self, editor):
        src, dst = self._chain(editor)
        editor.graph.get_state(src).value = 0

        editor.graph.get_state(src).value = 2

        assert editor.graph.get_state(src).inverse == 0.5
        assert editor.graph.get_state(dst).value == 0.5
        assert editor.graph.get_failures(dst).failed == {}

    async def test_async_output_failure_reaches_downstream(self, editor):
        src, dst = self._chain(editor, "Demo/async_inverse")
        await asyncio.sleep(0.05)
        assert editor.graph.get_state(dst).value == 0.25

        editor.graph.get_state(src).value = 0
        await asyncio.sleep(0.05)

        assert "ZeroDivisionError" in editor.graph.get_failures(dst).failed["value"].text
        assert editor.graph.get_state(dst).value == 0.25

        editor.graph.get_state(src).value = 5
        await asyncio.sleep(0.05)
        assert editor.graph.get_state(dst).value == 0.2
        assert editor.graph.get_failures(dst).failed == {}

    async def test_async_generator_failure_reaches_downstream(self, editor):
        src, dst = self._chain(editor, "Demo/gen_inverse")
        await asyncio.sleep(0.05)

        editor.graph.get_state(src).value = 0
        await asyncio.sleep(0.05)

        assert "ZeroDivisionError" in editor.graph.get_failures(dst).failed["value"].text

        editor.graph.get_state(src).value = 4
        await asyncio.sleep(0.05)
        assert editor.graph.get_failures(dst).failed == {}

    async def test_opted_in_component_receives_the_failure(self, editor):
        src, dst = self._chain(editor, target="Demo/tolerant")

        editor.graph.get_state(src).value = 0

        received = editor.graph.get_state(dst).value
        assert isinstance(received, param.ReactiveError)
        assert editor.graph.get_failures(dst).failed == {}

    async def test_opted_in_component_passes_the_failure_on_through_its_outputs(self, editor):
        src, dst = self._chain(editor, target="Demo/tolerant")
        after = editor.add_component("Demo/inverse")
        assert editor.connect(dst, "echo", after, "value") is True
        assert editor.graph.get_state(after).value == 0.25

        editor.graph.get_state(src).value = 0

        assert editor.graph.get_failures(after).failed["value"].source_id == dst

    async def test_tile_is_covered_by_a_message_naming_the_failure(self, editor):
        src, _ = self._chain(editor)
        view = editor._tile_objects[1]
        assert _overlay_text(view) == ("", False)

        editor.graph.get_state(src).value = 0

        text, visible = _overlay_text(view)
        assert visible
        assert text == (
            "Input 'value' is stale: 'inverse' of 'inverse' failed "
            "(ZeroDivisionError: division by zero)"
        )

        editor.graph.get_state(src).value = 1
        assert _overlay_text(view) == ("", False)

    async def test_opted_in_tile_has_no_message(self, editor):
        src, _ = self._chain(editor, target="Demo/tolerant")
        view = editor._tile_objects[1]

        editor.graph.get_state(src).value = 0

        assert _overlay_text(view)[1] is False

    async def test_failed_edge_and_node_are_marked_on_the_canvas(self, editor):
        src, dst = self._chain(editor)
        (edge_id,) = editor._edge_id_map
        assert _edge_style(editor, edge_id) is None

        editor.graph.get_state(src).value = 0

        assert "danger" in _edge_style(editor, edge_id)["stroke"]
        node = next(n for n in editor._flow.nodes if n["id"] == dst)
        assert "danger" in node["style"]["borderColor"]

        editor.graph.get_state(src).value = 1
        assert _edge_style(editor, edge_id) is None
        node = next(n for n in editor._flow.nodes if n["id"] == dst)
        assert not node.get("style")

    async def test_edge_wired_while_failed_is_marked_at_once(self, editor):
        src = editor.add_component("Demo/inverse")
        dst = editor.add_component("Demo/inverse")
        editor.graph.get_state(src).value = 0

        editor.connect(src, "inverse", dst, "value")

        (edge_id,) = editor._edge_id_map
        assert "danger" in _edge_style(editor, edge_id)["stroke"]

    async def test_transformed_failed_edge_stays_dashed(self, editor):
        src, dst = self._chain(editor)
        steps = [{"op": "round", "digits": 2}]
        assert editor.set_edge_transform(src, "inverse", dst, "value", steps) is True
        (edge_id,) = editor._edge_id_map

        editor.graph.get_state(src).value = 0

        style = _edge_style(editor, edge_id)
        assert "danger" in style["stroke"]
        assert style["strokeDasharray"] == "6 3"

    async def test_failing_edge_transform_reaches_the_target(self, editor):
        src = editor.add_component("Demo/inverse")
        dst = editor.add_component("Demo/inverse")
        editor.graph.get_state(src).value = 4
        assert editor.connect(src, "inverse", dst, "value", [{"op": "round", "digits": 1}]) is True
        assert editor.graph.get_state(dst).value == 0.2

        editor.graph.get_state(src).inverse = "text"

        assert "StepInputError" in editor.graph.get_failures(dst).failed["value"].text
        assert editor.graph.get_state(dst).value == 0.2

    async def test_list_input_fails_as_a_whole(self, editor):
        a = editor.add_component("Demo/inverse")
        b = editor.add_component("Demo/inverse")
        lst = editor.add_component("Demo/list")
        editor.connect(a, "inverse", lst, "tickers")
        editor.connect(b, "inverse", lst, "tickers")
        assert editor.graph.get_state(lst).tickers == [1, 1]

        editor.graph.get_state(b).value = 0

        assert editor.graph.get_state(lst).tickers == [1, 1]
        assert editor.graph.get_failures(lst).failed["tickers"].source_id == b

    async def test_failed_handle_shows_the_failure_in_the_value_popup(self, editor):
        src, dst = self._chain(editor)
        editor.graph.get_state(src).value = 0
        shown = []
        editor._show_inspection = lambda key, title, value, position, flow: shown.append(value)

        editor._on_handle_clicked(
            {
                "node_id": dst,
                "handle_id": "value",
                "direction": "input",
                "position": {"x": 0, "y": 0},
            },
            editor._flow,
        )

        assert isinstance(shown[0], param.ReactiveError)
        assert isinstance(editor._value_preview(shown[0]), pmui.Alert)

    async def test_failure_during_load_marks_the_canvas(self, editor):
        """A failure raised while a dashboard loads reaches nodes and edges still being batched."""
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Failing",
            items=[
                DashboardItem(instance_id="src", component_id="Demo/inverse"),
                DashboardItem(instance_id="lst", component_id="Demo/list"),
            ],
            edges=[
                DashboardEdge(
                    source="src",
                    source_port="inverse",
                    target="lst",
                    target_port="tickers",
                    transform=[{"op": "column", "column": "x"}],
                )
            ],
        )

        editor.load_model(model)

        assert "tickers" in editor.graph.get_failures("lst").failed
        node = next(n for n in editor._flow.nodes if n["id"] == "lst")
        assert "danger" in node["style"]["borderColor"]
        (edge,) = editor._flow.edges
        assert "danger" in edge["style"]["stroke"]

    async def test_removing_a_component_releases_its_failure_marks(self, editor):
        _, dst = self._chain(editor)
        failures = editor.graph.get_failures(dst)

        editor.remove_component(dst)

        callbacks = [
            w.fn for ws in failures._param__private.watchers["failed"].values() for w in ws
        ]
        assert not any(getattr(fn, "func", None) == editor._sync_failure_marks for fn in callbacks)

    @staticmethod
    def _failing_source(editor):
        src = editor.add_component("Demo/inverse")
        editor.graph.get_state(src).value = 0
        return src

    async def test_failure_shows_over_the_placeholder_of_a_waiting_input(self, editor):
        src = self._failing_source(editor)
        dst = editor.add_component("Demo/waiting_inverse")
        assert editor.connect(src, "inverse", dst, "value") is True
        view = editor._tile_objects[1]
        gated, overlay = view.objects
        placeholder, body = gated.objects

        assert editor.graph.get_state(dst).value is None
        assert placeholder.visible and not body.visible
        assert overlay.visible
        assert "ZeroDivisionError" in _overlay_text(view)[0]

        editor.graph.get_state(src).value = 2

        assert editor.graph.get_state(dst).value == 0.5
        assert not placeholder.visible and body.visible
        assert _overlay_text(view)[1] is False

    async def test_failure_on_a_handling_input_opens_its_gate(self, editor):
        """A component that handles failures is shown so it can present the failure itself."""
        src = editor.add_component("Demo/inverse")
        dst = editor.add_component("Demo/waiting_tolerant")
        gated, overlay = editor._tile_objects[1].objects
        placeholder, body = gated.objects
        assert placeholder.visible

        assert editor.connect(src, "inverse", dst, "value") is True
        editor.graph.get_state(src).value = 0

        assert isinstance(editor.graph.get_state(dst).value, param.ReactiveError)
        assert not placeholder.visible and body.visible
        assert not overlay.visible

    async def test_inspect_dialog_shows_a_failed_output_as_an_error(self):
        editor = FlowDash(
            FAILING_COMPONENTS, notifications=False, propagate_failures=True, inspect_outputs=True
        )
        src = self._failing_source(editor)
        editor.editable = False
        editor.mode = "dashboard"
        editor._inspect_tiles[src][1].clicks += 1

        (section,) = editor._inspect_dialog.objects
        (alert,) = [obj for obj in section.select(pmui.Alert)]
        assert alert.severity == "error"
        assert "ZeroDivisionError" in alert.objects[0].object

    async def test_unknown_handles_failures_input_is_rejected(self):
        @register(page=False, component=True, handles_failures=["nope"])
        class Typo(Viewer):
            value = param.Parameter()

        with pytest.raises(ValueError, match="unknown inputs: nope"):
            FlowDash({"Demo/typo": Typo}, notifications=False)


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


class TestSaveKeepsStoredIdentity:
    async def test_save_does_not_revert_sharing_or_rename_made_elsewhere(self, store_editor):
        store = store_editor.store
        model = store_editor.new_dashboard("Quarterly")
        store.set_permission(
            model.dashboard_id, Permission.from_spec(allow_users=["alice", "bob"])
        )
        store_editor.load_model(store._load_any(model.dashboard_id))

        store.set_permission(model.dashboard_id, Permission.from_spec(allow_users=["alice"]))
        store.rename_dashboard("alice", model.dashboard_id, "Quarterly (confidential)")
        store_editor.add_component(next(iter(store_editor.component_specs)))
        saved = store_editor.save()

        stored = store._load_any(model.dashboard_id)
        assert stored.permission.allow_users == frozenset({"alice"})
        assert stored.title == "Quarterly (confidential)"
        assert len(stored.items) == 1
        assert saved.permission == stored.permission
        assert store_editor.dashboard.title == "Quarterly (confidential)"

    async def test_save_after_delete_elsewhere_does_not_restore_previous_owner(self, store_editor):
        store = store_editor.store
        bobs = store.create_dashboard("bob", "Bob's board")
        store.set_permission(bobs.dashboard_id, Permission.from_spec(allow_users=["alice"]))
        store_editor.load_model(store._load_any(bobs.dashboard_id))

        store.delete_dashboard("bob", bobs.dashboard_id)
        store_editor.add_component(next(iter(store_editor.component_specs)))
        store_editor.save()

        assert store._load_any(bobs.dashboard_id).owner == "alice"

    async def test_explicit_title_still_renames(self, store_editor):
        model = store_editor.new_dashboard("Draft")
        store_editor.save(title="Final")
        assert store_editor.store._load_any(model.dashboard_id).title == "Final"


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
        store_editor.store.set_permission(
            target.dashboard_id, Permission.from_spec(allow_users=["bob"])
        )
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
            ({"items": [], "settings": "oops"}, "Malformed"),
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

    async def test_drop_keeps_components_the_editor_lacks(self):
        data, src, dst = _wired_export()
        editor = FlowDash({SELECTOR: ticker_select}, notifications=False)
        _file_drop(editor, ("dash.json", json.dumps(data)))
        await async_wait_until(lambda: len(editor._tile_items) == 2)
        assert editor._tile_items[0]["instance_id"] == src
        assert "placeholder" in editor._tile_items[1]
        assert editor.graph.edges == []
        assert [(e.source, e.target) for e in editor.to_model().edges] == [(src, dst)]


class BrandSettings(DashboardSettings):
    """An application's own dashboard-wide values."""

    accent = param.String(default="blue")
    density = param.Integer(default=1, bounds=(0, 3))


class FakeTimer:
    """Stands in for the object ``pn.state.add_periodic_callback`` returns."""

    def __init__(self, callback, period):
        self.callback = callback
        self.period = period
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def timers(monkeypatch):
    created = []

    def add_periodic_callback(callback, period=500, **_):
        created.append(FakeTimer(callback, period))
        return created[-1]

    monkeypatch.setattr(pn.state, "add_periodic_callback", add_periodic_callback)
    return created


def _dashboard(settings=None, dashboard_id="d1"):
    return DashboardModel(
        dashboard_id=dashboard_id, user_id="alice", title=dashboard_id, settings=settings or {}
    )


class TestDashboardSettings:
    async def test_defaults(self, editor):
        assert isinstance(editor.settings, DashboardSettings)
        assert editor.settings.refresh_period == 0

    async def test_period_is_bounded(self, editor):
        with pytest.raises(ValueError, match="at least 0"):
            editor.settings.refresh_period = -1

    async def test_settings_class_extends_the_settings(self):
        editor = FlowDash(COMPONENTS, notifications=False, settings_class=BrandSettings)
        assert isinstance(editor.settings, BrandSettings)
        assert editor.settings.accent == "blue"
        assert editor.settings.refresh_period == 0

    async def test_settings_class_must_extend_the_builtin_one(self):
        with pytest.raises(ValueError, match="subclass of DashboardSettings"):
            FlowDash(COMPONENTS, notifications=False, settings_class=dict)

    async def test_editors_do_not_share_settings(self):
        first, second = FlowDash(notifications=False), FlowDash(notifications=False)
        first.settings.refresh_period = 7
        assert second.settings.refresh_period == 0

    async def test_loading_applies_stored_values(self):
        editor = FlowDash(COMPONENTS, notifications=False, settings_class=BrandSettings)
        editor.load_model(_dashboard({"refresh_period": 15, "accent": "red", "density": 2}))
        assert editor.settings.refresh_period == 15
        assert editor.settings.accent == "red"
        assert editor.settings.density == 2

    async def test_missing_keys_take_the_default_not_the_previous_dashboards_value(self):
        editor = FlowDash(COMPONENTS, notifications=False, settings_class=BrandSettings)
        editor.load_model(_dashboard({"refresh_period": 15, "accent": "red"}, "a"))
        editor.load_model(_dashboard({"accent": "green"}, "b"))
        assert editor.settings.refresh_period == 0
        assert editor.settings.accent == "green"

    async def test_unknown_keys_are_ignored_with_a_debug_log(self, editor, caplog):
        with caplog.at_level(logging.DEBUG, logger="panel_flowdash"):
            editor.load_model(_dashboard({"refresh_period": 3, "retired": True, "name": "x"}))
        assert editor.settings.refresh_period == 3
        assert "unknown dashboard setting 'retired'" in caplog.text
        assert "unknown dashboard setting 'name'" in caplog.text
        assert not hasattr(editor.settings, "retired")

    async def test_unknown_keys_survive_a_save(self, editor):
        editor.load_model(_dashboard({"refresh_period": 3, "retired": True}))
        assert editor.save().settings == {
            "description": "",
            "refresh_period": 3,
            "retired": True,
            "viewers_can_inspect": False,
        }

    async def test_rejected_value_falls_back_to_the_default(self, editor, caplog):
        editor.load_model(_dashboard({"refresh_period": 9}, "a"))
        with caplog.at_level(logging.WARNING, logger="panel_flowdash"):
            editor.load_model(_dashboard({"refresh_period": -5}, "b"))
        assert editor.settings.refresh_period == 0
        assert "refresh_period" in caplog.text

    async def test_loading_does_not_mark_the_dashboard_dirty(self, editor):
        editor.load_model(_dashboard({"refresh_period": 15}))
        assert not editor.dirty

    async def test_changing_a_setting_marks_the_dashboard_dirty(self, editor):
        editor.load_model(_dashboard())
        editor.settings.refresh_period = 20
        assert editor.dirty

    async def test_saving_writes_the_current_values(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            store=MemoryDashboardStore(),
            user="alice",
            settings_class=BrandSettings,
        )
        model = editor.new_dashboard("Live")
        editor.settings.param.update(refresh_period=45, accent="red")
        saved = editor.save()
        assert saved.settings == {
            "description": "",
            "refresh_period": 45,
            "viewers_can_inspect": False,
            "accent": "red",
            "density": 1,
        }
        assert editor.store.load_dashboard("alice", model.dashboard_id).settings == saved.settings
        assert not editor.dirty

    async def test_settings_round_trip_through_a_store(self):
        store = MemoryDashboardStore()
        writer = FlowDash(notifications=False, store=store, user="alice")
        model = writer.new_dashboard("Live")
        writer.settings.refresh_period = 60
        writer.save()
        reader = FlowDash(notifications=False, store=store, user="alice")
        reader.load(model.dashboard_id)
        assert reader.settings.refresh_period == 60

    async def test_new_dashboard_starts_from_defaults(self, store_editor):
        store_editor.load_model(_dashboard({"refresh_period": 15}))
        store_editor.new_dashboard("Fresh")
        assert store_editor.settings.refresh_period == 0
        assert not store_editor.dirty

    async def test_export_and_import_carry_the_settings(self, editor):
        editor.load_model(_dashboard({"refresh_period": 15}))
        data = editor.export_dashboard()
        assert data["settings"] == {
            "description": "",
            "refresh_period": 15,
            "viewers_can_inspect": False,
        }
        other = FlowDash(COMPONENTS, notifications=False)
        other.import_dashboard(data)
        assert other.settings.refresh_period == 15
        assert other.dirty

    async def test_settings_are_in_place_before_components_are_created(self):
        seen = []

        @register(page=False, component=True)
        class ReadsSettings(Viewer):
            def __init__(self, **params):
                super().__init__(**params)
                seen.append(current_dashboard().settings.refresh_period)

            def __panel__(self):
                return "x"

        editor = FlowDash({"Demo/reads": ReadsSettings}, notifications=False)
        model = _dashboard({"refresh_period": 8})
        model.items = [DashboardItem(instance_id="n1", component_id="Demo/reads")]
        editor.load_model(model)
        assert seen == [8]


class TestSettingsForm:
    async def test_form_is_generated_from_the_settings(self):
        editor = FlowDash(COMPONENTS, notifications=False, settings_class=BrandSettings)
        form = editor._settings_view._extra_form
        assert set(form._widgets) == {"accent", "density"}

    async def test_form_edits_the_settings_on_save(self, editor):
        editor._settings_dialog.open = True
        editor._settings_view._custom_period.value = 12
        assert editor.settings.refresh_period == 0
        editor._settings_view._save_button.clicks += 1
        assert editor.settings.refresh_period == 12
        assert editor.dirty

    async def test_form_follows_a_loaded_dashboard(self, editor):
        editor.load_model(_dashboard({"refresh_period": 33}))
        editor._settings_dialog.open = True
        assert editor._settings_view._custom_period.value == 33

    async def test_button_opens_the_dialog(self, editor):
        assert not editor._settings_dialog.open
        editor._settings_button.clicks += 1
        assert editor._settings_dialog.open

    async def test_button_is_in_the_toolbar(self, editor):
        assert editor._settings_button in editor._controls.objects

    async def test_button_is_disabled_when_the_dashboard_cannot_be_saved(self, editor):
        assert not editor._settings_button.disabled
        editor.read_only = True
        assert editor._settings_button.disabled

    async def test_non_editable_view_has_no_settings_access(self, editor):
        editor.editable = False
        assert editor._controls not in editor._workspace_area.objects


class TestRefreshTimer:
    async def test_starts_for_a_dashboard_with_a_period(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}))
        assert [t.period for t in timers] == [5000]

    async def test_not_started_without_a_period(self, editor, timers):
        editor.load_model(_dashboard())
        assert timers == []

    async def test_not_started_without_a_dashboard(self, editor, timers):
        editor.settings.refresh_period = 5
        assert timers == []

    async def test_tick_triggers_refresh(self, editor, timers):
        fired = []
        editor.param.watch(fired.append, "refresh")
        editor.load_model(_dashboard({"refresh_period": 5}))
        timers[0].callback()
        timers[0].callback()
        assert len(fired) == 2

    async def test_period_change_restarts_the_callback(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}))
        editor.settings.refresh_period = 2.5
        assert [t.period for t in timers] == [5000, 2500]
        assert [t.stopped for t in timers] == [True, False]

    async def test_zero_stops_it(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}))
        editor.settings.refresh_period = 0
        assert [t.stopped for t in timers] == [True]
        editor.settings.refresh_period = 3
        assert [t.period for t in timers] == [5000, 3000]

    async def test_reloading_with_the_same_period_keeps_one_callback(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}, "a"))
        editor.load_model(_dashboard({"refresh_period": 5}, "b"))
        assert len(timers) == 1
        assert not timers[0].stopped

    async def test_loading_a_dashboard_with_another_period_restarts(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}, "a"))
        editor.load_model(_dashboard({"refresh_period": 9}, "b"))
        assert [t.period for t in timers] == [5000, 9000]
        assert [t.stopped for t in timers] == [True, False]

    async def test_loading_a_dashboard_without_a_period_stops_it(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}, "a"))
        editor.load_model(_dashboard({}, "b"))
        assert [t.stopped for t in timers] == [True]

    async def test_leaving_the_dashboard_stops_it(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}))
        editor.dashboard = None
        assert timers[0].stopped

    async def test_new_dashboard_stops_it(self, store_editor, timers):
        store_editor.load_model(_dashboard({"refresh_period": 5}))
        store_editor.new_dashboard("Fresh")
        assert timers[0].stopped

    async def test_ending_the_session_stops_it(self, timers):
        doc, session_context = _session_doc()
        with set_curdoc(doc):
            editor = FlowDash(COMPONENTS, notifications=False)
            editor.dashboard = _dashboard()
            editor.settings.refresh_period = 5
            editor._sync_refresh_timer()
        assert len(timers) == 1
        for callback in doc.session_destroyed_callbacks:
            callback(session_context)
        assert timers[0].stopped

    async def test_dispose_stops_it(self, editor, timers):
        editor.load_model(_dashboard({"refresh_period": 5}))
        editor.dispose()
        assert timers[0].stopped
        editor.settings.refresh_period = 6
        assert [t.period for t in timers] == [5000, 6000]

    async def test_waits_for_the_page_to_load(self, editor, timers, monkeypatch):
        deferred = []
        monkeypatch.setattr(pn.state, "onload", deferred.append)
        editor.load_model(_dashboard({"refresh_period": 5}))
        assert timers == []
        for callback in deferred:
            callback()
        assert len(timers) == 1


class TestRefreshButton:
    async def test_click_fires_refresh(self, editor):
        fired = []
        editor.param.watch(fired.append, "refresh")
        editor._refresh_button.clicks += 1
        assert len(fired) == 1

    async def test_click_updates_the_indicator(self, editor):
        editor._refresh_label.object = ""
        editor._refresh_button.clicks += 1
        assert re.fullmatch(r"Refreshed \d\d:\d\d:\d\d", editor._refresh_label.object)

    async def test_loading_a_dashboard_stamps_the_indicator(self, editor):
        editor._refresh_label.object = ""
        editor.load_model(_dashboard())
        assert editor._refresh_label.object.startswith("Refreshed ")

    async def test_view_mode_shows_the_bar_above_the_grid(self, editor):
        editor.editable = False
        assert editor._workspace_area.objects[:2] == [editor._refresh_bar, editor._tile_grid]

    async def test_preview_shows_the_bar(self, editor):
        editor.param.update(mode="dashboard", preview=True)
        assert editor._refresh_bar in editor._workspace_area.objects

    @pytest.mark.parametrize("mode", ["wiring", "dashboard"])
    async def test_edit_mode_has_no_bar(self, editor, mode):
        editor.mode = mode
        assert editor._refresh_bar not in editor._workspace_area.objects

    async def test_bar_returns_to_view_mode_after_editing(self, editor):
        editor.editable = False
        editor.editable = True
        editor.editable = False
        assert editor._workspace_area.objects.count(editor._refresh_bar) == 1


class Subscriber(Viewer):
    """A Viewer re-fetching when the dashboard refreshes."""

    calls: ClassVar[list] = []

    def __init__(self, **params):
        super().__init__(**params)
        dashboard = current_dashboard()
        self._text = pn.pane.Markdown(pn.bind(self._fetch, dashboard.param.refresh))

    def _fetch(self, _refresh):
        Subscriber.calls.append(_refresh)
        return f"fetch {len(Subscriber.calls)}"

    def __panel__(self):
        return self._text


def make_function_component():
    captured = []

    def func_component():
        captured.append(current_dashboard())
        return "func"

    return func_component, captured


class TestCurrentDashboard:
    async def test_none_outside_a_dashboard(self):
        assert current_dashboard() is None

    async def test_viewer_sees_its_editor_while_it_is_created(self):
        seen = []

        class Probe(Viewer):
            def __init__(self, **params):
                super().__init__(**params)
                seen.append(current_dashboard())

            def __panel__(self):
                return "probe"

        editor = FlowDash({"Demo/probe": Probe}, notifications=False)
        editor.add_component("Demo/probe")
        assert seen == [editor]
        assert seen[0].settings is editor.settings
        assert current_dashboard() is None

    async def test_function_component_sees_its_editor(self):
        func, captured = make_function_component()
        editor = FlowDash({"Demo/func": func}, notifications=False)
        editor.add_component("Demo/func")
        assert captured == [editor]

    async def test_each_editor_gives_its_own_components_their_own_context(self):
        func, captured = make_function_component()
        first = FlowDash({"Demo/func": func}, notifications=False)
        second = FlowDash({"Demo/func": func}, notifications=False)
        second.add_component("Demo/func")
        first.add_component("Demo/func")
        assert captured == [second, first]

    async def test_context_is_released_when_creation_fails(self):
        class Broken(Viewer):
            def __init__(self, **params):
                raise RuntimeError("boom")

        editor = FlowDash({"Demo/broken": Broken}, notifications=False)
        with pytest.raises(RuntimeError, match="boom"):
            editor.add_component("Demo/broken")
        assert current_dashboard() is None

    async def test_later_calls_in_a_session_find_the_sessions_editor(self):
        doc = Document()
        pn.state.curdoc = doc
        try:
            editor = FlowDash(notifications=False)
            assert current_dashboard() is editor
        finally:
            pn.state.curdoc = None
        assert current_dashboard() is None

    async def test_viewer_rebinds_on_refresh(self):
        Subscriber.calls.clear()
        editor = FlowDash({"Demo/sub": Subscriber}, notifications=False)
        editor.add_component("Demo/sub")
        pane = editor._tile_objects[0]
        assert pane.object == "fetch 1"
        editor._refresh_button.clicks += 1
        assert pane.object == "fetch 2"
        editor.param.trigger("refresh")
        assert pane.object == "fetch 3"


class Watcher(Viewer):
    """A Viewer subscribing with explicit watchers on the editor and its settings."""

    refreshes: ClassVar[list] = []
    changes: ClassVar[list] = []

    def __init__(self, **params):
        super().__init__(**params)
        dashboard = current_dashboard()
        dashboard.param.watch(lambda event: Watcher.refreshes.append(event.new), "refresh")
        dashboard.settings.param.watch(
            lambda event: Watcher.changes.append(event.new), "refresh_period"
        )

    def __panel__(self):
        return "watcher"


class GatedWatcher(Watcher):
    """A Watcher behind a required input, so its tile is gated by a placeholder."""

    company = param.String(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    def __panel__(self):
        return pn.pane.Markdown(pn.bind("watching {}".format, self.param.company))


class TestSubscriptionLifetime:
    @pytest.fixture
    def editor(self):
        Watcher.refreshes.clear()
        Watcher.changes.clear()
        Subscriber.calls.clear()
        return FlowDash({"Demo/watch": Watcher, "Demo/sub": Subscriber}, notifications=False)

    async def test_subscriptions_stay_while_the_component_is_placed(self, editor):
        editor.add_component("Demo/watch")
        editor.param.trigger("refresh")
        editor.settings.refresh_period = 4
        assert (len(Watcher.refreshes), len(Watcher.changes)) == (1, 1)

    async def test_removing_a_component_ends_its_subscriptions(self, editor):
        node = editor.add_component("Demo/watch")
        sub = editor.add_component("Demo/sub")
        editor.remove_component(node)
        editor.remove_component(sub)
        editor.param.trigger("refresh")
        editor.settings.refresh_period = 4
        assert Watcher.refreshes == []
        assert Watcher.changes == []
        assert len(Subscriber.calls) == 1  # the fetch when it was created

    async def test_only_the_removed_components_subscriptions_end(self, editor):
        gone = editor.add_component("Demo/watch")
        editor.add_component("Demo/watch")
        editor.remove_component(gone)
        editor.param.trigger("refresh")
        assert len(Watcher.refreshes) == 1

    async def test_switching_dashboard_ends_the_previous_ones_subscriptions(self, editor):
        editor.add_component("Demo/watch")
        editor.load_model(_dashboard())
        editor.param.trigger("refresh")
        assert Watcher.refreshes == []

    async def test_loading_a_dashboard_does_not_hand_its_settings_to_the_previous_one(
        self, editor
    ):
        editor.add_component("Demo/watch")
        editor.load_model(_dashboard({"refresh_period": 9}))
        assert Watcher.changes == []
        assert editor.settings.refresh_period == 9

    async def test_gated_component_keeps_its_placeholder_and_subscriptions_until_removed(self):
        Watcher.refreshes.clear()
        editor = FlowDash({"Demo/gated": GatedWatcher}, notifications=False)
        node = editor.add_component("Demo/gated")
        assert current_dashboard() is None
        assert len(editor._component_wiring[node]) >= 2
        editor.param.trigger("refresh")
        assert len(Watcher.refreshes) == 1

        editor.remove_component(node)
        editor.param.trigger("refresh")
        assert len(Watcher.refreshes) == 1
        assert node not in editor._component_wiring

    async def test_clearing_the_canvas_ends_subscriptions(self, editor):
        editor.add_component("Demo/watch")
        editor.clear()
        editor.param.trigger("refresh")
        assert Watcher.refreshes == []

    async def test_a_component_that_fails_to_load_leaves_nothing_behind(self, editor):
        class Failing(Viewer):
            def __init__(self, **params):
                super().__init__(**params)
                current_dashboard().param.watch(
                    lambda event: Watcher.refreshes.append(1), "refresh"
                )
                raise RuntimeError("boom")

        editor._registry["Demo/failing"] = normalize_components({"Demo/failing": Failing})[
            "Demo/failing"
        ]
        editor._component_entries["Demo/failing"] = editor._registry["Demo/failing"]
        editor.ensure_components_loaded(["Demo/failing"])
        with pytest.raises(RuntimeError, match="boom"):
            editor.add_component("Demo/failing")
        editor.param.trigger("refresh")
        assert Watcher.refreshes == []
