"""UI tests for the searchable component palette."""

import re

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


@register(
    page=False,
    component=True,
    title="Prices",
    provides=[{"key": "prices", "type": "DataFrame"}],
)
def prices(config):
    return "prices"


@register(
    page=False,
    component=True,
    title="Price Chart",
    requires=[{"key": "prices", "type": "DataFrame"}],
)
def price_chart(config):
    return "chart"


COMPONENTS = {"Test/prices": prices, "Test/chart": price_chart}


def _editor():
    return FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)


def _search(page):
    return page.get_by_placeholder("Search components")


def _item(page, title):
    """The palette entry titled *title*; port summaries may mention other titles."""
    primary = page.locator(".MuiListItemText-primary", has_text=re.compile(f"^{title}$"))
    return page.locator(".MuiListItemButton-root", has=primary)


def test_palette_groups_components_with_port_summaries(page):
    editor = _editor()
    editor.add_component("Test/prices", position=(0, 0))
    serve_component(page, editor)

    palette = page.locator(".MuiList-root").first
    expect(palette.get_by_text("Data sources", exact=True)).to_be_visible()
    expect(palette.get_by_text("Views", exact=True)).to_be_visible()
    expect(palette.get_by_text("in: prices (DataFrame)")).to_be_visible()
    prices_item = _item(page, "Prices")
    expect(prices_item).to_contain_text("Placed")
    expect(prices_item).to_contain_text("out: prices (DataFrame)")
    expect(page.get_by_text("drop on a port to add and connect")).to_be_visible()


def test_slash_focuses_search_and_typing_filters(page):
    editor = _editor()
    serve_component(page, editor)

    search = _search(page)
    expect(search).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    expect(search).to_be_focused()
    page.keyboard.type("chart")
    wait_until(lambda: editor._palette_panel.query == "chart", page=page, timeout=8000)
    expect(_item(page, "Price Chart")).to_be_visible()
    expect(_item(page, "Prices")).to_have_count(0)
    # The key typed into the field is not swallowed by the shortcut.
    expect(search).to_have_value("chart")

    page.keyboard.type(" nothing")
    expect(page.get_by_text("No components match")).to_be_visible()

    page.keyboard.press("Escape")
    wait_until(lambda: editor._palette_panel.query == "", page=page, timeout=8000)
    expect(search).to_have_value("")
    expect(_item(page, "Prices")).to_be_visible()


def test_slash_in_another_field_is_typed_not_captured(page):
    editor = _editor()
    other = pmui.TextInput(label="Other", placeholder="Other field")
    serve_component(page, pn.Column(other, editor))

    field = page.get_by_placeholder("Other field")
    field.click()
    page.keyboard.type("a/b")
    expect(field).to_have_value("a/b")
    expect(_search(page)).not_to_be_focused()
    expect(_search(page)).to_have_value("")


def test_drag_from_filtered_palette_adds_node(page):
    editor = _editor()
    serve_component(page, editor)

    _search(page).fill("chart")
    wait_until(lambda: editor._palette_panel.query == "chart", page=page, timeout=8000)
    item = _item(page, "Price Chart")
    expect(item).to_be_visible()
    item.drag_to(page.locator(".react-flow__pane"), target_position={"x": 80, "y": 80})
    wait_until(lambda: len(editor._tile_items) == 1, page=page, timeout=8000)
    assert editor._tile_items[0]["component_id"] == "Test/chart"
    expect(item).to_contain_text("Placed")
