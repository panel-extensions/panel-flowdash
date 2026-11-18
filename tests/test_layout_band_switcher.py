"""Tests for the "Arrange for" band switcher of the Layout tab."""

import pytest
from panel_tiles import TileGrid

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_bands import (
    AUTOMATIC,
    BASE,
    CUSTOM,
    band_for_width,
    band_labels,
    band_names,
    band_sources,
    band_summary,
    base_band,
)
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView


@pytest.fixture
def editor():
    return FlowDash({MAP: MapView, CHART: ChartView}, notifications=False)


def _layout_tab(editor):
    editor.mode = "wiring"
    ids = [editor.add_component(MAP), editor.add_component(CHART)]
    editor.mode = "dashboard"
    editor.dirty = False
    return ids


def _custom_layout(n):
    return [{"index": i, "width": 100, "height": 200, "visible": True} for i in range(n)]


class TestBandMapping:
    @pytest.mark.parametrize(
        "breakpoints", [[], [768], [768, 1200], [600, 900, 1400], [1, 2, 3, 4, 5, 6]]
    )
    def test_labels_match_tile_grid(self, breakpoints):
        grid = TileGrid(breakpoints=breakpoints)
        assert band_labels(breakpoints) == grid.param.preview_band.objects[1:]

    @pytest.mark.parametrize(
        ("width", "band"),
        [(0, "xs"), (767, "xs"), (768, "sm"), (1199, "sm"), (1200, "md"), (3000, "md")],
    )
    def test_band_for_width(self, width, band):
        assert band_for_width([1200, 768], width) == band

    def test_no_breakpoints_have_no_bands(self):
        assert band_for_width([], 1000) is None
        assert band_names([]) == {}

    def test_base_band_holds_the_reference_width(self):
        assert base_band([768, 1200], None) == "md"
        assert base_band([768, 1200], 1000) == "sm"
        assert base_band([600, 900], None) == "md"

    @pytest.mark.parametrize(
        ("breakpoints", "names"),
        [
            ([768], ["Desktop", "Phone"]),
            ([768, 1200], ["Desktop", "Tablet", "Phone"]),
            ([600, 900, 1400], ["Desktop", "Laptop", "Tablet", "Phone"]),
            (
                [400, 600, 900, 1400],
                ["Desktop", "900-1399 px", "600-899 px", "400-599 px", "Phone"],
            ),
        ],
    )
    def test_names_widest_first(self, breakpoints, names):
        assert list(band_names(breakpoints).values()) == names
        assert list(band_names(breakpoints)) == band_labels(breakpoints)[::-1]

    def test_sources(self):
        sources = band_sources([768, 1200], None, {"sm": [{"x": 0}], "xs": []})
        assert sources == {"xs": AUTOMATIC, "sm": CUSTOM, "md": BASE}
        # A custom layout for the base band is ignored, as TileGrid does.
        assert band_sources([768, 1200], 1000, {"sm": [{"x": 0}]})["sm"] == BASE

    def test_summary(self):
        names = band_names([768, 1200])
        assert band_summary(names, {"md": BASE, "sm": AUTOMATIC, "xs": AUTOMATIC}) == (
            "Tablet and Phone follow the Desktop layout automatically."
        )
        assert band_summary(names, {"md": BASE, "sm": CUSTOM, "xs": AUTOMATIC}) == (
            "Tablet is arranged separately. Phone follows the Desktop layout automatically."
        )


