"""Tests for tile frames: the component's ``framed`` hint and each tile's own frame option."""

import logging

import panel as pn
import pytest
import yaml

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps, OperationError, dashboard_schema
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
    dashboard_to_yaml,
)
from panel_flowdash.dashboard_store import (
    DashboardItem,
    DashboardModel,
    DashboardStore,
    MemoryDashboardStore,
)
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.registry import build_registry
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.ui.tile import resolve_framed
from panel_flowdash.ui.tile_layout import FRAME_DEFAULT, frame_choices, update_tile


@register(page=False, component=True, title="Plain")
def plain(config):
    return pn.pane.Markdown("plain")


@register(page=False, component=True, title="Rule", framed=False)
def rule(config):
    return pn.pane.Markdown("---")


@register(page=False, component=True, title="Boxed", framed=True)
def boxed(config):
    return pn.pane.Markdown("boxed")


COMPONENTS = {"T/plain": plain, "T/rule": rule, "T/boxed": boxed}


def _editor(**params):
    return FlowDash(
        components=COMPONENTS, include_builtin_components=False, notifications=False, **params
    )


def _model(tile_layout=(), **kwargs):
    return DashboardModel(
        dashboard_id="d1",
        user_id="alice",
        title="Frames",
        items=[
            DashboardItem(instance_id="p", component_id="T/plain"),
            DashboardItem(instance_id="r", component_id="T/rule"),
            DashboardItem(instance_id="b", component_id="T/boxed"),
        ],
        tile_layout=list(tile_layout),
        **kwargs,
    )


def _frames(editor) -> dict[str, bool]:
    return {iid: tile.framed for iid, tile in editor._inspect_tiles.items()}


def _shown(editor, model):
    editor.load_model(model)
    editor.mode = "dashboard"
    return editor


@pytest.mark.parametrize(
    "tile, component, dashboard, expected",
    [
        (None, None, True, True),
        (None, None, False, False),
        (None, False, True, False),
        (None, True, False, True),
        (True, False, False, True),
        (False, True, True, False),
    ],
)
def test_tile_option_wins_over_component_over_dashboard(tile, component, dashboard, expected):
    assert resolve_framed(tile, component, dashboard) is expected


class TestComponentHint:
    def test_hint_reaches_the_spec(self):
        specs = _editor()._component_specs
        assert specs["T/plain"].framed is None
        assert specs["T/rule"].framed is False
        assert specs["T/boxed"].framed is True

    def test_builtin_defaults(self):
        specs = FlowDash(notifications=False)._component_specs
        framed = {spec.title: spec.framed for spec in specs.values()}
        assert framed["Divider"] is False
        assert framed["Heading"] is False
        for title in ("Markdown", "Notes", "KPI", "Image", "Select", "Slider"):
            assert framed[title] is None, title

    def test_register_refuses_a_non_boolean(self):
        with pytest.raises(TypeError, match="framed must be True, False or None"):
            register(page=False, component=True, framed="no")

    def test_source_scan_reads_the_hint(self, tmp_path, caplog):
        section = tmp_path / "project" / "Lib"
        section.mkdir(parents=True)
        (section / "__init__.py").write_text("")
        for name, value in (("rule", "False"), ("odd", "'no'")):
            (section / f"{name}.py").write_text(
                "from panel_flowdash import register\n\n"
                f"@register(page=False, component=True, framed={value})\n"
                "def app(config):\n"
                "    return 'x'\n"
            )
        with caplog.at_level(logging.WARNING, logger="panel_flowdash"):
            registry = build_registry(tmp_path / "project")
        assert registry["Lib/rule"].metadata.framed is False
        assert registry["Lib/odd"].metadata.framed is None
        assert "ignoring framed" in caplog.text


