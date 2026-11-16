"""Tests for sidebar navigation and canvas reset behavior.

These exercise the ``FlowDashApp`` menu click / action handling directly,
without a live Bokeh server, by injecting a ``Location`` onto ``pn.state``
and driving the registered callbacks the way the frontend would.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
import pytest

from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardItem, DashboardStore


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
        editor = app._editor
        editor.add_component("Analytics/selector")
        assert editor.layout or editor._tile_items
        assert editor._flow.nodes

        app._reset_canvas()

        assert editor._tile_items == []
        assert editor._tile_objects == []
        assert editor._edge_id_map == {}
        assert list(editor.graph.node_ids) == []
        assert editor._flow.nodes == []
        assert editor._flow.edges == []
        assert app._sidebar_container.objects == []

    async def test_create_new_dashboard_resets_canvas(self, app):
        """Creating a dashboard while another has nodes must clear the canvas."""
        editor = app._editor
        editor.add_component("Analytics/selector")
        assert editor._flow.nodes

        app._create_new_dashboard("Fresh")

        assert editor._flow.nodes == []
        assert editor._tile_items == []
        assert list(editor.graph.node_ids) == []
        assert app._current_dashboard.title == "Fresh"

    async def test_clear_components_resets_canvas(self, app):
        editor = app._editor
        editor.add_component("Analytics/selector")
        editor.add_component("Analytics/chart")
        assert len(editor._flow.nodes) == 2

        editor.clear()

        assert editor._flow.nodes == []
        assert list(editor.graph.node_ids) == []


class TestMenuClickNavigation:
    async def test_plain_item_click_navigates(self, app):
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)

        _click_item(app, {"path": "/Analytics/page"})

        assert calls == ["/Analytics/page"]

    async def test_click_after_action_still_navigates(self, app):
        """Regression: an action must not swallow the next plain nav click."""
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)

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
        app._navigate_to = lambda path, search="": calls.append(path)
        _click_item(app, {"label": "No path here"})
        assert calls == []


class TestRequestNavigation:
    async def test_clean_state_navigates_directly(self, app):
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)
        app._dirty = False

        app._request_navigation("/Analytics/page")

        assert calls == ["/Analytics/page"]
        assert app._unsaved_dialog.open is False

    async def test_dirty_dashboard_prompts_before_navigating(self, app):
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)
        app._current_dashboard = app.store.create_dashboard(app._user_id, "Dirty")
        app._dirty = True

        app._request_navigation("/Analytics/page")

        assert calls == []
        assert app._unsaved_dialog.open is True
        assert app._pending_navigation == ("/Analytics/page", "")

    async def test_dirty_without_current_dashboard_navigates(self, app):
        """A dirty flag without a loaded dashboard should not block navigation."""
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)
        app._current_dashboard = None
        app._dirty = True

        app._request_navigation("/Analytics/page")

        assert calls == ["/Analytics/page"]


class _GatedLoads:
    """Hold back the component imports of chosen dashboards until released."""

    def __init__(self, app, gated_component):
        self.gated_component = gated_component
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self._original = app._ensure_components_loaded
        app._ensure_components_loaded = self

    async def __call__(self, component_ids=None):
        if component_ids and self.gated_component in component_ids:
            self.started.set()
            await self.release.wait()
        await self._original(component_ids)


def _dashboard(app, title, component_id, **settings):
    dash = app.store.create_dashboard(app._user_id, title)
    dash.items = [DashboardItem(instance_id=f"{title}-1", component_id=component_id)]
    dash.settings = settings
    app.store.save_dashboard(dash)
    return dash


def _goto(app, path):
    pn.state.location.param.update(pathname=path, search="")
    return asyncio.create_task(app._load_page_layout())


def _shows_progress(app):
    return any(isinstance(obj, pmui.LinearProgress) for obj in app._page.main)


class TestStaleNavigation:
    """A slow load must not overwrite a navigation that started after it."""

    async def test_slow_dashboard_does_not_replace_newer_dashboard(self, app):
        slow = _dashboard(app, "Slow", "Analytics/selector")
        fast = _dashboard(app, "Fast", "Analytics/chart")
        gate = _GatedLoads(app, "Analytics/selector")

        first = _goto(app, f"{DASH_ROUTE_PREFIX}{slow.dashboard_id}")
        await gate.started.wait()
        await _goto(app, f"{DASH_ROUTE_PREFIX}{fast.dashboard_id}")
        assert app._current_dashboard.dashboard_id == fast.dashboard_id

        gate.release.set()
        await asyncio.wait_for(first, 5)

        assert first.done() and first.exception() is None
        assert app._current_dashboard.dashboard_id == fast.dashboard_id
        assert [item["instance_id"] for item in app._editor._tile_items] == ["Fast-1"]

    async def test_slow_pending_page_import_does_not_replace_newer_dashboard(self, app):
        first_dash = _dashboard(app, "First", "Analytics/selector")
        second_dash = _dashboard(app, "Second", "Analytics/chart")
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def resolve_pending_entries():
            calls.append(None)
            if len(calls) == 1:
                started.set()
                await release.wait()

        app._resolve_pending_entries = resolve_pending_entries

        first = _goto(app, f"{DASH_ROUTE_PREFIX}{first_dash.dashboard_id}")
        await started.wait()
        await _goto(app, f"{DASH_ROUTE_PREFIX}{second_dash.dashboard_id}")
        release.set()
        await asyncio.wait_for(first, 5)

        assert app._current_dashboard.dashboard_id == second_dash.dashboard_id
        assert [item["instance_id"] for item in app._editor._tile_items] == ["Second-1"]

    async def test_slow_dashboard_progress_bar_does_not_replace_newer_page(self, app):
        app._loading_screen = partial(FlowDashApp._loading_screen, app, delay=0.05)
        slow = _dashboard(app, "Slow", "Analytics/selector")
        fast = _dashboard(app, "Fast", "Analytics/chart")
        gate = _GatedLoads(app, "Analytics/selector")

        first = _goto(app, f"{DASH_ROUTE_PREFIX}{slow.dashboard_id}")
        await gate.started.wait()
        await _goto(app, f"{DASH_ROUTE_PREFIX}{fast.dashboard_id}")
        await asyncio.sleep(0.1)

        assert not _shows_progress(app)
        gate.release.set()
        await asyncio.wait_for(first, 5)

    async def test_slow_dashboard_does_not_replace_launcher(self, app):
        slow = _dashboard(app, "Slow", "Analytics/selector")
        gate = _GatedLoads(app, "Analytics/selector")

        first = _goto(app, f"{DASH_ROUTE_PREFIX}{slow.dashboard_id}")
        await gate.started.wait()
        await _goto(app, "/")
        gate.release.set()
        await asyncio.wait_for(first, 5)

        assert app._current_dashboard is None
        assert app._editor._tile_items == []

    async def test_slow_edit_load_does_not_replace_newer_dashboard(self, app):
        slow = _dashboard(app, "Slow", "Analytics/selector")
        fast = _dashboard(app, "Fast", "Analytics/chart")
        gate = _GatedLoads(app, "Analytics/selector")

        first = asyncio.create_task(app._load_dashboard_edit(slow.dashboard_id))
        await gate.started.wait()
        await _goto(app, f"{DASH_ROUTE_PREFIX}{fast.dashboard_id}")
        gate.release.set()
        await asyncio.wait_for(first, 5)

        assert app._current_dashboard.dashboard_id == fast.dashboard_id

    async def test_navigation_cancels_in_flight_page(self, app):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_instantiate(entry, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        app._instantiate_entry = slow_instantiate
        first = _goto(app, "/Analytics/page")
        await started.wait()
        await _goto(app, "/")
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(first, 1)

        assert not any(isinstance(obj, pmui.Column) for obj in app._page.main)

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="needs Task.cancelling()")
    async def test_cancelling_navigation_itself_propagates(self, app):
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        outer = asyncio.create_task(app._run_navigation_task(hang()))
        await started.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 1)

    async def test_superseded_navigation_task_returns_none(self, app):
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        outer = asyncio.create_task(app._run_navigation_task(hang()))
        await started.wait()
        app._main_task.cancel()
        assert await asyncio.wait_for(outer, 1) is None

    async def test_access_denied_page_still_cancels_in_flight_page(self, app):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_instantiate(entry, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        app._instantiate_entry = slow_instantiate
        first = _goto(app, "/Analytics/page")
        await started.wait()
        app._can_access_entry = lambda entry: False
        await _goto(app, "/Analytics/page")
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(first, 1)


class _Timer:
    def __init__(self, period):
        self.period = period
        self.stopped = False

    def stop(self):
        self.stopped = True


class TestRefreshTimerNavigation:
    """The refresh timer follows the dashboard that navigation ends on."""

    @pytest.fixture
    def timers(self, monkeypatch):
        created = []

        def add_periodic_callback(callback, period=500, **_):
            created.append(_Timer(period))
            return created[-1]

        monkeypatch.setattr(pn.state, "add_periodic_callback", add_periodic_callback)
        return created

    @staticmethod
    def _running(timers):
        return [t.period for t in timers if not t.stopped]

    async def test_slow_dashboard_does_not_apply_its_settings_over_newer_one(self, app, timers):
        slow = _dashboard(app, "Slow", "Analytics/selector", refresh_period=5)
        fast = _dashboard(app, "Fast", "Analytics/chart", refresh_period=7)
        gate = _GatedLoads(app, "Analytics/selector")

        first = _goto(app, f"{DASH_ROUTE_PREFIX}{slow.dashboard_id}")
        await gate.started.wait()
        await _goto(app, f"{DASH_ROUTE_PREFIX}{fast.dashboard_id}")
        gate.release.set()
        await asyncio.wait_for(first, 5)

        assert app._editor.settings.refresh_period == 7
        assert self._running(timers) == [7000]

    async def test_leaving_for_a_page_stops_the_timer(self, app, timers):
        dash = _dashboard(app, "Live", "Analytics/chart", refresh_period=5)
        await _goto(app, f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}")
        assert self._running(timers) == [5000]

        await _goto(app, "/Analytics/page")
        assert self._running(timers) == []

        await _goto(app, f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}")
        assert self._running(timers) == [5000]

    async def test_deleting_the_shown_dashboard_stops_the_timer(self, app, timers):
        dash = _dashboard(app, "Live", "Analytics/chart", refresh_period=5)
        await _goto(app, f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}")

        assert app._delete_dashboard(dash.dashboard_id)
        assert self._running(timers) == []


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


def _labels(items):
    """Labels of menu *items*, with dividers as ``"---"``."""
    return ["---" if item is None else item["label"] for item in items]


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
        menubar_app._navigate_to = lambda path, search="": calls.append(path)
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
        assert _labels(submenu["items"]) == [
            "Open",
            "Edit",
            "Rename",
            "Duplicate",
            "Share",
            "Download as file",
            "Delete",
        ]
        assert (
            _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")["items"][0]["path"]
            == f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"
        )

    async def test_menubar_delete_action_routes(self, menubar_app):
        dash = menubar_app.store.create_dashboard(menubar_app._user_id, "Dash A")
        menubar_app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")
        delete_item = next(i for i in submenu["items"] if i and i["label"] == "Delete")
        menubar_app._on_menu_bar_click(delete_item)
        assert menubar_app._dialog.open is True
        assert menubar_app._dialog_context == {
            "action": "delete",
            "dashboard_id": dash.dashboard_id,
        }


@pytest.fixture
async def left_nav_app(tmp_path):
    """A FlowDashApp with the navigation drawer docked on the left."""
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store, nav_variant="left")
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


class TestNavSide:
    async def test_default_anchors_drawer_right(self, app):
        assert app._nav_drawer.anchor == "right"

    async def test_default_places_drawer_after_content(self, app):
        content = pn.pane.Markdown("x")
        wrapped = app._nav_content(content)
        assert list(wrapped) == [content, app._nav_drawer]

    async def test_left_anchors_drawer_left(self, left_nav_app):
        assert left_nav_app._nav_drawer.anchor == "left"

    async def test_left_places_drawer_before_content(self, left_nav_app):
        content = pn.pane.Markdown("x")
        wrapped = left_nav_app._nav_content(content)
        assert list(wrapped) == [left_nav_app._nav_drawer, content]


class ScopedProject:
    """A served project whose components record the fact of their own import.

    Each instance gets its own section name, because importing a module caches it
    under that name for the rest of the session: a shared section would leave
    later tests unable to observe an import at all.
    """

    def __init__(self, app, tmp_path, section):
        self.app = app
        self.tmp_path = tmp_path
        self.section = section
        self.used = f"{section}/used"
        self.unused = f"{section}/unused"

    def imported(self, name):
        return (self.tmp_path / f"{name}.imported").exists()

    @property
    def any_imported(self):
        return self.imported("used") or self.imported("unused")

    def seed(self, component_ids):
        """Persist a dashboard placing *component_ids* and return it."""
        dash = self.app.store.create_dashboard(self.app._user_id, "Dash A")
        dash.items = [
            DashboardItem(instance_id=f"n{i}", component_id=cid)
            for i, cid in enumerate(component_ids)
        ]
        self.app.store.save_dashboard(dash)
        return dash


@pytest.fixture
async def scoped(tmp_path, request):
    """A FlowDashApp over a project that records which components get imported."""
    section = "Scoped_" + request.node.name.replace("[", "_").replace("]", "")
    directory = tmp_path / section
    directory.mkdir()
    (directory / "__init__.py").write_text("")
    for name in ("used", "unused"):
        (directory / f"{name}.py").write_text(
            "import pathlib\n"
            "from panel_flowdash import register\n\n"
            f"pathlib.Path({str(tmp_path)!r}, '{name}.imported').touch()\n\n"
            "@register(page=False, component=True, provides=['company'])\n"
            "def app(config):\n"
            f"    return '{name}'\n"
        )

    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        app = FlowDashApp(project_dir=tmp_path, store=store)
        yield ScopedProject(app, tmp_path, section)
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


class TestScopedRouteLoading:
    """Routing to a dashboard must import only the components it places.

    Importing a module runs its top-level code, so a component doing I/O at
    import time would otherwise tax every dashboard and the editor route.
    """

    async def test_serving_app_imports_nothing(self, scoped):
        """Construction only scans; no component module is imported."""
        assert not scoped.any_imported

    async def test_dashboard_route_imports_only_placed_components(self, scoped):
        dash = scoped.seed([scoped.used])

        pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"
        await scoped.app._load_page_layout()

        assert scoped.imported("used")
        assert not scoped.imported("unused")
        assert scoped.app._current_dashboard.dashboard_id == dash.dashboard_id

    async def test_view_mode_imports_only_placed_components(self, scoped):
        """View mode ran the full-catalog load before the fix, not just edit mode."""
        dash = scoped.seed([scoped.used])

        await scoped.app._load_dashboard(dash.dashboard_id, edit=False)

        assert scoped.app._editor.editable is False
        assert scoped.imported("used")
        assert not scoped.imported("unused")

    async def test_empty_dashboard_route_imports_nothing(self, scoped):
        dash = scoped.seed([])

        await scoped.app._load_dashboard(dash.dashboard_id, edit=False)

        assert not scoped.any_imported

    async def test_editor_route_imports_nothing_until_a_component_is_added(self, scoped):
        pn.state.location.pathname = "/components"
        await scoped.app._load_page_layout()

        assert not scoped.any_imported

        scoped.app._editor.add_component(scoped.used)

        assert scoped.imported("used")
        assert not scoped.imported("unused")

    async def test_missing_dashboard_route_imports_nothing(self, scoped):
        await scoped.app._load_dashboard("no-such-dashboard", edit=False)

        assert not scoped.any_imported


_COPY_ACTION = {"label": "Copy", "icon": "content_copy"}


class CopyFlowDashApp(FlowDashApp):
    """A subclass adding a "Copy" action through the action tuples alone."""

    _ADMIN_DASHBOARD_ACTIONS = (*FlowDashApp._ADMIN_DASHBOARD_ACTIONS, _COPY_ACTION)
    _VIEWER_DASHBOARD_ACTIONS = (_COPY_ACTION,)

    def __init__(self, **params):
        super().__init__(**params)
        self.copied = []

    def _on_dashboard_action(self, action, dashboard_id, title):
        if action == "Copy":
            self.copied.append((dashboard_id, title))
        else:
            super()._on_dashboard_action(action, dashboard_id, title)


_CLONE_ACTION = {"action": "copy", "label": "Clone", "icon": "content_copy"}


class KeyedActionFlowDashApp(FlowDashApp):
    """A subclass whose action key differs from its display label."""

    _ADMIN_DASHBOARD_ACTIONS = (*FlowDashApp._ADMIN_DASHBOARD_ACTIONS, _CLONE_ACTION)
    _VIEWER_DASHBOARD_ACTIONS = (_CLONE_ACTION,)

    def __init__(self, **params):
        super().__init__(**params)
        self.duplicated = []

    def _on_dashboard_action(self, action, dashboard_id, title):
        if action == "copy":
            self.duplicated.append((dashboard_id, title))
        else:
            super()._on_dashboard_action(action, dashboard_id, title)


@asynccontextmanager
async def _subclass_app(tmp_path, cls, **params):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = cls(project_dir=tmp_path, store=store, **params)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


@pytest.fixture
async def copy_app(tmp_path):
    async with _subclass_app(tmp_path, CopyFlowDashApp) as instance:
        yield instance


@pytest.fixture
async def copy_menubar_app(tmp_path):
    async with _subclass_app(tmp_path, CopyFlowDashApp, nav_variant="menubar") as instance:
        yield instance


@pytest.fixture
async def keyed_app(tmp_path):
    async with _subclass_app(tmp_path, KeyedActionFlowDashApp) as instance:
        yield instance


@pytest.fixture
async def keyed_menubar_app(tmp_path):
    async with _subclass_app(tmp_path, KeyedActionFlowDashApp, nav_variant="menubar") as instance:
        yield instance


def _launcher_menus(app):
    """The actions MenuButton of each dashboard card in the launcher."""
    return list(app._build_launcher()._menus.values())


def _click_menu(menu, label):
    """Click the item with *label* through pmui's message handler, as the frontend does."""
    index = next(i for i, item in enumerate(menu.items) if item and item["label"] == label)
    menu._handle_msg({"type": "click", "item": index})


