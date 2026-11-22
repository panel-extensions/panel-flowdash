"""Tests for moving components between the tile grid, the top bar and the sidebar."""

import time

import panel as pn
import panel_material_ui as pmui
import pytest
import yaml
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps, OperationError, dashboard_schema
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
)
from panel_flowdash.dashboard_store import (
    DashboardItem,
    DashboardModel,
    DashboardStore,
    MemoryDashboardStore,
)
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.ui.tile_layout import (
    GRID,
    SIDEBAR,
    TOP,
    area_items,
    area_of,
    fits_beside,
    grid_tiles,
    move_targets,
    move_to_area,
    off_grid,
)


@register(page=False, component=True, sidebar=True, title="Region")
def region(config):
    return pmui.Select(label="Region", options=["North", "South"])


@register(page=False, component=True, title="Chart", default_size={"width": 600, "height": 300})
def chart(config):
    return pn.pane.Markdown("chart")


COMPONENTS = {"T/region": region, "T/chart": chart}


def _editor(**params):
    params.setdefault("notifications", False)
    params.setdefault("history_coalesce", 0)
    return FlowDash(components=COMPONENTS, **params)


def _ids(items) -> list[str]:
    return [item["instance_id"] for item in items]


@pytest.fixture
def editor():
    editor = _editor(store=MemoryDashboardStore(), user="alice")
    editor.new_dashboard("Drag")
    editor.add_component("T/region", instance_id="region")
    editor.add_component("Widgets/Select", instance_id="select")
    editor.add_component("Widgets/KPI", instance_id="kpi")
    editor.add_component("Text/Heading", instance_id="heading")
    editor.add_component("T/chart", instance_id="chart")
    editor.mode = "dashboard"
    yield editor
    editor.dispose()


def _item(editor, instance_id):
    return next(i for i in editor._tile_items if i["instance_id"] == instance_id)


def _send_grid_layout(editor, order: list[str]):
    """Send the grid layout the browser reports after a drag that left the tiles in *order*."""
    grid = editor._tile_grid
    by_id = dict(zip(editor._grid_ids, grid.layout, strict=True))
    layout = [
        {**by_id[iid], "index": editor._grid_ids.index(iid), "visible": True} for iid in order
    ]
    with set_curdoc(Document()):
        grid._process_events({"layout": layout})


def _areas(editor) -> dict[str, list[str]]:
    return {area: _ids(area_items(editor, area)) for area in (GRID, TOP, SIDEBAR)}


class TestPlacement:
    def test_controls_go_where_the_setting_says_and_the_rest_on_the_grid(self, editor):
        assert _areas(editor) == {
            GRID: ["select", "kpi", "heading", "chart"],
            TOP: ["region"],
            SIDEBAR: [],
        }
        editor.settings.controls_position = "left"
        assert _areas(editor)[SIDEBAR] == ["region"]
        assert [iid for iid, _ in editor._area_views[SIDEBAR]] == ["region"]

    def test_only_controls_kpis_and_text_fit_beside_the_grid(self, editor):
        fits = {iid: fits_beside(editor, _item(editor, iid)) for iid in _ids(editor._tile_items)}
        assert fits == {
            "region": True,
            "select": True,
            "kpi": True,
            "heading": True,
            "chart": False,
        }
        assert move_targets(editor, _item(editor, "chart")) == []
        assert move_targets(editor, _item(editor, "kpi")) == [TOP, SIDEBAR]
        assert move_targets(editor, _item(editor, "region")) == [GRID, SIDEBAR]


