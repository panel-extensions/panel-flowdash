"""Tests that sharing rules are enforced on every save, copy and access-denied path."""

import itertools

import panel_material_ui as pmui
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash.auth import AuthConfig, Identity, Permission, is_authorized
from panel_flowdash.dashboard_store import DashboardItem, MemoryDashboardStore
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.ui.settings_dialog import ShareEntry, permission_from_entries
from panel_flowdash.util import page_dialog
from tests.test_editor_role import ALICE, BOB, SOURCE, _app, location  # noqa: F401

REVOKED = "You no longer have edit access to this dashboard, so it was not saved."


def _capture(app):
    app.notices = []
    notify = lambda severity, message, duration=3000: app.notices.append((severity, message))
    app._notify = app._editor._notify = notify
    return app.notices


@pytest.fixture
def editing(location):  # noqa: F811
    """Bob editing alice's dashboard, with his edit rights then revoked from another session."""
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    model.items = [DashboardItem(instance_id="s1", component_id=SOURCE)]
    store.save_dashboard(model)
    store.set_permission(model.dashboard_id, Permission.from_spec(allow_edit_users=["bob"]))
    app = _app(store, BOB)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    assert not app._editor.read_only
    notices = _capture(app)
    return app, store, model.dashboard_id, notices


def _revoke(store, did, **spec):
    store.set_permission(did, Permission.from_spec(**spec))


def _assert_refused(app, store, did, notices, revision):
    assert store._load_any(did).revision == revision
    assert store._load_any(did).settings.get("description") in (None, "")
    assert ("error", REVOKED) in notices
    assert app._editor.read_only


class TestRevokedEditRights:
    @pytest.mark.parametrize("spec", [{"deny_users": ["bob"]}, {"allow_users": ["bob"]}])
    def test_save_button(self, editing, spec):
        app, store, did, notices = editing
        revision = store._load_any(did).revision
        _revoke(store, did, **spec)
        app._editor.settings.description = "written by bob"
        app._editor.dirty = True

        app._editor._save_button.clicks += 1

        _assert_refused(app, store, did, notices, revision)
        assert app._editor.dirty

    def test_save_and_view(self, editing):
        app, store, did, notices = editing
        revision = store._load_any(did).revision
        _revoke(store, did, deny_users=["bob"])
        app._editor.settings.description = "written by bob"
        app._editor.dirty = True

        with set_curdoc(Document()):
            app._editor._dashboard_bar._confirm_leave()
            save = next(b for b in page_dialog().select(pmui.Button) if b.label == "Save")
            save.clicks += 1

        _assert_refused(app, store, did, notices, revision)
        assert app._editor.editable

    def test_settings_auto_save(self, editing):
        app, store, did, notices = editing
        revision = store._load_any(did).revision
        _revoke(store, did, allow_users=["bob"])
        dialog = app._editor._settings_view
        dialog._draft.description = "written by bob"

        dialog._apply_settings()

        _assert_refused(app, store, did, notices, revision)

    def test_app_save(self, editing):
        app, store, did, notices = editing
        revision = store._load_any(did).revision
        _revoke(store, did, deny_users=["bob"])
        app._editor.settings.description = "written by bob"
        app._editor.dirty = True

        assert not app._save_current_dashboard()

        _assert_refused(app, store, did, notices, revision)

    def test_save_still_works_while_allowed(self, editing):
        app, store, did, notices = editing
        app._editor.settings.description = "written by bob"
        app._editor.dirty = True

        app._editor._save_button.clicks += 1

        assert store._load_any(did).settings["description"] == "written by bob"
        assert notices == []

    def test_discard_does_not_show_newer_content_after_access_is_revoked(self, editing):
        app, store, did, notices = editing
        newer = store._load_any(did)
        newer.settings = {"description": "alice's secret plan"}
        store.save_dashboard(newer)
        _revoke(store, did, deny_users=["bob"])
        app._editor.dirty = True

        app._editor._dashboard_bar.discard()

        assert app._editor.settings.description != "alice's secret plan"
        assert ("error", "You no longer have access to this dashboard.") in notices

    def test_reload_does_not_show_newer_content_after_access_is_revoked(self, editing):
        app, store, did, notices = editing
        newer = store._load_any(did)
        newer.settings = {"description": "alice's secret plan"}
        store.save_dashboard(newer)
        _revoke(store, did, deny_users=["bob"])

        app._editor._reload_from_store()

        assert app._editor.settings.description != "alice's secret plan"
        assert ("error", "You no longer have access to this dashboard.") in notices

    def test_discard_after_edit_rights_are_revoked_loads_read_only(self, editing):
        app, store, did, _notices = editing
        newer = store._load_any(did)
        newer.settings = {"description": "newer"}
        store.save_dashboard(newer)
        _revoke(store, did, allow_users=["bob"])

        app._editor._dashboard_bar.discard()

        assert app._editor.settings.description == "newer"
        assert app._editor.read_only


