"""Tests for arranging tiles in the Split tab, at the width of the dashboard's layout."""

import pytest

from panel_flowdash.editor import FlowDash
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView}, notifications=False)


def _grid_width(editor):
    grid = editor._tile_grid
    assert grid.min_width == grid.max_width
    return grid.min_width


async def test_split_grid_is_arranged_like_the_layout_tab(editor):
    map_id = editor.add_component(MAP)
    editor.mode = "split"
    assert editor._tile_grid.editable
    assert editor._tile_grid.card
    assert not editor._inspect_tiles[map_id].header.visible


async def test_split_grid_keeps_the_reference_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    # Without a recorded width TileGrid assumes the layout was arranged at the largest breakpoint.
    assert _grid_width(editor) == 1200
    editor._tile_grid.reference_width = 1440
    assert _grid_width(editor) == 1440


async def test_custom_breakpoints_set_the_assumed_width():
    editor = FlowDash({MAP: MapView}, notifications=False, breakpoints=[600, 900])
    editor.add_component(MAP)
    editor.mode = "split"
    assert _grid_width(editor) == 900


@pytest.mark.parametrize("mode", ["dashboard", "wiring"])
async def test_other_tabs_let_the_grid_fill_the_workspace(editor, mode):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.mode = mode
    assert _grid_width(editor) is None


async def test_width_arranged_in_the_layout_tab_applies_in_the_split(editor):
    editor.add_component(MAP)
    editor.mode = "dashboard"
    editor._tile_grid.reference_width = 1000
    assert _grid_width(editor) is None
    editor.mode = "split"
    assert _grid_width(editor) == 1000


async def test_view_mode_grid_fills_the_page(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.editable = False
    assert _grid_width(editor) is None
    assert not editor._tile_grid.editable


async def test_loaded_dashboard_width_applies_in_the_split(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    model = editor.to_model()
    model.reference_width = 1320
    editor.load_model(model)
    assert editor.mode == "split"
    assert _grid_width(editor) == 1320