class TestMoving:
    def test_grid_to_sidebar_and_top_and_back(self, editor):
        move_to_area(editor, "kpi", SIDEBAR)
        move_to_area(editor, "select", TOP)
        assert _areas(editor) == {
            GRID: ["heading", "chart"],
            TOP: ["region", "select"],
            SIDEBAR: ["kpi"],
        }
        assert editor._grid_ids == ["heading", "chart"]
        assert editor._view_controls.placed == {"region": TOP, "select": TOP, "kpi": SIDEBAR}
        assert editor._view_controls.side
        assert editor._workspace_area.objects[0] is editor._view_controls._left_row

        move_to_area(editor, "kpi", GRID, before="chart")
        move_to_area(editor, "region", GRID, before="heading")
        assert _areas(editor)[GRID] == ["region", "heading", "kpi", "chart"]
        assert not editor._view_controls.side
        assert editor._workspace_area.objects[0] is editor._view_controls._row

    def test_reorder_within_an_area(self, editor):
        for iid in ("select", "kpi", "heading"):
            move_to_area(editor, iid, TOP)
        assert _areas(editor)[TOP] == ["region", "select", "kpi", "heading"]
        move_to_area(editor, "heading", TOP, before="region")
        move_to_area(editor, "region", TOP)
        assert _areas(editor)[TOP] == ["heading", "select", "kpi", "region"]
        move_to_area(editor, "kpi", TOP, index=0)
        assert _areas(editor)[TOP] == ["kpi", "heading", "select", "region"]

    def test_a_chart_is_refused(self, editor):
        with pytest.raises(ValueError, match="only controls, KPIs and text fit in the sidebar"):
            move_to_area(editor, "chart", SIDEBAR)
        assert area_of(editor, _item(editor, "chart")) == GRID

    def test_a_tile_keeps_its_size_while_beside_the_grid(self, editor):
        move_to_area(editor, "kpi", TOP)
        entry = {e["instance_id"]: e for e in editor.layout}["kpi"]
        move_to_area(editor, "kpi", GRID)
        assert {e["instance_id"]: e for e in editor.layout}["kpi"]["width"] == entry["width"]

    def test_a_hidden_tile_moved_is_shown(self, editor):
        from panel_flowdash.ui.tile_layout import update_tile

        update_tile(editor, "kpi", visible=False)
        move_to_area(editor, "kpi", TOP)
        move_to_area(editor, "kpi", GRID)
        assert "kpi" in _ids(grid_tiles(editor))

    def test_moved_controls_stay_when_the_setting_changes(self, editor):
        move_to_area(editor, "select", TOP)
        editor.settings.controls_position = "left"
        assert _areas(editor)[TOP] == ["select"]
        assert _areas(editor)[SIDEBAR] == ["region"]

    def test_off_grid_names_the_area(self, editor):
        move_to_area(editor, "kpi", SIDEBAR)
        reasons = {entry.instance_id: entry.reason for entry in off_grid(editor)}
        assert reasons == {"region": "in top bar", "kpi": "in sidebar"}

    def test_the_layout_survives_the_canvas_tab(self, editor):
        move_to_area(editor, "kpi", SIDEBAR)
        move_to_area(editor, "region", GRID, before="select")
        editor.mode = "wiring"
        editor.mode = "dashboard"
        assert _areas(editor) == {
            GRID: ["region", "select", "heading", "chart"],
            TOP: [],
            SIDEBAR: ["kpi"],
        }


class TestHistory:
    def test_one_version_per_move_and_undo(self, editor):
        count = len(editor.edit_history.versions)
        move_to_area(editor, "kpi", SIDEBAR)
        assert len(editor.edit_history.versions) == count + 1
        version = editor.edit_history.versions[-1]
        assert version.metadata["args"] == {"name": "kpi", "area": SIDEBAR}
        assert version.metadata["label"] == "Moved kpi to the sidebar"
        move_to_area(editor, "kpi", SIDEBAR)
        assert len(editor.edit_history.versions) == count + 1

        assert editor.undo()
        assert area_of(editor, _item(editor, "kpi")) == GRID
        assert "kpi" in editor._grid_ids
        assert editor.redo()
        assert area_of(editor, _item(editor, "kpi")) == SIDEBAR


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda tmp_path: MemoryDashboardStore(),
        lambda tmp_path: DashboardStore(tmp_path / "d.db"),
        lambda tmp_path: JSONFolderDashboardStore(tmp_path / "dashboards"),
    ],
    ids=["memory", "sqlite", "json"],
)
def test_stores_round_trip_the_areas(tmp_path, store_factory):
    store = store_factory(tmp_path)
    editor = _editor(store=store, user="alice")
    model = editor.new_dashboard("Areas")
    for cid, iid in [("T/region", "region"), ("Widgets/KPI", "kpi"), ("Text/Heading", "heading")]:
        editor.add_component(cid, instance_id=iid)
    editor.add_component("T/chart", instance_id="chart")
    editor.mode = "dashboard"
    move_to_area(editor, "heading", SIDEBAR)
    move_to_area(editor, "kpi", SIDEBAR, before="heading")
    move_to_area(editor, "region", GRID, before="chart")
    editor.save()

    other = _editor(store=store, user="alice")
    other.load_model(store.load_dashboard("alice", model.dashboard_id))
    other.mode = "dashboard"
    assert _areas(other) == {GRID: ["region", "chart"], TOP: [], SIDEBAR: ["kpi", "heading"]}
    assert other._grid_ids == ["region", "chart"]


