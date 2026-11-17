"""Importing and downloading dashboards respects which components the user may use."""

import json

import pytest

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem
from panel_flowdash.editor import RESTRICTED_COMPONENT_ID, FlowDash


@register(
    page=False,
    component=True,
    title="Restricted",
    allow_groups=["finance"],
    provides=[{"key": "out"}],
)
def restricted(config):
    return "restricted"


@register(page=False, component=True, title="Open", requires=[{"key": "x", "required": False}])
def open_(config):
    return "open"


COMPONENTS = {"T/restricted": restricted, "T/open": open_}
SECRET = {"api_query": "SELECT secret"}


def _editor(store=None, allowed=False):
    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        authorize_component=lambda entry: allowed or entry.app_id != "T/restricted",
    )


def _items(model):
    return [(i.instance_id, i.component_id, i.config) for i in model.items]


def _edges(model):
    return [(e.source, e.target) for e in model.edges]


@pytest.fixture
def store():
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    model.items = [
        DashboardItem(instance_id="r1", component_id="T/restricted", config=SECRET),
        DashboardItem(instance_id="o1", component_id="T/open"),
    ]
    model.edges = [DashboardEdge(source="r1", source_port="out", target="o1", target_port="x")]
    store.save_dashboard(model)
    return store


def _loaded(store, allowed=False):
    editor = _editor(store, allowed)
    (model,) = store._all_dashboards()
    editor.load_model(store._load_any(model.dashboard_id))
    return editor, model.dashboard_id


def _app(store, monkeypatch):
    identity = Identity(user="alice", oauth_user="alice")
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
    return FlowDashApp(
        registry=normalize_components(COMPONENTS),
        store=store,
        auth_config=AuthConfig(),
        notifications=False,
        include_builtin_components=False,
    )


EVIL = {
    "title": "x",
    "items": [
        {"instance_id": "evil", "component_id": "T/restricted", "config": {"api_query": "DROP"}},
        {"instance_id": "o2", "component_id": "T/open"},
    ],
    "edges": [{"source": "evil", "source_port": "out", "target": "o2", "target_port": "x"}],
}


class TestImport:
    def test_new_restricted_items_and_their_edges_are_dropped(self):
        editor = _editor()
        editor.new_dashboard("mine")
        editor.import_dashboard(EVIL)
        model = editor.to_model()
        assert _items(model) == [("o2", "T/open", {})]
        assert model.edges == []

    def test_restricted_item_keeps_its_stored_config_and_wiring(self, store):
        editor, did = _loaded(store)
        editor.import_dashboard(
            {
                "items": [
                    {"instance_id": "r1", "component_id": "T/restricted", "config": {"a": 1}},
                    {"instance_id": "o1", "component_id": "T/open"},
                    {"instance_id": "o2", "component_id": "T/open"},
                ],
                "edges": [
                    {"source": "r1", "source_port": "out", "target": "o2", "target_port": "x"}
                ],
            }
        )
        editor.save()
        saved = store._load_any(did)
        assert _items(saved)[0] == ("r1", "T/restricted", SECRET)
        assert _edges(saved) == [("r1", "o1")]

    async def test_launcher_import_drops_restricted_items(self, monkeypatch):
        store = MemoryDashboardStore()
        app = _app(store, monkeypatch)
        model = app._import_dashboard_file("evil.json", json.dumps(EVIL))
        assert _items(store._load_any(model.dashboard_id)) == [("o2", "T/open", {})]


class TestExport:
    def test_restricted_items_keep_only_their_id_and_position(self, store):
        editor, _ = _loaded(store)
        data = editor.export_dashboard()
        assert data["items"][0] == {
            "instance_id": "r1",
            "component_id": RESTRICTED_COMPONENT_ID,
            "x": 0,
            "y": 0,
            "config": {},
        }
        assert data["edges"] == []
        assert "secret" not in json.dumps(data)

    def test_users_with_access_export_everything(self, store):
        editor, _ = _loaded(store, allowed=True)
        data = editor.export_dashboard()
        assert data["items"][0]["config"] == SECRET
        assert len(data["edges"]) == 1

    @pytest.mark.parametrize("allowed", [False, True])
    def test_reimporting_a_redacted_export_restores_the_stored_item(self, store, allowed):
        exported = _loaded(store)[0].export_dashboard()
        editor, did = _loaded(store, allowed)
        editor.import_dashboard(exported)
        editor.save()
        saved = store._load_any(did)
        assert _items(saved) == [("r1", "T/restricted", SECRET), ("o1", "T/open", {})]
        assert _edges(saved) == [("r1", "o1")]

    def test_redacted_export_into_another_dashboard_drops_the_item(self, store):
        exported = _loaded(store)[0].export_dashboard()
        editor = _editor(store, allowed=True)
        editor.new_dashboard("other")
        editor.import_dashboard(exported)
        assert _items(editor.to_model()) == [("o1", "T/open", {})]

    async def test_launcher_download_redacts_restricted_items(self, store, monkeypatch):
        app = _app(store, monkeypatch)
        (model,) = store._all_dashboards()
        assert app._download_dashboard(model.dashboard_id)
        data = json.loads(app._downloader.callback().getvalue())
        assert data["items"][0]["component_id"] == RESTRICTED_COMPONENT_ID
        assert "secret" not in json.dumps(data)
