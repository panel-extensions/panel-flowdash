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
    return [
        {"title": "No config"},
        {"config": {}},
        "nope",
        {"title": "Not JSON", "config": {"value": object()}},
        {"title": "Fine", "config": {}},
    ]


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


def test_failing_and_malformed_presets_are_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="panel_flowdash.ui.palette"):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            context=dict(SOURCES),
        )
        labels = [item["label"] for group in editor._palette_items() for item in group["items"]]
        editor._palette_panel.query = "o"
        editor._palette_panel.query = ""
    assert "Broken" in labels and "Odd" in labels and "Fine" in labels
    assert "No config" not in labels and "Not JSON" not in labels
    assert "palette_presets of 'Test/broken' failed" in caplog.text
    assert "malformed palette preset of 'Test/odd'" in caplog.text
    assert "is not JSON-serializable" in caplog.text
    assert caplog.text.count("palette_presets of 'Test/broken' failed") == 1


def test_searching_and_adding_do_not_ask_the_hook_again():
    calls = []

    def counted(context):
        calls.append(1)
        return source_presets(context)

    entry_editor = FlowDash(
        {
            "Test/data": register(
                page=False,
                component=True,
                title="Data",
                config=["ref", "table"],
                palette_presets=counted,
            )(type("Counted", (Data,), {}))
        },
        notifications=False,
        include_builtin_components=False,
        context=dict(SOURCES),
    )
    for query in ("o", "or", "orders", ""):
        entry_editor._palette_panel.query = query
    item = next(
        i
        for g in entry_editor._palette.items
        for i in g["items"]
        if i["label"] == "orders (sales)"
    )
    entry_editor._on_palette_clicked(item)
    entry_editor.undo()
    assert len(calls) == 1
    entry_editor.context["sources"] = {"stock": ["parts"]}
    entry_editor._palette_panel.refresh()
    assert len(calls) == 2
    assert "parts (stock)" in [i["label"] for g in entry_editor._palette.items for i in g["items"]]


SCANNED = """\
from panel_flowdash import register


def presets(context):
    return [{"title": f"{name} feed", "config": {"name": name}} for name in context.get("feeds", [])]


@register(page=False, component=True, title="Feed", provides=["rows"], palette_presets=presets)
def app(config):
    return "feed"
"""


def test_scan_flags_components_with_presets(tmp_path):
    from panel_flowdash.registry import build_registry

    section = tmp_path / "Data"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "feed.py").write_text(SCANNED)
    (section / "plain.py").write_text(
        SCANNED.replace("palette_presets=presets", "palette_presets=None")
    )
    registry = build_registry(tmp_path)
    flags = {
        app_id.split("/")[-1]: entry.metadata.presets_pending for app_id, entry in registry.items()
    }
    assert flags == {"feed": True, "plain": False}


async def test_editing_a_dashboard_imports_scanned_components_with_presets(tmp_path, monkeypatch):
    import sys

    import panel as pn

    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.dashboard_store import MemoryDashboardStore

    section = tmp_path / "Data"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "feed.py").write_text(SCANNED)
    monkeypatch.syspath_prepend(str(tmp_path))
    store = MemoryDashboardStore()
    app = FlowDashApp(project_dir=tmp_path, store=store, notifications=False)
    app._editor.context = {"feeds": ["north", "south"]}
    model = store.create_dashboard(app._user_id, "Feeds")
    monkeypatch.setattr(pn.state, "_location", None, raising=False)
    try:
        await app._load_dashboard(model.dashboard_id, edit=True)
        labels = [item["label"] for group in app._editor._palette.items for item in group["items"]]
        assert "north feed" in labels and "south feed" in labels
    finally:
        sys.modules.pop("Data.feed", None)
        sys.modules.pop("Data", None)
