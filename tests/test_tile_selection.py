"""Tests for keeping the grid's selected tile and the selected node in step."""

import pytest

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui import status_bar
from panel_flowdash.ui.status_bar import select_on_canvas
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView}, notifications=False)


@pytest.fixture(params=["dashboard", "split"])
def placed(request, editor):
    editor.mode = "wiring"
    ids = [editor.add_component(MAP), editor.add_component(CHART)]
    editor.mode = request.param
    editor.dirty = False
    return editor, ids


def _selected(editor):
    return editor._selected_tile_id()


def _click(editor, instance_id):
    """Select a tile as a click, drag or resize on the grid does."""
    editor._tile_grid.selected = editor._grid_ids.index(instance_id)


def _nodes(editor):
    return (editor._flow.selection or {}).get("nodes") or []


class TestCanvasToGrid:
    async def test_node_selects_its_tile(self, placed):
        editor, (map_id, chart_id) = placed
        select_on_canvas(editor._flow, node_id=chart_id)
        assert _selected(editor) == chart_id
        assert editor._layout_panel.selected == chart_id
        select_on_canvas(editor._flow, node_id=map_id)
        assert _selected(editor) == map_id

    async def test_deselecting_the_node_deselects_the_tile(self, placed):
        editor, (map_id, _chart_id) = placed
        select_on_canvas(editor._flow, node_id=map_id)
        select_on_canvas(editor._flow)
        assert editor._tile_grid.selected is None

    async def test_edge_selection_deselects_the_tile(self, placed):
        editor, (map_id, _chart_id) = placed
        select_on_canvas(editor._flow, node_id=map_id)
        editor._flow.selection = {"nodes": [], "edges": ["e1"]}
        assert editor._tile_grid.selected is None

    async def test_several_nodes_keep_the_selected_tile(self, placed):
        editor, (map_id, chart_id) = placed
        select_on_canvas(editor._flow, node_id=chart_id)
        editor._flow.selection = {"nodes": [map_id, chart_id], "edges": []}
        assert _selected(editor) == chart_id
        editor._flow.selection = {"nodes": ["unknown", map_id], "edges": []}
        assert _selected(editor) == map_id

    async def test_node_without_a_tile_deselects(self, placed):
        editor, (map_id, _chart_id) = placed
        select_on_canvas(editor._flow, node_id=map_id)
        editor._flow.selection = {"nodes": ["not-on-the-grid"], "edges": []}
        assert editor._tile_grid.selected is None

    async def test_selection_made_in_another_tab_applies(self, editor):
        map_id = editor.add_component(MAP)
        select_on_canvas(editor._flow, node_id=map_id)
        editor.mode = "dashboard"
        assert _selected(editor) == map_id

    async def test_view_mode_has_no_selected_tile(self, placed):
        editor, (map_id, _chart_id) = placed
        select_on_canvas(editor._flow, node_id=map_id)
        editor.editable = False
        assert editor._tile_grid.selected is None
        editor.editable = True
        assert _selected(editor) == map_id


class TestGridToCanvas:
    async def test_tile_selects_its_node_and_inspects_it(self, placed):
        editor, (_map_id, chart_id) = placed
        _click(editor, chart_id)
        assert editor._flow.selection == {"nodes": [chart_id], "edges": []}
        assert editor._layout_panel.selected == chart_id
        if editor.mode == "split":
            assert editor._inspector._shown == ("node", chart_id)
        else:
            assert editor._layout_panel.tile_select.value == chart_id

    async def test_layout_panel_tile_selects_the_tile_and_node(self, placed):
        editor, (map_id, chart_id) = placed
        editor._layout_panel.tile_select.value = chart_id
        assert _selected(editor) == chart_id
        assert _nodes(editor) == [chart_id]
        editor._layout_panel.tile_select.value = map_id
        assert _selected(editor) == map_id
        assert _nodes(editor) == [map_id]

    async def test_closed_tile_keeps_its_node_selected(self, placed):
        editor, (map_id, _chart_id) = placed
        _click(editor, map_id)
        editor._tile_grid.selected = None
        assert _nodes(editor) == [map_id]

    async def test_tile_without_a_node_selects_nothing_on_the_canvas(self, placed, monkeypatch):
        editor, (map_id, chart_id) = placed
        select_on_canvas(editor._flow, node_id=chart_id)
        monkeypatch.setattr(editor, "_grid_ids", [*editor._grid_ids, "orphan"])
        editor._tile_grid.objects = [
            *editor._tile_grid.objects,
            editor._inspect_tiles[map_id].view,
        ]
        editor._tile_grid.selected = len(editor._grid_ids) - 1
        assert _nodes(editor) == [chart_id]

    async def test_view_mode_clicks_select_nothing(self, placed):
        editor, (map_id, _chart_id) = placed
        editor.editable = False
        _click(editor, map_id)
        assert _nodes(editor) == []

    async def test_selection_is_not_a_change(self, placed):
        editor, (map_id, chart_id) = placed
        _click(editor, map_id)
        select_on_canvas(editor._flow, node_id=chart_id)
        editor._layout_panel.tile_select.value = map_id
        assert not editor.dirty


class TestReentrancy:
    @pytest.fixture
    def canvas_selects(self, monkeypatch):
        calls = []
        original = select_on_canvas

        def counting(flow, node_id=None, edge_id=None):
            calls.append(node_id)
            original(flow, node_id=node_id, edge_id=edge_id)

        monkeypatch.setattr(status_bar, "select_on_canvas", counting)
        monkeypatch.setattr("panel_flowdash.editor.select_on_canvas", counting)
        return calls

    async def test_canvas_selection_does_not_select_on_the_canvas_again(
        self, placed, canvas_selects
    ):
        editor, (map_id, _chart_id) = placed
        editor._flow.selection = {"nodes": [map_id], "edges": []}
        assert _selected(editor) == map_id
        assert canvas_selects == []

    async def test_tile_click_selects_on_the_canvas_once(self, placed, canvas_selects):
        editor, (map_id, chart_id) = placed
        _click(editor, map_id)
        _click(editor, chart_id)
        assert canvas_selects == [map_id, chart_id]

    async def test_clicking_the_selected_component_does_not_select_it_again(
        self, placed, canvas_selects
    ):
        editor, (map_id, _chart_id) = placed
        select_on_canvas(editor._flow, node_id=map_id)
        canvas_selects.clear()
        editor._tile_grid.selected = None
        _click(editor, map_id)
        assert canvas_selects == []

    async def test_rebuilding_the_grid_keeps_the_selection(self, placed, canvas_selects):
        editor, (map_id, chart_id) = placed
        _click(editor, chart_id)
        canvas_selects.clear()
        editor.mode = "wiring"
        editor.remove_component(map_id)
        editor.mode = "dashboard"
        assert _selected(editor) == chart_id
        assert editor._tile_grid.selected == 0
        assert canvas_selects == []

    async def test_tile_classes_are_left_alone(self, placed):
        # The agent's change outlines are classes on the tile; the selection is not.
        editor, (map_id, _chart_id) = placed
        before = list(editor._inspect_tiles[map_id].css_classes)
        _click(editor, map_id)
        assert editor._inspect_tiles[map_id].css_classes == before
