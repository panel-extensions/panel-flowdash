"""Tests for arranging tiles in the Split tab, which arranges the band of the main layout."""

import pytest

from panel_flowdash.editor import FlowDash
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView}, notifications=False)


def _band(editor):
    return editor._tile_grid.preview_band


async def test_split_grid_is_arranged_like_the_layout_tab(editor):
    map_id = editor.add_component(MAP)
    editor.mode = "split"
    assert editor._tile_grid.editable
    assert editor._tile_grid.card
    assert not editor._inspect_tiles[map_id].header.visible


async def test_split_grid_previews_the_base_band_at_the_pane_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    # Without a recorded width TileGrid assumes the layout was arranged at the largest breakpoint.
    assert _band(editor) == "md"
    grid = editor._tile_grid
    assert (grid.min_width, grid.max_width) == (None, None)
    assert not grid.show_breakpoint_toolbar
    editor._tile_grid.reference_width = 1000
    assert _band(editor) == "sm"


async def test_custom_breakpoints_set_the_assumed_width():
    editor = FlowDash({MAP: MapView}, notifications=False, breakpoints=[600, 900])
    editor.add_component(MAP)
    editor._tile_grid.reference_width = 700
    editor.mode = "split"
    assert _band(editor) == "sm"


async def test_wiring_tab_lets_the_grid_follow_its_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.mode = "wiring"
    assert _band(editor) is None


async def test_width_arranged_in_the_layout_tab_applies_in_the_split(editor):
    editor.add_component(MAP)
    editor.mode = "dashboard"
    editor._tile_grid.reference_width = 1000
    editor.mode = "split"
    assert _band(editor) == "sm"


async def test_view_mode_grid_follows_the_page_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.editable = False
    assert _band(editor) is None
    assert not editor._tile_grid.editable


async def test_loaded_dashboard_width_applies_in_the_split(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    model = editor.to_model()
    model.reference_width = 1000
    editor.load_model(model)
    assert editor.mode == "split"
    assert _band(editor) == "sm"
