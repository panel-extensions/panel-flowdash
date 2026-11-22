"""Tests for the admin view: who gets it, its sections and how they reach the project file."""

import argparse
import getpass
import importlib.metadata
import sys
import types
from types import SimpleNamespace

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.command.serve import Serve as PanelServe

from panel_flowdash import project as project_module
from panel_flowdash.admin import (
    ADMIN_ROUTE,
    AdminPage,
    SettingsForm,
    access,
    is_admin,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.command.serve import Serve
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.project import PROJECT_FILE, ProjectSection, load_project
from panel_flowdash.ui.states import EmptyState

ADMINS = AuthConfig(
    user_groups={getpass.getuser(): frozenset({"admins"})}, admin_groups=frozenset({"admins"})
)


@pytest.fixture
def sections(monkeypatch):
    """Give each test its own copy of the section registry, with the built-in sections."""
    monkeypatch.setattr(project_module, "_SECTIONS", dict(project_module._SECTIONS))
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


def _app(tmp_path, **params):
    params.setdefault("auth_config", ADMINS)
    return FlowDashApp(registry={}, project_dir=tmp_path, store=MemoryDashboardStore(), **params)


def _admin(app, section=None) -> AdminPage:
    return app._admin_view(f"{ADMIN_ROUTE}/{section}" if section else ADMIN_ROUTE)


@pytest.fixture
def token(monkeypatch):
    """Give the test a fresh admin token, as a server start does."""
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    access.reset_admin_token()
    yield access.admin_token()
    access.reset_admin_token()


class TestWhoIsAdmin:
    def test_members_of_an_admin_group_are_admins(self):
        identity = Identity(user="ana", oauth_user="ana", groups=frozenset({"admins"}))
        assert is_admin(identity, {"admins"})

    def test_without_the_token_nobody_else_is(self):
        identity = Identity(user="philipp", system_user="philipp")
        assert not is_admin(identity, ())
        assert is_admin(identity, (), token=True)

    def test_a_missing_or_wrong_token_is_refused(self, tmp_path, sections, token):
        app = _app(tmp_path, auth_config=AuthConfig())
        for wrong in ("", token[:-1], token + "x", "x" * len(token)):
            app._token_exchange.token = wrong
            assert not app._is_admin
        assert isinstance(_admin(app), EmptyState)
        assert not (tmp_path / PROJECT_FILE).exists()

    def test_the_token_makes_the_local_user_an_admin_on_every_start(
        self, tmp_path, sections, token
    ):
        app = _app(tmp_path, auth_config=AuthConfig())
        assert "admin" not in [item["key"] for item in app._shell.rail_items()]
        app._token_exchange.token = token
        assert app._is_admin
        assert [item["key"] for item in app._shell.rail_items()][-2:] == ["admin", "help"]
        assert isinstance(_admin(app), AdminPage)
        user = getpass.getuser()
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": [user]}

        # A restart makes a new token, and the recorded user needs none.
        access.reset_admin_token()
        assert access.admin_token() != token
        assert _app(tmp_path, auth_config=AuthConfig())._is_admin

    def test_a_signed_in_user_who_claims_it_is_recorded_and_others_are_not_admin(
        self, tmp_path, sections, token, monkeypatch
    ):
        def signed_in(name):
            identity = Identity(user=name, oauth_user=name)
            monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda config: identity)

        signed_in("alice")
        app = _app(tmp_path, auth_config=AuthConfig())
        app._token_exchange.token = token
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": ["alice"]}

        access.reset_admin_token()
        assert _app(tmp_path, auth_config=AuthConfig())._is_admin
        signed_in("bob")
        assert not _app(tmp_path, auth_config=AuthConfig())._is_admin

    def test_claiming_keeps_the_other_general_settings(self, tmp_path, sections, token):
        (tmp_path / PROJECT_FILE).write_text("general:\n  tile_titles: true\n  admins: [ana]\n")
        _app(tmp_path, auth_config=AuthConfig())._token_exchange.token = token
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {
            "tile_titles": True,
            "admins": ["ana", getpass.getuser()],
        }

    def test_admins_add_and_remove_others_in_general(self, tmp_path, sections):
        (tmp_path / PROJECT_FILE).write_text("general:\n  admins: [ana, ben]\n")
        page = _admin(_app(tmp_path))
        form = page.view("general")
        chosen = form._widgets["admins"]
        assert chosen.value == ["ana", "ben"]
        chosen.value = ["ana"]
        add = chosen._flowdash_parts[0]
        add.value = "cleo"
        add.param.trigger("enter_pressed")
        assert chosen.value == ["ana", "cleo"]
        assert page.save("general")
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"admins": ["ana", "cleo"]}

    def test_the_token_can_be_fixed_for_scripted_setups(self, monkeypatch):
        monkeypatch.setenv(access.TOKEN_ENV, "scripted-token")
        access.reset_admin_token()
        try:
            assert access.admin_token() == "scripted-token"
            assert access.check_token("scripted-token")
        finally:
            access.reset_admin_token()

    def test_the_link_carries_the_token_in_its_fragment(self, token):
        # Browsers never send the fragment, so the token is in no request line or access log.
        path = access.admin_path("/app")
        assert path == f"/app/admin#token={token}"
        assert token not in path.split("#")[0]

    def test_the_admin_link_can_be_turned_off(self, tmp_path, sections, token):
        off = _app(tmp_path, auth_config=AuthConfig(), admin_link=False)
        assert not off._is_admin
        assert not hasattr(off, "_token_exchange")

    def test_the_rail_offers_admin_to_admins_only(self, tmp_path, sections):
        admin = _app(tmp_path)
        assert admin._is_admin
        assert [item["key"] for item in admin._shell.rail_items()][-2:] == ["admin", "help"]

        other = _app(tmp_path, auth_config=AuthConfig(admin_groups=frozenset({"admins"})))
        assert not other._is_admin
        assert "admin" not in [item["key"] for item in other._shell.rail_items()]

    def test_without_sign_in_or_token_admin_is_refused(self, tmp_path, sections):
        app = _app(tmp_path, auth_config=AuthConfig())
        assert not app._is_admin
        view = _admin(app)
        assert isinstance(view, EmptyState)
        assert "Admin" in view.title


