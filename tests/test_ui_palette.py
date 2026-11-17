"""Tests for the searchable component palette, grouped by role."""

import textwrap

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.palette import (
    EMPTY_MESSAGE,
    PaletteEntry,
    PalettePort,
    palette_entry,
    palette_items,
)
from panel_flowdash.ui.roles import component_role


@register(
    page=False,
    component=True,
    title="Prices",
    icon="storage",
    description="Daily closing prices.",
    provides=[{"key": "prices", "type": "DataFrame"}],
)
def prices(config):
    return "prices"


@register(
    page=False,
    component=True,
    title="Ticker Filter",
    tags=["finance"],
    requires=[{"key": "prices", "type": "DataFrame"}, {"key": "ticker", "type": "str"}],
    provides=[{"key": "filtered", "type": "DataFrame"}],
)
def ticker_filter(config):
    return "filter"


@register(
    page=False,
    component=True,
    title="Price Chart",
    requires=[{"key": "filtered", "type": "DataFrame"}],
)
def price_chart(config):
    return "chart"


@register(page=False, component=True, title="Header", singleton=True)
def header(config):
    return "header"


@register(
    page=False,
    component=True,
    title="Live Feed",
    tags=["Source"],
    requires=[{"key": "interval", "type": "int"}],
    provides=[{"key": "ticks", "type": "DataFrame"}],
)
def live_feed(config):
    return "feed"


class Shouter(Viewer):
    text = param.String(default="")

    @param.output(param.String)
    def shouted(self):
        return self.text.upper()

    def __panel__(self):
        return self.text


COMPONENTS = {
    "Demo/prices": prices,
    "Demo/filter": ticker_filter,
    "Demo/chart": price_chart,
    "Demo/header": header,
    "Demo/feed": live_feed,
    "Demo/shouter": Shouter,
}


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False)


def _groups(editor):
    return {
        group["label"]: [item["component_id"] for item in group["items"]]
        for group in editor._palette.items
    }


def _item(editor, component_id):
    for group in editor._palette.items:
        for item in group["items"]:
            if item["component_id"] == component_id:
                return item
    raise AssertionError(f"{component_id} not in palette")


def _path(editor, component_id):
    for i, group in enumerate(editor._palette.items):
        for j, item in enumerate(group["items"]):
            if item["component_id"] == component_id:
                return [i, j]
    raise AssertionError(f"{component_id} not in palette")


def _drop(editor, path, position=(10, 20)):
    editor._flow._handle_msg(
        {
            "type": "drop",
            "drop_type": "application/x-flowdash-component",
            "data": {"path": path, "label": ""},
            "position": {"x": position[0], "y": position[1]},
            "target": None,
        }
    )


class TestRoles:
    def test_groups_follow_ports_in_order(self, editor):
        assert _groups(editor) == {
            "Data sources": ["Demo/prices", "Demo/feed"],
            "Transforms": ["Demo/filter", "Demo/shouter"],
            "Views": ["Demo/chart", "Demo/header"],
            "Widgets": ["Widgets/Select", "Widgets/MultiChoice", "Widgets/Slider"],
        }

    def test_groups_are_headers_that_cannot_be_dragged(self, editor):
        for group in editor._palette.items:
            assert group["draggable"] is False
            assert group["selectable"] is False
            assert "component_id" not in group

    @pytest.mark.parametrize(
        ("tag", "role"),
        [
            ("source", "source"),
            ("Data source", "source"),
            ("transform", "transform"),
            ("VIEW", "view"),
            ("widget", "widget"),
        ],
    )
    def test_role_tag_overrides_ports(self, tag, role):
        entry = RegistryEntry.from_app(
            register(page=False, component=True, tags=["misc", tag])(lambda config: None),
            app_id="Demo/tagged",
        )
        ports = (PalettePort("a"),)
        assert component_role(entry, ports, ports) == role

    def test_unrelated_tags_keep_the_structural_role(self):
        entry = RegistryEntry.from_app(ticker_filter, app_id="Demo/filter")
        assert component_role(entry, (PalettePort("a"),), ()) == "view"
        assert component_role(entry, (), (PalettePort("a"),)) == "source"
        assert component_role(entry, (PalettePort("a"),), (PalettePort("b"),)) == "transform"

    def test_builtin_widget_overridden_by_project_component_is_not_a_widget(self):
        editor = FlowDash({"Widgets/Select": price_chart}, notifications=False)
        assert "Widgets/Select" in _groups(editor)["Views"]


