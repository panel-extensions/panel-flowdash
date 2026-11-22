"""Tests for the Layout tab: the layout panel, the tile handles and the status bar."""

import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui import inspector
from panel_flowdash.ui.layout_panel import GRID_EDIT_CSS
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.ui.tile_layout import (
    HIDDEN,
    OffGrid,
    off_grid,
    tile_entry,
    to_columns,
    to_width,
    update_tile,
)


@register(
    page=False, component=True, title="Map", min_size={"height": 200}, max_size={"height": 600}
)
class MapView(Viewer):
    """A view with height bounds."""

    def __panel__(self):
        return pn.pane.Markdown("map")


@register(page=False, component=True, title="Chart")
class ChartView(Viewer):
    """A plain view."""

    def __panel__(self):
        return pn.pane.Markdown("chart")


@register(page=False, component=True, sidebar=True, title="State Filter")
class StateFilter(Viewer):
    """A widget shown beside the dashboard rather than as a tile."""

    state = param.String(default="TX")

    def __panel__(self):
        return pn.widgets.TextInput.from_param(self.param.state)


MAP, CHART, FILTER = "Demo/map", "Demo/chart", "Demo/filter"


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView, FILTER: StateFilter}, notifications=False)


def _place(editor, *component_ids) -> list[str]:
    """Place components on the canvas, then switch to the Layout tab as an author would."""
    editor.mode = "wiring"
    placed = [editor.add_component(component_id) for component_id in component_ids]
    editor.mode = "dashboard"
    return placed


def _texts(viewable) -> list[str]:
    texts = []
    for obj in pn.panel(viewable).select(lambda o: hasattr(o, "param")):
        if isinstance(obj, pn.pane.HTML):
            continue
        for name in ("object", "label"):
            value = getattr(obj, name, None) if name in obj.param else None
            if isinstance(value, str) and value:
                texts.append(value)
    return texts


class TestTileLayoutHelpers:
    async def test_inspector_reuses_the_shared_helpers(self):
        assert inspector.tile_entry is tile_entry
        assert inspector.update_tile is update_tile

    @pytest.mark.parametrize(
        ("width", "columns"), [(None, 12), (100, 12), (50, 6), (33.33, 4), (8, 1), (1, 1)]
    )
    async def test_widths_map_onto_twelve_columns(self, width, columns):
        assert to_columns(width) == columns

    async def test_columns_map_back_onto_widths(self):
        assert to_width(6) == 50.0
        assert to_width(7) == 58.33
        assert to_columns(to_width(7)) == 7

    async def test_off_grid_lists_sidebar_and_hidden_components(self, editor):
        map_id, chart_id, filter_id = _place(editor, MAP, CHART, FILTER)
        update_tile(editor, chart_id, visible=False)
        assert off_grid(editor) == [
            OffGrid(chart_id, "Chart", HIDDEN, hidden=True),
            OffGrid(filter_id, "State Filter", "in top bar", area="top"),
        ]
        assert [entry.restorable for entry in off_grid(editor)] == [True, False]
        assert map_id not in [entry.instance_id for entry in off_grid(editor)]

    async def test_off_grid_says_sidebar_without_a_controls_bar(self, editor):
        editor.controls_bar = False
        _, _, filter_id = _place(editor, MAP, CHART, FILTER)
        assert OffGrid(filter_id, "State Filter", "in sidebar", area="top") in off_grid(editor)


def _tile_options(editor, instance_id):
    select_on_canvas(editor._flow, node_id=instance_id)
    return editor._inspector.tile_options


