"""Tests for the admin view's Components and Dashboards sections and how the app applies them."""

import getpass
import io
import json
import logging
import zipfile

import pytest

from panel_flowdash import project as project_module, register
from panel_flowdash.admin import ADMIN_ROUTE
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity, Permission
from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_settings import load_components, project_components
from panel_flowdash.dashboard_store import (
    DashboardItem,
    DashboardModel,
    DashboardStore,
    DashboardStoreError,
    MemoryDashboardStore,
)
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.project import PROJECT_FILE, ProjectError, load_project, parse_project

ME = getpass.getuser()


@register(page=False, component=True, title="Number", provides=[{"key": "value", "type": "int"}])
def number(config):
    return "number"


@register(page=False, component=True, title="Image", tags=["media"])
def image(config):
    return "image"


@register(page=False, component=True, title="Map", aliases=["Old/map"])
def map_view(config):
    return "map"


NUMBER, IMAGE, MAP = "T/number", "T/image", "T/map"
COMPONENTS = {NUMBER: number, IMAGE: image, MAP: map_view}


@pytest.fixture
def sections(monkeypatch):
    """Give each test its own copy of the section registry, with the built-in sections."""
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


def _app(tmp_path, text="", store=None, identity=None, admins=(ME,), **params):
    general = f"general:\n  admins: [{', '.join(admins)}]\n" if admins else ""
    (tmp_path / PROJECT_FILE).write_text(general + text)
    params.setdefault("auth_config", AuthConfig())
    params.setdefault("notifications", False)
    app = FlowDashApp(
        registry=normalize_components(COMPONENTS),
        project_dir=tmp_path,
        store=store if store is not None else MemoryDashboardStore(),
        include_builtin_components=False,
        **params,
    )
    if identity is not None:
        app._identity = identity
    return app


def _section(app, name):
    return app._admin_view(f"{ADMIN_ROUTE}/{name}").view(name)


def _palette_ids(editor):
    return [
        item["component_id"] for section in editor._palette_items() for item in section["items"]
    ]


def _dashboard(store, owner, title, *components):
    model = store.create_dashboard(owner, title)
    model.items = [
        DashboardItem(instance_id=f"n{i}", component_id=cid, config={})
        for i, cid in enumerate(components)
    ]
    store.save_dashboard(model)
    return model


class TestSection:
    def test_the_loader_checks_each_setting(self, sections):
        with pytest.raises(ProjectError) as info:
            parse_project(
                "components:\n  T/image: {enabled: nope, placed_by: x, colour: red}\n"
            ).get("components")
        messages = [p.message for p in info.value.problems]
        assert any("`enabled` is true or false" in m for m in messages)
        assert any("`placed_by` is a list of group names" in m for m in messages)
        assert any("Unknown setting 'colour'" in m for m in messages)

    def test_saving_drops_defaults(self, sections, tmp_path):
        app = _app(tmp_path)
        view = _section(app, "components")
        assert view.set_enabled(IMAGE, False)
        assert view.set_enabled(IMAGE, True)
        assert load_project(tmp_path / PROJECT_FILE).get("components") == {}

    def test_a_broken_file_keeps_the_settings_last_read(self, sections, tmp_path, caplog):
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n")
        project = app._settings_project()
        assert project_components(project) == {IMAGE: {"enabled": False}}
        (tmp_path / PROJECT_FILE).write_text("components: [\n")
        with caplog.at_level(logging.ERROR):
            assert project_components(project) == {IMAGE: {"enabled": False}}
        assert load_components  # the loader is importable for extensions


