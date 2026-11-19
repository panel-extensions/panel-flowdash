"""Tests for the Split tab: the canvas and the tile grid side by side, and what it remembers."""

import json

import pytest
from panel_splitjs import HSplit

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.command_palette import SHORTCUTS
from panel_flowdash.ui.split_view import (
    DEFAULT_SIZES,
    SplitPreferences,
    is_collapsed,
    valid_sizes,
)
from panel_flowdash.ui.status_bar import select_on_canvas
from tests.test_ui_command_palette import (
    _open,
    app,  # noqa: F401
)
from tests.test_ui_layout_tab import CHART, FILTER, MAP, ChartView, MapView, StateFilter


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView, FILTER: StateFilter}, notifications=False)


def _selected_tile(editor):
    return editor._selected_tile_id()


def _click_tile(editor, instance_id):
    """Select a tile as a click on the grid does."""
    editor._tile_grid.selected = editor._grid_ids.index(instance_id)


class TestPreferences:
    async def test_round_trip(self):
        prefs = SplitPreferences(split=True, sizes=(60.0, 40.0))
        restored = SplitPreferences()
        assert restored.load(prefs.to_json())
        assert (restored.split, restored.sizes) == (True, (60.0, 40.0))

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "",
            "not json",
            "[1, 2]",
            json.dumps({"split": "yes", "sizes": "wide"}),
            json.dumps({"sizes": [100, 0]}),
            json.dumps({"sizes": [50, 50, 0]}),
            json.dumps({"sizes": [True, False]}),
        ],
    )
    async def test_malformed_entries_are_ignored(self, stored):
        prefs = SplitPreferences()
        assert not prefs.load(stored)
        assert (prefs.split, prefs.sizes) == (False, DEFAULT_SIZES)

    async def test_sizes_are_normalized(self):
        assert valid_sizes([30, 10]) == (75.0, 25.0)
        assert valid_sizes((99, 1)) is None

    @pytest.mark.parametrize(
        ("collapsed", "sizes", "pane"),
        [(None, (50, 50), None), (0, (50, 50), 0), (None, (0.5, 99.5), 0), (None, (97, 3), 1)],
    )
    async def test_collapsed_pane(self, collapsed, sizes, pane):
        assert is_collapsed(collapsed, sizes) == pane


class TestSplitTab:
    async def test_shows_canvas_and_grid_in_a_splitter(self, editor):
        editor.mode = "split"
        split = editor._workspace_area[0]
        assert isinstance(split, HSplit)
        left, right = split.objects
        assert list(left) == [editor._flow, editor._split_view.events]
        assert editor._tile_grid in list(right)
        assert editor._side_panel.visible
        assert editor._inspector.visible
        assert not editor._layout_panel.visible
        assert editor._split_view.events.active

    async def test_offered_by_the_edit_tabs(self, editor):
        tabs = editor._dashboard_bar.mode_tabs
        assert list(tabs.options.values()) == ["explore", "wiring", "dashboard", "split"]
        tabs.value = "split"
        assert editor.mode == "split"

    async def test_view_mode_shows_only_the_grid(self, editor):
        editor.mode = "split"
        editor.editable = False
        assert editor._workspace_area[1] is editor._tile_grid
        assert not editor._side_panel.visible
        assert not editor._split_view.events.active

    async def test_added_and_removed_components_reach_the_grid(self, editor):
        editor.mode = "split"
        map_id = editor.add_component(MAP)
        assert editor._grid_ids == [map_id]
        chart_id = editor.add_component(CHART)
        assert editor._grid_ids == [map_id, chart_id]
        editor.remove_component(map_id)
        assert editor._grid_ids == [chart_id]
        assert len(editor._tile_grid.objects) == 1

    async def test_tile_layout_survives_leaving_the_split(self, editor):
        editor.mode = "split"
        map_id = editor.add_component(MAP)
        editor.add_component(CHART)
        editor._tile_grid.layout = [
            {"index": i, "width": 50.0, "height": 300, "visible": True} for i in range(2)
        ]
        editor.mode = "wiring"
        assert {e["instance_id"]: e["width"] for e in editor.layout}[map_id] == 50.0
        editor.mode = "split"
        assert editor._tile_grid.layout[0]["width"] == 50.0

    async def test_canvas_selection_selects_its_tile(self, editor):
        editor.mode = "split"
        map_id, chart_id = editor.add_component(MAP), editor.add_component(CHART)
        select_on_canvas(editor._flow, node_id=chart_id)
        assert _selected_tile(editor) == chart_id
        select_on_canvas(editor._flow, node_id=map_id)
        assert _selected_tile(editor) == map_id
        select_on_canvas(editor._flow)
        assert _selected_tile(editor) is None

    async def test_clicked_tile_selects_its_component(self, editor):
        editor.mode = "split"
        editor.add_component(MAP)
        chart_id = editor.add_component(CHART)
        _click_tile(editor, chart_id)
        assert editor._flow.selection == {"nodes": [chart_id], "edges": []}
        assert editor._inspector._shown == ("node", chart_id)
        assert _selected_tile(editor) == chart_id

    async def test_canvas_fits_its_view_when_shown_and_resized(self, editor, monkeypatch):
        fits = []
        monkeypatch.setattr(editor._flow, "fit_view", lambda *a, **k: fits.append((a, k)))
        editor.mode = "split"
        editor._split_view.events._handle_msg({"type": "resize"})
        assert fits == [((), {})]
        editor._split_view.split.sizes = (40.0, 60.0)
        assert fits == [((), {}), ((), {})]


