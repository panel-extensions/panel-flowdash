"""Tests for who gets admin rights from where, and for admin saves checked on the server."""

import getpass
import logging
import socket
from types import SimpleNamespace

import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import project as project_module
from panel_flowdash.admin import ADMIN_ROUTE, AdminPage, access
from panel_flowdash.admin.access import (
    admin_entry,
    admin_name,
    client_address,
    entry_matches,
    is_admin,
    is_loopback,
    namespaced_entry,
    session_is_local,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.project import PROJECT_FILE, load_project
from panel_flowdash.ui.states import EmptyState

ME = getpass.getuser()
REMOTE = "203.0.113.5"


@pytest.fixture
def sections(monkeypatch):
    """Give each test its own copy of the section registry, with the built-in sections."""
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


@pytest.fixture
def token(monkeypatch):
    """Give the test a fresh admin token, as a server start does."""
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    yield access.admin_token()
    access.reset_admin_token()


def _request(peer: str | None, headers=None, family=socket.AF_INET):
    context = SimpleNamespace(
        address=(peer, 50000) if peer else "/tmp/flowdash.sock",
        address_family=family if peer else socket.AF_UNIX,
    )
    return SimpleNamespace(
        arguments={},
        cookies={},
        headers=dict(headers or {}),
        connection=SimpleNamespace(context=context),
    )


def _doc(peer: str | None, headers=None):
    doc = Document()
    request = _request(peer, headers)
    session_context = SimpleNamespace(id="session", request=request, server_context=None)
    doc._session_context = lambda: session_context
    return doc


def _app(tmp_path, text=None, doc=None, **params):
    if text is not None:
        (tmp_path / PROJECT_FILE).write_text(text)
    params.setdefault("auth_config", AuthConfig())
    params.setdefault("notifications", False)
    build = lambda: FlowDashApp(
        registry={},
        project_dir=tmp_path,
        store=params.pop("store", MemoryDashboardStore()),
        include_builtin_components=False,
        **params,
    )
    if doc is None:
        return build()
    with set_curdoc(doc):
        return build()


def _signed_in(monkeypatch, name, groups=()):
    identity = Identity(user=name, oauth_user=name, groups=frozenset(groups))
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda config: identity)


class TestClientAddress:
    def test_the_socket_peer_is_the_client(self):
        assert client_address(_request("127.0.0.1")) == "127.0.0.1"
        assert client_address(_request(REMOTE)) == REMOTE
        assert client_address(_request("::1", family=socket.AF_INET6)) == "::1"

    def test_forwarding_headers_from_an_untrusted_peer_make_the_client_unknown(self):
        # A reverse proxy on the same machine must not make its visitors local.
        for header in ("X-Forwarded-For", "x-real-ip", "Forwarded"):
            assert client_address(_request("127.0.0.1", {header: REMOTE})) is None
        # Nor can a client name itself local.
        assert client_address(_request(REMOTE, {"X-Forwarded-For": "127.0.0.1"})) is None

    def test_a_trusted_proxy_forwards_the_client(self):
        proxy = ["127.0.0.1"]
        assert client_address(_request("127.0.0.1", {"X-Forwarded-For": REMOTE}), proxy) == REMOTE
        # The proxy appends the address it saw; what the client sent before it is ignored.
        spoofed = _request("127.0.0.1", {"X-Forwarded-For": f"127.0.0.1, {REMOTE}"})
        assert client_address(spoofed, proxy) == REMOTE
        local = _request("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"})
        assert client_address(local, proxy) == "127.0.0.1"
        assert client_address(_request("127.0.0.1"), proxy) is None
        garbled = _request("127.0.0.1", {"X-Forwarded-For": "not-an-ip"})
        assert client_address(garbled, proxy) is None

    def test_a_unix_socket_or_a_request_without_a_socket_has_no_client(self):
        assert client_address(_request(None)) is None
        assert client_address(SimpleNamespace(headers={})) is None

    def test_loopback_addresses(self):
        for address in ("127.0.0.1", "127.8.0.1", "::1", "::ffff:127.0.0.1"):
            assert is_loopback(address)
        for address in (REMOTE, "10.0.0.1", "0.0.0.0", "", None, "localhost"):
            assert not is_loopback(address)

    def test_a_session_is_local_by_its_client(self):
        assert session_is_local(None)
        assert session_is_local(Document())
        assert session_is_local(_doc("127.0.0.1"))
        assert not session_is_local(_doc(REMOTE))
        assert not session_is_local(_doc("127.0.0.1", {"X-Forwarded-For": REMOTE}))
        assert session_is_local(_doc("127.0.0.1", {"X-Forwarded-For": "::1"}), ["127.0.0.1"])


