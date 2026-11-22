"""The layout algorithms that place nodes on the canvas and tiles on the grid."""

import itertools

import pytest

from panel_flowdash.auto_layout import (
    COLUMN_GAP,
    ROW_GAP,
    NodeBox,
    TileBox,
    arrange_graph,
    arrange_tiles,
    assign_columns,
    band_widths,
    insert_tiles,
    order_rows,
    place_nodes,
    stack_tiles,
    tile_kind,
)

WIDTH = 240

NODES = [
    NodeBox("title", WIDTH, 120, "view"),
    NodeBox("turbines", WIDTH, 100, "source"),
    NodeBox("state", WIDTH, 120, "widget"),
    NodeBox("years", WIDTH, 100, "widget"),
    NodeBox("filtered", WIDTH, 160, "transform"),
    NodeBox("kpi", WIDTH, 100, "view"),
    NodeBox("timeline", WIDTH, 100, "view"),
    NodeBox("makers", WIDTH, 100, "view"),
    NodeBox("table", WIDTH, 100, "view"),
]
EDGES = [
    ("turbines", "state"),
    ("turbines", "filtered"),
    ("state", "filtered"),
    ("years", "filtered"),
    *(("filtered", view) for view in ("kpi", "timeline", "makers", "table")),
]


def _boxes(nodes, positions):
    return {n.id: (*positions[n.id], n.width, n.height) for n in nodes if n.id in positions}


def _overlapping(boxes):
    pairs = []
    for (a, (ax, ay, aw, ah)), (b, (bx, by, bw, bh)) in itertools.combinations(boxes.items(), 2):
        if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
            pairs.append((a, b))
    return pairs


class TestColumns:
    def test_columns_follow_the_dataflow(self):
        column = assign_columns(NODES, EDGES)
        assert column["turbines"] == 0
        assert column["state"] == column["years"] == 1
        assert column["filtered"] == 2
        assert {column[v] for v in ("kpi", "timeline", "makers", "table")} == {3}

    def test_a_control_sits_just_before_the_node_it_feeds(self):
        nodes = [NodeBox(i, WIDTH, 100) for i in ("a", "b", "c", "d", "knob")]
        edges = [("a", "b"), ("b", "c"), ("c", "d"), ("knob", "d")]
        column = assign_columns(nodes, edges)
        assert column["knob"] == column["d"] - 1 == 2

    def test_unwired_nodes_go_in_columns_by_role(self):
        nodes = [
            NodeBox("view", WIDTH, 100, "view"),
            NodeBox("source", WIDTH, 100, "source"),
            NodeBox("transform", WIDTH, 100, "transform"),
            NodeBox("widget", WIDTH, 100, "widget"),
        ]
        column = assign_columns(nodes, [])
        assert column == {"source": 0, "widget": 1, "transform": 2, "view": 3}

    def test_a_cycle_does_not_hang(self):
        nodes = [NodeBox(i, WIDTH, 100) for i in "abc"]
        column = assign_columns(nodes, [("a", "b"), ("b", "c"), ("c", "a")])
        assert set(column) == {"a", "b", "c"}


class TestOrdering:
    def test_barycenter_ordering_removes_crossings(self):
        # a1 feeds b2 and a2 feeds b1: the second column flips to match the first.
        nodes = [NodeBox(i, WIDTH, 100) for i in ("a1", "a2", "b1", "b2")]
        edges = [("a1", "b2"), ("a2", "b1")]
        column = assign_columns(nodes, edges)
        layers = order_rows(nodes, edges, column)
        assert layers == [["a1", "a2"], ["b2", "b1"]]

    def test_initial_order_breaks_ties(self):
        nodes = [NodeBox(i, WIDTH, 100, "view") for i in ("x", "y")]
        layers = order_rows(nodes, [], {"x": 0, "y": 0}, initial={"x": 50, "y": 10})
        assert layers == [["y", "x"]]


