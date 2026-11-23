"""Nodes and tiles the agent adds without a position are laid out by the editor."""

import itertools
import os

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.tools import dashboard_toolset
from panel_flowdash.ui.node_card import CARD_WIDTH, NodeCard

from .test_agent_ops import COMPONENTS
from .test_agent_tools import _runner

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@register(
    page=False,
    component=True,
    title="Pick",
    tags=["widget"],
    provides=[{"key": "value", "type": "int"}],
    default_size={"width": 360, "height": 90},
)
def pick(config):
    config.value = 2
    return "pick"


@register(
    page=False,
    component=True,
    title="Headline",
    requires=[{"key": "value", "type": "int", "required": False}],
    default_size={"width": 280, "height": 130},
)
def headline(config):
    return "headline"


@register(
    page=False,
    component=True,
    title="Chart",
    tags=["view", "chart"],
    requires=[{"key": "value", "type": "int", "required": False}],
    default_size={"width": 560, "height": 320},
)
def chart(config):
    return "chart"


@register(
    page=False,
    component=True,
    title="Table",
    tags=["view", "table"],
    requires=[{"key": "value", "type": "int", "required": False}],
    default_size={"width": 560, "height": 360},
)
def table(config):
    return "table"


@register(
    page=False,
    component=True,
    title="Combine",
    provides=[{"key": "out", "type": "int"}],
    requires=[
        {"key": "value", "type": "int", "required": False},
        {"key": "factor", "type": "int", "required": False},
    ],
)
def combine(config):
    return "combine"


VIEWS = {"V/headline": headline, "V/chart": chart, "V/table": table}
EXTRA = {"T/pick": pick, "T/combine": combine, **VIEWS}

GRAPH = [
    ("add_node", {"component": "V/chart", "name": "trend"}),
    ("add_node", {"component": "T/number", "name": "source"}),
    ("add_node", {"component": "V/headline", "name": "kpi"}),
    ("add_node", {"component": "T/pick", "name": "factor"}),
    ("add_node", {"component": "T/combine", "name": "combined"}),
    ("add_node", {"component": "V/table", "name": "rows"}),
    ("add_node", {"component": "V/chart", "name": "split"}),
    ("connect", {"source": "source.value", "target": "combined.value"}),
    ("connect", {"source": "factor.value", "target": "combined.factor"}),
    ("connect", {"source": "combined.out", "target": "kpi.value"}),
    ("connect", {"source": "combined.out", "target": "trend.value"}),
    ("connect", {"source": "combined.out", "target": "split.value"}),
    ("connect", {"source": "combined.out", "target": "rows.value"}),
]


@pytest.fixture
def ops():
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Mine")
    yield DashboardOps(editor)
    editor.dispose()


def _build(ops, calls=GRAPH):
    for name, args in calls:
        getattr(ops, name)(**args)


def _positions(editor):
    return {n["id"]: (n["position"]["x"], n["position"]["y"]) for n in editor._flow.nodes}


def _boxes(editor):
    boxes = {}
    for node in editor._flow.nodes:
        view = node["view"]
        height = view.height if isinstance(view, NodeCard) else 100
        boxes[node["id"]] = (node["position"]["x"], node["position"]["y"], CARD_WIDTH, height)
    return boxes


def _overlaps(editor):
    found = []
    for (a, (ax, ay, aw, ah)), (b, (bx, by, bw, bh)) in itertools.combinations(
        _boxes(editor).items(), 2
    ):
        if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
            found.append((a, b))
    return found


def _tiles(editor):
    return sorted(editor.layout, key=lambda e: e.get("index", 0))


