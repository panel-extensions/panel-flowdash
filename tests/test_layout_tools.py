"""The agent's layout tools apply what they report and compose with each other.

The cases come from playtests of the dashboard assistant, where sizes and
positions the tools reported as laid out were not what the dashboard showed.
"""

import os

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps, NotApplied, OperationError
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.auto_layout import pack_rows, resize_in_row
from panel_flowdash.text_components import TEXT_COMPONENTS
from panel_flowdash.ui import tile_layout

from .test_agent_ops import COMPONENTS
from .test_editor_auto_layout import EXTRA, _tiles

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@register(
    page=False,
    component=True,
    title="Short chart",
    tags=["view", "chart"],
    requires=[{"key": "value", "type": "int", "required": False}],
    default_size={"width": 560, "height": 320},
    max_size={"height": 500},
)
def short_chart(config):
    return "short chart"


@pytest.fixture
def ops():
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA, **TEXT_COMPONENTS, "V/short": short_chart},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Mine")
    yield DashboardOps(editor)
    editor.dispose()


def _widths(ops):
    return {t["instance_id"]: t.get("width") for t in _tiles(ops.editor)}


def _shown(ops):
    return [
        t["instance_id"]
        for t in _tiles(ops.editor)
        if t.get("visible", True) and t.get("area") in (None, "grid")
    ]


def _saved(ops):
    """Return ops on a fresh editor loaded from the dashboard's saved model."""
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA, **TEXT_COMPONENTS},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.load_model(ops.editor.to_model())
    return DashboardOps(editor)


def _request(ops, during):
    deps = FlowDashDeps(ops)
    deps.start_request("Change it")
    try:
        during()
    finally:
        deps.end_request()


def _charts(ops, *names):
    for name in names:
        ops.add_node("V/chart", name=name)


class TestSizesStick:
    def test_widths_given_to_tiles_added_in_the_request_stay(self, ops):
        _request(ops, lambda: (ops.add_node("Text/Heading", name="title"), _charts(ops, "a", "b")))

        def during():
            for name in ("k1", "k2", "k3", "k4"):
                ops.add_node("V/headline", name=name)
            ops.arrange_tiles(order=["k1", "k2", "k3", "k4", "a", "b"])
            for name in ("k1", "k2", "k3", "k4"):
                ops.place_tile(name, width=25)

        _request(ops, during)
        # Laying out the request's new tiles when it ended put them back at full width, last.
        widths = _widths(ops)
        assert [widths[k] for k in ("k1", "k2", "k3", "k4")] == [25, 25, 25, 25]
        assert _shown(ops)[:5] == ["title", "k1", "k2", "k3", "k4"]
        assert ops.describe_layout().splitlines() == [
            "Row 1: title 100% 96px",
            "Row 2: k1 25% 130px | k2 25% 130px | k3 25% 130px | k4 25% 130px",
            "Row 3: a 50% 320px | b 50% 320px",
        ]

    def test_a_tile_moved_in_the_request_it_was_added_in_stays(self, ops):
        _request(ops, lambda: _charts(ops, "a", "b"))
        _request(ops, lambda: (_charts(ops, "c"), ops.place_tile("c", index=0)))
        assert _shown(ops) == ["c", "a", "b"]

    def test_arrange_tiles_keeps_the_sizes_place_tile_set(self, ops):
        def during():
            _charts(ops, "map", "areas")
            ops.place_tile("map", width=100, height=750)
            ops.arrange_tiles()

        _request(ops, during)
        tiles = {t["instance_id"]: t for t in _tiles(ops.editor)}
        assert (tiles["map"]["width"], tiles["map"]["height"]) == (100, 750)
        assert _shown(ops) == ["map", "areas"]

    def test_place_tile_after_arrange_tiles_applies(self, ops):
        def during():
            _charts(ops, "a", "b")
            ops.arrange_tiles()
            ops.place_tile("a", width=65)

        _request(ops, during)
        assert (_widths(ops)["a"], _widths(ops)["b"]) == (65, 35)

    def test_sizes_are_kept_only_within_their_request(self, ops):
        _request(ops, lambda: (_charts(ops, "a", "b"), ops.place_tile("a", width=65)))
        _request(ops, lambda: ops.arrange_tiles())
        assert (_widths(ops)["a"], _widths(ops)["b"]) == (50, 50)