class TestWithoutSignInAdminIsLocal:
    def test_a_session_from_another_machine_is_not_admin_even_after_the_claim(
        self, tmp_path, sections, token
    ):
        local = _app(tmp_path, doc=_doc("127.0.0.1"))
        local._token_exchange.token = token
        assert local._is_admin and local._still_admin()
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": [f"local:{ME}"]}

        # Every visitor without sign-in is the server's account, so only where they are counts.
        remote = _app(tmp_path, doc=_doc(REMOTE))
        assert remote._identity.user == ME
        assert not remote._is_admin and not remote._still_admin()
        assert "admin" not in [item["key"] for item in remote._shell.rail_items()]
        view = remote._admin_view(f"{ADMIN_ROUTE}/general")
        assert isinstance(view, EmptyState) and "server's machine" in view.title
        assert not isinstance(remote._admin_view(f"{ADMIN_ROUTE}/setup"), AdminPage)

    def test_the_link_from_another_machine_is_refused_and_stays_unused(
        self, tmp_path, sections, token
    ):
        remote = _app(tmp_path, doc=_doc(REMOTE))
        remote._token_exchange.token = token
        assert not remote._is_admin
        assert not (tmp_path / PROJECT_FILE).exists()
        assert access.check_token(token)

        local = _app(tmp_path, doc=_doc("::1"))
        local._token_exchange.token = token
        assert local._is_admin

    def test_a_proxied_session_is_not_local_unless_the_proxy_is_trusted(self, tmp_path, sections):
        text = f"general:\n  admins: [local:{ME}]\n"
        proxied = _doc("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"})
        assert not _app(tmp_path, text, doc=proxied)._is_admin
        assert _app(tmp_path, text, doc=proxied, trusted_proxies=["127.0.0.1"])._is_admin
        outside = _doc("127.0.0.1", {"X-Forwarded-For": REMOTE})
        assert not _app(tmp_path, text, doc=outside, trusted_proxies=["127.0.0.1"])._is_admin

    def test_admin_groups_without_sign_in_are_local_too(self, tmp_path, sections):
        config = AuthConfig(
            user_groups={ME: frozenset({"admins"})}, admin_groups=frozenset({"admins"})
        )
        assert _app(tmp_path, doc=_doc("127.0.0.1"), auth_config=config)._is_admin
        remote = _app(tmp_path, doc=_doc(REMOTE), auth_config=config)
        assert not remote._is_admin
        # Nor do the admin groups give it rights over other people's dashboards.
        theirs = remote.store.create_dashboard("ana", "Theirs")
        assert not remote._can_administer_dashboard(theirs.dashboard_id)
        assert not remote._can_edit_dashboard(theirs.dashboard_id)
        mine = remote.store.create_dashboard(ME, "Mine")
        assert remote._can_administer_dashboard(mine.dashboard_id)

    def test_a_signed_in_admin_may_be_anywhere(self, tmp_path, sections, monkeypatch):
        _signed_in(monkeypatch, "alice")
        text = "general:\n  admins: [auth:alice]\n"
        assert _app(tmp_path, text, doc=_doc(REMOTE))._is_admin
        _signed_in(monkeypatch, "ops", groups={"admins"})
        config = AuthConfig(admin_groups=frozenset({"admins"}))
        assert _app(tmp_path, doc=_doc(REMOTE), auth_config=config)._is_admin