class TestSections:
    def test_lists_general_and_theme_first(self, tmp_path, sections):
        page = _admin(_app(tmp_path))
        assert list(page.sections)[:2] == ["general", "theme"]
        assert page.section == "general"
        assert isinstance(page.view("general"), SettingsForm)

    def test_the_route_selects_the_section(self, tmp_path, sections):
        app = _app(tmp_path)
        assert _admin(app, "theme").section == "theme"
        assert _admin(app, "unknown").section == "general"

    def test_a_section_registered_through_an_entry_point_appears(
        self, tmp_path, sections, monkeypatch
    ):
        def limits_view(project, editable):
            return pmui.Typography(f"Limits {editable}")

        module = types.ModuleType("fd_admin_ext")
        module.LIMITS = ProjectSection(
            "limits",
            lambda section: dict(section.value),
            title="Limits",
            icon="speed",
            description="Row limits.",
            settings_view=limits_view,
        )
        module.SOURCES = ProjectSection(
            "sources", dict, title="Sources", settings_view=limits_view, group="Data"
        )
        monkeypatch.setitem(sys.modules, "fd_admin_ext", module)
        group = project_module.ENTRY_POINT_GROUP
        entry_points = [
            importlib.metadata.EntryPoint(name.lower(), f"fd_admin_ext:{name}", group)
            for name in ("LIMITS", "SOURCES")
        ]
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda group=None: entry_points if group == project_module.ENTRY_POINT_GROUP else [],
        )
        monkeypatch.setattr(project_module, "_entry_points_loaded", False)

        page = _admin(_app(tmp_path))
        assert list(page.sections) == ["general", "theme", "sources", "limits"]
        assert list(page._menus) == ["App", "Data", "Extensions"]
        assert [item["label"] for item in page._menus["Data"].items] == ["Sources"]
        assert [item["label"] for item in page._menus["Extensions"].items] == ["Limits"]
        page.show("limits")
        assert page._title.object == "Limits"
        assert page.view("limits").object == "Limits True"
        # A view that is not a SettingsForm saves on its own.
        assert not page._save_button.visible

    def test_a_section_whose_view_fails_shows_why(self, tmp_path, sections):
        def broken(project, editable):
            raise RuntimeError("no database")

        project_module.register_section("broken", dict, title="Broken", settings_view=broken)
        page = _admin(_app(tmp_path))
        view = page.view("broken")
        assert isinstance(view, pn.pane.Alert)
        assert "no database" in view.object


