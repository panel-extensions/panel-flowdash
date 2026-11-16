"""Tests for the combined dashboard settings and sharing dialog."""

import pytest

from panel_flowdash import DashboardItem, MemoryDashboardStore, Permission
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.settings_dialog import (
    ShareEntry,
    entries_from_permission,
    permission_from_entries,
    refresh_choice,
)


class TestRoleMapping:
    def test_each_role_maps_to_its_permission_fields(self):
        entries = [
            ShareEntry("analytics", "group", "view"),
            ShareEntry("ana", "user", "view"),
            ShareEntry("leads", "group", "edit"),
            ShareEntry("bob", "user", "edit"),
            ShareEntry("contractors", "group", "blocked"),
            ShareEntry("eve", "user", "blocked"),
        ]
        permission = permission_from_entries(entries, everyone=False, owner="alice")
        assert permission == Permission.from_spec(
            allow_groups=["analytics"],
            allow_users=["ana"],
            allow_edit_groups=["leads"],
            allow_edit_users=["bob"],
            deny_groups=["contractors"],
            deny_users=["eve"],
        )
        assert entries_from_permission(permission, "alice") == (
            sorted(entries, key=lambda e: e.name.lower()),
            False,
        )

    def test_blocked_wins_over_other_rules_naming_the_same_principal(self):
        permission = Permission.from_spec(
            allow_users=["bob"], allow_edit_users=["bob"], deny_users=["bob"]
        )
        entries, _ = entries_from_permission(permission, "alice")
        assert entries == [ShareEntry("bob", "user", "blocked")]

    def test_edit_wins_over_view(self):
        permission = Permission.from_spec(allow_groups=["sales"], allow_edit_groups=["sales"])
        entries, _ = entries_from_permission(permission, "alice")
        assert entries == [ShareEntry("sales", "group", "edit")]

    def test_everyone_means_no_view_rules(self):
        entries = [ShareEntry("ana", "user", "view"), ShareEntry("bob", "user", "edit")]
        permission = permission_from_entries(entries, everyone=True, owner="alice")
        assert permission == Permission.from_spec(allow_edit_users=["bob"])
        assert entries_from_permission(permission, "alice")[1] is True

    def test_nobody_else_may_view_is_stored_as_an_owner_rule(self):
        entries = [ShareEntry("bob", "user", "edit")]
        permission = permission_from_entries(entries, everyone=False, owner="alice")
        assert permission == Permission.from_spec(allow_users=["alice"], allow_edit_users=["bob"])
        assert entries_from_permission(permission, "alice") == (entries, False)

    def test_empty_permission_lets_everyone_view(self):
        assert entries_from_permission(Permission(), "alice") == ([], True)


@pytest.mark.parametrize(
    ("period", "choice"),
    [(0, "Off"), (60, "1 min"), (300, "5 min"), (900, "15 min"), (3600, "1 hour"), (42, "Custom")],
)
def test_refresh_choice(period, choice):
    assert refresh_choice(period) == choice


ALICE = Identity(user="alice", oauth_user="alice")
BOB = Identity(user="bob", oauth_user="bob")


@pytest.fixture
def store():
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard("alice", "Plant")
    store.set_permission(dashboard.dashboard_id, Permission.from_spec(allow_edit_users=["bob"]))
    return store


def _app(store, identity, monkeypatch, **params):
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
    return FlowDashApp(
        registry={},
        store=store,
        auth_config=AuthConfig(user_groups={"erin": ["sales"]}),
        notifications=False,
        **params,
    )


def _loaded(store, identity, monkeypatch):
    app = _app(store, identity, monkeypatch)
    (dashboard,) = store._all_dashboards()
    app._editor.read_only = not app._can_edit_dashboard(dashboard.dashboard_id)
    app._editor.load_model(dashboard)
    return app, dashboard.dashboard_id


