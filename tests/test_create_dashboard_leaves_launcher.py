"""Creating a dashboard from the launcher opens the editor without rebuilding the launcher."""

import sys

import panel as pn
import pytest

from panel_flowdash.app import FlowDashApp
from panel_flowdash.dashboard_store import MemoryDashboardStore
from tests.test_navigation import FakeLocation, _create_project


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        instance = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
        await instance._ensure_components_loaded()
        instance._notify = lambda *args, **kwargs: None
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


async def test_create_dialog_opens_the_editor_without_refreshing_the_launcher(app):
    launcher = app._build_launcher()
    refreshes = []
    launcher.refresh = lambda *args, **kwargs: refreshes.append(args)
    app._open_create_dialog()
    app._dialog_name_input._process_events({"value_input": "Fresh"})

    app._dialog_name_input.param.trigger("enter_pressed")

    assert [m.title for m in app.store._all_dashboards()] == ["Fresh"]
    assert pn.state.location.pathname.startswith("/dash/")
    assert not app._dialog.open
    assert refreshes == []


async def test_deleting_from_the_launcher_still_refreshes_it(app):
    model = app.store.create_dashboard(app._user_id, "Sales")
    launcher = app._build_launcher()
    refreshes = []
    launcher.refresh = lambda *args, **kwargs: refreshes.append(args)

    assert app._delete_dashboard(model.dashboard_id)

    assert refreshes
