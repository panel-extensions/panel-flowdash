"""Groups: nodes framed together on the canvas, collapsed into one node, in the file and the stores."""

import json
import sqlite3
from contextlib import closing

import param
import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
)
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.groups import (
    DashboardGroup,
    GroupError,
    decode_groups,
    group_node_data,
    group_node_id,
    group_ports,
    unique_group_id,
)
from panel_flowdash.json_store import JSONFolderDashboardStore


class TableConfig(param.Parameterized):
    columns = param.List(default=["a", "b"])


@register(
    page=False,
    component=True,
    title="Table",
    config_schema=TableConfig,
    provides=[{"key": "table", "type": "str"}],
)
def table(config, instance_config):
    config.table = ",".join(instance_config.columns)
    return "table"


class PickConfig(param.Parameterized):
    column = param.String(default="a")


@register(
    page=False,
    component=True,
    title="Pick",
    config_schema=PickConfig,
    provides=[{"key": "picked", "type": "str"}],
    requires=[{"key": "table", "type": "str", "required": True}],
)
def pick(config, instance_config):
    def update(*_):
        config.picked = (
            instance_config.column if instance_config.column in (config.table or "") else None
        )

    config.param.watch(update, "table")
    update()
    return "pick"


@register(
    page=False,
    component=True,
    title="Show",
    provides=[],
    requires=[{"key": "picked", "type": "str", "required": True}],
)
def show(config):
    return "show"


COMPONENTS = {"T/table": table, "T/pick": pick, "T/show": show}

DOC = """\
flowdash: 4
title: Picks
groups:
  picks: {label: Two picks, collapsed: true}
nodes:
  data: {component: T/table, config: {columns: [a, b, c]}, position: [0, 0]}
  first: {component: T/pick, group: picks, config: {column: a}, position: [300, 0]}
  second: {component: T/pick, group: picks, config: {column: c}, position: [300, 200]}
  out: {component: T/show, position: [700, 0]}
edges:
  - {from: data.table, to: first.table}
  - {from: data.table, to: second.table}
  - {from: first.picked, to: out.picked}
"""


def _editor(**params):
    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        user="alice",
        history_coalesce=0,
        **params,
    )


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Picks")
    editor.import_dashboard(DOC)
    yield editor
    editor.dispose()


def _canvas(editor, node_id):
    return next(n for n in editor._flow.nodes if n["id"] == node_id)


class TestPorts:
    def test_an_outside_output_feeding_two_inner_ports_is_one_input(self):
        edges = [
            ("data", "table", "first", "table"),
            ("data", "table", "second", "table"),
            ("first", "picked", "out", "picked"),
            ("first", "picked", "second", "column"),
        ]
        inputs, outputs = group_ports({"first", "second"}, edges)
        assert [(p.name, p.ends) for p in inputs] == [
            ("table", (("first", "table"), ("second", "table")))
        ]
        assert [(p.name, p.ends) for p in outputs] == [("picked", (("first", "picked"),))]

    def test_ports_of_the_same_name_are_numbered(self):
        edges = [("a", "x", "first", "table"), ("b", "x", "second", "table")]
        inputs, _ = group_ports({"first", "second"}, edges)
        assert [p.name for p in inputs] == ["table", "table_2"]

    def test_handle_map_names_each_inner_port_and_its_direction(self):
        group = DashboardGroup("g", nodes=["first", "second"])
        edges = [("data", "table", "first", "table"), ("first", "table", "out", "picked")]
        data = group_node_data(group, *group_ports(group.nodes, edges))
        assert data["inputs"] == [{"id": "in:table", "label": "table", "top": 47.0}]
        assert data["outputs"] == [{"id": "out:table", "label": "table", "top": 47.0}]
        assert data["handle_map"] == [
            {"node": "first", "handle": "table", "group_handle": "in:table", "direction": "input"},
            {
                "node": "first",
                "handle": "table",
                "group_handle": "out:table",
                "direction": "output",
            },
        ]


