"""Tests for the project file: parsing, discovery, the section registry and the CLI flag."""

import argparse
import importlib.metadata
import logging
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import panel as pn
import pytest
from panel.command.serve import Serve as PanelServe

from panel_flowdash import project as project_module
from panel_flowdash.app import FlowDashApp
from panel_flowdash.command.serve import Serve
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.project import (
    PROJECT_FILE,
    Project,
    ProjectError,
    ProjectSection,
    SectionError,
    find_project,
    load_project,
    parse_project,
    register_section,
    sections,
    unregister_section,
)


@pytest.fixture
def registry(monkeypatch):
    """Give each test its own section registry, with no entry points."""
    monkeypatch.setattr(project_module, "_SECTIONS", {})
    monkeypatch.setattr(project_module, "_entry_points_loaded", True)
    return project_module._SECTIONS


def _limits(section):
    value = section.value
    if not isinstance(value, dict):
        raise SectionError("`limits` is a mapping of names to numbers.")
    for name, limit in value.items():
        if not isinstance(limit, int):
            raise SectionError(f"Limit {name!r} must be a whole number.", path=(name,))
    return dict(value)


def _write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


class TestParse:
    def test_registered_section_is_loaded(self, registry):
        register_section("limits", _limits)
        project = parse_project("limits:\n  rows: 10\n")
        assert project.get("limits") == {"rows": 10}
        assert project.raw("limits") == {"rows": 10}
        assert "limits" in project

    def test_missing_section_gives_the_default(self, registry):
        register_section("limits", _limits)
        assert parse_project("").get("limits", {}) == {}

    def test_unknown_section_is_ignored_with_a_warning(self, registry, caplog):
        register_section("limits", _limits)
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.project"):
            project = parse_project("limits:\n  rows: 1\nfuture:\n  x: 1\n", "flowdash.yaml")
        assert project.get("limits") == {"rows": 1}
        assert "flowdash.yaml:3:1: Ignoring the unknown section 'future'" in caplog.text
        assert "`limits`" in caplog.text

    def test_loader_error_points_to_the_value(self, registry):
        register_section("limits", _limits)
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  rows: 10\n  cols: many\n", "p.yaml")
        (problem,) = info.value.problems
        assert (problem.path, problem.line, problem.column) == ("p.yaml", 3, 9)
        assert str(problem) == "p.yaml:3:9: Limit 'cols' must be a whole number."

    def test_section_error_at_a_key(self, registry):
        def loader(section):
            raise SectionError("Unknown limit.", path=("cols",), key=True)

        register_section("limits", loader)
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  rows: 1\n  cols: 2\n")
        assert (info.value.problems[0].line, info.value.problems[0].column) == (3, 3)

    def test_several_problems_from_one_loader(self, registry):
        def loader(section):
            raise ProjectError([section.problem((name,), f"bad {name}") for name in section.value])

        register_section("limits", loader)
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  a: 1\n  b: 2\n")
        assert [(p.message, p.line) for p in info.value.problems] == [("bad a", 2), ("bad b", 3)]

    def test_problems_of_every_section_are_reported(self, registry):
        register_section("limits", _limits)
        register_section("other", lambda section: int(section.value))
        with pytest.raises(ProjectError) as info:
            parse_project("limits: 3\nother: text\n")
        assert [p.line for p in info.value.problems] == [1, 2]
        assert "Section 'other': invalid literal" in str(info.value)

    def test_duplicate_keys_are_errors(self, registry):
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  rows: 1\n  rows: 2\n", "p.yaml")
        assert str(info.value) == "p.yaml:3:3: 'rows' is given twice."

    def test_duplicate_sections_are_errors(self, registry):
        with pytest.raises(ProjectError, match=r":3:1: 'a' is given twice"):
            parse_project("a: 1\nb: 2\na: 3\n", "p.yaml")

    def test_invalid_yaml_points_to_the_problem(self, registry):
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  rows: [1, 2\n", "p.yaml")
        problem = info.value.problems[0]
        assert problem.message.startswith("Not valid YAML")
        assert problem.line == 3

    def test_file_must_be_a_mapping(self, registry):
        with pytest.raises(ProjectError, match="mapping of sections"):
            parse_project("- a\n- b\n")

    def test_section_registered_after_loading_is_loaded_on_get(self, registry):
        project = parse_project("limits:\n  rows: x\n", "p.yaml")
        assert project.get("limits") == {"rows": "x"}
        register_section("limits", _limits)
        with pytest.raises(ProjectError, match=r"p.yaml:2:9"):
            project.get("limits")

    def test_load_project_reports_a_missing_file(self, tmp_path, registry):
        with pytest.raises(ProjectError, match="Cannot read the project file"):
            load_project(tmp_path / "missing.yaml")

    def test_load_project_names_the_file(self, tmp_path, registry):
        register_section("limits", _limits)
        path = _write(tmp_path / PROJECT_FILE, "limits:\n  rows: x\n")
        with pytest.raises(ProjectError, match=rf"^{path}:2:9: "):
            load_project(path)
        assert load_project(_write(path, "limits:\n  rows: 2\n")).directory == tmp_path


class TestRegistry:
    def test_register_a_project_section(self, registry):
        section = ProjectSection("limits", _limits, title="Limits", icon="speed")
        assert register_section(section) is section
        assert sections() == {"limits": section}
        unregister_section("limits")
        assert sections() == {}

    def test_register_by_name_and_fields(self, registry):
        section = register_section("row_limits", _limits, description="Row limits.")
        assert section.label == "Row limits"
        assert section.description == "Row limits."

    def test_register_rejects_mixed_arguments(self, registry):
        with pytest.raises(TypeError):
            register_section(ProjectSection("a", _limits), _limits)
        with pytest.raises(TypeError):
            register_section("a")

    def test_registered_from_an_extension_module(self, registry, tmp_path, monkeypatch):
        (tmp_path / "fd_ext_limits.py").write_text(
            "from panel_flowdash.project import SectionError, register_section\n"
            "def load(section):\n"
            "    return {k: int(v) for k, v in section.value.items()}\n"
            "register_section('limits', load, title='Limits')\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "fd_ext_limits", raising=False)
        __import__("fd_ext_limits")
        assert parse_project("limits:\n  rows: '5'\n").get("limits") == {"rows": 5}

    def test_entry_points_are_registered(self, registry, monkeypatch):
        module = types.ModuleType("fd_ext_entry")
        module.single = ProjectSection("limits", _limits)
        module.several = lambda: [ProjectSection("a", _limits), ProjectSection("b", _limits)]
        module.wrong = "not a section"
        monkeypatch.setitem(sys.modules, "fd_ext_entry", module)
        group = project_module.ENTRY_POINT_GROUP
        entry_points = [
            importlib.metadata.EntryPoint(name, f"fd_ext_entry:{name}", group)
            for name in ("single", "several", "wrong")
        ]
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda group=None: entry_points if group == project_module.ENTRY_POINT_GROUP else [],
        )
        monkeypatch.setattr(project_module, "_entry_points_loaded", False)
        assert set(sections()) == {"limits", "a", "b"}


class TestWriteBack:
    def test_set_writes_the_section_and_keeps_the_rest(self, registry, tmp_path, caplog):
        register_section(
            "limits", lambda s: tuple(s.value.items()), dump=lambda value: dict(value)
        )
        path = _write(
            tmp_path / PROJECT_FILE,
            """
            # Shared settings
            metrics:  # kept as written
              capacity: {table: plants, sum: mw}
            limits:
              rows: 1
            """,
        )
        project = load_project(path)
        project.set("limits", (("rows", 5), ("cols", 2)))

        text = path.read_text()
        assert "# Shared settings" in text
        assert "metrics:  # kept as written" in text
        assert "capacity: {table: plants, sum: mw}" in text
        assert project.get("limits") == (("rows", 5), ("cols", 2))
        assert load_project(path).raw("limits") == {"rows": 5, "cols": 2}
        assert project.position(("limits", "cols")) == (6, 9)

    def test_set_creates_the_file(self, registry, tmp_path):
        path = tmp_path / PROJECT_FILE
        project = Project(str(path))
        project.set("limits", {"rows": 1})
        assert load_project(path).raw("limits") == {"rows": 1}

    def test_set_refuses_data_that_reads_back_differently(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "a: 1\n")
        with pytest.raises(ProjectError, match="does not read back"):
            load_project(path).set("flags", {"mode": "on"})
        assert path.read_text() == "a: 1\n"

    def test_set_without_a_file_stays_in_memory(self, registry):
        project = parse_project("a: 1\n")
        project.set("a", 2)
        assert project.get("a") == 2


class TestDiscovery:
    def test_none_without_a_file(self, tmp_path):
        assert find_project([tmp_path], cwd=tmp_path) is None

    def test_beside_the_first_place_that_has_one(self, tmp_path):
        first, second, cwd = (tmp_path / name for name in ("first", "second", "cwd"))
        for directory in (first, second, cwd):
            directory.mkdir()
        data_file = second / "data.csv"
        data_file.write_text("a\n1\n")
        (second / PROJECT_FILE).write_text("")
        (cwd / PROJECT_FILE).write_text("")
        places = [first, "https://example.com/data.csv", 42, data_file]
        assert find_project(places, cwd=cwd) == (second / PROJECT_FILE).resolve()
        (first / PROJECT_FILE).write_text("")
        assert find_project(places, cwd=cwd) == (first / PROJECT_FILE).resolve()

    def test_falls_back_to_the_working_directory(self, tmp_path, monkeypatch):
        served = tmp_path / "served"
        served.mkdir()
        (tmp_path / PROJECT_FILE).write_text("")
        monkeypatch.chdir(tmp_path)
        assert find_project([served]) == (tmp_path / PROJECT_FILE).resolve()


def _project_dir(tmp_path: Path) -> Path:
    project_dir = tmp_path / "served"
    section = project_dir / "Pages"
    section.mkdir(parents=True)
    (section / "page.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=True, title='Overview')\n"
        "def app():\n"
        "    return 'page'\n"
    )
    (project_dir / "__init__.py").write_text(
        "from panel_flowdash.dashboard_store import MemoryDashboardStore\n"
        "from panel_flowdash.project import register_section\n"
        "store = MemoryDashboardStore()\n"
        "register_section('limits', lambda s: dict(s.value))\n"
    )
    return project_dir


class TestApp:
    def test_app_loads_a_project_path(self, registry, tmp_path):
        register_section("limits", _limits)
        path = _write(tmp_path / PROJECT_FILE, "limits:\n  rows: 3\n")
        app = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore(), project=path)
        assert isinstance(app.project, Project)
        assert app.project.get("limits") == {"rows": 3}

    def test_build_routes_loads_the_project_once(self, registry, tmp_path):
        register_section("limits", _limits)
        path = _write(tmp_path / PROJECT_FILE, "limits:\n  rows: 3\n")
        routes = FlowDashApp.build_routes(
            project_dir=_project_dir(tmp_path), store=MemoryDashboardStore(), project=path
        )
        first, second = routes["/"](), routes["/"]()
        assert first.project is second.project
        assert first.project.get("limits") == {"rows": 3}


