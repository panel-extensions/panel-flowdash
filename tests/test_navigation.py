"""Tests for sidebar navigation and canvas reset behavior.

These exercise the ``FlowDashApp`` menu click / action handling directly,
without a live Bokeh server, by injecting a ``Location`` onto ``pn.state``
and driving the registered callbacks the way the frontend would.
"""

import sys

import panel as pn
import param
import pytest

from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore


class FakeLocation(param.Parameterized):
    """A minimal stand-in for ``pn.state.location``.

    The real :class:`panel.io.location.Location` watches ``pathname`` and
    reaches for ``state.curdoc.session_context``, which does not exist
    outside a live server session. This exposes just the attributes the
    navigation code touches.
    """

    pathname = param.String(default="/")
    search = param.String(default="")


def _create_project(tmp_path):
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


@pytest.fixture
async def app(tmp_path):
    """A FlowDashApp with a fake Location so navigation code paths run."""
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _click_item(app, item):
    """Invoke the menu list click callback the way the frontend would."""
    for cb in app._menu_list._on_click_callbacks:
        cb(item)


def _fire_action(app, name, item):
    """Invoke an on_action callback the way the frontend would."""
    for cb in app._menu_list._on_action_callbacks.get(name, []):
        cb(item)


class TestCanvasReset:
    async def test_reset_clears_all_state(self, app):
        app._add_component_to_graph()
        assert app._tile_items
        assert app._flow.nodes

        app._reset_canvas()

        assert app._tile_items == []
        assert app._tile_objects == []
        assert app._edge_id_map == {}
        assert list(app._dataflow_graph.node_ids) == []
        assert app._flow.nodes == []
        assert app._flow.edges == []
        assert app._sidebar_container.objects == []

    async def test_create_new_dashboard_resets_canvas(self, app):
        """Creating a dashboard while another has nodes must clear the canvas."""
        app._add_component_to_graph()
        assert app._flow.nodes

        app._create_new_dashboard("Fresh")

        assert app._flow.nodes == []
        assert app._tile_items == []
        assert list(app._dataflow_graph.node_ids) == []
        assert app._current_dashboard.title == "Fresh"

    async def test_clear_components_resets_canvas(self, app):
        app._add_component_to_graph()
        app._add_component_to_graph()
        assert len(app._flow.nodes) == 2

        app._clear_components()

        assert app._flow.nodes == []
        assert list(app._dataflow_graph.node_ids) == []


class TestMenuClickNavigation:
    async def test_plain_item_click_navigates(self, app):
        calls = []
        app._navigate_to = lambda path: calls.append(path)

        _click_item(app, {"path": "/Analytics/page"})

        assert calls == ["/Analytics/page"]

    async def test_click_after_action_still_navigates(self, app):
        """Regression: an action must not swallow the next plain nav click."""
        calls = []
        app._navigate_to = lambda path: calls.append(path)

        # Enter edit mode via the Edit action on some dashboard.
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        edit_calls = []
        app._load_dashboard_edit = lambda did: edit_calls.append(did)
        _fire_action(app, "Edit", {"path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"})

        # Now a plain click on a different item must navigate immediately.
        _click_item(app, {"path": "/Analytics/page"})

        assert calls == ["/Analytics/page"]

    async def test_new_dashboard_click_opens_create_dialog(self, app):
        _click_item(app, {"path": "__new_dashboard__"})
        assert app._dialog.open is True
        assert app._dialog_context == {"action": "create"}

    async def test_click_current_path_in_edit_mode_switches_to_view(self, app):
        pn.state.location.param.update(pathname="/Analytics/page", search="?edit=true")
        switched = []
        app._show_view_mode = lambda: switched.append(True)

        _click_item(app, {"path": "/Analytics/page"})

        assert switched == [True]
        assert "edit=true" not in (pn.state.location.search or "")

    async def test_click_without_path_is_ignored(self, app):
        calls = []
        app._navigate_to = lambda path: calls.append(path)
        _click_item(app, {"label": "No path here"})
        assert calls == []


class TestRequestNavigation:
    async def test_clean_state_navigates_directly(self, app):
        calls = []
        app._navigate_to = lambda path: calls.append(path)
        app._dirty = False

        app._request_navigation("/Analytics/page")

        assert calls == ["/Analytics/page"]
        assert app._unsaved_dialog.open is False

    async def test_dirty_dashboard_prompts_before_navigating(self, app):
        calls = []
        app._navigate_to = lambda path: calls.append(path)
        app._current_dashboard = app.store.create_dashboard(app._user_id, "Dirty")
        app._dirty = True

        app._request_navigation("/Analytics/page")

        assert calls == []
        assert app._unsaved_dialog.open is True
        assert app._pending_navigation == "/Analytics/page"

    async def test_dirty_without_current_dashboard_navigates(self, app):
        """A dirty flag without a loaded dashboard should not block navigation."""
        calls = []
        app._navigate_to = lambda path: calls.append(path)
        app._current_dashboard = None
        app._dirty = True

        app._request_navigation("/Analytics/page")

        assert calls == ["/Analytics/page"]


