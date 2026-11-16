"""Tests for the authorization model in panel_flowdash.auth."""

import importlib
import logging
import uuid
from contextlib import contextmanager
from typing import ClassVar
from unittest.mock import PropertyMock, patch

import panel as pn
import pytest

from panel_flowdash.auth import (
    ANONYMOUS_USER,
    AuthConfig,
    Identity,
    Permission,
    can_administer,
    entry_authorized,
    is_authorized,
    make_authorize_callback,
    resolve_identity,
)
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry, build_registry


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

    def test_from_dict_treats_a_string_as_one_name(self):
        perm = Permission.from_dict({"deny_users": "bob", "allow_groups": "finance"})
        assert perm.deny_users == frozenset({"bob"})
        assert perm.allow_groups == frozenset({"finance"})
        assert not is_authorized(perm, make_identity("bob", oauth="bob"))

    def test_from_dict_refuses_a_non_list(self):
        with pytest.raises(TypeError):
            Permission.from_dict({"deny_users": 5})

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

    def test_no_admin_groups_allows_only_the_owner(self):
        ident = make_identity("bob", groups=["eng"])
        assert can_administer(ident, "alice", []) is False
        assert can_administer(make_identity("alice"), "alice", []) is True

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

    def test_oauth_user_does_not_carry_the_system_user(self):
        cfg = AuthConfig(user_groups={"svc": frozenset({"admins"})})
        with panel_session("mallory", {}):
            with patch("panel_flowdash.auth._system_user", return_value="svc"):
                ident = resolve_identity(cfg)
        assert ident.system_user is None
        assert ident.user_names == frozenset({"mallory"})
        assert "admins" not in ident.groups
        assert is_authorized(Permission.from_spec(deny_users=["svc"]), ident)
        assert not can_administer(ident, "svc", frozenset({"admins"}))

    def test_system_user_gets_its_mapped_groups(self):
        cfg = AuthConfig(user_groups={"svc": frozenset({"admins"})})
        with panel_session(None, None):
            with patch("panel_flowdash.auth._system_user", return_value="svc"):
                ident = resolve_identity(cfg)
        assert ident.groups == frozenset({"admins"})

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

    def test_string_values_are_single_names(self):
        class Mod:
            group_claims = "cognito:groups"
            user_groups: ClassVar = {"alice": "execs"}
            admin_groups = "admins"
            default_allow = "False"

        cfg = AuthConfig.from_module(Mod)
        assert cfg.group_claims == ("cognito:groups",)
        assert cfg.user_groups == {"alice": frozenset({"execs"})}
        assert cfg.admin_groups == frozenset({"admins"})
        assert cfg.default_allow is False

    @pytest.mark.parametrize("value", ["no", None, 2, []])
    def test_ambiguous_default_allow_refused(self, value):
        class Mod:
            default_allow = value

        with pytest.raises(TypeError, match="default_allow"):
            AuthConfig.from_module(Mod)


_SECRET = """
from panel_flowdash import register
ADMINS = ["admins"]
@register(allow_groups=ADMINS)
def app():
    return "secret"
"""

_CUSTOM = """
from panel_flowdash import register
@register(authorize=lambda identity: "admins" in identity.groups)
def app():
    return "custom"
"""

_CONCAT = """
from panel_flowdash import register
@register(deny_users=["bob"] + [])
def app():
    return "concat"
"""

_BROKEN = """
from panel_flowdash import register
ADMINS = ["admins"]
@register(allow_groups=ADMINS)
def app():
    return "broken"
raise RuntimeError("import fails")
"""


