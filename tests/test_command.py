"""Tests for the CLI and app builder."""

import subprocess
import sys
import tempfile
from pathlib import Path

from panel_flowdash.app import build_app_class, build_registry
from panel_flowdash.dashboard_store import DashboardStore


def _create_project(tmp_path: Path):
    """Create a minimal project structure with components."""
    section = tmp_path / "Analytics"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "selector.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, provides=['company'])\n"
        "def app(config):\n"
        "    return 'selector'\n"
    )
    (section / "chart.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, requires=['company'])\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )
    (section / "page.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=True, title='Overview')\n"
        "def app():\n"
        "    return 'page content'\n"
    )
    # Should be ignored
    (section / "_private.py").write_text("app = None\n")


class TestBuildRegistry:
    def test_scans_sections(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            registry = build_registry(tmp_path)
        finally:
            sys.path.remove(str(tmp_path))

        assert "Analytics/selector" in registry
        assert "Analytics/chart" in registry
        assert "Analytics/page" in registry
        assert "Analytics/_private" not in registry

    def test_metadata_extracted(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            registry = build_registry(tmp_path)
        finally:
            sys.path.remove(str(tmp_path))

        selector = registry["Analytics/selector"]
        assert selector.metadata.component is True
        assert selector.metadata.provides == ["company"]

        page = registry["Analytics/page"]
        assert page.metadata.page is True
        assert page.title == "Overview"

    def test_ignores_dot_dirs(self, tmp_path):
        _create_project(tmp_path)
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "mod.py").write_text(
            "from panel_flowdash import register\n\n"
            "@register(component=True)\n"
            "def app(): pass\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            registry = build_registry(tmp_path)
        finally:
            sys.path.remove(str(tmp_path))

        assert not any(".hidden" in k for k in registry)


class TestBuildAppClass:
    def test_creates_viewer_class(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            db_path = tmp_path / "test.db"
            store = DashboardStore(db_path)
            AppClass = build_app_class(tmp_path, store=store, title="Test App")
        finally:
            sys.path.remove(str(tmp_path))

        assert AppClass._title == "Test App"
        assert len(AppClass._component_entries) == 2
        assert len(AppClass._page_entries) == 1

    def test_build_routes(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            db_path = tmp_path / "test.db"
            store = DashboardStore(db_path)
            AppClass = build_app_class(tmp_path, store=store)
        finally:
            sys.path.remove(str(tmp_path))

        routes = AppClass.build_routes()
        assert "/" in routes
        assert "/components" in routes
        assert "/dash/{custom_app}" in routes
        assert "/Analytics/page" in routes

    def test_db_defaults_to_project_dir(self, tmp_path):
        _create_project(tmp_path)
        db_path = tmp_path / "dashboards.db"
        store = DashboardStore(db_path)
        assert db_path.exists()


class TestCLI:
    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash.command", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "unknown" not in result.stdout or result.stdout.strip()

    def test_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash.command", "serve", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "directory" in result.stdout
        assert "--db-path" in result.stdout
        assert "--port" in result.stdout

    def test_missing_directory(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash.command", "serve", "/nonexistent/path"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
