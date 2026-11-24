"""Tests for the admin view's AI and Permissions sections and how the app applies them."""

import getpass
import json
import logging
import os

import panel as pn
import pytest
from bokeh.document import Document

from panel_flowdash import ai as ai_module, project as project_module
from panel_flowdash.admin import ADMIN_ROUTE, AISettings, PermissionsForm
from panel_flowdash.ai import (
    KEY_CHECKS,
    ModelEntry,
    UsageLedger,
    check_key,
    ledger_for,
    select_model,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity, make_authorize_callback
from panel_flowdash.command import serve as serve_module
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.permissions import (
    PermissionsUnavailable,
    approval_rules,
    project_permissions,
)
from panel_flowdash.project import PROJECT_FILE, ProjectError, load_project, parse_project
from panel_flowdash.secret_store import (
    KEY_ENV,
    SecretError,
    describe_secret,
    encrypt_secret,
    fernet_key,
    resolve_secret,
)

cryptography = pytest.importorskip("cryptography")

ME = getpass.getuser()
SECRET = "sk-or-v1-this-is-the-secret-0123456789"
ENV_SECRET = "sk-env-another-secret-abcdefgh"


@pytest.fixture
def sections(monkeypatch):
    """Give each test its own copy of the section registry, with the built-in sections."""
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


@pytest.fixture(autouse=True)
def secret_key(monkeypatch, tmp_path):
    """Encrypt with a key of the test's own, and never touch the real keyring or key file."""
    monkeypatch.setenv(KEY_ENV, fernet_key())
    monkeypatch.setenv("FLOWDASH_SECRET_KEY_FILE", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setattr("panel_flowdash.secret_store._keyring", lambda: (None, ()))
    for name in ("FLOWDASH_AGENT_MODEL", "FLOWDASH_AGENT_MODELS", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    KEY_CHECKS.clear()
    yield
    KEY_CHECKS.clear()


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


def _section(app, name):
    return app._admin_view(f"{ADMIN_ROUTE}/{name}").view(name)


def _document_text(viewable) -> str:
    """Return everything the browser would receive for *viewable*, as one JSON string."""
    doc = Document()
    doc.add_root(pn.panel(viewable).get_root(doc))
    return json.dumps(doc.to_json().content, default=str)


class TestSecrets:
    def test_a_stored_secret_round_trips_and_names_its_key(self):
        stored = encrypt_secret(SECRET)
        assert SECRET not in str(stored)
        assert set(stored) == {"encrypted", "key_id"}
        assert resolve_secret(stored) == SECRET

    def test_rotation_decrypts_values_made_with_an_older_key(self, monkeypatch):
        old = os.environ[KEY_ENV]
        stored = encrypt_secret(SECRET)
        monkeypatch.setenv(KEY_ENV, f"{fernet_key()},{old}")
        assert resolve_secret(stored) == SECRET
        assert encrypt_secret(SECRET)["key_id"] != stored["key_id"]

    def test_a_value_whose_key_is_gone_asks_to_be_entered_again(self, monkeypatch):
        stored = encrypt_secret(SECRET)
        monkeypatch.setenv(KEY_ENV, fernet_key())
        with pytest.raises(SecretError, match="enter it again") as info:
            resolve_secret(stored)
        assert SECRET not in str(info.value)
        assert describe_secret(stored).error and describe_secret(stored).source == "stored"

    def test_without_a_configured_key_one_is_made_in_a_private_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv(KEY_ENV)
        stored = encrypt_secret(SECRET)
        key_file = tmp_path / "keys" / "secret.key"
        assert key_file.is_file()
        if os.name != "nt":
            assert key_file.stat().st_mode & 0o777 == 0o600
        assert resolve_secret(stored) == SECRET

    def test_a_reference_reads_its_variable_and_shows_only_the_last_four(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", ENV_SECRET)
        info = describe_secret("${MY_KEY}")
        assert (info.source, info.env, info.last4) == ("env", "MY_KEY", ENV_SECRET[-4:])
        monkeypatch.delenv("MY_KEY")
        assert "MY_KEY" in describe_secret("${MY_KEY}").error

    def test_the_project_file_refuses_a_key_written_in_it(self, sections, tmp_path):
        text = "ai:\n  providers:\n    openai:\n      api_key: sk-plain-text\n"
        with pytest.raises(ProjectError) as info:
            parse_project(text, tmp_path / PROJECT_FILE)
        (problem,) = info.value.problems
        assert problem.keys == ("providers", "openai", "api_key") and problem.line == 4
        assert "never written in the file" in problem.message


class TestKeysNeverReachTheBrowserOrTheFile:
    def test_a_stored_key_is_encrypted_in_the_file_and_never_sent(
        self, sections, tmp_path, caplog
    ):
        caplog.set_level(logging.DEBUG)
        app = _app(tmp_path, "ai:\n  models:\n    - id: openrouter:ibm/granite\n")
        view = _section(app, "ai")
        assert isinstance(view, AISettings)
        view.open_key_dialog("openrouter")
        view._use_env.value = False
        # What the browser sends as the admin types the key.
        view._key.value = SECRET
        assert view.save_key()
        written = (tmp_path / PROJECT_FILE).read_text()
        assert SECRET not in written
        stored = load_project(tmp_path / PROJECT_FILE).get("ai")["providers"]["openrouter"][
            "api_key"
        ]
        assert resolve_secret(stored) == SECRET
        assert view._key.value == "" and view._key.value_input == ""
        page = app._admin_view(f"{ADMIN_ROUTE}/ai")
        text = _document_text(page)
        assert SECRET not in text and SECRET[-4:] in text
        assert SECRET not in caplog.text

    def test_a_key_from_the_environment_is_written_as_a_reference(
        self, sections, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TEAM_OPENAI_KEY", ENV_SECRET)
        app = _app(tmp_path, "ai:\n  models:\n    - id: openai:gpt-6\n")
        view = _section(app, "ai")
        view.open_key_dialog("openai")
        view._env_name.value = "TEAM_OPENAI_KEY"
        assert view.save_key()
        written = (tmp_path / PROJECT_FILE).read_text()
        assert "${TEAM_OPENAI_KEY}" in written and ENV_SECRET not in written
        assert ENV_SECRET not in _document_text(app._admin_view(f"{ADMIN_ROUTE}/ai"))

    def test_the_model_gets_the_key_without_it_entering_the_environment(self, sections, tmp_path):
        pytest.importorskip("pydantic_ai")
        pytest.importorskip("openai")
        stored = encrypt_secret(SECRET)
        text = (
            "ai:\n  providers:\n    openai:\n      api_key: {encrypted: "
            f"{stored['encrypted']}, key_id: {stored['key_id']}}}\n"
            "  models:\n    - id: openai:gpt-6\n"
        )
        app = _app(tmp_path, text)
        from panel_flowdash.ai import model_object

        ai = app._project_section("ai")
        (entry,), _ = app._model_entries(ai)
        model = model_object(entry, ai)
        assert model is not None and model.model_name == "gpt-6"
        assert SECRET not in os.environ.values()

    async def test_a_rejected_key_says_so_without_the_key(self):
        seen = {}

        async def fetch(url, headers):
            seen.update(headers)
            return 401, "Unauthorized"

        result = await check_key("openrouter", SECRET, fetch=fetch)
        assert not result.ok and "401 Unauthorized" in result.message
        assert SECRET not in result.message and seen["Authorization"] == f"Bearer {SECRET}"


class TestModels:
    def test_code_models_come_first_and_the_file_adds_limits_and_models(self, sections, tmp_path):
        text = (
            "ai:\n  default: anthropic:claude-haiku\n  models:\n"
            "    - id: openai:luna\n      monthly_limit: 50\n      fallback: anthropic:claude-haiku\n"
            "    - id: anthropic:claude-haiku\n      label: Haiku\n      offered_to: [assistant]\n"
            "      groups: [analysts]\n      settings: {temperature: 0}\n"
        )
        app = _app(
            tmp_path,
            text,
            agent_models=[{"id": "openai:luna", "label": "Luna"}],
            settings_sources={"agent_models": "--agent-model"},
        )
        entries, default = app._model_entries(app._project_section("ai"))
        luna, haiku = entries
        assert (luna.label, luna.locked, luna.monthly_limit) == (
            "Luna",
            "set with --agent-model",
            50,
        )
        assert haiku.settings == {"temperature": 0} and haiku.locked is None
        assert default == "anthropic:claude-haiku"
        analyst = Identity(user="ana", groups=frozenset({"analysts"}))
        assert haiku.offered(analyst, "assistant") and not haiku.offered(analyst, "explore")
        assert not haiku.offered(Identity(user="bo"), "assistant")

    def test_the_drawer_saves_a_model_and_the_table_shows_it(self, sections, tmp_path):
        app = _app(tmp_path)
        view = _section(app, "ai")
        view.open_model(None)
        view._model_id.value = "openai:gpt-6"
        view._label.value = "Six"
        view._offered.value = ["explore"]
        view._settings_inputs["max_tokens"].value = "2,000"
        view._limit.value = "25"
        assert view.save_model()
        (model,) = load_project(tmp_path / PROJECT_FILE).get("ai")["models"]
        assert model == {
            "id": "openai:gpt-6",
            "label": "Six",
            "offered_to": ["explore"],
            "settings": {"max_tokens": 2000},
            "monthly_limit": 25.0,
        }
        assert not view._drawer.open
        view.open_model(None)
        view._model_id.value = "no-provider"
        assert not view.save_model()
        assert "names the provider" in view._drawer_alert.object

    async def test_the_drawer_suggests_the_models_of_providers_with_a_key(
        self, sections, tmp_path, monkeypatch
    ):
        for provider in ai_module.PROVIDERS.values():
            if provider.env:
                monkeypatch.delenv(provider.env, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_SECRET)
        lists = {
            "https://api.openai.com/v1/models": {"data": [{"id": "gpt-6"}]},
            "https://api.anthropic.com/v1/models": {"data": [{"id": "claude-x"}]},
        }
        asked = []

        async def fetch(url, headers):
            asked.append(url.split("?")[0])
            return 200, json.dumps(lists[url.split("?")[0]])

        monkeypatch.setattr(ai_module, "_fetch_body", fetch)
        view = _section(_app(tmp_path), "ai")
        assert await view.suggest_models() == ["openai:gpt-6", "anthropic:claude-x"]
        assert view._model_id.options == ["openai:gpt-6", "anthropic:claude-x"]
        assert view._model_id.helper_text == "Models listed by OpenAI and Anthropic."
        assert sorted(asked) == sorted(lists)
        view._model_id.value = "openai:not-listed"
        assert view._model_id.value == "openai:not-listed"

    async def test_the_drawer_says_when_no_provider_has_a_key(
        self, sections, tmp_path, monkeypatch
    ):
        for provider in ai_module.PROVIDERS.values():
            if provider.env:
                monkeypatch.delenv(provider.env, raising=False)
        view = _section(_app(tmp_path), "ai")
        assert await view.suggest_models() == []
        assert "Set a provider's key" in view._model_id.helper_text

    def test_removing_a_model_drops_the_fallbacks_to_it(self, sections, tmp_path):
        text = "ai:\n  models:\n    - id: openai:a\n      fallback: openai:b\n    - id: openai:b\n"
        view = _section(_app(tmp_path, text), "ai")
        assert view.remove_model("openai:b")
        assert load_project(tmp_path / PROJECT_FILE).get("ai")["models"] == [{"id": "openai:a"}]


def _entries(*entries):
    return {entry.id: entry for entry in entries}


class TestFallback:
    LUNA = ModelEntry("openai:luna", "Luna", monthly_limit=50, fallback="openai:mini")
    MINI = ModelEntry("openai:mini", "Mini", monthly_limit=10)

    def test_a_model_under_its_limit_is_used(self):
        assert select_model(_entries(self.LUNA, self.MINI), "openai:luna", lambda _m: 49.99) == (
            "openai:luna",
            None,
        )

    def test_a_model_at_its_limit_falls_back_and_says_so(self):
        spent = {"openai:luna": 50.0, "openai:mini": 1.0}.get
        model, note = select_model(_entries(self.LUNA, self.MINI), "openai:luna", spent)
        assert model == "openai:mini" and "Luna reached its monthly limit of $50.00" in note

    def test_with_every_fallback_over_its_limit_the_request_is_refused(self):
        spent = {"openai:luna": 50.0, "openai:mini": 10.0}.get
        model, note = select_model(_entries(self.LUNA, self.MINI), "openai:luna", spent)
        assert model is None and "fallback is over its limit" in note

    def test_a_fallback_that_is_no_longer_a_model_refuses(self):
        model, _note = select_model(_entries(self.LUNA), "openai:luna", lambda _m: 60.0)
        assert model is None

    def test_a_model_without_an_entry_has_no_limit(self):
        assert select_model({}, "openai:other", lambda _m: 1e9) == ("openai:other", None)

    async def test_a_request_over_the_limit_runs_on_the_fallback_and_counts_there(
        self, sections, tmp_path, monkeypatch
    ):
        pytest.importorskip("pydantic_ai")
        from panel_agents import ModelChoice
        from pydantic_ai.models.function import FunctionModel

        from panel_flowdash.agent.assistant import create_agent

        calls = []

        def model(name, settings):
            async def stream(messages, info):
                calls.append(name)
                settings.append(dict(info.model_settings or {}))
                yield "ok"

            return FunctionModel(stream_function=stream)

        seen: list = []
        main, cheap = model("main", seen), model("cheap", seen)
        text = (
            "ai:\n  models:\n    - id: fn:main\n      monthly_limit: 5\n      fallback: fn:cheap\n"
            "    - id: fn:cheap\n      settings: {temperature: 0.5}\n"
        )
        monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _c: Identity(user=ME))
        app = _app(
            tmp_path,
            text,
            agent=create_agent(main),
            agent_models=[
                ModelChoice("fn:main", model=main),
                ModelChoice("fn:cheap", model=cheap),
            ],
        )
        notes = []
        monkeypatch.setattr(app, "_notify", lambda severity, message, **_kw: notes.append(message))
        ledger = ledger_for(tmp_path)
        ledger.add("fn:main", 5.0)
        runner = app._agent_session.runner
        runner.model = main
        await runner.run("Hi")
        assert calls == ["cheap"] and seen[-1]["temperature"] == 0.5
        assert ledger.month("fn:cheap")[1] == 1 and ledger.month("fn:main")[1] == 1
        assert "reached its monthly limit" in notes[0]


class TestPermissions:
    def test_saved_permissions_apply_to_new_sessions(self, sections, tmp_path, monkeypatch):
        store = MemoryDashboardStore()
        app = _app(tmp_path, store=store)
        form = _section(app, "permissions")
        assert isinstance(form, PermissionsForm)
        form._widgets["default_allow"].value = False
        form._widgets["admin_groups"].value = ["ops"]
        form.add_person("bo")
        form.set_groups("bo", ["ops"])
        form.add_person("cy")
        form.set_assistant("cy", False)
        form.add_pattern("Sources/*")
        assert form.add_rule("run_command", "command=action:refresh")
        assert form.dirty
        assert app._admin_page.save("permissions")
        assert not form.dirty
        saved = load_project(tmp_path / PROJECT_FILE).get("permissions")
        assert saved == {
            "default_allow": False,
            "admin_groups": ["ops"],
            "people": {"bo": {"groups": ["ops"]}, "cy": {"assistant": False}},
            "sensitive_components": ["Sources/*"],
            "approval_rules": [{"tool": "run_command", "when": {"command": "action:refresh"}}],
        }
        # A new session of bo: admin through the file's group, and dashboards are private.
        monkeypatch.setattr(
            "panel_flowdash.app.resolve_identity",
            lambda config: Identity(user="bo", groups=frozenset(config.user_groups.get("bo", ()))),
        )
        bo = FlowDashApp(
            registry={},
            project_dir=tmp_path,
            store=store,
            notifications=False,
            include_builtin_components=False,
            auth_config=AuthConfig(),
        )
        assert bo._default_allow() is False and bo._is_admin
        store.create_dashboard("ana", "Ana's")
        assert store.list_accessible(bo._identity, default_allow=bo._default_allow()) == []
        (rule,) = approval_rules(bo._permissions)
        assert rule.matches("run_command", {"command": "action:refresh"})
        assert not rule.matches("run_command", {"command": "action:save"})
        # cy gets no assistant, whatever model is configured.
        monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _c: Identity(user="cy"))
        cy = FlowDashApp(
            registry={},
            project_dir=tmp_path,
            store=store,
            notifications=False,
            include_builtin_components=False,
            auth_config=AuthConfig(),
            agent_model="test",
        )
        assert not cy._assistant_allowed() and cy._agent_session is None

    def test_settings_given_in_code_show_locked_and_win(self, sections, tmp_path):
        text = "permissions:\n  default_allow: true\n  admin_groups: [file-admins]\n"
        app = _app(
            tmp_path,
            text,
            auth_config=AuthConfig(default_allow=False),
            settings_sources={
                "auth_config": "__init__.py",
                "auth_config.default_allow": "__init__.py",
            },
        )
        assert app._default_allow() is False
        assert app._admin_groups() == frozenset({"file-admins"})
        form = _section(app, "permissions")
        assert form.locks == {"default_allow": "default_allow in __init__.py"}
        assert (
            form._widgets["default_allow"].value is False
            and form._widgets["default_allow"].disabled
        )

    def test_assistant_groups_limit_who_gets_the_assistant(self, sections, tmp_path, monkeypatch):
        text = "permissions:\n  assistant_groups: [analysts]\n"
        for groups, allowed in ((frozenset({"analysts"}), True), (frozenset(), False)):
            monkeypatch.setattr(
                "panel_flowdash.app.resolve_identity",
                lambda _c, groups=groups: Identity(user="ana", groups=groups),
            )
            assert _app(tmp_path, text)._assistant_allowed() is allowed

    def test_page_routes_follow_the_project_files_policy(self, sections, tmp_path, monkeypatch):
        path = tmp_path / PROJECT_FILE
        path.write_text("permissions:\n  admin_groups: [ops]\n")
        current = serve_module._project_auth_config(AuthConfig(), {}, path, tmp_path)
        assert current().admin_groups == frozenset({"ops"})
        locked = serve_module._project_auth_config(
            AuthConfig(admin_groups=frozenset({"code"})), {"admin_groups": "x"}, path, tmp_path
        )
        assert locked().admin_groups == frozenset({"code"})

    def test_a_rule_without_a_tool_is_refused(self, sections, tmp_path):
        form = _section(_app(tmp_path), "permissions")
        assert not form.add_rule(" ", "")
        assert not form.add_rule("run_sql", "rows")
        assert form._agent_alert.visible and not form.dirty


class TestFailClosed:
    BROKEN = "permissions:\n  default_allow: false\n<<<<<<< HEAD\n  admin_groups: [ops]\n"

    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        monkeypatch.setattr("panel_flowdash.permissions._LAST_GOOD", {})

    def test_a_broken_file_after_a_good_read_keeps_the_permissions(self, sections, tmp_path):
        path = tmp_path / PROJECT_FILE
        path.write_text("permissions:\n  default_allow: false\n")
        assert project_permissions(path) == {"default_allow": False}
        path.write_text(self.BROKEN)
        os.utime(path, ns=(1, 1))
        assert project_permissions(path) == {"default_allow": False}
        current = serve_module._project_auth_config(AuthConfig(), {}, path, tmp_path)
        assert current().default_allow is False

    def test_a_broken_file_at_startup_denies(self, sections, tmp_path, monkeypatch):
        path = tmp_path / PROJECT_FILE
        path.write_text(self.BROKEN)
        with pytest.raises(PermissionsUnavailable):
            project_permissions(path)
        current = serve_module._project_auth_config(AuthConfig(), {}, path, tmp_path)
        callback = make_authorize_callback({"Sec/page": _page()}, current)
        assert callback({}, "/Sec/page") is False
        monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _c: Identity(user=ME))
        app = FlowDashApp(
            registry={},
            project_dir=tmp_path,
            store=MemoryDashboardStore(),
            notifications=False,
            include_builtin_components=False,
            auth_config=AuthConfig(user_groups={ME: frozenset({"ops"})}),
            agent_model="test",
        )
        assert app._permissions_error and "cannot be read" in app._permissions_error
        assert not app._is_admin and app._agent_session is None

    def test_an_invalid_section_at_startup_denies_too(self, sections, tmp_path):
        path = tmp_path / PROJECT_FILE
        path.write_text("permissions:\n  default_allow: maybe\n")
        with pytest.raises(PermissionsUnavailable):
            project_permissions(path)


def _page():
    from panel_flowdash.registry import PanelAppMetadata, RegistryEntry

    return RegistryEntry(
        app_id="Sec/page",
        section="Sec",
        name="page",
        page_path="/Sec/page",
        module_name="Sec.page",
        metadata=PanelAppMetadata(page=True, component=False),
    )


async def test_taking_the_assistant_away_applies_to_an_open_session(
    sections, tmp_path, monkeypatch
):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.models.function import FunctionModel

    from panel_flowdash.agent.assistant import create_agent

    calls = []

    async def stream(messages, info):
        calls.append(1)
        yield "ok"

    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _c: Identity(user="cy"))
    app = _app(tmp_path, agent=create_agent(FunctionModel(stream_function=stream)))
    notes = []
    monkeypatch.setattr(app, "_notify", lambda severity, message, **_kw: notes.append(message))
    runner = app._agent_session.runner
    await runner.run("Hi")
    assert calls == [1]
    path = tmp_path / PROJECT_FILE
    path.write_text(path.read_text() + "permissions:\n  people:\n    cy: {assistant: false}\n")
    os.utime(path, ns=(10**18, 10**18))
    assert await runner.run("Again") is None
    assert calls == [1] and "no longer have access" in notes[-1]


async def test_a_caller_passed_model_does_not_skip_the_fallback(sections, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from panel_agents import ModelChoice
    from pydantic_ai.models.function import FunctionModel

    from panel_flowdash.agent.assistant import create_agent

    calls = []

    def model(name):
        async def stream(messages, info):
            calls.append(name)
            yield "ok"

        return FunctionModel(stream_function=stream)

    main, cheap = model("main"), model("cheap")
    text = (
        "ai:\n  models:\n    - id: fn:main\n      monthly_limit: 1\n      fallback: fn:cheap\n"
        "    - id: fn:cheap\n"
    )
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _c: Identity(user=ME))
    app = _app(
        tmp_path,
        text,
        agent=create_agent(main),
        agent_models=[ModelChoice("fn:main", model=main), ModelChoice("fn:cheap", model=cheap)],
    )
    monkeypatch.setattr(app, "_notify", lambda *_a, **_kw: None)
    ledger_for(tmp_path).add("fn:main", 2.0)
    await app._agent_session.runner.run("Hi", model=main)
    assert calls == ["cheap"]


def _add_spend(directory, times):
    ledger = UsageLedger(directory / ".flowdash" / "usage.json")
    for _ in range(times):
        ledger.add("openai:luna", 0.5)


def test_processes_sharing_the_ledger_lose_no_spend(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_add_spend, args=(tmp_path, 25)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    usd, requests = UsageLedger(tmp_path / ".flowdash" / "usage.json").month("openai:luna")
    assert requests == 100 and usd == pytest.approx(50.0)
