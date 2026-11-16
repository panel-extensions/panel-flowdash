"""Saved items and edges that cannot be restored are kept, shown and saved back unchanged."""

import json

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    MemoryDashboardStore,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.placeholders import PLACEHOLDER_NODE_TYPE, Placeholder


@register(page=False, component=True, provides=[{"key": "ticker", "type": "str"}])
def source(config):
    return "source"


@register(page=False, component=True, requires=[{"key": "ticker", "type": "str"}])
def sink(config):
    return "sink"


@register(page=False, component=True, requires=[{"key": "count", "type": "int"}])
def counter(config):
    return "counter"


@register(page=False, component=True, min_size={"width": 600}, max_size={"height": 100})
class Broken(Viewer):
    value = param.String()

    def __init__(self, **params):
        raise RuntimeError("boom")

    @param.output(param.String)
    def result(self):
        return self.value


COMPONENTS = {"T/source": source, "T/sink": sink, "T/counter": counter, "T/broken": Broken}


@pytest.fixture
def notices(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "panel_flowdash.editor.notify",
        lambda severity, message, **kw: sent.append((severity, message)),
    )
    return sent


def _editor(**params):
    return FlowDash(COMPONENTS, include_builtin_components=False, **params)


def _model():
    return DashboardModel(
        dashboard_id="d",
        user_id="alice",
        title="Partly broken",
        items=[
            DashboardItem(instance_id="s1", component_id="T/source", x=1, y=2),
            DashboardItem(
                instance_id="g1", component_id="Gone/thing", x=300, y=40, config={"k": [1, 2]}
            ),
            DashboardItem(instance_id="b1", component_id="T/broken", x=600, y=0),
            DashboardItem(instance_id="k1", component_id="T/sink"),
            DashboardItem(instance_id="c1", component_id="T/counter"),
        ],
        edges=[
            DashboardEdge(source="s1", source_port="ticker", target="g1", target_port="in"),
            DashboardEdge(source="s1", source_port="ticker", target="k1", target_port="ticker"),
            DashboardEdge(source="s1", source_port="ticker", target="c1", target_port="count"),
            DashboardEdge(
                source="s1",
                source_port="old_port",
                target="k1",
                target_port="x",
                transform=[{"op": "head", "n": 3}],
            ),
        ],
        tile_layout=[
            {"instance_id": "g1", "x": 0, "y": 0, "w": 4, "h": 3},
        ],
    )


class TestRoundTrip:
    async def test_unresolved_items_and_edges_are_saved_unchanged(self):
        editor = _editor(notifications=False)
        model = _model()
        editor.load_model(model)

        saved = editor.to_model()
        assert [i.to_dict() for i in saved.items] == [i.to_dict() for i in model.items]
        key = lambda e: json.dumps(e.to_dict(), sort_keys=True)
        assert sorted(map(key, saved.edges)) == sorted(map(key, model.edges))
        assert editor.graph.edges == [
            {
                "source": "s1",
                "source_port": "ticker",
                "target": "k1",
                "target_port": "ticker",
                "transform": [],
            }
        ]
        assert any(entry.get("instance_id") == "g1" for entry in saved.tile_layout)

    async def test_save_round_trips_through_a_store(self):
        store = MemoryDashboardStore()
        editor = _editor(notifications=False, store=store)
        model = _model()
        store.save_dashboard(model)
        editor.load("d")
        editor.save()

        reloaded = store.load_dashboard("alice", "d")
        assert [i.component_id for i in reloaded.items] == [i.component_id for i in model.items]
        assert len(reloaded.edges) == len(model.edges)

    async def test_import_keeps_unresolved_items(self):
        editor = _editor(notifications=False)
        source_editor = _editor(notifications=False)
        source_editor.load_model(_model())
        editor.import_dashboard(source_editor.export_dashboard())
        assert [i.instance_id for i in editor.to_model().items] == ["s1", "g1", "b1", "k1", "c1"]
        assert len(editor.to_model().edges) == 4

    async def test_save_as_keeps_unresolved_items(self):
        store = MemoryDashboardStore()
        editor = _editor(notifications=False, store=store, user="alice")
        store.save_dashboard(_model())
        revision = store.load_dashboard("alice", "d").revision
        editor.load("d")
        copy = editor.save_as("Copy")

        reloaded = store.load_dashboard("alice", copy.dashboard_id)
        assert [i.to_dict() for i in reloaded.items] == [i.to_dict() for i in _model().items]
        assert len(reloaded.edges) == 4
        assert store.load_dashboard("alice", "d").revision == revision


