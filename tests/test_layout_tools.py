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

from panel_flowdash import FlowDash
from panel_flowdash.agent import DashboardOps, NotApplied
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.auto_layout import resize_in_row
from panel_flowdash.text_components import TEXT_COMPONENTS

from .test_agent_ops import COMPONENTS
from .test_editor_auto_layout import EXTRA, _tiles

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@pytest.fixture
def ops():
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA, **TEXT_COMPONENTS},
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
            _charts(ops, "k1", "k2", "k3", "k4")
            ops.arrange_tiles(order=["k1", "k2", "k3", "k4", "a", "b"])
            for name in ("k1", "k2", "k3", "k4"):
                ops.place_tile(name, width=25)

        _request(ops, during)
        # Laying out the request's new tiles when it ended put them back at full width, last.
        widths = _widths(ops)
        assert [widths[k] for k in ("k1", "k2", "k3", "k4")] == [25, 25, 25, 25]
        assert _shown(ops)[:4] == ["k1", "k2", "k3", "k4"]
        assert ops.describe_layout().splitlines() == [
            "Row 1: k1 25% 320px | k2 25% 320px | k3 25% 320px | k4 25% 320px",
            "Row 2: title 100% 96px",
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
        ops.place_tile("a", width=25)
        assert (_widths(ops)["a"], _widths(ops)["b"]) == (25, 75)

    def test_a_narrower_tile_lets_the_next_row_move_up(self, ops):
        _charts(ops, "a", "b", "c", "d")
        ops.arrange_tiles()
        ops.place_tile("a", width=25)
        ops.place_tile("b", width=25)
        # The chart left alone in the row below fills it.
        assert ops.describe_layout().splitlines() == [
            "Row 1: a 25% 320px | b 25% 320px | c 50% 320px",
            "Row 2: d 100% 320px",
        ]

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