def _menu_bar_path(items, label, path=()):
    """The index path pmui's MenuBar sends for the item with *label*."""
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("label") == label:
            return [*path, i]
        found = _menu_bar_path(item.get("items", []), label, (*path, i))
        if found:
            return found
    return None


def _click_menu_bar(app, *labels):
    """Click the MenuBar item at the end of *labels* through pmui's message handler."""
    items = app._menu_bar.items
    path = []
    for label in labels:
        sub = _menu_bar_path(items, label)
        for idx in sub:
            items = items[idx].get("items", [])
        path += sub
    app._menu_bar._handle_msg({"type": "click", "path": path})


def _record_dispatch(app):
    calls = []
    app._on_dashboard_action = lambda label, did, title: calls.append((label, did, title))
    return calls


class TestLauncherCardMenu:
    async def test_admin_card_has_admin_actions(self, app):
        app.store.create_dashboard(app._user_id, "Dash A")
        (menu,) = _launcher_menus(app)
        assert _labels(menu.items) == [
            "Open",
            "Edit",
            "Rename",
            "Duplicate",
            "Share",
            "Download as file",
            "---",
            "Delete",
        ]

    async def test_viewer_card_has_read_actions(self, app):
        app.store.create_dashboard(app._user_id, "Dash A")
        app._can_administer_dashboard = lambda dashboard_id: False
        app._can_edit_dashboard = lambda dashboard: False
        (menu,) = _launcher_menus(app)
        assert _labels(menu.items) == ["Open", "Duplicate", "Download as file"]

    async def test_viewer_card_lists_subclass_viewer_action(self, copy_app):
        copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        copy_app._can_administer_dashboard = lambda dashboard_id: False
        copy_app._can_edit_dashboard = lambda dashboard: False
        (menu,) = _launcher_menus(copy_app)
        assert _labels(menu.items) == ["Open", "Copy"]


