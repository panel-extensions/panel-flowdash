"""Playwright UI tests for MCP server administration at /admin/mcp and embedded MCPSettings."""

from __future__ import annotations

import getpass
import logging
from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.admin.mcp import MCPSettings
from panel_flowdash.app import FlowDashApp
from panel_flowdash.mcp import MCP_CHECKS, MCPServerCheck, MCPToolInfo
from panel_flowdash.project import PROJECT_FILE, load_project
from panel_flowdash.secret_store import KEY_ENV, fernet_key, resolve_secret

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

SECRET = "mcp-secret-token-typed-in-browser-987654321"


def _serve(page, tmp_path, suffix="/admin/mcp", admin=True, **params):
    store = MemoryDashboardStore()
    if admin and not (tmp_path / PROJECT_FILE).exists():
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{getpass.getuser()}]\n")

    def factory():
        return FlowDashApp(
            registry={},
            project_dir=tmp_path,
            store=store,
            notifications=False,
            include_builtin_components=False,
            **params,
        )

    routes = {
        "/": factory,
        "/components": factory,
        "/admin": factory,
        "/admin/[^/]+": factory,
        "/dash/[^/]+": factory,
    }
    serve_component(page, routes, suffix=suffix)


def test_admin_adds_tests_and_removes_mcp_server_with_encrypted_token(
    page, tmp_path, monkeypatch, caplog
):
    pytest.importorskip("cryptography")
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(KEY_ENV, fernet_key())
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))
    MCP_CHECKS.clear()

    async def fake_probe(config, *, allowed_stdio=(), directory=None):
        return MCPServerCheck(
            True,
            "Connected (2 tools)",
            tools=(
                MCPToolInfo("list_issues", "List repository issues"),
                MCPToolInfo("create_issue", "Create a new issue"),
            ),
        )

    monkeypatch.setattr("panel_flowdash.agent.mcp.probe_mcp_server", fake_probe)

    _serve(page, tmp_path, suffix="/admin/mcp")
    expect(page.locator(".flowdash-admin-content")).to_contain_text("MCP")

    page.locator(".flowdash-mcp-add").click()
    drawer = page.locator(".flowdash-admin-mcp-drawer")
    expect(drawer).to_be_visible()

    drawer.get_by_label("Server id").fill("github")
    drawer.get_by_label("Server URL").fill("https://api.githubcopilot.com/mcp/")
    drawer.get_by_label("Description").fill("GitHub MCP")
    drawer.get_by_role("button", name="Encrypted secret").click()
    drawer.get_by_label("Bearer token").fill(SECRET)
    drawer.get_by_placeholder("Add tool name or glob, e.g. list_*").fill("list_issues")
    drawer.get_by_role("button", name="Add pattern").click()
    drawer.locator(".flowdash-mcp-save").click()

    def stored_server():
        mcp = load_project(tmp_path / PROJECT_FILE).get("mcp") or {}
        return (mcp.get("servers") or {}).get("github")

    wait_until(lambda: stored_server() is not None, page)
    saved = stored_server()
    assert isinstance(saved["token"], dict)
    assert resolve_secret(saved["token"]) == SECRET
    assert saved["auto_approve"] == ["list_issues"]
    assert SECRET not in (tmp_path / PROJECT_FILE).read_text()

    card = page.locator(".flowdash-mcp-card-github")
    expect(card).to_be_visible()
    expect(card).to_contain_text("GitHub MCP")
    expect(card).to_contain_text(f"ends in {SECRET[-4:]}")
    assert SECRET not in page.content()
    assert SECRET not in caplog.text

    # Test connection and verify discovered tools appear with auto-approve badge.
    card.locator(".flowdash-mcp-test").click()
    expect(card).to_contain_text("Connected")
    expect(card).to_contain_text("list_issues · auto")
    expect(card).to_contain_text("create_issue")

    # Edit and remove server.
    card.locator(".flowdash-mcp-edit").click()
    expect(drawer).to_be_visible()
    drawer.locator(".flowdash-mcp-remove").click()
    wait_until(lambda: stored_server() is None, page)
    expect(page.locator(".flowdash-mcp-card-github")).to_have_count(0)


def test_admin_section_redirect_hides_mcp_and_redirects(page, tmp_path):
    _serve(
        page,
        tmp_path,
        suffix="/admin/general",
        admin_section_redirects={"mcp": "/components"},
    )
    nav = page.locator(".flowdash-admin-nav")
    expect(nav).to_be_visible()
    expect(nav.get_by_role("button", name="MCP")).to_have_count(0)

    _serve(
        page,
        tmp_path,
        suffix="/admin/mcp",
        admin_section_redirects={"mcp": "/components"},
    )
    wait_until(lambda: urlparse(page.url).path == "/components", page)


def test_embedded_readonly_mcp_settings_hides_admin_controls(page, tmp_path):
    MCP_CHECKS.clear()
    (tmp_path / PROJECT_FILE).write_text(
        "general:\n  admins: [someone_else]\n"
        "mcp:\n"
        "  servers:\n"
        "    docs:\n"
        "      transport: http\n"
        "      url: https://docs.internal/mcp\n"
        "      description: Documentation search\n"
    )
    MCP_CHECKS["docs"] = MCPServerCheck(
        True, "Connected (1 tool)", tools=(MCPToolInfo("search_docs", "Search docs"),)
    )

    def factory():
        project = load_project(tmp_path / PROJECT_FILE)
        return MCPSettings(project, editable=False)

    serve_component(page, factory)
    card = page.locator(".flowdash-mcp-card-docs")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Documentation search")
    expect(card).to_contain_text("search_docs")
    expect(page.locator(".flowdash-mcp-add")).to_have_count(0)
    expect(card.locator(".flowdash-mcp-edit")).to_have_count(0)
    expect(card.locator(".flowdash-mcp-test")).to_have_count(0)
    assert "docs.internal" not in page.content()