class TestArrangeGraph:
    def test_no_overlaps(self):
        positions = arrange_graph(NODES, EDGES)
        assert _overlapping(_boxes(NODES, positions)) == []

    def test_every_edge_points_right(self):
        positions = arrange_graph(NODES, EDGES)
        for source, target in EDGES:
            assert positions[target][0] > positions[source][0]

    def test_columns_are_spaced_by_card_width(self):
        positions = arrange_graph(NODES, EDGES)
        xs = sorted({x for x, _ in positions.values()})
        assert [b - a for a, b in itertools.pairwise(xs)] == [WIDTH + COLUMN_GAP] * (len(xs) - 1)

    def test_nodes_in_a_column_keep_the_row_gap(self):
        positions = arrange_graph(NODES, EDGES)
        views = sorted(positions[v][1] for v in ("kpi", "timeline", "makers", "table"))
        assert [b - a for a, b in itertools.pairwise(views)] == [100 + ROW_GAP] * 3

    def test_a_chain_is_a_straight_line(self):
        nodes = [NodeBox(i, WIDTH, 100) for i in "abc"]
        positions = arrange_graph(nodes, [("a", "b"), ("b", "c")])
        assert len({y for _, y in positions.values()}) == 1

    def test_the_layout_starts_at_its_origin(self):
        positions = arrange_graph(NODES, EDGES, origin=(100, 50))
        assert min(x for x, _ in positions.values()) == 100
        assert min(y for _, y in positions.values()) == 50

    def test_unwired_nodes_go_below_the_graph(self):
        positions = arrange_graph(NODES, EDGES)
        bottom = max(positions[n.id][1] + n.height for n in NODES if n.id != "title")
        assert positions["title"][1] > bottom

    def test_results_are_deterministic(self):
        assert arrange_graph(NODES, EDGES) == arrange_graph(list(NODES), list(EDGES))

    def test_empty(self):
        assert arrange_graph([], []) == {}


class TestPlaceNodes:
    def test_fixed_nodes_never_move(self):
        fixed = {"turbines": (500.0, 500.0), "filtered": (900.0, 480.0)}
        positions = place_nodes(NODES, EDGES, fixed)
        assert not set(positions) & set(fixed)
        assert set(positions) == {n.id for n in NODES} - set(fixed)

    def test_new_nodes_overlap_nothing(self):
        fixed = {"turbines": (500.0, 500.0), "filtered": (900.0, 480.0), "kpi": (1260.0, 480.0)}
        positions = place_nodes(NODES, EDGES, fixed)
        assert _overlapping(_boxes(NODES, {**fixed, **positions})) == []

    def test_new_nodes_follow_the_dataflow_around_fixed_ones(self):
        fixed = {"turbines": (500.0, 500.0), "filtered": (1220.0, 480.0)}
        positions = place_nodes(NODES, EDGES, fixed)
        # The controls go in the column between the source and the filter.
        assert 500 < positions["state"][0] < 1220
        assert 500 < positions["years"][0] < 1220
        for view in ("kpi", "timeline", "makers", "table"):
            assert positions[view][0] > 1220

    def test_new_views_stack_in_their_order_beside_what_feeds_them(self):
        fixed = arrange_graph(NODES[:5], EDGES)
        positions = place_nodes(NODES, EDGES, fixed)
        views = ["kpi", "timeline", "makers", "table"]
        ys = [positions[v][1] for v in views]
        assert ys == sorted(ys)
        assert len({positions[v][0] for v in views}) == 1

    def test_a_group_wired_to_no_fixed_node_goes_below_everything(self):
        fixed = {"turbines": (0.0, 0.0)}
        nodes = [*NODES[:2], NodeBox("a", WIDTH, 100), NodeBox("b", WIDTH, 100)]
        positions = place_nodes(nodes, [("a", "b")], fixed)
        assert positions["a"][1] > 100
        assert positions["b"][0] > positions["a"][0]

    def test_without_fixed_nodes_it_is_a_full_layout(self):
        assert place_nodes(NODES, EDGES, {}) == arrange_graph(NODES, EDGES)

    def test_results_are_deterministic(self):
        fixed = {"turbines": (500.0, 500.0)}
        assert place_nodes(NODES, EDGES, fixed) == place_nodes(NODES, EDGES, dict(fixed))


class TestTileKind:
    @pytest.mark.parametrize(
        ("args", "kwargs", "kind"),
        [
            (("view", ["view"], {"height": 96}), {"text": True}, "text"),
            (("view", ["text"], None), {}, "text"),
            (("widget", [], {"width": 360}), {}, "control"),
            (("source", [], {"width": 360}), {}, "data"),
            (("transform", [], None), {}, "data"),
            (("view", ["view", "table"], {"width": 560}), {}, "table"),
            (("view", ["kpi"], {"width": 600}), {}, "kpi"),
            (("view", [], {"width": 280, "height": 130}), {}, "kpi"),
            (("view", ["chart"], {"width": 560}), {}, "chart"),
            (("view", [], None), {}, "chart"),
        ],
    )
    def test_kinds(self, args, kwargs, kind):
        assert tile_kind(*args, **kwargs) == kind


