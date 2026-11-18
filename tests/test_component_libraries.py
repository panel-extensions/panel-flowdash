"""Tests for adding an installed library's components with `component_libraries`."""

import sys
import textwrap

import pytest

from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import Identity
from panel_flowdash.component_library import (
    ComponentLibraryError,
    library_components,
    merge_libraries,
)
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry, build_registry
from panel_flowdash.widgets import BUILTIN_COMPONENTS

NODES = """
import param
from panel.viewable import Viewer

from panel_flowdash import register


@register(page=False, component=True, title="Source",
          provides=[{"key": "value", "type": "int"}])
class SourceNode(Viewer):
    value = param.Integer(default=1)

    @param.output(param.Integer)
    def out(self):
        return self.value

    def __panel__(self):
        return "source"


@register(component=True, title="Secret", allow_groups=["finance"])
def secret(config):
    return "secret"


def helper():
    return "not a component"
"""

LIBRARIES = {
    "fdlib_dict": (
        "from ._nodes import SourceNode, secret\n"
        "COMPONENTS = {'Lib/source': SourceNode, 'Lib/secret': secret}\n"
    ),
    "fdlib_declared": (
        "from ._nodes import SourceNode, secret\n"
        "__flowdash_components__ = {'Declared/source': SourceNode}\n"
    ),
    "fdlib_scanned": "from ._nodes import SourceNode, helper, secret\n",
    "fdlib_empty": "VALUE = 1\n",
    "fdlib_mixed": (
        "from ._nodes import SourceNode, helper\n"
        "COMPONENTS = {'Lib/source': SourceNode, 'Lib/helper': helper}\n"
    ),
    "fdlib_broken": "raise RuntimeError('boom')\n",
    "fdlib_reexport": ("from panel_flowdash.widgets import KPI\nfrom ._nodes import SourceNode\n"),
    "fdlib_dupes": (
        "from .sub1.views import chart as chart1\n"
        "from .sub2.views import chart as chart2\n"
        "__flowdash_components__ = [chart1, chart2]\n"
    ),
    "fdlib_aliased": (
        "from ._aliased import retired, renamed\nRETIRED = [retired]\nRENAMED = [renamed]\n"
    ),
}

CHART = """
from panel_flowdash import register


@register(component=True)
def chart(config):
    return "chart"
"""

ALIASED = """
from panel_flowdash import register


@register(component=True, aliases=["Lib/retired"])
def retired(config):
    return "retired"


@register(component=True, aliases=["Lib/chart"])
def renamed(config):
    return "renamed"
"""

EXTRA_FILES = {
    "fdlib_dupes": {
        "sub1/__init__.py": "",
        "sub1/views.py": CHART,
        "sub2/__init__.py": "",
        "sub2/views.py": CHART,
    },
    "fdlib_aliased": {"_aliased.py": ALIASED},
}


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    """Importable packages sharing one module of components."""
    for name, source in LIBRARIES.items():
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text(source)
        (package / "_nodes.py").write_text(textwrap.dedent(NODES))
        for path, content in EXTRA_FILES.get(name, {}).items():
            (package / path).parent.mkdir(exist_ok=True)
            (package / path).write_text(content)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield
    for module in [m for m in sys.modules if m.startswith("fdlib_")]:
        del sys.modules[module]


