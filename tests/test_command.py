"""Tests for the CLI and app builder."""

import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from panel_flowdash.app import FlowDashApp, build_registry
from panel_flowdash.command.serve import Serve, _load_project_init, _resolve_store
from panel_flowdash.dashboard_store import DashboardStore, MemoryDashboardStore
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.registry import RegistryEntry


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
    (section / "restricted.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=True, title='Restricted', allow_groups=['finance'],"
        " deny_users=['bob'])\n"
        "def app():\n"
        "    return 'secret'\n"
    )
    # Should be ignored
    (section / "_private.py").write_text("app = None\n")


class TestBuildRegistry:
    async def test_scans_sections(self, tmp_path):
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

    async def test_metadata_extracted(self, tmp_path):
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

    async def test_permission_fields_captured_by_ast(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            registry = build_registry(tmp_path)
        finally:
            sys.path.remove(str(tmp_path))

        restricted = registry["Analytics/restricted"]
        assert restricted.metadata.allow_groups == ["finance"]
        assert restricted.metadata.deny_users == ["bob"]
        perm = restricted.metadata.permission
        assert perm.allow_groups == frozenset({"finance"})
        assert perm.deny_users == frozenset({"bob"})

        # A page with no permission kwargs yields an empty permission.
        assert registry["Analytics/page"].metadata.permission.is_empty

    async def test_ignores_dot_dirs(self, tmp_path):
        _create_project(tmp_path)
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "mod.py").write_text(
            "from panel_flowdash import register\n\n@register(component=True)\ndef app(): pass\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            registry = build_registry(tmp_path)
        finally:
            sys.path.remove(str(tmp_path))

        assert not any(".hidden" in k for k in registry)


@pytest.fixture
def package_project(tmp_path, monkeypatch):
    """A project laid out as an importable package, its parent already on sys.path.

    Package and section names are unique to this fixture so modules cached in
    ``sys.modules`` by other tests cannot shadow it.
    """
    package = "flowdash_pkg_catalogue"
    project_dir = tmp_path / "src" / package
    section = project_dir / "PkgSection"
    section.mkdir(parents=True)
    (project_dir / "__init__.py").write_text(
        "from panel_flowdash import MemoryDashboardStore\n\nstore = MemoryDashboardStore()\n"
    )
    (section / "__init__.py").write_text("")
    (section / "chart.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, requires=['company'])\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )
    (section / "overview.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=True, title='Overview')\n"
        "def app():\n"
        "    return 'page content'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    yield package, project_dir
    for name in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
        del sys.modules[name]


class TestBuildRegistryPackage:
    async def test_default_names_modules_relative_to_project_dir(self, tmp_path):
        _create_project(tmp_path)
        registry = build_registry(tmp_path)
        assert registry["Analytics/chart"].module_name == "Analytics.chart"

    async def test_names_modules_under_package(self, package_project):
        package, project_dir = package_project
        registry = build_registry(project_dir, package=package)

        assert set(registry) == {"PkgSection/chart", "PkgSection/overview"}
        entry = registry["PkgSection/chart"]
        assert entry.module_name == f"{package}.PkgSection.chart"
        assert entry.section == "PkgSection"
        assert entry.name == "chart"

    async def test_load_shares_the_module_with_the_package(self, package_project):
        package, project_dir = package_project
        sys_path = list(sys.path)
        entry = build_registry(project_dir, package=package)["PkgSection/chart"]
        app = entry.load()

        module = importlib.import_module(f"{package}.PkgSection.chart")
        assert app is module.app
        assert "PkgSection.chart" not in sys.modules
        assert sys.path == sys_path

    async def test_live_app_maps_to_the_scanned_id(self, package_project):
        package, project_dir = package_project
        registry = build_registry(project_dir, package=package)
        module = importlib.import_module(f"{package}.PkgSection.chart")

        assert RegistryEntry.from_app(module.app).app_id in registry

    async def test_package_resolving_elsewhere_is_rejected(self, package_project):
        _, project_dir = package_project
        with pytest.raises(ValueError, match="not to the project directory"):
            build_registry(project_dir, package="json")

    async def test_unimportable_package_is_rejected(self, package_project):
        _, project_dir = package_project
        with pytest.raises(ValueError, match="not importable"):
            build_registry(project_dir, package="flowdash_no_such_package")

    async def test_app_threads_package_to_the_registry(self, package_project):
        package, project_dir = package_project
        app = FlowDashApp(project_dir=project_dir, package=package, store=MemoryDashboardStore())
        assert app._registry["PkgSection/chart"].module_name == f"{package}.PkgSection.chart"

    async def test_build_routes_threads_package_to_the_registry(self, package_project):
        package, project_dir = package_project
        routes = FlowDashApp.build_routes(
            project_dir=project_dir, package=package, store=MemoryDashboardStore()
        )
        assert "/PkgSection/overview" in routes

    async def test_serve_imports_project_init_as_the_package(self, package_project):
        package, project_dir = package_project
        init_mod = _load_project_init(project_dir, package)

        assert init_mod is importlib.import_module(package)
        assert isinstance(init_mod.store, MemoryDashboardStore)


class TestFlowDashApp:
    async def test_creates_viewer_instance(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            db_path = tmp_path / "test.db"
            store = DashboardStore(db_path)
            app = FlowDashApp(project_dir=tmp_path, store=store, title="Test App")
        finally:
            sys.path.remove(str(tmp_path))

        assert app.title == "Test App"
        assert app.home_dashboard is None
        assert len(app._component_entries) == 5
        assert len(app._page_entries) == 2

    async def test_builtin_widgets_can_be_disabled(self, tmp_path):
        _create_project(tmp_path)
        app = FlowDashApp(
            project_dir=tmp_path,
            store=DashboardStore(tmp_path / "widgets.db"),
            include_builtin_components=False,
        )
        assert len(app._component_entries) == 2

    @pytest.mark.parametrize("backend", [MemoryDashboardStore, JSONFolderDashboardStore])
    async def test_accepts_any_store_backend(self, tmp_path, backend):
        _create_project(tmp_path)
        store = backend() if backend is MemoryDashboardStore else backend(tmp_path / "json")
        store.create_dashboard("local", "Sales")
        app = FlowDashApp(project_dir=tmp_path, store=store)

        assert app.store is store
        assert [d.title for d in app.store.list_accessible(app._identity)] == ["Sales"]

    async def test_home_dashboard_param_passthrough(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            app = FlowDashApp(project_dir=tmp_path, store=store, home_dashboard="Sales")
        finally:
            sys.path.remove(str(tmp_path))

        assert app.home_dashboard == "Sales"

    async def test_build_routes(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            db_path = tmp_path / "test.db"
            store = DashboardStore(db_path)
            routes = FlowDashApp.build_routes(project_dir=tmp_path, store=store)
        finally:
            sys.path.remove(str(tmp_path))

        assert "/" in routes
        assert "/components" in routes
        assert "/dash/[^/]+" in routes
        assert "/Analytics/page" in routes

    async def test_db_defaults_to_project_dir(self, tmp_path):
        _create_project(tmp_path)
        db_path = tmp_path / "dashboards.db"
        DashboardStore(db_path)
        assert db_path.exists()


class TestResolveStore:
    def test_defaults_to_sqlite_in_project_dir(self, tmp_path):
        store, description = _resolve_store(None, tmp_path, None)
        assert isinstance(store, DashboardStore)
        assert (tmp_path / "dashboards.db").exists()
        assert str(tmp_path / "dashboards.db") in description

    def test_db_path_overrides_the_default_location(self, tmp_path):
        db_path = tmp_path / "elsewhere.db"
        store, _ = _resolve_store(types.SimpleNamespace(), tmp_path, str(db_path))
        assert isinstance(store, DashboardStore)
        assert db_path.exists()

    def test_project_store_is_used(self, tmp_path):
        mine = JSONFolderDashboardStore(tmp_path / "dashboards")
        store, description = _resolve_store(types.SimpleNamespace(store=mine), tmp_path, None)
        assert store is mine
        assert "JSONFolderDashboardStore" in description
        assert not (tmp_path / "dashboards.db").exists()

    def test_project_store_must_be_a_store(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _resolve_store(types.SimpleNamespace(store="dashboards.db"), tmp_path, None)
        assert "must be a BaseDashboardStore" in capsys.readouterr().err

    def test_db_path_conflicts_with_project_store(self, tmp_path, capsys):
        init_mod = types.SimpleNamespace(store=MemoryDashboardStore())
        with pytest.raises(SystemExit):
            _resolve_store(init_mod, tmp_path, str(tmp_path / "x.db"))
        assert "--db-path conflicts" in capsys.readouterr().err


class TestCLI:
    def test_excludes_file_args(self):
        arg_names = {name for name, _ in Serve.args}
        assert "files" not in arg_names
        assert "--args" not in arg_names

    def test_has_flowdash_args(self):
        arg_names = {name for name, _ in Serve.args}
        for expected in ("directory", "--db-path", "--package", "--title", "--home-dashboard"):
            assert expected in arg_names, f"missing FlowDash arg: {expected}"

    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "unknown" not in result.stdout or result.stdout.strip()

    def test_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash", "serve", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        # FlowDash specific
        assert "directory" in result.stdout
        assert "--db-path" in result.stdout
        assert "--port" in result.stdout
        # Panel specific (non exhaustive)
        assert "--dev" in result.stdout
        assert "--admin" in result.stdout
        assert "--profiler" in result.stdout
        assert "--num-threads" in result.stdout
        assert "--allow-websocket-origin" in result.stdout
        assert "--oauth-provider" in result.stdout

    def test_package_mismatch_fails_before_serving(self, tmp_path):
        _create_project(tmp_path)
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash", "serve", str(tmp_path), "--package", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1
        assert "not to the project directory" in result.stderr

    def test_missing_directory(self):
        result = subprocess.run(
            [sys.executable, "-m", "panel_flowdash", "serve", "/nonexistent/path"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