class TestNavigateTo:
    async def test_updates_location_and_menu_active(self, app):
        app._load_page_layout = lambda: None

        app._navigate_to("/Analytics/page")

        assert pn.state.location.pathname == "/Analytics/page"
        assert pn.state.location.search == ""
        assert app._menu_list.active is not None


def _menu_index_for_path(app, path):
    """Return the (section, item) index of the menu entry with *path*."""
    for si, section in enumerate(app._menu_list.items):
        if section.get("path") == path:
            return (si,)
        for pi, item in enumerate(section.get("items", [])):
            if item.get("path") == path:
                return (si, pi)
    return None


class TestMenuActiveSync:
    """The nav menu highlight must track the loaded dashboard in every mode."""

    async def test_load_dashboard_view_mode_highlights_menu(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._refresh_sidebar_dashboards()
        path = f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"

        app._load_dashboard_sync(dash.dashboard_id, edit=False)

        assert app._menu_list.active == _menu_index_for_path(app, path)

    async def test_load_dashboard_edit_mode_highlights_menu(self, app):
        """Regression: edit mode bypasses _navigate_to but must still sync."""
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._refresh_sidebar_dashboards()
        path = f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"

        app._load_dashboard_sync(dash.dashboard_id, edit=True)

        assert app._menu_list.active == _menu_index_for_path(app, path)
        assert app._menu_list.active is not None

    async def test_switching_dashboards_moves_highlight(self, app):
        a = app.store.create_dashboard(app._user_id, "Dash A")
        b = app.store.create_dashboard(app._user_id, "Dash B")
        app._refresh_sidebar_dashboards()

        app._load_dashboard_sync(a.dashboard_id, edit=True)
        first = app._menu_list.active
        app._load_dashboard_sync(b.dashboard_id, edit=False)
        second = app._menu_list.active

        assert first == _menu_index_for_path(app, f"{DASH_ROUTE_PREFIX}{a.dashboard_id}")
        assert second == _menu_index_for_path(app, f"{DASH_ROUTE_PREFIX}{b.dashboard_id}")
        assert first != second

    async def test_create_new_dashboard_highlights_menu(self, app):
        app._create_new_dashboard("Fresh")
        path = f"{DASH_ROUTE_PREFIX}{app._current_dashboard.dashboard_id}"

        assert app._menu_list.active == _menu_index_for_path(app, path)
        assert app._menu_list.active is not None


@pytest.fixture
async def menubar_app(tmp_path):
    """A FlowDashApp configured with the header MenuBar navigation."""
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store, nav_variant="menubar")
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _find_menu_bar_item(items, label):
    """Depth-first search for a MenuBar item dict with the given label."""
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("label") == label:
            return item
        found = _find_menu_bar_item(item.get("items", []), label)
        if found is not None:
            return found
    return None


class TestMenuBarVariant:
    async def test_menubar_mounts_in_header(self, menubar_app):
        assert menubar_app._menu_bar is not None
        assert menubar_app._menu_bar in menubar_app._page.header

    async def test_drawer_default_keeps_header_empty(self, app):
        assert app._menu_bar is None
        assert app._page.header == []

    async def test_nav_content_omits_drawer_in_menubar(self, menubar_app):
        content = pn.pane.Markdown("x")
        assert menubar_app._nav_content(content) is content

    async def test_nav_content_wraps_drawer_in_drawer_mode(self, app):
        content = pn.pane.Markdown("x")
        wrapped = app._nav_content(content)
        assert app._nav_drawer in wrapped

    async def test_menubar_click_navigates(self, menubar_app):
        calls = []
        menubar_app._navigate_to = lambda path: calls.append(path)
        item = _find_menu_bar_item(menubar_app._menu_bar.items, "Overview")
        assert item is not None
        menubar_app._on_menu_bar_click(item)
        assert calls == ["/Analytics/page"]

    async def test_menubar_new_dashboard_opens_dialog(self, menubar_app):
        item = _find_menu_bar_item(menubar_app._menu_bar.items, "New Dashboard")
        assert item is not None
        menubar_app._on_menu_bar_click(item)
        assert menubar_app._dialog.open is True

    async def test_menubar_dashboard_submenu_has_admin_actions(self, menubar_app):
        dash = menubar_app.store.create_dashboard(menubar_app._user_id, "Dash A")
        menubar_app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")
        assert submenu is not None
        labels = [i["label"] for i in submenu["items"]]
        assert labels == ["Open", "Edit", "Rename", "Delete"]
        assert (
            _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")["items"][0]["path"]
            == f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"
        )

    async def test_menubar_delete_action_routes(self, menubar_app):
        dash = menubar_app.store.create_dashboard(menubar_app._user_id, "Dash A")
        menubar_app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")
        delete_item = next(i for i in submenu["items"] if i["label"] == "Delete")
        menubar_app._on_menu_bar_click(delete_item)
        assert menubar_app._dialog.open is True
        assert menubar_app._dialog_context == {
            "action": "delete",
            "dashboard_id": dash.dashboard_id,
        }