class TestNonLiteralPagePermissions:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        # A fresh package name per test, so modules imported by one test are not reused.
        package_name = f"authproj_{uuid.uuid4().hex[:8]}"
        package = tmp_path / package_name
        (package / "pages").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "pages" / "__init__.py").write_text("")
        for module, source in [
            ("secret", _SECRET),
            ("custom", _CUSTOM),
            ("concat", _CONCAT),
            ("broken", _BROKEN),
        ]:
            (package / "pages" / f"{module}.py").write_text(source)
        (package / "pages" / "plain.py").write_text(
            "from panel_flowdash import register\n"
            "@register(allow_users=['bob'])\n"
            "def app():\n    return 'plain'\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        return build_registry(package, package=package_name)

    def test_unevaluable_rules_are_pending(self, project):
        for name in ("secret", "custom", "concat", "broken"):
            assert project[f"pages/{name}"].metadata.auth_pending, name
        assert not project["pages/plain"].metadata.auth_pending
        assert project["pages/plain"].metadata.allow_users == ["bob"]

    def test_pending_entry_is_resolved_by_import(self, project):
        bob = make_identity("bob", oauth="bob")
        admin = make_identity("carol", oauth="carol", groups=["admins"])
        for name in ("secret", "custom"):
            entry = project[f"pages/{name}"]
            assert entry_authorized(entry, bob) is False
            assert entry.metadata.auth_pending is False
            assert entry_authorized(entry, admin) is True
        assert entry_authorized(project["pages/concat"], bob) is False
        assert entry_authorized(project["pages/concat"], admin) is True

    def test_failed_import_stays_denied(self, project):
        entry = project["pages/broken"]
        assert entry_authorized(entry, make_identity("carol", groups=["admins"])) is False
        assert entry.metadata.auth_pending

    def test_failed_import_is_not_retried(self, project):
        entry = project["pages/broken"]
        admin = make_identity("carol", groups=["admins"])
        cb = make_authorize_callback(project, AuthConfig())
        with patch("importlib.import_module", wraps=importlib.import_module) as spy:
            assert entry_authorized(entry, admin) is False
            assert entry_authorized(entry, admin) is False
            with panel_session("carol", {"groups": ["admins"]}):
                assert cb({}, "/pages/broken") is False
        assert spy.call_count == 1
        assert isinstance(entry.load_error, RuntimeError)

    def test_no_import_mode_denies_pending_without_importing(self, project):
        entry = project["pages/secret"]
        admin = make_identity("carol", groups=["admins"])
        assert entry_authorized(entry, admin, load=False) is False
        assert entry.app is None
        assert entry_authorized(project["pages/plain"], make_identity("bob"), load=False)

    def test_http_gate_fails_closed_and_reads_live_metadata(self, project):
        cb = make_authorize_callback(project, AuthConfig())
        with panel_session("bob", {}):
            assert cb({}, "/pages/secret") is False
            assert cb({}, "/pages/custom") is False
            assert cb({}, "/pages/broken") is False
        with panel_session("carol", {"groups": ["admins"]}):
            assert cb({}, "/pages/secret") is True
            assert cb({}, "/pages/custom") is True
            assert cb({}, "/pages/broken") is False

    def test_http_gate_sees_rules_changed_after_it_was_built(self):
        registry = {"Sec/open": _page_entry("Sec/open")}
        cb = make_authorize_callback(registry, AuthConfig())
        registry["Sec/open"].metadata = PanelAppMetadata(page=True, allow_groups=["finance"])
        with panel_session("alice", {}):
            assert cb({}, "/Sec/open") is False

    @pytest.mark.parametrize("value", ["5", "{'admins': 1}", "[1]"])
    def test_invalid_literal_rules_deny_the_page_without_aborting_the_scan(
        self, tmp_path, caplog, value
    ):
        (tmp_path / "pages").mkdir()
        (tmp_path / "pages" / "bad.py").write_text(
            "from panel_flowdash import register\n"
            f"@register(allow_groups={value})\n"
            "def app():\n    return 'bad'\n"
        )
        (tmp_path / "pages" / "ok.py").write_text(
            "from panel_flowdash import register\n@register()\ndef app():\n    return 'ok'\n"
        )
        with caplog.at_level(logging.ERROR, logger="panel_flowdash.registry"):
            registry = build_registry(tmp_path)
        assert "allow_groups must be a list of names" in caplog.text
        bad = registry["pages/bad"]
        assert bad.metadata.auth_pending
        assert entry_authorized(bad, make_identity("carol"), load=False) is False
        assert entry_authorized(registry["pages/ok"], make_identity("carol"), load=False)

    def test_string_literal_is_one_name(self, tmp_path):
        (tmp_path / "pages").mkdir()
        (tmp_path / "pages" / "p.py").write_text(
            "from panel_flowdash import register\n"
            "@register(deny_users='bob')\n"
            "def app():\n    return 'p'\n"
        )
        entry = build_registry(tmp_path)["pages/p"]
        assert entry.metadata.deny_users == ["bob"]


class TestInAppPendingPermissions:
    async def test_page_with_non_literal_rules_is_gated_on_first_render(
        self, tmp_path, monkeypatch
    ):
        from panel_flowdash.app import FlowDashApp
        from panel_flowdash.dashboard_store import MemoryDashboardStore

        section = f"Gate{uuid.uuid4().hex[:8]}"
        (tmp_path / section).mkdir()
        (tmp_path / section / "__init__.py").write_text("")
        (tmp_path / section / "secret.py").write_text(_SECRET)
        monkeypatch.syspath_prepend(str(tmp_path))

        with panel_session("bob", {}):
            app = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
        entry = app._page_entries[f"{section}/secret"]

        view = await app._render_page((section, "secret"))
        assert "Access denied" in view.object
        assert entry.metadata.allow_groups == ["admins"]
        assert f"{section}/secret" not in app._accessible_page_entries()

    @pytest.fixture
    def gated_project(self, tmp_path, monkeypatch):
        section = f"Gate{uuid.uuid4().hex[:8]}"
        (tmp_path / section).mkdir()
        (tmp_path / section / "__init__.py").write_text("")
        (tmp_path / section / "secret.py").write_text(_SECRET)
        (tmp_path / section / "broken.py").write_text(_BROKEN)
        monkeypatch.syspath_prepend(str(tmp_path))
        return tmp_path, section

    async def test_listings_never_import_and_show_pages_once_resolved(self, gated_project):
        from panel_flowdash.app import FlowDashApp
        from panel_flowdash.dashboard_store import MemoryDashboardStore

        tmp_path, section = gated_project
        with (
            panel_session("carol", {"groups": ["admins"]}),
            patch("importlib.import_module", wraps=importlib.import_module) as spy,
        ):
            app = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
            assert app._accessible_page_entries() == {}
            assert not any(call.args[0].startswith(section) for call in spy.call_args_list)

            await app._resolve_pending_entries()
            await app._resolve_pending_entries()

        imports = [c.args[0] for c in spy.call_args_list if c.args[0].startswith(section)]
        assert sorted(imports) == [f"{section}.broken", f"{section}.secret"]
        assert list(app._accessible_page_entries()) == [f"{section}/secret"]
        paths = [
            item.get("path") for group in app._menu_list.items for item in group.get("items", [])
        ]
        assert f"/{section}/secret" in paths
        assert f"/{section}/broken" not in paths

    async def test_failed_page_import_is_not_retried_on_render(self, gated_project):
        from panel_flowdash.app import FlowDashApp
        from panel_flowdash.dashboard_store import MemoryDashboardStore

        tmp_path, section = gated_project
        with panel_session("carol", {"groups": ["admins"]}):
            app = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
        with patch("importlib.import_module", wraps=importlib.import_module) as spy:
            for _ in range(2):
                view = await app._render_page((section, "broken"))
                assert "Access denied" in view.object
        assert [c.args[0] for c in spy.call_args_list] == [f"{section}.broken"]