class TestCLI:
    @pytest.fixture
    def invoke(self, monkeypatch, registry):
        built = {}
        build_routes = FlowDashApp.build_routes

        def record_build_routes(**kwargs):
            built["routes"] = build_routes(**kwargs)
            return built["routes"]

        monkeypatch.setattr(FlowDashApp, "build_routes", record_build_routes)
        monkeypatch.setattr(PanelServe, "invoke", lambda self, args: None)
        monkeypatch.setattr(pn.config, "authorize_callback", None)
        monkeypatch.setattr(sys, "path", list(sys.path))

        def run(*argv):
            parser = argparse.ArgumentParser()
            serve = Serve(parser)
            serve.invoke(parser.parse_args([str(a) for a in argv]))
            return built["routes"]["/"]().project

        return run

    def test_has_the_project_flag(self):
        assert "--project" in {name for name, _ in Serve.args}

    def test_project_flag_is_relative_to_the_working_directory(
        self, tmp_path, monkeypatch, invoke
    ):
        project_dir = _project_dir(tmp_path)
        (project_dir / PROJECT_FILE).write_text("limits:\n  rows: 1\n")
        _write(tmp_path / "other.yaml", "limits:\n  rows: 2\n")
        monkeypatch.chdir(tmp_path)
        project = invoke(project_dir, "--project", "other.yaml")
        assert project.path == str((tmp_path / "other.yaml").resolve())
        assert project.get("limits") == {"rows": 2}

    def test_discovers_the_file_in_the_served_directory(self, tmp_path, monkeypatch, invoke):
        project_dir = _project_dir(tmp_path)
        (project_dir / PROJECT_FILE).write_text("limits:\n  rows: 1\n")
        (tmp_path / PROJECT_FILE).write_text("limits:\n  rows: 2\n")
        monkeypatch.chdir(tmp_path)
        assert invoke(project_dir).get("limits") == {"rows": 1}

    def test_discovers_the_file_in_the_working_directory(self, tmp_path, monkeypatch, invoke):
        project_dir = _project_dir(tmp_path)
        (tmp_path / PROJECT_FILE).write_text("limits:\n  rows: 2\n")
        monkeypatch.chdir(tmp_path)
        assert invoke(project_dir).get("limits") == {"rows": 2}

    def test_no_project_file(self, tmp_path, monkeypatch, invoke):
        project_dir = _project_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert invoke(project_dir) is None

    def test_invalid_file_fails_at_startup_with_its_position(self, tmp_path):
        project_dir = _project_dir(tmp_path)
        (project_dir / PROJECT_FILE).write_text("limits:\n  rows: 1\n  rows: 2\n")
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash", "serve", str(project_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 1
        expected = f"ERROR: {(project_dir / PROJECT_FILE).resolve()}:3:3: 'rows' is given twice."
        assert expected in result.stderr


def test_how_to_examples_run(registry, tmp_path):
    from tests.test_docs_examples import ROOT, run, snippets

    namespace = run("\n".join(snippets(ROOT / "docs" / "how-to" / "project-sections.md")))
    path = _write(tmp_path / PROJECT_FILE, "# kept\nbranding:\n  logo: 42\n")
    with pytest.raises(ProjectError, match=r":3:9: `logo` is the path of an image"):
        load_project(path)
    _write(path, "# kept\nbranding:\n  logo: logo.svg\n")
    project = load_project(path)
    register_section(namespace["BRANDING"])
    view = namespace["branding_settings"](project, True)
    view.objects[0].value = "new.svg"
    view.objects[1].clicks += 1
    assert load_project(path).get("branding") == namespace["Branding"]("new.svg")
    assert path.read_text().startswith("# kept\n")
