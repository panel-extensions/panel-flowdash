"""Unit tests for MCP configuration, secrets, stdio policy, access rules and MCPSettings UI."""

from __future__ import annotations

import getpass
import json
import logging
import sys

import panel as pn
import pytest
from bokeh.document import Document

from panel_flowdash import project as project_module
from panel_flowdash.admin import ADMIN_ROUTE, MCPSettings
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity, Permission
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.mcp import (
    MCP_CHECKS,
    STDIO_COMMANDS_ENV,
    MCPServerCheck,
    MCPServerConfig,
    MCPToolInfo,
    allowed_stdio_commands,
    check_stdio_command,
    configured_servers,
    dump_mcp,
    evaluate_permission,
    is_mcp_tool,
    mcp_server_authorized,
    mcp_tool_authorized,
    mcp_tool_name,
    parse_mcp_tool_name,
    resolve_http_headers,
    resolve_stdio_env,
    resolve_stdio_executable,
    validate_server_name,
)
from panel_flowdash.project import PROJECT_FILE, ProjectError, load_project, parse_project
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
    """Encrypt with a per-test key and isolate MCP_CHECKS and stdio env vars."""
    monkeypatch.setenv(KEY_ENV, fernet_key())
    monkeypatch.setenv("FLOWDASH_SECRET_KEY_FILE", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))
    monkeypatch.delenv(STDIO_COMMANDS_ENV, raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
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


class TestMCPProjectSchema:
    def test_loads_valid_http_and_stdio_servers(self, sections, tmp_path):
        (tmp_path / "docs").mkdir()
        text = """\
mcp:
  servers:
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp/
      token: ${GITHUB_TOKEN}
      headers:
        X-Api-Key: ${EXTRA_KEY}
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
      command: uvx
      args: ["mcp-server-fetch"]
      cwd: docs
      env:
        DOCS_KEY: ${DOCS_KEY}
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
        assert servers["local_docs"].args == ("mcp-server-fetch",)

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
      command: uvx
      env:
        API_KEY: raw-env-secret
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "github", "token") in keys
        assert ("servers", "github", "headers", "X-Api-Key") in keys
        assert ("servers", "local", "env", "API_KEY") in keys

    def test_rejects_cwd_escape_and_blocked_env_vars(self, sections, tmp_path):
        text = """\
mcp:
  servers:
    local:
      transport: stdio
      command: uvx
      cwd: ../outside
      env:
        LD_PRELOAD: ${BAD_LIB}
        PYTHONPATH: ${BAD_PY}
"""
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        keys = {p.keys for p in info.value.problems}
        assert ("servers", "local", "cwd") in keys
        assert ("servers", "local", "env", "LD_PRELOAD") in keys
        assert ("servers", "local", "env", "PYTHONPATH") in keys

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


class TestStdioCommandPolicy:
    def test_empty_allowlist_denies_everything(self):
        err = check_stdio_command("uvx", ())
        assert err is not None and "disabled by server policy" in err

    def test_bare_command_matches_exact_name_only(self):
        assert check_stdio_command("uvx", ("uvx", "npx")) is None
        assert check_stdio_command("bash", ("uvx", "npx")) is not None
        assert check_stdio_command("/usr/bin/uvx", ("uvx",)) is not None
        assert check_stdio_command("./uvx", ("uvx",)) is not None

    def test_absolute_path_matches_resolved_executable(self, tmp_path):
        exe = tmp_path / "my-mcp"
        exe.write_text("#!/bin/sh\n")
        assert check_stdio_command(str(exe), (str(exe),)) is None
        assert check_stdio_command("my-mcp", (str(exe),)) is not None
        assert resolve_stdio_executable(str(exe), (str(exe),)) == str(exe.resolve())

    def test_env_var_fallback_and_explicit_override(self, monkeypatch):
        monkeypatch.setenv(STDIO_COMMANDS_ENV, "uvx, npx")
        assert allowed_stdio_commands(None) == ("uvx", "npx")
        assert allowed_stdio_commands([]) == ()
        assert allowed_stdio_commands(["python"]) == ("python",)

    def test_resolve_stdio_executable_raises_for_disallowed(self):
        with pytest.raises(PermissionError):
            resolve_stdio_executable(sys.executable, ())


class TestSecretsResolution:
    def test_http_headers_and_bearer_token(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", TOKEN_SECRET)
        cfg = MCPServerConfig(
            name="gh",
            transport="http",
            url="https://example.com/mcp",
            token="${MY_TOKEN}",
            headers={"X-Custom": encrypt_secret(HEADER_SECRET)},
        )
        headers = resolve_http_headers(cfg)
        assert headers == {
            "X-Custom": HEADER_SECRET,
            "Authorization": f"Bearer {TOKEN_SECRET}",
        }

    def test_explicit_authorization_header_wins_over_token(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", TOKEN_SECRET)
        cfg = MCPServerConfig(
            name="gh",
            transport="http",
            url="https://example.com/mcp",
            token="${MY_TOKEN}",
            headers={"Authorization": encrypt_secret("Token custom-auth")},
        )
        headers = resolve_http_headers(cfg)
        assert headers == {"Authorization": "Token custom-auth"}

    def test_stdio_env_resolution_and_missing_error(self):
        cfg = MCPServerConfig(
            name="local",
            transport="stdio",
            command="uvx",
            env={"API_KEY": "${MISSING_VAR}"},
        )
        with pytest.raises(SecretError, match="MISSING_VAR"):
            resolve_stdio_env(cfg)


class TestMCPAccessEvaluation:
    def test_precedence_rules(self):
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

        # Re-opening and saving without typing a new token preserves the encrypted token.
        view.open_server("github")
        view._description.value = "GitHub updated"
        assert view.save_server() is True
        stored_again = load_project(tmp_path / PROJECT_FILE).get("mcp")
        assert resolve_secret(stored_again["servers"]["github"]["token"]) == TOKEN_SECRET
        assert stored_again["servers"]["github"]["description"] == "GitHub updated"

    def test_stdio_blocked_without_allowlist_and_allowed_with_it(self, sections, tmp_path):
        app = _app(tmp_path, mcp_stdio_commands=[])
        view = MCPSettings(app._settings_project(), editable=True, app=app)
        view.open_server(None)
        view._name_input.value = "local"
        view._transport.value = "stdio"
        view._command.value = "uvx"
        assert view.save_server() is False
        assert "disabled by server policy" in view._drawer_alert.object

        app_allowed = _app(tmp_path, mcp_stdio_commands=["uvx"])
        view2 = MCPSettings(app_allowed._settings_project(), editable=True, app=app_allowed)
        view2.open_server(None)
        view2._name_input.value = "local"
        view2._transport.value = "stdio"
        view2._command.value = "uvx"
        view2._args.value = "mcp-server-fetch --timeout 10"
        assert view2.save_server() is True
        saved = load_project(tmp_path / PROJECT_FILE).get("mcp")["servers"]["local"]
        assert saved["command"] == "uvx"
        assert saved["args"] == ["mcp-server-fetch", "--timeout", "10"]

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
        MCP_CHECKS["public"] = MCPServerCheck(
            True,
            "Connected (2 tools)",
            tools=(
                MCPToolInfo("public_tool", "Safe read"),
                MCPToolInfo("secret_tool", "Admin only"),
            ),
        )
        view = MCPSettings(app._settings_project(), editable=False, app=app)
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