class TestModel:
    def test_round_trip_and_repair(self):
        group = DashboardGroup("g", label="G", nodes=["a", "b"], collapsed=True)
        assert decode_groups([group.to_dict()]) == [group]
        # A node in two groups stays in the first; an emptied group goes.
        decoded = decode_groups([group.to_dict(), {"id": "h", "nodes": ["a"]}])
        assert [g.id for g in decoded] == ["g"]

    @pytest.mark.parametrize(
        "value",
        [
            {"id": "a b", "nodes": ["x"]},
            {"id": "g", "nodes": "x"},
            {"id": "g", "collapsed": "yes"},
        ],
    )
    def test_malformed_groups_raise(self, value):
        with pytest.raises(GroupError):
            decode_groups([value])

    def test_unique_ids(self):
        assert unique_group_id("Paid revenue!", []) == "Paid_revenue"
        assert unique_group_id("g", ["g", "g_2"]) == "g_3"
        assert unique_group_id("", []) == "group"


class TestYAML:
    def test_reads_groups_and_members(self):
        data = dashboard_from_yaml(DOC)
        assert data["groups"] == [
            {"id": "picks", "label": "Two picks", "collapsed": True, "nodes": ["first", "second"]}
        ]

    def test_writes_groups_and_node_keys(self, editor):
        doc = dashboard_to_dict(editor)
        assert doc["groups"] == {"picks": {"label": "Two picks", "collapsed": True}}
        assert doc["nodes"]["first"]["group"] == "picks"
        assert "group" not in doc["nodes"]["data"]

    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            (
                lambda d: d["nodes"]["first"].update(group="nope"),
                "nodes.first.group: names no group",
            ),
            (lambda d: d["groups"].update(empty={}), "groups.empty: has no nodes"),
            (lambda d: d["groups"]["picks"].update(size=3), "groups.picks"),
            (lambda d: d.update(flowdash=3), "document"),
        ],
    )
    def test_refuses_malformed_groups(self, edit, message):
        doc = yaml.safe_load(DOC)
        edit(doc)
        with pytest.raises(DashboardYAMLError, match=message):
            dashboard_from_yaml(yaml.safe_dump(doc))

    def test_version_3_files_have_no_group_key(self):
        doc = yaml.safe_load(DOC)
        doc["flowdash"] = 3
        del doc["groups"]
        with pytest.raises(DashboardYAMLError, match=r"nodes\.first"):
            dashboard_from_yaml(yaml.safe_dump(doc))


