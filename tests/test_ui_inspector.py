"""Tests for the compact node cards, the inspector and the status bar of the wiring canvas."""

import itertools

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.placeholders import Placeholder
from panel_flowdash.ui.node_card import (
    CARD_WIDTH,
    NodeCard,
    card_height,
    placeholder_card,
    port_offsets,
    summarize_value,
)
from panel_flowdash.ui.status_bar import canvas_issues, select_on_canvas
from panel_flowdash.ui.text import escape_markdown
from tests.test_editor import CHART, COMPONENTS, FAILING_COMPONENTS, SELECTOR


@register(
    page=False, component=True, title="Rows", provides=[{"key": "rows", "type": "DataFrame"}]
)
class RowSource(Viewer):
    """Publishes a DataFrame."""

    size = param.Integer(default=3)

    @param.output(param.DataFrame)
    @param.depends("size")
    def rows(self):
        return pd.DataFrame({"a": range(self.size), "b": range(self.size)})

    def __panel__(self):
        return "rows"


@register(
    page=False,
    component=True,
    title="Table",
    description="Shows rows.",
    config=["title", "page_size"],
    default_size={"height": 300},
)
class Table(Viewer):
    """A view with config and one required DataFrame input."""

    rows = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )
    title = param.String(default="Turbines")
    page_size = param.Integer(default=10, bounds=(1, 100))

    def __panel__(self):
        return pn.pane.Markdown(self.param.title)


def custom_editor(data, schema, *, id, type, on_patch):
    button = pn.widgets.Button(label=f"custom {data['title']}")
    button.on_click(lambda _event: on_patch({"title": "From custom"}))
    return button


@register(
    page=False, component=True, title="Custom", config=["title"], config_editor=custom_editor
)
class Custom(Viewer):
    """A view whose config has its own editor."""

    title = param.String(default="Mine")

    def __panel__(self):
        return self.title


ROWS, TABLE, CUSTOM = "Demo/rows", "Demo/table", "Demo/custom"
TABLE_COMPONENTS = {ROWS: RowSource, TABLE: Table, CUSTOM: Custom}


@pytest.fixture
def editor():
    return FlowDash(TABLE_COMPONENTS, notifications=False)


def _card(editor, instance_id) -> NodeCard:
    return next(n["view"] for n in editor._flow.nodes if n["id"] == instance_id)


def _texts(viewables) -> list[str]:
    """Collect the text of every Typography, button and chip under *viewables*."""
    texts = []
    for viewable in viewables:
        for obj in pn.panel(viewable).select(lambda o: hasattr(o, "param")):
            for name in ("object", "label"):
                value = getattr(obj, name, None) if name in obj.param else None
                if isinstance(value, str) and value:
                    texts.append(value)
    return texts


