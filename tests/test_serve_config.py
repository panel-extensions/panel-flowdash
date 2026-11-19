"""Tests for the project configuration `flowdash serve` reads from `__init__.py`."""

import argparse
import re
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
        calls["built"] = build_routes(**kwargs)
        return calls["built"]

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


@pytest.fixture
def library(tmp_path_factory, monkeypatch):
    """An installed library offering one component, importable as ``cfg_library``."""
    root = tmp_path_factory.mktemp("site")
    (root / "cfg_library.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True)\n"
        "def table(config):\n"
        "    return 'table'\n\n"
        "__flowdash_components__ = {'Library/table': table}\n"
    )
    monkeypatch.syspath_prepend(str(root))
    yield "cfg_library"
    sys.modules.pop("cfg_library", None)


async def test_component_library_flag_offers_its_components(project, library, monkeypatch):
    calls = _serve(project, monkeypatch, "--component-library", library)

    assert calls["routes"]["component_libraries"] == [library]
    app = calls["built"]["/"]()
    assert {"CfgSection/cfg_chart", "Library/table"} <= set(app._component_entries)


def test_component_library_flag_extends_project_libraries(project, library, monkeypatch):
    _write_init(project, "component_libraries = ['cfg_library:__flowdash_components__']\n")

    calls = _serve(
        project,
        monkeypatch,
        "--component-library",
        "cfg_library:__flowdash_components__",
        "--component-library",
        library,
    )

    assert calls["routes"]["component_libraries"] == [
        "cfg_library:__flowdash_components__",
        library,
    ]


def test_unusable_component_library_exits_with_an_error(project, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc_info:
        _serve(project, monkeypatch, "--component-library", "cfg_missing_library")

    assert exc_info.value.code == 1
    assert "Cannot import component library 'cfg_missing_library'" in capsys.readouterr().err


def test_agent_model_flags_set_the_models_and_the_default(project, monkeypatch):
    _write_init(
        project,
        'agent_model = "openai:project"\nagent_models = ["openai:project", "openai:other"]\n',
    )

    kwargs = _serve(project, monkeypatch)["routes"]
    assert kwargs["agent_model"] == "openai:project"
    assert kwargs["agent_models"] == ["openai:project", "openai:other"]

    kwargs = _serve(
        project,
        monkeypatch,
        "--agent-model",
        "openai:fast=Fast",
        "--agent-model",
        "anthropic:deep",
    )["routes"]
    assert kwargs["agent_model"] == "openai:fast"
    assert kwargs["agent_models"] == [
        {"id": "openai:fast", "label": "Fast"},
        {"id": "anthropic:deep"},
    ]


def test_cli_models_split_on_commas_and_beat_a_project_agent_string(project, monkeypatch):
    _write_init(project, 'agent = "openai:project"\n')
    kwargs = _serve(project, monkeypatch, "--agent-model", "openai:a=A,openai:b")["routes"]
    assert "agent" not in kwargs and kwargs["agent_model"] == "openai:a"
    assert kwargs["agent_models"] == [{"id": "openai:a", "label": "A"}, {"id": "openai:b"}]


@pytest.mark.parametrize(
    ("init", "cli", "env", "message"),
    [
        ('agent_models = ["test", {"label": "x"}]\n', [], None, "agent_models\\[1\\]"),
        ('agent_models = [{"id": "a", "price": 1}]\n', [], None, "unknown keys 'price'"),
        ("", ["--agent-model", "=Fast"], None, "--agent-model: '=Fast' names no model"),
        ("", [], "a,=B", "FLOWDASH_AGENT_MODELS: '=B' names no model"),
    ],
)
def test_serve_refuses_invalid_models_at_startup(
    project, monkeypatch, capsys, init, cli, env, message
):
    _write_init(project, init)
    if env is not None:
        monkeypatch.setenv("FLOWDASH_AGENT_MODELS", env)
    with pytest.raises(SystemExit):
        _serve(project, monkeypatch, *cli)
    assert re.search(message, capsys.readouterr().err)