class TestCanvas:
    def test_the_agents_nodes_are_laid_out_by_the_dataflow(self, ops):
        _build(ops)
        editor = ops.editor
        positions = _positions(editor)
        assert _overlaps(editor) == []
        for source, target in [(e["source"], e["target"]) for e in editor.graph.edges]:
            assert positions[target][0] > positions[source][0]
        # The control feeding the combiner sits beside the source that feeds it too.
        assert positions["source"][0] == positions["factor"][0] < positions["combined"][0]
        views = {positions[v][0] for v in ("kpi", "trend", "split", "rows")}
        assert len(views) == 1

    def test_a_node_moved_by_hand_stays_put(self, ops):
        _build(ops, GRAPH[:6])
        editor = ops.editor
        editor._flow.patch_node_props("rows", {"position": {"x": -500, "y": -500}})
        editor._on_node_moved({"node_id": "rows"})
        _build(ops, GRAPH[6:])
        assert _positions(editor)["rows"] == (-500, -500)
        assert _overlaps(editor) == []

    def test_nodes_added_by_the_python_api_keep_the_default_placement(self, ops):
        editor = ops.editor
        editor.add_component("T/number", instance_id="a")
        editor.add_component("V/chart", instance_id="b")
        before = _positions(editor)
        assert editor.connect("a", "value", "b", "value") is True
        assert _positions(editor) == before

    def test_arrange_canvas_moves_every_node_in_one_edit(self, ops):
        _build(ops)
        editor = ops.editor
        for node_id in ("source", "kpi", "rows"):
            editor._flow.patch_node_props(node_id, {"position": {"x": 0, "y": 0}})
            editor._on_node_moved({"node_id": node_id})
        assert _overlaps(editor)
        editor.end_turn()
        ops.arrange_canvas()
        assert _overlaps(editor) == []
        assert editor.turns[-1].summary == "Arranged the dataflow."
        positions = _positions(editor)
        for source, target in [(e["source"], e["target"]) for e in editor.graph.edges]:
            assert positions[target][0] > positions[source][0]

    def test_a_replace_lays_out_only_the_nodes_without_a_position(self, ops):
        _build(ops, GRAPH[:2])
        editor = ops.editor
        editor._flow.patch_node_props("source", {"position": {"x": 1000, "y": 2000}})
        editor._on_node_moved({"node_id": "source"})
        doc = yaml.safe_load(ops.get_dashboard())
        # Positions left out of nodes already on the canvas keep theirs.
        del doc["nodes"]["source"]["position"]
        doc["nodes"]["double"] = {"component": "T/doubler"}
        doc["edges"] = [{"from": "source.value", "to": "double.value"}]
        text = f"# revision: {ops.revision()}\n" + yaml.safe_dump(doc, sort_keys=False)
        ops.replace_yaml(text)
        positions = _positions(editor)
        assert positions["source"] == (1000, 2000)
        assert positions["double"][0] > 1000
        assert _overlaps(editor) == []

    def test_a_file_without_positions_is_laid_out_on_import(self, ops):
        text = """flowdash: 2
nodes:
  rows: {component: V/table}
  combined: {component: T/combine}
  source: {component: T/number}
  factor: {component: T/pick}
edges:
  - {from: source.value, to: combined.value}
  - {from: factor.value, to: combined.factor}
  - {from: combined.out, to: rows.value}
"""
        ops.editor.import_dashboard(text)
        positions = _positions(ops.editor)
        assert positions["source"][0] == positions["factor"][0]
        assert positions["factor"][0] < positions["combined"][0] < positions["rows"][0]
        assert _overlaps(ops.editor) == []