class TestYAML:
    def _model(self, tile_layout):
        return DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Areas",
            items=[
                DashboardItem(instance_id="r", component_id="T/region"),
                DashboardItem(instance_id="c", component_id="T/chart"),
            ],
            tile_layout=tile_layout,
        )

    def test_round_trip(self):
        data = self._model(
            [
                {"instance_id": "c", "index": 0},
                {"instance_id": "r", "index": 1, "area": "sidebar"},
            ]
        ).to_dict()
        doc = dashboard_to_dict(data)
        assert doc["layout"]["tiles"] == [{"node": "c"}, {"node": "r", "area": "sidebar"}]
        loaded = dashboard_from_yaml(yaml.safe_dump(doc))
        assert loaded["tile_layout"][1] == {"instance_id": "r", "index": 1, "area": "sidebar"}

    @pytest.mark.parametrize(
        "tile, path",
        [
            ("{node: r, area: left}", "layout.tiles[0].area"),
            ("{node: r, area: true}", "layout.tiles[0].area"),
        ],
    )
    def test_invalid_area_is_refused(self, tile, path):
        text = "flowdash: 1\nnodes:\n  r: {component: T/region}\nlayout:\n  tiles:\n"
        with pytest.raises(DashboardYAMLError) as info:
            dashboard_from_yaml(text + f"    - {tile}\n")
        assert info.value.path == path

    def test_responsive_layouts_may_not_set_an_area(self):
        text = "flowdash: 1\nnodes:\n  r: {component: T/region}\nlayout:\n  responsive:\n"
        with pytest.raises(DashboardYAMLError, match="unknown key"):
            dashboard_from_yaml(text + "    sm:\n      - {node: r, area: top}\n")

    def test_schema_offers_the_area_on_base_tiles_only(self):
        layout = dashboard_schema({})["properties"]["layout"]["properties"]
        assert layout["tiles"]["items"]["properties"]["area"] == {
            "enum": ["grid", "top", "sidebar"]
        }
        responsive = layout["responsive"]["additionalProperties"]["items"]
        assert "area" not in responsive["properties"]

    def test_a_chart_placed_beside_the_grid_by_hand_stays_on_it(self):
        editor = _editor()
        editor.load_model(
            self._model(
                [{"instance_id": "c", "area": "top"}, {"instance_id": "r", "area": "grid"}]
            )
        )
        editor.mode = "dashboard"
        assert editor._grid_ids == ["r", "c"]
        assert editor._area_views[TOP] == []


class TestAgent:
    @pytest.fixture
    def ops(self, editor):
        return DashboardOps(editor)

    def test_place_tile_moves_between_areas(self, ops, editor):
        assert ops.place_tile("kpi", area="sidebar")["area"] == "sidebar"
        assert area_of(editor, _item(editor, "kpi")) == SIDEBAR
        ops.place_tile("heading", area="sidebar", index=0)
        assert _areas(editor)[SIDEBAR] == ["heading", "kpi"]
        ops.place_tile("kpi", index=0)
        assert _areas(editor)[SIDEBAR] == ["kpi", "heading"]
        ops.place_tile("region", area="grid", index=0)
        assert _areas(editor)[GRID][0] == "region"
        doc = yaml.safe_load(ops.get_dashboard())
        tiles = {t["node"]: t for t in doc["layout"]["tiles"]}
        assert tiles["kpi"]["area"] == "sidebar" and tiles["region"]["area"] == "grid"

    def test_place_tile_refuses_charts_and_sizes_beside_the_grid(self, ops):
        with pytest.raises(OperationError, match="needs the room of a tile"):
            ops.place_tile("chart", area="top")
        with pytest.raises(OperationError, match="is in the top bar"):
            ops.place_tile("region", width=50)
        with pytest.raises(OperationError, match="area is one of"):
            ops.place_tile("kpi", area="left")


class TestControls:
    def test_inspector_moves_the_selected_component(self, editor):
        from panel_flowdash.ui.status_bar import select_on_canvas

        editor.mode = "wiring"
        select_on_canvas(editor._flow, node_id="kpi")
        inspector = editor._inspector
        inspector.refresh(force=True)

        def buttons():
            return {
                b.label: b
                for b in inspector._body.select(pmui.Button)
                if b.label.startswith("Move to")
            }

        buttons()["Move to sidebar"].clicks += 1
        assert area_of(editor, _item(editor, "kpi")) == SIDEBAR
        assert set(buttons()) == {"Move to grid", "Move to top bar"}
        buttons()["Move to grid"].clicks += 1
        assert area_of(editor, _item(editor, "kpi")) == GRID

        select_on_canvas(editor._flow, node_id="chart")
        inspector.refresh(force=True)
        assert buttons() == {}

    def test_tile_tab_moves_the_selected_tile_and_back(self, editor):
        select_on_canvas(editor._flow, node_id="kpi")
        options = editor._inspector.tile_options
        top = next(b for b in options._moves if b.label == "Move to top bar")
        top.clicks += 1
        assert area_of(editor, _item(editor, "kpi")) == TOP
        select_on_canvas(editor._flow)
        panel = editor._layout_panel
        back = [
            b
            for b in panel._off_grid.select(pmui.Button)
            if b.label == "Move to grid" and "KPI" in b.description
        ]
        assert len(back) == 1
        back[0].clicks += 1
        assert area_of(editor, _item(editor, "kpi")) == GRID


