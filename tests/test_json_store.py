"""Tests for the behavior specific to the JSON folder store.

The shared store contract is exercised for this backend in test_dashboard_store.py.
"""

import json
import logging
import os

import pytest

from panel_flowdash.auth import Identity
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.json_store import JSONFolderDashboardStore


def _identity(user):
    return Identity(user=user, oauth_user=user, groups=frozenset())


def _write(folder, model):
    (folder / f"{model.dashboard_id}.json").write_text(json.dumps(model.to_dict()))


class TestFiles:
    def test_creates_missing_folder(self, tmp_path):
        folder = tmp_path / "a" / "b"
        JSONFolderDashboardStore(folder)
        assert folder.is_dir()

    def test_saves_one_readable_file_per_dashboard(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        model = DashboardModel(
            dashboard_id="d1",
            user_id="u",
            title="Café",
            items=[DashboardItem(instance_id="n1", component_id="A/b")],
        )
        store.save_dashboard(model)

        text = (tmp_path / "d1.json").read_text(encoding="utf-8")
        assert text.endswith("}\n")
        assert "Café" in text
        assert DashboardModel.from_dict(json.loads(text)) == model
        assert [p.name for p in tmp_path.iterdir()] == ["d1.json"]

    def test_picks_up_files_added_outside_the_store(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        assert store.list_dashboards("u") == []
        _write(tmp_path, DashboardModel(dashboard_id="ext", user_id="u", title="External"))
        assert [m.title for m in store.list_dashboards("u")] == ["External"]
        assert store.load_dashboard("u", "ext").title == "External"

    def test_lists_most_recently_written_first(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        for name in ("old", "new", "mid"):
            store.save_dashboard(DashboardModel(dashboard_id=name, user_id="u", title=name))
        for age, name in enumerate(("old", "mid", "new")):
            os.utime(tmp_path / f"{name}.json", ns=(1_000_000_000 * age,) * 2)
        assert [m.dashboard_id for m in store.list_dashboards("u")] == ["new", "mid", "old"]

    def test_failed_write_leaves_the_original_and_no_temp_file(self, tmp_path, monkeypatch):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(DashboardModel(dashboard_id="d1", user_id="u", title="Before"))

        def refuse(tmp, target):
            raise OSError("disk full")

        monkeypatch.setattr("panel_flowdash.json_store.os.replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            store.rename_dashboard("u", "d1", "After")

        assert store.load_dashboard("u", "d1").title == "Before"
        assert [p.name for p in tmp_path.iterdir()] == ["d1.json"]

    def test_retries_a_transient_replace_refusal(self, tmp_path, monkeypatch):
        store = JSONFolderDashboardStore(tmp_path)
        real_replace = os.replace
        calls = []

        def flaky(tmp, target):
            calls.append(tmp)
            if len(calls) < 3:
                raise PermissionError("in use")
            real_replace(tmp, target)

        monkeypatch.setattr("panel_flowdash.json_store.os.replace", flaky)
        monkeypatch.setattr("panel_flowdash.json_store._REPLACE_PAUSE", 0)
        store.save_dashboard(DashboardModel(dashboard_id="d1", user_id="u", title="T"))
        assert len(calls) == 3
        assert store.load_dashboard("u", "d1").title == "T"


class TestIds:
    def test_create_derives_the_id_from_the_title(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        assert store.create_dashboard("u", "Sales Overview").dashboard_id == "sales-overview"
        assert store.create_dashboard("v", "Sales  Overview!").dashboard_id == "sales-overview-2"
        assert store.create_dashboard("u", "???").dashboard_id == "dashboard"

    def test_create_does_not_reuse_the_id_of_a_deleted_file_in_use(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        first = store.create_dashboard("u", "Sales")
        store.delete_dashboard("u", first.dashboard_id)
        assert store.create_dashboard("u", "Sales").dashboard_id == "sales"

    @pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "a\\b", "", "with space"])
    def test_unsafe_ids_cannot_be_saved(self, tmp_path, bad):
        store = JSONFolderDashboardStore(tmp_path / "root")
        with pytest.raises(ValueError, match="file name"):
            store.save_dashboard(DashboardModel(dashboard_id=bad, user_id="u", title="T"))
        assert not any(tmp_path.rglob("*.json"))

    @pytest.mark.parametrize("bad", ["../secret", "a/b", ".hidden", ""])
    def test_unsafe_ids_cannot_be_loaded(self, tmp_path, bad):
        (tmp_path / "secret.json").write_text(
            json.dumps(DashboardModel(dashboard_id="secret", user_id="u", title="S").to_dict())
        )
        store = JSONFolderDashboardStore(tmp_path / "root")
        assert store.find_by_id_or_title(bad) is None
        assert store.load_dashboard("u", bad) is None

    def test_find_by_title_falls_back_when_the_title_is_not_a_valid_id(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        store.create_dashboard("u", "Sales Overview")
        assert store.find_by_id_or_title("Sales Overview").dashboard_id == "sales-overview"


class TestBadFiles:
    def test_malformed_file_is_skipped_with_one_warning(self, tmp_path, caplog):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(DashboardModel(dashboard_id="ok", user_id="u", title="OK"))
        (tmp_path / "broken.json").write_text("{not json")
        (tmp_path / "partial.json").write_text('{"dashboard_id": "partial"}')

        with caplog.at_level(logging.WARNING, logger="panel_flowdash.json_store"):
            for _ in range(3):
                assert [m.dashboard_id for m in store.list_dashboards("u")] == ["ok"]
        assert sorted(r.args[0].name for r in caplog.records) == ["broken.json", "partial.json"]

    def test_a_file_warns_again_after_it_changes(self, tmp_path, caplog):
        store = JSONFolderDashboardStore(tmp_path)
        path = tmp_path / "broken.json"
        path.write_text("{")
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.json_store"):
            store.list_dashboards("u")
            path.write_text("[")
            os.utime(path, ns=(2_000_000_000, 2_000_000_000))
            store.list_dashboards("u")
        assert len(caplog.records) == 2

    def test_file_named_for_another_id_is_skipped(self, tmp_path, caplog):
        _write(tmp_path, DashboardModel(dashboard_id="real", user_id="u", title="T"))
        (tmp_path / "real.json").rename(tmp_path / "alias.json")
        store = JSONFolderDashboardStore(tmp_path)

        with caplog.at_level(logging.WARNING, logger="panel_flowdash.json_store"):
            assert store.list_dashboards("u") == []
            assert store.load_dashboard("u", "alias") is None
        assert "named for 'alias'" in caplog.records[0].getMessage()

    def test_non_json_and_temp_files_are_ignored(self, tmp_path, caplog):
        store = JSONFolderDashboardStore(tmp_path)
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / ".d1.json.abc.tmp").write_text("{")
        (tmp_path / "folder.json").mkdir()
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.json_store"):
            assert store.list_dashboards("u") == []
        assert not caplog.records

    def test_unreachable_folder_logs_once_and_recovers(self, tmp_path, caplog):
        folder = tmp_path / "share"
        store = JSONFolderDashboardStore(folder)
        _write(folder, DashboardModel(dashboard_id="d1", user_id="u", title="T"))
        folder.rename(tmp_path / "gone")

        with caplog.at_level(logging.INFO, logger="panel_flowdash.json_store"):
            assert store.list_dashboards("u") == []
            assert store.list_dashboards("u") == []
            (tmp_path / "gone").rename(folder)
            assert [m.dashboard_id for m in store.list_dashboards("u")] == ["d1"]
        assert [r.levelname for r in caplog.records] == ["ERROR", "INFO"]


class TestReadOnly:
    @pytest.fixture
    def store(self, tmp_path):
        _write(tmp_path, DashboardModel(dashboard_id="d1", user_id="u", title="Shipped"))
        return JSONFolderDashboardStore(tmp_path, read_only=True)

    def test_reads_work(self, store):
        assert store.load_dashboard("u", "d1").title == "Shipped"
        assert [m.dashboard_id for m in store.list_accessible(_identity("u"))] == ["d1"]

    def test_missing_folder_is_not_created(self, tmp_path):
        JSONFolderDashboardStore(tmp_path / "absent", read_only=True)
        assert not (tmp_path / "absent").exists()

    def test_writes_are_refused(self, store):
        model = store.load_dashboard("u", "d1")
        with pytest.raises(PermissionError):
            store.save_dashboard(model)
        with pytest.raises(PermissionError):
            store.create_dashboard("u", "New")
        with pytest.raises(PermissionError):
            store.rename_dashboard("u", "d1", "Renamed")
        with pytest.raises(PermissionError):
            store.delete_dashboard("u", "d1")
        with pytest.raises(PermissionError):
            store.set_permission("d1", model.permission)
        assert store.load_dashboard("u", "d1").title == "Shipped"

    def test_nobody_administers_not_even_the_owner(self, store):
        assert not store.can_administer(_identity("u"), "d1")


class TestAdministration:
    def test_follows_the_base_admin_group_rules(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(DashboardModel(dashboard_id="d1", user_id="owner", title="T"))
        admins = frozenset({"admins"})
        boss = Identity(user="boss", oauth_user="boss", groups=admins)

        assert store.can_administer(_identity("other"), "d1")
        assert store.can_administer(_identity("owner"), "d1", admins)
        assert store.can_administer(boss, "d1", admins)
        assert not store.can_administer(_identity("other"), "d1", admins)
        assert not store.can_administer(_identity("owner"), "missing")
