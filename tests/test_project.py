"""Tests for the project file: parsing, discovery, the section registry and the CLI flag."""

import argparse
import concurrent.futures
import dataclasses
import importlib.metadata
import logging
import re
import subprocess
import sys
import textwrap
import threading
import time
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
    monkeypatch.setattr(project_module, "_entry_points_loading", False)
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

    def test_a_loader_error_masks_credentials(self, registry):
        def loader(section):
            raise ValueError(f"cannot reach postgresql://u:hunter2@h/db for {section.name}")

        register_section("db", loader)
        with pytest.raises(ProjectError) as info:
            parse_project("db:\n  uri: x\n")
        (problem,) = info.value.problems
        assert problem.message == "Section 'db': cannot reach postgresql://u:***@h/db for db"

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
            parse_project("limits:\n  rows: 1\n  rows: 2\n  rows: 3\n", "p.yaml")
        assert (
            str(info.value)
            == "p.yaml:3:3: 'rows' is given twice.\np.yaml:4:3: 'rows' is given twice."
        )
        assert (info.value.problems[0].section, info.value.problems[0].keys) == (
            "limits",
            ("rows",),
        )

    def test_problems_name_their_section_and_keys(self, registry):
        register_section("limits", _limits)
        with pytest.raises(ProjectError) as info:
            parse_project("limits:\n  cols: many\n")
        problem = info.value.problems[0]
        assert (problem.section, problem.keys) == ("limits", ("cols",))

    def test_loaders_get_the_context_and_can_warn(self, registry, caplog):
        def loader(section):
            sources = section.context["sources"]
            kept = {}
            for name, table in section.value.items():
                if table in sources:
                    kept[name] = table
                else:
                    section.warn((name,), f"{name!r} is left out: no source has {table!r}.")
            return kept

        register_section("metrics", loader)
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.project"):
            project = parse_project(
                "metrics:\n  a: plants\n  b: missing\n", "p.yaml", context={"sources": {"plants"}}
            )
        assert project.get("metrics") == {"a": "plants"}
        (warning,) = project.warnings
        assert str(warning) == "p.yaml:3:6: 'b' is left out: no source has 'missing'."
        assert (warning.section, warning.keys) == ("metrics", ("b",))
        assert "left out" in caplog.text

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
        with pytest.raises(ProjectError, match=rf"^{re.escape(str(path))}:2:9: "):
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

    def test_concurrent_lookups_see_every_entry_point(self, registry, monkeypatch):
        def slow():
            time.sleep(0.2)
            return [ProjectSection("a", _limits), ProjectSection("b", _limits)]

        module = types.ModuleType("fd_ext_slow")
        module.slow = slow
        monkeypatch.setitem(sys.modules, "fd_ext_slow", module)
        group = project_module.ENTRY_POINT_GROUP
        entry_point = importlib.metadata.EntryPoint("slow", "fd_ext_slow:slow", group)
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda group=None: [entry_point] if group == project_module.ENTRY_POINT_GROUP else [],
        )
        monkeypatch.setattr(project_module, "_entry_points_loaded", False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            seen = list(pool.map(lambda _: set(sections()), range(8)))
        assert seen == [{"a", "b"}] * 8


class TestEnv:
    def test_expand_env(self):
        env = {"KEY": "secret", "EMPTY": ""}
        assert project_module.expand_env("${KEY}", env) == "secret"
        assert project_module.expand_env("pre-${KEY}-${EMPTY}", env) == "pre-secret-"
        assert project_module.expand_env("${MISSING:-fallback}", env) == "fallback"
        assert project_module.expand_env({"a": ["${KEY}", 1]}, env) == {"a": ["secret", 1]}
        with pytest.raises(ValueError, match="MISSING is not set"):
            project_module.expand_env("${MISSING}", env)

    def test_dump_keeps_the_reference_not_the_secret(self, registry, tmp_path, monkeypatch):
        @dataclasses.dataclass
        class Agent:
            api_key: str
            model: str

            @property
            def resolved_key(self):
                return project_module.expand_env(self.api_key)

        register_section(
            "agent",
            lambda section: Agent(**section.value),
            dump=dataclasses.asdict,
        )
        monkeypatch.setenv("FD_TEST_API_KEY", "sk-very-secret")
        path = _write(
            tmp_path / PROJECT_FILE, "agent:\n  api_key: ${FD_TEST_API_KEY}\n  model: a\n"
        )
        project = load_project(path)
        agent = project.get("agent")
        assert agent.resolved_key == "sk-very-secret"
        project.set("agent", dataclasses.replace(agent, model="b"))
        text = path.read_text()
        assert "${FD_TEST_API_KEY}" in text
        assert "sk-very-secret" not in text


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

    def test_ambiguous_text_round_trips_as_text(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "a: 1\n")
        values = {"mode": "on", "answer": "no", "ratio": "1:20", "n": "1", "flag": True}
        load_project(path).set("flags", values)
        assert load_project(path).raw("flags") == values
        text = path.read_text()
        assert 'mode: "on"' in text
        assert 'ratio: "1:20"' in text
        assert "flag: true" in text

    def test_yaml_1_2_reads_no_and_sexagesimal_as_text(self, registry):
        assert parse_project("a: [no, on, 1:20, true]\n").raw("a") == ["no", "on", "1:20", True]

    def test_tuples_compare_as_lists_on_read_back(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "")
        project = load_project(path)
        project.set("limits", {"pairs": (("a", 1), ("b", 2))})
        assert load_project(path).raw("limits") == {"pairs": [["a", 1], ["b", 2]]}

    def test_untouched_sections_keep_their_formatting(self, registry, tmp_path):
        other = (
            "# leading comment\n"
            "metrics:\n"
            "    capacity:   {table: plants}   # odd spacing\n"
            "    list:\n"
            "        - a\n"
            "        -   b\n"
            "\n"
            "# about limits\n"
        )
        tail = "\nlast:\n      - x\n"
        path = _write(tmp_path / PROJECT_FILE, other + "limits:\n  rows: 1\n" + tail)
        long_text = "word " * 40
        load_project(path).set("limits", {"rows": 2, "note": long_text})
        text = path.read_text()
        assert text.startswith(other + "limits:\n")
        assert text.endswith(tail)
        assert load_project(path).raw("limits")["note"] == long_text
        assert any(long_text.strip() in line for line in text.splitlines())

    def test_set_validates_with_the_loader_before_writing(self, registry, tmp_path):
        register_section("limits", _limits, dump=lambda value: value)
        original = "# keep\nlimits:\n  rows: 1\n"
        path = _write(tmp_path / PROJECT_FILE, original)
        project = load_project(path)
        with pytest.raises(ProjectError) as info:
            project.set("limits", {"rows": 1, "cols": "many"})
        (problem,) = info.value.problems
        assert (problem.line, problem.column) == (4, 9)
        assert (problem.section, problem.keys) == ("limits", ("cols",))
        assert path.read_text() == original
        assert project.get("limits") == {"rows": 1}

    def test_set_leaves_no_temporary_file(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "a: 1\n")
        load_project(path).set("b", 2)
        assert load_project(path).data == {"a": 1, "b": 2}
        assert [p.name for p in tmp_path.iterdir()] == [PROJECT_FILE]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and symlinks")
    def test_set_keeps_the_file_mode_and_follows_symlinks(self, registry, tmp_path):
        target = _write(tmp_path / "real.yaml", "a: 1\n")
        target.chmod(0o640)
        link = tmp_path / PROJECT_FILE
        link.symlink_to(target)
        load_project(link).set("b", 2)
        assert link.is_symlink()
        assert (target.stat().st_mode & 0o777) == 0o640
        assert load_project(target).raw("b") == 2
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_loader_may_load_a_project_file_inside_set(self, registry, tmp_path):
        other = _write(tmp_path / "other.yaml", "shared: 1\n")
        register_section("linked", lambda section: load_project(other).raw("shared"))
        path = _write(tmp_path / PROJECT_FILE, "linked: {}\n")
        project = load_project(path)
        errors = []

        def write():
            try:
                project.set("linked", {"changed": True})
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "Project.set deadlocked"
        assert errors == []
        assert project.get("linked") == 1

    def test_sharing_violations_are_retried_on_windows(self, monkeypatch):
        monkeypatch.setattr(project_module.os, "name", "nt")
        monkeypatch.setattr(project_module.time, "sleep", lambda seconds: None)
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, "Access is denied")
            return "done"

        assert project_module._retry_sharing(flaky) == "done"
        assert len(attempts) == 3

    def test_a_lasting_sharing_violation_is_raised(self, monkeypatch):
        monkeypatch.setattr(project_module.os, "name", "nt")
        monkeypatch.setattr(project_module, "_SHARING_RETRY_SECONDS", 0.05)

        def locked():
            raise PermissionError(13, "Access is denied")

        with pytest.raises(PermissionError):
            project_module._retry_sharing(locked)

    def test_concurrent_setters_lose_no_update(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "# shared\n")

        def setter(i):
            load_project(path).set(f"s{i}", {"value": i})

        with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
            list(pool.map(setter, range(40)))
        data = load_project(path).data
        assert data == {f"s{i}": {"value": i} for i in range(40)}
        assert path.read_text().startswith("# shared\n")

    def test_set_refreshes_sections_others_changed(self, registry, tmp_path):
        path = _write(tmp_path / PROJECT_FILE, "a: 1\nb: 1\n")
        mine, theirs = load_project(path), load_project(path)
        theirs.set("b", 2)
        mine.set("a", 3)
        assert mine.data == {"a": 3, "b": 2}

    def test_set_without_a_file_stays_in_memory(self, registry):
        project = parse_project("a: 1\n")
        project.set("a", 2)
        assert project.get("a") == 2

    def test_set_keeps_comments_and_merge_keys_of_a_file_written_whole(
        self, registry, tmp_path, caplog
    ):
        original = "base: &base\n  limits: {rows: 1}  # base\n<<: *base\n# end\nother: 3  # o\n"
        path = _write(tmp_path / PROJECT_FILE, original)
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.project"):
            load_project(path).set("extra", {"rows": 5})
        assert path.read_text() == original + "extra:\n  rows: 5\n"
        assert load_project(path).raw("extra") == {"rows": 5}
        assert "dropped" not in caplog.text

    def test_set_warns_when_a_merged_key_drops_the_comments(self, registry, tmp_path, caplog):
        original = "base: &base\n  limits: {rows: 1}  # base\n<<: *base\n"
        path = _write(tmp_path / PROJECT_FILE, original)
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.project"):
            load_project(path).set("limits", {"rows": 5})
        assert load_project(path).data == {"base": {"limits": {"rows": 1}}, "limits": {"rows": 5}}
        assert "dropped 1 comment(s), e.g. '# base'" in caplog.text

    def test_set_warns_about_comments_a_flow_mapping_drops(self, registry, tmp_path, caplog):
        path = _write(tmp_path / PROJECT_FILE, "{a: 1,  # about a\n b: 2}\n")
        with caplog.at_level(logging.WARNING, logger="panel_flowdash.project"):
            load_project(path).set("a", 3)
        assert load_project(path).data == {"a": 3, "b": 2}
        assert "dropped 1 comment(s), e.g. '# about a'" in caplog.text

    @pytest.mark.parametrize(
        "other, user", [("other: *l\n", "'other'"), ("other:\n  <<: *l\n", "'other'")]
    )
    def test_set_refuses_to_drop_an_anchor_aliased_elsewhere(
        self, registry, tmp_path, other, user
    ):
        original = "limits: &l\n  rows: 1\n" + other
        path = _write(tmp_path / PROJECT_FILE, original)
        project = load_project(path)
        with pytest.raises(ProjectError) as info:
            project.set("limits", {"rows": 2})
        (problem,) = info.value.problems
        assert problem.section == "limits"
        assert f"anchor &l, which {user} refers to with the alias *l" in problem.message
        assert path.read_text() == original

    def test_set_replaces_a_section_that_aliases_an_anchor_defined_before(
        self, registry, tmp_path
    ):
        path = _write(tmp_path / PROJECT_FILE, "base: &b\n  rows: 1\nlimits: *b\n")
        load_project(path).set("limits", {"rows": 2})
        assert load_project(path).data == {"base": {"rows": 1}, "limits": {"rows": 2}}


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

    def test_ignores_the_working_directory_of_a_served_directory(
        self, tmp_path, monkeypatch, invoke
    ):
        project_dir = _project_dir(tmp_path)
        (tmp_path / PROJECT_FILE).write_text("limits:\n  rows: 2\n")
        monkeypatch.chdir(tmp_path)
        assert invoke(project_dir) is None

    def test_loaders_get_the_registry_and_project_dir(self, tmp_path, monkeypatch, invoke):
        project_dir = _project_dir(tmp_path)
        (project_dir / "__init__.py").write_text(
            "from panel_flowdash.dashboard_store import MemoryDashboardStore\n"
            "from panel_flowdash.project import register_section\n"
            "store = MemoryDashboardStore()\n"
            "register_section('limits', lambda s: (s.context['project_dir'], "
            "'Pages/page' in s.context['registry']))\n"
        )
        (project_dir / PROJECT_FILE).write_text("limits: {}\n")
        monkeypatch.chdir(tmp_path)
        assert invoke(project_dir).get("limits") == (project_dir.resolve(), True)

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
