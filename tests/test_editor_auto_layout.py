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
        widths = {t["instance_id"]: t["width"] for t in tiles}
        assert widths["trend"] == widths["split"] == 50
        assert widths["rows"] == 100
        # The control and the KPI share a row at their own widths; no chart fits the rest.
        assert (widths["factor"], widths["kpi"]) == (33.33, 25)
        assert widths["source"] == widths["combined"] == 33.33

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
        # The KPI keeps its 3 columns and the chart fills the rest of its row.
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
        widths = {e["instance_id"]: e["width"] for e in ops.editor.layout}
        assert (widths["kpi"], widths["kpi_2"], widths["trend"]) == (25, 25, 50)
        ops.remove_node("kpi_2")
        widths = {e["instance_id"]: e["width"] for e in ops.editor.layout}
        assert (widths["kpi"], widths["trend"]) == (25, 75)

    def test_the_turn_names_only_the_added_node(self, ops):
        _build(ops, GRAPH[:3])
        assert ops.editor.turns[-1].summary == "Added trend. Added source. Added kpi."


class TestTools:
    def test_the_arrange_tools_are_offered(self):
        names = set(dashboard_toolset().tools)
        assert {"arrange_canvas", "arrange_layout"} <= names
        assert set(dashboard_toolset(["arrange_layout"]).tools) == {"arrange_layout"}

    async def test_an_agent_builds_a_laid_out_dashboard(self, ops):
        runner = _runner(ops, *GRAPH, ("arrange_canvas", {}), ("arrange_layout", {}))
        output = await runner.run("Build a dashboard")
        assert output == "Done: Arranged the tile layout."
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
