"""Tests for showing a whole dashboard in full screen."""

import sys

import panel as pn
import panel_material_ui as pmui
import pytest

from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.fullscreen import EXIT_CLASS, PRESENTING_CLASS, TOGGLE_CLASS
from tests.test_editor import COMPONENTS, SELECTOR
from tests.test_navigation import FakeLocation, _create_project


@pytest.fixture
def editor():
    editor = FlowDash(COMPONENTS, notifications=False)
    editor.add_component(SELECTOR)
    editor.editable = False
    return editor


def _labels(editor):
    return [item["label"] for item in editor._dashboard_bar.overflow.items if item]


def _presenting(editor) -> bool:
    return PRESENTING_CLASS in editor._view.css_classes


class TestEditor:
    async def test_button_follows_the_view_actions(self, editor):
        bar = editor._dashboard_bar
        actions = bar._actions.objects
        assert bar.fullscreen_button in actions
        assert actions.index(bar.fullscreen_button) == actions.index(bar.refresh_period) + 1
        assert TOGGLE_CLASS in bar.fullscreen_button.css_classes

    async def test_button_follows_extra_toolbar_items(self, editor):
        share = pmui.Button(label="Share")
        editor.toolbar_extra = [share]
        actions = editor._dashboard_bar._actions.objects
        assert actions.index(editor._dashboard_bar.fullscreen_button) == actions.index(share) + 1

    async def test_button_in_view_mode_only_and_not_repeated_in_the_menu(self, editor):
        bar = editor._dashboard_bar
        assert "Full screen" not in _labels(editor)
        editor.read_only = True
        assert bar.fullscreen_button in bar._actions
        editor.param.update(read_only=False, editable=True)
        assert bar.fullscreen_button not in bar._actions

    async def test_setting_the_param_enters_full_screen(self, editor):
        editor.fullscreen = True
        assert editor._fullscreen_controller.active

    async def test_overlay_is_empty_until_full_screen(self, editor):
        # A hidden copy of the bar's title and health chip would still be in the page.
        assert not editor._fullscreen_overlay.visible
        assert editor._fullscreen_overlay.objects == []

    async def test_full_screen_shows_only_the_dashboard(self, editor):
        editor.fullscreen = True
        assert not editor._dashboard_bar.__panel__().visible
        assert editor._fullscreen_overlay.visible
        assert _presenting(editor)
        assert editor._view_controls.__panel__() in editor._workspace_area
        assert editor._tile_grid in editor._workspace_area

    async def test_overlay_shows_the_health_and_is_named_by_the_title(self, editor):
        editor.new_dashboard("Plant")
        editor.add_component(SELECTOR)
        editor.editable = False
        editor.fullscreen = True
        chip, exit_button = editor._fullscreen_overlay
        assert editor._fullscreen_controller.overlay_label == "Plant, full screen controls"
        assert chip.object == editor._dashboard_bar.health_chip.label == "1/1 up to date"
        assert chip.sx == editor._dashboard_bar.health_chip.sx
        assert EXIT_CLASS in exit_button.css_classes

    async def test_the_button_toggles_this_editor_from_outside_it(self, editor):
        # With the compact header the button sits in the app's top bar.
        toggle_class = editor._fullscreen_controller.toggle_class
        assert toggle_class
        assert toggle_class in editor._dashboard_bar.fullscreen_button.css_classes

    async def test_leaving_full_screen_in_the_browser_restores_the_layout(self, editor):
        editor.fullscreen = True
        # Esc or the browser's own control: the browser turns the hook off.
        editor._fullscreen_controller.active = False
        assert not editor.fullscreen
        assert editor._dashboard_bar.__panel__().visible
        assert not editor._fullscreen_overlay.visible
        assert not editor._fullscreen_overlay.objects
        assert not _presenting(editor)

    async def test_bar_stays_hidden_without_a_toolbar(self, editor):
        editor.toolbar = False
        editor.fullscreen = True
        editor.fullscreen = False
        assert not editor._dashboard_bar.__panel__().visible

    async def test_edit_mode_leaves_full_screen(self, editor):
        editor.fullscreen = True
        editor.editable = True
        assert not editor.fullscreen
        assert not editor._fullscreen_controller.active
        assert not editor._fullscreen_controller.enabled
        assert not _presenting(editor)
        assert editor._dashboard_bar.__panel__().visible

    async def test_not_available_in_edit_mode(self, editor):
        editor.editable = True
        editor.fullscreen = True
        assert not editor.fullscreen
        assert not editor._fullscreen_overlay.visible

    async def test_controller_enabled_in_view_mode(self, editor):
        assert editor._fullscreen_controller.enabled


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store, notifications=False)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _open(app, search=""):
    model = app.store.create_dashboard(app._user_id, "Plant")
    pn.state.location.param.update(
        pathname=f"{DASH_ROUTE_PREFIX}{model.dashboard_id}", search=search
    )
    app._load_dashboard_sync(model.dashboard_id, edit="edit=true" in search)
    return model


class TestApp:
    async def test_kiosk_link_opens_in_full_screen(self, app):
        _open(app, "?kiosk=1")
        assert app._editor.fullscreen
        assert pn.state.location.search == "?kiosk=1"

    async def test_leaving_kiosk_drops_the_query(self, app):
        _open(app, "?kiosk=1")
        app._editor._fullscreen_controller.active = False
        assert not app._editor.fullscreen
        assert pn.state.location.search == ""

    async def test_kiosk_is_ignored_in_edit_mode(self, app):
        _open(app, "?edit=true&kiosk=1")
        assert app._editor.editable
        assert not app._editor.fullscreen

    async def test_plain_link_opens_normally(self, app):
        _open(app)
        assert not app._editor.fullscreen

    async def test_navigating_leaves_full_screen(self, app):
        _open(app)
        app._editor.fullscreen = True
        app._begin_navigation()
        assert not app._editor.fullscreen

    async def test_palette_action(self, app):
        _open(app)
        controller = app._command_palette
        item = next(i for i in controller.items() if i["id"] == "action:fullscreen")
        assert (item["title"], item["keys"]) == ("Full screen", ["F"])
        controller.run("action:fullscreen")
        assert app._editor.fullscreen
        item = next(i for i in controller.items() if i["id"] == "action:fullscreen")
        assert item["title"] == "Exit full screen"
        controller.run("action:fullscreen")
        assert not app._editor.fullscreen

    async def test_palette_action_not_offered_in_edit_mode(self, app):
        _open(app, "?edit=true")
        assert "action:fullscreen" not in [i["id"] for i in app._command_palette.items()]
        app._command_palette.run("action:fullscreen")
        assert not app._editor.fullscreen

    async def test_shortcut_is_listed(self, app):
        shortcuts = app._command_palette.palette.shortcuts
        assert {"group": "Viewing", "label": "Full screen", "keys": ["F"]} in shortcuts