class TestEditor:
    def test_import_draws_a_collapsed_frame_with_ports(self, editor):
        assert editor.groups["picks"].nodes == ["first", "second"]
        frame = _canvas(editor, group_node_id("picks"))
        assert frame["type"] == "group"
        assert frame["label"] == "Two picks"
        assert frame["data"]["collapsed"] is True
        assert [h["id"] for h in frame["data"]["inputs"]] == ["in:table"]
        assert [h["id"] for h in frame["data"]["outputs"]] == ["out:picked"]
        assert _canvas(editor, "first")["parentId"] == group_node_id("picks")

    def test_positions_stay_absolute_in_the_model(self, editor):
        model = editor.to_model()
        positions = {item.instance_id: (item.x, item.y) for item in model.items}
        assert positions["first"] == (300, 0)
        assert positions["second"] == (300, 200)
        assert model.groups == [DashboardGroup("picks", "Two picks", ["first", "second"], True)]

    def test_group_and_ungroup_are_undoable_edits(self, editor):
        editor.ungroup("picks")
        assert editor.groups == {}
        assert not any(n["type"] == "group" for n in editor._flow.nodes)
        assert "parentId" not in _canvas(editor, "first")
        assert editor.to_model().items[1].x == 300
        group_id = editor.group_nodes(["data", "first"], "Load and pick")
        assert group_id == "Load_and_pick"
        assert _canvas(editor, "data")["parentId"] == group_node_id(group_id)
        frame = _canvas(editor, group_node_id(group_id))
        assert [h["id"] for h in frame["data"]["outputs"]] == ["out:table", "out:picked"]
        assert editor.undo(whole_runs=False)
        assert editor.groups == {}
        assert editor.undo(whole_runs=False)
        assert list(editor.groups) == ["picks"]
        assert _canvas(editor, "second")["parentId"] == group_node_id("picks")

    def test_grouping_refuses_grouped_and_unknown_nodes(self, editor):
        with pytest.raises(ValueError, match="already in group 'picks'"):
            editor.group_nodes(["first", "data"])
        with pytest.raises(ValueError, match="No component"):
            editor.group_nodes(["nope"])
        with pytest.raises(ValueError, match="at least one"):
            editor.group_nodes([])

    def test_connections_update_the_ports(self, editor):
        editor.disconnect("first", "picked", "out", "picked")
        frame = _canvas(editor, group_node_id("picks"))
        assert frame["data"]["outputs"] == []
        editor.connect("second", "picked", "out", "picked")
        frame = _canvas(editor, group_node_id("picks"))
        assert [h["id"] for h in frame["data"]["outputs"]] == ["out:picked"]
        assert frame["data"]["handle_map"][-1]["node"] == "second"

    def test_removing_the_last_node_removes_the_group(self, editor):
        editor.remove_component("first")
        assert editor.groups["picks"].nodes == ["second"]
        editor.remove_component("second")
        assert editor.groups == {}
        assert not any(n["type"] == "group" for n in editor._flow.nodes)

    def test_expanding_pushes_overlapping_neighbours_just_clear_and_collapsing_moves_them_back(
        self, editor
    ):
        editor._flow.patch_node_props("out", {"position": {"x": 540, "y": 0}})
        editor.expand_group("picks")
        assert editor.groups["picks"].collapsed is False
        _x0, _y0, x1, _y1 = editor._group_box(
            editor._groups["picks"], editor._absolute_positions()
        )
        assert _canvas(editor, "out")["position"] == {"x": x1 + 24, "y": 0}
        assert _canvas(editor, "data")["position"] == {"x": 0, "y": 0}
        editor.collapse_group("picks")
        assert _canvas(editor, "out")["position"] == {"x": 540, "y": 0}

    def test_expanding_leaves_nodes_it_does_not_overlap(self, editor):
        before = dict(_canvas(editor, "out")["position"])
        editor.expand_group("picks")
        assert _canvas(editor, "out")["position"] == before

    def test_a_node_moved_after_the_push_stays(self, editor):
        editor._flow.patch_node_props("out", {"position": {"x": 540, "y": 0}})
        editor.expand_group("picks")
        editor._flow.patch_node_props("out", {"position": {"x": 2000, "y": 0}})
        editor.collapse_group("picks")
        assert _canvas(editor, "out")["position"] == {"x": 2000, "y": 0}

    def test_collapse_from_the_canvas_is_recorded(self, editor):
        editor._flow.collapse_group(group_node_id("picks"), False)
        assert editor.groups["picks"].collapsed is False
        assert editor.dirty

    def test_a_frame_the_page_ungroups_is_forgotten(self, editor):
        editor._flow.ungroup(group_node_id("picks"))
        assert editor.groups == {}
        assert {i["instance_id"] for i in editor._tile_items} == {"data", "first", "second", "out"}
        assert len(editor._dataflow_graph.edges) == 3

    def test_delete_group_deletes_its_nodes(self, editor):
        assert editor.delete_group("picks") == ["first", "second"]
        assert editor.groups == {}
        assert {i["instance_id"] for i in editor._tile_items} == {"data", "out"}

    def test_a_new_group_gets_a_numbered_label(self, editor):
        editor.ungroup("picks")
        assert editor.group_nodes(["first"]) == "Group_1"
        assert editor.group_nodes(["second"]) == "Group_2"
        assert editor.groups["Group_2"].label == "Group 2"
        assert (
            editor.edit_history.versions[-1].metadata["label"] == "Grouped 1 component as Group 2"
        )

    def test_moving_nodes_keeps_children_relative_to_a_refitted_frame(self, editor):
        editor.expand_group("picks")
        editor._move_nodes({"first": (1000, 500), "data": (0, 40)})
        frame = _canvas(editor, group_node_id("picks"))["position"]
        positions = editor._absolute_positions()
        assert positions["first"] == {"x": 1000, "y": 500}
        assert positions["second"] == {"x": 300, "y": 200}
        assert positions["data"] == {"x": 0, "y": 40}
        assert frame == {"x": 300 - 24, "y": 200 - 24 - 32}
        assert _canvas(editor, "first")["position"] == {
            "x": 1000 - frame["x"],
            "y": 500 - frame["y"],
        }
        model = {i.instance_id: (i.x, i.y) for i in editor.to_model().items}
        assert model["first"] == (1000, 500)

    def test_pushed_nodes_are_remembered_across_redraws(self, editor):
        editor._flow.patch_node_props("out", {"position": {"x": 540, "y": 0}})
        editor.expand_group("picks")
        editor._erase_canvas_groups()
        editor._draw_groups(list(editor._groups.values()))
        editor.collapse_group("picks")
        assert _canvas(editor, "out")["position"] == {"x": 540, "y": 0}

    def test_a_collapsed_port_shows_the_inner_ports_value(self, editor, monkeypatch):
        shown = []
        monkeypatch.setattr(
            editor,
            "_show_inspection",
            lambda key, title, value, *a, **k: shown.append((key, title, value)),
        )
        payload = {
            "node_id": group_node_id("picks"),
            "handle_id": "out:picked",
            "direction": "output",
            "position": {"x": 0, "y": 0},
        }
        editor._on_handle_clicked(payload, editor._flow)
        assert shown == [(("handle", "first:picked"), "picked (output · str)", "a")]

    def test_moving_nodes_patches_members_relative_to_the_frame_it_keeps(
        self, editor, monkeypatch
    ):
        def refuse(*args, **kwargs):
            raise AssertionError("moving nodes must not take frames off or put them back")

        monkeypatch.setattr(editor._flow, "ungroup", refuse)
        monkeypatch.setattr(editor._flow, "group_nodes", refuse)
        patches = []
        patch = editor._flow.patch_node_props
        monkeypatch.setattr(
            editor._flow, "patch_node_props", lambda i, p: (patches.append((i, p)), patch(i, p))
        )
        editor._move_nodes({"first": (500, 100), "second": (560, 300)})
        frame = group_node_id("picks")
        assert any(n["id"] == frame for n in editor._flow.nodes)
        assert (frame, {"position": {"x": 500 - 24, "y": 100 - 24 - 32}}) in patches
        # A member is only patched when its place in the frame changes.
        assert ("second", {"position": {"x": 84, "y": 256}}) in patches
        assert all(i != "first" for i, _ in patches)
        assert _canvas(editor, "first")["position"] == {"x": 24, "y": 56}
        assert editor._absolute_positions()["second"] == {"x": 560, "y": 300}

    def test_arranging_lays_a_group_out_as_one_block_with_others_outside_it(self, editor):
        editor.expand_group("picks")
        before = editor._absolute_positions()
        offset = (
            before["second"]["x"] - before["first"]["x"],
            before["second"]["y"] - before["first"]["y"],
        )
        editor.arrange_canvas()
        after = editor._absolute_positions()
        assert (
            after["second"]["x"] - after["first"]["x"],
            after["second"]["y"] - after["first"]["y"],
        ) == offset
        x0, y0, x1, y1 = editor._group_box(editor._groups["picks"], after)
        for node in ("data", "out"):
            p = after[node]
            assert not (x0 <= p["x"] <= x1 and y0 <= p["y"] <= y1), node

    def test_arranging_keeps_groups(self, editor):
        editor.arrange_canvas()
        assert editor.groups["picks"].nodes == ["first", "second"]
        assert _canvas(editor, "first")["parentId"] == group_node_id("picks")

    def test_rename(self, editor):
        editor.rename_group("picks", "Renamed")
        assert _canvas(editor, group_node_id("picks"))["label"] == "Renamed"
        assert dashboard_to_dict(editor)["groups"]["picks"]["label"] == "Renamed"


