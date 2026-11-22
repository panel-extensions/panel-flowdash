"""UI tests for the admin view at /admin."""

from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig
from panel_flowdash.project import PROJECT_FILE, load_project

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _serve(page, tmp_path, suffix="/admin", **params):
    store = MemoryDashboardStore()

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


def test_admin_rail_item_opens_the_admin_view_on_this_computer(page, tmp_path):
    _serve(page, tmp_path, suffix="")
    admin = _rail(page, "Admin")
    expect(admin).to_be_visible()
    help_box = _rail(page, "Help").bounding_box()
    assert admin.bounding_box()["y"] < help_box["y"]

    admin.click()
    wait_until(lambda: urlparse(page.url).path == "/admin", page)
    expect(page.locator(".flowdash-admin-content")).to_contain_text("General")
    expect(page.get_by_text("On this computer as", exact=False)).to_be_visible()

    page.locator(".flowdash-admin-nav").get_by_role("button", name="Theme").click()
    wait_until(lambda: urlparse(page.url).path == "/admin/theme", page)
    expect(page.locator(".flowdash-admin-content")).to_contain_text("Colour scheme")
    expect(page.locator(".flowdash-theme-preview")).to_be_visible()


def test_no_admin_item_without_admin_rights(page, tmp_path):
    _serve(page, tmp_path, suffix="/admin", local_admin=False, auth_config=AuthConfig())
    expect(page.get_by_text("You don't have access to Admin")).to_be_visible()
    expect(_rail(page, "Help")).to_be_visible()
    expect(_rail(page, "Admin")).to_have_count(0)


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
    assert not (tmp_path / PROJECT_FILE).exists()

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
    assert load_project(tmp_path / PROJECT_FILE).get("general") == {"tile_titles": True}