class TestTurningOff:
    def test_turning_off_hides_from_the_palette_and_refuses_adding(self, sections, tmp_path):
        app = _app(tmp_path)
        assert IMAGE in _palette_ids(app._editor)
        assert _section(app, "components").set_enabled(IMAGE, False)
        assert load_project(tmp_path / PROJECT_FILE).get("components") == {
            IMAGE: {"enabled": False}
        }
        assert IMAGE not in _palette_ids(app._editor)
        with pytest.raises(PermissionError, match="turned off by an admin"):
            app._editor.add_component(IMAGE)

    def test_the_agent_is_not_offered_it_and_cannot_add_it(self, sections, tmp_path):
        from panel_flowdash.agent.live import LiveDashboardOps
        from panel_flowdash.agent.ops import OperationError

        store = MemoryDashboardStore()
        mine = store.create_dashboard(ME, "Mine")
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        app._load_dashboard_sync(mine.dashboard_id, edit=True)
        ops = LiveDashboardOps(app)
        assert IMAGE not in {c["component"] for c in ops.list_components()}
        with pytest.raises(OperationError, match="turned off by an admin"):
            ops.add_node(IMAGE)
        doc = "flowdash: 1\nnodes:\n  pic:\n    component: T/image\n"
        issues = ops.validate(doc)
        assert any("turned off by an admin" in i.message for i in issues)

    def test_dashboards_using_it_keep_working_and_editors_are_told(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, ME, "Gallery", IMAGE, NUMBER)
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        told = []
        app._editor._notify = lambda severity, message, **_kw: told.append(message)
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        assert sorted(i["component_id"] for i in app._editor._tile_items) == [IMAGE, NUMBER]
        assert not any("placeholder" in i for i in app._editor._tile_items)
        assert any("Image is turned off by an admin" in m for m in told)
        # Its YAML still loads with the node in it.
        from panel_flowdash.agent.live import LiveDashboardOps

        ops = LiveDashboardOps(app)
        assert not [i for i in ops.validate() if i.severity == "error"]

    def test_people_who_cannot_edit_it_are_not_told(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, "ana", "Gallery", IMAGE)
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        told = []
        app._editor._notify = lambda severity, message, **_kw: told.append(message)
        app._load_dashboard_sync(model.dashboard_id, edit=False)
        assert not any("turned off" in m for m in told)


class TestAccess:
    def test_placed_by_limits_who_may_add_it(self, sections, tmp_path):
        text = "components:\n  T/map: {placed_by: [analysts]}\n"
        ana = Identity(user="ana", oauth_user="ana", groups=frozenset({"analysts"}))
        bo = Identity(user="bo", oauth_user="bo")
        app = _app(tmp_path, text, identity=ana)
        assert MAP in _palette_ids(app._editor)
        assert app._editor.add_component(MAP)
        app = _app(tmp_path, text, identity=bo)
        assert MAP not in _palette_ids(app._editor)
        with pytest.raises(PermissionError, match="only analysts may add it"):
            app._editor.add_component(MAP)

    def test_the_drawer_saves_access_and_approval(self, sections, tmp_path):
        app = _app(tmp_path)
        view = _section(app, "components")
        view.open_component(MAP)
        assert view._drawer.open
        view._placed_by.value = ["analysts"]
        view._approval.value = True
        assert view.done()
        assert not view._drawer.open
        assert load_project(tmp_path / PROJECT_FILE).get("components") == {
            MAP: {"placed_by": ["analysts"], "approval": True}
        }
        assert app._approval_components() == [MAP]

    def test_approval_is_part_of_the_agents_sensitive_components(
        self, sections, tmp_path, monkeypatch
    ):
        pytest.importorskip("pydantic_ai")
        from pydantic_ai.models.test import TestModel

        from panel_flowdash.agent.assistant import create_agent

        app = _app(
            tmp_path,
            "components:\n  T/map: {approval: true}\n",
            agent=create_agent(TestModel()),
        )
        policy = app._agent_session._approval_policy()
        assert policy.requires_approval("run_command", {"command": "component:T/map"})
        assert policy.requires_approval("run_command", {"command": "component:Old/map"})
        assert not policy.requires_approval("run_command", {"command": "component:T/number"})

    def test_a_pattern_set_elsewhere_locks_the_approval_switch(self, sections, tmp_path):
        app = _app(tmp_path, agent_sensitive_components=["T/*"])
        view = _section(app, "components")
        view.open_component(MAP)
        assert view._approval.value and view._approval.disabled
        assert "T/*" in view.sensitive_lock(MAP)

    def test_rows_show_source_usage_and_filters(self, sections, tmp_path):
        store = MemoryDashboardStore()
        _dashboard(store, ME, "One", NUMBER)
        _dashboard(store, "ana", "Two", NUMBER, "Old/map")
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        view = _section(app, "components")
        rows = {r["id"]: r for r in view.rows()}
        assert len(rows[NUMBER]["used_in"]) == 2
        assert len(rows[MAP]["used_in"]) == 1
        assert not rows[IMAGE]["enabled"]
        view.filter = "off"
        assert [r["id"] for r in view.visible_rows()] == [IMAGE]
        view.filter = "all"
        view.query = "media"
        assert [r["id"] for r in view.visible_rows()] == [IMAGE]


