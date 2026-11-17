"""Imports with repeated or unknown items, and access-denied pages for ids a caller chose."""

import pytest

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.auth import Permission
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from tests.test_import_restricted_components import (  # noqa: F401
    SECRET,
    _edges,
    _items,
    _loaded,
    store,
)
from tests.test_roles_enforcement import BOB, _app, location  # noqa: F401


class TestDuplicateIds:
    def test_a_repeated_id_cannot_unplace_a_restored_restricted_item(self, store):  # noqa: F811
        editor, did = _loaded(store)
        editor.import_dashboard(
            {
                "items": [
                    {"instance_id": "r1", "component_id": "flowdash:restricted"},
                    {"instance_id": "r1", "component_id": "T/open"},
                    {"instance_id": "o1", "component_id": "T/open"},
                ],
                "edges": [],
            }
        )
        editor.save()
        saved = store._load_any(did)
        assert _items(saved) == [("r1", "T/restricted", SECRET), ("o1", "T/open", {})]
        assert _edges(saved) == [("r1", "o1")]

    def test_only_the_first_of_repeated_ids_is_imported(self, store):  # noqa: F811
        editor, _ = _loaded(store, allowed=True)
        editor.import_dashboard(
            {
                "items": [
                    {"instance_id": "o1", "component_id": "T/open", "config": {"title": "A"}},
                    {"instance_id": "o1", "component_id": "T/open", "config": {"title": "B"}},
                ],
                "edges": [],
            }
        )
        assert _items(editor.to_model()) == [("o1", "T/open", {"title": "A"})]


class TestUnknownComponents:
    def test_imports_keep_unknown_components_by_default(self, store):  # noqa: F811
        editor, _ = _loaded(store, allowed=True)
        editor.import_dashboard(
            {"items": [{"instance_id": "x1", "component_id": "Other/secret"}], "edges": []}
        )
        assert _items(editor.to_model()) == [("x1", "Other/secret", {})]

    def test_a_new_item_of_an_unknown_component_is_left_out(self, store):  # noqa: F811
        editor, _ = _loaded(store, allowed=True)
        editor.import_unknown_components = False
        editor.import_dashboard(
            {
                "items": [
                    {"instance_id": "x1", "component_id": "Other/secret", "config": {"q": "DROP"}},
                    {"instance_id": "o2", "component_id": "T/open"},
                ],
                "edges": [
                    {"source": "x1", "source_port": "out", "target": "o2", "target_port": "x"}
                ],
            }
        )
        model = editor.to_model()
        assert _items(model) == [("o2", "T/open", {})]
        assert model.edges == []

    def test_a_stored_item_of_an_unknown_component_keeps_its_stored_config(self, store):  # noqa: F811
        model = store._all_dashboards()[0]
        model = store._load_any(model.dashboard_id)
        model.items[0].component_id = "Other/secret"
        store.save_dashboard(model)
        editor, did = _loaded(store, allowed=True)
        editor.import_unknown_components = False
        editor.import_dashboard(
            {
                "items": [
                    {"instance_id": "r1", "component_id": "Other/secret", "config": {"q": "DROP"}},
                    {"instance_id": "o1", "component_id": "T/open"},
                ],
                "edges": [],
            }
        )
        editor.save()
        saved = store._load_any(did)
        assert _items(saved)[0] == ("r1", "Other/secret", SECRET)
        assert _edges(saved) == [("r1", "o1")]


class TestCallerChosenIds:
    @pytest.fixture
    def seeded(self):
        seeded = MemoryDashboardStore()
        seeded.save_dashboard(
            DashboardModel(dashboard_id="wind-data-quality", user_id="alice", title="Quality")
        )
        seeded.set_permission("wind-data-quality", Permission.from_spec(allow_users=["alice"]))
        return seeded

    def test_store_reports_caller_chosen_ids_as_guessable(self, seeded):
        created = seeded.create_dashboard("alice", "Random")
        assert not seeded.guessable_id(created.dashboard_id)
        assert seeded.guessable_id("wind-data-quality")

    async def test_denied_page_does_not_name_the_owner_of_a_chosen_id(self, seeded, location):  # noqa: F811
        app = _app(seeded, BOB)
        denied = app._missing_dashboard_view("wind-data-quality")
        missing = app._missing_dashboard_view("no-such-dashboard")
        assert (denied.icon, denied.title) == (missing.icon, missing.title)
        assert "alice" not in denied.text


class TestRepeatedIdsOnLoad:
    def _load(self, store, items):  # noqa: F811
        model = store._load_any(store._all_dashboards()[0].dashboard_id)
        model.items = [DashboardItem(**item) for item in items]
        model.edges = []
        store.save_dashboard(model)
        editor, did = _loaded(store, allowed=True)
        return editor, did

    def _tiles(self, editor):
        return [item["instance_id"] for item in editor._tile_items]

    def _nodes(self, editor):
        return [node["id"] for node in editor._flow.nodes]

    def test_a_valid_and_a_missing_item_under_one_id_load_once(self, store, caplog):  # noqa: F811
        editor, did = self._load(
            store,
            [
                {"instance_id": "a", "component_id": "T/open"},
                {"instance_id": "a", "component_id": "Gone/thing"},
            ],
        )
        assert self._nodes(editor) == ["a"]
        assert self._tiles(editor) == ["a"]
        assert "repeating the id" in caplog.text
        editor.save()
        assert _items(store._load_any(did)) == [("a", "T/open", {})]

    def test_two_missing_items_under_one_id_give_one_placeholder(self, store):  # noqa: F811
        editor, did = self._load(
            store,
            [
                {"instance_id": "g", "component_id": "Gone/one"},
                {"instance_id": "g", "component_id": "Gone/two"},
            ],
        )
        assert self._nodes(editor) == ["g"]
        assert self._tiles(editor) == ["g"]
        editor.save()
        assert _items(store._load_any(did)) == [("g", "Gone/one", {})]
