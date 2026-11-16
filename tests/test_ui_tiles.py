"""Tests for the framed dashboard tiles, their failed and waiting states and the inspect sheet."""

import io

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.failure_overlay import FailureMessage, FailureOverlay, with_failure_overlay
from panel_flowdash.ui.inspect_sheet import (
    MAX_COLUMNS,
    MAX_ROWS,
    dataframe_preview,
    describe_value,
    python_literal,
    reserve_space,
    short_dtype,
)
from panel_flowdash.ui.tile import Tile
from panel_flowdash.ui.waiting import WaitingPlaceholder, join_labels

FRAME = pd.DataFrame({f"c{i}": range(40) for i in range(12)}).assign(
    name=[f"n{i}" for i in range(40)]
)


@register(
    page=False,
    component=True,
    title="Source",
    description="Loads the frame.\n\nMore detail nobody needs on a tile.",
)
class Source(Viewer):
    rows = param.Integer(default=40)

    @param.output(param.DataFrame)
    @param.depends("rows")
    def frame(self):
        return FRAME.head(self.rows)

    def __panel__(self):
        return pn.pane.Markdown("source tile")


@register(page=False, component=True, title="Table", config=["title"])
class Table(Viewer):
    frame = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    title = param.String(default="")

    @param.output(param.Integer)
    @param.depends("frame")
    def count(self):
        return None if self.frame is None else len(self.frame)

    def __panel__(self):
        return pn.pane.Markdown("table tile")


@register(page=False, component=True, title="Inverse")
class Inverse(Viewer):
    value = param.Number(default=1)

    @param.output(param.Number)
    @param.depends("value")
    def inverse(self):
        return 1 / self.value

    def __panel__(self):
        return pn.pane.Markdown(str(self.value))


COMPONENTS = {"Test/source": Source, "Test/table": Table, "Test/inverse": Inverse}


@pytest.fixture
def editor():
    return FlowDash(
        COMPONENTS, notifications=False, include_builtin_components=False, inspect_outputs=True
    )


def _texts(viewable):
    return [str(pane.object) for pane in viewable.select(pn.pane.base.PaneBase)]


def _view_tiles(editor):
    editor.editable = False
    return {tile.view: tile for tile in editor._tile_grid.objects}