class TestSidecarInLayout:
    async def test_shown_while_arranging(self, editor):
        editor.mode = "dashboard"
        assert editor._inspector.visible and editor._layout_panel.visible
        editor.mode = "wiring"
        assert not editor._layout_panel.visible
        editor.mode = "dashboard"
        editor.editable = False
        assert not editor._inspector.visible

    async def test_shows_the_tile_selected_on_the_canvas(self, editor):
        editor.mode = "wiring"
        editor.add_component(MAP)
        chart_id = editor.add_component(CHART)
        select_on_canvas(editor._flow, node_id=chart_id)
        editor.mode = "dashboard"
        assert editor._inspector._shown == ("node", chart_id)
        assert editor._inspector.tile_options.instance_id == chart_id

    async def test_width_and_height_edit_the_selected_tile(self, editor):
        (map_id,) = _place(editor, MAP)
        options = _tile_options(editor, map_id)
        assert options.width_select.value == "auto"
        assert options.height_select.value == "auto"
        # Only heights within the component's size hints are offered.
        assert list(options.height_select.options.values()) == ["auto", 200, 300, 400, 500, 600]
        options.width_select.value = 6
        options.height_select.value = 400
        entry = editor.to_model().tile_layout[0]
        assert (entry["width"], entry["height"]) == (50.0, 400)
        assert editor.dirty
        options.height_select.value = "auto"
        assert "height" not in editor.to_model().tile_layout[0]

    async def test_resized_tile_is_shown(self, editor):
        (map_id,) = _place(editor, MAP)
        options = _tile_options(editor, map_id)
        editor._tile_grid.layout = [{"index": 0, "width": 50.0, "height": 437}]
        assert options.width_select.value == 6
        assert options.height_select.value == 437
        assert options.height_select.options["437 px"] == 437

    async def test_selecting_another_tile_shows_its_size(self, editor):
        map_id, chart_id = _place(editor, MAP, CHART)
        update_tile(editor, chart_id, width=25.0)
        options = _tile_options(editor, chart_id)
        assert options.width_select.value == 3
        assert options.width_select.options["Quarter (3/12)"] == 3
        assert tile_entry(editor, map_id).get("width") is None

    async def test_hidden_component_is_listed_and_restorable(self, editor):
        _, chart_id, _ = _place(editor, MAP, CHART, FILTER)
        _tile_options(editor, chart_id).show_switch.value = False
        assert tile_entry(editor, chart_id)["visible"] is False
        select_on_canvas(editor._flow)
        panel = editor._layout_panel
        texts = _texts(panel._off_grid)
        assert texts[:2] == ["Chart", HIDDEN]
        assert "State Filter" in texts and "in top bar" in texts
        assert panel.off_grid_section in editor._inspector._body.objects
        show = [
            b
            for b in panel._off_grid.select(pn.viewable.Viewable)
            if getattr(b, "label", None) == "Show"
        ]
        assert len(show) == 1
        show[0].clicks += 1
        assert tile_entry(editor, chart_id)["visible"] is True
        assert "Chart" not in _texts(panel._off_grid)

    async def test_tile_hidden_in_the_browser_is_listed(self, editor):
        _place(editor, MAP, CHART)
        editor._tile_grid.layout = [{"index": 0}, {"index": 1, "visible": False}]
        assert _texts(editor._layout_panel._off_grid)[:2] == ["Chart", HIDDEN]

    async def test_empty_grid_explains_itself(self, editor):
        editor.mode = "dashboard"
        assert "Every placed component has a tile." in _texts(editor._layout_panel._off_grid)

    async def test_arrange_for_only_in_the_layout_tab(self, editor):
        _place(editor, MAP)
        section = editor._layout_panel.arrange_section
        assert section in editor._inspector._body.objects
        editor.mode = "wiring"
        assert section not in editor._inspector._body.objects


class TestLayoutGrid:
    async def test_grid_styles_its_edit_handles(self, editor):
        editor.mode = "dashboard"
        assert GRID_EDIT_CSS in editor._tile_grid.stylesheets
        assert editor._tile_grid.editable


class TestLayoutStatusBar:
    async def test_describes_the_grid_in_the_layout_tab(self, editor):
        _, chart_id, _ = _place(editor, MAP, CHART, FILTER)
        bar = editor._status_bar
        assert bar.visible and bar.grid_label.visible
        assert bar.grid_label.object == "12-column grid"
        assert bar.counts.object == "2 tiles, 1 component hidden"
        update_tile(editor, chart_id, visible=False)
        assert bar.counts.object == "1 tile, 2 components hidden"
        assert not bar.issues_button.visible
        assert not bar.view_controls.visible

    async def test_counts_components_in_the_dataflow_tab(self, editor):
        _place(editor, MAP)
        editor.mode = "wiring"
        bar = editor._status_bar
        assert not bar.grid_label.visible
        assert bar.issues_button.visible and bar.view_controls.visible
        assert bar.counts.object == "1 component, 0 connections"

    async def test_hidden_in_view_mode(self, editor):
        editor.editable = False
        assert not editor._status_bar.visible