def _rows(entries):
    """Group entries into the rows a grid of 12 columns shows them in."""
    rows, row, total = [], [], 0
    for entry in entries:
        if total + entry["width"] > 100.05:
            rows.append(row)
            row, total = [], 0
        row.append(entry)
        total += entry["width"]
    rows.append(row)
    return rows


TILES = [
    TileBox("title", "text", {"height": 96}),
    TileBox("timeline", "chart", {"width": 560, "height": 320}),
    TileBox("kpi_a", "kpi", {"width": 280, "height": 130}),
    TileBox("table", "table", {"width": 560, "height": 360}),
    TileBox("makers", "chart", {"width": 420, "height": 300}),
    TileBox("kpi_b", "kpi", {"width": 280, "height": 130}),
    TileBox("kpi_c", "kpi", {"width": 280, "height": 110}),
    TileBox("picker", "control", {"width": 360, "height": 90}),
    TileBox("map", "chart", {"width": 760, "height": 460}),
    TileBox("source", "data", {"width": 360, "height": 150}),
]


def _kpis(count, heading=True):
    tiles = [TileBox("title", "text", {"height": 96})] if heading else []
    tiles += [TileBox(f"kpi_{i}", "kpi", {"width": 280, "height": 130}) for i in range(count)]
    tiles += [TileBox(f"chart_{i}", "chart", {"width": 560, "height": 320}) for i in range(2)]
    return {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}