class TestEditor:
    def test_component_hint_overrides_the_dashboard(self):
        editor = _shown(_editor(), _model())
        assert _frames(editor) == {"p": True, "r": False, "b": True}

        editor.settings.tile_cards = False
        assert _frames(editor) == {"p": False, "r": False, "b": True}

    def test_tile_option_overrides_the_component(self):
        editor = _shown(_editor(), _model())
        update_tile(editor, "r", framed=True)
        update_tile(editor, "p", framed=False)
        assert _frames(editor) == {"p": False, "r": True, "b": True}

        update_tile(editor, "r", framed=None)
        assert _frames(editor)["r"] is False
        assert editor.dirty

    def test_saved_option_applies_on_load(self):
        layout = [{"instance_id": "b", "index": 0, "framed": False}]
        editor = _shown(_editor(), _model(layout))
        assert _frames(editor) == {"p": True, "r": False, "b": False}

    def test_option_stays_out_of_the_grid_layout_and_survives_browser_edits(self):
        editor = _shown(_editor(), _model())
        update_tile(editor, "p", framed=False, width=50)
        grid = editor._tile_grid
        assert all("framed" not in entry for entry in grid.layout)

        # The browser reports a drag with only the keys TileGrid knows.
        grid.layout = [
            {"index": 1, "width": 50, "height": 200, "visible": True},
            {"index": 0, "width": 100, "height": 200, "visible": True},
            {"index": 2, "width": 100, "height": 200, "visible": True},
        ]
        saved = {e["instance_id"]: e for e in editor.to_model().tile_layout}
        assert saved["p"]["framed"] is False
        assert saved["p"]["index"] == 1
        assert "framed" not in saved["r"]

    def test_option_is_saved_from_wiring_mode(self):
        editor = _shown(_editor(), _model())
        update_tile(editor, "r", framed=True)
        editor.mode = "wiring"
        saved = {e["instance_id"]: e for e in editor.to_model().tile_layout}
        assert saved["r"]["framed"] is True

    def test_frame_choices_name_the_default(self):
        editor = _shown(_editor(), _model())
        assert frame_choices(editor, "p") == {
            "Default (framed)": FRAME_DEFAULT,
            "Framed": True,
            "Unframed": False,
        }
        assert next(iter(frame_choices(editor, "r"))) == "Default (unframed)"

    def test_unframed_tiles_keep_the_frame_inset(self):
        editor = _shown(_editor(), _model())
        tiles = editor._inspect_tiles
        assert tiles["p"].body.margin == tiles["r"].body.margin == 8

    def test_inspector_follows_the_setting_and_frames_set_elsewhere(self):
        editor = _shown(_editor(), _model())
        select_on_canvas(editor._flow, node_id="r")
        frame = editor._inspector.tile_options.frame_select
        assert frame.value == FRAME_DEFAULT

        update_tile(editor, "r", framed=True)
        assert frame.value is True

        update_tile(editor, "r", framed=None)
        editor.settings.tile_cards = False
        assert next(iter(frame.options)) == "Default (unframed)"
        update_tile(editor, "p", framed=None)
        select_on_canvas(editor._flow, node_id="p")
        frame = editor._inspector.tile_options.frame_select
        editor.settings.tile_cards = True
        assert next(iter(frame.options)) == "Default (framed)"

        frame.value = False
        assert editor._inspect_tiles["p"].framed is False

    def test_tile_tab_sets_the_option(self):
        editor = _shown(_editor(), _model())
        editor.mode = "dashboard"
        select_on_canvas(editor._flow, node_id="r")
        frame = editor._inspector.tile_options.frame_select
        assert frame.value == FRAME_DEFAULT
        frame.value = True
        assert _frames(editor)["r"] is True
        assert {e["instance_id"]: e for e in editor.layout}["r"]["framed"] is True
        frame.value = FRAME_DEFAULT
        assert "framed" not in {e["instance_id"]: e for e in editor.layout}.get("r", {})


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda tmp_path: MemoryDashboardStore(),
        lambda tmp_path: DashboardStore(tmp_path / "d.db"),
        lambda tmp_path: JSONFolderDashboardStore(tmp_path / "dashboards"),
    ],
    ids=["memory", "sqlite", "json"],
)
def test_stores_round_trip_the_option(tmp_path, store_factory):
    store = store_factory(tmp_path)
    editor = _editor(store=store, user="alice")
    model = editor.new_dashboard("Frames")
    editor.add_component("T/plain")
    rule_id = editor.add_component("T/rule")
    editor.mode = "dashboard"
    update_tile(editor, rule_id, framed=True)
    editor.save()

    other = _editor(store=store, user="alice")
    other.load_model(store.load_dashboard("alice", model.dashboard_id))
    other.mode = "dashboard"
    assert {e["instance_id"]: e.get("framed") for e in other.layout}[rule_id] is True
    assert other._inspect_tiles[rule_id].framed is True