class TestPlaceholders:
    async def test_missing_and_failed_components_become_placeholders(self):
        editor = _editor(notifications=False)
        editor.load_model(_model())
        placeholders = editor._placeholders()
        assert {k: v.reason for k, v in placeholders.items()} == {
            "g1": "missing",
            "b1": "failed",
        }
        nodes = {n["id"]: n for n in editor._flow.nodes}
        assert nodes["g1"]["type"] == PLACEHOLDER_NODE_TYPE
        assert nodes["g1"]["data"] == {}
        assert nodes["g1"]["position"] == {"x": 300, "y": 40}
        assert "g1" not in editor.graph.node_ids

    async def test_placeholder_tiles_are_shown(self):
        editor = _editor(notifications=False)
        editor.load_model(_model())
        editor.mode = "dashboard"
        assert {"g1", "b1"} <= set(editor._grid_ids)

    async def test_placeholder_tiles_get_no_size_bounds_or_inspect_button(self):
        editor = _editor(notifications=False, inspect_outputs=True)
        model = _model()
        model.tile_layout.append({"instance_id": "b1", "x": 4, "y": 0, "w": 2, "h": 6})
        editor.load_model(model)
        editor.mode = "dashboard"
        assert "b1" not in editor._inspect_tiles
        assert editor._tile_grid[editor._grid_ids.index("b1")] is editor._tile_objects[2]
        assert editor.to_model().tile_layout == editor.layout
        (entry,) = [e for e in editor.layout if e["instance_id"] == "b1"]
        assert entry["h"] == 6

        editor.read_only = True
        editor.settings.viewers_can_inspect = True
        assert "b1" not in editor._inspect_tiles
        assert editor._tile_grid[editor._grid_ids.index("b1")] is editor._tile_objects[2]

    async def test_removing_a_placeholder_drops_it_and_its_edges(self):
        editor = _editor(notifications=False)
        editor.load_model(_model())
        editor.remove_component("g1")
        saved = editor.to_model()
        assert "g1" not in [i.instance_id for i in saved.items]
        assert all("g1" not in (e.source, e.target) for e in saved.edges)
        assert editor.dirty

    async def test_broken_edges_are_listed_and_can_be_discarded(self):
        editor = _editor(notifications=False)
        editor.load_model(_model())
        listing = editor._flow.bottom_panel[-1]
        text = listing[0].objects[0].object
        assert "Type mismatch" in text
        assert "old_port" in text
        listing[1].clicks += 1
        assert editor._unresolved_edges == []
        assert editor._flow.bottom_panel == []
        assert len(editor.to_model().edges) == 1
        assert editor.dirty

    async def test_private_placeholder_edges_are_not_described(self, monkeypatch):
        editor = _editor(notifications=False)

        def denied(item, component_id):
            if item.instance_id == "s1":
                return Placeholder("denied", "Restricted", "No access.", private=True)
            return FlowDash._item_placeholder(editor, item, component_id)

        monkeypatch.setattr(editor, "_item_placeholder", denied)
        editor.load_model(_model())
        assert "s1" not in editor.graph.node_ids
        text = editor._flow.bottom_panel[-1][0].objects[0].object
        assert "ticker" not in text and "T/source" not in text
        assert "4 connections to unavailable components" in text
        assert editor.to_model().items[0].component_id == "T/source"

    async def test_reloading_clears_previous_unresolved_state(self):
        editor = _editor(notifications=False)
        editor.load_model(_model())
        editor.load_model(DashboardModel(dashboard_id="e", user_id="alice", title="Empty"))
        assert editor._placeholders() == {}
        assert editor.to_model().edges == []


class TestNotification:
    async def test_editor_is_told(self, notices):
        _editor().load_model(_model())
        warnings = [m for s, m in notices if s == "warning"]
        assert warnings == [
            "2 components and 3 connections could not be restored. They are kept and "
            "saved unchanged; remove them to drop them from the dashboard."
        ]

    async def test_viewer_is_not_told_until_editing(self, notices):
        editor = _editor(editable=False)
        editor.load_model(_model())
        assert not [m for s, m in notices if s == "warning"]
        editor.editable = True
        assert len([m for s, m in notices if s == "warning"]) == 1
        editor.editable = False
        editor.editable = True
        assert len([m for s, m in notices if s == "warning"]) == 1

    async def test_read_only_editor_is_not_told(self, notices):
        editor = _editor(read_only=True)
        editor.load_model(_model())
        assert not [m for s, m in notices if s == "warning"]

    async def test_clean_dashboard_sends_no_warning(self, notices):
        editor = _editor()
        model = _model()
        model.items = model.items[:1]
        model.edges = []
        editor.load_model(model)
        assert not [m for s, m in notices if s == "warning"]


class TestApp:
    @pytest.mark.parametrize("can_edit", [True, False])
    async def test_only_someone_who_may_save_is_told(
        self, tmp_path, notices, monkeypatch, can_edit
    ):
        from panel_flowdash.app import FlowDashApp

        store = MemoryDashboardStore()
        model = _model()
        model.user_id = "local"
        store.save_dashboard(model)
        app = FlowDashApp(project_dir=tmp_path, store=store)
        # Left editable by a dashboard the user could edit before.
        app._editor.param.update(editable=True, read_only=False)
        monkeypatch.setattr(app, "_can_edit_dashboard", lambda _id: can_edit)

        app._load_dashboard_sync("d", edit=False, dashboard=model)

        warned = any("could not be restored" in m for s, m in notices if s == "warning")
        assert warned is can_edit
