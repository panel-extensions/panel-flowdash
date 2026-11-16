"""Tests for the editor role in dashboard sharing and who may inspect outputs."""

import json
import sqlite3

import panel as pn
import param
import pytest

from panel_flowdash import register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import Identity, Permission, can_edit, is_authorized
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardItem, DashboardStore, MemoryDashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.ui.settings_dialog import ShareEntry


@register(page=False, component=True, title="Source", provides=[{"key": "value", "type": "int"}])
def source(config):
    return "source"


@register(page=False, component=True, title="Bounded", min_size={"width": 240})
def bounded(config):
    return "bounded"


SOURCE = "Role/source"
BOUNDED = "Role/bounded"
COMPONENTS = {SOURCE: source, BOUNDED: bounded}

ALICE = Identity(user="alice", oauth_user="alice")
BOB = Identity(user="bob", oauth_user="bob")
CAROL = Identity(user="carol", oauth_user="carol", groups=frozenset({"analysts"}))
DAVE = Identity(user="dave", oauth_user="dave")
ADMIN = Identity(user="root", oauth_user="root", groups=frozenset({"admins"}))

EDITORS = Permission.from_spec(allow_edit_users=["bob"], allow_edit_groups=["analysts"])


class FakeLocation(param.Parameterized):
    pathname = param.String(default="/")
    search = param.String(default="")


