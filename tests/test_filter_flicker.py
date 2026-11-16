"""A control change updates the dashboard in one go, without rebuilding tiles."""

import importlib.util
import sys
from pathlib import Path

import pytest
from bokeh.document import Document
from panel.io.document import set_curdoc

from panel_flowdash import FlowDash, MemoryDashboardStore

EXAMPLE = Path(__file__).parents[1] / "examples" / "wind_energy"

pytestmark = [
    pytest.mark.skipif(
        not (EXAMPLE / "_data" / "turbines.csv.gz").exists(), reason="Example data not available."
    ),
    pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index"),
]


@pytest.fixture
def editor(tmp_path, monkeypatch):
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    store = MemoryDashboardStore()
    project.seed_dashboards(store, "demo")
    editor = FlowDash(
        str(EXAMPLE),
        store=store,
        settings_class=project.settings_class,
        propagate_failures=project.propagate_failures,
    )
    yield editor
    editor.dispose()
    project.store.close()
    for name in list(sys.modules):
        if name.split(".")[0] in {"Sources", "Controls", "Transforms", "Views", "Explore"}:
            del sys.modules[name]


def render(editor, dashboard_id):
    """Render the tiles of *dashboard_id* into a document and collect the changes it receives."""
    editor.load(dashboard_id)
    doc = Document()
    for view in editor._tile_objects:
        doc.add_root(view.get_root(doc))
    events = []
    doc.on_change(events.append)
    return doc, events


def set_control(editor, node_id, name, value):
    setattr(editor._component_instances[node_id], name, value)


def test_the_page_receives_nothing_until_every_tile_has_its_input(editor):
    doc, events = render(editor, "wind-data-quality")
    views = ["status", "completeness", "export", "kpi_checked", "timeline", "manufacturers"]
    received = {}
    for node_id in views:
        state = editor.graph.get_state(node_id)
        inputs = [p for p in state.param if p != "name"]
        state.param.watch(lambda _, n=node_id: received.setdefault(n, len(events)), inputs)

    with set_curdoc(doc):
        set_control(editor, "state", "state", "TX")

    assert received == dict.fromkeys(views, 0)
    assert events


@pytest.mark.parametrize(
    ("dashboard_id", "changes"),
    [
        ("wind-data-quality", [("state", "state", "TX"), ("state", "state", "DE")]),
        ("wind-overview", [("years", "years", (2000, 2010))]),
        ("wind-state-deep-dive", [("state", "state", "CA"), ("project", "project", "Alta I")]),
    ],
)
def test_tiles_update_in_place(editor, dashboard_id, changes):
    doc, events = render(editor, dashboard_id)
    for change in changes:
        with set_curdoc(doc):
            set_control(editor, *change)

        assert events
        replaced = [e for e in events if getattr(e, "attr", None) == "children"]
        assert replaced == [], change
        events.clear()