class TestNonAdmins:
    def test_a_non_admin_gets_no_admin_view(self, sections, tmp_path):
        app = _app(tmp_path, admins=("someone-else",))
        assert not app._is_admin
        assert app._admin_view(f"{ADMIN_ROUTE}/dashboards") is not app._admin_page

    def test_actions_refuse_once_the_admin_was_removed(self, sections, tmp_path):
        store = MemoryDashboardStore()
        theirs = _dashboard(store, "ana", "Theirs")
        app = _app(tmp_path, store=store)
        components = _section(app, "components")
        dashboards = _section(app, "dashboards")
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [someone-else]\n")
        assert not app._still_admin()
        assert not components.set_enabled(IMAGE, False)
        assert not components.set_access(MAP, ["analysts"])
        assert not dashboards.delete([theirs.dashboard_id])
        assert not dashboards.prune([theirs.dashboard_id], 1)
        assert not dashboards.change_owner([theirs.dashboard_id], ME)
        assert not dashboards.share([theirs.dashboard_id], "owner")
        assert not dashboards.download([theirs.dashboard_id])
        assert dashboards.duplicate(theirs.dashboard_id) is None
        assert store.get_owner(theirs.dashboard_id) == "ana"
        assert store._load_any(theirs.dashboard_id).permission == Permission()
        assert "Only admins" in dashboards._alert.object
        assert "components" not in load_project(tmp_path / PROJECT_FILE)


def _history(store, dashboard_id, versions, by=ME):
    for i in range(versions):
        store.append_history(
            dashboard_id,
            {
                "type": "ARTIFACT_PUBLISHED",
                "artifact_id": f"dashboard:{dashboard_id}",
                "value": "x" * 100,
                "metadata": {"version_id": f"v{i}"},
                "actor": "user",
                "user": by,
                "timestamp": 1000.0 + i,
            },
        )


