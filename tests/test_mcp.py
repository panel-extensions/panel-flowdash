"""Unit tests for MCP configuration, secrets, stdio policy, access rules, PermissionEditor and MCPSettings UI."""

from __future__ import annotations

import getpass
import json
import logging
import sys

import panel as pn
import pytest
from bokeh.document import Document

from panel_flowdash import project as project_module
from panel_flowdash.admin import (
    ADMIN_ROUTE,
    MCPSettings,
    PermissionDialog,
    PermissionEditor,
    format_name_list,
    known_principals,
    parse_name_list,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import (
    AuthConfig,
    Identity,
    Permission,
    evaluate_permission,
    permission_lockout_reason,
)
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.mcp import (
    MCP_CHECKS,
    SECRET_ENV_VARS_ENV,
    STDIO_COMMANDS_ENV,
    STDIO_ENV_VARS_ENV,
    MCPServerCheck,
    MCPServerConfig,
    MCPToolInfo,
    access_to_dict,
    allowed_secret_env_vars,
    allowed_stdio_commands,
    allowed_stdio_env,
    check_mcp_secret_ref,
    check_stdio_command,
    check_stdio_env,
    configured_servers,
    dump_mcp,
    get_mcp_check,
    is_mcp_tool,
    is_valid_mcp_tool_name,
    mcp_server_authorized,
    mcp_tool_authorized,
    mcp_tool_name,
    parse_mcp_tool_name,
    resolve_http_headers,
    resolve_stdio_env,
    resolve_stdio_executable,
    set_mcp_check,
    url_origin,
    validate_server_name,
)
from panel_flowdash.project import PROJECT_FILE, Problem, ProjectError, load_project, parse_project
from panel_flowdash.secret_store import (
    KEY_ENV,
    SecretError,
    encrypt_secret,
    fernet_key,
    resolve_secret,
)

cryptography = pytest.importorskip("cryptography")

ME = getpass.getuser()
TOKEN_SECRET = "ghp_super_secret_mcp_token_987654321"
HEADER_SECRET = "hdr_secret_value_abcdef1234"


@pytest.fixture
def sections(monkeypatch):
    """Give each test an isolated copy of the project section registry."""
    # Ensure MCP section is registered.
    import panel_flowdash.admin.mcp  # noqa: F401

    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


@pytest.fixture(autouse=True)
def secret_env(monkeypatch, tmp_path):
    """Encrypt with a per-test key and isolate MCP_CHECKS and MCP env vars."""
    monkeypatch.setenv(KEY_ENV, fernet_key())
    monkeypatch.setenv("FLOWDASH_SECRET_KEY_FILE", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))
    monkeypatch.delenv(STDIO_COMMANDS_ENV, raising=False)
    monkeypatch.delenv(STDIO_ENV_VARS_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV_VARS_ENV, raising=False)
    monkeypatch.delenv("FLOWDASH_MCP_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("FLOWDASH_MCP_TOKEN", raising=False)
    MCP_CHECKS.clear()
    yield
    MCP_CHECKS.clear()


def _app(tmp_path, text="", **params):
    (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{ME}]\n{text}")
    params.setdefault("auth_config", AuthConfig())
    return FlowDashApp(
        registry={},
        project_dir=tmp_path,
        store=params.pop("store", MemoryDashboardStore()),
        notifications=False,
        include_builtin_components=False,
        **params,
    )


def _document_text(viewable) -> str:
    doc = Document()
    doc.add_root(pn.panel(viewable).get_root(doc))
    return json.dumps(doc.to_json().content, default=str)


class TestMCPToolNaming:
    def test_round_trip(self):
        name = mcp_tool_name("github", "list_issues")
        assert name == "mcp_github__list_issues"
        assert is_mcp_tool(name)
        assert parse_mcp_tool_name(name) == ("github", "list_issues")

    def test_tool_with_double_underscore_splits_on_first(self):
        assert parse_mcp_tool_name("mcp_my-srv__a__b") == ("my-srv", "a__b")

    def test_non_mcp_names(self):
        for name in ("get_dashboard", "mcp_", "mcp_only", "mcp___tool", "mcp_srv__"):
            assert not is_mcp_tool(name)
            assert parse_mcp_tool_name(name) is None

    def test_server_name_validation(self):
        assert validate_server_name("github") is None
        assert validate_server_name("my_srv-1") is None
        assert validate_server_name("bad__name") is not None
        assert validate_server_name("-leading") is not None
        assert validate_server_name("has space") is not None
        assert validate_server_name("") is not None

    def test_tool_name_compatibility_check(self):
        assert is_valid_mcp_tool_name("github", "list_issues") is True
        assert is_valid_mcp_tool_name("github", "get-pr-1") is True
        assert is_valid_mcp_tool_name("github", "issues.list") is False
        assert is_valid_mcp_tool_name("github", "has space") is False
        assert is_valid_mcp_tool_name("github", "x" * 60) is False


class TestMCPProjectSchema:
    def test_loads_valid_http_and_stdio_servers(self, sections, tmp_path):
        (tmp_path / "docs").mkdir()
        text = """\
mcp:
  servers:
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp/
      token: ${FLOWDASH_MCP_GITHUB_TOKEN}
      headers:
        X-Api-Key: ${FLOWDASH_MCP_EXTRA_KEY}
      description: GitHub tools
      offered_to: [assistant]
      init_timeout: 5
      timeout: 20
      auto_approve: [list_issues, get_*]
      access:
        allow_groups: [engineering]
        deny_users: [contractor1]
      tool_access:
        delete_*:
          allow_groups: [admins]
    local_docs:
      transport: stdio
      command: /opt/mcp/docs-server
      args: ["--readonly"]
      cwd: docs
      env:
        DOCS_KEY: ${FLOWDASH_MCP_DOCS_KEY}
      enabled: false
"""
        project = parse_project(text, tmp_path / PROJECT_FILE)
        data = project.get("mcp")
        assert set(data["servers"]) == {"github", "local_docs"}
        servers = configured_servers(project)
        assert servers["github"].transport == "http"
        assert servers["github"].is_auto_approved("list_issues")
        assert servers["github"].is_auto_approved("get_pr")
        assert not servers["github"].is_auto_approved("create_issue")
        assert servers["github"].access == Permission.from_spec(
            allow_groups=["engineering"], deny_users=["contractor1"]
        )
        assert servers["local_docs"].enabled is False
        assert servers["local_docs"].args == ("--readonly",)

    def test_rejects_plaintext_token_headers_and_env(self, sections, tmp_path):
        text = """\
mcp:
  servers:
    github:
      transport: http
      url: https://example.com/mcp
      token: ghp_plaintext_should_fail
      headers:
        X-Api-Key: raw-secret
    local:
      transport: stdio
      command: /opt/mcp/docs-server
      env:
        API_KEY: raw-env-secret
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "github", "token") in keys
        assert ("servers", "github", "headers", "X-Api-Key") in keys
        assert ("servers", "local", "env", "API_KEY") in keys

    def test_rejects_disallowed_env_var_references_by_default(self, sections, tmp_path):
        text = """\
mcp:
  servers:
    github:
      transport: http
      url: https://example.com/mcp
      token: ${OPENAI_API_KEY}
      headers:
        X-Key: ${FLOWDASH_SECRET_KEY}
    local:
      transport: stdio
      command: /opt/mcp/docs-server
      env:
        AWS_SECRET: ${AWS_SECRET_ACCESS_KEY}
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "github", "token") in keys
        assert ("servers", "github", "headers", "X-Key") in keys
        assert ("servers", "local", "env", "AWS_SECRET") in keys

    def test_custom_secret_env_allowlist_in_context_and_env(self, sections, tmp_path, monkeypatch):
        text = """\
mcp:
  servers:
    github:
      transport: http
      url: https://example.com/mcp
      token: ${CUSTOM_MCP_TOKEN}
"""
        project = parse_project(
            text, tmp_path / PROJECT_FILE, context={"mcp_secret_env_vars": ["CUSTOM_*"]}
        )
        assert project.get("mcp")["servers"]["github"]["token"] == "${CUSTOM_MCP_TOKEN}"

        monkeypatch.setenv(SECRET_ENV_VARS_ENV, "CUSTOM_MCP_TOKEN")
        project2 = parse_project(text, tmp_path / PROJECT_FILE)
        assert project2.get("mcp")["servers"]["github"]["token"] == "${CUSTOM_MCP_TOKEN}"
        assert allowed_secret_env_vars([]) == ()
        assert check_mcp_secret_ref("${FLOWDASH_MCP_TOKEN}", "token", []) is not None

    def test_rejects_cwd_escape_and_relative_stdio_command(self, sections, tmp_path):
        text = """\
mcp:
  servers:
    local:
      transport: stdio
      command: ./bin/mcp-server
      cwd: ../outside
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "local", "command") in keys
        assert ("servers", "local", "cwd") in keys

    def test_rejects_invalid_url_and_unknown_access_keys(self, sections, tmp_path):
        text = """\