class TestDashboardActionDispatch:
    async def test_card_routes_through_dispatch(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        (menu,) = _launcher_menus(app)
        calls = _record_dispatch(app)
        _click_menu(menu, "Rename")
        assert calls == [("Rename", dash.dashboard_id, "Dash A")]

    async def test_nav_menu_routes_through_dispatch(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        calls = _record_dispatch(app)
        item = {"label": "Dash A", "path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"}
        for label in ("Edit", "Rename", "Delete"):
            _fire_action(app, label, item)
        assert calls == [
            ("Edit", dash.dashboard_id, "Dash A"),
            ("Rename", dash.dashboard_id, "Dash A"),
            ("Delete", dash.dashboard_id, "Dash A"),
        ]

    async def test_menubar_routes_through_dispatch(self, menubar_app):
        dash = menubar_app.store.create_dashboard(menubar_app._user_id, "Dash A")
        menubar_app._refresh_sidebar_dashboards()
        calls = _record_dispatch(menubar_app)
        submenu = _find_menu_bar_item(menubar_app._menu_bar.items, "Dash A")
        menubar_app._on_menu_bar_click(next(i for i in submenu["items"] if i["label"] == "Edit"))
        assert calls == [("Edit", dash.dashboard_id, "Dash A")]

    async def test_nav_create_opens_dialog_without_dispatch(self, app):
        calls = _record_dispatch(app)
        _fire_action(app, "Create", {"label": "New Dashboard", "path": "__new_dashboard__"})
        assert calls == []
        assert app._dialog.open is True
        assert app._dialog_context == {"action": "create"}

    async def test_card_rename_opens_dialog(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        (menu,) = _launcher_menus(app)
        _click_menu(menu, "Rename")
        assert app._dialog.open is True
        assert app._dialog.title == "Rename dashboard"
        assert app._dialog_name_input.value == "Dash A"
        assert app._dialog_context == {"action": "rename", "dashboard_id": dash.dashboard_id}

    async def test_nav_delete_opens_dialog(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        item = {"label": "Dash A", "path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"}
        _fire_action(app, "Delete", item)
        assert app._dialog.title == "Delete Dash A?"
        assert app._dialog_delete_input.visible is True
        assert app._dialog_context == {"action": "delete", "dashboard_id": dash.dashboard_id}

    async def test_card_edit_loads_dashboard_in_edit_mode(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        loaded = []
        app._load_dashboard_edit = lambda did: loaded.append(did)
        (menu,) = _launcher_menus(app)
        _click_menu(menu, "Edit")
        assert loaded == [dash.dashboard_id]
        assert pn.state.location.pathname == f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"
        assert pn.state.location.search == "?edit=true"

    async def test_edit_with_unsaved_changes_prompts(self, app):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._current_dashboard = app.store.create_dashboard(app._user_id, "Dirty")
        app._dirty = True
        loaded = []
        app._load_dashboard_edit = lambda did: loaded.append(did)
        _fire_action(app, "Edit", {"path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"})
        assert loaded == []
        assert app._unsaved_dialog.open is True
        assert app._pending_navigation == (f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}", "?edit=true")

    @pytest.mark.parametrize("button", ["Discard", "Save & Continue"])
    async def test_edit_after_unsaved_prompt_opens_edit_mode(self, app, button):
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._current_dashboard = app.store.create_dashboard(app._user_id, "Dirty")
        app._dirty = True
        saved = []
        app._save_current_dashboard = lambda: saved.append(True) or True
        app._load_page_layout = lambda: None
        _fire_action(app, "Edit", {"path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"})

        (row,) = (o for o in app._unsaved_dialog.objects[0] if isinstance(o, pn.Row))
        next(b for b in row if b.label == button).clicks += 1

        assert saved == ([True] if button == "Save & Continue" else [])
        assert app._unsaved_dialog.open is False
        assert pn.state.location.pathname == f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"
        assert pn.state.location.search == "?edit=true"
        await FlowDashApp._load_page_layout(app)
        assert app._current_dashboard.dashboard_id == dash.dashboard_id
        assert app._editor.editable is True


class TestSubclassAction:
    async def test_action_in_nav_menu_items(self, copy_app):
        copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        (item,) = [i for i in copy_app._get_dashboard_menu_items() if i["label"] == "Dash A"]
        assert _labels(item["actions"]) == [
            "Edit",
            "Rename",
            "Duplicate",
            "Share",
            "Download as file",
            "Delete",
            "Copy",
        ]

    async def test_action_from_card(self, copy_app):
        dash = copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        (menu,) = _launcher_menus(copy_app)
        _click_menu(menu, "Copy")
        assert copy_app.copied == [(dash.dashboard_id, "Dash A")]

    async def test_action_from_nav_menu(self, copy_app):
        dash = copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        item = {"label": "Dash A", "path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"}
        _fire_action(copy_app, "Copy", item)
        assert copy_app.copied == [(dash.dashboard_id, "Dash A")]

    async def test_viewer_action_from_card(self, copy_app):
        dash = copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        copy_app._can_administer_dashboard = lambda dashboard_id: False
        copy_app._can_edit_dashboard = lambda dashboard: False
        (menu,) = _launcher_menus(copy_app)
        _click_menu(menu, "Copy")
        assert copy_app.copied == [(dash.dashboard_id, "Dash A")]

    async def test_builtin_actions_still_reach_super(self, copy_app):
        dash = copy_app.store.create_dashboard(copy_app._user_id, "Dash A")
        item = {"label": "Dash A", "path": f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}"}
        _fire_action(copy_app, "Rename", item)
        assert copy_app.copied == []
        assert copy_app._dialog_context == {
            "action": "rename",
            "dashboard_id": dash.dashboard_id,
        }

    async def test_admin_action_in_menu_bar(self, copy_menubar_app):
        app = copy_menubar_app
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(app._menu_bar.items, "Dash A")
        assert _labels(submenu["items"]) == [
            "Open",
            "Edit",
            "Rename",
            "Duplicate",
            "Share",
            "Download as file",
            "Delete",
            "Copy",
        ]
        _click_menu_bar(app, "Dash A", "Copy")
        assert app.copied == [(dash.dashboard_id, "Dash A")]

    async def test_viewer_action_in_menu_bar(self, copy_menubar_app):
        app = copy_menubar_app
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._can_administer_dashboard = lambda dashboard_id: False
        app._can_edit_dashboard = lambda dashboard: False
        app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(app._menu_bar.items, "Dash A")
        assert _labels(submenu["items"]) == ["Open", "Copy"]
        _click_menu_bar(app, "Dash A", "Copy")
        assert app.copied == [(dash.dashboard_id, "Dash A")]

    async def test_menu_bar_entries_drop_action_only_keys(self, copy_menubar_app):
        app = copy_menubar_app
        app._ADMIN_DASHBOARD_ACTIONS = (
            {"label": "Copy", "icon": "content_copy", "inline": True, "toggle": True, "items": []},
        )
        app.store.create_dashboard(app._user_id, "Dash A")
        app._refresh_sidebar_dashboards()
        submenu = _find_menu_bar_item(app._menu_bar.items, "Dash A")
        copy = next(i for i in submenu["items"] if i["label"] == "Copy")
        assert not {"inline", "toggle", "items"} & set(copy)


class TestActionKey:
    """An action with an ``"action"`` key dispatches under that key, as pmui does."""

    async def test_card_dispatches_action_key(self, keyed_app):
        dash = keyed_app.store.create_dashboard(keyed_app._user_id, "Dash A")
        (menu,) = _launcher_menus(keyed_app)
        _click_menu(menu, "Clone")
        assert keyed_app.duplicated == [(dash.dashboard_id, "Dash A")]

    async def test_drawer_dispatches_action_key(self, keyed_app):
        dash = keyed_app.store.create_dashboard(keyed_app._user_id, "Dash A")
        keyed_app._refresh_sidebar_dashboards()
        index = _menu_index_for_path(keyed_app, f"{DASH_ROUTE_PREFIX}{dash.dashboard_id}")
        keyed_app._menu_list._handle_msg({"type": "action", "action": "copy", "item": list(index)})
        assert keyed_app.duplicated == [(dash.dashboard_id, "Dash A")]

    async def test_menu_bar_dispatches_action_key(self, keyed_menubar_app):
        app = keyed_menubar_app
        dash = app.store.create_dashboard(app._user_id, "Dash A")
        app._refresh_sidebar_dashboards()
        _click_menu_bar(app, "Dash A", "Clone")
        assert app.duplicated == [(dash.dashboard_id, "Dash A")]