class TestRoundTrip:
    def test_saving_writes_the_section_and_a_new_session_uses_it(self, tmp_path, sections):
        page = _admin(_app(tmp_path))
        general = page.view("general")
        general._widgets["history_limit"].value = 100
        general._widgets["edit_workspace"].value = "split"
        assert general.dirty and page.dirty
        assert page.save("general")
        assert not page.dirty

        theme = page.view("theme")
        theme._widgets["primary_color"].value = "#006D77"
        theme._widgets["border_radius"].value = 8
        assert page.save("theme")

        stored = load_project(tmp_path / PROJECT_FILE)
        assert stored.get("general") == {"edit_workspace": "split", "history_limit": 100}
        assert stored.get("theme") == {"primary_color": "#006D77", "border_radius": 8}

        app = _app(tmp_path)
        assert app._editor.history_limit == 100
        assert app._editor.mode == "split"
        theme_config = app._page.theme_config
        assert theme_config["palette"]["primary"] == {"main": "#006D77"}
        assert theme_config["shape"] == {"borderRadius": 8}
        form = _admin(app).view("general")
        assert form.values["history_limit"] == 100
        assert not form.dirty

    def test_discard_puts_the_saved_values_back(self, tmp_path, sections):
        page = _admin(_app(tmp_path))
        form = page.view("general")
        form._widgets["tile_titles"].value = True
        page.discard()
        assert form._widgets["tile_titles"].value is False
        assert not page.dirty
        assert not (tmp_path / PROJECT_FILE).exists()

    def test_other_sections_and_comments_are_kept(self, tmp_path, sections):
        (tmp_path / PROJECT_FILE).write_text("# My app\nmetrics:\n  capacity: 1\n")
        page = _admin(_app(tmp_path))
        page.view("general")._widgets["tile_cards"].value = False
        assert page.save("general")
        text = (tmp_path / PROJECT_FILE).read_text()
        assert text.startswith("# My app\nmetrics:\n  capacity: 1\n")
        assert "general:\n  tile_cards: false\n" in text

    def test_leaving_with_unsaved_settings_asks_first(self, tmp_path, sections, monkeypatch):
        app = _app(tmp_path)
        page = _admin(app)
        monkeypatch.setattr(pn.state, "_location", SimpleNamespace(pathname="/admin/general"))
        page.view("general")._widgets["tile_titles"].value = True
        assert app._has_unsaved_changes()
        assert app._unsaved_changes() == [("settings", "General settings")]


class TestConcurrentAdmins:
    def test_two_admins_saving_keep_each_others_changes(self, tmp_path, sections):
        first = _admin(_app(tmp_path)).view("general")
        second = _admin(_app(tmp_path)).view("general")
        first._widgets["tile_cards"].value = False
        second._widgets["tile_titles"].value = True
        assert first.save()
        assert second.save()
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {
            "tile_cards": False,
            "tile_titles": True,
        }

    def test_a_change_written_by_another_process_is_kept(self, tmp_path, sections):
        form = _admin(_app(tmp_path)).view("theme")
        (tmp_path / PROJECT_FILE).write_text("theme:\n  border_radius: 8\n")
        form._widgets["primary_color"].value = "#5E35B1"
        assert form.save()
        assert load_project(tmp_path / PROJECT_FILE).get("theme") == {
            "border_radius": 8,
            "primary_color": "#5E35B1",
        }
        assert form.values["border_radius"] == 8

    def test_a_setting_both_change_takes_the_last_save(self, tmp_path, sections):
        first = _admin(_app(tmp_path)).view("general")
        second = _admin(_app(tmp_path)).view("general")
        first._widgets["history_limit"].value = 100
        second._widgets["history_limit"].value = 200
        assert first.save() and second.save()
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {"history_limit": 200}