class TestItems:
    def test_item_shows_icon_title_and_port_summary(self, editor):
        item = _item(editor, "Demo/filter")
        assert item["label"] == "Ticker Filter"
        assert item["secondary"] == "in: prices, ticker, out: filtered (DataFrame)"
        assert item["draggable"] is True

    def test_declared_icon_wins_over_the_role_icon(self, editor):
        assert _item(editor, "Demo/prices")["icon"] == "storage"
        assert _item(editor, "Demo/chart")["icon"] == "insert_chart"

    def test_description_is_the_tooltip(self, editor):
        assert _item(editor, "Demo/prices")["tooltip"] == "Daily closing prices."

    def test_summary_types_the_last_group_only(self):
        entry = PaletteEntry(
            component_id="x",
            title="X",
            role="transform",
            inputs=(PalettePort("dataset", "DataFrame"),),
            outputs=(PalettePort("filtered", "DataFrame"), PalettePort("count")),
        )
        assert entry.summary == "in: dataset, out: filtered (DataFrame), count"

    def test_view_summary_types_its_inputs(self, editor):
        assert _item(editor, "Demo/chart")["secondary"] == "in: filtered (DataFrame)"

    def test_component_without_ports_has_no_summary(self, editor):
        assert _item(editor, "Demo/header")["secondary"] is None


class TestPlaced:
    def test_placed_component_is_marked_and_stays_draggable(self, editor):
        editor.add_component("Demo/filter")
        item = _item(editor, "Demo/filter")
        assert item["secondary"].startswith(":material/check_circle@color=primary: Placed, in:")
        assert item["draggable"] is True
        assert "Placed" not in (_item(editor, "Demo/chart")["secondary"] or "")

    def test_placed_singleton_is_marked_and_not_draggable(self, editor):
        editor.add_component("Demo/header")
        item = _item(editor, "Demo/header")
        assert item["secondary"] == ":material/check_circle@color=primary: Placed"
        assert item["draggable"] is False

    def test_marker_goes_when_the_component_is_removed(self, editor):
        instance = editor.add_component("Demo/filter")
        editor.remove_component(instance)
        assert "Placed" not in _item(editor, "Demo/filter")["secondary"]


class TestSearch:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("ticker filter", ["Demo/filter"]),
            ("closing", ["Demo/prices"]),
            ("FINANCE", ["Demo/filter"]),
            ("ticks", ["Demo/feed"]),
            ("shouted", ["Demo/shouter"]),
            ("dataframe prices", ["Demo/prices", "Demo/filter"]),
        ],
    )
    def test_matches_title_description_tags_and_ports(self, editor, query, expected):
        editor._palette_panel.query = query
        found = [cid for ids in _groups(editor).values() for cid in ids]
        assert found == expected

    def test_search_drops_empty_groups(self, editor):
        editor._palette_panel.query = "chart"
        assert list(_groups(editor)) == ["Views"]
        assert editor._palette.expanded == [(0,)]

    def test_no_match_shows_message(self, editor):
        editor._palette_panel.query = "nothing like this"
        assert editor._palette.items == []
        assert editor._palette_panel._empty.visible
        assert editor._palette_panel._empty.object == EMPTY_MESSAGE

    def test_clearing_the_query_restores_every_component(self, editor):
        everything = editor._palette.items
        editor._palette_panel.query = "chart"
        editor._palette_panel.query = ""
        assert editor._palette.items == everything
        assert not editor._palette_panel._empty.visible

    def test_empty_palette_without_query_shows_no_message(self):
        editor = FlowDash(notifications=False, include_builtin_components=False)
        assert editor._palette.items == []
        assert not editor._palette_panel._empty.visible

    def test_typing_in_the_field_filters(self, editor):
        editor._palette_panel.search._process_events({"value_input": "slider"})
        assert _groups(editor) == {"Widgets": ["Widgets/Slider"]}

    def test_placing_keeps_the_filter(self, editor):
        editor._palette_panel.query = "chart"
        editor.add_component("Demo/chart")
        assert _groups(editor) == {"Views": ["Demo/chart"]}

    def test_drop_from_filtered_palette_adds_the_dragged_component(self, editor):
        editor._palette_panel.query = "chart"
        _drop(editor, _path(editor, "Demo/chart"))
        assert [i["component_id"] for i in editor._tile_items] == ["Demo/chart"]

    def test_click_from_filtered_palette_adds_the_component(self, editor):
        editor._palette_panel.query = "feed"
        path = _path(editor, "Demo/feed")
        item = editor._palette.items[path[0]]["items"][path[1]]
        editor._palette._process_click({}, tuple(path), item)
        assert [i["component_id"] for i in editor._tile_items] == ["Demo/feed"]