class TestSwitcher:
    async def test_offers_the_bands_and_defaults_to_the_base_band(self, editor):
        _layout_tab(editor)
        panel = editor._layout_panel
        assert panel.band_select.options == {"Desktop": "md", "Tablet": "sm", "Phone": "xs"}
        assert panel.band_select.value == "md"
        assert editor._tile_grid.preview_band == "md"
        assert not editor._tile_grid.show_breakpoint_toolbar

    async def test_picking_a_band_previews_it(self, editor):
        _layout_tab(editor)
        panel = editor._layout_panel
        panel.band_select.value = "xs"
        assert panel.band == "xs"
        assert editor._tile_grid.preview_band == "xs"
        assert not editor.dirty

    async def test_split_keeps_the_base_band_and_layout_tab_its_own(self, editor):
        _layout_tab(editor)
        editor._layout_panel.band_select.value = "sm"
        editor.mode = "split"
        assert editor._tile_grid.preview_band == "md"
        editor.mode = "dashboard"
        assert editor._tile_grid.preview_band == "sm"
        assert editor._layout_panel.band_select.value == "sm"

    async def test_base_band_follows_the_reference_width(self, editor):
        _layout_tab(editor)
        editor._tile_grid.reference_width = 1000
        assert editor._tile_grid.preview_band == "sm"
        assert editor._layout_panel.band_select.value == "sm"

    async def test_changed_breakpoints_fall_back_to_the_base_band(self, editor):
        _layout_tab(editor)
        editor._layout_panel.band_select.value = "sm"
        editor._tile_grid.breakpoints = [900]
        assert editor._tile_grid.preview_band == "sm"
        assert editor._layout_panel.band_select.options == {"Desktop": "sm", "Phone": "xs"}
        editor._tile_grid.breakpoints = [768, 1200]
        assert editor._tile_grid.preview_band == "sm"

    async def test_hidden_without_breakpoints(self):
        editor = FlowDash({MAP: MapView}, notifications=False, breakpoints=[])
        editor.add_component(MAP)
        editor.mode = "dashboard"
        assert not editor._layout_panel._bands.visible
        assert editor._tile_grid.preview_band is None

    async def test_loading_a_dashboard_returns_to_the_base_band(self, editor):
        _layout_tab(editor)
        editor._layout_panel.band_select.value = "xs"
        editor.load_model(editor.to_model())
        assert editor._tile_grid.preview_band == "md"
        assert editor._layout_panel.band_select.value == "md"

    async def test_view_mode_follows_the_width(self, editor):
        _layout_tab(editor)
        editor._layout_panel.band_select.value = "xs"
        editor.editable = False
        assert editor._tile_grid.preview_band is None
        editor.editable = True
        assert editor._tile_grid.preview_band == "xs"

    async def test_only_narrower_bands_are_framed(self, editor):
        _layout_tab(editor)
        assert "fd-unframed-band" in editor._tile_grid.css_classes
        editor._layout_panel.band_select.value = "sm"
        assert "fd-unframed-band" not in editor._tile_grid.css_classes
        editor.mode = "split"
        assert "fd-unframed-band" in editor._tile_grid.css_classes


class TestCustomArrangements:
    async def test_status_and_reset(self, editor):
        _layout_tab(editor)
        panel = editor._layout_panel
        panel.band_select.value = "sm"
        assert not panel.reset_band_button.visible
        assert "Tablet and Phone follow the Desktop layout" in panel.band_summary.object
        assert panel.band_hint.visible

        editor._tile_grid.responsive_layouts = {"sm": _custom_layout(2)}
        assert panel.reset_band_button.visible
        assert panel.band_summary.object.startswith("Tablet is arranged separately.")

        editor.dirty = False
        panel._on_reset_band(None)
        assert editor._tile_grid.responsive_layouts == {}
        assert not panel.reset_band_button.visible
        assert editor.dirty

    async def test_base_band_has_no_reset_or_hint(self, editor):
        _layout_tab(editor)
        panel = editor._layout_panel
        assert not panel.reset_band_button.visible
        assert not panel.band_hint.visible

    async def test_custom_arrangement_is_saved(self, editor):
        map_id, _chart_id = _layout_tab(editor)
        editor._tile_grid.responsive_layouts = {"xs": _custom_layout(2)}
        model = editor.to_model()
        assert set(model.responsive_layouts) == {"xs"}
        assert map_id in {entry.get("instance_id") for entry in model.responsive_layouts["xs"]}
