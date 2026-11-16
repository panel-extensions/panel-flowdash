"""Tests for detecting concurrent saves of a dashboard (optimistic concurrency)."""

import json
import sqlite3
import sys
import threading
import time

import panel as pn
import panel_material_ui as pmui
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import (
    BaseDashboardStore,
    DashboardConflictError,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    DashboardStoreError,
    JSONFolderDashboardStore,
    MemoryDashboardStore,
    Permission,
    dashboard_store as dashboard_store_module,
    json_store as json_store_module,
)
from panel_flowdash.app import DASH_ROUTE_PREFIX
from panel_flowdash.editor import FlowDash
from panel_flowdash.util import page_dialog
from panel_flowdash.widgets import BUILTIN_COMPONENTS
from tests.test_store_errors import location, make_app  # noqa: F401

WIDGET = next(iter(BUILTIN_COMPONENTS))


@pytest.fixture(params=["memory", "sqlite", "json"])
def make_store(request, tmp_path):
    """Return a factory for stores sharing one backend, as separate server processes would."""
    memory = MemoryDashboardStore()

    def make():
        if request.param == "memory":
            return memory
        if request.param == "sqlite":
            return DashboardStore(tmp_path / "dashboards.db")
        return JSONFolderDashboardStore(tmp_path / "dashboards")

    return make


@pytest.fixture
def store(make_store):
    return make_store()


@pytest.fixture
def doc():
    document = Document()
    with set_curdoc(document):
        yield document


def _click(dialog, label):
    button = next(b for b in dialog.select(pmui.Button) if b.label == label)
    button.clicks += 1


