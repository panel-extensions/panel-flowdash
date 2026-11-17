"""Tests for the project configuration `flowdash serve` reads from `__init__.py`."""

import argparse
import sys
import textwrap

import panel as pn
import pytest
from panel.command.serve import Serve as PanelServe

import panel_flowdash.command.serve as serve_module
from panel_flowdash.app import FlowDashApp
from panel_flowdash.command.serve import PROJECT_OPTIONS, Serve

PROJECT_INIT = """
import param

from panel_flowdash import DashboardSettings, FlowDash, MemoryDashboardStore

store = MemoryDashboardStore()


class ProjectSettings(DashboardSettings):
    currency = param.Selector(default="EUR", objects=["EUR", "USD"])


class ProjectFlowDash(FlowDash):
    pass


def configure_layout(app, content, route):
    pass


breakpoints = [500, 900]
controls_position = "left"
editor_class = ProjectFlowDash
editor_options = {"popup_trigger": "click"}
header_layout = "full"
home_dashboard = "sales"
include_builtin_components = False
nav_variant = "menubar"
notifications = False
page_options = {"sidebar_width": 333}
propagate_failures = True
settings_class = ProjectSettings
title = "Project Title"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A served project directory with a single component, and no ``__init__.py`` yet."""
    section = tmp_path / "CfgSection"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "cfg_chart.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True)\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(pn.config, "authorize_callback", None)
    monkeypatch.setattr(PanelServe, "invoke", lambda self, args: None)
    yield tmp_path
    for name in [m for m in sys.modules if m == "CfgSection" or m.startswith("CfgSection.")]:
        del sys.modules[name]


def _serve(project_dir, monkeypatch, *cli_args):
    """Run ``flowdash serve`` up to starting the server and return what it built."""
    calls = {}
    build_routes = FlowDashApp.build_routes

    def record_build_routes(**kwargs):
        calls["routes"] = kwargs
        return build_routes(**kwargs)

    def record_build_applications(routes, title, location):
        calls["title"] = title
        return {}

    monkeypatch.setattr(FlowDashApp, "build_routes", record_build_routes)
    monkeypatch.setattr(serve_module, "build_applications", record_build_applications)
    parser = argparse.ArgumentParser()
    serve = Serve(parser)
    serve.invoke(parser.parse_args([str(project_dir), *cli_args]))
    return calls


def _write_init(project_dir, source):
    (project_dir / "__init__.py").write_text(textwrap.dedent(source))


def test_every_option_is_a_flowdash_app_parameter():
    assert set(PROJECT_OPTIONS) <= set(FlowDashApp.param)
    assert list(PROJECT_OPTIONS) == sorted(PROJECT_OPTIONS)


def test_project_options_reach_build_routes(project, monkeypatch):
    _write_init(project, PROJECT_INIT)

    calls = _serve(project, monkeypatch)

    kwargs = calls["routes"]
    assert kwargs["breakpoints"] == [500, 900]
    assert kwargs["controls_position"] == "left"
    assert kwargs["editor_class"].__name__ == "ProjectFlowDash"
    assert kwargs["editor_options"] == {"popup_trigger": "click"}
    assert kwargs["header_layout"] == "full"
    assert kwargs["home_dashboard"] == "sales"
    assert kwargs["include_builtin_components"] is False
    assert kwargs["nav_variant"] == "menubar"
    assert kwargs["notifications"] is False
    assert kwargs["page_options"] == {"sidebar_width": 333}
    assert kwargs["propagate_failures"] is True
    assert kwargs["settings_class"].__name__ == "ProjectSettings"
    assert kwargs["title"] == "Project Title"
    assert callable(kwargs["configure_layout"])
    assert calls["title"] == "Project Title"


async def test_project_options_reach_the_app_and_editor(project, monkeypatch):
    _write_init(project, PROJECT_INIT.replace("propagate_failures = True", ""))

    kwargs = _serve(project, monkeypatch)["routes"]
    app = FlowDashApp.build_routes(**kwargs)["/"]()

    editor = app._editor
    assert type(editor).__name__ == "ProjectFlowDash"
    assert editor.popup_trigger == "click"
    assert editor.breakpoints == [500, 900]
    assert editor.controls_position == "left"
    assert editor.header_layout == "full"
    assert editor.include_builtin_components is False
    assert editor.notifications is False
    assert editor.settings_class.__name__ == "ProjectSettings"
    assert editor.settings.currency == "EUR"
    assert app._page.sidebar_width == 333
    assert app.title == "Project Title"
    assert app.nav_variant == "menubar"
    assert app.home_dashboard == "sales"


@pytest.mark.failures
async def test_propagate_failures_reaches_the_editor(project, monkeypatch):
    _write_init(project, "propagate_failures = True\nnotifications = False\n")

    kwargs = _serve(project, monkeypatch)["routes"]
    app = FlowDashApp.build_routes(**kwargs)["/"]()

    assert app._editor.propagate_failures is True


def test_cli_flags_override_project_options(project, monkeypatch):
    _write_init(project, PROJECT_INIT)

    calls = _serve(
        project,
        monkeypatch,
        "--title",
        "CLI Title",
        "--nav-variant",
        "left",
        "--home-dashboard",
        "ops",
    )

    kwargs = calls["routes"]
    assert kwargs["title"] == "CLI Title"
    assert kwargs["nav_variant"] == "left"
    assert kwargs["home_dashboard"] == "ops"
    assert calls["title"] == "CLI Title"


def test_defaults_without_project_init(project, monkeypatch):
    calls = _serve(project, monkeypatch)

    assert not set(PROJECT_OPTIONS) & set(calls["routes"])
    assert calls["title"] == "FlowDash"


def test_none_leaves_an_option_unset(project, monkeypatch):
    _write_init(project, "title = None\nsettings_class = None\n")

    calls = _serve(project, monkeypatch)

    assert "settings_class" not in calls["routes"]
    assert calls["title"] == "FlowDash"


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("settings_class = int", "settings_class"),
        ("settings_class = __import__('panel_flowdash').DashboardSettings()", "settings_class"),
        ("editor_class = object", "editor_class"),
        ("editor_options = [('popup_trigger', 'click')]", "editor_options"),
        ("breakpoints = 768", "breakpoints"),
        ("propagate_failures = 'yes'", "propagate_failures"),
        ("nav_variant = 'top'", "nav_variant"),
        ("configure_layout = 'sidebar'", "configure_layout"),
    ],
)
def test_invalid_option_exits_with_an_error(project, monkeypatch, capsys, source, name):
    _write_init(project, source)

    with pytest.raises(SystemExit) as exc_info:
        _serve(project, monkeypatch)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert f"invalid `{name}` in {project / '__init__.py'}" in err
