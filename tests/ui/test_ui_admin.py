"""UI tests for the admin view at /admin."""

import getpass
import logging
from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.admin import access
from panel_flowdash.app import FlowDashApp
from panel_flowdash.project import PROJECT_FILE, load_project

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _serve(page, tmp_path, suffix="/admin", admin=True, **params):
    store = MemoryDashboardStore()
    if admin:
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


def _rail(page, label):
    return page.get_by_role("button", name=label, exact=True).or_(
        page.get_by_role("link", name=label, exact=True)
    )


def test_the_admin_link_claims_the_admin_view(page, tmp_path, monkeypatch):
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    token = access.admin_token()
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    _serve(page, tmp_path, suffix=f"/#token={token}", admin=False)

    admin = _rail(page, "Admin")
    expect(admin).to_be_visible()
    assert admin.bounding_box()["y"] < _rail(page, "Help").bounding_box()["y"]
    # The fragment is gone from the address bar and was never sent to the server.
    wait_until(lambda: "token" not in page.url, page)
    assert not [url for url in requests if token in url]
    user = getpass.getuser()
    assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": [user]}

    admin.click()
    wait_until(lambda: urlparse(page.url).path == "/admin", page)
    expect(page.locator(".flowdash-admin-content")).to_contain_text("General")
    expect(page.locator(".flowdash-setting-admins")).to_contain_text(user)
    assert "token" not in page.url

    page.locator(".flowdash-admin-nav").get_by_role("button", name="Theme").click()
    wait_until(lambda: urlparse(page.url).path == "/admin/theme", page)
    expect(page.locator(".flowdash-admin-content")).to_contain_text("Colour scheme")
    expect(page.locator(".flowdash-theme-preview")).to_be_visible()
    access.reset_admin_token()


def test_a_wrong_token_is_refused(page, tmp_path):
    _serve(page, tmp_path, suffix="/admin#token=wrong", admin=False)
    expect(page.get_by_text("You don't have access to Admin")).to_be_visible()
    expect(_rail(page, "Help")).to_be_visible()
    expect(_rail(page, "Admin")).to_have_count(0)
    assert not (tmp_path / PROJECT_FILE).exists()


def test_save_writes_the_project_file_and_shows_field_errors(page, tmp_path):
    _serve(page, tmp_path, suffix="/admin/theme")
    save = page.get_by_role("button", name="Save")
    expect(save).to_be_disabled()

    field = page.locator(".flowdash-setting-primary_color input")
    field.fill("teal")
    field.press("Enter")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    save.click()
    row = page.locator(".flowdash-setting-primary_color")
    expect(row.get_by_role("alert")).to_contain_text("is a hex colour")
    assert "theme" not in load_project(tmp_path / PROJECT_FILE)

    field.fill("#006D77")
    field.press("Enter")
    save.click()
    expect(page.get_by_text("All changes saved")).to_be_visible()
    expect(row.get_by_role("alert")).to_have_count(0)
    assert load_project(tmp_path / PROJECT_FILE).get("theme") == {"primary_color": "#006D77"}


def test_settings_set_in_code_are_locked(page, tmp_path):
    _serve(
        page,
        tmp_path,
        suffix="/admin/theme",
        title="Wind",
        settings_sources={"title": "__init__.py"},
    )
    row = page.locator(".flowdash-setting-title")
    expect(row).to_contain_text("title in __init__.py")
    expect(row.locator("input")).to_be_disabled()
    expect(row.locator("input")).to_have_value("Wind")


def test_leaving_with_unsaved_settings_asks_first(page, tmp_path):
    _serve(page, tmp_path, suffix="/admin/general")
    page.locator(".flowdash-setting-tile_titles .MuiSwitch-root").click()
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    _rail(page, "Components").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("General settings")
    dialog.get_by_role("button", name="Save and leave").click()
    wait_until(lambda: urlparse(page.url).path == "/components", page)
    assert load_project(tmp_path / PROJECT_FILE).get("general")["tile_titles"] is True


SECRET = "sk-or-v1-typed-in-the-browser-0123456789"


def test_a_key_typed_in_the_dialog_is_stored_encrypted_and_never_sent_back(
    page, tmp_path, monkeypatch, caplog
):
    from panel_flowdash.secret_store import KEY_ENV, fernet_key, resolve_secret

    pytest.importorskip("cryptography")
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(KEY_ENV, fernet_key())
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    sent = []

    async def fetch(url, headers):
        # The key is tested after saving; answer for OpenRouter rather than calling it.
        sent.append(headers.get("Authorization"))
        return 200, "OK"

    monkeypatch.setattr("panel_flowdash.ai._fetch", fetch)
    (tmp_path / PROJECT_FILE).write_text(
        f"general:\n  admins: [{getpass.getuser()}]\n"
        "ai:\n  models:\n    - id: openrouter:ibm/granite\n      label: Granite\n"
    )
    _serve(page, tmp_path, suffix="/admin/ai", admin=False)
    card = page.locator(".flowdash-admin-provider-openrouter")
    expect(card).to_contain_text("Not set up")
    card.get_by_role("button", name="Set up").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Replace the OpenRouter key")
    dialog.get_by_text("Read the key from an environment variable").click()
    dialog.get_by_label("API key").fill(SECRET)
    dialog.get_by_role("button", name="Save key").click()
    expect(dialog).to_be_hidden()

    def stored():
        ai = load_project(tmp_path / PROJECT_FILE).get("ai") or {}
        return (ai.get("providers") or {}).get("openrouter", {}).get("api_key")

    wait_until(lambda: isinstance(stored(), dict), page)
    assert resolve_secret(stored()) == SECRET
    assert SECRET not in (tmp_path / PROJECT_FILE).read_text()
    expect(card).to_contain_text(f"ends in {SECRET[-4:]}")
    expect(card).to_contain_text("Connected")
    assert sent == [f"Bearer {SECRET}"]
    assert SECRET not in page.content()
    card.get_by_role("button", name="Replace key").click()
    expect(dialog.get_by_label("API key")).to_have_value("")
    assert SECRET not in caplog.text


def test_an_approval_rule_added_on_the_agent_tab_is_saved(page, tmp_path):
    _serve(page, tmp_path, suffix="/admin/permissions")
    page.get_by_role("tab", name="Agent").click()
    rules = page.locator(".flowdash-admin-rules")
    expect(rules).to_contain_text("save_dashboard")
    page.get_by_placeholder("Tool, e.g. run_sql").fill("run_sql")
    page.get_by_role("button", name="Add rule").click()
    expect(rules).to_contain_text("run_sql")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("All changes saved")).to_be_visible()
    permissions = load_project(tmp_path / PROJECT_FILE).get("permissions")
    assert permissions == {"approval_rules": [{"tool": "run_sql"}]}


def test_a_project_file_whose_permissions_cannot_be_read_denies_access(page, tmp_path):
    (tmp_path / PROJECT_FILE).write_text("permissions:\n  default_allow: false\n<<<<<<< HEAD\n")
    _serve(page, tmp_path, suffix="/", admin=False)
    expect(page.get_by_text("Access is denied.")).to_be_visible()
    expect(page.get_by_text("cannot be read")).to_be_visible()