class TestArrangeTiles:
    def test_kinds_go_in_order_with_the_leading_heading_first(self):
        order = [e["instance_id"] for e in arrange_tiles(TILES, 1200)]
        assert order == [
            "title",
            "picker",
            "kpi_a",
            "kpi_b",
            "kpi_c",
            "timeline",
            "makers",
            "map",
            "table",
            "source",
        ]

    def test_rows_share_one_height(self):
        for row in _rows(list(_kpis(4).values())):
            assert len({e.get("height") for e in row}) == 1

    def test_kpis_and_controls_take_their_default_width(self):
        entries = {e["instance_id"]: e for e in arrange_tiles(TILES, 1200)}
        # 280 px of 1200 is 3 of 12 columns, 360 px is 4.
        assert [entries[k]["width"] for k in ("kpi_a", "kpi_b", "kpi_c")] == [25] * 3
        assert entries["kpi_c"]["height"] == 130
        # The control and the KPIs don't fit one row, so the KPIs start their own; the
        # leading heading finishes the control's row.
        assert (entries["title"]["width"], entries["picker"]["width"]) == (66.67, 33.33)
        assert entries["picker"]["height"] == 90
        assert entries["source"]["width"] == 33.33

    def test_charts_go_two_to_a_row(self):
        entries = {e["instance_id"]: e for e in arrange_tiles(TILES, 1200)}
        assert entries["timeline"]["width"] == entries["makers"]["width"] == 50
        assert entries["timeline"]["height"] == entries["makers"]["height"] == 320
        assert entries["map"]["width"] == entries["table"]["width"] == 100
        tiles = [TileBox(f"c{i}", "chart", {"width": 560, "height": 320}) for i in range(3)]
        # The odd chart out takes the row, up to twice its default width.
        assert [e["width"] for e in arrange_tiles(tiles, 1200)] == [50, 50, 100]
        tiles[2] = TileBox("c2", "chart", {"width": 420, "height": 320})
        assert arrange_tiles(tiles, 1200)[2]["width"] == round(100 * 8 / 12, 2)

    def test_controls_and_kpis_that_do_not_fit_a_row_keep_to_their_own(self):
        tiles = [
            TileBox("select", "control", {"width": 360, "height": 90}),
            TileBox("toggle", "control", {"width": 360, "height": 72}),
            *(TileBox(f"kpi_{i}", "kpi", {"width": 280, "height": 130}) for i in range(2)),
            TileBox("table", "table", {"height": 360}),
        ]
        entries = arrange_tiles(tiles, 1200)
        assert [e["instance_id"] for e in _rows(entries)[1]][:2] == ["kpi_0", "kpi_1"]

    @pytest.mark.parametrize(
        ("count", "title", "kpi"), [(1, 75, 25), (2, 50, 25), (3, 100, 25), (4, 100, 25)]
    )
    def test_a_heading_finishes_a_row_of_kpis(self, count, title, kpi):
        # Three KPIs leave 3 columns, too narrow for the heading and for any later tile.
        entries = _kpis(count)
        assert entries["title"]["width"] == title
        assert {entries[f"kpi_{i}"]["width"] for i in range(count)} == {kpi}

    @pytest.mark.parametrize(("count", "kpi"), [(1, 58.33), (2, 33.33), (3, 25)])
    def test_a_chart_pair_stays_together(self, count, kpi):
        # The grid would move the pair's first chart into room of 6 columns or more, so
        # the KPIs widen, alike, only until less is left; the rest stays empty.
        entries = _kpis(count, heading=False)
        assert {entries[f"kpi_{i}"]["width"] for i in range(count)} == {kpi}
        assert entries["chart_0"]["width"] == entries["chart_1"]["width"] == 50

    @pytest.mark.parametrize(("count", "chart"), [(1, 75), (2, 50)])
    def test_a_lone_chart_fills_the_room_kpis_leave(self, count, chart):
        tiles = [TileBox(f"kpi_{i}", "kpi", {"width": 280, "height": 130}) for i in range(count)]
        tiles.append(TileBox("chart", "chart", {"width": 560, "height": 320}))
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        assert entries["chart"]["width"] == chart
        assert (entries["chart"]["height"], entries["kpi_0"]["height"]) == (320, 130)

    def test_no_chart_fills_the_room_when_a_later_one_would_move_under_the_kpis(self):
        tiles = [TileBox(f"kpi_{i}", "kpi", {"width": 280, "height": 130}) for i in range(2)]
        tiles += [TileBox(f"chart_{i}", "chart", {"width": 560, "height": 320}) for i in range(3)]
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        # Two charts of 6 columns follow, so the KPIs widen instead, to at most twice their width.
        assert entries["kpi_0"]["width"] == entries["kpi_1"]["width"] == 50
        assert entries["chart_0"]["width"] == entries["chart_1"]["width"] == 50

    def test_room_no_later_tile_fits_stays_empty(self):
        tiles = [
            TileBox("select", "control", {"width": 360, "height": 90}),
            TileBox("toggle", "control", {"width": 360, "height": 72}),
            TileBox("chart_0", "chart", {"width": 560, "height": 320}),
            TileBox("chart_1", "chart", {"width": 560, "height": 320}),
        ]
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        assert entries["select"]["width"] == entries["toggle"]["width"] == 33.33
        assert entries["chart_0"]["width"] == entries["chart_1"]["width"] == 50

    def test_widening_stops_at_twice_the_width(self):
        tiles = [TileBox(f"kpi_{i}", "kpi", {"width": 200, "height": 130}) for i in range(2)]
        # Charts at most 4 columns wide neither fill the room nor leave it alone.
        tiles += [
            TileBox(f"chart_{i}", "chart", {"height": 320}, max_size={"width": 400})
            for i in range(3)
        ]
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        assert entries["kpi_0"]["width"] == entries["kpi_1"]["width"] == round(100 * 4 / 12, 2)

    def test_source_tiles_go_last_when_shown(self):
        tiles = [
            TileBox("source", "data", {"width": 360, "height": 150}),
            TileBox("toggle", "control", {"width": 360, "height": 72}),
            TileBox("chart", "chart", {"width": 560, "height": 320}),
        ]
        order = [e["instance_id"] for e in arrange_tiles(tiles, 1200)]
        assert order == ["toggle", "chart", "source"]

    def test_a_lone_control_keeps_its_width(self):
        tiles = [
            TileBox("toggle", "control", {"width": 360, "height": 72}),
            TileBox("chart", "chart", {"width": 560, "height": 320}),
        ]
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        assert entries["toggle"] == {
            "instance_id": "toggle",
            "width": 33.33,
            "height": 72,
            "index": 0,
        }

    def test_indexes_follow_the_order(self):
        entries = arrange_tiles(TILES, 1200)
        assert [e["index"] for e in entries] == list(range(len(TILES)))

    def test_a_heading_stays_with_the_tile_after_it(self):
        tiles = [
            TileBox("kpi", "kpi", {"width": 280}),
            TileBox("charts_heading", "text", {"height": 96}),
            TileBox("chart", "chart", {"width": 560}),
            TileBox("table_heading", "text", {"height": 96}),
            TileBox("table", "table", {}),
            TileBox("notes", "text", {"height": 220}),
        ]
        order = [e["instance_id"] for e in arrange_tiles(tiles, 1200)]
        assert order == ["kpi", "charts_heading", "chart", "table_heading", "table", "notes"]

    def test_narrow_widths_put_fewer_tiles_in_a_row(self):
        entries = {e["instance_id"]: e for e in arrange_tiles(TILES, 768)}
        assert entries["timeline"]["width"] == 100
        assert all(len(row) == 1 for row in _rows(arrange_tiles(TILES, 360)))

    def test_size_hints_are_respected(self):
        tiles = [
            TileBox("a", "kpi", {"width": 200, "height": 100}, max_size={"height": 90}),
            TileBox("b", "kpi", {"width": 200, "height": 140}, min_size={"width": 700}),
        ]
        entries = {e["instance_id"]: e for e in arrange_tiles(tiles, 1200)}
        assert entries["a"]["height"] == 90
        assert entries["a"]["width"] == round(100 * 2 / 12, 2)
        # A minimum width of 700 px rounds up to 7 of 12 columns.
        assert entries["b"]["width"] == round(100 * 7 / 12, 2)
        (chart,) = arrange_tiles(
            [TileBox("c", "chart", {"height": 300}, max_size={"width": 300})], 1200
        )
        assert chart["width"] == 25

    def test_other_keys_of_an_entry_survive(self):
        previous = {"kpi_a": {"instance_id": "kpi_a", "width": 10, "index": 7, "framed": False}}
        entries = {e["instance_id"]: e for e in arrange_tiles(TILES, 1200, previous)}
        assert entries["kpi_a"]["framed"] is False
        assert entries["kpi_a"]["width"] == 25

    def test_text_without_a_height_hint_measures_its_content(self):
        (entry,) = arrange_tiles([TileBox("t", "text")], 1200)
        assert "height" not in entry

    def test_results_are_deterministic(self):
        assert arrange_tiles(TILES, 1200) == arrange_tiles(list(TILES), 1200)