class TestNamespacedAdmins:
    def test_entries_name_a_local_account_or_a_signed_in_user(self):
        local = Identity(user=ME, system_user=ME)
        alice = Identity(user="alice", oauth_user="alice")
        assert admin_entry(local) == f"local:{ME}"
        assert admin_entry(alice) == "auth:alice"
        assert admin_entry(Identity(user="anonymous")) is None
        assert entry_matches(f"local:{ME}", local) and not entry_matches(f"auth:{ME}", local)
        assert entry_matches("auth:alice", alice) and not entry_matches("local:alice", alice)
        assert admin_name("local:x") == admin_name("auth:x") == admin_name("x") == "x"
        assert namespaced_entry(" bo ", signed_in=False) == "local:bo"
        assert namespaced_entry("bo", signed_in=True) == "auth:bo"
        assert namespaced_entry("local:bo", signed_in=True) == "local:bo"

    def test_an_old_plain_entry_is_a_local_account(self):
        assert is_admin(Identity(user=ME, system_user=ME), (), [ME])
        # Sign-in turned on later: a signed-in user of that name is not the local account.
        assert not is_admin(Identity(user=ME, oauth_user=ME), (), [ME, f"local:{ME}"])

    def test_turning_on_sign_in_does_not_make_the_local_admin_s_namesake_admin(
        self, tmp_path, sections, monkeypatch
    ):
        text = f"general:\n  admins: [{ME}, local:bo]\n"
        assert _app(tmp_path, text)._is_admin
        _signed_in(monkeypatch, ME)
        assert not _app(tmp_path)._is_admin
        _signed_in(monkeypatch, "bo")
        assert not _app(tmp_path)._is_admin

    def test_a_signed_in_admin_adds_signed_in_users(self, tmp_path, sections, monkeypatch):
        _signed_in(monkeypatch, "alice")
        app = _app(tmp_path, "general:\n  admins: [auth:alice]\n")
        form = app._admin_view(f"{ADMIN_ROUTE}/general").view("general")
        chosen = form._widgets["admins"]
        add = chosen._flowdash_parts[0]
        add.value = "bob"
        add.param.trigger("enter_pressed")
        assert chosen.value == ["auth:alice", "auth:bob"]

    def test_people_and_known_names_show_names_without_the_prefix(self, tmp_path, sections):
        app = _app(tmp_path, f"general:\n  admins: [local:{ME}, auth:alice]\n")
        people = app._admin_view(f"{ADMIN_ROUTE}/permissions").view("permissions").people()
        assert people[ME]["role"] == people["alice"]["role"] == "Admin"
        assert not any(":" in name for name in people)
        dashboards = app._admin_view(f"{ADMIN_ROUTE}/dashboards").view("dashboards")
        assert {ME, "alice"} <= set(dashboards._known_people())


class TestSavesAreChecked:
    def _removed(self, tmp_path):
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [local:someone-else]\n")

    def test_settings_forms_refuse_once_the_admin_was_removed(self, tmp_path, sections):
        app = _app(tmp_path, f"general:\n  admins: [local:{ME}]\n")
        page = app._admin_view(f"{ADMIN_ROUTE}/general")
        general = page.view("general")
        general._widgets["tile_titles"].value = True
        theme = page.view("theme")
        theme._widgets["primary_color"].value = "#ff0000"
        permissions = page.view("permissions")
        permissions._widgets["default_allow"].value = False
        self._removed(tmp_path)
        for name, form in (("general", general), ("theme", theme), ("permissions", permissions)):
            assert not page.save(name), name
            assert "no longer an admin" in form._alert.object
            assert form.dirty
        assert load_project(tmp_path / PROJECT_FILE).data == {
            "general": {"admins": ["local:someone-else"]}
        }

    def test_ai_writes_and_tests_refuse_once_the_admin_was_removed(
        self, tmp_path, sections, monkeypatch
    ):
        app = _app(
            tmp_path, f"general:\n  admins: [local:{ME}]\nai:\n  models:\n    - id: openai:a\n"
        )
        ai = app._admin_view(f"{ADMIN_ROUTE}/ai").view("ai")
        self._removed(tmp_path)
        assert not ai.set_default("openai:a")
        assert "no longer an admin" in ai._alert.object
        ai.open_key_dialog("openai")
        ai._env_name.value = "OPENAI_API_KEY"
        ai._use_env.value = True
        assert not ai.save_key()

        async def never(*_args, **_kwargs):
            raise AssertionError("A provider was asked.")

        monkeypatch.setattr("panel_flowdash.admin.ai.check_key", never)
        import asyncio

        assert not asyncio.run(ai.test("openai")).ok
        assert "ai" not in load_project(tmp_path / PROJECT_FILE).data

    def test_a_saved_form_still_saves_for_an_admin(self, tmp_path, sections):
        app = _app(tmp_path, f"general:\n  admins: [local:{ME}]\n")
        page = app._admin_view(f"{ADMIN_ROUTE}/general")
        page.view("general")._widgets["tile_titles"].value = True
        assert page.save("general")
        assert load_project(tmp_path / PROJECT_FILE).get("general")["tile_titles"] is True