class TestDashboards:
    def test_rows_show_owner_history_access_and_health(self, sections, tmp_path):
        store = MemoryDashboardStore()
        mine = _dashboard(store, ME, "Mine", NUMBER, "Gone/component")
        theirs = _dashboard(store, "ana", "Theirs", IMAGE)
        store.set_permission(theirs.dashboard_id, Permission(allow_groups=frozenset({"ops"})))
        _history(store, mine.dashboard_id, 3, by="ana")
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        view = _section(app, "dashboards")
        rows = {r.id: r for r in view._rows}
        row = rows[mine.dashboard_id]
        assert (row.versions, row.edited_by, row.access) == (3, "ana", ("Everyone",))
        assert row.history_bytes > 300
        assert row.tiles == 2 and len(row.issues) == 1
        assert rows[theirs.dashboard_id].access == ("ops",)
        assert "turned off" in rows[theirs.dashboard_id].issues[0]
        counts = view.counts()
        assert counts == {"all": 2, "mine": 1, "private": 0, "issues": 2, "stale": 0}

    def test_stale_counts_dashboards_not_opened_in_90_days(self, sections, tmp_path):
        import datetime as dt

        store = MemoryDashboardStore()
        old = _dashboard(store, ME, "Old")
        fresh = _dashboard(store, ME, "Fresh")
        app = _app(tmp_path, store=store)
        ledger = app._usage_ledger()
        ledger.opened(old.dashboard_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=120))
        ledger.opened(fresh.dashboard_id)
        view = _section(app, "dashboards")
        view.refresh()
        view.filter = "stale"
        assert [r.id for r in view.visible()] == [old.dashboard_id]

    def test_delete_asks_first_and_is_recorded(self, sections, tmp_path):
        store = MemoryDashboardStore()
        a = _dashboard(store, "ana", "A")
        b = _dashboard(store, "bo", "B")
        keep = _dashboard(store, ME, "Keep")
        _history(store, a.dashboard_id, 2)
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        view.select(a.dashboard_id)
        view.select(b.dashboard_id)
        view.ask("delete")
        assert view._dialog.open and "Delete 2 dashboards" in view._confirm.label
        view.cancel()
        assert len(store._all_dashboards()) == 3
        view.ask("delete")
        assert view.confirm()
        assert [m.dashboard_id for m in store._all_dashboards()] == [keep.dashboard_id]
        assert store.load_history(a.dashboard_id) == []
        (entry,) = app._usage_ledger().admin_log()
        assert entry["user"] == ME and entry["action"] == "deleted"
        assert {d["title"] for d in entry["dashboards"]} == {"A", "B"}
        assert entry["when"]
        assert not view.selection

    def test_deleting_the_home_page_clears_it(self, sections, tmp_path):
        store = MemoryDashboardStore()
        home = _dashboard(store, ME, "Home")
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        assert view.make_home(home.dashboard_id)
        assert load_project(tmp_path / PROJECT_FILE).get("general")["home_dashboard"] == (
            home.dashboard_id
        )
        assert view.delete([home.dashboard_id])
        assert "home_dashboard" not in load_project(tmp_path / PROJECT_FILE).get("general")

    def test_prune_keeps_the_latest_versions(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, "ana", "A")
        _history(store, model.dashboard_id, 10)
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        view.ask("prune", [model.dashboard_id])
        view._keep_input.value = 3
        assert view.confirm()
        records = store.load_history(model.dashboard_id)
        assert [r["metadata"]["version_id"] for r in records] == ["v7", "v8", "v9"]
        assert app._usage_ledger().admin_log()[-1]["dropped"] == 7

    def test_change_owner_moves_ownership(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, "ana", "A")
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        view.ask("owner", [model.dashboard_id])
        view._owner_input.value = "bo"
        assert view.confirm()
        assert store.get_owner(model.dashboard_id) == "bo"
        assert app._usage_ledger().admin_log()[-1]["owner"] == "bo"
        # A later save from a session that loaded it before keeps the new owner.
        stale = model
        stale.revision = store._load_any(model.dashboard_id).revision
        store.save_dashboard(stale)
        assert store.get_owner(model.dashboard_id) == "bo"

    def test_share_sets_who_can_view(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, "ana", "A")
        store.set_permission(model.dashboard_id, Permission(allow_edit_users=frozenset({"cy"})))
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        assert view.share([model.dashboard_id], "names", ["ops", "dee"])
        permission = store._load_any(model.dashboard_id).permission
        assert permission.allow_edit_users == {"cy"}
        assert permission.allow_users | permission.allow_groups == {"ops", "dee"}
        assert view.share([model.dashboard_id], "owner")
        assert store._load_any(model.dashboard_id).permission.allow_users == {"ana"}
        assert view.share([model.dashboard_id], "everyone")
        assert view._rows[0].access == ("Everyone",)

    def test_download_writes_yaml_and_zips_several(self, sections, tmp_path):
        store = MemoryDashboardStore()
        a = _dashboard(store, "ana", "A", NUMBER)
        b = _dashboard(store, "bo", "B")
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        sent = []
        app._downloader.transfer = lambda **_kw: sent.append(
            (app._downloader.filename, app._downloader.callback())
        )
        assert view.download([a.dashboard_id])
        assert sent[-1][0].endswith(".yaml") and "T/number" in sent[-1][1].getvalue()
        assert view.download([a.dashboard_id, b.dashboard_id])
        name, payload = sent[-1]
        assert name == "dashboards.zip"
        assert len(zipfile.ZipFile(io.BytesIO(payload.getvalue())).namelist()) == 2

    def test_duplicate_and_open_from_the_row_menu(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, "ana", "A", NUMBER)
        app = _app(tmp_path, store=store)
        view = _section(app, "dashboards")
        copy_id = view.duplicate(model.dashboard_id)
        copy = store._load_any(copy_id)
        assert copy.title == "A copy" and copy.owner == app._user_id
        assert [i.component_id for i in copy.items] == [NUMBER]
        items = view._menu_items(view._rows[0])
        assert [i["label"] for i in items] == [
            "Open",
            "Duplicate",
            "Dashboard settings",
            "Make home page",
        ]

    def test_make_home_page_is_locked_when_set_in_code(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, ME, "A")
        app = _app(
            tmp_path, store=store, home_dashboard="x", settings_sources={"home_dashboard": "-x"}
        )
        view = _section(app, "dashboards")
        assert view._menu_items(view._rows[0])[-1]["disabled"]
        assert not view.make_home(model.dashboard_id)


class TestStores:
    @pytest.mark.parametrize("kind", ["memory", "sqlite", "json"])
    def test_set_owner(self, kind, tmp_path):
        if kind == "memory":
            store = MemoryDashboardStore()
        elif kind == "sqlite":
            store = DashboardStore(tmp_path / "d.db")
        else:
            store = JSONFolderDashboardStore(tmp_path / "dashboards")
        model = store.create_dashboard("ana", "A")
        revision = store._load_any(model.dashboard_id).revision
        assert store.set_owner(model.dashboard_id, "bo")
        stored = store._load_any(model.dashboard_id)
        assert stored.owner == "bo" and stored.revision == revision
        assert not store.set_owner("missing", "bo")

    def test_a_store_without_it_refuses(self):
        from panel_flowdash.dashboard_store import BaseDashboardStore

        class Minimal(MemoryDashboardStore):
            set_owner = BaseDashboardStore.set_owner

        with pytest.raises(DashboardStoreError):
            Minimal().set_owner("x", "bo")


