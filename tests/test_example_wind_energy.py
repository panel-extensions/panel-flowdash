"""The wind energy example: every module imports and every seeded dashboard loads and runs."""

import importlib.util
import json
import sys
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import pytest

from panel_flowdash import FlowDash, MemoryDashboardStore
from panel_flowdash.registry import build_registry
from panel_flowdash.util import ReactiveError

EXAMPLE = Path(__file__).parents[1] / "examples" / "wind_energy"
SEEDS = sorted(p.stem for p in (EXAMPLE / "_dashboards").glob("*.json"))

pytestmark = [
    pytest.mark.skipif(
        not (EXAMPLE / "_data" / "turbines.csv.gz").exists(), reason="Example data not available."
    ),
    # Bokeh's ColumnDataSource.from_df, used by Tabulator, still passes inplace=True.
    pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index"),
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The example's ``__init__.py``, executed with its dashboards stored under *tmp_path*."""
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.store.close()
    for name in list(sys.modules):
        if name.split(".")[0] in {"Sources", "Controls", "Transforms", "Views", "Explore"}:
            del sys.modules[name]


@pytest.fixture
def editor(project):
    store = MemoryDashboardStore()
    project.seed_dashboards(store, "demo")
    editor = FlowDash(
        str(EXAMPLE),
        store=store,
        settings_class=project.settings_class,
        propagate_failures=project.propagate_failures,
        **project.editor_options,
    )
    yield editor
    editor.dispose()


def tile(editor, instance_id):
    index = [item["instance_id"] for item in editor._tile_items].index(instance_id)
    return editor._tile_objects[index]


def texts(view):
    alerts = [a.title for a in view.select(pmui.Alert) if a.visible]
    return [*alerts, *(str(t.object) for t in view.select(pmui.Typography) if t.visible)]


def test_every_module_imports(project):
    registry = build_registry(EXAMPLE)
    components = [e for e in registry.values() if e.metadata.component]
    pages = [e for e in registry.values() if e.metadata.page]
    assert len(components) == 20
    assert {e.app_id for e in pages} == {"Explore/about", "Explore/fleet_growth"}
    for entry in registry.values():
        assert entry.load() is not None, entry.app_id
        assert entry.metadata.description, entry.app_id
        assert entry.metadata.icon, entry.app_id
    roles = {
        e.app_id: set(e.metadata.tags) & {"source", "transform", "view", "widget"}
        for e in components
    }
    assert all(len(r) == 1 for r in roles.values()), roles
    assert registry["Views/raw_export"].metadata.allow_groups == ["analysts"]


def test_pages_render(project):
    registry = build_registry(EXAMPLE)
    for app_id in ("Explore/about", "Explore/fleet_growth"):
        assert isinstance(registry[app_id].load()(), pn.viewable.Viewable)


def test_seeding_is_idempotent(project):
    assert sorted(project.seed_dashboards(MemoryDashboardStore(), "demo")) == SEEDS
    # The store the project defines was seeded when its __init__.py ran.
    assert project.seed_dashboards(project.store, "demo") == []
    store = MemoryDashboardStore()
    project.seed_dashboards(store, "demo")
    store.rename_dashboard("demo", "wind-overview", "Renamed")
    assert project.seed_dashboards(store, "demo") == []
    assert store.load_dashboard("demo", "wind-overview").title == "Renamed"


@pytest.mark.parametrize("dashboard_id", SEEDS)
def test_seeded_dashboard_loads(editor, dashboard_id):
    data = json.loads((EXAMPLE / "_dashboards" / f"{dashboard_id}.json").read_text())
    editor.load(dashboard_id)

    assert editor._placeholders() == {}
    assert editor._unresolved_edges == []
    assert len(editor.graph.edges) == len(data["edges"])
    assert editor.settings.capacity_unit == data["settings"]["capacity_unit"]
    for instance_id in editor.graph.node_ids:
        assert not editor.graph.get_failures(instance_id).failed, instance_id


def test_overview_edge_transform_keeps_the_ten_largest_projects(editor):
    editor.load("wind-overview")
    table = editor.graph.get_state("largest").table
    projects = editor.graph.get_state("projects").projects
    assert len(table) == 10
    assert table["capacity_mw"].tolist() == projects["capacity_mw"].nlargest(10).tolist()


def test_deep_dive_waits_for_a_project(editor):
    editor.load("wind-state-deep-dive")
    assert editor.graph.get_state("state").state == "TX"
    assert "Pick a project in the sidebar to see its turbines" in texts(tile(editor, "detail"))

    picker = editor.graph.get_state("project")
    name = editor.graph.get_state("projects").projects["project"].iat[0]
    picker.project = name

    assert editor.graph.get_state("detail").project == name
    assert name in texts(tile(editor, "detail"))


def test_port_types_keep_the_population_and_turbine_tables_apart(editor):
    editor.load("wind-overview")
    population = editor.add_component("Sources/population")
    join = editor.add_component("Transforms/state_join")

    assert "Type mismatch" in editor.connect("turbines", "turbines", join, "population")
    assert editor.connect(population, "population", join, "population") is True
    assert editor.connect("filter", "filtered", join, "turbines") is True
    assert len(editor.graph.get_state(join).by_state) > 40


@pytest.mark.failures
def test_quality_gate_failure_propagates_and_recovers(editor):
    editor.load("wind-data-quality")
    picker = editor.graph.get_state("state")

    picker.state = "CT"
    assert "turbines" in editor.graph.get_failures("timeline").failed
    assert isinstance(editor.graph.get_state("status").checked, ReactiveError)
    assert any("Selection blocked" in t for t in texts(tile(editor, "status")))

    picker.state = "TX"
    assert not editor.graph.get_failures("timeline").failed
    assert len(editor.graph.get_state("status").checked) > 100


@pytest.mark.parametrize("dashboard_id", SEEDS)
def test_seeded_cards_do_not_overlap_with_previews(editor, dashboard_id):
    editor.load(dashboard_id)
    editor._show_node_previews(True)
    boxes = [
        (node["position"]["x"], node["position"]["y"], node["view"].height)
        for node in editor._flow.nodes
    ]
    for i, (x, y, height) in enumerate(boxes):
        for other_x, other_y, other_height in boxes[i + 1 :]:
            if x == other_x:
                assert y + height <= other_y or other_y + other_height <= y
