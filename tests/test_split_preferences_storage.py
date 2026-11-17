"""Tests for reading the Split tab's preferences back from the browser."""

import json

import panel as pn
import pytest

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui import split_view
from panel_flowdash.ui.split_view import DEFAULT_SIZES, STORAGE_KEY, SplitPreferences, valid_sizes
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView

STORED_SPLIT = json.dumps({"split": True, "sizes": [70, 30]})


@pytest.mark.parametrize(
    "raw",
    [
        '{"sizes": [NaN, 50]}',
        '{"sizes": [Infinity, 1]}',
        '{"sizes": [-Infinity, 1]}',
        '{"sizes": [1e308, 1e308]}',
        '{"sizes": [-60, 160]}',
    ],
)
def test_unusable_sizes_are_ignored(raw):
    preferences = SplitPreferences()
    assert not preferences.load(raw)
    assert preferences.sizes == DEFAULT_SIZES


def test_negative_sizes_are_unusable():
    assert valid_sizes([-60, 160]) is None
    assert valid_sizes([60, 40]) == (60.0, 40.0)


def _editor(**params):
    return FlowDash(
        {MAP: MapView, CHART: ChartView},
        notifications=False,
        include_builtin_components=False,
        **params,
    )


def _load(editor, value=STORED_SPLIT):
    editor._split_view.storage._handle_msg({"type": "load", "value": value})


class TestStoredSplit:
    def test_reopens_the_split(self):
        editor = _editor()
        _load(editor)
        assert editor.mode == "split"
        assert editor._split_view.split.sizes == (70.0, 30.0)

    def test_an_explicit_mode_wins(self):
        editor = _editor(mode="dashboard")
        _load(editor)
        assert editor.mode == "dashboard"
        # The remembered sizes still apply for when the split is opened.
        assert editor._split_view.preferences.sizes == (70.0, 30.0)

    def test_a_tab_chosen_before_the_preferences_arrive_wins(self):
        editor = _editor()
        editor.mode = "dashboard"
        _load(editor)
        assert editor.mode == "dashboard"

    def test_waits_for_the_editor_to_become_editable(self):
        editor = _editor(editable=False)
        _load(editor)
        assert editor.mode == "wiring"
        editor.editable = True
        assert editor.mode == "split"

    def test_applies_once_per_session(self):
        editor = _editor()
        _load(editor, json.dumps({"split": False, "sizes": [60, 40]}))
        # A re-render reads storage again, possibly after another tab wrote to it.
        _load(editor)
        assert editor.mode == "wiring"
        assert editor._split_view.preferences.sizes == (60.0, 40.0)


class TestStorageKey:
    def test_an_app_at_the_root_keeps_the_plain_key(self, monkeypatch):
        monkeypatch.setattr(type(pn.state), "app_url", property(lambda self: "/"))
        assert split_view.storage_key() == STORAGE_KEY

    def test_without_a_server_the_plain_key(self):
        assert split_view.storage_key() == STORAGE_KEY

    def test_apps_on_one_origin_get_their_own_key(self, monkeypatch):
        monkeypatch.setattr(type(pn.state), "app_url", property(lambda self: "/plant/"))
        assert split_view.storage_key() == f"{STORAGE_KEY}:/plant"
        assert _editor()._split_view.storage.key == f"{STORAGE_KEY}:/plant"
