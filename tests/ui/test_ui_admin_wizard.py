"""UI tests of the setup wizard at /admin/setup."""

import getpass
from urllib.parse import urlparse

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore, project as project_module
from panel_flowdash.admin import WizardStep, access
from panel_flowdash.app import FlowDashApp
from panel_flowdash.project import PROJECT_FILE, ProjectSection, load_project

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

SECRET = "sk-wizard-ui-secret-0123456789abcdef"


@pytest.fixture
def sections(monkeypatch):
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


def _serve(page, tmp_path, suffix="/admin/setup", admin=True):
    if admin:
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{getpass.getuser()}]\n")

    def factory():
        return FlowDashApp(
            registry={},
            project_dir=tmp_path,
            store=MemoryDashboardStore(),
            notifications=False,
            include_builtin_components=False,
        )

    routes = {"/": factory, "/admin": factory, "/admin/[^/]+": factory}
    serve_component(page, routes, suffix=suffix)


def _path(page):
    return urlparse(page.url).path


def _footer(page, name):
    return page.locator(".flowdash-wizard-footer").get_by_role("button", name=name)


def _heading(page):
    return page.locator(".flowdash-wizard-content")


def test_the_first_admin_lands_in_the_wizard_and_can_skip_and_reopen_it(
    page, tmp_path, sections, monkeypatch
):
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    token = access.admin_token()
    try:
        _serve(page, tmp_path, suffix=f"/#token={token}", admin=False)
        wait_until(lambda: _path(page) == "/admin/setup", page)
        expect(_heading(page)).to_contain_text("Found when starting")
        written = (tmp_path / PROJECT_FILE).read_text()

        _footer(page, "Skip setup").click()
        wait_until(lambda: _path(page) == "/admin/general", page)
        expect(page.locator(".flowdash-setting-admins")).to_contain_text(getpass.getuser())
        assert (tmp_path / PROJECT_FILE).read_text() == written

        page.get_by_role("button", name="Setup wizard").click()
        wait_until(lambda: _path(page) == "/admin/setup", page)
        expect(_footer(page, "Start")).to_be_visible()
    finally:
        access.reset_admin_token()


def test_finish_writes_what_review_showed(page, tmp_path, sections, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _serve(page, tmp_path)
    _footer(page, "Start").click()
    expect(_heading(page)).to_contain_text("Served data folder")
    _footer(page, "Continue").click()

    expect(page.locator(".flowdash-wizard-env input")).to_have_value("OPENAI_API_KEY")
    expect(_heading(page)).to_contain_text("is set, ends in cdef")
    _footer(page, "Continue").click()

    title = page.locator(".flowdash-setting-title input")
    title.fill("Wind Energy")
    title.press("Enter")
    expect(page.locator(".flowdash-theme-preview")).to_contain_text("Wind Energy")
    _footer(page, "Continue").click()

    page.locator(".flowdash-setting-default_allow").get_by_role(
        "button", name="Only its owner"
    ).click()
    _footer(page, "Continue").click()

    diff = page.locator(".flowdash-wizard-diff")
    expect(diff).to_contain_text("title: Wind Energy")
    expect(diff).to_contain_text("api_key: ${OPENAI_API_KEY}")
    expect(diff).to_contain_text("default_allow: false")
    expect(diff).not_to_contain_text(SECRET)
    expect(page.locator(".flowdash-wizard-card-3")).to_contain_text("Title: Wind Energy")
    assert "theme" not in load_project(tmp_path / PROJECT_FILE)

    _footer(page, "Finish and open").click()
    wait_until(lambda: _path(page) == "/", page)
    project = load_project(tmp_path / PROJECT_FILE)
    assert project.get("theme") == {"title": "Wind Energy"}
    assert project.get("permissions") == {"default_allow": False}
    assert project.get("ai")["providers"] == {"openai": {"api_key": "${OPENAI_API_KEY}"}}
    assert SECRET not in (tmp_path / PROJECT_FILE).read_text()


def test_cancel_leaves_the_file_untouched(page, tmp_path, sections):
    _serve(page, tmp_path)
    original = (tmp_path / PROJECT_FILE).read_text()
    _footer(page, "Start").click()
    _footer(page, "Skip this step").click()
    _footer(page, "Skip this step").click()
    title = page.locator(".flowdash-setting-title input")
    title.fill("Not kept")
    title.press("Enter")
    _footer(page, "Continue").click()
    _footer(page, "Continue").click()
    expect(page.locator(".flowdash-wizard-diff")).to_contain_text("title: Not kept")

    _footer(page, "Cancel setup").click()
    wait_until(lambda: _path(page) == "/admin/general", page)
    assert (tmp_path / PROJECT_FILE).read_text() == original


class _LimitsStep(WizardStep):
    def __init__(self, project):
        super().__init__(project, title="Limits", heading="Row limits")
        self.rows = pmui.IntInput(value=1000, label="Rows", css_classes=["limits-rows"])

    def changes(self):
        return {"limits": {"rows": self.rows.value}}

    def summary(self):
        return [f"Rows: {self.rows.value}"]

    def view(self):
        return self.rows


def test_a_plugin_step_appears_in_the_stepper(page, tmp_path, sections):
    project_module.register_section(
        ProjectSection(
            "limits", lambda section: dict(section.value), title="Limits", wizard_view=_LimitsStep
        )
    )
    _serve(page, tmp_path)
    stepper = page.locator(".flowdash-wizard-stepper")
    expect(stepper).to_contain_text("Limits")
    stepper.get_by_text("Limits", exact=True).click()
    expect(_heading(page)).to_contain_text("Row limits")
    _footer(page, "Continue").click()
    expect(page.locator(".flowdash-wizard-diff")).to_contain_text("rows: 1000")