mcp:
  servers:
    bad:
      transport: http
      url: file:///etc/passwd
      access:
        allow_all: true
      timeout: -5
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "bad", "url") in keys
        assert ("servers", "bad", "access", "allow_all") in keys
        assert ("servers", "bad", "timeout") in keys

    def test_dump_mcp_round_trips_encrypted_secrets(self, sections, tmp_path):
        encrypted = encrypt_secret(TOKEN_SECRET)
        cfg = MCPServerConfig(
            name="github",
            transport="http",
            url="https://example.com/mcp",
            token=encrypted,
            auto_approve=("list_*",),
        )
        dumped = dump_mcp({"servers": {"github": cfg}})
        assert dumped["servers"]["github"]["token"] == encrypted
        assert TOKEN_SECRET not in str(dumped)


class TestStdioCommandAndEnvPolicy:
    def test_empty_allowlist_denies_everything(self):
        err = check_stdio_command("/opt/mcp/server", ())
        assert err is not None and "disabled by server policy" in err

    def test_argv_prefix_matching_blocks_launcher_argument_injection(self):
        # Allowlisting a full prefix like "uvx mcp-server-fetch" allows trailing args
        # after the prefix, but refuses argument injection such as "--from git+...".
        allowed = allowed_stdio_commands(
            [
                "uvx mcp-server-fetch",
                ["npx", "-y", "@modelcontextprotocol/server-github"],
            ]
        )
        assert check_stdio_command("uvx", allowed, args=["mcp-server-fetch"]) is None
        assert (
            check_stdio_command("uvx", allowed, args=["mcp-server-fetch", "--timeout", "10"])
            is None
        )
        # Attacker attempts --from git+... before or instead of the allowlisted subcommand.
        assert (
            check_stdio_command("uvx", allowed, args=["--from", "git+https://attacker/x", "x"])
            is not None
        )
        assert (
            check_stdio_command(
                "uvx", allowed, args=["--from", "git+https://attacker/x", "mcp-server-fetch"]
            )
            is not None
        )
        # Bare launcher with no matching prefix args is refused.
        assert check_stdio_command("uvx", allowed, args=[]) is not None
        # Sequence prefix entry matches exact prefix and allows trailing args.
        assert (
            check_stdio_command(
                "npx", allowed, args=["-y", "@modelcontextprotocol/server-github", "--read-only"]
            )
            is None
        )
        assert check_stdio_command("npx", allowed, args=["-y", "evil-pkg"]) is not None

    def test_absolute_path_required_for_separator_entries(self, tmp_path):
        exe = tmp_path / "my-mcp"
        exe.write_text("#!/bin/sh\n")
        assert (
            check_stdio_command(str(exe), ([str(exe), "--readonly"],), args=["--readonly"]) is None
        )
        assert check_stdio_command(str(exe), ([str(exe), "--readonly"],), args=[]) is not None
        assert check_stdio_command("my-mcp", (str(exe),)) is not None
        assert resolve_stdio_executable(str(exe), (str(exe),)) == str(exe.resolve())
        # Relative paths are rejected in both command and allowlist.
        assert allowed_stdio_commands(["./bin/server"]) == ()
        assert check_stdio_command("./bin/server", ("./bin/server",)) is not None

    def test_env_var_fallback_and_explicit_override(self, monkeypatch):
        monkeypatch.setenv(STDIO_COMMANDS_ENV, "/opt/mcp/a --read, /opt/mcp/b")
        assert allowed_stdio_commands(None) == (("/opt/mcp/a", "--read"), ("/opt/mcp/b",))
        assert allowed_stdio_commands([]) == ()
        assert allowed_stdio_commands([["/opt/mcp/c", "--safe"]]) == (("/opt/mcp/c", "--safe"),)

    def test_resolve_stdio_executable_raises_for_disallowed(self):
        with pytest.raises(PermissionError):
            resolve_stdio_executable(sys.executable, ())

    def test_stdio_env_allowlist_denies_by_default_and_blocks_launcher_overrides(
        self, monkeypatch
    ):
        # Empty allowlist denies any env var, including launcher overrides.
        assert check_stdio_env(["DOCS_API_KEY"], ()) is not None
        assert check_stdio_env(["UV_INDEX_URL"], ()) is not None
        assert check_stdio_env(["npm_config_registry"], ()) is not None
        assert check_stdio_env([], ()) is None

        allowed = allowed_stdio_env(["DOCS_API_KEY"])
        assert check_stdio_env(["DOCS_API_KEY"], allowed) is None
        assert check_stdio_env(["UV_INDEX_URL"], allowed) is not None
        assert check_stdio_env(["npm_config_registry"], allowed) is not None
        assert check_stdio_env(["LD_PRELOAD"], allowed) is not None

        monkeypatch.setenv(STDIO_ENV_VARS_ENV, "DOCS_API_KEY, EXTRA_KEY")
        assert allowed_stdio_env(None) == frozenset({"DOCS_API_KEY", "EXTRA_KEY"})
        assert allowed_stdio_env([]) == frozenset()


