"""Tests for the command palette's items and the actions it and the shortcuts run."""

import sys
from types import SimpleNamespace

import panel as pn
import pytest

from panel_flowdash.app import COMPONENTS_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.ui.command_palette import (
    PLACEHOLDER,
    SHORTCUTS,
    CommandPalette,
    CommandPaletteController,
)
from panel_flowdash.ui.status_bar import select_on_canvas
from tests.test_navigation import FakeLocation, _create_project

SELECTOR = "Analytics/selector"
CHART = "Analytics/chart"


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    # Toggling a page's theme sets the global one, which the next page starts from.
    theme = pn.config.theme
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store, notifications=False)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.config.theme = theme
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _open(app, title="Plant", edit=False, components=()):
    model = app.store.create_dashboard(app._user_id, title)
    pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    for component_id in components:
        app._editor.add_component(component_id)
    if components:
        app._editor.save()
    if not edit:
        app._show_view_mode()
    return model


def _ids(controller, group=None):
    return [i["id"] for i in controller.items() if group is None or i["group"] == group]


def _item(controller, item_id):
    return next(i for i in controller.items() if i["id"] == item_id)


class TestPlacement:
    async def test_fills_the_shell_search_slot(self, app):
        palette = app._command_palette.palette
        assert isinstance(palette, CommandPalette)
        assert app._shell.search_slot.objects == [palette]
        assert palette.placeholder == PLACEHOLDER == "Search dashboards, components and actions"
        assert palette.shortcuts == SHORTCUTS

    async def test_needs_the_shell(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            store = DashboardStore(tmp_path / "test.db")
            app = FlowDashApp(project_dir=tmp_path, store=store, nav_variant="left")
            assert app._command_palette is None
        finally:
            sys.path.remove(str(tmp_path))


class TestItems:
    async def test_launcher(self, app):
        app.store.create_dashboard(app._user_id, "Depot")
        controller = app._command_palette
        assert _ids(controller, "Dashboards") == [
            f"dashboard:{d.dashboard_id}" for d in app.store.list_accessible(app._identity)
        ]
        depot = controller.items()[0]
        assert (depot["title"], depot["subtitle"]) == ("Depot", "Owned by you")
        components = _ids(controller, "Components")
        assert {f"component:{SELECTOR}", f"component:{CHART}", "component:Widgets/Slider"} <= set(
            components
        )
        assert _ids(controller, "Actions") == ["action:theme", "action:home"]
        assert not _ids(controller, "On this dashboard")

    async def test_groups_come_in_order(self, app):
        _open(app, components=[SELECTOR])
        editor = app._editor
        instance_id = editor._tile_items[0]["instance_id"]
        editor._inspect_tiles[instance_id] = SimpleNamespace(expand=lambda: None)
        groups = list(dict.fromkeys(i["group"] for i in app._command_palette.items()))
        assert groups == ["Dashboards", "Components", "On this dashboard", "Actions"]

    async def test_view_mode(self, app):
        model = _open(app)
        controller = app._command_palette
        current = _item(controller, f"dashboard:{model.dashboard_id}")
        assert current["subtitle"] == "Current dashboard"
        assert _ids(controller, "Actions") == [
            "action:refresh",
            "action:edit",
            "action:fullscreen",
            "action:settings",
            "action:theme",
            "action:home",
        ]
        assert _item(controller, f"component:{SELECTOR}")["hint"] == "View"

    async def test_edit_mode(self, app):
        _open(app, edit=True)
        controller = app._command_palette
        assert _ids(controller, "Actions") == [
            "action:refresh",
            "action:save",
            "action:split",
            "action:view",
            "action:settings",
            "action:theme",
            "action:home",
        ]
        component = _item(controller, f"component:{SELECTOR}")
        assert (component["hint"], component["subtitle"]) == ("Add", "Add to this dashboard")
        assert _item(controller, "action:save")["keys"] == ["Mod", "S"]

    async def test_arrange_actions_with_components_to_arrange(self, app):
        _open(app, edit=True, components=[SELECTOR, CHART])
        controller = app._command_palette
        actions = _ids(controller, "Actions")
        assert actions[actions.index("action:split") + 1 :][:2] == [
            "action:arrange_canvas",
            "action:arrange_layout",
        ]
        editor = app._editor
        editor._flow.patch_node_props(
            editor._tile_items[1]["instance_id"], {"position": {"x": 0, "y": 0}}
        )
        editor._flow.patch_node_props(
            editor._tile_items[0]["instance_id"], {"position": {"x": 0, "y": 0}}
        )
        controller.run("action:arrange_canvas")
        assert len({(n["position"]["x"], n["position"]["y"]) for n in editor._flow.nodes}) == 2
        assert editor.turns[-1].summary.endswith("Arranged the dataflow.")
        controller.run("action:arrange_layout")
        assert editor.turns[-1].summary.endswith("Arranged the tile layout.")
        assert {e["instance_id"] for e in editor.layout} == {
            item["instance_id"] for item in editor._tile_items
        }

    async def test_save_says_whether_there_are_changes(self, app):
        _open(app, edit=True)
        controller = app._command_palette
        assert _item(controller, "action:save")["subtitle"] == "No unsaved changes"
        app._editor.add_component(SELECTOR)
        assert _item(controller, "action:save")["subtitle"] == "Unsaved changes"

    async def test_read_only_dashboard(self, app):
        _open(app)
        app._dashboard_rights = lambda _dashboard: (False, False)
        app._editor.read_only = True
        assert _ids(app._command_palette, "Actions") == [
            "action:refresh",
            "action:fullscreen",
            "action:theme",
            "action:home",
        ]

    async def test_unauthorized_components_are_left_out(self, app):
        app._editor.authorize_component = lambda entry: entry.title != "chart"
        components = _ids(app._command_palette, "Components")
        assert f"component:{SELECTOR}" in components
        assert f"component:{CHART}" not in components

    async def test_theme_action_names_the_other_theme(self, app):
        controller = app._command_palette
        app._page.dark_theme = False
        assert _item(controller, "action:theme")["title"] == "Switch to dark theme"
        app._page.dark_theme = True
        assert _item(controller, "action:theme")["title"] == "Switch to light theme"

    async def test_inspect_for_tiles_with_outputs(self, app):
        _open(app, components=[SELECTOR, CHART])
        editor = app._editor
        editor.inspect_outputs = True
        selector = next(i for i in editor._tile_items if i["component_id"] == SELECTOR)
        editor._grid_view(selector, editor._tile_objects[0])
        # Framed tiles also offer Expand; this test is about Inspect only.
        items = [i for i in app._command_palette.items() if i["id"].startswith("inspect:")]
        assert [i["id"] for i in items] == [f"inspect:{selector['instance_id']}"]
        assert items[0]["title"] == "Inspect selector"

    async def test_expand_when_the_tile_offers_it(self, app):
        _open(app, components=[CHART])
        editor = app._editor
        instance_id = editor._tile_items[0]["instance_id"]
        expanded = []
        editor._inspect_tiles[instance_id] = SimpleNamespace(expand=lambda: expanded.append(1))
        controller = app._command_palette
        assert _ids(controller, "On this dashboard") == [f"expand:{instance_id}"]
        controller.run(f"expand:{instance_id}")
        assert expanded == [1]

    async def test_no_tile_items_in_edit_mode(self, app):
        _open(app, edit=True, components=[SELECTOR])
        editor = app._editor
        editor._inspect_tiles[editor._tile_items[0]["instance_id"]] = SimpleNamespace(
            expand=lambda: None
        )
        assert not _ids(app._command_palette, "On this dashboard")

    async def test_a_failing_group_leaves_the_actions(self, app, monkeypatch):
        def fail(*_args, **_kwargs):
            raise RuntimeError("store down")

        monkeypatch.setattr(app.store, "list_accessible", fail)
        assert _ids(app._command_palette) == ["action:theme", "action:home"]


class TestRun:
    @pytest.fixture
    def navigations(self, app):
        calls = []
        app._request_navigation = lambda path, search="": calls.append(path)
        return calls

    async def test_dashboard_navigates(self, app, navigations):
        _open(app)
        other = app.store.create_dashboard(app._user_id, "Depot")
        app._command_palette.run(f"dashboard:{other.dashboard_id}")
        assert navigations == [f"{DASH_ROUTE_PREFIX}{other.dashboard_id}"]

    async def test_component_opens_the_library_outside_edit_mode(self, app, navigations):
        _open(app)
        app._command_palette.run(f"component:{SELECTOR}")
        assert navigations == [COMPONENTS_ROUTE]
        assert not app._editor._tile_items

    async def test_component_is_added_in_edit_mode(self, app, navigations):
        _open(app, edit=True)
        app._command_palette.run(f"component:{SELECTOR}")
        assert [i["component_id"] for i in app._editor._tile_items] == [SELECTOR]
        assert app._editor.dirty
        assert not navigations

    async def test_unknown_component_is_ignored(self, app, navigations):
        _open(app, edit=True)
        app._command_palette.run("component:nope")
        assert not app._editor._tile_items
        assert not navigations

    async def test_home(self, app, navigations):
        _open(app)
        app._command_palette.run("action:home")
        assert navigations == ["/"]

    async def test_edit_and_back_to_view(self, app):
        _open(app)
        controller = app._command_palette
        controller.run("action:edit")
        assert app._editor.editable
        assert pn.state.location.search == "?edit=true"
        controller.run("action:view")
        assert not app._editor.editable
        assert pn.state.location.search == ""

    async def test_back_to_view_with_changes_asks_first(self, app):
        _open(app, edit=True)
        app._editor.add_component(SELECTOR)
        app._command_palette.run("action:view")
        assert app._unsaved_dialog.open
        assert app._editor.editable

    async def test_save(self, app):
        model = _open(app, edit=True)
        app._editor.add_component(SELECTOR)
        app._command_palette.run("action:save")
        assert not app._editor.dirty
        assert len(app.store._load_any(model.dashboard_id).items) == 1

    async def test_settings_opens_the_dialog(self, app):
        _open(app)
        app._command_palette.run("action:settings")
        assert app._editor._settings_dialog.open

    async def test_refresh(self, app):
        _open(app)
        refreshed = []
        app._editor.param.watch(lambda _event: refreshed.append(1), "refresh")
        app._command_palette.run("action:refresh")
        assert refreshed == [1]

    async def test_theme(self, app):
        dark = app._page.dark_theme
        app._command_palette.run("action:theme")
        assert app._page.dark_theme is not dark
        app._command_palette.run("action:theme")
        assert app._page.dark_theme is dark

    async def test_inspect_opens_the_inspection(self, app):
        _open(app, components=[SELECTOR])
        editor = app._editor
        editor.inspect_outputs = True
        item = editor._tile_items[0]
        editor._grid_view(item, editor._tile_objects[0])
        app._command_palette.run(f"inspect:{item['instance_id']}")
        assert editor._inspect_dialog.open
        assert editor._inspected_id == item["instance_id"]

    async def test_actions_without_a_dashboard_do_nothing(self, app):
        controller = app._command_palette
        for action in ("refresh", "edit", "save", "settings"):
            controller.run(f"action:{action}")
        assert not app._editor._settings_dialog.open


class TestShortcuts:
    async def test_edit_in_view_mode(self, app):
        _open(app)
        app._command_palette.run("shortcut:edit")
        assert app._editor.editable

    async def test_edit_needs_edit_rights(self, app):
        _open(app)
        app._dashboard_rights = lambda _dashboard: (False, False)
        app._editor.read_only = True
        app._command_palette.run("shortcut:edit")
        assert not app._editor.editable

    async def test_save_only_in_edit_mode(self, app):
        model = _open(app, edit=True)
        editor = app._editor
        editor.add_component(SELECTOR)
        app._show_view_mode()
        app._command_palette.run("shortcut:save")
        assert editor.dirty
        app._show_edit_mode()
        app._command_palette.run("shortcut:save")
        assert not editor.dirty
        assert len(app.store._load_any(model.dashboard_id).items) == 1

    async def test_save_refused_for_a_viewer(self, app):
        _open(app, edit=True)
        app._editor.add_component(SELECTOR)
        app._editor.read_only = True
        app._command_palette.run("shortcut:save")
        assert app._editor.dirty

    async def test_escape_returns_to_view(self, app):
        _open(app, edit=True)
        app._command_palette.run("shortcut:view")
        assert not app._editor.editable

    async def test_deselect_clears_the_canvas_selection_and_stays_in_edit_mode(self, app):
        _open(app, edit=True)
        editor = app._editor
        node = editor.add_component(SELECTOR)
        editor.dirty = False
        select_on_canvas(editor._flow, node_id=node)
        app._command_palette.run("shortcut:deselect")
        assert editor.editable
        assert editor._flow.selection == {"nodes": [], "edges": []}

    async def test_shortcuts_need_a_dashboard(self, app):
        app._show_edit_mode()
        app._command_palette.run("shortcut:view")
        app._command_palette.run("shortcut:save")
        assert app._editor.editable

    async def test_unknown_shortcut(self, app):
        _open(app)
        app._command_palette.run("shortcut:delete")
        assert not app._editor.editable


class TestMessages:
    async def test_open_sends_the_items(self, app):
        palette = app._command_palette.palette
        assert palette.items == []
        palette._handle_msg({"type": "open"})
        assert palette.items == app._command_palette.items()

    async def test_run_and_shortcut(self):
        commands = []
        palette = CommandPalette(on_command=commands.append)
        palette._handle_msg({"type": "run", "id": "action:home"})
        palette._handle_msg({"type": "shortcut", "name": "save"})
        palette._handle_msg({"type": "run", "id": 3})
        palette._handle_msg("open")
        assert commands == ["action:home", "shortcut:save"]

    async def test_without_handlers(self):
        palette = CommandPalette()
        palette._handle_msg({"type": "open"})
        palette._handle_msg({"type": "run", "id": "action:home"})
        assert palette.items == []

    async def test_controller_without_shell(self, app):
        app._shell = None
        assert CommandPaletteController.install(app) is None