class TestStores:
    def _model(self):
        model = DashboardModel(dashboard_id="d1", user_id="alice", title="T")
        model.groups = [DashboardGroup("g", "G", ["a"], True)]
        return model

    def test_memory_store(self):
        store = MemoryDashboardStore()
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").groups == self._model().groups

    def test_sqlite_store_and_malformed_rows(self, tmp_path):
        store = DashboardStore(tmp_path / "db.sqlite")
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").groups == self._model().groups
        with closing(sqlite3.connect(tmp_path / "db.sqlite")) as conn, conn:
            conn.execute("UPDATE dashboards SET groups_json = '[{\"id\": 1}]'")
        assert store.load_dashboard("alice", "d1").groups == []

    def test_json_store(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").groups == self._model().groups
        path = next(tmp_path.glob("*.json"))
        data = json.loads(path.read_text())
        data["groups"] = "bad"
        path.write_text(json.dumps(data))
        assert store.load_dashboard("alice", "d1").groups == []


class TestOps:
    def test_group_and_ungroup(self, editor):
        ops = DashboardOps(editor)
        ops.ungroup("picks")
        result = ops.group_nodes(["first", "second"], label="Picks again")
        assert result["group"] == "Picks_again"
        assert result["inputs"] == [{"name": "table", "connects": ["first.table", "second.table"]}]
        assert result["outputs"] == [{"name": "picked", "connects": ["first.picked"]}]
        with pytest.raises(OperationError, match="already in group"):
            ops.group_nodes(["first"])
        with pytest.raises(OperationError, match="No group"):
            ops.ungroup("nope")


class TestAgent:
    async def test_the_agent_groups_and_ungroups_without_approval(self, editor):
        from .test_agent_tools import _runner

        ops = DashboardOps(editor)
        runner = _runner(
            ops,
            ("ungroup", {"name": "picks"}),
            ("group_nodes", {"nodes": ["data", "first"], "label": "Load"}),
        )
        output = await runner.run("Group the table with the first pick")
        assert "Grouped as 'Load'" in output
        assert editor.groups["Load"].nodes == ["data", "first"]
        assert "picks" not in editor.groups

    async def test_a_mistake_goes_back_to_the_model(self, editor):
        from .test_agent_tools import _runner

        runner = _runner(DashboardOps(editor), ("group_nodes", {"nodes": ["first"]}))
        output = await runner.run("Group first")
        assert "already in group 'picks'" in output


def test_the_schema_accepts_groups(editor):
    jsonschema = pytest.importorskip("jsonschema")
    from panel_flowdash.agent import dashboard_schema

    schema = dashboard_schema(editor.component_specs)
    doc = dashboard_to_dict(editor)
    jsonschema.validate(doc, schema)
    doc["groups"]["picks"]["size"] = 3
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, schema)