class TestNodeCard:
    async def test_handles_and_labels_share_positions(self):
        assert port_offsets(1) == [50.0]
        assert port_offsets(3) == [25.0, 50.0, 75.0]

    @pytest.mark.parametrize("ports", [(0, 0), (1, 0), (1, 1), (2, 1), (3, 5)])
    async def test_rows_clear_header_and_summary(self, ports):
        height = card_height(*ports)
        for count in ports:
            offsets = [height * p / 100 for p in port_offsets(count)]
            assert all(34 + 12 <= o <= height - 30 - 12 for o in offsets)
            assert all(b - a >= 24 for a, b in itertools.pairwise(offsets))

    async def test_labels_types_and_required_marker(self, editor):
        table = editor.add_component(TABLE)
        card = _card(editor, table)
        html = card.__panel__().object
        assert card.title == "Table"
        assert card.inputs == [
            {"name": "rows", "label": "Rows", "type": "DataFrame", "required": True}
        ]
        assert 'data-port="rows"' in html and "DataFrame" in html
        assert 'class="fd-required"' in html
        assert "top: 50.0000%" in html
        assert card.__panel__().width == CARD_WIDTH
        assert card.__panel__().height == card_height(1, 0)

    async def test_node_shows_card_and_tile_shows_live_view(self, editor):
        table = editor.add_component(TABLE)
        node = next(n for n in editor._flow.nodes if n["id"] == table)
        assert isinstance(node["view"], NodeCard)
        assert editor._tile_objects[0] is not node["view"]
        assert not isinstance(editor._tile_objects[0], NodeCard)

    async def test_summary_follows_output(self, editor):
        src = editor.add_component(ROWS)
        card = _card(editor, src)
        assert card.summary == "3 rows, 2 columns"
        editor._component_instances[src].size = 1200
        assert card.summary == "1,200 rows, 2 columns"

    async def test_view_waits_then_summarizes_its_input(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        card = _card(editor, table)
        assert (card.status, card.status_detail) == ("waiting", "Needs Rows")
        editor.connect(src, "rows", table, "rows")
        assert card.status == "ok"
        assert card.summary == "Showing 3 rows, 2 columns"

    async def test_widget_summary_is_its_value(self):
        editor = FlowDash({}, notifications=False)
        select = editor.add_component("Widgets/Select", config={"default_options": ["A", "B"]})
        card = _card(editor, select)
        assert card.summary == "selected: A"
        editor._tile_objects[0].value = "B"
        assert card.summary == "selected: B"

    @pytest.mark.failures
    async def test_failed_input_marks_the_card(self):
        editor = FlowDash(FAILING_COMPONENTS, notifications=False, propagate_failures=True)
        src, dst = editor.add_component("Demo/inverse"), editor.add_component("Demo/inverse")
        editor.connect(src, "inverse", dst, "value")
        editor.graph.get_state(src).value = 4
        editor.graph.get_state(src).value = 0
        assert _card(editor, dst).status == "failed"
        assert _card(editor, src).status == "failed"

    async def test_placeholder_card(self):
        card = placeholder_card(Placeholder("missing", "Gone/widget", "Not available."))
        assert (card.status, card.title, card.summary) == (
            "unavailable",
            "Gone/widget",
            "Not available.",
        )
        assert card.inputs == card.outputs == []

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (None, "No value yet"),
            (pd.DataFrame({"a": [1, 2]}), "2 rows, 1 columns"),
            (["A", "B"], "A, B"),
            (list(range(10)), "10 items"),
            ({"a": 1}, "1 entries"),
            ("x" * 80, "x" * 59 + "…"),
        ],
    )
    async def test_summarize_value(self, value, text):
        assert summarize_value(value) == text

    async def test_cards_release_their_watchers_with_the_component(self, editor):
        src = editor.add_component(ROWS)
        state = editor.graph.get_state(src)
        card = _card(editor, src)
        editor.remove_component(src)
        state.rows = pd.DataFrame({"a": [1]})
        assert card.summary == "3 rows, 2 columns"


