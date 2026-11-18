"""Tests for the tile grid in the Split tab, which shows the dashboard at the pane's width."""

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


async def test_split_grid_follows_the_pane_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    grid = editor._tile_grid
    assert _band(editor) is None
    assert (grid.min_width, grid.max_width) == (None, None)
    assert not grid.show_breakpoint_toolbar
    # A recorded width moves the Layout tab's base band, not the Split tab's.
    grid.reference_width = 1000
    assert _band(editor) is None
    editor.mode = "dashboard"
    assert _band(editor) == "sm"


async def test_split_grid_hides_the_tile_handles(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    assert "fd-split-preview" in editor._tile_grid.css_classes
    editor.mode = "dashboard"
    assert "fd-split-preview" not in editor._tile_grid.css_classes


async def test_wiring_tab_lets_the_grid_follow_its_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.mode = "wiring"
    assert _band(editor) is None
    assert "fd-split-preview" not in editor._tile_grid.css_classes


async def test_view_mode_grid_follows_the_page_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    editor.editable = False
    assert _band(editor) is None
    assert not editor._tile_grid.editable
    assert "fd-split-preview" not in editor._tile_grid.css_classes


async def test_loaded_dashboard_keeps_the_split_following_its_width(editor):
    editor.add_component(MAP)
    editor.mode = "split"
    model = editor.to_model()
    model.reference_width = 1000
    editor.load_model(model)
    assert editor.mode == "split"
    assert _band(editor) is None
