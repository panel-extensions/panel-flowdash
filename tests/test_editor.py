"""Tests for the embeddable ``FlowDash`` editor.

These exercise the programmatic API, which is the whole point of the editor
existing separately from ``FlowDashApp``: constructing it from live component
objects, wiring them, and round-tripping through a store, all without a server.
"""

import asyncio

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel, MemoryDashboardStore
from panel_flowdash.editor import FlowDash


@register(page=False, component=True, title="Ticker", provides=[{"key": "ticker", "type": "str"}])
def ticker_select(config):
    return "selector"


@register(page=False, component=True, title="Chart", requires=[{"key": "ticker", "type": "str"}])
def price_chart(config):
    return "chart"


class Shouter(Viewer):
    """A Viewer component with a real input param and output method."""

    ticker = param.String(default="")

    @param.output(param.String)
    def shouted(self):
        return self.ticker.upper()

    def __panel__(self):
        return self.ticker


SELECTOR = "Demo/selector"
CHART = "Demo/chart"
SHOUTER = "Demo/shouter"

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
        assert set(editor.component_specs) == {SELECTOR, CHART, SHOUTER}

    async def test_components_positional(self):
        editor = FlowDash({SELECTOR: ticker_select}, notifications=False)
        assert set(editor.component_specs) == {SELECTOR}

    async def test_ids_default_to_the_defining_module(self):
        editor = FlowDash(ticker_select, notifications=False)
        assert set(editor.component_specs) == {"test_editor/ticker_select"}

    async def test_no_components_disables_add(self):
        editor = FlowDash(notifications=False)
        assert editor.component_specs == {}
        assert editor._add_button.disabled
        assert editor._component_picker.disabled

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
    async def test_wiring_mode_shows_the_flow_canvas(self, editor):
        assert editor.mode == "wiring"
        assert editor._workspace_area.objects == [editor._flow]

    async def test_dashboard_mode_shows_the_tile_grid(self, editor):
        editor.mode = "dashboard"
        assert editor._workspace_area.objects == [editor._tile_grid]

    async def test_non_editable_always_shows_the_grid(self, editor):
        editor.editable = False
        assert editor._workspace_area.objects == [editor._tile_grid]
        assert not editor._controls_row.visible

    async def test_toolbar_can_be_hidden(self):
        editor = FlowDash(COMPONENTS, notifications=False, toolbar=False)
        assert not editor._controls_row.visible

    async def test_preview_locks_the_grid(self, editor):
        editor.param.update(mode="dashboard", preview=True)
        assert not editor._tile_grid.editable
        assert not editor._tile_grid.card

    async def test_toolbar_extra_is_seated_in_the_toolbar(self):
        button = pmui.Button(label="Share")
        editor = FlowDash(COMPONENTS, notifications=False, toolbar_extra=[button])
        assert button in editor._controls_row.objects

    async def test_toolbar_extra_updates_reactively(self, editor):
        button = pmui.Button(label="Later")
        editor.toolbar_extra = [button]
        assert button in editor._controls_row.objects

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