def _write_project(root):
    section = root / "LazyPalette"
    section.mkdir()
    (section / "declared.py").write_text(
        textwrap.dedent(
            """
            from panel_flowdash import register

            @register(
                page=False,
                component=True,
                title="Declared",
                provides=[{"key": "rows", "type": "DataFrame"}],
            )
            def app(config):
                return "declared"
            """
        )
    )
    (section / "viewer.py").write_text(
        textwrap.dedent(
            """
            import param
            from panel.viewable import Viewer
            from panel_flowdash import register

            @register(component=True, page=False, title="Introspected")
            class app(Viewer):
                rows = param.DataFrame()

                def __panel__(self):
                    return "viewer"
            """
        )
    )


class TestLazyComponents:
    def test_declared_ports_are_used_before_import(self, tmp_path):
        _write_project(tmp_path)
        editor = FlowDash(tmp_path, notifications=False, include_builtin_components=False)
        assert editor._component_specs == {}
        assert _groups(editor) == {
            "Data sources": ["LazyPalette/declared"],
            "Components": ["LazyPalette/viewer"],
        }
        assert _item(editor, "LazyPalette/declared")["secondary"] == "out: rows (DataFrame)"
        assert _item(editor, "LazyPalette/viewer")["secondary"] is None

    def test_loading_specs_refreshes_the_palette(self, tmp_path):
        _write_project(tmp_path)
        editor = FlowDash(tmp_path, notifications=False, include_builtin_components=False)
        editor.ensure_components_loaded()
        assert _groups(editor) == {
            "Data sources": ["LazyPalette/declared"],
            "Views": ["LazyPalette/viewer"],
        }
        assert _item(editor, "LazyPalette/viewer")["secondary"] == "in: rows (DataFrame)"


def test_palette_entry_falls_back_to_metadata_description():
    entry = RegistryEntry.from_app(prices, app_id="Demo/prices")
    assert palette_entry(entry).description == "Daily closing prices."


def test_palette_items_keeps_role_order():
    entries = [
        PaletteEntry(component_id="w", title="W", role="widget"),
        PaletteEntry(component_id="v", title="V", role="view"),
        PaletteEntry(component_id="s", title="S", role="source"),
    ]
    assert [g["label"] for g in palette_items(entries)] == ["Data sources", "Views", "Widgets"]


class TestAuthorization:
    def test_restricted_component_stays_out_of_groups_and_search(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            authorize_component=lambda entry: entry.app_id != "Demo/filter",
        )
        assert "Demo/filter" not in _groups(editor)["Transforms"]
        editor._palette_panel.query = "ticker"
        assert editor._palette.items == []
        assert editor._palette_panel._empty.visible

    def test_changing_the_rule_updates_the_filtered_palette(self, editor):
        editor._palette_panel.query = "dataframe"
        assert "Demo/filter" in _groups(editor)["Transforms"]
        editor.authorize_component = lambda entry: entry.app_id != "Demo/filter"
        assert "Transforms" not in _groups(editor)
