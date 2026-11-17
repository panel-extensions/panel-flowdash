"""Tests for placing the dashboard controls around the tile grid in every tab."""

import panel as pn
import pytest
from bokeh.document import Document

from panel_flowdash.editor import FlowDash
from tests.test_ui_layout_tab import CHART, FILTER, MAP, ChartView, MapView, StateFilter

COMPONENTS = {MAP: MapView, CHART: ChartView, FILTER: StateFilter}


def _editor(**params):
    editor = FlowDash(COMPONENTS, notifications=False, **params)
    for component in (FILTER, MAP, CHART):
        editor.add_component(component)
    return editor


def _grid_pane(editor) -> list:
    """Return everything in the Split tab's grid pane, one level deep."""
    right = list(editor._split_view.right)
    return right + [o for r in right if hasattr(r, "objects") for o in r.objects]


class TestControlsInTheSplit:
    def test_left_controls_sit_above_the_grid_in_the_split(self):
        editor = _editor(controls_position="left")
        editor.mode = "split"
        controls = editor._view_controls
        pane = _grid_pane(editor)
        assert controls._row in pane and controls._row.visible
        assert controls._panel not in pane and not controls._panel.visible
        assert editor._tile_grid in pane
        assert controls._row.objects[0].objects == list(controls._wrappers.values())

    def test_switching_to_left_in_the_split_keeps_the_controls(self):
        editor = _editor()
        editor.mode = "split"
        editor.settings.controls_position = "left"
        controls = editor._view_controls
        assert controls._row in _grid_pane(editor)
        assert controls._row.visible
        assert len(controls._controls) == 1

    def test_leaving_the_split_restores_the_left_panel(self):
        editor = _editor(controls_position="left")
        editor.mode = "split"
        editor.mode = "dashboard"
        controls = editor._view_controls
        assert editor._workspace_area.objects[0] is controls._left_row
        assert controls._panel.visible and not controls._row.visible
        assert editor._tile_grid in controls._grid_slot

    def test_switching_position_in_the_dashboard_tab_lays_out_again(self):
        editor = _editor(mode="dashboard")
        editor.settings.controls_position = "left"
        assert editor._workspace_area.objects[0] is editor._view_controls._left_row
        editor.settings.controls_position = "top"
        assert editor._workspace_area.objects[0] is editor._view_controls._row


@pytest.mark.parametrize("position", ["top", "left"])
def test_tiles_keep_their_models_across_mode_changes(position):
    editor = _editor(controls_position=position, editable=False)
    pn.panel(editor).get_root(Document())

    def models():
        grid = {id(m) for m, _ in editor._tile_grid._models.values()}
        tiles = {
            id(m) for tile in editor._inspect_tiles.values() for m, _ in tile._models.values()
        }
        return grid, tiles

    before = models()
    assert before[0] and before[1]
    for change in (
        {"editable": True, "mode": "dashboard"},
        {"editable": False},
        {"editable": True},
        {"preview": True},
        {"preview": False},
        {"editable": False},
    ):
        editor.param.update(**change)
        assert models() == before, change
