"""Tests for folding the editor's component palette beside the canvas."""

import json

import pytest

from panel_flowdash.editor import _SIDE_PANEL_WIDTH, FlowDash
from panel_flowdash.ui.drag_guard import HANDLES, DragGuard
from panel_flowdash.ui.palette_drawer import FOLDED_CLASS
from panel_flowdash.ui.sidecar import FOLDED_WIDTH
from panel_flowdash.ui.split_view import SplitPreferences
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView


def _editor(**params):
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView},
        notifications=False,
        include_builtin_components=False,
        **params,
    )
    editor.mode = "wiring"
    return editor


def _load(editor, value):
    editor._split_view.storage._handle_msg({"type": "load", "value": value})


class TestPreference:
    def test_open_by_default_and_stored(self):
        preferences = SplitPreferences()
        assert preferences.palette_open is True
        preferences.palette_open = False
        assert json.loads(preferences.to_json())["palette_open"] is False

    @pytest.mark.parametrize("value", [False, True])
    def test_loaded(self, value):
        preferences = SplitPreferences()
        assert preferences.load(json.dumps({"palette_open": value}))
        assert preferences.palette_open is value

    @pytest.mark.parametrize("value", ["no", 0, None])
    def test_malformed_is_ignored(self, value):
        preferences = SplitPreferences()
        assert not preferences.load(json.dumps({"palette_open": value}))
        assert preferences.palette_open is True


class TestFold:
    def test_folding_narrows_the_column_to_the_strip(self):
        editor = _editor()
        drawer = editor._palette_drawer
        assert editor._side_panel.width == _SIDE_PANEL_WIDTH
        assert drawer.fold_button.visible and not drawer.unfold_button.visible
        editor._palette_drawer.fold_button.clicks += 1
        assert not drawer.open
        assert editor._side_panel.width == FOLDED_WIDTH
        assert FOLDED_CLASS in editor._side_panel.css_classes
        assert drawer.unfold_button.visible
        assert drawer.bridge.collapsed
        assert drawer.bridge.focus_target == "unfold"
        drawer.unfold_button.clicks += 1
        assert drawer.open
        assert editor._side_panel.width == _SIDE_PANEL_WIDTH
        assert FOLDED_CLASS not in editor._side_panel.css_classes
        assert drawer.bridge.focus_target == "search"

    def test_the_fold_is_remembered(self):
        editor = _editor()
        editor.toggle_palette()
        stored = json.loads(editor._split_view.storage.value)
        assert stored["palette_open"] is False
        editor.toggle_palette()
        assert json.loads(editor._split_view.storage.value)["palette_open"] is True

    def test_a_stored_fold_applies_on_load(self):
        editor = _editor()
        _load(editor, json.dumps({"palette_open": False}))
        assert not editor._palette_drawer.open
        assert editor._side_panel.width == FOLDED_WIDTH

    def test_slash_from_the_browser_opens_and_focuses_the_search(self):
        editor = _editor()
        editor.toggle_palette()
        focus_count = editor._palette_drawer.bridge.focus_count
        editor._palette_drawer.bridge._handle_msg({"type": "open"})
        assert editor._palette_drawer.open
        assert editor._palette_drawer.bridge.focus_count == focus_count + 1
        assert editor._palette_drawer.bridge.focus_target == "search"

    def test_the_sidecar_counts_the_folded_width(self):
        editor = _editor()
        editor._split_view.storage.window_width = 1200
        room = editor._sidecar.room()
        editor.toggle_palette()
        assert editor._sidecar.room() == room + _SIDE_PANEL_WIDTH - FOLDED_WIDTH

    def test_the_phone_sheet_shows_a_folded_palette(self):
        editor = _editor()
        drawer = editor._palette_drawer
        editor.toggle_palette()
        editor._dashboard_bar._phone_query.phone = True
        region = editor._palette_panel.__panel__()
        assert region not in editor._side_panel.objects
        assert region.visible
        assert not drawer.unfold_button.visible
        assert not drawer.fold_button.visible
        editor._dashboard_bar._phone_query.phone = False
        assert region in editor._side_panel.objects
        assert FOLDED_CLASS in editor._side_panel.css_classes
        assert drawer.unfold_button.visible
        assert drawer.fold_button.visible


def test_drag_guard_covers_every_kind_of_drag():
    assert {
        ".muuri-handle",
        ".fd-control-handle",
        ".react-flow",
        ".fd-report-handle",
        "[draggable='true']",
    } <= set(HANDLES)
    assert json.dumps(list(HANDLES)) in DragGuard._esm


def test_the_editor_holds_one_drag_guard():
    editor = _editor()
    assert len(editor.__panel__().select(DragGuard)) == 1