class TestTheTokenIsNeverLogged:
    def _listen(self, caplog, *loggers):
        caplog.set_level(logging.DEBUG)
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
            logger.addHandler(caplog.handler)

    def test_the_token_the_page_sends_is_not_in_the_debug_log(
        self, tmp_path, sections, token, caplog
    ):
        app = _app(tmp_path)
        exchange = app._token_exchange
        # Panel's logger does not propagate, so listen to it directly.
        panel_logger = exchange._logger
        self._listen(caplog, panel_logger, logging.getLogger("panel_flowdash"))
        try:
            # What the page sends with the token from its URL fragment.
            exchange._process_events({"token": token})
        finally:
            panel_logger.removeHandler(caplog.handler)
            logging.getLogger("panel_flowdash").removeHandler(caplog.handler)
        assert app._is_admin
        assert exchange.token == ""
        assert "received events" in caplog.text
        assert token not in caplog.text

    def test_a_failing_claim_does_not_log_the_token(
        self, tmp_path, sections, token, caplog, monkeypatch
    ):
        def fail(*_args, **_kwargs):
            raise RuntimeError("could not claim")

        monkeypatch.setattr("panel_flowdash.app.claim_admin", fail)
        app = _app(tmp_path)
        exchange = app._token_exchange
        panel_logger = exchange._logger
        self._listen(caplog, panel_logger, logging.getLogger("panel"))
        try:
            exchange._process_events({"token": token})
        finally:
            panel_logger.removeHandler(caplog.handler)
            logging.getLogger("panel").removeHandler(caplog.handler)
        assert not app._is_admin
        assert "Could not use the admin link" in caplog.text
        assert token not in caplog.text


class TestDownloadYaml:
    def test_download_asks_first_and_is_recorded(self, tmp_path, sections):
        store = MemoryDashboardStore()
        theirs = store.create_dashboard("ana", "Theirs")
        app = _app(tmp_path, f"general:\n  admins: [local:{ME}]\n", store=store)
        view = app._admin_view(f"{ADMIN_ROUTE}/dashboards").view("dashboards")
        sent = []
        app._downloader.transfer = lambda **_kw: sent.append(app._downloader.filename)
        view.select(theirs.dashboard_id)
        view.ask("download")
        assert view._dialog.open and view._confirm.label == "Download"
        assert "admin log" in view._dialog_text.object
        view.cancel()
        assert sent == [] and app._usage_ledger().admin_log() == []
        view.ask("download")
        assert view.confirm()
        assert len(sent) == 1 and not view._dialog.open
        (entry,) = app._usage_ledger().admin_log()
        assert entry["user"] == ME and entry["action"] == "downloaded"
        assert [d["title"] for d in entry["dashboards"]] == ["Theirs"]

    def test_download_refuses_once_the_admin_was_removed(self, tmp_path, sections):
        store = MemoryDashboardStore()
        theirs = store.create_dashboard("ana", "Theirs")
        app = _app(tmp_path, f"general:\n  admins: [local:{ME}]\n", store=store)
        view = app._admin_view(f"{ADMIN_ROUTE}/dashboards").view("dashboards")
        app._downloader.transfer = lambda **_kw: pytest.fail("downloaded")
        view.ask("download", [theirs.dashboard_id])
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [local:someone-else]\n")
        assert not view.confirm()
        assert view._dialog.open and "Only admins" in view._dialog_alert.object
        assert app._usage_ledger().admin_log() == []