def test_dashboard_items_render(sections, tmp_path):
    model = DashboardModel(dashboard_id="d", user_id=ME, title="T")
    store = MemoryDashboardStore({"d": model})
    app = _app(tmp_path, store=store)
    view = _section(app, "dashboards")
    assert view._table._rows.objects


def _file(*components, owner="ana"):
    model = DashboardModel(
        dashboard_id="f",
        user_id=owner,
        title="From a file",
        items=[
            DashboardItem(instance_id=f"n{i}", component_id=cid, config={})
            for i, cid in enumerate(components)
        ],
    )
    return json.dumps(model.to_dict())


class TestImports:
    @pytest.mark.parametrize(
        "text",
        [
            "components:\n  T/image: {enabled: false}\n",
            "components:\n  T/image: {placed_by: [x]}\n",
        ],
    )
    def test_importing_a_file_with_a_refused_component_is_refused(self, sections, tmp_path, text):
        store = MemoryDashboardStore()
        app = _app(tmp_path, text, store=store)
        told = []
        app._notify = lambda severity, message, **_kw: told.append(message)
        assert app._import_dashboard_file("x.json", _file(NUMBER, IMAGE)) is None
        assert store._all_dashboards() == []
        assert "n1 (Image:" in told[-1] and "Nothing was imported" in told[-1]
        assert app._import_dashboard_file("y.json", _file(NUMBER)) is not None

    def test_dropping_a_file_on_the_canvas_is_refused_for_new_nodes_only(self, sections, tmp_path):
        store = MemoryDashboardStore()
        model = _dashboard(store, ME, "Mine", IMAGE)
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n", store=store)
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        editor = app._editor
        told = []
        editor._notify = lambda severity, message, **_kw: told.append(message)
        editor._on_files_dropped([{"name": "x.json", "content": _file(IMAGE, IMAGE)}])
        assert "n1 (Image: turned off by an admin)" in told[-1]
        assert not editor._import_dialog.open
        # The node the dashboard already has may come back from a file.
        kept = editor._imported_model(_file(IMAGE, NUMBER))
        assert [i.component_id for i in kept.items] == [IMAGE, NUMBER]


class TestFailClosed:
    def test_unreadable_settings_never_read_refuse_every_component(self, sections, tmp_path):
        from panel_flowdash import component_settings

        app = _app(tmp_path)
        component_settings._LAST.clear()
        (tmp_path / PROJECT_FILE).write_text("components: [\n")
        assert "cannot be read" in app._placement_refusal(NUMBER)
        assert _palette_ids(app._editor) == []
        assert app._approval_components() == ["*"]

    def test_unreadable_settings_keep_the_last_read(self, sections, tmp_path):
        app = _app(tmp_path, "components:\n  T/image: {enabled: false}\n")
        assert app._placement_refusal(IMAGE) == "turned off by an admin"
        (tmp_path / PROJECT_FILE).write_text("components: [\n")
        assert app._placement_refusal(IMAGE) == "turned off by an admin"
        assert app._placement_refusal(NUMBER) is None


def test_an_admin_by_the_link_is_checked_like_any_once_recorded(sections, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from panel_flowdash.admin import access

    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    app = _app(tmp_path, admins=())
    assert not app._is_admin
    app._on_admin_token(SimpleNamespace(new=access.admin_token()))
    assert app._is_admin and app._still_admin()
    (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [someone-else]\n")
    assert not app._still_admin()
    access.reset_admin_token()


def test_opens_are_written_in_batches(tmp_path, monkeypatch):
    from panel_flowdash import ai

    ledger = ai.UsageLedger(tmp_path / "usage.json")
    writes = []
    write = ledger._write
    monkeypatch.setattr(ledger, "_write", lambda data: (writes.append(1), write(data)))
    for _ in range(50):
        ledger.opened("a")
    ledger.opened("b")
    assert writes == []
    assert set(ledger.dashboards()) == {"a", "b"}
    ledger.flush_opens()
    assert len(writes) == 1
    assert set(json.loads((tmp_path / "usage.json").read_text())["dashboards"]) == {"a", "b"}
    monkeypatch.setattr(ai, "OPENS_FLUSH_SECONDS", 0)
    ledger.opened("c")
    assert len(writes) == 2