class TestCanvasOptions:
    async def test_config_is_edited_in_the_inspector_not_on_the_node(self, editor):
        node_id = editor.add_component(CUSTOM)
        assert editor._flow.editor_mode == "side"
        assert editor._flow.node_editors == {}
        # ReactFlow builds a node's editor once the node is selected.
        select_on_canvas(editor._flow, node_id=node_id)
        assert editor._flow._node_editors == {node_id: None}
        assert editor._flow._selected_editor is None

    async def test_popups_open_on_click(self, editor):
        assert editor.popup_trigger == editor._flow.popup_trigger == "click"

    async def test_color_mode_follows_the_page_theme(self, editor):
        editor._status_bar.issues_button.dark_theme = True
        assert editor._flow.color_mode == "dark"
        editor._status_bar.issues_button.dark_theme = False
        assert editor._flow.color_mode == "light"

    async def test_edge_click_leaves_the_value_to_the_inspector(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows")
        edge_id = editor._flow.edges[0]["id"]
        editor._on_edge_clicked({"edge_id": edge_id, "position": {"x": 1, "y": 1}}, editor._flow)
        assert editor._flow._value_popup is None


class TestInspector:
    async def test_shown_beside_the_canvas_only_while_editing_the_dataflow(self, editor):
        assert editor._inspector.visible and editor._status_bar.visible
        editor.mode = "dashboard"
        # The status bar stays and describes the tile grid in the Layout tab.
        assert not editor._inspector.visible and editor._status_bar.visible
        editor.mode = "wiring"
        editor.editable = False
        assert not editor._inspector.visible and not editor._status_bar.visible

    async def test_summary_without_a_selection(self, editor):
        editor.add_component(TABLE)
        texts = _texts(editor._inspector._body)
        assert "Untitled dashboard" in texts
        assert "1 component, 0 connections" in texts
        assert "Issues (1)" in texts
        assert "Table: Needs Rows" in texts

    async def test_selecting_a_node_shows_its_config(self, editor):
        table = editor.add_component(TABLE)
        select_on_canvas(editor._flow, node_id=table)
        body = editor._inspector._body
        texts = _texts(body)
        assert "Table" in texts and "Shows rows." in texts
        form = next(iter(pn.Column(*body).select(pn.Param)))
        assert form.object is editor.graph.get_config_state(table)
        assert [n for n in form._widgets if n != "_title"] == ["title", "page_size"]

    async def test_editing_config_updates_the_component(self, editor):
        table = editor.add_component(TABLE)
        editor.dirty = False
        select_on_canvas(editor._flow, node_id=table)
        form = next(iter(pn.Column(*editor._inspector._body).select(pn.Param)))
        form._widgets["title"].value = "Capacity"
        assert editor._component_instances[table].title == "Capacity"
        assert editor._tile_items[0]["config"] == {"title": "Capacity"}
        assert editor.dirty
        assert editor.to_model().items[0].config == {"title": "Capacity"}

    async def test_custom_config_editor(self, editor):
        custom = editor.add_component(CUSTOM)
        select_on_canvas(editor._flow, node_id=custom)
        button = next(iter(pn.Column(*editor._inspector._body).select(pn.widgets.Button)))
        assert button.label == "custom Mine"
        button.clicks += 1
        assert editor._component_instances[custom].title == "From custom"
        assert editor._tile_items[0]["config"] == {"title": "From custom"}

    async def test_status_and_data_follow_the_component(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        select_on_canvas(editor._flow, node_id=table)
        assert editor._inspector._status_chip.label == "Needs Rows"
        editor.connect(src, "rows", table, "rows")
        assert editor._inspector._status_chip.label == "Up to date"
        assert "3 rows, 2 columns" in _texts(editor._inspector._data_column)

    async def test_wired_inputs_list_source_and_transform(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows", [{"op": "head", "n": 2}])
        select_on_canvas(editor._flow, node_id=table)
        texts = _texts(editor._inspector._inputs_column)
        assert "1 of 1 connected" in texts
        assert any(t.startswith("from Rows, rows, with transform") for t in texts)
        assert "2 rows, 2 columns" in texts

    async def test_remove_from_dashboard(self, editor):
        table = editor.add_component(TABLE)
        select_on_canvas(editor._flow, node_id=table)
        remove = next(
            b
            for b in pn.Column(*editor._inspector._body).select(pn.viewable.Viewable)
            if getattr(b, "label", None) == "Remove from dashboard"
        )
        assert remove.color == "error"
        remove.clicks += 1
        assert editor._tile_items == []
        assert editor._flow.nodes == []
        assert "0 components, 0 connections" in _texts(editor._inspector._body)

    async def test_tile_settings_change_the_saved_layout(self, editor):
        table = editor.add_component(TABLE)
        select_on_canvas(editor._flow, node_id=table)
        tab = editor._inspector._tile_tab(
            table, editor._tile_items[0], editor._component_specs[TABLE]
        )
        show, width, height = tab[:3]
        assert height.value == 300
        show.value = False
        width.value = 50.0
        height.value = 400
        entry = editor.to_model().tile_layout[0]
        assert (entry["visible"], entry["width"], entry["height"]) == (False, 50.0, 400)
        width.value = "auto"
        assert "width" not in editor.to_model().tile_layout[0]

    async def test_edge_selection_hook(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows")
        edge_id = editor._flow.edges[0]["id"]
        select_on_canvas(editor._flow, edge_id=edge_id)
        texts = _texts(editor._inspector._body)
        assert "Rows to Table" in texts
        assert escape_markdown("In: 3 rows, 2 columns") in texts

    async def test_edge_view_can_be_replaced(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows")
        marker = pn.pane.Markdown("edge editor")
        editor._inspector._edge_view = lambda edge_id: [marker]
        select_on_canvas(editor._flow, edge_id=editor._flow.edges[0]["id"])
        assert editor._inspector._body.objects == [marker]

    async def test_collapse(self, editor):
        inspector = editor._inspector
        inspector.collapse_button.clicks += 1
        assert inspector.collapsed and inspector.__panel__().width < 60
        inspector.expand_button.clicks += 1
        assert not inspector.collapsed and inspector.__panel__().width == 360

    async def test_placeholder_selection(self, editor):
        editor.load_model(
            DashboardModel(
                dashboard_id="d",
                user_id="u",
                title="Kept",
                items=[DashboardItem(instance_id="gone", component_id="Gone/widget", x=0, y=0)],
            )
        )
        select_on_canvas(editor._flow, node_id="gone")
        texts = _texts(editor._inspector._body)
        assert "Gone/widget" in texts and "Remove from dashboard" in texts

    @pytest.mark.parametrize("component_id", ["Gone/widget", SELECTOR])
    async def test_placeholder_node_and_tile_are_separate_views(self, component_id):
        # A Panel object rendered in two places at once blanks the page in the
        # browser ("reference ... isn't known"); a restricted component is locked.
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            authorize_component=lambda entry: entry.app_id != SELECTOR,
        )
        editor.load_model(
            DashboardModel(
                dashboard_id="d",
                user_id="u",
                title="Kept",
                items=[DashboardItem(instance_id="p", component_id=component_id, x=0, y=0)],
            )
        )
        (item,) = editor._tile_items
        assert item["placeholder"].private == (component_id == SELECTOR)
        (node,) = editor._flow.nodes
        (tile,) = editor._tile_objects
        assert node["view"] is not tile
        assert all(view is not node["view"] for view in tile.select())


class TestStatusBar:
    async def test_counts_and_issues(self, editor):
        bar = editor._status_bar
        assert bar.issues_button.label == "All outputs up to date"
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        assert bar.counts.object == "2 components, 0 connections"
        assert bar.issues_button.label == "1 issue: Table"
        assert bar.issues_button.color == "warning"
        editor.connect(src, "rows", table, "rows")
        assert bar.counts.object == "2 components, 1 connection"
        assert bar.issues_button.label == "All outputs up to date"

    async def test_clicking_the_issues_selects_the_first(self, editor):
        editor.add_component(ROWS)
        table = editor.add_component(TABLE)
        editor._status_bar.issues_button.clicks += 1
        assert editor._flow.selection == {"nodes": [table], "edges": []}
        assert next(n for n in editor._flow.nodes if n["id"] == table)["selected"]
        assert editor._inspector._shown == ("node", table)

    async def test_issues_list_unresolved_edges(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        a, b = editor.add_component(SELECTOR), editor.add_component(CHART)
        editor.connect(a, "ticker", b, "ticker")
        issues = canvas_issues(editor)
        assert [(i.instance_id, i.status) for i in issues] == [(b, "waiting")]

    async def test_zoom(self, editor):
        editor._flow.viewport = {"x": 0, "y": 0, "zoom": 0.75}
        assert editor._status_bar.zoom.object == "Zoom 75%"

    async def test_minimap_is_off_until_toggled(self, editor):
        toggle = editor._status_bar.minimap_toggle
        assert not editor._flow.show_minimap
        assert not toggle.value
        toggle.value = True
        assert editor._flow.show_minimap
        editor._flow.show_minimap = False
        assert not toggle.value