class TestRevealingTheNodeOfAClickedTile:
    @pytest.fixture
    def fits(self, editor, monkeypatch):
        fits = []
        monkeypatch.setattr(editor._flow, "fit_view", lambda *a, **k: fits.append((a, k)))
        return fits

    def _click(self, editor, instance_id):
        _click_tile(editor, instance_id)

    async def test_off_screen_node_is_panned_to_at_the_same_zoom(self, editor, fits):
        editor.mode = "split"
        map_id = editor.add_component(MAP, position=(2000, 1500))
        editor._split_view.events.pane_size = [500, 600]
        editor._flow.viewport = {"x": 0, "y": 0, "zoom": 0.5}
        self._click(editor, map_id)
        assert fits == [((map_id,), {"min_zoom": 0.5, "max_zoom": 0.5, "duration": 300})]

    async def test_node_in_view_stays_put(self, editor, fits):
        editor.mode = "split"
        map_id = editor.add_component(MAP, position=(100, 100))
        editor._split_view.events.pane_size = [500, 600]
        editor._flow.viewport = {"x": 0, "y": 0, "zoom": 1}
        self._click(editor, map_id)
        assert fits == []
        assert editor._flow.selection["nodes"] == [map_id]

    async def test_panned_viewport_puts_a_near_node_off_screen(self, editor, fits):
        editor.mode = "split"
        map_id = editor.add_component(MAP, position=(100, 100))
        editor._split_view.events.pane_size = [500, 600]
        editor._flow.viewport = {"x": -900, "y": 0, "zoom": 1}
        self._click(editor, map_id)
        assert [a for a, _ in fits] == [(map_id,)]

    async def test_folded_inspector_opens_before_the_node_is_revealed(self, editor, fits):
        editor._split_view.storage.window_width = 1280
        editor._split_view.events.pane_size = [700, 600]
        editor.mode = "split"
        assert editor._inspector.collapsed
        map_id = editor.add_component(MAP, position=(2000, 1500))
        editor._flow.viewport = {"x": 0, "y": 0, "zoom": 1}
        self._click(editor, map_id)
        assert not editor._inspector.collapsed
        assert fits == []
        editor._split_view.events.pane_size = [400, 600]
        assert [a for a, _ in fits] == [(map_id,)]
        editor._split_view.events.pane_size = [500, 600]
        assert len(fits) == 1

    async def test_unknown_pane_size_leaves_the_viewport(self, editor, fits):
        editor.mode = "split"
        map_id = editor.add_component(MAP, position=(5000, 5000))
        self._click(editor, map_id)
        assert fits == []