class TestLogo:
    @pytest.mark.parametrize(
        "logo", ["../outside.svg", "assets/../../outside.svg", "/etc/logo.png", "ftp://x/logo.png"]
    )
    def test_a_logo_outside_the_project_directory_is_refused(self, tmp_path, sections, logo):
        page = _admin(_app(tmp_path))
        form = page.view("theme")
        form._widgets["logo"].value = logo
        assert not page.save("theme")
        assert "inside the project directory" in form._texts["logo"].object
        assert not (tmp_path / PROJECT_FILE).exists()

    def test_a_file_written_by_hand_with_such_a_logo_is_not_used(self, tmp_path, sections):
        from panel_flowdash.admin.sections import project_overrides

        project = project_module.Project(
            str(tmp_path / PROJECT_FILE), {"theme": {"logo": "../../etc/passwd"}}
        )
        project._values["theme"] = {"logo": "../../etc/passwd"}
        assert project_overrides(project, lambda target: False) == {}

    @pytest.mark.parametrize("logo", ["logo.svg", "assets/logo.svg", "https://x.org/logo.svg"])
    def test_a_url_or_a_file_inside_is_used(self, tmp_path, sections, logo):
        page = _admin(_app(tmp_path))
        page.view("theme")._widgets["logo"].value = logo
        assert page.save("theme")
        expected = logo if "://" in logo else str((tmp_path / logo).resolve())
        assert _app(tmp_path)._page.logo == expected


class TestValidation:
    def test_a_rejected_value_is_shown_at_its_field(self, tmp_path, sections):
        page = _admin(_app(tmp_path))
        form = page.view("theme")
        form._widgets["primary_color"].value = "teal"
        assert not page.save("theme")
        widget = form._widgets["primary_color"]
        assert widget.error_state
        assert "hex colour" in form._texts["primary_color"].object
        assert [p.keys for p in form.problems] == [("primary_color",)]
        assert page.dirty
        assert not (tmp_path / PROJECT_FILE).exists()

        widget.value = "#C2185B"
        assert page.save("theme")
        assert not widget.error_state
        assert "problem" not in form._texts["primary_color"].object

    def test_problems_in_one_part_of_a_row_mark_that_part(self, tmp_path, sections):
        page = _admin(_app(tmp_path))
        form = page.view("general")
        form._widgets["history_max_days"].value = -1
        assert not page.save("general")
        assert form._widgets["history_max_days"].error_state
        assert not form._widgets["history_limit"].error_state
        assert "at least 0" in form._texts["history_keep"].object

    def test_a_file_the_loader_rejects_starts_from_the_defaults(self, tmp_path, sections):
        (tmp_path / PROJECT_FILE).write_text("theme:\n  border_radius: round\n")
        app = _app(tmp_path)
        form = _admin(app, "theme").view("theme")
        assert form.values["border_radius"] == 4
        assert "is a number" in form._texts["border_radius"].object

    def test_unknown_settings_are_reported_where_they_are_written(self, tmp_path, sections):
        path = tmp_path / PROJECT_FILE
        path.write_text("general:\n  tiles: true\n")
        with pytest.raises(project_module.ProjectError, match=r":2:3: Unknown setting 'tiles'"):
            load_project(path)