def _restricted(store, *, viewers_can_inspect=False):
    model = store.create_dashboard("alice", "Secret")
    model.settings = {"viewers_can_inspect": viewers_can_inspect}
    store.save_dashboard(model)
    store.set_permission(model.dashboard_id, Permission.from_spec(allow_users=["bob"]))
    return store._load_any(model.dashboard_id)


def _labels(actions):
    return [action["label"] for action in actions]


class TestCopyingRespectsInspection:
    def test_viewer_may_not_copy_a_dashboard_that_hides_outputs(self, location):  # noqa: F811
        store = MemoryDashboardStore()
        model = _restricted(store)
        app = _app(store, BOB, editor_options={"inspect_outputs": True})
        notices = _capture(app)

        assert app._dashboard_menu_actions(model) == ()
        assert _labels(app._build_launcher()._menu_items(model)) == ["Open"]
        assert app._duplicate_dashboard(model.dashboard_id) is None
        assert not app._download_dashboard(model.dashboard_id)
        assert [m.dashboard_id for m in store._all_dashboards()] == [model.dashboard_id]
        assert notices[-1][0] == "error"

    @pytest.mark.parametrize(
        ("inspect_outputs", "viewers_can_inspect", "identity"),
        [(True, True, BOB), (False, False, BOB), (True, False, Identity(user="carl"))],
        ids=["viewers-can-inspect", "inspection-off", "editor"],
    )
    def test_copy_is_offered_when_it_reveals_nothing_new(
        self,
        location,  # noqa: F811
        inspect_outputs,
        viewers_can_inspect,
        identity,
    ):
        store = MemoryDashboardStore()
        model = _restricted(store, viewers_can_inspect=viewers_can_inspect)
        store.set_permission(
            model.dashboard_id,
            Permission.from_spec(allow_users=["bob"], allow_edit_users=["carl"]),
        )
        model = store._load_any(model.dashboard_id)
        app = _app(store, identity, editor_options={"inspect_outputs": inspect_outputs})

        assert {"Duplicate", "Download as file"} <= set(
            _labels(app._dashboard_menu_actions(model))
        )
        copy = app._duplicate_dashboard(model.dashboard_id)
        assert copy is not None and copy.user_id == identity.user
        assert app._download_dashboard(model.dashboard_id)


class TestDeniedDashboardDisclosure:
    def _views(self, store):
        model = store.create_dashboard("alice", "Layoffs 2026")
        store.set_permission(model.dashboard_id, Permission.from_spec(allow_users=["alice"]))
        app = _app(store, BOB)
        denied = app._missing_dashboard_view(model.dashboard_id)
        missing = app._missing_dashboard_view("no-such-dashboard")
        return denied, missing

    def test_guessable_ids_answer_like_a_missing_dashboard(self, tmp_path, location):  # noqa: F811
        denied, missing = self._views(JSONFolderDashboardStore(tmp_path / "dashboards"))
        assert (denied.icon, denied.title, denied.tone) == (
            missing.icon,
            missing.title,
            missing.tone,
        )
        assert "alice" not in denied.text
        assert "access" not in denied.title.lower()

    def test_random_ids_name_the_owner(self, location):  # noqa: F811
        denied, missing = self._views(MemoryDashboardStore())
        assert denied.icon == "lock"
        assert "alice" in denied.text
        assert missing.title != denied.title


ROLES = (None, "view", "edit", "blocked")
OUTSIDER = Identity(user="zoe", oauth_user="zoe")
BOB_ID = Identity(user="bob", oauth_user="bob")
ANALYST = Identity(user="ann", oauth_user="ann", groups=frozenset({"analysts"}))


def _expected_chip(permission, default_allow):
    """The chip label the access rules themselves imply."""

    def can_view(identity):
        return is_authorized(permission, identity, default_allow=default_allow, owner="alice")

    if can_view(OUTSIDER):
        return "Everyone"
    shared = can_view(BOB_ID) + can_view(ANALYST)
    return f"Shared with {shared}" if shared else "Only you"


def test_sharing_chip_matches_access_for_every_dialog_combination(location):  # noqa: F811
    store = MemoryDashboardStore()
    app = _app(store, ALICE)
    launcher = app._build_launcher()
    model = store.create_dashboard("alice", "Shared")
    for bob, group, everyone, default_allow in itertools.product(
        ROLES, ROLES, (True, False), (True, False)
    ):
        entries = [
            ShareEntry(name, kind, role)
            for name, kind, role in (("bob", "user", bob), ("analysts", "group", group))
            if role is not None
        ]
        # The dialog has no Everyone switch in a private-by-default project.
        model.permission = permission_from_entries(
            entries, everyone=everyone and default_allow, owner="alice"
        )
        app.auth_config = AuthConfig(default_allow=default_allow)
        expected = _expected_chip(model.permission, default_allow)
        chip = launcher._sharing_chip(model)
        assert f">{expected}<" in chip, (bob, group, everyone, default_allow, chip)