class TestRebalance:
    def test_a_wider_tile_takes_room_from_its_neighbour(self, ops):
        _charts(ops, "count", "mass", "scatter")
        ops.arrange_tiles()
        ops.place_tile("count", width=65)
        widths = _widths(ops)
        # The neighbour used to wrap to a row of its own, leaving two half empty rows.
        assert (widths["count"], widths["mass"]) == (65, 35)
        assert ops.describe_layout().splitlines()[0] == "Row 1: count 65% 320px | mass 35% 320px"

    def test_a_narrower_tile_lets_its_neighbour_fill_the_row(self, ops):
        _charts(ops, "a", "b")
        ops.add_node("V/table", name="rows")
        ops.arrange_tiles()
        ops.place_tile("a", width=40)
        assert (_widths(ops)["a"], _widths(ops)["b"]) == (40, 60)

    def test_a_narrower_tile_lets_the_next_row_move_up(self):
        entries = [{"instance_id": iid, "width": 50} for iid in "abcd"]
        entries = resize_in_row(entries, "a", width=25)
        entries = resize_in_row(entries, "b", width=25)
        # The tile left alone in the row below fills it.
        assert [e["width"] for e in entries] == [25, 25, 50, 100]

    def test_a_wider_tile_is_held_where_its_neighbour_is_narrowest(self, ops):
        _charts(ops, "a", "b")
        ops.arrange_tiles()
        ops.place_tile("a", width=80)
        # A chart is laid out at least 420 px wide, 35% of the 1200 px grid.
        assert (_widths(ops)["a"], _widths(ops)["b"]) == (65, 35)
        assert "Width 80 not applied" in ops.layout_notes[0]
        ops.place_tile("a", width=100)
        # The neighbour moves to a row of its own at full width, not a 35% orphan.
        assert ops.describe_layout().splitlines() == ["Row 1: a 100% 320px", "Row 2: b 100% 320px"]

    def test_a_new_height_aligns_the_row(self, ops):
        def place():
            _charts(ops, "chart")
            ops.add_node("V/table", name="rows")
            ops.place_tile("rows", index=0, width=50)
            ops.place_tile("chart", width=50)

        _request(ops, place)
        _request(ops, lambda: ops.place_tile("rows", height=600))
        heights = {t["instance_id"]: t.get("height") for t in _tiles(ops.editor)}
        assert heights == {"rows": 600, "chart": 600}

    @pytest.mark.parametrize("count", [2, 3, 4])
    def test_a_full_width_tile_leaves_no_orphan(self, ops, count):
        names = [f"c{i}" for i in range(count)]
        _charts(ops, *names, "after")
        ops.arrange_tiles(style="stack")
        for name in names:
            ops.place_tile(name, width=round(100 / count, 2))
        ops.sized_tiles.clear()
        ops.place_tile("c0", width=100)
        rows = ops.describe_layout().splitlines()
        assert rows[0] == "Row 1: c0 100% 320px"
        for row in rows[1:]:
            widths = [float(t.split()[1].rstrip("%")) for t in row.split(": ", 1)[1].split(" | ")]
            assert sum(widths) == pytest.approx(100, abs=0.1), rows

    def test_a_replayed_full_width_chart_keeps_its_row_mate_whole(self, ops):
        # Playtest s3 t3: place_tile(timeline, width=100, height=500) after arrange_tiles.
        _request(
            ops,
            lambda: (
                _charts(ops, "timeline", "states", "manu"),
                ops.add_node("V/table", name="rows"),
            ),
        )
        _request(
            ops, lambda: (ops.arrange_tiles(), ops.place_tile("timeline", width=100, height=500))
        )
        assert ops.describe_layout().splitlines() == [
            "Row 1: timeline 100% 500px",
            "Row 2: states 50% 320px | manu 50% 320px",
            "Row 3: rows 100% 360px",
        ]

    def test_tiles_sized_on_purpose_keep_their_size(self):
        entries = [
            {"instance_id": "a", "width": 25},
            {"instance_id": "b", "width": 25},
            {"instance_id": "c", "width": 50},
            {"instance_id": "d", "width": 50},
        ]
        resized = resize_in_row(entries, "c", width=25, fixed={"a", "b", "c"})
        assert [e["width"] for e in resized] == [25, 25, 25, 50]

    def test_neighbours_shrink_no_further_than_their_minimum(self):
        entries = [{"instance_id": "a", "width": 50}, {"instance_id": "b", "width": 50}]
        resized = resize_in_row(entries, "a", width=60, bounds={"b": (35.0, 100.0)})
        assert [e["width"] for e in resized] == [60, 40]
        resized = resize_in_row(entries, "a", width=80, bounds={"b": (35.0, 100.0)})
        assert [e["width"] for e in resized] == [65, 35]