class TestTiles:
    def test_the_agents_tiles_are_grouped_by_kind(self, ops):
        _build(ops)
        tiles = _tiles(ops.editor)
        assert [t["instance_id"] for t in tiles] == [
            "factor",
            "kpi",
            "trend",
            "split",
            "rows",
            "source",
            "combined",
        ]
        widths = {t["instance_id"]: t.get("width") for t in tiles}
        assert widths["trend"] == widths["split"] == 50
        assert widths["rows"] == 100
        # The control and the KPI share a row at their own widths; no chart fits the rest.
        assert (widths["factor"], widths["kpi"]) == (33.33, 25)
        # The source and the transform are plumbing: their tiles start hidden.
        hidden = {t["instance_id"] for t in tiles if t.get("visible") is False}
        assert hidden == {"source", "combined"}

    def test_arranging_keeps_plumbing_hidden_and_puts_shown_plumbing_last(self, ops):
        _build(ops)
        ops.arrange_layout()
        hidden = {t["instance_id"] for t in _tiles(ops.editor) if t.get("visible") is False}
        assert hidden == {"source", "combined"}
        ops.place_tile("source", visible=True)
        ops.arrange_layout()
        shown = [t["instance_id"] for t in _tiles(ops.editor) if t.get("visible", True)]
        assert shown[-1] == "source"

    def _request(self, ops, calls, during):
        from panel_flowdash.agent.tools import FlowDashDeps

        deps = FlowDashDeps(ops)
        deps.start_request("Build it")
        _build(ops, calls)
        during()
        deps.end_request()

    @pytest.mark.parametrize("who", ["agent", "person"])
    def test_a_source_shown_during_a_request_is_arranged_and_stays_shown(self, ops, who):
        from panel_flowdash.ui.tile_layout import update_tile

        def show():
            if who == "agent":
                ops.place_tile("source", visible=True)
            else:
                update_tile(ops.editor, "source", visible=True)

        self._request(ops, GRAPH, show)
        tiles = _tiles(ops.editor)
        widths = {t["instance_id"]: t.get("width") for t in tiles}
        hidden = {t["instance_id"] for t in tiles if t.get("visible") is False}
        assert hidden == {"combined"}
        # The whole grid is arranged, the shown source last.
        assert widths["trend"] == widths["split"] == 50
        assert (widths["factor"], widths["kpi"]) == (33.33, 25)
        assert [t["instance_id"] for t in tiles if t.get("visible", True)][-1] == "source"

    def test_a_chart_hidden_during_a_request_leaves_a_proper_arrangement(self, ops):
        self._request(ops, GRAPH, lambda: ops.place_tile("split", visible=False))
        tiles = _tiles(ops.editor)
        widths = {t["instance_id"]: t.get("width") for t in tiles}
        # The 5 columns the control and the KPI leave are too few for the one chart left.
        assert (widths["factor"], widths["kpi"]) == (33.33, 25)
        assert widths["trend"] == widths["rows"] == 100
        shown = [t["instance_id"] for t in tiles if t.get("visible", True)]
        assert shown == ["factor", "kpi", "trend", "rows"]

    def test_a_later_request_still_arranges(self, ops):
        self._request(ops, GRAPH[:3], lambda: ops.place_tile("source", visible=True))
        self._request(
            ops, [("add_node", {"component": "V/headline", "name": "kpi_2"})], lambda: None
        )
        tiles = _tiles(ops.editor)
        # Arranged again, not only inserted: the new KPI joins the first one's row and,
        # as the shown source would move in beside them, both widen alike.
        assert [t["instance_id"] for t in tiles] == ["kpi", "kpi_2", "trend", "source"]
        assert tiles[0]["width"] == tiles[1]["width"] == 50

    @pytest.mark.parametrize("who", ["agent", "person"])
    def test_showing_a_source_between_requests_keeps_the_layout_automatic(self, ops, who):
        from panel_flowdash.ui.tile_layout import update_tile

        self._request(ops, GRAPH[:3], lambda: None)
        if who == "agent":
            ops.place_tile("source", visible=True)
        else:
            update_tile(ops.editor, "source", visible=True)
        entry = next(t for t in _tiles(ops.editor) if t["instance_id"] == "source")
        # The shown source gets a real place, last.
        assert entry["visible"] is True and entry["width"] == 33.33 and "height" not in entry
        assert _tiles(ops.editor)[-1]["instance_id"] == "source"
        self._request(
            ops, [("add_node", {"component": "V/headline", "name": "kpi_2"})], lambda: None
        )
        self._request(
            ops, [("add_node", {"component": "V/chart", "name": "trend_2"})], lambda: None
        )
        tiles = _tiles(ops.editor)
        widths = {t["instance_id"]: t.get("width") for t in tiles}
        # Still arranged: the KPIs share a row, alike, and the charts pair up.
        assert widths["kpi"] == widths["kpi_2"]
        assert widths["trend"] == widths["trend_2"] == 50
        assert tiles[-1]["instance_id"] == "source"

    def test_hidden_sources_say_what_they_are(self, ops):
        from panel_flowdash.ui.tile_layout import off_grid

        _build(ops)
        reasons = {o.instance_id: (o.reason, o.restorable) for o in off_grid(ops.editor)}
        assert reasons == {
            "source": ("data source · hidden", True),
            "combined": ("transform · hidden", True),
        }

    def test_tiles_wait_for_the_end_of_the_agents_request(self, ops):
        from panel_flowdash.agent.tools import FlowDashDeps

        deps = FlowDashDeps(ops)
        deps.start_request("Build it")
        _build(ops, GRAPH[:3])
        assert ops.editor.layout == []
        deps.end_request()
        assert [t["instance_id"] for t in _tiles(ops.editor)] == ["kpi", "trend", "source"]

    def test_arranging_keeps_other_keys_of_a_tile(self, ops):
        _build(ops, GRAPH[:3])
        editor = ops.editor
        layout = [
            {**e, "framed": False} if e["instance_id"] == "kpi" else e for e in editor.layout
        ]
        editor._set_tile_layout(layout, {})
        ops.arrange_layout()
        (entry,) = [e for e in editor.layout if e["instance_id"] == "kpi"]
        assert entry["framed"] is False

    def test_a_tile_laid_out_by_hand_keeps_its_place(self, ops):
        _build(ops, GRAPH[:3])
        ops.place_tile("kpi", width=30, height=200, index=0)
        before = {e["instance_id"]: e for e in ops.editor.layout}
        _build(ops, GRAPH[3:])
        after = {e["instance_id"]: e for e in ops.editor.layout}
        assert {k: after["kpi"][k] for k in ("width", "height")} == {"width": 30, "height": 200}
        for name in ("trend", "source"):
            assert {k: v for k, v in after[name].items() if k != "index"} == {
                k: v for k, v in before[name].items() if k != "index"
            }
        assert "rows" in after

    def test_arrange_layout_rearranges_every_tile_in_one_edit(self, ops):
        _build(ops, GRAPH[:3])
        ops.place_tile("kpi", width=30, height=200, index=2)
        editor = ops.editor
        editor.end_turn()
        ops.arrange_layout()
        assert editor.turns[-1].summary == "Arranged the tile layout."
        assert [t["instance_id"] for t in _tiles(editor)] == ["kpi", "trend", "source"]
        # The KPI keeps the width it was given and the chart fills the rest of its row.
        assert [t["width"] for t in _tiles(editor)[:2]] == [30, 70]
        ops.arrange_tiles(keep_sizes=False)
        # Without the sizes given, the KPI gets its 3 columns again.
        assert [t["width"] for t in _tiles(editor)[:2]] == [25, 75]

    def test_arrange_layout_drops_band_layouts(self, ops):
        _build(ops, GRAPH[:3])
        editor = ops.editor
        editor.mode = "dashboard"
        editor._tile_grid.responsive_layouts = {"xs": [{"index": 0, "width": 100}]}
        ops.arrange_layout()
        assert editor._grid_config()["responsive_layouts"] == {}

    def test_removing_a_tile_closes_the_gap(self, ops):
        _build(ops, GRAPH[:3])
        ops.add_node("V/headline", name="kpi_2")
        widths = {e["instance_id"]: e.get("width") for e in ops.editor.layout}
        assert (widths["kpi"], widths["kpi_2"], widths["trend"]) == (25, 25, 50)
        ops.remove_node("kpi_2")
        widths = {e["instance_id"]: e.get("width") for e in ops.editor.layout}
        assert (widths["kpi"], widths["trend"]) == (25, 75)

    def test_charts_added_to_a_laid_out_dashboard_go_side_by_side(self, ops):
        _build(ops, GRAPH[:3])
        ops.place_tile("kpi", width=30, height=200, index=0)
        for name in ("trend_2", "trend_3", "trend_4"):
            ops.add_node("V/chart", name=name)
        ops.add_node("V/table", name="rows")
        widths = {e["instance_id"]: e.get("width") for e in ops.editor.layout}
        # The KPI keeps the size it was given and the chart beside it makes room for it.
        assert (widths["kpi"], widths["trend"]) == (30, 70)
        assert widths["trend_2"] == widths["trend_3"] == 50
        assert widths["trend_4"] == widths["rows"] == 100
        shown = [t["instance_id"] for t in _tiles(ops.editor) if t.get("visible", True)]
        assert shown == ["kpi", "trend", "trend_2", "trend_3", "trend_4", "rows"]

    def test_arrange_tiles_follows_the_order_given(self, ops):
        _build(ops)
        ops.arrange_tiles(order=["split", "rows"])
        shown = [t["instance_id"] for t in _tiles(ops.editor) if t.get("visible", True)]
        assert shown == ["factor", "kpi", "split", "trend", "rows"]

    def test_arrange_tiles_stacks_in_one_edit(self, ops):
        _build(ops)
        editor = ops.editor
        editor.end_turn()
        ops.arrange_tiles(order=["rows", "kpi"], style="stack")
        assert editor.turns[-1].summary == "Arranged the tile layout."
        tiles = [t for t in _tiles(editor) if t.get("visible", True)]
        assert [t["instance_id"] for t in tiles][:2] == ["rows", "kpi"]
        assert {t["width"] for t in tiles} == {100}
        ops.add_node("V/chart", name="trend_2")
        assert next(t for t in _tiles(editor) if t["instance_id"] == "trend_2")["width"] == 100

    def test_arrange_tiles_moves_a_heading_with_the_tile_it_titles(self):
        from panel_flowdash.text_components import TEXT_COMPONENTS

        editor = FlowDash(
            components={**COMPONENTS, **EXTRA, "Text/Heading": TEXT_COMPONENTS["Text/Heading"]},
            include_builtin_components=False,
            notifications=False,
            user="alice",
        )
        editor.new_dashboard("Mine")
        ops = DashboardOps(editor)
        try:
            ops.add_node("Text/Heading", name="title")
            ops.add_node("V/chart", name="first")
            ops.add_node("V/chart", name="second")
            for style in ("grid", "stack"):
                ops.arrange_tiles(order=["second"], style=style)
                assert [t["instance_id"] for t in _tiles(editor)] == ["second", "title", "first"]
                ops.arrange_tiles(order=["first"], style=style)
                assert [t["instance_id"] for t in _tiles(editor)] == ["title", "first", "second"]
        finally:
            editor.dispose()

    def test_new_charts_keep_a_stack_stacked(self, ops):
        _build(ops, GRAPH[:3])
        ops.arrange_tiles(style="stack")
        ops.add_node("V/chart", name="trend_2")
        ops.add_node("V/chart", name="trend_3")
        widths = {e["instance_id"]: e.get("width") for e in ops.editor.layout}
        assert widths["trend_2"] == widths["trend_3"] == 100
        ops.arrange_tiles()
        ops.add_node("V/chart", name="trend_4")
        ops.add_node("V/chart", name="trend_5")
        widths = {e["instance_id"]: e.get("width") for e in ops.editor.layout}
        assert widths["trend_4"] == widths["trend_5"] == 50

    def test_arrange_tiles_rejects_an_unknown_style_or_node(self, ops):
        from panel_flowdash.agent import OperationError

        _build(ops, GRAPH[:3])
        with pytest.raises(OperationError, match="style"):
            ops.arrange_tiles(style="masonry")
        with pytest.raises(OperationError, match="No node 'nope'"):
            ops.arrange_tiles(order=["nope"])

    def test_the_turn_names_only_the_added_node(self, ops):
        _build(ops, GRAPH[:3])
        assert ops.editor.turns[-1].summary == "Added trend. Added source. Added kpi."