class TestStoreRevisions:
    def test_create_and_save_number_revisions(self, store):
        model = store.create_dashboard("alice", "Sales")
        assert model.revision == 1
        assert store.load_dashboard("alice", model.dashboard_id).revision == 1

        store.save_dashboard(model)

        assert model.revision == 2
        assert store.load_dashboard("alice", model.dashboard_id).revision == 2

    def test_stale_save_is_rejected_and_keeps_stored_contents(self, store):
        created = store.create_dashboard("alice", "Sales")
        first = store.load_dashboard("alice", created.dashboard_id)
        second = store.load_dashboard("alice", created.dashboard_id)
        first.items = [DashboardItem(instance_id="a", component_id="X/a")]
        store.save_dashboard(first)

        second.items = [DashboardItem(instance_id="b", component_id="X/b")]
        with pytest.raises(DashboardConflictError) as info:
            store.save_dashboard(second)

        assert isinstance(info.value, DashboardStoreError)
        assert (info.value.dashboard_id, info.value.expected, info.value.current) == (
            created.dashboard_id,
            1,
            2,
        )
        assert second.revision == 1
        stored = store.load_dashboard("alice", created.dashboard_id)
        assert [item.instance_id for item in stored.items] == ["a"]
        assert stored.revision == 2

    def test_model_without_revision_saves_unconditionally(self, store):
        created = store.create_dashboard("alice", "Sales")
        store.save_dashboard(store.load_dashboard("alice", created.dashboard_id))

        model = DashboardModel(dashboard_id=created.dashboard_id, user_id="alice", title="Sales")
        store.save_dashboard(model)

        assert model.revision == 3

    def test_rename_and_share_keep_the_revision(self, store):
        created = store.create_dashboard("alice", "Sales")
        loaded = store.load_dashboard("alice", created.dashboard_id)

        assert store.rename_dashboard("alice", created.dashboard_id, "Revenue")
        assert store.set_permission(
            created.dashboard_id, Permission(allow_users=frozenset({"bob"}))
        )

        assert store.load_dashboard("alice", created.dashboard_id).revision == 1
        store.save_dashboard(loaded)
        assert loaded.revision == 2

    def test_saving_a_deleted_dashboard_recreates_it(self, store):
        created = store.create_dashboard("alice", "Sales")
        loaded = store.load_dashboard("alice", created.dashboard_id)
        assert store.delete_dashboard("alice", created.dashboard_id)

        store.save_dashboard(loaded)

        assert store.load_dashboard("alice", created.dashboard_id).revision == loaded.revision == 2

    def test_concurrent_stale_saves_let_exactly_one_through(self, make_store, monkeypatch):
        created = make_store().create_dashboard("alice", "Sales")
        check = dashboard_store_module.check_revision

        def slow_check(dashboard, stored):
            # Widen the window between reading the stored revision and writing.
            time.sleep(0.05)
            return check(dashboard, stored)

        monkeypatch.setattr(dashboard_store_module, "check_revision", slow_check)
        monkeypatch.setattr(json_store_module, "check_revision", slow_check)
        stores = [make_store() for _ in range(4)]
        models = [s.load_dashboard("alice", created.dashboard_id) for s in stores]
        barrier = threading.Barrier(len(stores))
        outcomes = []

        def save(s, model):
            barrier.wait()
            try:
                s.save_dashboard(model)
            except DashboardConflictError:
                outcomes.append("conflict")
            else:
                outcomes.append("saved")

        threads = [
            threading.Thread(target=save, args=pair) for pair in zip(stores, models, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["conflict"] * 3 + ["saved"]
        assert stores[0].load_dashboard("alice", created.dashboard_id).revision == 2


class TestSQLiteMigration:
    def test_existing_database_gains_revisions(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE dashboards (
                dashboard_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                items_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO dashboards (dashboard_id, user_id, title) VALUES ('d', 'u', 'T')"
        )
        conn.commit()
        conn.close()

        store = DashboardStore(path)
        first = store.load_dashboard("u", "d")
        second = store.load_dashboard("u", "d")
        assert first.revision == 0

        store.save_dashboard(first)
        assert store.load_dashboard("u", "d").revision == 1
        with pytest.raises(DashboardConflictError):
            store.save_dashboard(second)


class TestJSONFolderRevisions:
    def test_revision_is_written_to_the_file(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        model = store.create_dashboard("alice", "Sales")
        store.save_dashboard(model)

        assert json.loads((tmp_path / "sales.json").read_text())["revision"] == 2
        assert [p.name for p in tmp_path.iterdir()] == ["sales.json"]

    def test_file_without_revision_is_checked_as_revision_zero(self, tmp_path):
        data = {"dashboard_id": "d", "user_id": "u", "title": "T", "items": []}
        (tmp_path / "d.json").write_text(json.dumps(data))
        store = JSONFolderDashboardStore(tmp_path)
        first = store.load_dashboard("u", "d")
        second = store.load_dashboard("u", "d")
        assert first.revision == 0

        store.save_dashboard(first)

        with pytest.raises(DashboardConflictError):
            store.save_dashboard(second)

    def test_rename_does_not_revert_a_concurrent_save(self, tmp_path, monkeypatch):
        writer, renamer = JSONFolderDashboardStore(tmp_path), JSONFolderDashboardStore(tmp_path)
        created = writer.create_dashboard("alice", "Sales")
        load = JSONFolderDashboardStore._load_any
        entered = threading.Event()

        def slow_load(self, dashboard_id):
            model = load(self, dashboard_id)
            if self is renamer:
                entered.set()
                time.sleep(0.1)
            return model

        monkeypatch.setattr(JSONFolderDashboardStore, "_load_any", slow_load)
        thread = threading.Thread(
            target=renamer.rename_dashboard, args=("alice", created.dashboard_id, "Revenue")
        )
        thread.start()
        entered.wait()
        created.items = [DashboardItem(instance_id="a", component_id="X/a")]
        writer.save_dashboard(created)
        thread.join()

        stored = writer.load_dashboard("alice", created.dashboard_id)
        assert [item.instance_id for item in stored.items] == ["a"]


class LastWriterStore(BaseDashboardStore):
    """A third-party store written before revisions existed."""

    def __init__(self):
        self.models = {}

    def save_dashboard(self, dashboard):
        self.models[dashboard.dashboard_id] = DashboardModel.from_dict(dashboard.to_dict())

    def _load_any(self, dashboard_id):
        model = self.models.get(dashboard_id)
        return None if model is None else DashboardModel.from_dict(model.to_dict())

    def _all_dashboards(self):
        return list(self.models.values())

    def delete_dashboard(self, user_id, dashboard_id):
        return self.models.pop(dashboard_id, None) is not None

    def rename_dashboard(self, user_id, dashboard_id, new_title):
        return False

    def set_permission(self, dashboard_id, permission):
        return False


def _editors(store, dashboard_id, n=2):
    return [FlowDash(store=store, user="alice", dashboard=dashboard_id) for _ in range(n)]


class TestEditorSave:
    def test_stale_editor_save_raises_and_keeps_its_canvas(self, store):
        created = store.create_dashboard("alice", "Sales")
        first, second = _editors(store, created.dashboard_id)
        first.add_component(WIDGET)
        first.save()
        second.add_component(WIDGET)

        with pytest.raises(DashboardConflictError):
            second.save()

        assert second.dirty
        assert second.dashboard.revision == 1
        assert len(second._tile_items) == 1
        assert (
            store.load_dashboard("alice", created.dashboard_id).items[0].instance_id
            == (first._tile_items[0]["instance_id"])
        )

    def test_consecutive_saves_from_one_editor_do_not_conflict(self, store):
        created = store.create_dashboard("alice", "Sales")
        (editor,) = _editors(store, created.dashboard_id, n=1)
        editor.add_component(WIDGET)
        editor.save()
        editor.add_component(WIDGET)
        editor.save()

        assert editor.dashboard.revision == 3

    def test_settings_change_from_another_session_is_a_conflict(self, store):
        created = store.create_dashboard("alice", "Sales")
        first, second = _editors(store, created.dashboard_id)
        first.settings.refresh_period = 30
        first.save()
        second.settings.refresh_period = 60

        with pytest.raises(DashboardConflictError):
            second.save()
        assert store.load_dashboard("alice", created.dashboard_id).settings["refresh_period"] == 30

    def test_export_omits_the_revision(self, store):
        created = store.create_dashboard("alice", "Sales")
        (editor,) = _editors(store, created.dashboard_id, n=1)
        assert "revision" not in editor.export_dashboard()

    def test_import_keeps_the_loaded_revision(self, store):
        created = store.create_dashboard("alice", "Sales")
        first, second = _editors(store, created.dashboard_id)
        exported = {**first.export_dashboard(), "revision": 99}
        second.save()

        first.import_dashboard(json.dumps(exported))

        assert first.dashboard.revision == 1
        with pytest.raises(DashboardConflictError):
            first.save()

    def test_store_without_revisions_stays_last_writer_wins(self):
        store = LastWriterStore()
        store.save_dashboard(DashboardModel(dashboard_id="d", user_id="alice", title="T"))
        first, second = _editors(store, "d")
        first.add_component(WIDGET)
        first.save()
        second.save()

        assert store.models["d"].items == []

    def test_save_as_creates_an_unshared_copy(self, store):
        created = store.create_dashboard("bob", "Sales")
        store.set_permission(created.dashboard_id, Permission(allow_users=frozenset({"carol"})))
        editor = FlowDash(store=store, user="alice", dashboard=created.dashboard_id)
        editor.add_component(WIDGET)

        copy = editor.save_as("Mine")

        assert copy.dashboard_id != created.dashboard_id
        assert editor.dashboard.dashboard_id == copy.dashboard_id
        assert not editor.dirty
        stored = store.load_dashboard("alice", copy.dashboard_id)
        assert stored.title == "Mine"
        assert stored.permission == Permission()
        assert len(stored.items) == 1
        assert store.load_dashboard("bob", created.dashboard_id).items == []

    def test_save_as_without_a_store(self):
        editor = FlowDash()
        editor.new_dashboard("Draft")
        model = editor.save_as("Other")
        assert model.title == "Other"
        assert model.revision is None


class TestConflictDialog:
    def _conflict(self, store):
        created = store.create_dashboard("alice", "Sales")
        first, second = _editors(store, created.dashboard_id)
        notices = []
        second._notify = lambda severity, message, duration=3000: notices.append(
            (severity, message)
        )
        first.add_component(WIDGET)
        first.save()
        second.add_component(WIDGET)
        second.add_component(WIDGET)
        assert second._on_save_clicked() is False
        return created, first, second, notices

    def test_rejected_save_offers_reload_or_copy(self, store, doc):
        _, _, _, notices = self._conflict(store)

        dialog = page_dialog()
        assert dialog.open
        assert dialog.title == "Dashboard changed since you opened it"
        assert {b.label for b in dialog.select(pmui.Button)} == {
            "Keep editing",
            "Reload",
            "Save as copy",
        }
        assert notices == [("error", str(DashboardConflictError("", 0, 0)))]

    def test_reload_discards_local_changes(self, store, doc):
        created, _, second, _ = self._conflict(store)

        _click(page_dialog(), "Reload")

        assert not page_dialog().open
        assert len(second._tile_items) == 1
        assert not second.dirty
        assert second.dashboard.revision == 2
        second.add_component(WIDGET)
        second.save()
        assert store.load_dashboard("alice", created.dashboard_id).revision == 3

    def test_save_as_copy_keeps_local_changes(self, store, doc):
        created, _, second, notices = self._conflict(store)

        _click(page_dialog(), "Save as copy")

        assert not page_dialog().open
        assert second.dashboard.title == "Sales (copy)"
        assert len(store.load_dashboard("alice", second.dashboard.dashboard_id).items) == 2
        assert len(store.load_dashboard("alice", created.dashboard_id).items) == 1
        assert notices[-1] == ("success", 'Saved your changes as "Sales (copy)".')

    def test_copy_title_is_unique(self, store, doc):
        store.create_dashboard("alice", "Sales (copy)")
        self._conflict(store)
        second_dialog = page_dialog()

        _click(second_dialog, "Save as copy")

        titles = {m.title for m in store.list_dashboards("alice")}
        assert "Sales (copy 2)" in titles

    def test_double_click_saves_one_copy(self, store, doc):
        self._conflict(store)
        dialog = page_dialog()
        button = next(b for b in dialog.select(pmui.Button) if b.label == "Save as copy")

        button.clicks += 1
        button.clicks += 1

        assert len(store.list_dashboards("alice")) == 2

    def test_keep_editing_leaves_the_canvas(self, store, doc):
        _, _, second, _ = self._conflict(store)

        _click(page_dialog(), "Keep editing")

        assert not page_dialog().open
        assert second.dirty
        assert len(second._tile_items) == 2


class TestAppConflict:
    async def test_save_as_copy_follows_the_new_dashboard(
        self,
        make_app,  # noqa: F811
        location,  # noqa: F811
        doc,
        monkeypatch,
    ):
        # The page dialog needs a session document, which has its own location.
        monkeypatch.setitem(pn.state._locations, doc, location)
        store = MemoryDashboardStore()
        app = await make_app(store)
        model = store.create_dashboard(app._user_id, "Sales")
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        other = store.load_dashboard(app._user_id, model.dashboard_id)
        store.save_dashboard(other)
        app._editor.add_component("Analytics/selector")

        assert app._save_current_dashboard() is False
        _click(page_dialog(), "Save as copy")

        copy = app._current_dashboard
        assert copy.dashboard_id != model.dashboard_id
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{copy.dashboard_id}"
        assert location.search == "?edit=true"
        labels = [item["label"] for item in app._menu_list.items[-1]["items"]]
        assert "Sales (copy)" in labels

    async def test_plain_save_keeps_the_location(self, make_app, location):  # noqa: F811
        store = MemoryDashboardStore()
        app = await make_app(store)
        model = store.create_dashboard(app._user_id, "Sales")
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        location.search = ""

        assert app._save_current_dashboard() is True

        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        assert location.search == ""


def test_conflict_error_is_exported():
    assert sys.modules["panel_flowdash"].DashboardConflictError is DashboardConflictError


def test_malformed_revision_in_a_file_counts_as_zero(tmp_path):
    data = {"dashboard_id": "d", "user_id": "u", "title": "T", "revision": "3"}
    (tmp_path / "d.json").write_text(json.dumps(data))
    store = JSONFolderDashboardStore(tmp_path)

    model = store.load_dashboard("u", "d")

    assert model.revision == 0
    store.save_dashboard(model)
    assert model.revision == 1