class TestPositions:
    def test_a_title_goes_in_front_of_the_tiles(self, ops):
        _request(ops, lambda: _charts(ops, "a", "b"))
        _request(
            ops,
            lambda: ops.add_node(
                "Text/Heading", {"text": "Penguins", "size": "Title"}, name="title"
            ),
        )
        assert _shown(ops)[0] == "title"

    def test_a_title_added_with_the_tiles_goes_first(self, ops):
        def during():
            _charts(ops, "a", "b")
            ops.add_node("Text/Heading", {"text": "Wind", "size": "Title"}, name="title")

        _request(ops, during)
        assert _shown(ops) == ["title", "a", "b"]

    @pytest.mark.parametrize("arrange", [False, True])
    def test_a_title_moved_to_the_top_before_its_charts_stays_first(self, ops, arrange):
        # Lumen's add_text places a Title heading at index 0 before the charts are added.
        def during():
            ops.add_node("Text/Heading", {"text": "Wind", "size": "Title"}, name="title")
            ops.place_tile("title", index=0)
            _charts(ops, "a", "b", "c")
            if arrange:
                ops.arrange_tiles()

        _request(ops, during)
        assert _shown(_saved(ops)) == ["title", "a", "b", "c"]

    def test_text_before_a_hidden_tile_is_not_applied(self, ops):
        ops.add_node("T/number", name="source")
        _charts(ops, "a")
        ops.add_node("Text/Heading", {"text": "Data"}, name="section")
        with pytest.raises(NotApplied, match="'source' has no tile on the dashboard"):
            ops.place_tile("section", before="source")
        assert ops.hidden_tiles() == ["source"]

    def test_other_text_still_goes_last(self, ops):
        _charts(ops, "a")
        ops.add_node("Text/Markdown", {"text": "Notes"}, name="notes")
        assert _shown(ops)[-1] == "notes"

    def test_place_tile_puts_a_tile_before_or_after_another(self, ops):
        _charts(ops, "a", "b", "c")
        ops.place_tile("c", before="a")
        assert _shown(ops) == ["c", "a", "b"]
        ops.place_tile("c", after="a")
        assert _shown(ops) == ["a", "c", "b"]
        ops.place_tile("a", after="b")
        assert _shown(ops) == ["c", "b", "a"]