class TestTools:
    def test_the_arrange_tools_are_offered(self):
        names = set(dashboard_toolset().tools)
        assert {"arrange_canvas", "arrange_tiles"} <= names
        assert set(dashboard_toolset(["arrange_tiles"]).tools) == {"arrange_tiles"}
        # The former name still selects the tool.
        assert set(dashboard_toolset(["arrange_layout"]).tools) == {"arrange_tiles"}

    async def test_an_agent_builds_a_laid_out_dashboard(self, ops):
        runner = _runner(ops, *GRAPH, ("arrange_canvas", {}), ("arrange_tiles", {}))
        output = await runner.run("Build a dashboard")
        assert output == (
            "Done: Arranged the tile layout.\nLayout now:\n"
            "Row 1: factor 33.33% 130px | kpi 25% 130px\n"
            "Row 2: trend 50% 320px | split 50% 320px\n"
            "Row 3: rows 100% 360px\n"
            "Hidden: source, combined"
        )
        editor = ops.editor
        assert _overlaps(editor) == []
        assert [t["instance_id"] for t in _tiles(editor)][:3] == ["factor", "kpi", "trend"]
        doc = yaml.safe_load(ops.get_dashboard())
        assert all("position" in node for node in doc["nodes"].values())

    async def test_the_instructions_say_layout_is_automatic(self):
        from panel_flowdash.agent.assistant import INSTRUCTIONS

        assert "Positions and tile layout are automatic" in INSTRUCTIONS
        assert "arrange_canvas" in INSTRUCTIONS


class TestHistory:
    @pytest.mark.parametrize("op", ["arrange_canvas", "arrange_layout"])
    def test_an_arrangement_is_one_version_that_undo_takes_back(self, ops, op):
        _build(ops)
        editor = ops.editor
        editor._flow.patch_node_props("source", {"position": {"x": 0, "y": 0}})
        editor._on_node_moved({"node_id": "source"})
        ops.place_tile("kpi", width=30, index=4)
        history = editor.edit_history
        history.flush()
        before = (len(history.versions), _positions(editor), _tiles(editor))
        getattr(ops, op)()
        history.flush()
        assert len(history.versions) == before[0] + 1
        assert history.versions[-1].metadata["op"] == op
        assert editor.undo()
        assert (_positions(editor), _tiles(editor)) == before[1:]
