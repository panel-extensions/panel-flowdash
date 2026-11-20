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
    async def test_rows_are_spread_evenly(self):
        assert port_offsets(1) == [50.0]
        assert port_offsets(3) == [25.0, 50.0, 75.0]

    @pytest.mark.parametrize("ports", [(0, 0), (1, 0), (1, 1), (2, 1), (3, 5)])
    async def test_rows_clear_header_and_summary(self, ports):
        from panel_flowdash.ui.node_card import port_tops

        height = card_height(*ports)
        tops_in, tops_out = port_tops(*ports)
        all_tops = [*tops_in, *tops_out]
        assert all(44 + 11 <= o <= height - 34 - 11 for o in all_tops)
        assert all(b - a == 22 for a, b in itertools.pairwise(all_tops))

    async def test_labels_types_and_required_marker(self, editor):
        table = editor.add_component(TABLE)
        card = _card(editor, table)
        markup = card.__panel__().object
        assert card.title == "Table"
        assert len(card.inputs) == 1
        assert card.inputs[0]["name"] == "rows"
        assert card.inputs[0]["label"] == "Rows"
        assert card.inputs[0]["type"] == "DataFrame"
        assert card.inputs[0]["required"] is True
        assert card.inputs[0]["missing"] is True
        # Names only on rows; type is in the handle's tooltip and colour, missing required is a ring.
        assert 'data-port="rows"' in markup
        assert "DataFrame" in card.handle_layout()[0]["in:rows"]["tooltip"]
        assert 'class="fd-required"' not in markup
        assert 'class="fd-type"' not in markup
        handles, _ = card.handle_layout()
        assert handles["in:rows"]["missing"] is True
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
            (pd.DataFrame({"a": [1, 2]}), "2 rows, 1 column"),
            (["A", "B"], "A, B"),
            (list(range(10)), "10 items"),
            ({"a": 1}, "1 entry"),
            ({"a": 1, "b": 2}, "2 entries"),
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
        assert "Table" in texts
        # Description lives in the inline info icon tooltip, not a body paragraph.
        assert "Shows rows." not in texts
        assert "Configuration" not in texts
        info_tips = [
            obj
            for obj in pn.Column(*body).select(pn.viewable.Viewable)
            if "fd-inspector-info" in getattr(obj, "css_classes", [])
        ]
        assert len(info_tips) == 1 and info_tips[0].title == "Shows rows."
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
        assert editor._inspector._status_dot.title == "Needs Rows"
        assert editor._inspector._status_info == ("Needs Rows", "warning")
        editor.connect(src, "rows", table, "rows")
        assert editor._inspector._status_dot.title == "Up to date"
        assert editor._inspector._status_info == ("Up to date", "success")
        assert "3 rows, 2 columns" in _texts(editor._inspector._data_column)

    async def test_wired_inputs_list_source_and_transform(self, editor):
        src, table = editor.add_component(ROWS), editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows", [{"op": "head", "n": 2}])
        select_on_canvas(editor._flow, node_id=table)
        texts = _texts(editor._inspector._inputs_column)
        assert "1 of 1 connected" in texts
        assert any(t.startswith("from Rows, rows, with transform") for t in texts)
        assert any(
            "2 rows \u00d7 2" in t and "<code>a</code>" in t and "<code>b</code>" in t
            for t in texts
        )

    async def test_remove_from_dashboard(self, editor):
        table = editor.add_component(TABLE)
        select_on_canvas(editor._flow, node_id=table)
        body = pn.Column(*editor._inspector._body)
        # Remove lives only in the overflow menu, never as a separate button.
        assert not [
            b
            for b in body.select(pn.viewable.Viewable)
            if getattr(b, "label", None) == "Remove from dashboard"
        ]
        menu = next(
            b
            for b in body.select(pn.viewable.Viewable)
            if "fd-inspector-menu" in getattr(b, "css_classes", [])
        )
        labels = [item["label"] for item in menu.items]
        assert labels == ["Duplicate", "Remove from dashboard"]
        editor._inspector._on_node_menu(table, menu.items[-1])
        assert editor._tile_items == []
        assert editor._flow.nodes == []
        assert "0 components, 0 connections" in _texts(editor._inspector._body)

    async def test_duplicate_from_menu(self, editor):
        table = editor.add_component(TABLE, config={"title": "Original"})
        select_on_canvas(editor._flow, node_id=table)
        menu = next(
            b
            for b in pn.Column(*editor._inspector._body).select(pn.viewable.Viewable)
            if "fd-inspector-menu" in getattr(b, "css_classes", [])
        )
        dup_item = next(item for item in menu.items if item["action"] == "duplicate")
        editor._inspector._on_node_menu(table, dup_item)
        assert len(editor._tile_items) == 2
        copy_id = next(i["instance_id"] for i in editor._tile_items if i["instance_id"] != table)
        assert editor._component_instances[copy_id].title == "Original"
        assert editor._flow.selection == {"nodes": [copy_id], "edges": []}

    async def test_a_copy_is_named_after_the_original_and_keeps_its_tile_size(self, editor):
        from panel_flowdash.ui.tile_layout import tile_entry, update_tile

        table = editor.add_component(TABLE, instance_id="by_state")
        update_tile(editor, table, width=50.0, height=400)
        editor._inspector._duplicate_component(table)
        editor._inspector._duplicate_component(table)
        ids = [i["instance_id"] for i in editor._tile_items]
        assert ids == ["by_state", "by_state_copy", "by_state_copy_2"]
        entry = tile_entry(editor, "by_state_copy")
        assert (entry["width"], entry["height"]) == (50.0, 400)

    async def test_how_was_this_made_only_in_menu_when_agent_made(self, editor):
        from panel_flowdash.turns import agent_edits

        with agent_edits(run="r1", question="Add table"):
            made = editor.add_component(TABLE, instance_id="agent_table")
        select_on_canvas(editor._flow, node_id=made)
        body = pn.Column(*editor._inspector._body)
        assert not [
            b
            for b in body.select(pn.viewable.Viewable)
            if getattr(b, "label", None) == "How was this made?"
        ]
        menu = next(
            b
            for b in body.select(pn.viewable.Viewable)
            if "fd-inspector-menu" in getattr(b, "css_classes", [])
        )
        assert [item["label"] for item in menu.items] == [
            "How was this made?",
            "Duplicate",
            "Remove from dashboard",
        ]

    async def test_summarize_input_html_for_rich_types(self, monkeypatch):
        from panel_flowdash import port_types
        from panel_flowdash.port_types import register_port_type
        from panel_flowdash.ui.inspector import summarize_input_html

        monkeypatch.setattr(port_types, "_COLUMN_READERS", dict(port_types._COLUMN_READERS))
        monkeypatch.setattr(port_types, "_ALIASES", dict(port_types._ALIASES))
        monkeypatch.setattr(
            port_types, "_SUPERTYPES", {k: set(v) for k, v in port_types._SUPERTYPES.items()}
        )

        class LazyPipe:
            def __init__(self, cols):
                self._cols = cols

            def __repr__(self):
                return "<LazyPipe object at 0x1234>"

        class DuckSource:
            def __init__(self):
                self.tables = {"turbines": "SELECT 1", "state_population": "SELECT 2"}

            def get_tables(self):
                raise AssertionError("listing the tables may query the source")

        class OpaqueVal:
            def __repr__(self):
                return "<OpaqueVal object at 0xdead>"

            def __flowdash_preview__(self):
                raise AssertionError("the inspector never calls a preview hook")

        register_port_type("test.lazy_pipe", columns=lambda v: v._cols)

        pipe_html = summarize_input_html(
            LazyPipe(["case_id", "t_state", "p_name", "t_cap"]), "test.lazy_pipe"
        )
        assert "4 columns" in pipe_html
        assert "<code>case_id</code>" in pipe_html
        assert "<LazyPipe" not in pipe_html

        src_html = summarize_input_html(DuckSource())
        assert "DuckSource" in src_html
        assert "<code>turbines</code>" in src_html
        assert "<code>state_population</code>" in src_html

        frame_html = summarize_input_html(pd.DataFrame({"x": [1, 2], "y": [3, 4]}))
        assert "2 rows \u00d7 2" in frame_html and "<code>x</code>" in frame_html

        chips_html = summarize_input_html(["TX", "IA", "OK"])
        assert chips_html.count('class="fd-inp-chip"') == 3
        assert ">TX<" in chips_html and ">IA<" in chips_html and ">OK<" in chips_html

        opaque_html = summarize_input_html(OpaqueVal())
        assert "OpaqueVal" in opaque_html and "0xdead" not in opaque_html

        xss_html = summarize_input_html(["<img src=x onerror=alert(1)>"])
        assert "<img" not in xss_html and "&lt;img" in xss_html

    async def test_the_input_summary_follows_the_upstream_value(self, editor):
        src = editor.add_component(ROWS)
        table = editor.add_component(TABLE)
        editor.connect(src, "rows", table, "rows")
        select_on_canvas(editor._flow, node_id=table)
        pane = editor._inspector._value_panes["rows"]
        assert "3 rows" in pane.object
        editor._component_instances[src].size = 5
        assert "5 rows \u00d7 2" in editor._inspector._value_panes["rows"].object

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
        assert "Gone/widget" in texts
        menu = next(
            b
            for b in pn.Column(*editor._inspector._body).select(pn.viewable.Viewable)
            if "fd-inspector-menu" in getattr(b, "css_classes", [])
        )
        assert [item["label"] for item in menu.items] == ["Remove from dashboard"]

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