class TestInsertTiles:
    def test_appending_leaves_the_other_entries_alone(self):
        layout = [
            {"instance_id": "kpi", "index": 0, "width": 40, "height": 150},
            {"instance_id": "chart", "index": 1, "width": 60, "height": 400},
        ]
        kinds = {"kpi": "kpi", "chart": "chart"}
        new = [TileBox("table", "table", {"height": 360})]
        result = insert_tiles(layout, ["kpi", "chart"], kinds, new, 1200)
        assert result[:2] == layout
        assert result[2] == {"instance_id": "table", "index": 2, "width": 100, "height": 360}

    def test_appending_to_tiles_without_entries_adds_only_the_new_one(self):
        kinds = {"a": "chart", "b": "chart"}
        new = [TileBox("t", "table", {"height": 360})]
        result = insert_tiles([], ["a", "b"], kinds, new, 1200)
        assert result == [{"instance_id": "t", "width": 100, "height": 360}]

    def test_an_entry_saying_what_the_grid_does_anyway_is_left_out(self):
        result = insert_tiles([], ["a"], {"a": "chart"}, [TileBox("t", "table")], 1200)
        assert result == []

    def test_a_new_tile_joins_its_kind(self):
        layout = [
            {"instance_id": "kpi_a", "index": 0, "width": 25, "height": 130},
            {"instance_id": "chart", "index": 1, "width": 100, "height": 400},
        ]
        kinds = {"kpi_a": "kpi", "chart": "chart"}
        new = [TileBox("kpi_b", "kpi", {"height": 120})]
        result = insert_tiles(layout, ["kpi_a", "chart"], kinds, new, 1200)
        assert [e["instance_id"] for e in result] == ["kpi_a", "kpi_b", "chart"]
        assert [e["index"] for e in result] == [0, 1, 2]
        assert result[1]["width"] == 25
        assert result[1]["height"] == 130
        assert {k: v for k, v in result[2].items() if k != "index"} == {
            k: v for k, v in layout[1].items() if k != "index"
        }

    def test_a_new_kind_goes_before_later_kinds(self):
        layout = [{"instance_id": "table", "index": 0, "width": 100}]
        result = insert_tiles(
            layout, ["table"], {"table": "table"}, [TileBox("pick", "control")], 1200
        )
        assert [e["instance_id"] for e in result] == ["pick", "table"]

    def test_other_keys_of_a_new_tiles_entry_survive(self):
        previous = {"kpi": {"instance_id": "kpi", "width": 10, "framed": False}}
        layout = [{"instance_id": "chart", "index": 0, "width": 100}]
        result = insert_tiles(
            layout, ["chart"], {"chart": "chart"}, [TileBox("kpi", "kpi")], 1200, previous
        )
        (entry,) = [e for e in result if e["instance_id"] == "kpi"]
        assert entry["framed"] is False and entry["width"] == 25

    def test_hidden_tiles_stay_last(self):
        layout = [
            {"instance_id": "gone", "index": 0, "visible": False},
            {"instance_id": "chart", "index": 1, "width": 100},
        ]
        kinds = {"gone": "chart", "chart": "chart"}
        result = insert_tiles(layout, ["chart"], kinds, [TileBox("kpi", "kpi")], 1200)
        assert [e["instance_id"] for e in result] == ["kpi", "chart", "gone"]
        assert [e["index"] for e in result] == [0, 1, 2]

    def test_a_new_chart_shares_the_row_of_a_chart_the_editor_placed_alone(self):
        layout = [{"instance_id": "a", "index": 0, "width": 100, "height": 300}]
        new = [TileBox("b", "chart", {"height": 320})]
        resizable = {"a": TileBox("a", "chart", {"height": 300})}
        result = insert_tiles(layout, ["a"], {"a": "chart"}, new, 1200, None, resizable)
        assert [(e["instance_id"], e["width"], e["height"]) for e in result] == [
            ("a", 50, 320),
            ("b", 50, 320),
        ]

    def test_charts_added_one_by_one_pair_up(self):
        layout, order, kinds, resizable = [], [], {}, {}
        for name in ("a", "b", "c", "d", "e"):
            box = TileBox(name, "chart", {"height": 300})
            layout = insert_tiles(layout, order, kinds, [box], 1200, None, resizable)
            order.append(name)
            kinds[name] = "chart"
            resizable[name] = box
        widths = {e["instance_id"]: e.get("width", 100) for e in layout}
        assert widths == {"a": 50, "b": 50, "c": 50, "d": 50, "e": 100}

    def test_a_chart_someone_placed_keeps_its_width(self):
        layout = [{"instance_id": "a", "index": 0, "width": 100, "height": 300}]
        result = insert_tiles(layout, ["a"], {"a": "chart"}, [TileBox("b", "chart")], 1200)
        assert [e["width"] for e in result] == [100, 100]
        assert result[0] == layout[0]

    def test_charts_too_wide_for_half_a_row_do_not_pair(self):
        layout = [{"instance_id": "a", "index": 0, "width": 100}]
        wide = TileBox("b", "chart", min_size={"width": 700})
        resizable = {"a": TileBox("a", "chart")}
        result = insert_tiles(layout, ["a"], {"a": "chart"}, [wide], 1200, None, resizable)
        assert [e["width"] for e in result] == [100, 100]


