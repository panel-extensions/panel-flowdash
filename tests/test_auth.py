"""Tests for the authorization model in panel_flowdash.auth."""

from contextlib import contextmanager
from typing import ClassVar
from unittest.mock import PropertyMock, patch

import panel as pn

from panel_flowdash.auth import (
    ANONYMOUS_USER,
    AuthConfig,
    Identity,
    Permission,
    can_administer,
    is_authorized,
    make_authorize_callback,
    resolve_identity,
)
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry


def _page_entry(app_id, *, allow_groups=(), deny_users=(), page=True):
    section, name = app_id.split("/")
    return RegistryEntry(
        app_id=app_id,
        section=section,
        name=name,
        page_path=f"/{app_id}",
        module_name=app_id.replace("/", "."),
        metadata=PanelAppMetadata(
            page=page,
            component=not page,
            allow_groups=list(allow_groups),
            deny_users=list(deny_users),
        ),
    )


def make_identity(user="alice", *, oauth=None, system=None, groups=()):
    return Identity(
        user=user,
        oauth_user=oauth,
        system_user=system,
        groups=frozenset(groups),
    )


@contextmanager
def panel_session(user, user_info):
    """Patch the read-only pn.state.user / user_info properties."""
    state_cls = type(pn.state)
    with (
        patch.object(state_cls, "user", PropertyMock(return_value=user)),
        patch.object(state_cls, "user_info", PropertyMock(return_value=user_info)),
    ):
        yield


class TestPermissionSerialization:
    def test_empty_permission(self):
        perm = Permission()
        assert perm.is_empty

    def test_round_trip(self):
        perm = Permission.from_spec(
            allow_users=["alice"],
            allow_groups=["finance"],
            deny_users=["bob"],
            deny_groups=["temps"],
        )
        restored = Permission.from_dict(perm.to_dict())
        assert restored == perm
        assert not restored.is_empty

    def test_from_dict_partial(self):
        perm = Permission.from_dict({"allow_groups": ["finance"]})
        assert perm.allow_groups == frozenset({"finance"})
        assert perm.allow_users == frozenset()

    def test_from_dict_none(self):
        assert Permission.from_dict(None) == Permission()


class TestIsAuthorized:
    def test_no_rules_default_allow(self):
        assert is_authorized(Permission(), make_identity(), default_allow=True) is True

    def test_no_rules_default_deny(self):
        assert is_authorized(Permission(), make_identity(), default_allow=False) is False

    def test_none_permission_defers_to_default(self):
        assert is_authorized(None, make_identity(), default_allow=True) is True
        assert is_authorized(None, make_identity(), default_allow=False) is False

    def test_allow_group_match(self):
        perm = Permission.from_spec(allow_groups=["finance"])
        assert is_authorized(perm, make_identity(groups=["finance"])) is True
        assert is_authorized(perm, make_identity(groups=["eng"])) is False

    def test_allow_user_match(self):
        perm = Permission.from_spec(allow_users=["alice"])
        assert is_authorized(perm, make_identity("alice")) is True
        assert is_authorized(perm, make_identity("bob")) is False

    def test_allow_user_matches_system_user(self):
        perm = Permission.from_spec(allow_users=["svc"])
        ident = make_identity("svc", system="svc")
        assert is_authorized(perm, ident) is True

    def test_deny_wins_over_allow(self):
        perm = Permission.from_spec(allow_groups=["finance"], deny_users=["alice"])
        ident = make_identity("alice", groups=["finance"])
        assert is_authorized(perm, ident) is False

    def test_deny_group_wins_over_owner(self):
        perm = Permission.from_spec(deny_groups=["temps"])
        ident = make_identity("alice", groups=["temps"])
        assert is_authorized(perm, ident, owner="alice") is False

    def test_owner_allowed_without_rule(self):
        perm = Permission.from_spec(allow_groups=["finance"])
        ident = make_identity("alice", groups=["eng"])
        assert is_authorized(perm, ident, owner="alice") is True

    def test_allow_any_of_multiple(self):
        perm = Permission.from_spec(allow_groups=["finance", "execs"])
        assert is_authorized(perm, make_identity(groups=["execs"])) is True