AREAS_DOC = DOC.replace(
    "edges:",
    """layout:
  tiles:
    - {node: data, area: top}
    - {node: first, area: sidebar}
    - {node: second}
    - {node: out}
edges:""",
)


class TestTileAreas:
    def test_a_version_4_file_keeps_groups_and_tile_areas(self):
        data = dashboard_from_yaml(AREAS_DOC)
        assert data["groups"][0]["nodes"] == ["first", "second"]
        areas = {e["instance_id"]: e.get("area") for e in data["tile_layout"]}
        assert areas == {"data": "top", "first": "sidebar", "second": None, "out": None}

    def test_grouping_and_collapsing_never_move_tiles(self):
        editor = _editor()
        try:
            editor.new_dashboard("Picks")
            editor.import_dashboard(AREAS_DOC)

            def tiles():
                model = editor.to_model()
                return model.tile_layout, model.responsive_layouts

            before = tiles()
            editor.expand_group("picks")
            editor.collapse_group("picks")
            editor.ungroup("picks")
            editor.group_nodes(["data", "first", "out"], "Mixed")
            editor.collapse_group("Mixed")
            editor.arrange_canvas()
            assert tiles() == before
            areas = {
                e["node"]: e.get("area") for e in dashboard_to_dict(editor)["layout"]["tiles"]
            }
            assert areas["data"] == "top" and areas["first"] == "sidebar"
        finally:
            editor.dispose()