class TestDragBridge:
    def test_lists_the_tiles_and_components_that_can_be_dragged(self, editor):
        drag = editor._tile_drag
        assert drag.enabled and drag.areas == [TOP, SIDEBAR]
        tiles = {info["id"]: info["fits"] for info in drag.tiles.values()}
        assert tiles == {"select": True, "kpi": True, "heading": True, "chart": False}
        assert [info["id"] for info in drag.controls.values()] == ["region"]
        editor.mode = "split"
        assert editor._tile_drag.areas == [TOP]
        editor.editable = False
        assert not editor._tile_drag.enabled

    def test_a_drop_moves_the_component(self, editor):
        drag = editor._tile_drag
        drag._handle_msg({"action": "move", "id": "kpi", "area": "sidebar", "before": None})
        assert area_of(editor, _item(editor, "kpi")) == SIDEBAR
        drag._handle_msg({"action": "move", "id": "kpi", "area": "grid", "before": "select"})
        assert _areas(editor)[GRID][:2] == ["kpi", "select"]

    def test_a_refused_drop_changes_nothing(self, editor):
        count = len(editor.edit_history.versions)
        editor._tile_drag._handle_msg({"action": "refuse", "id": "chart", "area": "top"})
        editor._tile_drag._handle_msg({"action": "move", "id": "chart", "area": "top"})
        assert area_of(editor, _item(editor, "chart")) == GRID
        assert len(editor.edit_history.versions) == count

    def test_a_drag_off_the_grid_is_one_version(self, editor):
        drag, count = editor._tile_drag, len(editor.edit_history.versions)
        drag._handle_msg({"action": "release", "id": "kpi", "area": "sidebar"})
        _send_grid_layout(editor, ["select", "heading", "chart", "kpi"])
        drag._handle_msg({"action": "move", "id": "kpi", "area": "sidebar", "before": None})
        versions = editor.edit_history.versions[count:]
        assert [v.metadata["ops"] for v in versions] == [
            [{"op": "layout", "args": {"name": "kpi", "area": SIDEBAR}}]
        ]
        editor.undo()
        assert _areas(editor)[GRID] == ["select", "kpi", "heading", "chart"]

    def test_the_layout_of_a_refused_drag_is_its_own_version(self, editor):
        drag, count = editor._tile_drag, len(editor.edit_history.versions)
        drag._handle_msg({"action": "release", "id": "chart", "area": "top"})
        _send_grid_layout(editor, ["chart", "select", "kpi", "heading"])
        assert len(editor.edit_history.versions) == count
        drag._handle_msg({"action": "refuse", "id": "chart", "area": "top"})
        versions = editor.edit_history.versions[count:]
        assert [v.metadata["ops"] for v in versions] == [[{"op": "layout", "args": {}}]]

    def test_a_layout_long_after_a_release_is_its_own_version(self, editor, monkeypatch):
        count = len(editor.edit_history.versions)
        editor._tile_drag._handle_msg({"action": "release", "id": "kpi", "area": "sidebar"})
        clock = time.monotonic() + 10
        monkeypatch.setattr(time, "monotonic", lambda: clock)
        _send_grid_layout(editor, ["select", "heading", "chart", "kpi"])
        assert len(editor.edit_history.versions) == count + 1

    def test_a_release_without_a_drop_records_the_layout_once_it_expires(
        self, editor, monkeypatch
    ):
        later = []
        monkeypatch.setattr(
            editor, "_call_later", lambda _seconds, callback: later.append(callback)
        )
        editor.dirty = False
        count = len(editor.edit_history.versions)
        editor._tile_drag._handle_msg({"action": "release", "id": "kpi", "area": "sidebar"})
        _send_grid_layout(editor, ["select", "heading", "chart", "kpi"])
        assert not editor.dirty
        assert len(later) == 1
        later[0]()
        assert editor.dirty
        versions = editor.edit_history.versions[count:]
        assert [v.metadata["ops"] for v in versions] == [[{"op": "layout", "args": {}}]]

    def test_leaving_edit_mode_records_a_held_layout(self, editor):
        count = len(editor.edit_history.versions)
        drag = editor._tile_drag
        drag._handle_msg({"action": "release", "id": "kpi", "area": "sidebar"})
        _send_grid_layout(editor, ["select", "heading", "chart", "kpi"])
        editor.editable = False
        drag._handle_msg({"action": "move", "id": "kpi", "area": "sidebar", "before": None})
        assert area_of(editor, _item(editor, "kpi")) == GRID
        versions = editor.edit_history.versions[count:]
        assert [v.metadata["ops"] for v in versions] == [[{"op": "layout", "args": {}}]]

    def test_a_second_release_keeps_the_first_move_whole(self, editor):
        drag, count = editor._tile_drag, len(editor.edit_history.versions)
        drag._handle_msg({"action": "release", "id": "kpi", "area": "sidebar"})
        _send_grid_layout(editor, ["select", "heading", "chart", "kpi"])
        drag._handle_msg({"action": "release", "id": "heading", "area": "top"})
        drag._handle_msg({"action": "move", "id": "kpi", "area": "sidebar", "before": None})
        _send_grid_layout(editor, ["select", "chart", "heading"])
        drag._handle_msg({"action": "move", "id": "heading", "area": "top", "before": None})
        versions = editor.edit_history.versions[count:]
        assert [v.metadata["ops"] for v in versions] == [
            [{"op": "layout", "args": {"name": "kpi", "area": SIDEBAR}}],
            [{"op": "layout", "args": {"name": "heading", "area": TOP}}],
        ]

    def test_drops_are_ignored_while_viewing(self, editor):
        editor.editable = False
        editor._tile_drag._handle_msg({"action": "move", "id": "kpi", "area": "sidebar"})
        assert area_of(editor, _item(editor, "kpi")) == GRID