class TestCanAdminister:
    def test_owner_can_administer(self):
        assert can_administer(make_identity("alice"), "alice", []) is True

    def test_no_admin_groups_allows_anyone(self):
        # Without configured admin groups administration is unrestricted.
        ident = make_identity("bob", groups=["eng"])
        assert can_administer(ident, "alice", []) is True

    def test_admin_group_can_administer(self):
        ident = make_identity("bob", groups=["admins"])
        assert can_administer(ident, "alice", ["admins"]) is True

    def test_non_owner_non_admin_cannot(self):
        ident = make_identity("bob", groups=["eng"])
        assert can_administer(ident, "alice", ["admins"]) is False


class TestResolveIdentity:
    def test_anonymous_when_no_user(self):
        with panel_session(None, None):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                ident = resolve_identity()
        assert ident.user == ANONYMOUS_USER
        assert ident.oauth_user is None

    def test_system_user_fallback(self):
        with panel_session(None, None):
            with patch("panel_flowdash.auth._system_user", return_value="svc"):
                ident = resolve_identity()
        assert ident.user == "svc"
        assert ident.system_user == "svc"
        assert ident.oauth_user is None

    def test_oauth_user_preferred(self):
        with panel_session("alice", {}):
            with patch("panel_flowdash.auth._system_user", return_value="svc"):
                ident = resolve_identity()
        assert ident.user == "alice"
        assert ident.oauth_user == "alice"

    def test_groups_from_claims_list(self):
        with panel_session("alice", {"groups": ["finance", "eng"]}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                ident = resolve_identity()
        assert ident.groups == frozenset({"finance", "eng"})

    def test_groups_from_claims_string(self):
        cfg = AuthConfig(group_claims=("roles",))
        with panel_session("alice", {"roles": "finance,eng"}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                ident = resolve_identity(cfg)
        assert ident.groups == frozenset({"finance", "eng"})

    def test_groups_union_with_mapping(self):
        cfg = AuthConfig(user_groups={"alice": frozenset({"execs"})})
        with panel_session("alice", {"groups": ["eng"]}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                ident = resolve_identity(cfg)
        assert ident.groups == frozenset({"eng", "execs"})

    def test_resolve_groups_callback(self):
        cfg = AuthConfig(resolve_groups=lambda ident: ["dynamic"])
        with panel_session("alice", {}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                ident = resolve_identity(cfg)
        assert "dynamic" in ident.groups


class TestMakeAuthorizeCallback:
    def _registry(self):
        return {
            "Sec/open": _page_entry("Sec/open"),
            "Sec/restricted": _page_entry("Sec/restricted", allow_groups=["finance"]),
            "Sec/comp": _page_entry("Sec/comp", page=False),
        }

    def test_unrestricted_page_allowed(self):
        cb = make_authorize_callback(self._registry(), AuthConfig())
        with panel_session("alice", {}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                assert cb({}, "/Sec/open") is True

    def test_restricted_denied_without_group(self):
        cb = make_authorize_callback(self._registry(), AuthConfig())
        with panel_session("alice", {"groups": ["eng"]}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                assert cb({}, "/Sec/restricted") is False

    def test_restricted_allowed_with_group(self):
        cb = make_authorize_callback(self._registry(), AuthConfig())
        with panel_session("alice", {"groups": ["finance"]}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                assert cb({}, "/Sec/restricted") is True

    def test_non_page_route_passes_through(self):
        cb = make_authorize_callback(self._registry(), AuthConfig())
        with panel_session("alice", {}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                assert cb({}, "/components") is True
                assert cb({}, "/dash/abc123") is True
                assert cb({}, "/Sec/comp") is True

    def test_prefix_stripped(self):
        cb = make_authorize_callback(self._registry(), AuthConfig(), url_prefix="/app")
        with panel_session("alice", {"groups": ["finance"]}):
            with patch("panel_flowdash.auth._system_user", return_value=None):
                assert cb({}, "/app/Sec/restricted") is True


class TestAuthConfigFromModule:
    def test_none_module(self):
        cfg = AuthConfig.from_module(None)
        assert cfg.default_allow is True
        assert cfg.admin_groups == frozenset()

    def test_reads_names(self):
        class Mod:
            group_claims: ClassVar = ["cognito:groups"]
            user_groups: ClassVar = {"alice": ["execs"]}
            admin_groups: ClassVar = ["admins"]
            default_allow = False

        cfg = AuthConfig.from_module(Mod)
        assert cfg.group_claims == ("cognito:groups",)
        assert cfg.user_groups == {"alice": frozenset({"execs"})}
        assert cfg.admin_groups == frozenset({"admins"})
        assert cfg.default_allow is False