class TestDialog:
    async def test_presets_set_refresh_period(self):
        editor = FlowDash({}, notifications=False)
        view = editor._settings_view
        editor._settings_dialog.open = True
        view._refresh_choice.value = "15 min"
        assert view._draft.refresh_period == 900
        assert editor.settings.refresh_period == 0
        view._save_button.clicks += 1
        assert editor.settings.refresh_period == 900
        assert not editor._settings_dialog.open

    async def test_cancel_discards_the_changes(self):
        editor = FlowDash({}, notifications=False)
        editor._settings_dialog.open = True
        editor._settings_view._refresh_choice.value = "1 hour"
        editor._settings_view._cancel_button.clicks += 1
        editor._settings_dialog.open = True
        assert editor.settings.refresh_period == 0
        assert editor._settings_view._refresh_choice.value == "Off"

    async def test_custom_period_selects_custom(self):
        editor = FlowDash({}, notifications=False)
        editor.settings.refresh_period = 42
        editor._settings_dialog.open = True
        assert editor._settings_view._refresh_choice.value == "Custom"
        assert editor._settings_view._custom_period.visible

    async def test_settings_alone_are_saved_right_away(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        view = app._editor._settings_view
        app._editor._settings_dialog.open = True
        view._draft.description = "Wind capacity by year."
        view._save_button.clicks += 1
        assert store._load_any(did).settings["description"] == "Wind capacity by year."
        assert not app._editor.dirty

    async def test_settings_join_other_unsaved_changes(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        app._editor.dirty = True
        view = app._editor._settings_view
        app._editor._settings_dialog.open = True
        assert "unsaved changes" in view._footer_note.object
        view._refresh_choice.value = "5 min"
        view._save_button.clicks += 1
        assert app._editor.settings.refresh_period == 300
        assert app._editor.dirty
        assert "refresh_period" not in store._load_any(did).settings

    async def test_administrator_sees_sharing_and_delete(self, store, monkeypatch):
        app, _did = _loaded(store, ALICE, monkeypatch)
        view = app._editor._settings_view
        app._editor._settings_dialog.open = True
        assert [item["label"] for item in view._nav.items] == ["General", "Refresh", "Sharing"]
        assert view._delete_button.visible
        assert not view._title_input.disabled

    async def test_editor_does_not_see_sharing(self, store, monkeypatch):
        app, did = _loaded(store, BOB, monkeypatch)
        view = app._editor._settings_view
        app._editor._settings_dialog.open = True
        assert [item["label"] for item in view._nav.items] == ["General", "Refresh"]
        assert not view._delete_button.visible
        assert view._title_input.disabled
        app._open_share_dialog(did, "Plant")
        assert view.section == "general"

    async def test_share_opens_the_sharing_section(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        app._open_share_dialog(did, "Plant")
        assert app._share_dialog is app._editor._settings_dialog
        assert app._share_dialog.open
        assert app._editor._settings_view.section == "sharing"

    async def test_role_change_is_stored_on_save(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        view = app._editor._settings_view
        app._open_share_dialog(did, "Plant")
        view.set_role("bob", "user", "blocked")
        assert store._load_any(did).permission == Permission.from_spec(allow_edit_users=["bob"])
        view._save_button.clicks += 1
        assert store._load_any(did).permission == Permission.from_spec(deny_users=["bob"])
        assert app._current_dashboard.permission == Permission.from_spec(deny_users=["bob"])

    async def test_unchanged_sharing_is_not_written(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        calls = []
        monkeypatch.setattr(store, "set_permission", lambda *args: calls.append(args) or True)
        app._open_share_dialog(did, "Plant")
        app._editor._settings_view._save_button.clicks += 1
        assert calls == []

    async def test_group_names_are_added_as_groups(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        view = app._editor._settings_view
        app._open_share_dialog(did, "Plant")
        view._add_input.value = "sales"
        view._add_input.value = "group:ops"
        view._add_input.value = "erin"
        view._add_input.value = "alice"
        assert [(e.name, e.kind, e.role) for e in view._entries] == [
            ("bob", "user", "edit"),
            ("sales", "group", "view"),
            ("ops", "group", "view"),
            ("erin", "user", "view"),
        ]

    async def test_rename_refuses_a_duplicate_title(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        store.create_dashboard("alice", "Taken")
        view = app._editor._settings_view
        app._editor._settings_dialog.open = True
        view._title_input.value = "Taken"
        view._save_button.clicks += 1
        assert view._title_input.error_state
        assert app._editor._settings_dialog.open
        view._title_input.value = "Plant 2"
        view._save_button.clicks += 1
        assert store._load_any(did).title == "Plant 2"
        assert app._current_dashboard.title == "Plant 2"

    async def test_delete_opens_the_typed_confirmation(self, store, monkeypatch):
        app, did = _loaded(store, ALICE, monkeypatch)
        app._editor._settings_dialog.open = True
        app._editor._settings_view._delete_button.clicks += 1
        assert not app._editor._settings_dialog.open
        assert app._dialog.open
        assert app._dialog_context == {"action": "delete", "dashboard_id": did}

    async def test_switch_is_hidden_without_a_default_allow_policy(self, store, monkeypatch):
        app = _app(store, ALICE, monkeypatch)
        app.auth_config = AuthConfig(default_allow=False)
        (dashboard,) = store._all_dashboards()
        app._open_share_dialog(dashboard.dashboard_id, "Plant")
        view = app._editor._settings_view
        assert not view._everyone_row.visible
        view._add_input.value = "erin"
        view._save_button.clicks += 1
        assert store._load_any(dashboard.dashboard_id).permission == Permission.from_spec(
            allow_users=["erin"], allow_edit_users=["bob"]
        )

    async def test_last_refresh_duration_is_reported(self):
        editor = FlowDash({}, notifications=False)
        editor.param.trigger("refresh")
        editor._settings_dialog.open = True
        assert "Last refresh took" in editor._settings_view._refresh_info.object

    async def test_dialog_for_another_dashboard_only_offers_its_sharing(self, monkeypatch):
        store = MemoryDashboardStore()
        other = store.create_dashboard("alice", "Other")
        current = store.create_dashboard("alice", "Current")
        current.items = [DashboardItem(instance_id="x", component_id="missing")]
        app = _app(store, ALICE, monkeypatch)
        app._editor.load_model(current)
        app._open_share_dialog(other.dashboard_id, "Other")
        view = app._editor._settings_view
        assert [item["label"] for item in view._nav.items] == ["General", "Sharing"]
        view._title_input.value = "Renamed"
        view._save_button.clicks += 1
        assert store._load_any(other.dashboard_id).title == "Renamed"
        assert app._current_dashboard.title == "Current"
