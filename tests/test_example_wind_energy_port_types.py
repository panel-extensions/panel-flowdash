"""The wind energy example's port types exist before any of its components is placed."""

import importlib.util
import sys
from pathlib import Path

import pytest

from panel_flowdash import FlowDash, MemoryDashboardStore, port_types

EXAMPLE = Path(__file__).parents[1] / "examples" / "wind_energy"
SECTIONS = {"Sources", "Controls", "Transforms", "Views", "Explore"}

pytestmark = pytest.mark.skipif(
    not (EXAMPLE / "_data" / "turbines.csv.gz").exists(), reason="Example data not available."
)


def _drop_sections():
    for name in list(sys.modules):
        if name.split(".")[0] in SECTIONS:
            del sys.modules[name]


@pytest.fixture
def fresh_port_types(monkeypatch):
    """A port type registry holding only the built-in types, as in a new process."""
    monkeypatch.setattr(port_types, "_ALIASES", {})
    monkeypatch.setattr(port_types, "_SUPERTYPES", {})
    port_types._register_builtin_types()
    assert not port_types.port_types_compatible("Turbines", "DataFrame")


@pytest.fixture
def editor(fresh_port_types, tmp_path, monkeypatch):
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    _drop_sections()
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    editor = FlowDash(str(EXAMPLE), store=MemoryDashboardStore(), notifications=False)
    yield editor
    editor.dispose()
    project.store.close()
    _drop_sections()


def test_transforms_connect_before_any_source_is_placed(editor):
    filtered = editor.add_component("Transforms/filter_turbines")
    kpi = editor.add_component("Views/kpi")
    join = editor.add_component("Transforms/state_join")
    assert not any(name.split(".")[0] == "Sources" for name in sys.modules)

    assert editor.connect(filtered, "filtered", kpi, "turbines") is True
    assert editor.connect(filtered, "filtered", join, "turbines") is True
    assert "Type mismatch" in editor.connect(filtered, "filtered", join, "population")