def _project(tmp_path, component="chart", aliases=()):
    section = tmp_path / "project" / "Lib"
    section.mkdir(parents=True)
    (section / "__init__.py").write_text("")
    (section / f"{component}.py").write_text(
        "from panel_flowdash import register\n\n"
        f"@register(page=False, component=True, aliases={list(aliases)!r})\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )
    return tmp_path / "project"


def _app(project_dir, identity=None, **params):
    app = FlowDashApp(
        project_dir=project_dir,
        store=MemoryDashboardStore(),
        notifications=False,
        **params,
    )
    if identity is not None:
        app._identity = identity
    return app


class TestLibraryComponents:
    def test_mapping_attribute_keeps_its_ids(self, libraries):
        entries = library_components("fdlib_dict:COMPONENTS")
        assert sorted(entries) == ["Lib/secret", "Lib/source"]
        assert entries["Lib/source"].app.__name__ == "SourceNode"
        assert entries["Lib/source"].section == "Lib"

    def test_module_offers_its_declared_components(self, libraries):
        assert list(library_components("fdlib_declared")) == ["Declared/source"]

    def test_module_without_declaration_offers_its_registered_exports(self, libraries):
        entries = library_components("fdlib_scanned")
        assert sorted(e.name for e in entries.values()) == ["SourceNode", "secret"]

    def test_single_component(self, libraries):
        entries = library_components("fdlib_dict:SourceNode")
        assert [e.name for e in entries.values()] == ["SourceNode"]

    def test_module_without_all_skips_imported_components(self, libraries):
        entries = library_components("fdlib_reexport")
        assert [e.name for e in entries.values()] == ["SourceNode"]

    def test_two_components_with_one_derived_id_raise(self, libraries):
        with pytest.raises(
            ComponentLibraryError, match="both have the component id 'views/chart'"
        ):
            library_components("fdlib_dupes")

    def test_library_entries_are_copied_not_mutated(self, libraries):
        import fdlib_dict

        entry = RegistryEntry.from_app(fdlib_dict.secret, app_id="Lib/secret")
        assert entry.metadata.page
        fdlib_dict.ENTRIES = [entry]
        offered = library_components("fdlib_dict:ENTRIES")["Lib/secret"]
        assert not offered.metadata.page
        assert entry.metadata.page
        del fdlib_dict.ENTRIES

    def test_library_adds_no_pages(self, libraries):
        entry = library_components("fdlib_dict:COMPONENTS")["Lib/secret"]
        assert entry.metadata.component
        assert not entry.metadata.page

    @pytest.mark.parametrize(
        ("name", "message"),
        [
            ("fdlib_missing", "Cannot import component library 'fdlib_missing': ModuleNotFound"),
            ("fdlib_broken", "Cannot import component library 'fdlib_broken': RuntimeError: boom"),
            ("fdlib_dict:NOPE", "'fdlib_dict' has no attribute 'NOPE'"),
            ("fdlib_dict:", "is not a module name or 'module:attribute'"),
            ("fdlib_empty", "holds no FlowDash components"),
            ("fdlib_empty:VALUE", "has no @register metadata"),
            ("fdlib_scanned:helper", "offers 'helper' as '.*', but it has no @register"),
            ("fdlib_mixed:COMPONENTS", "'helper' as 'Lib/helper', but it has no @register"),
        ],
    )
    def test_unusable_names_raise(self, libraries, name, message):
        with pytest.raises(ComponentLibraryError, match=message):
            library_components(name)


class TestMerge:
    def test_without_libraries_the_registry_is_unchanged(self):
        registry = {}
        assert merge_libraries(registry, []) is registry

    def test_merging_again_is_a_no_op(self, libraries):
        merged = merge_libraries({}, ["fdlib_dict:COMPONENTS"])
        again = merge_libraries(merged, ["fdlib_dict:COMPONENTS"])
        assert again == merged

    def test_two_libraries_with_one_id_collide(self, libraries, monkeypatch):
        import fdlib_declared

        monkeypatch.setattr(
            fdlib_declared, "OTHER", {"Lib/source": fdlib_declared.secret}, raising=False
        )
        with pytest.raises(ComponentLibraryError, match="library 'fdlib_dict:COMPONENTS'"):
            merge_libraries({}, ["fdlib_dict:COMPONENTS", "fdlib_declared:OTHER"])

    def test_unloaded_entries_with_one_id_collide(self, libraries, monkeypatch):
        import fdlib_dict

        def unloaded(module_name):
            return RegistryEntry(
                app_id="Lib/lazy",
                section="Lib",
                name="lazy",
                page_path="/Lib/lazy",
                module_name=module_name,
                metadata=PanelAppMetadata(page=False, component=True),
            )

        monkeypatch.setattr(fdlib_dict, "FIRST", [unloaded("one.lazy")], raising=False)
        monkeypatch.setattr(fdlib_dict, "SECOND", [unloaded("two.lazy")], raising=False)
        merged = merge_libraries({}, ["fdlib_dict:FIRST", "fdlib_dict:FIRST"])
        assert merged["Lib/lazy"].module_name == "one.lazy"
        with pytest.raises(ComponentLibraryError, match="'Lib/lazy', which library"):
            merge_libraries({}, ["fdlib_dict:FIRST", "fdlib_dict:SECOND"])

    def test_alias_of_a_registered_id_raises(self, libraries, tmp_path):
        registry = build_registry(_project(tmp_path))
        with pytest.raises(ComponentLibraryError, match="'Lib/chart' as a former id of"):
            merge_libraries(registry, ["fdlib_aliased:RENAMED"])

    def test_alias_of_a_project_alias_raises(self, libraries, tmp_path):
        registry = build_registry(_project(tmp_path, aliases=["Lib/retired"]))
        with pytest.raises(ComponentLibraryError, match="already a former id of 'Lib/chart'"):
            merge_libraries(registry, ["fdlib_aliased:RETIRED"])

    def test_id_that_is_a_project_alias_raises(self, libraries, tmp_path):
        registry = build_registry(_project(tmp_path, aliases=["Lib/source"]))
        with pytest.raises(ComponentLibraryError, match="'Lib/source', which is a former id"):
            merge_libraries(registry, ["fdlib_dict:COMPONENTS"])

    def test_reserved_id_collides(self, libraries):
        with pytest.raises(ComponentLibraryError, match="'Lib/source', which the built-in"):
            merge_libraries({}, ["fdlib_dict:COMPONENTS"], reserved=["Lib/source"])


class TestApp:
    async def test_without_libraries_nothing_changes(self, tmp_path):
        app = _app(_project(tmp_path))
        assert sorted(app._component_entries) == sorted(["Lib/chart", *BUILTIN_COMPONENTS])

    async def test_library_components_join_the_project(self, tmp_path, libraries):
        app = _app(_project(tmp_path), component_libraries=["fdlib_dict:COMPONENTS"])
        assert {"Lib/chart", "Lib/source", "Lib/secret"} <= set(app._component_entries)
        assert "Lib/secret" not in app._page_entries
        instance_id = app._editor.add_component("Lib/source")
        assert app._editor.graph.get_state(instance_id).value == 1

    async def test_project_id_collision_raises(self, tmp_path, libraries):
        project = _project(tmp_path, component="source")
        with pytest.raises(ComponentLibraryError, match="'Lib/source', which the project"):
            _app(project, component_libraries=["fdlib_dict:COMPONENTS"])

    async def test_builtin_collision_only_with_builtins(self, tmp_path, libraries, monkeypatch):
        builtin = next(iter(BUILTIN_COMPONENTS))
        module = sys.modules.get("fdlib_dict") or __import__("fdlib_dict")
        monkeypatch.setattr(module, "CLASH", {builtin: module.SourceNode}, raising=False)
        with pytest.raises(ComponentLibraryError, match="the built-in components"):
            _app(_project(tmp_path), component_libraries=["fdlib_dict:CLASH"])
        app = _app(
            tmp_path / "project",
            component_libraries=["fdlib_dict:CLASH"],
            include_builtin_components=False,
        )
        assert app._component_entries[builtin].app is module.SourceNode

    async def test_permissions_apply(self, tmp_path, libraries):
        project = _project(tmp_path)
        alice = Identity(user="alice", oauth_user="alice", groups=frozenset({"finance"}))
        bob = Identity(user="bob", oauth_user="bob")
        libs = ["fdlib_dict:COMPONENTS"]
        assert _app(project, alice, component_libraries=libs)._editor._component_authorized(
            "Lib/secret"
        )
        editor = _app(project, bob, component_libraries=libs)._editor
        assert not editor._component_authorized("Lib/secret")
        with pytest.raises(PermissionError):
            editor.add_component("Lib/secret")

    async def test_authorize_component_applies(self, tmp_path, libraries):
        app = _app(
            _project(tmp_path),
            component_libraries=["fdlib_dict:COMPONENTS"],
            editor_options={},
        )
        app._editor.authorize_component = lambda entry: entry.app_id != "Lib/source"
        assert not app._editor._component_authorized("Lib/source")
        assert app._editor._component_authorized("Lib/chart")

    async def test_build_routes_merges_once_for_every_session(self, tmp_path, libraries):
        routes = FlowDashApp.build_routes(
            project_dir=_project(tmp_path),
            store=MemoryDashboardStore(),
            notifications=False,
            component_libraries=["fdlib_dict:COMPONENTS"],
        )
        first, second = routes["/"](), routes["/"]()
        assert first._registry["Lib/source"] is second._registry["Lib/source"]
        assert "/Lib/secret" not in routes
