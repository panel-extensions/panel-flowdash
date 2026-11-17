"""Dashboard files in the YAML format: importing, dropping and downloading them."""

import yaml

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.editor import RESTRICTED_COMPONENT_ID, export_filename
from panel_flowdash.ui.launcher import parse_dashboard_file

from .test_import_restricted_components import _app, _editor, _loaded, store  # noqa: F401

YAML_FILE = "flowdash: 1\ntitle: From YAML\nnodes:\n  o: {component: T/open}\n"


async def test_export_filename_takes_the_format():
    assert export_filename("Plant A") == "Plant_A.json"
    assert export_filename("Plant A", "yaml") == "Plant_A.yaml"


def test_dropping_a_yaml_file_loads_it():
    editor = _editor()
    editor.new_dashboard("mine")
    editor._on_files_dropped([{"name": "board.yml", "content": YAML_FILE}])
    assert [i["component_id"] for i in editor._tile_items] == ["T/open"]


async def test_the_editor_downloads_yaml_with_restricted_stand_ins(store):  # noqa: F811
    editor, _ = _loaded(store)
    editor._download_format = "yaml"
    text = editor._download_callback().getvalue()
    assert editor._download_button.filename.endswith(".yaml")
    doc = yaml.safe_load(text)
    assert doc["nodes"]["r1"]["component"] == RESTRICTED_COMPONENT_ID
    assert {"from": "r1", "to": "o1.x"} in doc["edges"]
    assert "SELECT secret" not in text


async def test_the_launcher_reads_yaml_files():
    model = parse_dashboard_file(YAML_FILE, "alice")
    assert model.title == "From YAML" and [i.component_id for i in model.items] == ["T/open"]


async def test_the_launcher_downloads_yaml(store, monkeypatch):  # noqa: F811
    app = _app(store, monkeypatch)
    (model,) = store._all_dashboards()
    assert app._download_dashboard(model.dashboard_id, "yaml")
    assert app._downloader.filename.endswith(".yaml")
    text = app._downloader.callback().getvalue()
    assert yaml.safe_load(text)["nodes"]["r1"]["component"] == RESTRICTED_COMPONENT_ID
    assert "SELECT secret" not in text


async def test_the_launcher_imports_yaml_files(monkeypatch):
    empty = MemoryDashboardStore()
    app = _app(empty, monkeypatch)
    model = app._import_dashboard_file("board.yaml", YAML_FILE)
    assert [i.component_id for i in empty._load_any(model.dashboard_id).items] == ["T/open"]