class TestTileFrame:
    def test_view_mode_frames_every_tile_without_a_title_by_default(self, editor):
        editor.add_component("Test/source")
        editor.editable = False

        (tile,) = editor._tile_grid.objects
        assert isinstance(tile, Tile)
        assert tile.header.visible
        assert "flowdash-tile-framed" in tile.css_classes
        assert "flowdash-tile-floating" in tile.css_classes
        assert not tile.titles.visible
        assert not tile.title.visible and not tile.subtitle.visible

    def test_titles_are_opt_in_per_dashboard(self, editor):
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        editor.settings.tile_titles = True

        assert tile.titles.visible
        assert tile.title.object == "Source"
        assert tile.subtitle.object == "Loads the frame."
        assert tile.subtitle.visible
        assert "flowdash-tile-floating" not in tile.css_classes
        assert editor.dirty
        assert editor.to_model().settings["tile_titles"] is True

    def test_unframed_tiles_float_their_actions(self, editor):
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        editor.settings.tile_cards = False

        assert "flowdash-tile-framed" not in tile.css_classes
        assert "flowdash-tile-floating" in tile.css_classes
        assert tile.header.visible and tile.inspect_button.visible
        assert tile.body.margin == 0

        editor.settings.tile_titles = True
        assert not tile.titles.visible
        assert "flowdash-tile-floating" in tile.css_classes

        editor.settings.tile_cards = True
        assert "flowdash-tile-framed" in tile.css_classes
        assert "flowdash-tile-floating" not in tile.css_classes
        assert tile.titles.visible

    def test_editor_default_applies_to_dashboards_without_the_setting(self):
        editor = FlowDash(
            COMPONENTS, notifications=False, include_builtin_components=False, tile_cards=False
        )
        assert editor.settings.tile_cards is False
        assert not editor.dirty
        editor.load_model(DashboardModel(dashboard_id="a", user_id="u", title="A"))
        assert editor.settings.tile_cards is False
        editor.load_model(
            DashboardModel(dashboard_id="b", user_id="u", title="B", settings={"tile_cards": True})
        )
        assert editor.settings.tile_cards is True

    def test_setting_is_offered_in_the_settings_form(self, editor):
        assert {"tile_cards", "tile_titles"} <= set(editor._settings_view._extra_form._widgets)

    def test_arranging_leaves_the_frame_to_the_grid(self, editor):
        instance_id = editor.add_component("Test/source")
        editor.mode = "dashboard"

        tile = editor._inspect_tiles[instance_id]
        assert not tile.header.visible
        assert "flowdash-tile-framed" not in tile.css_classes

        editor.settings.tile_cards = False
        assert "flowdash-tile-floating" not in tile.css_classes

        editor.editable = False
        assert tile.header.visible
        assert "flowdash-tile-floating" in tile.css_classes

    def test_title_follows_the_configured_title(self, editor):
        instance_id = editor.add_component("Test/table", {"title": "Top projects"})
        editor.settings.tile_titles = True
        tile = _view_tiles(editor)[editor._tile_objects[0]]
        assert tile.title.object == "Top projects"
        assert tile.inspect_button.description == "Inspect data for Top projects"

        editor.graph.get_config_state(instance_id).title = ""

        assert tile.title.object == "Table"

    def test_actions_have_accessible_names(self, editor):
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        assert tile.inspect_button.description == "Inspect data for Source"
        assert tile.expand_button.description == "Expand Source"
        assert tile.more_menu.description == "More for Source"

    def test_expand_moves_the_view_into_a_dialog_and_back(self, editor):
        editor.add_component("Test/source")
        view = editor._tile_objects[0]
        (tile,) = _view_tiles(editor).values()

        tile.expand_button.clicks += 1

        assert tile.expand_dialog.open
        assert tile.expand_dialog.objects == [view]
        assert view not in tile.body.objects

        tile.expand_dialog.open = False

        assert tile.body.objects == [view]
        assert tile.expand_dialog.objects == []

    def test_menu_never_repeats_a_visible_button(self, editor):
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        assert tile.inspect_button.visible and tile.expand_button.visible
        assert tile.more_menu.items == []
        assert not tile.more_menu.visible

        editor.show_in_editor = lambda _: None

        assert [item["label"] for item in tile.more_menu.items] == ["Show in editor"]
        assert tile.more_menu.visible

    def test_inspect_button_opens_the_sheet(self, editor):
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        tile.inspect_button.clicks += 1

        assert editor._inspect_dialog.open

    def test_no_inspect_action_without_outputs_to_inspect(self):
        editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        assert not tile.inspect_button.visible
        assert tile.expand_button.visible
        assert not tile.more_menu.visible

    def test_restricted_placeholder_is_framed_under_its_own_title(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            inspect_outputs=True,
            authorize_component=lambda entry: False,
        )
        editor.load_model(
            DashboardModel(
                dashboard_id="d",
                user_id="u",
                title="D",
                items=[DashboardItem(instance_id="s1", component_id="Test/source")],
            )
        )
        (tile,) = _view_tiles(editor).values()

        assert tile.title.object == "Restricted component"
        assert tile.inspect_button.description == "Inspect data for Restricted component"
        assert not tile.inspect_button.visible
        assert "Source" not in _texts(tile.header)

    def test_tile_carries_width_bounds_for_the_grid(self):
        @register(page=False, component=True, min_size={"width": 240}, max_size={"width": 800})
        def bounded(config):
            return pn.pane.Markdown("bounded")

        editor = FlowDash({"Test/bounded": bounded}, notifications=False)
        editor.add_component("Test/bounded")
        (tile,) = _view_tiles(editor).values()

        assert (tile.min_width, tile.max_width) == (240, 800)


class TestShowInEditor:
    def test_offered_only_in_view_mode_when_wired_and_not_read_only(self, editor):
        shown = []
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()
        assert "editor" not in [item["action"] for item in tile.more_menu.items]

        editor.show_in_editor = shown.append
        assert "editor" in [item["action"] for item in tile.more_menu.items]

        tile._on_menu({"label": "Show in editor", "action": "editor"})
        assert shown == [editor._tile_items[0]["instance_id"]]

        editor.read_only = True
        assert "editor" not in [item["action"] for item in tile.more_menu.items]

        editor.param.update(read_only=False, editable=True)
        assert "editor" not in [item["action"] for item in tile.more_menu.items]

    def test_hook_is_not_called_once_the_action_is_withdrawn(self, editor):
        shown = []
        editor.show_in_editor = shown.append
        editor.add_component("Test/source")
        (tile,) = _view_tiles(editor).values()

        editor.read_only = True
        tile._on_menu({"label": "Show in editor", "action": "editor"})

        assert shown == []


