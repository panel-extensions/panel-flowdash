"""The palette's query and its search field stay in sync without writing value_input."""

import pytest

from panel_flowdash.ui.palette import ComponentPalette


@pytest.fixture
def palette():
    return ComponentPalette(drag_type="test", items=list, on_click=lambda item: None)


def _type(widget, text):
    widget._process_events({"value_input": text})


def test_typing_sets_the_query(palette):
    _type(palette.search, "chart")
    assert palette.query == "chart"


def test_setting_the_query_fills_the_field(palette):
    palette.query = "chart"
    assert palette.search.value_input == "chart"
    assert palette.search.value == "chart"


def test_clearing_the_query_clears_typed_text(palette):
    _type(palette.search, "chart")
    palette.query = ""
    assert palette.search.value_input == ""


def test_setting_the_committed_value_again_replaces_typed_text(palette):
    palette.query = "chart"
    _type(palette.search, "chartx")
    palette.query = "chart"
    assert palette.search.value_input == "chart"


def test_typing_does_not_commit_the_value(palette):
    _type(palette.search, "chart")
    assert palette.search.value == ""