class TestYAML:
    def test_round_trip(self):
        data = _model([{"instance_id": "r", "index": 0, "width": 50, "framed": True}]).to_dict()
        doc = dashboard_to_dict(data)
        assert doc["flowdash"] == 1
        assert doc["layout"]["tiles"] == [{"node": "r", "width": 50, "framed": True}]
        loaded = dashboard_from_yaml(dashboard_to_yaml(data))
        assert loaded["tile_layout"] == [
            {"instance_id": "r", "index": 0, "width": 50, "framed": True}
        ]

    def test_responsive_layouts_never_carry_the_option(self):
        data = _model(
            [{"instance_id": "r", "framed": False}],
            breakpoints=[600],
            responsive_layouts={"sm": [{"instance_id": "r", "width": 100, "framed": False}]},
        ).to_dict()
        doc = dashboard_to_dict(data)
        assert doc["layout"]["responsive"] == {"sm": [{"node": "r", "width": 100}]}

        text = "flowdash: 1\nnodes:\n  r: {component: T/rule}\nlayout:\n  responsive:\n"
        with pytest.raises(DashboardYAMLError, match=r"layout\.responsive\.sm\[0\]: unknown key"):
            dashboard_from_yaml(text + "    sm:\n      - {node: r, framed: true}\n")

    def test_invalid_option_is_refused(self):
        text = "flowdash: 1\nnodes:\n  r: {component: T/rule}\nlayout:\n  tiles:\n"
        with pytest.raises(DashboardYAMLError) as info:
            dashboard_from_yaml(text + "    - {node: r, framed: 'yes'}\n")
        assert info.value.path == "layout.tiles[0].framed"

    def test_null_means_default(self):
        text = "flowdash: 1\nnodes:\n  r: {component: T/rule}\nlayout:\n  tiles:\n"
        data = dashboard_from_yaml(text + "    - {node: r, framed: null}\n")
        assert data["tile_layout"] == [{"instance_id": "r", "index": 0}]

    def test_pre_release_files_load_as_the_current_format(self):
        text = (
            "flowdash: 3\nnodes:\n  r: {component: T/rule}\nlayout:\n  tiles:\n"
            "    - {node: r, framed: false}\n"
        )
        data = dashboard_from_yaml(text)
        assert data["tile_layout"] == [{"instance_id": "r", "index": 0, "framed": False}]
        assert dashboard_to_dict(data)["flowdash"] == 1
        assert dashboard_to_dict(data)["layout"]["tiles"] == [{"node": "r", "framed": False}]

    def test_schema_offers_the_option_on_base_tiles_only(self):
        layout = dashboard_schema({})["properties"]["layout"]["properties"]
        assert layout["tiles"]["items"]["properties"]["framed"] == {"type": ["boolean", "null"]}
        responsive = layout["responsive"]["additionalProperties"]["items"]
        assert "framed" not in responsive["properties"]


class TestAgent:
    @pytest.fixture
    def ops(self):
        editor = _editor()
        editor.new_dashboard("Mine")
        yield DashboardOps(editor)
        editor.dispose()

    def test_place_tile_sets_and_clears_the_frame(self, ops):
        name = ops.add_node("T/rule")
        assert ops.place_tile(name, frame="framed")["framed"] is True
        doc = yaml.safe_load(ops.get_dashboard())
        assert doc["layout"]["tiles"][0]["framed"] is True
        assert ops.place_tile(name, frame="unframed")["framed"] is False
        assert "framed" not in ops.place_tile(name, frame="default")

    def test_arranging_keeps_the_frame(self, ops):
        names = [ops.add_node(c) for c in ("T/plain", "T/rule", "T/boxed")]
        editor = ops.editor
        editor.mode = "dashboard"
        ops.place_tile(names[1], frame="framed")
        ops.place_tile(names[2], frame="unframed")
        ops.arrange_layout()
        frames = {e["instance_id"]: e.get("framed") for e in editor.layout}
        ids = [i["instance_id"] for i in editor._tile_items]
        assert [frames.get(i) for i in ids] == [None, True, False]
        assert [editor._inspect_tiles[i].framed for i in ids] == [True, True, False]

    def test_the_layout_at_the_end_of_an_agent_request_keeps_the_frame(self, ops):
        pytest.importorskip("pydantic_ai")
        from panel_flowdash.agent.tools import FlowDashDeps

        editor = ops.editor
        editor.mode = "dashboard"
        deps = FlowDashDeps(ops)
        deps.start_request("Add a rule")
        rule = ops.add_node("T/rule")
        ops.add_node("T/plain")
        ops.place_tile(rule, frame="framed")
        deps.end_request()
        instance_id = editor._tile_items[0]["instance_id"]
        assert {e["instance_id"]: e.get("framed") for e in editor.layout}[instance_id] is True
        assert editor._inspect_tiles[instance_id].framed is True

    def test_place_tile_refuses_an_unknown_frame(self, ops):
        name = ops.add_node("T/rule")
        with pytest.raises(OperationError, match="frame is one of default, framed, unframed"):
            ops.place_tile(name, frame="boxed")
