"""Tests of the setup wizard at /admin/setup."""

import getpass
import json
import logging
from types import SimpleNamespace

import panel_material_ui as pmui
import pytest

from panel_flowdash import ai as ai_module, project as project_module
from panel_flowdash.admin import SettingsForm, WizardStep, access
from panel_flowdash.admin.page import ADMIN_ROUTE, AdminPage
from panel_flowdash.admin.wizard import (
    PENDING_SECRET,
    AIStep,
    DataStep,
    SetupWizard,
    project_is_new,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig
from panel_flowdash.dashboard_store import DashboardStore, MemoryDashboardStore
from panel_flowdash.project import PROJECT_FILE, ProjectSection, load_project
from panel_flowdash.secret_store import encryption_available, resolve_secret

ADMINS = AuthConfig(
    user_groups={getpass.getuser(): frozenset({"admins"})}, admin_groups=frozenset({"admins"})
)

SECRET = "sk-wizard-secret-0123456789abcdef"


@pytest.fixture
def sections(monkeypatch):
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


@pytest.fixture
def token(monkeypatch):
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    yield access.admin_token()
    access.reset_admin_token()


@pytest.fixture
def secret_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWDASH_SECRET_KEY_FILE", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))


def _app(tmp_path, **params):
    params.setdefault("auth_config", ADMINS)
    return FlowDashApp(registry={}, project_dir=tmp_path, store=MemoryDashboardStore(), **params)


def _wizard(app) -> SetupWizard:
    return app._admin_view(f"{ADMIN_ROUTE}/setup")


def _step(wizard, title):
    return next(step for step in wizard.steps if step.title == title)


def _go(wizard, title):
    wizard.show(wizard.titles.index(title))


def _file(tmp_path):
    path = tmp_path / PROJECT_FILE
    return path.read_text() if path.exists() else None


class TestFirstVisit:
    def test_claiming_a_new_project_opens_the_wizard(self, tmp_path, sections, token):
        app = _app(tmp_path, auth_config=AuthConfig())
        visited = []
        app._navigate_to = visited.append
        app._token_exchange.token = token
        assert visited == ["/admin/setup"]
        assert isinstance(_wizard(app), SetupWizard)

    def test_a_project_with_settings_opens_no_wizard(self, tmp_path, sections, token):
        (tmp_path / PROJECT_FILE).write_text("theme:\n  title: Wind\n")
        app = _app(tmp_path, auth_config=AuthConfig())
        visited = []
        app._navigate_to = visited.append
        app._token_exchange.token = token
        assert app._is_admin
        assert visited == []

    def test_a_file_with_only_admins_is_still_new(self, tmp_path, sections):
        project = _app(tmp_path)._settings_project()
        assert project_is_new(project)
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [ana]\n")
        assert project_is_new(project)
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [ana]\n  autosave: false\n")
        assert not project_is_new(project)

    def test_someone_who_is_not_an_admin_gets_no_wizard(self, tmp_path, sections):
        app = _app(tmp_path, auth_config=AuthConfig())
        assert not isinstance(_wizard(app), SetupWizard)


class TestSkipAndReopen:
    def test_skip_setup_leaves_the_file_alone_and_goes_to_general(self, tmp_path, sections):
        app = _app(tmp_path)
        visited = []
        app._navigate_to = visited.append
        wizard = _wizard(app)
        assert wizard._skip.label == "Skip setup"
        wizard.skip()
        assert visited == ["/admin/general"]
        assert _file(tmp_path) is None
        # The next visit gets a new wizard.
        assert _wizard(app) is not wizard

    def test_general_reopens_the_wizard(self, tmp_path, sections):
        app = _app(tmp_path)
        visited = []
        app._navigate_to = visited.append
        page = app._admin_view(f"{ADMIN_ROUTE}/general")
        assert isinstance(page, AdminPage)
        assert page._wizard_button.visible
        page._wizard_button.clicks += 1
        assert visited == ["/admin/setup"]
        page.show("theme")
        assert not page._wizard_button.visible


class TestFinish:
    def test_finish_writes_exactly_the_reviewed_values(self, tmp_path, sections, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{getpass.getuser()}]\n")
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        assert wizard.titles == ["Welcome", "Data", "AI", "Theme", "People", "Review"]
        theme = _step(wizard, "Theme").forms[0]
        theme._widgets["title"].value = "Wind"
        theme._widgets["primary_color"].value = "#006D77"
        admins, policy = _step(wizard, "People").forms
        admins._widgets["admins"].value = [getpass.getuser(), "ana"]
        policy._widgets["default_allow"].value = False
        _go(wizard, "Review")

        assert wizard.problems == []
        reviewed = wizard.plan
        assert set(reviewed) == {"ai", "theme", "general", "permissions"}
        assert reviewed["theme"] == {"title": "Wind", "primary_color": "#006D77"}
        assert reviewed["general"] == {"admins": [getpass.getuser(), "ana"]}
        assert reviewed["permissions"] == {"default_allow": False}
        assert reviewed["ai"]["providers"] == {"openai": {"api_key": "${OPENAI_API_KEY}"}}
        diff = wizard.diff()
        assert ("add", "  title: Wind") in diff
        assert ("ctx", f"  - {getpass.getuser()}") in diff
        assert ("add", "  - ana") in diff

        assert wizard.finish()
        project = load_project(tmp_path / PROJECT_FILE)
        assert {name: project.get(name) for name in reviewed} == reviewed
        assert set(project.data) == set(reviewed)
        assert SECRET not in _file(tmp_path)

    def test_review_is_what_finish_writes_even_if_a_field_changes_after(self, tmp_path, sections):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        wizard.skipped.add(wizard.titles.index("AI"))
        theme = _step(wizard, "Theme").forms[0]
        theme._widgets["title"].value = "Wind"
        _go(wizard, "Review")
        theme._widgets["title"].value = "Not reviewed"
        assert wizard.finish()
        assert load_project(tmp_path / PROJECT_FILE).get("theme") == {"title": "Wind"}

    def test_a_skipped_step_writes_nothing(self, tmp_path, sections):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        _step(wizard, "Theme").forms[0]._widgets["title"].value = "Wind"
        _go(wizard, "AI")
        wizard.skip()
        assert wizard.step == wizard.titles.index("Theme")
        wizard.skip()
        wizard.next()
        assert wizard.step == wizard.review_index
        assert wizard.plan == {}
        assert wizard._note.object == "Nothing to save"
        assert wizard.finish()
        assert _file(tmp_path) is None

    def test_a_value_the_loader_refuses_blocks_finish(self, tmp_path, sections):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        wizard.skipped.add(wizard.titles.index("AI"))
        _step(wizard, "Theme").forms[0]._widgets["logo"].value = "../outside.svg"
        _go(wizard, "Review")
        assert any("logo" in p for p in wizard.problems)
        assert wizard._alert.visible
        assert not wizard.finish()
        assert _file(tmp_path) is None

    def test_finish_applies_the_theme_and_opens_the_app(self, tmp_path, sections):
        app = _app(tmp_path)
        visited = []
        app._navigate_to = visited.append
        wizard = _wizard(app)
        wizard.skipped.add(wizard.titles.index("AI"))
        _step(wizard, "Theme").forms[0]._widgets["title"].value = "Wind"
        _go(wizard, "Review")
        wizard.next()
        assert visited == ["/"]
        assert app._page.title == "Wind"


class TestCancel:
    def test_cancel_writes_nothing(self, tmp_path, sections, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        original = f"general:\n  admins: [{getpass.getuser()}]  # me\n"
        (tmp_path / PROJECT_FILE).write_text(original)
        app = _app(tmp_path)
        visited = []
        app._navigate_to = visited.append
        wizard = _wizard(app)
        _step(wizard, "Theme").forms[0]._widgets["title"].value = "Wind"
        _go(wizard, "Review")
        assert wizard.plan
        wizard.cancel()
        assert visited == ["/admin/general"]
        assert _file(tmp_path) == original


class TestSecrets:
    def test_the_key_is_an_environment_reference_by_default(self, tmp_path, sections, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        wizard = _wizard(_app(tmp_path))
        ai = _step(wizard, "AI")
        assert isinstance(ai, AIStep)
        # The provider whose key the environment has is picked.
        assert ai.provider == "anthropic"
        assert ai._env.value == "ANTHROPIC_API_KEY"
        assert "is set, ends in cdef" in ai._key_note.object
        assert SECRET not in ai._key_note.object
        _go(wizard, "Review")
        assert wizard.plan["ai"]["providers"]["anthropic"] == {"api_key": "${ANTHROPIC_API_KEY}"}
        # No model was given on start, so none is made up.
        assert ai._model.value == ""
        assert "models" not in wizard.plan["ai"] and "default" not in wizard.plan["ai"]
        assert not any(SECRET in text for _kind, text in wizard.diff())
        assert wizard.finish()
        assert SECRET not in _file(tmp_path)

    def test_the_model_given_on_start_is_suggested_with_its_label(
        self, tmp_path, sections, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        project = _app(tmp_path)._settings_project()
        # Only what the step reads, so no provider package is needed to check the models.
        started = SimpleNamespace(
            agent_models=[
                {"id": "openai:gpt-6-luna", "label": "Luna"},
                {"id": "anthropic:claude-haiku-5.5", "label": "Haiku"},
            ],
            agent_model="anthropic:claude-haiku-5.5",
        )
        ai = AIStep(project, started)
        assert (ai._model.value, ai._label.value) == ("anthropic:claude-haiku-5.5", "Haiku")
        # Its provider is picked, though OpenAI's key is in the environment.
        assert ai.provider == "anthropic"
        ai = AIStep(project, SimpleNamespace(agent_models=["openai:gpt-6-luna"], agent_model=None))
        assert (ai._model.value, ai._label.value) == ("openai:gpt-6-luna", "")

    def test_a_bad_variable_name_is_a_problem_on_review(self, tmp_path, sections):
        wizard = _wizard(_app(tmp_path))
        _step(wizard, "AI")._env.value = "not a name"
        _go(wizard, "Review")
        assert any(p.startswith("AI: ") for p in wizard.problems)
        assert not wizard.finish()

    @pytest.mark.skipif(not encryption_available(), reason="needs cryptography")
    def test_a_pasted_key_is_stored_encrypted_and_never_shown(
        self, tmp_path, sections, secret_key
    ):
        key_file = tmp_path / "keys" / "secret.key"
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        ai = _step(wizard, "AI")
        ai._source.value = "stored"
        ai._key.value = SECRET
        _go(wizard, "Review")
        # Review shows a placeholder: nothing is encrypted, so no key is made, before Finish.
        assert wizard.plan["ai"]["providers"]["openai"]["api_key"] == PENDING_SECRET
        assert ("add", "        encrypted: (stored encrypted)") in wizard.diff()
        assert not key_file.exists()
        assert wizard.finish()
        assert key_file.exists()
        text = _file(tmp_path)
        assert SECRET not in text and "pending" not in text
        written = load_project(tmp_path / PROJECT_FILE).get("ai")["providers"]["openai"]
        assert set(written["api_key"]) == {"encrypted", "key_id"}
        assert resolve_secret(written["api_key"]) == SECRET

    @pytest.mark.skipif(not encryption_available(), reason="needs cryptography")
    def test_cancel_after_pasting_a_key_makes_no_secret_key(self, tmp_path, sections, secret_key):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        ai = _step(wizard, "AI")
        ai._source.value = "stored"
        ai._key.value = SECRET
        _go(wizard, "Review")
        wizard.cancel()
        assert not (tmp_path / "keys" / "secret.key").exists()
        assert _file(tmp_path) is None

    def test_a_pasted_key_never_reaches_the_debug_log(self, tmp_path, sections, caplog):
        ai = _step(_wizard(_app(tmp_path)), "AI")
        # Panel's logger does not propagate, so listen to it directly.
        panel_logger = ai._key._logger
        caplog.set_level(logging.DEBUG, logger=panel_logger.name)
        panel_logger.addHandler(caplog.handler)
        try:
            # What the browser sends as the admin types the key.
            ai._key._process_events({"value_input": SECRET, "value": SECRET})
        finally:
            panel_logger.removeHandler(caplog.handler)
        assert ai._key.value == SECRET
        assert "received events" in caplog.text
        assert SECRET not in caplog.text

    async def test_test_key_asks_the_provider_without_saving(
        self, tmp_path, sections, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        seen = []

        async def fetch(url, headers):
            seen.append(headers)
            return 401, "Unauthorized"

        async def check(name, key, base_url=None):
            from panel_flowdash.ai import check_key

            return await check_key(name, key, base_url, fetch=fetch)

        monkeypatch.setattr("panel_flowdash.admin.wizard.check_key", check)
        ai = _step(_wizard(_app(tmp_path)), "AI")
        result = await ai.test()
        assert not result.ok
        assert seen == [{"Authorization": f"Bearer {SECRET}"}]
        assert "Not connected" in ai._check.object
        assert SECRET not in ai._check.object
        assert _file(tmp_path) is None


class _LimitsStep(WizardStep):
    def __init__(self, project):
        super().__init__(project, title="Limits", heading="Row limits")
        self.rows = pmui.IntInput(value=1000, label="Rows")

    def changes(self):
        return {"limits": {"rows": self.rows.value}}

    def summary(self):
        return [f"Rows: {self.rows.value}"]

    def view(self):
        return self.rows


class TestPluginSteps:
    def test_a_plugin_step_appears_and_is_written(self, tmp_path, sections):
        project_module.register_section(
            ProjectSection(
                "limits",
                lambda section: dict(section.value),
                title="Limits",
                wizard_view=_LimitsStep,
            )
        )
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        assert wizard.titles == ["Welcome", "Data", "AI", "Theme", "People", "Limits", "Review"]
        wizard.skipped.add(wizard.titles.index("AI"))
        _step(wizard, "Limits").rows.value = 500
        _go(wizard, "Review")
        assert wizard.plan == {"limits": {"rows": 500}}
        assert wizard._step_lines[wizard.titles.index("Limits")] == ["Rows: 500"]
        assert wizard.finish()
        assert load_project(tmp_path / PROJECT_FILE).get("limits") == {"rows": 500}

    def test_a_data_step_takes_the_place_of_the_data_folder(self, tmp_path, sections):
        def sources(project):
            return SettingsForm(project, "sources", ())

        project_module.register_section(
            ProjectSection(
                "sources", lambda s: s.value, title="Sources", group="Data", wizard_view=sources
            )
        )
        wizard = _wizard(_app(tmp_path))
        assert wizard.titles[:3] == ["Welcome", "Sources", "AI"]
        assert not any(isinstance(step, DataStep) for step in wizard.steps)

    def test_a_step_that_fails_to_build_is_shown_and_writes_nothing(self, tmp_path, sections):
        def broken(project):
            raise RuntimeError("no database")

        project_module.register_section(
            ProjectSection("broken", lambda s: s.value, title="Broken", wizard_view=broken)
        )
        wizard = _wizard(_app(tmp_path))
        _go(wizard, "Broken")
        assert "no database" in wizard._body[0].object
        _go(wizard, "Review")
        assert "broken" not in wizard.plan

    def test_the_data_step_lists_the_served_data_files(self, tmp_path, sections):
        (tmp_path / "turbines.csv").write_text("a,b\n1,2\n")
        (tmp_path / "notes.txt").write_text("not data")
        wizard = _wizard(_app(tmp_path))
        data = _step(wizard, "Data")
        assert data.summary() == ["1 data file in the project folder"]
        assert "turbines.csv" in data.view()[2].object
        assert "notes.txt" not in data.view()[2].object
        assert ("file", "1 data file in the project folder", "turbines.csv") in _step(
            wizard, "Welcome"
        ).found()

    def test_the_data_step_leaves_out_the_dashboard_store(self, tmp_path, sections):
        (tmp_path / "turbines.csv").write_text("a,b\n1,2\n")
        (tmp_path / "plants.db").write_bytes(b"")
        (tmp_path / "dashboards.db").write_bytes(b"")
        store = DashboardStore(str(tmp_path / "custom.db"))
        app = FlowDashApp(registry={}, project_dir=tmp_path, store=store, auth_config=ADMINS)
        data = _step(_wizard(app), "Data")
        assert data.summary() == ["2 data files in the project folder"]
        listing = data.view()[2].object
        assert "turbines.csv" in listing and "plants.db" in listing
        assert "dashboards.db" not in listing and "custom.db" not in listing


class TestFinishIsSafe:
    def test_a_save_by_another_admin_after_review_sends_back_to_review(self, tmp_path, sections):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        wizard.skipped.add(wizard.titles.index("AI"))
        _step(wizard, "Theme").forms[0]._widgets["title"].value = "Wind"
        _go(wizard, "Review")
        # Another admin saves General while this one is on Review.
        (tmp_path / PROJECT_FILE).write_text("general:\n  tile_titles: false\n")
        assert not wizard.finish()
        assert wizard.step == wizard.review_index
        assert "changed since Review opened" in wizard._alert.object
        assert _file(tmp_path) == "general:\n  tile_titles: false\n"
        # Reviewed again, Finish keeps both.
        assert wizard.finish()
        project = load_project(tmp_path / PROJECT_FILE)
        assert project.get("general") == {"tile_titles": False}
        assert project.get("theme") == {"title": "Wind"}

    def test_someone_no_longer_admin_cannot_finish(self, tmp_path, sections):
        app = _app(tmp_path)
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        _step(wizard, "Theme").forms[0]._widgets["title"].value = "Wind"
        _go(wizard, "Review")
        app._still_admin = lambda: False
        assert not wizard.finish()
        assert "no longer an admin" in wizard._alert.object
        assert _file(tmp_path) is None

    def test_removing_yourself_as_admin_needs_confirming(self, tmp_path, sections):
        user = getpass.getuser()
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{user}, ana]\n")
        app = _app(tmp_path, auth_config=AuthConfig())
        app._navigate_to = lambda path: None
        wizard = _wizard(app)
        wizard.skipped.add(wizard.titles.index("AI"))
        admins = _step(wizard, "People").forms[0]
        admins._widgets["admins"].value = ["ana"]
        _go(wizard, "Review")
        assert wizard.confirmations and user in wizard.confirmations[0]
        assert wizard._confirm.visible and wizard._confirm_alert.visible
        assert not wizard.finish()
        assert "ana" in _file(tmp_path) and user in _file(tmp_path)
        wizard._confirm.value = True
        assert wizard.finish()
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": ["ana"]}

    def test_keeping_yourself_needs_no_confirmation(self, tmp_path, sections):
        user = getpass.getuser()
        (tmp_path / PROJECT_FILE).write_text(f"general:\n  admins: [{user}]\n")
        wizard = _wizard(_app(tmp_path, auth_config=AuthConfig()))
        _step(wizard, "People").forms[0]._widgets["admins"].value = [user, "ana"]
        _go(wizard, "Review")
        assert wizard.confirmations == []
        assert not wizard._confirm.visible


async def test_the_ai_step_suggests_the_models_the_provider_lists(tmp_path, sections, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def fetch(url, headers):
        assert headers["Authorization"] == f"Bearer {SECRET}"
        return 200, json.dumps({"data": [{"id": "gpt-6"}]})

    monkeypatch.setattr(ai_module, "_fetch_body", fetch)
    step = _step(_wizard(_app(tmp_path)), "AI")
    step._provider.value = "openai"
    assert await step.suggest_models() == ["openai:gpt-6"]
    assert step._model.helper_text == "Models listed by OpenAI."
    step._provider.value = "anthropic"
    assert await step.suggest_models() == []
    assert step._model.options == []