class TestSecretsResolutionAndOrigin:
    def test_url_origin_normalizes_default_ports_and_case(self):
        assert url_origin("HTTPS://Example.COM/mcp") == ("https", "example.com", 443)
        assert url_origin("https://example.com:443/v2") == ("https", "example.com", 443)
        assert url_origin("http://example.com/mcp") == ("http", "example.com", 80)
        assert url_origin("https://example.com:8443/mcp") == ("https", "example.com", 8443)
        assert url_origin("file:///etc/passwd") is None

    def test_http_headers_and_bearer_token(self, monkeypatch):
        monkeypatch.setenv("FLOWDASH_MCP_TOKEN", TOKEN_SECRET)
        cfg = MCPServerConfig(
            name="gh",
            transport="http",
            url="https://example.com/mcp",
            token="${FLOWDASH_MCP_TOKEN}",
            headers={"X-Custom": encrypt_secret(HEADER_SECRET)},
        )
        headers = resolve_http_headers(cfg)
        assert headers == {
            "X-Custom": HEADER_SECRET,
            "Authorization": f"Bearer {TOKEN_SECRET}",
        }

    def test_http_headers_reject_disallowed_env_ref_for_file_server(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        cfg = MCPServerConfig(
            name="gh",
            transport="http",
            url="https://attacker.example/mcp",
            token="${OPENAI_API_KEY}",
        )
        with pytest.raises(SecretError, match="not allowed"):
            resolve_http_headers(cfg)

    def test_explicit_authorization_header_wins_over_token(self, monkeypatch):
        monkeypatch.setenv("FLOWDASH_MCP_TOKEN", TOKEN_SECRET)
        cfg = MCPServerConfig(
            name="gh",
            transport="http",
            url="https://example.com/mcp",
            token="${FLOWDASH_MCP_TOKEN}",
            headers={"Authorization": encrypt_secret("Token custom-auth")},
        )
        headers = resolve_http_headers(cfg)
        assert headers == {"Authorization": "Token custom-auth"}

    def test_stdio_env_resolution_enforces_allowlists(self, monkeypatch):
        monkeypatch.setenv("FLOWDASH_MCP_DOCS_KEY", "docs-secret-1234")
        cfg = MCPServerConfig(
            name="local",
            transport="stdio",
            command="/opt/mcp/docs",
            env={"DOCS_API_KEY": "${FLOWDASH_MCP_DOCS_KEY}"},
        )
        assert resolve_stdio_env(cfg, allowed_env=["DOCS_API_KEY"]) == {
            "DOCS_API_KEY": "docs-secret-1234"
        }
        with pytest.raises(PermissionError, match="not in the allowed list"):
            resolve_stdio_env(cfg, allowed_env=["OTHER_KEY"])
        cfg_missing = MCPServerConfig(
            name="local",
            transport="stdio",
            command="/opt/mcp/docs",
            env={"DOCS_API_KEY": "${FLOWDASH_MCP_MISSING}"},
        )
        with pytest.raises(SecretError, match="FLOWDASH_MCP_MISSING"):
            resolve_stdio_env(cfg_missing, allowed_env=["DOCS_API_KEY"])


class TestMCPAccessEvaluation:
    def test_precedence_rules_and_access_to_dict(self):
        perm = Permission.from_spec(allow_groups=["eng"], deny_users=["bob"])
        alice = Identity(user="alice", groups=frozenset({"eng"}))
        bob_admin = Identity(user="bob", groups=frozenset({"eng", "admins"}))
        carol_admin = Identity(user="carol", groups=frozenset({"admins"}))
        dave = Identity(user="dave", groups=frozenset({"sales"}))

        assert evaluate_permission(perm, alice) is True
        assert evaluate_permission(perm, dave) is False
        assert evaluate_permission(perm, None) is False
        assert evaluate_permission(perm, carol_admin, is_admin=True) is True
        # Explicit deny wins even for admins.
        assert evaluate_permission(perm, bob_admin, is_admin=True) is False
        # Empty permission allows everyone.
        assert evaluate_permission(Permission(), None) is True
        # access_to_dict omits empty fields and dashboard edit fields.
        full_perm = Permission.from_spec(allow_groups=["eng"], allow_edit_users=["alice"])
        assert access_to_dict(full_perm) == {"allow_groups": ["eng"]}

    def test_lockout_reason(self):
        admin = Identity(user="alice", groups=frozenset({"admins"}))
        assert permission_lockout_reason(Permission.from_spec(allow_groups=["eng"]), admin) is None
        reason = permission_lockout_reason(Permission.from_spec(deny_users=["alice"]), admin)
        assert reason is not None and "deny_users" in reason
        reason_grp = permission_lockout_reason(Permission.from_spec(deny_groups=["admins"]), admin)
        assert reason_grp is not None and "deny_groups" in reason_grp

    def test_server_and_tool_intersection(self):
        server = MCPServerConfig(
            name="github",
            transport="http",
            url="https://example.com/mcp",
            access=Permission.from_spec(allow_groups=["eng"]),
            tool_access={"delete_*": Permission.from_spec(allow_groups=["maintainers"])},
        )
        eng = Identity(user="alice", groups=frozenset({"eng"}))
        maintainer = Identity(user="eve", groups=frozenset({"eng", "maintainers"}))
        sales = Identity(user="dave", groups=frozenset({"sales"}))

        assert mcp_server_authorized(server, eng) is True
        assert mcp_server_authorized(server, sales) is False
        assert mcp_tool_authorized(server, "list_issues", eng) is True
        assert mcp_tool_authorized(server, "delete_issue", eng) is False
        assert mcp_tool_authorized(server, "delete_issue", maintainer) is True
        assert mcp_tool_authorized(server, "delete_issue", sales) is False


class TestPermissionEditor:
    def test_name_helpers_and_known_principals(self, sections, tmp_path):
        assert parse_name_list("alice, bob\nalice,  ") == ["alice", "bob"]
        assert format_name_list({"bob", "alice"}) == "alice, bob"
        app = _app(
            tmp_path,
            "permissions:\n  admin_groups: [ops]\n  people:\n    alice:\n      groups: [eng]\n",
        )
        users, groups = known_principals(app, app._settings_project())
        assert "alice" in users
        assert {"ops", "eng"}.issubset(set(groups))

    def test_editor_hooks_save_clear_and_dialog(self):
        saved: list[Permission] = []
        admin = Identity(user="alice", groups=frozenset({"admins"}))

        def effective_html(perm: Permission) -> str:
            return f"<span>rules:{len(perm.allow_groups)}</span>"

        def on_save(perm: Permission):
            reason = permission_lockout_reason(perm, admin)
            if reason:
                return [Problem(reason)]
            saved.append(perm)
            return []

        editor = PermissionEditor(
            Permission.from_spec(allow_groups=["eng"]),
            effective_display=effective_html,
            lockout_check=lambda p: permission_lockout_reason(p, admin),
            on_save=on_save,
        )
        assert editor.permission() == Permission.from_spec(allow_groups=["eng"])
        assert "rules:1" in editor._effective_cell.object
        assert editor._lockout_warn.visible is False

        # Live lockout warning when admin denies themselves.
        editor.deny_users_input.value = "alice"
        assert editor._lockout_warn.visible is True
        assert editor.save() is False
        assert editor._alert.visible is True
        assert saved == []

        # Clear removes all fields and succeeds on save.
        editor.clear()
        assert editor.permission().is_empty
        assert editor._lockout_warn.visible is False
        assert editor.save() is True
        assert saved == [Permission()]

        # PermissionDialog opens, saves, and closes on success.
        dialog = PermissionDialog(lockout_check=lambda p: permission_lockout_reason(p, admin))
        dialog.open("Edit source", Permission.from_spec(allow_users=["bob"]), on_save)
        assert dialog.is_open is True
        assert dialog.editor.save() is True
        assert dialog.is_open is False
        assert saved[-1] == Permission.from_spec(allow_users=["bob"])


class TestMCPSettingsAndHostHooks:
    def test_admin_saves_encrypted_token_and_never_leaks_secret(self, sections, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        app = _app(tmp_path)
        view = app._admin_view(f"{ADMIN_ROUTE}/mcp").view("mcp")
        assert isinstance(view, MCPSettings)

        view.open_server(None)
        view._name_input.value = "github"
        view._url.value = "https://api.githubcopilot.com/mcp/"
        view._description.value = "GitHub"
        view._token_mode.value = "encrypted"
        view._token_secret.value = TOKEN_SECRET
        view._auto_pattern.value = "list_*"
        view._add_auto_pattern()
        view._allow_groups.value = "engineering"
        assert view.save_server() is True

        raw_yaml = (tmp_path / PROJECT_FILE).read_text()
        assert TOKEN_SECRET not in raw_yaml
        stored_mcp = load_project(tmp_path / PROJECT_FILE).get("mcp")
        token_entry = stored_mcp["servers"]["github"]["token"]
        assert isinstance(token_entry, dict) and resolve_secret(token_entry) == TOKEN_SECRET
        assert stored_mcp["servers"]["github"]["auto_approve"] == ["list_*"]
        assert stored_mcp["servers"]["github"]["access"] == {"allow_groups": ["engineering"]}

        # Secret must not appear in Bokeh document JSON or logs.
        assert TOKEN_SECRET not in _document_text(view)
        assert TOKEN_SECRET not in caplog.text
        assert view._token_secret.value == ""

        # Re-opening and changing only the path (same origin) preserves the encrypted token.
        view.open_server("github")
        view._url.value = "https://api.githubcopilot.com/mcp/v2"
        view._description.value = "GitHub updated"
        assert view.save_server() is True
        stored_again = load_project(tmp_path / PROJECT_FILE).get("mcp")
        assert resolve_secret(stored_again["servers"]["github"]["token"]) == TOKEN_SECRET
        assert stored_again["servers"]["github"]["description"] == "GitHub updated"

    @pytest.mark.parametrize(
        "new_url",
        [
            "https://attacker.example/mcp",  # host change
            "http://api.example.com/mcp",  # scheme change
            "https://api.example.com:8443/mcp",  # port change
        ],
    )
    def test_url_origin_change_drops_stored_token_and_headers_unless_reentered(
        self, sections, tmp_path, new_url
    ):
        app = _app(tmp_path)
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        view.open_server(None)
        view._name_input.value = "srv"
        view._url.value = "https://api.example.com/mcp"
        view._token_mode.value = "encrypted"
        view._token_secret.value = TOKEN_SECRET
        view._header_name.value = "X-Api-Key"
        view._header_use_env.value = False
        view._header_secret.value = HEADER_SECRET
        view._add_secret_entry("headers")
        assert view.save_server() is True

        # Editing the URL to a different scheme/host/port drops un-reentered credentials.
        view.open_server("srv")
        view._url.value = new_url
        assert view._url_origin_warn.visible is True
        assert view.save_server() is True
        saved = load_project(tmp_path / PROJECT_FILE).get("mcp")["servers"]["srv"]
        assert saved["url"] == new_url
        assert "token" not in saved
        assert "headers" not in saved

    def test_url_origin_change_keeps_only_reentered_credentials(self, sections, tmp_path):
        app = _app(tmp_path)
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        view.open_server(None)
        view._name_input.value = "srv"
        view._url.value = "https://old.example.com/mcp"
        view._token_mode.value = "encrypted"
        view._token_secret.value = TOKEN_SECRET
        view._header_name.value = "X-Old"
        view._header_use_env.value = False
        view._header_secret.value = HEADER_SECRET
        view._add_secret_entry("headers")
        assert view.save_server() is True

        view.open_server("srv")
        view._url.value = "https://new.example.com/mcp"
        view._token_secret.value = "new-secret-token-12345"
        view._header_name.value = "X-New"
        view._header_use_env.value = False
        view._header_secret.value = "new-header-value-12345"
        view._add_secret_entry("headers")
        assert view.save_server() is True
        saved = load_project(tmp_path / PROJECT_FILE).get("mcp")["servers"]["srv"]
        assert resolve_secret(saved["token"]) == "new-secret-token-12345"
        assert set(saved["headers"]) == {"X-New"}
        assert resolve_secret(saved["headers"]["X-New"]) == "new-header-value-12345"

    def test_ui_rejects_disallowed_env_var_reference(self, sections, tmp_path):
        app = _app(tmp_path)
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        view.open_server(None)
        view._name_input.value = "exfil"
        view._url.value = "https://attacker.example/mcp"
        view._token_mode.value = "env"
        view._token_env.value = "OPENAI_API_KEY"
        assert view.save_server() is False
        assert "not allowed" in view._drawer_alert.object

    def test_stdio_prefix_and_env_allowlists_enforced_in_ui(self, sections, tmp_path):
        app = _app(tmp_path, mcp_stdio_commands=[])
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        view.open_server(None)
        view._name_input.value = "local"
        view._transport.value = "stdio"
        view._command.value = "/opt/mcp/docs-server"
        assert view.save_server() is False
        assert "disabled by server policy" in view._drawer_alert.object

        app_allowed = _app(
            tmp_path,
            mcp_stdio_commands=["/opt/mcp/docs-server --readonly"],
            mcp_stdio_env=["DOCS_API_KEY"],
        )
        view2 = MCPSettings(app_allowed._settings_project(), editable=True, app=app_allowed)
        view2.open_server(None)
        view2._name_input.value = "local"
        view2._transport.value = "stdio"
        view2._command.value = "/opt/mcp/docs-server"
        # Missing required prefix arg --readonly is refused.
        view2._args.value = "--write"
        assert view2.save_server() is False
        assert "does not match" in view2._drawer_alert.object

        # Disallowed env var name is refused when adding.
        view2._env_name.value = "UV_INDEX_URL"
        view2._env_ref.value = "FLOWDASH_MCP_INDEX"
        view2._add_secret_entry("env")
        assert "not in the allowed list" in view2._drawer_alert.object

        # Matching prefix + trailing args + allowlisted env var succeeds.
        view2._args.value = "--readonly --timeout 10"
        view2._env_name.value = "DOCS_API_KEY"
        view2._env_ref.value = "FLOWDASH_MCP_DOCS_KEY"
        view2._add_secret_entry("env")
        assert view2.save_server() is True
        saved = load_project(tmp_path / PROJECT_FILE).get("mcp")["servers"]["local"]
        assert saved["command"] == "/opt/mcp/docs-server"
        assert saved["args"] == ["--readonly", "--timeout", "10"]
        assert saved["env"] == {"DOCS_API_KEY": "${FLOWDASH_MCP_DOCS_KEY}"}

    def test_mcp_checks_are_isolated_per_project(self, sections, tmp_path):
        dir_a = tmp_path / "proj_a"
        dir_b = tmp_path / "proj_b"
        dir_a.mkdir()
        dir_b.mkdir()
        yaml_text = "mcp:\n  servers:\n    github:\n      transport: http\n      url: https://example.com/mcp\n"
        app_a = _app(dir_a, yaml_text)
        app_b = _app(dir_b, yaml_text)
        proj_a = app_a._settings_project()
        proj_b = app_b._settings_project()

        set_mcp_check(
            "github",
            MCPServerCheck(True, "Connected (1 tool)", tools=(MCPToolInfo("tool_from_a"),)),
            project=proj_a,
        )
        assert get_mcp_check("github", project=proj_a) is not None
        assert get_mcp_check("github", project=proj_b) is None

        view_b = MCPSettings(proj_b, editable=False, app=app_b)
        assert "tool_from_a" not in _document_text(view_b)

    def test_toggle_missing_server_refreshes_cards(self, sections, tmp_path):
        app = _app(
            tmp_path,
            "mcp:\n  servers:\n    gh:\n      transport: http\n      url: https://example.com/mcp\n",
        )
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        # Remove the server directly from the file behind the UI's back.
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{ME}]\n")
        assert view.toggle_server("gh", False) is False
        assert "No MCP servers configured yet" in _document_text(view)

    def test_live_admin_recheck_blocks_revoked_admin(self, sections, tmp_path):
        app = _app(
            tmp_path,
            "mcp:\n  servers:\n    gh:\n      transport: http\n      url: https://example.com/mcp\n",
        )
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        # Revoke admin rights in flowdash.yaml while page is still open.
        (tmp_path / PROJECT_FILE).write_text(
            "general:\n  admins: [someone_else]\n"
            "mcp:\n  servers:\n    gh:\n      transport: http\n      url: https://example.com/mcp\n"
        )
        assert view.toggle_server("gh", False) is False
        assert view.remove_server("gh") is False
        view.open_server("gh")
        view._description.value = "hacked"
        assert view.save_server() is False
        assert "no longer an admin" in view._drawer_alert.object

    def test_read_only_mode_hides_controls_secrets_and_unauthorized_tools(
        self, sections, tmp_path
    ):
        encrypted = encrypt_secret(TOKEN_SECRET)
        app = _app(
            tmp_path,
            "mcp:\n"
            "  servers:\n"
            "    public:\n"
            "      transport: http\n"
            "      url: https://secret-host.internal/mcp\n"
            f"      token: {{encrypted: {encrypted['encrypted']!r}, key_id: {encrypted['key_id']!r}}}\n"
            "      description: Public integration\n"
            "      tool_access:\n"
            "        secret_tool:\n"
            "          allow_groups: [admins]\n"
            "    restricted_corp_mcp:\n"
            "      transport: http\n"
            "      url: https://private.internal/mcp\n"
            "      access:\n"
            "        allow_groups: [executives]\n",
        )
        app._is_admin = False
        app._identity = Identity(user="viewer", groups=frozenset({"readers"}))
        project = app._settings_project()
        set_mcp_check(
            "public",
            MCPServerCheck(
                True,
                "Connected (2 tools)",
                tools=(
                    MCPToolInfo("public_tool", "Safe read"),
                    MCPToolInfo("secret_tool", "Admin only"),
                ),
            ),
            project=project,
        )
        view = MCPSettings(project, editable=False, app=app)
        doc_text = _document_text(view)
        assert "Public integration" in doc_text
        assert "public_tool" in doc_text
        # Unauthorized server, unauthorized tool, URL, token, and admin buttons are hidden.
        assert "restricted_corp_mcp" not in doc_text, "unauthorized server leaked"
        assert "secret_tool" not in doc_text, "unauthorized tool leaked"
        assert "secret-host.internal" not in doc_text, "server URL leaked"
        assert TOKEN_SECRET not in doc_text, "token leaked"
        assert "Add server" not in doc_text, "admin button leaked"
        assert view._drawer is None

    def test_host_hooks_show_add_show_access_server_extra_and_on_change(self, sections, tmp_path):
        changed = []
        extra_seen = []
        app = _app(
            tmp_path,
            "mcp:\n"
            "  servers:\n"
            "    gh:\n"
            "      transport: http\n"
            "      url: https://example.com/mcp\n"
            "      access:\n"
            "        allow_groups: [engineering]\n",
        )

        def server_extra(server: MCPServerConfig, editable: bool):
            extra_seen.append((server.name, editable))
            return pn.pane.Markdown(f"Host badge for {server.name}")

        view = MCPSettings(
            app._settings_project(),
            editable=True,
            app=app,
            show_add=False,
            show_access=False,
            server_extra=server_extra,
            on_change=lambda: changed.append(True),
        )
        assert extra_seen == [("gh", True)]
        assert "Host badge for gh" in _document_text(view)
        assert view._add_button.visible is False
        assert view._access_box.visible is False

        # Editing with show_access=False preserves existing access rules.
        view.open_server("gh")
        view._description.value = "Updated by host"
        assert view.save_server() is True
        assert changed == [True]
        saved = load_project(tmp_path / PROJECT_FILE).get("mcp")["servers"]["gh"]
        assert saved["access"] == {"allow_groups": ["engineering"]}
        assert saved["description"] == "Updated by host"

    def test_admin_hidden_sections_and_redirects(self, sections, tmp_path):
        app = _app(tmp_path, admin_section_redirects={"mcp": "/data/integrations"})
        admin_page = app._admin_view(ADMIN_ROUTE)
        assert "mcp" not in admin_page.sections

        app2 = _app(tmp_path, admin_hidden_sections=["mcp"])
        admin_page2 = app2._admin_view(ADMIN_ROUTE)
        assert "mcp" not in admin_page2.sections

    def test_code_defined_servers_merged_and_locked(self, sections, tmp_path):
        app = _app(
            tmp_path,
            "mcp:\n  servers:\n    file_srv:\n      transport: http\n      url: https://b.example/mcp\n",
            mcp_servers={"code_srv": {"transport": "http", "url": "https://a.example/mcp"}},
        )
        servers = configured_servers(app._settings_project(), app)
        assert list(servers) == ["code_srv", "file_srv"]
        assert servers["code_srv"].locked is not None
        assert servers["file_srv"].locked is None