class TestWaitingState:
    def test_tile_says_what_it_needs_and_shows_a_waiting_chip(self, editor):
        source = editor.add_component("Test/source")
        target = editor.add_component("Test/table")
        tiles = _view_tiles(editor)
        tile = tiles[editor._tile_objects[1]]
        placeholder, body = tile.view
        assert isinstance(placeholder, WaitingPlaceholder)
        assert placeholder.visible and not body.visible
        assert placeholder.headline.object == "This tile needs Frame"
        assert "connected component" in placeholder.hint.object
        assert tile.status.visible and tile.status.object == "Waiting"

        editor.connect(source, "frame", target, "frame")

        assert not placeholder.visible and body.visible
        assert not tile.status.visible

    def test_registered_message_leads_and_names_the_input(self):
        placeholder = WaitingPlaceholder()
        placeholder.show(["Project"], "Pick a wind project to see its turbines")

        assert placeholder.headline.object == "Pick a wind project to see its turbines"
        assert placeholder.hint.object.startswith("This tile needs Project.")

    @pytest.mark.parametrize(
        ("labels", "text"),
        [(["A"], "A"), (["A", "B"], "A and B"), (["A", "B", "C"], "A, B and C")],
    )
    def test_labels_join_like_a_sentence(self, labels, text):
        assert join_labels(labels) == text


class TestFailureOverlay:
    def _failures(self, **failed):
        from panel_flowdash.dataflow_engine import InputFailures

        return InputFailures(failed=failed)

    def test_message_names_the_sources_and_keeps_details_behind_a_toggle(self):
        failures = self._failures()
        overlay = with_failure_overlay(
            pn.pane.Markdown("chart"),
            failures,
            lambda failed: FailureMessage(sources=["Quality Gate"], details="ValueError: too few"),
        )
        (scrim,) = [c for c in overlay.select(pn.Column) if "flowdash-failure" in c.css_classes]
        assert not scrim.visible

        failures.failed = {"filtered": object()}

        assert scrim.visible
        assert "This tile could not update" in _texts(scrim)
        assert overlay.summary.object == (
            "**Quality Gate** stopped the data, so this shows its last result."
        )
        assert overlay.details.object == "ValueError: too few"
        assert not overlay.details.visible

        overlay.details_toggle.clicks += 1
        assert overlay.details.visible
        assert overlay.details_toggle.label == "Hide details"

    def test_plain_string_descriptions_still_work(self):
        failures = self._failures(value=object())
        overlay = with_failure_overlay(pn.pane.Markdown("x"), failures, lambda failed: "boom")

        assert overlay.details.object == "boom"
        assert overlay.summary.object.startswith("An upstream step failed")

    def test_keeps_view_and_overlay_as_its_objects(self):
        view = pn.pane.Markdown("x", height=120)
        overlay = with_failure_overlay(view, self._failures(), lambda failed: "")

        assert isinstance(overlay, FailureOverlay)
        assert overlay.objects[0] is view
        assert overlay.height == 120

    @pytest.mark.failures
    def test_failed_tile_names_the_upstream_component_and_offers_the_editor(self):
        shown = []
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            propagate_failures=True,
            show_in_editor=shown.append,
        )
        src = editor.add_component("Test/inverse")
        dst = editor.add_component("Test/inverse")
        editor.connect(src, "inverse", dst, "value")
        tile = _view_tiles(editor)[editor._tile_objects[1]]
        overlay = tile.view
        (button,) = overlay.actions

        editor.graph.get_state(src).value = 0

        assert tile.status.object == "Failed" and tile.status.visible
        assert overlay.summary.object.startswith("**Inverse** stopped the data")
        assert "ZeroDivisionError" in overlay.details.object
        assert button.visible
        button.clicks += 1
        assert shown == [dst]

        editor.graph.get_state(src).value = 2
        assert not tile.status.visible

    @pytest.mark.failures
    def test_editor_action_is_hidden_on_the_canvas(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            propagate_failures=True,
            show_in_editor=lambda _: None,
        )
        editor.add_component("Test/inverse")
        overlay = editor._tile_objects[0]
        _view_tiles(editor)
        (button,) = overlay.actions
        assert button.visible

        editor.editable = True

        assert not button.visible