class TestAutoLayout:
    def test_arranging_only_moves_grid_tiles(self, editor):
        move_to_area(editor, "kpi", SIDEBAR)
        move_to_area(editor, "heading", TOP, before="region")
        move_to_area(editor, "region", GRID, before="chart")
        DashboardOps(editor).arrange_layout()
        assert _areas(editor)[TOP] == ["heading"]
        assert _areas(editor)[SIDEBAR] == ["kpi"]
        assert set(_areas(editor)[GRID]) == {"select", "region", "chart"}
        assert editor._area_views[SIDEBAR][0][0] == "kpi"
        doc = yaml.safe_load(DashboardOps(editor).get_dashboard())
        areas = {t["node"]: t.get("area") for t in doc["layout"]["tiles"]}
        assert areas["kpi"] == "sidebar" and areas["heading"] == "top"
        assert areas["region"] == "grid"

    def test_new_tiles_are_placed_around_what_sits_beside_the_grid(self, editor):
        move_to_area(editor, "kpi", SIDEBAR)
        move_to_area(editor, "select", TOP)
        editor.add_component("Widgets/KPI", instance_id="kpi2")
        editor._place_new_tiles(["kpi2"])
        assert _areas(editor)[SIDEBAR] == ["kpi"]
        assert _areas(editor)[TOP] == ["region", "select"]
        assert "kpi2" in _areas(editor)[GRID]


class TestPlaceholders:
    def test_a_placeholder_keeps_its_area_when_saved(self):
        store = MemoryDashboardStore()
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Areas",
            items=[
                DashboardItem(instance_id="r", component_id="T/region"),
                DashboardItem(instance_id="c", component_id="T/chart"),
            ],
            tile_layout=[
                {"instance_id": "c", "index": 0},
                {"instance_id": "r", "index": 1, "area": "sidebar"},
            ],
        )
        store.save_dashboard(model)
        editor = _editor(
            store=store,
            user="alice",
            authorize_component=lambda entry: entry.app_id != "T/region",
        )
        editor.load("d1")
        editor.mode = "dashboard"
        assert "r" in editor._placeholders()
        assert "r" in editor._grid_ids
        assert move_targets(editor, _item(editor, "r")) == []
        editor.add_component("Widgets/KPI", instance_id="kpi")
        editor.save()
        saved = {e["instance_id"]: e for e in store._load_any("d1").tile_layout}
        assert saved["r"]["area"] == "sidebar"

        other = _editor(store=store, user="alice")
        other.load("d1")
        other.mode = "dashboard"
        assert _areas(other)[SIDEBAR] == ["r"]