class TestReports:
    async def test_tiles_without_a_height_are_reported_as_auto(self, ops):
        # A playtest asked for a table left of a chart at the same height and
        # the model set the widths only, so the layout says what height they get.
        def place():
            _charts(ops, "chart")
            ops.add_node("V/table", name="rows")

        _request(ops, place)
        for name in ("rows", "chart"):
            tile_layout.update_tile(ops.editor, ops._instance(name), height=None)

        def model(messages, info):
            results = [
                p
                for m in messages
                for p in getattr(m, "parts", [])
                if isinstance(p, ToolReturnPart)
            ]
            if not results:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "place_tile",
                            {"name": "rows", "width": 50, "index": 0},
                            tool_call_id="1",
                        ),
                        ToolCallPart(
                            "place_tile",
                            {"name": "chart", "width": 50, "index": 1},
                            tool_call_id="2",
                        ),
                    ]
                )
            return ModelResponse(parts=[TextPart(str(results[-1].content))])

        agent = Agent(FunctionModel(model), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
        ops.versioned = False
        result = await agent.run("Table left, chart right, same height", deps=FlowDashDeps(ops))
        assert (
            result.output.splitlines()[-1]
            == "Row 1: rows 50% auto (content) | chart 50% auto (content)"
        )
        _request(ops, lambda: ops.place_tile("rows", height=400))
        assert ops.describe_layout() == "Row 1: rows 50% 400px | chart 50% 400px"

    def test_a_hidden_tile_is_reported(self, ops):
        ops.add_node("T/number", name="source")
        ops.add_node("V/chart", name="chart")
        ops.connect("source.value", "chart.value")
        ops.place_tile("source", width=50)
        assert "'source' is not shown on the dashboard" in " ".join(ops.layout_notes)
        assert "visible=True" in " ".join(ops.layout_notes)
        assert ops.describe_layout().endswith("Hidden: source")
        ops.place_tile("source", width=50, visible=True)
        assert ops.layout_notes == []

    def test_a_chart_refused_from_the_top_bar_is_not_applied(self, ops):
        _charts(ops, "a")
        with pytest.raises(NotApplied, match="'a' stays on the grid"):
            ops.place_tile("a", area="top")

    async def test_refusals_in_parallel_leave_the_turn_its_answer(self, ops):
        _charts(ops, "k1", "k2", "k3", "k4", "k5")
        names = ["k1", "k2", "k3", "k4", "k5"]

        def model(messages, info):
            results = [
                p
                for m in messages
                for p in getattr(m, "parts", [])
                if isinstance(p, ToolReturnPart)
            ]
            if not results:
                return ModelResponse(
                    parts=[
                        ToolCallPart("place_tile", {"name": n, "area": "top"}, tool_call_id=n)
                        for n in names
                    ]
                )
            return ModelResponse(parts=[TextPart(" / ".join(str(r.content) for r in results))])

        agent = Agent(
            FunctionModel(model),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset(["place_tile"])],
            retries=3,
        )
        result = await agent.run("Put the KPIs in the top bar", deps=FlowDashDeps(ops))
        assert result.output.count("Not applied:") == 5
        assert "only controls, KPIs and text fit in the top bar" in result.output

    async def test_place_tile_returns_the_layout(self, ops):
        _charts(ops, "a", "b")
        ops.arrange_tiles()

        def model(messages, info):
            results = [
                p
                for m in messages
                for p in getattr(m, "parts", [])
                if isinstance(p, ToolReturnPart)
            ]
            if not results:
                return ModelResponse(
                    parts=[
                        ToolCallPart("place_tile", {"name": "a", "width": 65}, tool_call_id="1")
                    ]
                )
            return ModelResponse(parts=[TextPart(str(results[0].content))])

        agent = Agent(FunctionModel(model), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
        ops.versioned = False
        result = await agent.run("Make a bigger", deps=FlowDashDeps(ops))
        assert result.output == (
            "Laid out the tile of 'a'.\nLayout now:\nRow 1: a 65% 320px | b 35% 320px"
        )


class TestPlaceTileEdits:
    def test_a_refused_call_leaves_the_requests_tiles_waiting(self, ops):
        _request(ops, lambda: _charts(ops, "a"))
        deps = FlowDashDeps(ops)
        deps.start_request("Add b and a title")
        try:
            _charts(ops, "b")
            ops.add_node("Text/Heading", {"text": "T", "size": "Title"}, name="title")
            before = ops.editor.layout
            with pytest.raises(NotApplied):
                ops.place_tile("b", area="top")
            with pytest.raises(OperationError, match="width is a percentage"):
                ops.place_tile("b", width=0)
            assert ops.editor.layout == before
        finally:
            deps.end_request()
        assert ops.describe_layout().splitlines()[0] == "Row 1: title 100% 96px"

    def test_index_and_width_are_one_edit(self, ops):
        _request(ops, lambda: _charts(ops, "a", "b", "c"))
        edits = []
        record = ops.editor._record_edit
        ops.editor._record_edit = lambda *a, **k: (edits.append(a), record(*a, **k))[1]
        ops.place_tile("c", index=0, width=60)
        assert edits == [("Changed the tile layout",)]
        assert _shown(ops)[0] == "c"

    def test_a_height_alone_leaves_the_width_free(self, ops):
        def during():
            _charts(ops, "a", "b")
            ops.arrange_tiles()
            ops.place_tile("b", height=400)
            ops.place_tile("a", width=65)

        _request(ops, during)
        assert ops.describe_layout().splitlines() == ["Row 1: a 65% 400px | b 35% 400px"]

    def test_a_width_alone_leaves_the_height_free(self, ops):
        def during():
            _charts(ops, "a", "b")
            ops.arrange_tiles()
            ops.place_tile("b", width=40)
            ops.place_tile("a", height=500)

        _request(ops, during)
        assert ops.describe_layout().splitlines() == ["Row 1: a 60% 500px | b 40% 500px"]

    def test_a_height_past_the_components_bound_is_noted(self, ops):
        ops.add_node("V/short", name="s")
        entry = ops.place_tile("s", height=700)
        assert entry["height"] == 500
        assert ops.layout_notes == [
            "Height 700 not applied: 's' is at most 500 px tall, so it got height 500."
        ]

    def test_only_the_current_requests_waiting_tiles_are_laid_out(self, ops):
        editor = ops.editor
        _request(ops, lambda: _charts(ops, "a"))
        editor._defer_tile_layout("other")
        _charts(ops, "b")
        editor._deferred_by_run["other"] = {"b"}
        editor._defer_tile_layout(None)
        _charts(ops, "c")
        editor._deferred_by_run[None] = {"c"}
        editor._settle_deferred_tiles()
        assert editor._deferred_tiles == {"b"}
        assert editor._deferred_by_run["other"] == {"b"}
        assert editor._deferred_by_run[None] == set()


class TestRows:
    def test_the_grid_packs_rows(self, ops):
        grid = ops.editor._tile_grid
        assert (grid.layout_mode, grid.row_resize) == ("rows", "split")

    def test_a_gap_is_not_filled_by_a_later_tile(self):
        rows = pack_rows(
            [
                {"instance_id": "a", "width": 60, "height": 320},
                {"instance_id": "b", "width": 60, "height": 320},
                {"instance_id": "c", "width": 35, "height": 320},
            ]
        )
        assert [[e["instance_id"] for e, _ in row] for row in rows] == [["a"], ["b", "c"]]

    def test_a_tile_stacks_under_a_shorter_one_beside_a_taller_one(self):
        entries = [
            {"instance_id": "map", "width": 60, "height": 620},
            {"instance_id": "k1", "width": 40, "height": 300},
            {"instance_id": "k2", "width": 40, "height": 300},
            {"instance_id": "k3", "width": 40, "height": 300},
        ]
        rows = pack_rows(entries)
        assert [[(e["instance_id"], under) for e, under in row] for row in rows] == [
            [("map", None), ("k1", None), ("k2", 1)],
            [("k3", None)],
        ]
        unsized = pack_rows([{**e, "height": None} for e in entries])
        assert [len(row) for row in unsized] == [2, 2]

    def test_the_margins_count_in_whether_a_tile_fits_below_another(self):
        # Beside a 600 px table, two rows of 130 px KPIs leave 600 - 2 * 130 = 340 px, but
        # with the 20 px margin of each tile the room is 620 - 2 * 150 = 300 px, too short
        # for a 320 px chart, which starts a row below the table, as the browser shows it.
        entries = [
            {"instance_id": "t", "width": 50, "height": 600},
            *({"instance_id": f"k{i}", "width": 25, "height": 130} for i in range(1, 5)),
            {"instance_id": "c", "width": 50, "height": 320},
        ]
        rows = pack_rows(entries)
        assert [[(e["instance_id"], under) for e, under in row] for row in rows] == [
            [("t", None), ("k1", None), ("k2", None), ("k3", 1), ("k4", 2)],
            [("c", None)],
        ]
        entries[-1]["height"] = 280
        assert [(e["instance_id"], u) for e, u in pack_rows(entries)[0]][-1] == ("c", 3)

    def test_the_report_names_a_stacked_tile(self, ops):
        _charts(ops, "map", "k1", "k2")
        layout = [
            {"instance_id": "map", "width": 60, "height": 640, "index": 0},
            {"instance_id": "k1", "width": 40, "height": 300, "index": 1},
            {"instance_id": "k2", "width": 40, "height": 300, "index": 2},
        ]
        ops.editor._set_tile_layout(layout, {})
        assert ops.describe_layout().splitlines() == [
            "Row 1: map 60% 640px | k1 40% 300px | k2 40% 300px (under k1)"
        ]


class TestWidthBounds:
    def test_a_chart_is_not_narrower_than_a_chart_can_be_read(self, ops):
        _charts(ops, "a", "b")
        ops.arrange_tiles()
        entry = ops.place_tile("a", width=15)
        assert entry["width"] == 35
        assert ops.layout_notes == [
            "Width 15 not applied: a chart tile is at least 420 px wide, 35% of the 1200 px "
            "grid, so 'a' got width 35."
        ]
        assert ops.describe_layout().splitlines() == ["Row 1: a 35% 320px | b 65% 320px"]

    def test_a_kpi_may_be_narrower_than_a_chart(self, ops):
        ops.add_node("V/headline", name="k")
        assert ops.place_tile("k", width=20)["width"] == 20
        assert ops.layout_notes == []