class TestInspectorInNarrowWindows:
    async def test_folded_until_something_is_selected(self, editor):
        map_id = editor.add_component(MAP)
        editor._split_view.storage.window_width = 1280
        editor.mode = "split"
        assert editor._inspector.collapsed
        select_on_canvas(editor._flow, node_id=map_id)
        assert not editor._inspector.collapsed
        select_on_canvas(editor._flow)
        assert not editor._inspector.collapsed

    async def test_opened_again_when_leaving_the_split(self, editor):
        editor._split_view.storage.window_width = 1280
        editor.mode = "split"
        assert editor._inspector.collapsed
        editor.mode = "wiring"
        assert not editor._inspector.collapsed

    async def test_left_as_the_user_set_it(self, editor):
        editor._split_view.storage.window_width = 1280
        editor.mode = "split"
        editor._inspector.collapsed = False
        editor._inspector.collapsed = True
        editor.mode = "wiring"
        assert editor._inspector.collapsed

    async def test_open_in_wide_windows_and_with_a_selection(self, editor):
        map_id = editor.add_component(MAP)
        editor._split_view.storage.window_width = 1920
        editor.mode = "split"
        assert not editor._inspector.collapsed
        editor.mode = "wiring"
        editor._split_view.storage.window_width = 1280
        select_on_canvas(editor._flow, node_id=map_id)
        editor.mode = "split"
        assert not editor._inspector.collapsed


class TestToggleAndCollapse:
    async def test_toggle_returns_to_the_previous_tab(self, editor):
        editor.mode = "dashboard"
        editor.toggle_split()
        assert editor.mode == "split"
        editor.toggle_split()
        assert editor.mode == "dashboard"

    async def test_collapsing_the_grid_falls_back_to_dataflow(self, editor):
        editor.mode = "split"
        split = editor._split_view.split
        split.param.update(collapsed=1, sizes=(100.0, 0.0))
        assert editor.mode == "wiring"
        assert split.collapsed is None
        assert split.sizes == DEFAULT_SIZES

    async def test_collapsing_the_canvas_falls_back_to_layout(self, editor):
        editor.mode = "split"
        editor._split_view.split.sizes = (2.0, 98.0)
        assert editor.mode == "dashboard"
        assert editor._split_view.preferences.sizes == DEFAULT_SIZES

    async def test_resizing_is_remembered(self, editor):
        editor.mode = "split"
        split_view = editor._split_view
        split_view.split.sizes = (70.0, 30.0)
        assert split_view.preferences.sizes == (70.0, 30.0)
        assert split_view.split.expanded_sizes == (70.0, 30.0)
        assert json.loads(split_view.storage.value) == {"split": True, "sizes": [70.0, 30.0]}
        editor.mode = "wiring"
        assert json.loads(split_view.storage.value)["split"] is False
        editor.mode = "split"
        assert split_view.split.sizes == (70.0, 30.0)


class TestStoredPreference:
    async def test_stored_split_opens_the_split_tab(self, editor):
        stored = json.dumps({"split": True, "sizes": [65, 35]})
        editor._split_view.storage._handle_msg({"type": "load", "value": stored})
        assert editor.mode == "split"
        assert editor._split_view.split.sizes == (65.0, 35.0)

    async def test_nothing_stored_keeps_the_tab(self, editor):
        editor.mode = "dashboard"
        editor._split_view.storage._handle_msg({"type": "load", "value": None})
        assert editor.mode == "dashboard"
        assert editor._split_view.storage.value is None


class TestCommandPalette:
    async def test_split_shortcut_is_listed(self):
        assert {
            "group": "Editing",
            "label": "Dataflow and Layout side by side",
            "keys": ["S"],
        } in (SHORTCUTS)

    async def test_action_toggles_the_split(self, app):  # noqa: F811
        _open(app, edit=True)
        controller = app._command_palette
        item = next(i for i in controller.items() if i["id"] == "action:split")
        assert (item["title"], item["keys"]) == ("Split view", ["S"])
        controller.run("shortcut:split")
        assert app._editor.mode == "split"
        item = next(i for i in controller.items() if i["id"] == "action:split")
        assert item["title"] == "Close split view"
        controller.run("action:split")
        assert app._editor.mode == "wiring"

    async def test_shortcut_does_nothing_in_view_mode(self, app):  # noqa: F811
        _open(app)
        app._command_palette.run("shortcut:split")
        assert app._editor.mode == "wiring"

    async def test_show_in_editor_keeps_the_split(self, app):  # noqa: F811
        _open(app)
        app._editor.mode = "split"
        app._show_in_editor("anything")
        assert app._editor.mode == "split"
        assert app._editor.editable
