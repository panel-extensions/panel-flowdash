"""Preset entries a component's ``palette_presets`` adds to the palette, each placing it with a config."""

import logging

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash


def source_presets(context):
    return [
        {
            "title": f"{table} ({ref})",
            "description": f"Table {table} of {ref}.",
            "tooltip": f"Source {ref}, table {table}",
            "tags": [ref],
            "config": {"ref": ref, "table": table},
        }
        for ref, tables in context.get("sources", {}).items()
        for table in tables
    ]


@register(
    page=False,
    component=True,
    title="Data",
    icon="storage",
    description="A table the host provides.",
    config=["ref", "table"],
    palette_presets=source_presets,
)
class Data(Viewer):
    ref = param.String(default="")
    table = param.String(default="")

    @param.output(param.String)
    def rows(self):
        return f"{self.ref}.{self.table}"

    def __panel__(self):
        return self.rows()


def broken_presets(context):
    raise RuntimeError("boom")


@register(page=False, component=True, title="Broken", palette_presets=broken_presets)
def broken(config):
    return "broken"


def odd_presets(context):
    return [{"title": "No config"}, {"config": {}}, "nope", {"title": "Fine", "config": {}}]


@register(page=False, component=True, title="Odd", palette_presets=odd_presets)
def odd(config):
    return "odd"


COMPONENTS = {"Test/data": Data, "Test/broken": broken, "Test/odd": odd}
SOURCES = {"sources": {"sales": ["orders", "customers"]}}


@pytest.fixture
def editor():
    return FlowDash(
        COMPONENTS, notifications=False, include_builtin_components=False, context=dict(SOURCES)
    )


def _items(editor):
    return [item for group in editor._palette.items for item in group["items"]]


def _path(editor, title):
    for i, group in enumerate(editor._palette.items):
        for j, item in enumerate(group["items"]):
            if item["label"] == title:
                return [i, j]
    raise AssertionError(f"{title} not in palette")


def test_presets_follow_their_component_with_its_ports(editor):
    items = [item for item in _items(editor) if item["component_id"] == "Test/data"]
    assert [item["label"] for item in items] == ["Data", "orders (sales)", "customers (sales)"]
    base, orders, _ = items
    assert "config" not in base
    assert orders["config"] == {"ref": "sales", "table": "orders"}
    assert orders["tooltip"] == "Source sales, table orders"
    assert orders["secondary"] == base["secondary"]
    assert orders["icon"] == base["icon"]


def test_presets_follow_the_context(editor):
    editor.context = {"sources": {"stock": ["parts"]}}
    labels = [item["label"] for group in editor._palette_items() for item in group["items"]]
    assert "parts (stock)" in labels and "orders (sales)" not in labels


def test_search_matches_preset_titles_and_tags(editor):
    editor._palette_panel.query = "customers"
    assert [item["label"] for item in _items(editor)] == ["customers (sales)"]
    editor._palette_panel.query = "sales"
    assert [item["label"] for item in _items(editor)] == ["orders (sales)", "customers (sales)"]


def test_clicking_a_preset_places_it_with_its_config(editor):
    item = next(item for item in _items(editor) if item["label"] == "orders (sales)")
    editor._on_palette_clicked(item)
    (tile,) = editor._tile_items
    assert tile["component_id"] == "Test/data"
    assert tile["config"] == {"ref": "sales", "table": "orders"}
    assert editor.graph.get_state(tile["instance_id"]).rows == "sales.orders"


def test_dropping_a_preset_places_it_with_its_config(editor):
    editor._flow._handle_msg(
        {
            "type": "drop",
            "drop_type": "application/x-flowdash-component",
            "data": {"path": _path(editor, "customers (sales)"), "label": ""},
            "position": {"x": 10, "y": 20},
            "target": None,
        }
    )
    (tile,) = editor._tile_items
    assert tile["config"] == {"ref": "sales", "table": "customers"}


def test_clicking_the_component_itself_places_it_unconfigured(editor):
    editor._on_palette_clicked(next(item for item in _items(editor) if item["label"] == "Data"))
    assert editor._tile_items[0]["config"] == {}


def test_failing_and_malformed_presets_are_skipped(editor, caplog):
    with caplog.at_level(logging.WARNING, logger="panel_flowdash.ui.palette"):
        labels = [item["label"] for group in editor._palette_items() for item in group["items"]]
    assert "Broken" in labels and "Odd" in labels and "Fine" in labels
    assert "No config" not in labels
    assert "palette_presets of 'Test/broken' failed" in caplog.text
    assert "malformed palette preset of 'Test/odd'" in caplog.text