def test_stack_tiles_puts_each_tile_on_a_row_of_its_own():
    tiles = [TileBox("k", "kpi", {"width": 280, "height": 130}), TileBox("c", "chart")]
    previous = {"k": {"instance_id": "k", "width": 25, "framed": False}}
    assert stack_tiles(tiles, previous) == [
        {"instance_id": "k", "width": 100, "index": 0, "height": 130, "framed": False},
        {"instance_id": "c", "width": 100, "index": 1},
    ]


def test_band_widths_use_the_narrowest_width_of_each_band():
    assert band_widths([768, 1200], ["xs", "sm", "md"]) == {"xs": 360, "sm": 768, "md": 1200}


def test_unconnected_groups_go_side_by_side():
    nodes, edges = [], []
    for i in range(40):
        nodes += [NodeBox(f"s{i}", WIDTH, 100, "source"), NodeBox(f"v{i}", WIDTH, 100, "view")]
        edges.append((f"s{i}", f"v{i}"))
    positions = arrange_graph(nodes, edges)
    boxes = _boxes(nodes, positions)
    assert _overlapping(boxes) == []
    height = max(y + h for _, y, _, h in boxes.values())
    width = max(x + w for x, _, w, _ in boxes.values())
    # Stacked, the 40 pairs would be 40 * (100 + ROW_GAP) tall.
    assert height < 40 * (100 + ROW_GAP) / 3
    assert width > height
    for source, target in edges:
        assert positions[target][0] > positions[source][0]