@pytest.fixture(params=["sqlite", "memory", "json"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return DashboardStore(tmp_path / "test.db")
    if request.param == "json":
        return JSONFolderDashboardStore(tmp_path / "dashboards")
    return MemoryDashboardStore()


class TestPermission:
    def test_edit_rules_round_trip(self):
        data = EDITORS.to_dict()
        assert data["allow_edit_users"] == ["bob"]
        assert data["allow_edit_groups"] == ["analysts"]
        assert Permission.from_dict(data) == EDITORS
        assert not EDITORS.is_empty

    def test_stored_permission_without_edit_rules_still_loads(self):
        perm = Permission.from_dict({"allow_users": ["bob"]})
        assert perm.allow_edit_users == frozenset()
        assert perm.allow_edit_groups == frozenset()

    def test_editor_can_view_despite_restrictive_view_rules(self):
        perm = Permission.from_spec(allow_users=["dave"], allow_edit_users=["bob"])
        assert is_authorized(perm, BOB)
        assert is_authorized(perm, DAVE)
        assert not is_authorized(perm, CAROL)

    def test_edit_rules_do_not_restrict_viewing(self):
        assert is_authorized(EDITORS, DAVE, default_allow=True)
        assert not is_authorized(EDITORS, DAVE, default_allow=False)
        assert is_authorized(EDITORS, CAROL, default_allow=False)

    def test_deny_wins_over_edit(self):
        perm = Permission.from_spec(allow_edit_users=["bob"], deny_users=["bob"])
        assert not is_authorized(perm, BOB)
        assert not can_edit(perm, BOB, "alice", ())

    def test_can_edit(self):
        assert can_edit(EDITORS, ALICE, "alice", ())
        assert can_edit(EDITORS, BOB, "alice", ())
        assert can_edit(EDITORS, CAROL, "alice", ())
        assert can_edit(Permission(), ADMIN, "alice", ["admins"])
        assert not can_edit(EDITORS, DAVE, "alice", ())
        # A view grant is not an edit grant.
        assert not can_edit(Permission.from_spec(allow_users=["dave"]), DAVE, "alice", ())


class TestStores:
    def test_edit_rules_persist(self, store):
        dashboard = store.create_dashboard("alice", "Shared")
        assert store.set_permission(dashboard.dashboard_id, EDITORS)
        assert store._load_any(dashboard.dashboard_id).permission == EDITORS
        (listed,) = store.list_accessible(BOB)
        assert listed.permission == EDITORS

    def test_save_keeps_edit_rules(self, store):
        dashboard = store.create_dashboard("alice", "Shared")
        dashboard.permission = EDITORS
        store.save_dashboard(dashboard)
        assert store._load_any(dashboard.dashboard_id).permission == EDITORS

    def test_can_edit_versus_can_administer(self, store):
        dashboard = store.create_dashboard("alice", "Shared")
        store.set_permission(dashboard.dashboard_id, EDITORS)
        did = dashboard.dashboard_id
        admins = frozenset({"admins"})
        for identity, edit, administer in [
            (ALICE, True, True),
            (ADMIN, True, True),
            (BOB, True, False),
            (CAROL, True, False),
            (DAVE, False, False),
        ]:
            assert store.can_edit(identity, did, admins) is edit, identity.user
            assert store.can_administer(identity, did, admins) is administer, identity.user
            model = store._load_any(did)
            assert store.can_edit_model(identity, model, admins) is edit, identity.user
        assert not store.can_edit(BOB, "missing")

    def test_read_only_json_store_lets_nobody_edit(self, tmp_path):
        writable = JSONFolderDashboardStore(tmp_path)
        dashboard = writable.create_dashboard("alice", "Shared")
        writable.set_permission(dashboard.dashboard_id, EDITORS)
        store = JSONFolderDashboardStore(tmp_path, read_only=True)
        assert not store.can_edit(BOB, dashboard.dashboard_id)
        assert not store.can_edit(ALICE, dashboard.dashboard_id)

    def test_sqlite_row_from_before_edit_rules(self, tmp_path):
        store = DashboardStore(tmp_path / "test.db")
        dashboard = store.create_dashboard("alice", "Old")
        conn = sqlite3.connect(tmp_path / "test.db")
        with conn:
            conn.execute(
                "UPDATE dashboards SET permission_json = ?",
                (json.dumps({"allow_users": ["dave"], "allow_groups": []}),),
            )
        conn.close()
        model = store._load_any(dashboard.dashboard_id)
        store.close()
        assert model.permission == Permission.from_spec(allow_users=["dave"])


@pytest.fixture
def location():
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        yield pn.state._location
    finally:
        pn.state._location = previous


def _app(store, identity, **params):
    app = FlowDashApp(
        registry=normalize_components(COMPONENTS),
        store=store,
        notifications=False,
        **params,
    )
    app._identity = identity
    app._user_id = identity.user
    app._editor.user = identity.user
    return app


@pytest.fixture
def shared():
    """A dashboard owned by alice, editable by bob and the analysts."""
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard("alice", "Shared")
    dashboard.items = [DashboardItem(instance_id="s1", component_id=SOURCE)]
    dashboard.permission = EDITORS
    store.save_dashboard(dashboard)
    return store, dashboard.dashboard_id


class TestApp:
    async def test_editor_can_open_edit_mode_and_save(self, shared, location):
        store, did = shared
        app = _app(store, BOB)
        await app._load_dashboard(did, edit=True)
        assert app._editor.editable
        assert not app._editor.read_only
        assert not app._share_button.visible
        assert location.search == "?edit=true"

        app._editor.settings.refresh_period = 5
        assert app._save_current_dashboard()
        saved = store._load_any(did)
        assert saved.settings["refresh_period"] == 5
        assert saved.user_id == "alice"
        assert saved.permission == EDITORS

    async def test_viewer_is_downgraded_and_refused(self, shared, location):
        store, did = shared
        store.set_permission(did, Permission.from_spec(allow_users=["dave"]))
        app = _app(store, DAVE)
        await app._load_dashboard(did, edit=True)
        assert not app._editor.editable
        assert app._editor.read_only
        assert not app._save_current_dashboard()

    async def test_editor_may_not_rename_delete_or_share(self, shared, location):
        store, did = shared
        app = _app(store, CAROL)
        assert not app._rename_dashboard(did, "Mine now")
        assert not app._delete_dashboard(did)
        app._open_share_dialog(did, "Shared")
        assert not app._share_dialog.open
        assert app._sharing_access(did) is None
        assert not app._save_sharing(did, Permission())
        assert app._rename_from_settings(did, "Mine now")
        assert store._load_any(did).title == "Shared"
        assert store._load_any(did).permission == EDITORS

    async def test_menu_actions_per_role(self, shared, location):
        store, did = shared

        def actions(identity):
            app = _app(store, identity)
            item = next(i for i in app._get_dashboard_menu_items() if i["label"] == "Shared")
            return ["---" if a is None else a["label"] for a in item.get("actions", [])]

        assert actions(ALICE) == [
            "Edit",
            "Rename",
            "Duplicate",
            "Share",
            "Download as file",
            "Delete",
        ]
        assert actions(BOB) == ["Edit", "Duplicate", "Download as file"]
        store.set_permission(did, Permission())
        assert actions(DAVE) == ["Duplicate", "Download as file"]

    async def test_editor_actions_include_viewer_actions(self, shared, location):
        class CopyApp(FlowDashApp):
            _VIEWER_DASHBOARD_ACTIONS = ({"label": "Copy", "icon": "content_copy"},)

        app = CopyApp(registry=normalize_components(COMPONENTS), store=shared[0])
        assert [a["label"] for a in app._dashboard_actions(False, True)] == ["Edit", "Copy"]
        assert [a["label"] for a in app._dashboard_actions(False)] == ["Copy"]

    async def test_share_dialog_edits_editor_rules(self, shared, location):
        store, did = shared
        app = _app(store, ALICE)
        app._open_share_dialog(did, "Shared")
        view = app._editor._settings_view
        assert app._share_dialog.open
        assert view.section == "sharing"
        assert view._entries == [
            ShareEntry("analysts", "group", "edit"),
            ShareEntry("bob", "user", "edit"),
        ]

        view._add_input.value = "erin"
        view.set_role("erin", "user", "edit")
        view.set_role("analysts", "group", "remove")
        view._save_button.clicks += 1
        assert store._load_any(did).permission == Permission.from_spec(
            allow_edit_users=["bob", "erin"]
        )


class TestInspectPermission:
    def _button(self, editor):
        editor.editable = False
        return editor._inspect_tiles["s1"].inspect_button

    def _editor(self, **params):
        editor = FlowDash(COMPONENTS, notifications=False, inspect_outputs=True, **params)
        store = MemoryDashboardStore()
        dashboard = store.create_dashboard("alice", "D")
        dashboard.items = [DashboardItem(instance_id="s1", component_id=SOURCE)]
        editor.load_model(dashboard)
        return editor

    async def test_read_only_editor_hides_inspection_unless_allowed(self):
        editor = self._editor(read_only=True)
        button = self._button(editor)
        assert not button.visible
        button.clicks += 1
        assert not editor._inspect_dialog.open

        editor.settings.viewers_can_inspect = True
        assert button.visible
        button.clicks += 1
        assert editor._inspect_dialog.open

    async def test_revoking_closes_an_open_dialog(self):
        editor = self._editor()
        button = self._button(editor)
        assert button.visible
        button.clicks += 1
        assert editor._inspect_dialog.open

        editor.read_only = True
        assert not button.visible
        assert not editor._inspect_dialog.open

    async def test_granting_skips_bounded_tiles_without_outputs(self):
        editor = FlowDash(COMPONENTS, notifications=False, inspect_outputs=True, read_only=True)
        dashboard = MemoryDashboardStore().create_dashboard("alice", "D")
        dashboard.items = [DashboardItem(instance_id="b1", component_id=BOUNDED)]
        editor.load_model(dashboard)
        editor.editable = False
        button = editor._inspect_tiles["b1"].inspect_button
        editor.settings.viewers_can_inspect = True
        assert not button.visible

    async def test_setting_is_only_offered_with_inspection_on(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        assert not editor._settings_view._inspect_switch.visible
        editor.inspect_outputs = True
        assert editor._settings_view._inspect_switch.visible

    async def test_setting_round_trips(self):
        editor = self._editor()
        editor.settings.viewers_can_inspect = True
        assert editor.to_model().settings["viewers_can_inspect"] is True

    async def test_app_shows_inspection_to_editors_not_viewers(self, shared, location):
        store, did = shared
        options = {"editor_options": {"inspect_outputs": True}}

        viewer = _app(store, DAVE, **options)
        await viewer._load_dashboard(did)
        assert viewer._editor.read_only
        assert not viewer._editor._inspect_tiles["s1"].inspect_button.visible

        editor = _app(store, BOB, **options)
        await editor._load_dashboard(did)
        assert not editor._editor.read_only
        assert editor._editor._inspect_tiles["s1"].inspect_button.visible

        dashboard = store._load_any(did)
        dashboard.settings = {"viewers_can_inspect": True}
        store.save_dashboard(dashboard)
        viewer = _app(store, DAVE, **options)
        await viewer._load_dashboard(did)
        assert viewer._editor._inspect_tiles["s1"].inspect_button.visible