class TestLocked:
    def test_settings_given_in_code_are_read_only_with_their_source(self, tmp_path, sections):
        (tmp_path / PROJECT_FILE).write_text("general:\n  home_dashboard: other\n")
        app = _app(
            tmp_path,
            home_dashboard="fleet",
            editor_options={"popup_trigger": "hover"},
            title="Wind",
            settings_sources={"home_dashboard": "__init__.py", "title": "--title"},
        )
        general = _admin(app).view("general")
        assert general.locks == {
            "home_dashboard": "home_dashboard in __init__.py",
            "live_values": "editor_options set in code",
        }
        assert general._widgets["home_dashboard"].disabled
        assert general.values["live_values"] == "hover"
        assert "home_dashboard in __init__.py" in general._texts["home_dashboard"].object
        assert not general._widgets["tile_cards"].disabled
        theme = _admin(app).view("theme")
        assert theme.locks == {"title": "set with --title"}
        assert theme.values["title"] == "Wind"

        # Code wins over the file, and saving keeps what the file has for a locked setting.
        assert app.home_dashboard == "fleet"
        general._widgets["tile_cards"].value = False
        assert _admin(app).save("general")
        assert load_project(tmp_path / PROJECT_FILE).get("general") == {
            "home_dashboard": "other",
            "tile_cards": False,
        }

    def test_an_extension_locks_settings_through_the_project_context(self, tmp_path, sections):
        project = project_module.Project(
            str(tmp_path / PROJECT_FILE),
            context={"locked_settings": {"theme": {"logo": "--logo"}}},
        )
        app = _app(tmp_path, project=project)
        assert _admin(app).view("theme").locks == {"logo": "--logo"}


def test_serve_names_where_each_option_is_set(
    tmp_path, monkeypatch, sections, caplog, capsys, token
):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "__init__.py").write_text(
        "from panel_flowdash.dashboard_store import MemoryDashboardStore\n"
        "store = MemoryDashboardStore()\n"
        "title = 'Wind'\n"
    )
    built = {}
    build_routes = FlowDashApp.build_routes

    def record(**kwargs):
        built["routes"] = build_routes(**kwargs)
        return built["routes"]

    monkeypatch.setattr(FlowDashApp, "build_routes", record)
    monkeypatch.setattr(PanelServe, "invoke", lambda self, args: None)
    monkeypatch.setattr(pn.config, "authorize_callback", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    Serve(parser).invoke(parser.parse_args([str(project_dir), "--home-dashboard", "fleet"]))
    app = built["routes"]["/"]()
    assert app.settings_sources == {"title": "__init__.py", "home_dashboard": "--home-dashboard"}
    assert app._code_setting_source("title") == "title in __init__.py"
    assert app._code_setting_source("home_dashboard") == "set with --home-dashboard"
    out = capsys.readouterr().out
    assert f"/admin#token={access.admin_token()}" in out
    assert access.admin_token() not in caplog.text
    # Without a project file the admin view writes one in the project directory.
    assert app._settings_project().path == str((project_dir / PROJECT_FILE).resolve())


@pytest.mark.parametrize(
    ("project_file", "argv", "printed"),
    [
        (None, [], True),
        ("general:\n  tile_titles: true\n", [], True),
        ("general:\n  admins: [ana]\n", [], False),
        ("general:\n  admins: [ana]\n", ["--admin-link"], True),
    ],
)
def test_serve_prints_the_admin_link_while_no_one_is_admin(
    tmp_path, monkeypatch, sections, capsys, token, project_file, argv, printed
):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "__init__.py").write_text(
        "from panel_flowdash.dashboard_store import MemoryDashboardStore\n"
        "store = MemoryDashboardStore()\n"
    )
    if project_file is not None:
        (project_dir / PROJECT_FILE).write_text(project_file)
    monkeypatch.setattr(PanelServe, "invoke", lambda self, args: None)
    monkeypatch.setattr(pn.config, "authorize_callback", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    Serve(parser).invoke(parser.parse_args([str(project_dir), "--port", "5999", *argv]))
    out = capsys.readouterr().out
    assert (f"http://localhost:5999/admin#token={token}" in out) is printed