class TestInspectSheet:
    def _open(self, editor, instance_id):
        _view_tiles(editor)
        editor._inspect_tiles[instance_id].inspect_button.clicks += 1
        return editor._inspect_dialog

    def test_dataframe_shows_shape_lineage_columns_and_first_rows_only(self, editor):
        source = editor.add_component("Test/source")
        target = editor.add_component("Test/table", {"title": "Projects"})
        editor.connect(source, "frame", target, "frame")
        editor.set_edge_transform(source, "frame", target, "frame", [{"op": "head", "n": 20}])

        sheet = self._open(editor, source)

        texts = _texts(sheet)
        assert sheet.open
        assert "Computed" in texts
        assert "DataFrame" in texts
        assert "40 rows, 13 columns" in texts
        assert "Projects" in texts
        assert "+1 more" in texts
        (table,) = sheet.select(pn.pane.HTML)
        assert table.object.count("<tr>") == MAX_ROWS + 1
        assert table.object.count("</th>") == MAX_COLUMNS
        assert "First 10 rows and 8 of 13 columns" in texts

        target_sheet = self._open(editor, target)
        texts = _texts(target_sheet)
        assert any(text.startswith("Source, ") for text in texts)

    def test_sheet_reserves_its_width_on_wide_screens_while_open(self, editor):
        source = editor.add_component("Test/source")
        self._open(editor, source)
        assert "flowdash-inspecting" in editor._workspace_area.css_classes
        assert any("padding-right" in css for css in editor._workspace_area.stylesheets)

        editor._close_inspection_dialog()

        assert "flowdash-inspecting" not in editor._workspace_area.css_classes
        assert not editor._inspect_dialog.open

    def test_sheet_is_a_persistent_right_drawer(self, editor):
        sheet = editor._inspect_dialog
        assert isinstance(sheet, pmui.Drawer)
        assert (sheet.anchor, sheet.variant) == ("right", "persistent")

    def test_value_repr_replaces_the_dataframe_preview(self, editor):
        editor.value_repr = lambda value: f"custom {type(value).__name__}"
        source = editor.add_component("Test/source")

        sheet = self._open(editor, source)

        assert "custom DataFrame" in _texts(sheet)
        assert not sheet.select(pn.pane.HTML)

    def test_download_writes_the_whole_frame(self):
        preview = dataframe_preview(FRAME, "frame")
        (download,) = preview.select(pmui.FileDownload)

        data = download.callback()

        assert isinstance(data, io.StringIO)
        assert pd.read_csv(data).shape == FRAME.shape
        assert download.filename == "frame.csv"

    def test_copy_as_python_rebuilds_the_first_rows(self):
        frame = pd.DataFrame(
            {"x": [1.5, None], "when": pd.to_datetime(["2020-01-01", None]), "s": ["a", "b"]}
        )
        namespace = {}
        exec(python_literal(frame), namespace)

        pd.testing.assert_frame_equal(namespace["df"], frame, check_dtype=False)

    def test_copy_of_a_long_frame_is_truncated_and_says_so(self):
        source = python_literal(pd.DataFrame({"x": range(500)}))

        assert "# The first 50 of 500 rows" in source

    def test_describe_value(self):
        assert describe_value(None) == (None, None)
        assert describe_value(FRAME) == ("DataFrame", "40 rows, 13 columns")
        assert describe_value(FRAME["c0"]) == ("Series", "40 values")
        assert describe_value([1, 2]) == ("list", "2 items")
        assert describe_value(3) == ("int", None)

    def test_short_dtypes(self):
        frame = pd.DataFrame(
            {"f": [1.0], "i": [1], "b": [True], "s": ["x"], "t": pd.to_datetime(["2020"])}
        )
        assert [short_dtype(frame[c]) for c in frame] == [
            "float",
            "int",
            "bool",
            "str",
            "datetime",
        ]

    def test_reserve_space_adds_its_stylesheet_once(self):
        layout = pn.Column()
        reserve_space(layout, True)
        reserve_space(layout, False)
        reserve_space(layout, True)

        assert len(layout.stylesheets) == 1
        assert layout.css_classes == ["flowdash-inspecting"]
