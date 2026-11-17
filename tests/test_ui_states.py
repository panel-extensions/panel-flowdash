"""Tests for the empty, error and system states: not found, access denied, loading, unsaved."""

import asyncio
import sys
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import pytest

from panel_flowdash.app import ACCESS_DENIED_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.auth import AuthConfig, Permission, make_authorize_callback
from panel_flowdash.command import serve as serve_module
from panel_flowdash.command.serve import _NotFoundHandler
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    MemoryDashboardStore,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import build_registry
from panel_flowdash.ui.states import (
    CHANGES_LIMIT,
    EmptyState,
    LoadingSkeleton,
    access_denied,
    describe_changes,
    first_data_source,
    signed_in_as,
    similar_dashboards,
)
from tests.test_editor import COMPONENTS, SELECTOR
from tests.test_navigation import FakeLocation, _create_project, _shows_progress

_RESTRICTED_PAGE = (
    "from panel_flowdash import register\n\n"
    "@register(page=True, title='Fleet Finance', allow_users=['finance-lead'])\n"
    "def app():\n"
    "    return 'finance only'\n"
)


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    (tmp_path / "Analytics" / "finance.py").write_text(_RESTRICTED_PAGE)
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


def _private_dashboard(app, owner="maya", title="Fleet Budget"):
    model = DashboardModel(dashboard_id="f1ee7f1a2c90", user_id=owner, title=title)
    model.permission = Permission(allow_users=frozenset({owner}))
    app.store.save_dashboard(model)
    return model


class _Shown(t.NamedTuple):
    title: str
    text: str
    layout: pn.Column

    @property
    def actions(self) -> list:
        return list(self.layout[-1])

    @property
    def suggestions(self):
        """The similar-dashboards menu, if any."""
        menus = self.layout.select(pmui.MenuList)
        return menus[0] if menus else None


def _main_state(app) -> _Shown:
    """The empty state shown as the page's content, inside the navigation wrapper."""
    (layout,) = [
        obj
        for obj in app._page.main[0].select(pn.Column)
        if "flowdash-empty-state" in obj.css_classes
    ]
    return _Shown(layout[1].object, layout[2].object, layout)


def _texts(state) -> str:
    return f"{state.title} {state.text}"


class TestEmptyState:
    def test_tones_use_theme_colours(self):
        for tone, colour in (("primary", "primary.main"), ("error", "error.main")):
            state = EmptyState(icon="lock", title="t", tone=tone)
            assert state._icon.sx["color"] == colour
            assert "--mui-palette" in state._icon.sx["bgcolor"]
        neutral = EmptyState(icon="lock", title="t", tone="neutral")
        assert neutral._icon.sx["bgcolor"] == "action.selected"

    def test_text_and_actions_hidden_when_empty(self):
        state = EmptyState(icon="info", title="Nothing here")
        assert not state._text.visible
        assert not state._layout[-1].visible

    def test_icon_is_a_material_symbol(self):
        assert EmptyState(icon="search_off", title="t")._icon.content == ":material/search_off:"


class TestSimilarDashboards:
    @staticmethod
    def _models():
        return [
            DashboardModel(dashboard_id="e98eea8f0338", user_id="a", title="US Wind Turbines"),
            DashboardModel(dashboard_id="b7c41d09aa21", user_id="a", title="Texas Repowering"),
            DashboardModel(dashboard_id="0123456789ab", user_id="a", title="Solar Farms"),
        ]

    def test_id_with_a_typo(self):
        (match,) = similar_dashboards("e98eea8f0339", self._models())
        assert match.title == "US Wind Turbines"

    def test_partial_title(self):
        assert [m.title for m in similar_dashboards("wind", self._models())] == [
            "US Wind Turbines"
        ]

    def test_unrelated_reference_matches_nothing(self):
        assert similar_dashboards("zzzzzzzzzzzz", self._models()) == []
        assert similar_dashboards("  ", self._models()) == []

    def test_limit(self):
        models = [
            DashboardModel(dashboard_id=f"wind{i}", user_id="a", title=f"Wind {i}")
            for i in range(5)
        ]
        assert len(similar_dashboards("wind", models, limit=2)) == 2


class TestNotFoundAndDenied:
    async def test_unknown_id_is_not_found_with_suggestions(self, app):
        model = app.store.create_dashboard(app._user_id, "US Wind Turbines")
        typo = model.dashboard_id[:-1] + ("0" if model.dashboard_id[-1] != "0" else "1")
        app._load_dashboard_sync(typo)
        state = _main_state(app)
        assert state.title == "This dashboard does not exist"
        assert typo in state.text
        menu = state.suggestions
        assert [item["dashboard_id"] for item in menu.items] == [model.dashboard_id]
        assert [b.label for b in state.actions] == ["All dashboards"]

    async def test_existing_dashboard_without_access_is_denied_not_missing(self, app):
        """Regression: a dashboard you may not see used to say "Dashboard not found"."""
        private = _private_dashboard(app)
        app._load_dashboard_sync(private.dashboard_id)
        state = _main_state(app)
        assert state.title == "You don't have access to this dashboard"
        assert "Ask the owner, maya, to add you." in state.text
        assert f"signed in as {app._identity.user}" in state.text
        assert [b.label for b in state.actions] == ["Go home"]

    async def test_denied_page_never_shows_the_title(self, app):
        private = _private_dashboard(app, title="Secret Merger Plans")
        app._load_dashboard_sync(private.dashboard_id)
        state = _main_state(app)
        assert "Secret" not in _texts(state)
        assert private.title not in str(state.layout)

    async def test_private_dashboards_are_never_suggested(self, app):
        _private_dashboard(app, title="Fleet Budget")
        app._load_dashboard_sync("fleet budget")
        state = _main_state(app)
        assert state.title == "This dashboard does not exist"
        assert state.suggestions is None

    async def test_suggestion_opens_the_dashboard(self, app):
        model = app.store.create_dashboard(app._user_id, "Wind")
        navigated = []
        app._request_navigation = lambda path, search="": navigated.append(path)
        state = app._missing_dashboard_view("wind")
        (menu,) = state.__panel__().select(pmui.MenuList)
        for callback in menu._on_click_callbacks:
            callback(menu.items[0])
        assert navigated == [f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"]

    async def test_go_home(self, app):
        navigated = []
        app._request_navigation = lambda path, search="": navigated.append(path)
        state = app._access_denied_view(owner="maya")
        state.actions[0].clicks += 1
        assert navigated == ["/"]

    def test_anonymous_user_is_not_signed_in(self):
        assert signed_in_as("anonymous") == "You're not signed in."
        assert signed_in_as("alice") == "You're signed in as alice."

    def test_page_without_owner_asks_an_administrator(self):
        state = access_denied(user="alice", subject="Fleet Finance")
        assert state.title == "You don't have access to Fleet Finance"
        assert "administrator" in state.text
        assert state.actions == []


class TestRoutes:
    async def test_unknown_route_is_an_in_app_404(self, app):
        """Regression: an unknown path used to show the bare text "Invalid URL: ..."."""
        pn.state.location.pathname = "/reports/missing"
        await app._load_page_layout()
        state = _main_state(app)
        assert state.title == "This page does not exist"
        assert "/reports/missing" in state.text

    async def test_dash_route_without_id(self, app):
        pn.state.location.pathname = "/dash/"
        await app._load_page_layout()
        assert _main_state(app).title == "This page does not exist"

    async def test_access_denied_route_names_the_page(self, app):
        pn.state.location.param.update(
            pathname=ACCESS_DENIED_ROUTE, search="?page=%2FAnalytics%2Ffinance"
        )
        await app._load_page_layout()
        state = _main_state(app)
        assert state.title == "You don't have access to Fleet Finance"

    async def test_access_denied_route_for_an_allowed_page_opens_it(self, app, monkeypatch):
        navigated = []
        monkeypatch.setattr(app, "_navigate_to", lambda path, search="": navigated.append(path))
        monkeypatch.setattr(pn.state, "execute", lambda callback, schedule=False: callback())
        pn.state.location.param.update(
            pathname=ACCESS_DENIED_ROUTE, search="?page=%2FAnalytics%2Fpage"
        )
        await app._load_page_layout()
        assert navigated == ["/Analytics/page"]

    async def test_access_denied_route_for_an_unknown_page_is_a_404(self, app):
        pn.state.location.param.update(pathname=ACCESS_DENIED_ROUTE, search="?page=%2Fnope")
        await app._load_page_layout()
        assert _main_state(app).title == "This page does not exist"

    def test_build_routes_serves_the_access_denied_route(self, tmp_path):
        _create_project(tmp_path)
        routes = FlowDashApp.build_routes(tmp_path, store=MemoryDashboardStore())
        assert ACCESS_DENIED_ROUTE in routes


class TestAuthorizeRedirect:
    @staticmethod
    def _registry(tmp_path):
        _create_project(tmp_path)
        (tmp_path / "Analytics" / "finance.py").write_text(_RESTRICTED_PAGE)
        return build_registry(tmp_path)

    def test_refused_page_redirects_to_the_denied_route(self, tmp_path):
        cb = make_authorize_callback(
            self._registry(tmp_path), AuthConfig(), denied_route=ACCESS_DENIED_ROUTE
        )
        assert cb(None, "/Analytics/finance") == "/access-denied?page=%2FAnalytics%2Ffinance"
        assert cb(None, "/Analytics/page") is True

    def test_redirect_keeps_the_url_prefix(self, tmp_path):
        cb = make_authorize_callback(
            self._registry(tmp_path),
            AuthConfig(),
            denied_route=ACCESS_DENIED_ROUTE,
            url_prefix="/app",
        )
        assert cb(None, "/app/Analytics/finance") == (
            "/app/access-denied?page=%2FAnalytics%2Ffinance"
        )

    def test_without_denied_route_refuses(self, tmp_path):
        cb = make_authorize_callback(self._registry(tmp_path), AuthConfig())
        assert cb(None, "/Analytics/finance") is False


class TestNotFoundHandler:
    class _Request:
        def __init__(self, path):
            self.path = path

    def test_websocket_requests_get_the_websocket_handler(self, monkeypatch):
        made = []
        monkeypatch.setattr(
            serve_module, "_NotFoundWSHandler", lambda app, request, **kw: made.append(request)
        )
        request = self._Request("/nowhere/ws")
        _NotFoundHandler.__new__(_NotFoundHandler, object(), request)
        assert made == [request]

    def test_page_requests_get_the_document_handler(self):
        handler = _NotFoundHandler.__new__(_NotFoundHandler, object(), self._Request("/nowhere"))
        assert type(handler) is _NotFoundHandler

    def test_serve_installs_it_as_the_default_handler(self, monkeypatch):
        monkeypatch.setattr(
            serve_module._PanelServe, "customize_kwargs", lambda self, args, kw: {**kw}
        )
        kwargs = serve_module.Serve.customize_kwargs(
            object.__new__(serve_module.Serve), None, {"extra_patterns": []}
        )
        assert kwargs["default_handler_class"] is _NotFoundHandler


class TestLoading:
    async def test_loading_screen_is_a_skeleton_inside_the_navigation(self, app):
        """Regression: loading used to replace the page, rail included, with a progress bar."""
        app._loading_screen = partial(FlowDashApp._loading_screen, app, delay=0)
        async with app._loading_screen():
            await asyncio.sleep(0.05)
            content = app._page.main[0]
            assert "flowdash-loading" in content.css_classes
            assert list(app._page.rail) == app._shell.rail_content
            assert _shows_progress(app)

    def test_skeleton_tiles(self):
        assert len(LoadingSkeleton(tiles=3)._layout[1]) == 3


class TestDescribeChanges:
    @staticmethod
    def _models():
        saved = DashboardModel(dashboard_id="d", user_id="u", title="T")
        saved.items = [
            DashboardItem("src", "Demo/source", 0, 0, {}),
            DashboardItem("old", "Demo/chart", 100, 0, {}),
            DashboardItem("tbl", "Demo/table", 200, 0, {"title": "Turbines"}),
        ]
        saved.edges = [
            DashboardEdge("src", "out", "old", "in"),
            DashboardEdge("src", "out", "tbl", "data", [{"op": "head", "n": 5}]),
        ]
        saved.settings = {"refresh_period": 0}
        current = DashboardModel(dashboard_id="d", user_id="u", title="T")
        current.items = [
            DashboardItem("src", "Demo/source", 0, 0, {}),
            DashboardItem("tbl", "Demo/table", 200, 0, {"title": "Turbines", "page_size": 5}),
            DashboardItem("new", "Demo/slider", 0, 100, {}),
        ]
        current.edges = [
            DashboardEdge("src", "out", "tbl", "data", [{"op": "head", "n": 10}]),
            DashboardEdge("new", "value", "src", "n_rows"),
        ]
        current.settings = {"refresh_period": 60}
        return saved, current

    def test_lists_every_kind_of_change(self):
        titles = {"Demo/source": "Source", "Demo/chart": "Chart", "Demo/slider": "Slider"}
        changes = describe_changes(*self._models(), titles)
        assert changes == [
            ("add_circle", "Added Slider"),
            ("remove_circle", "Removed Chart"),
            ("tune", "Changed the settings of Turbines"),
            ("link", "Connected Slider to Source, n_rows"),
            ("link_off", "Disconnected Source to Chart, in"),
            ("functions", "Changed the transform into Turbines, data"),
            ("settings", "Changed dashboard settings: refresh period"),
        ]

    def test_moves_are_one_entry(self):
        saved, _ = self._models()
        moved = DashboardModel(**{**saved.__dict__, "items": list(saved.items)})
        moved.items = [
            DashboardItem(i.instance_id, i.component_id, i.x + 5, i.y, i.config)
            for i in saved.items
        ]
        assert describe_changes(saved, moved) == [("open_with", "Moved or resized components")]

    def test_new_dashboard_without_a_saved_model(self):
        _, current = self._models()
        changes = describe_changes(None, current, {"Demo/slider": "Slider"})
        assert ("add_circle", "Added Slider") in changes


class TestUnsavedDialog:
    async def test_lists_the_changes(self, app):
        """Regression: the dialog said only "You have unsaved changes"."""
        model = app.store.create_dashboard(app._user_id, "Plant")
        pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        app._editor.add_component("Analytics/selector")
        app._request_navigation("/")
        assert app._unsaved_dialog.open
        assert app._unsaved_view.changes == [("add_circle", "Added selector")]
        assert "Added selector" in app._unsaved_view._changes.object

    async def test_unchanged_settings_are_not_reported(self, app):
        model = app.store.create_dashboard(app._user_id, "Plant")
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        assert app._unsaved_changes() == []

    async def test_buttons(self, app):
        view = app._unsaved_view
        assert [view.leave_button.label, view.stay_button.label, view.save_button.label] == [
            "Leave without saving",
            "Keep editing",
            "Save and leave",
        ]
        assert view.leave_button.color == "error"
        assert app._unsaved_dialog.title == "Save changes before leaving?"

    async def test_long_lists_are_capped(self, app):
        changes = [("add_circle", f"Added {i}") for i in range(CHANGES_LIMIT + 3)]
        app._unsaved_view.show(changes)
        html = app._unsaved_view._changes.object
        assert f"Added {CHANGES_LIMIT - 1}" in html
        assert f"Added {CHANGES_LIMIT}<" not in html
        assert "and 3 more changes" in html

    async def test_keep_editing_stays(self, app):
        model = app.store.create_dashboard(app._user_id, "Plant")
        app._load_dashboard_sync(model.dashboard_id, edit=True)
        app._editor.add_component("Analytics/selector")
        app._request_navigation("/")
        app._unsaved_view.stay_button.clicks += 1
        assert not app._unsaved_dialog.open
        assert app._pending_navigation is None
        assert app._dirty


class TestSaveFeedback:
    @pytest.fixture
    def editor(self):
        editor = FlowDash(COMPONENTS, notifications=False, store=MemoryDashboardStore())
        editor.new_dashboard("Plant")
        editor.add_component(SELECTOR)
        return editor

    def test_bar_shows_the_save_instead_of_a_toast(self, editor, monkeypatch):
        """Regression: every save raised a toast over the assistant's input."""
        notices = []
        monkeypatch.setattr(editor, "_notify", lambda *args, **kwargs: notices.append(args))
        assert editor._on_save_clicked()
        assert notices == []
        assert editor._dashboard_bar.save_state.object.startswith("Saved ")

    def test_toast_without_the_bar(self, editor, monkeypatch):
        editor.toolbar = False
        notices = []
        monkeypatch.setattr(editor, "_notify", lambda *args, **kwargs: notices.append(args))
        assert editor._on_save_clicked()
        assert notices == [("success", 'Dashboard "Plant" saved.')]


class TestStartCard:
    @pytest.fixture
    def editor(self, tmp_path):
        _create_project(tmp_path)
        sys.path.insert(0, str(tmp_path))
        try:
            yield FlowDash(build_registry(tmp_path), notifications=False)
        finally:
            sys.path.remove(str(tmp_path))

    def test_offers_the_first_data_source(self, editor):
        assert first_data_source(editor) == "Analytics/selector"
        card = editor._start_card
        assert card.add_button.visible
        assert card.add_button.label == "Add selector"
        assert "selector" in card._steps.object

    def test_add_places_the_source(self, editor):
        editor._start_card.add_button.clicks += 1
        assert [item["component_id"] for item in editor._tile_items] == ["Analytics/selector"]
        assert editor._flow.bottom_panel == []

    def test_is_the_empty_canvas_hint(self, editor):
        assert editor._flow.bottom_panel == [editor._empty_hint]
        assert editor._empty_hint is editor._start_card.__panel__()

    def test_read_only_has_no_actions(self, editor):
        editor.new_dashboard("Plant")
        editor.read_only = True
        card = editor._start_card
        assert not card._actions.visible
        assert card._title.object == "This dashboard is empty"

    def test_scratch_canvas_without_a_dashboard_keeps_the_steps(self, editor):
        """Regression: the components route (read-only, no dashboard) said "empty"."""
        editor.read_only = True
        card = editor._start_card
        assert card._actions.visible
        assert card._title.object == "Start with a data source"

    def test_tags_mark_a_source(self, editor):
        entry = editor._component_entries["Analytics/chart"]
        entry.metadata.tags.append("source")
        try:
            assert first_data_source(editor) == "Analytics/chart"
        finally:
            entry.metadata.tags.remove("source")

    def test_no_source_offers_no_add(self, editor, monkeypatch):
        entries = {"Analytics/chart": editor._component_entries["Analytics/chart"]}
        monkeypatch.setattr(editor, "_component_entries", entries)
        editor._start_card.refresh()
        assert first_data_source(editor) is None
        assert not editor._start_card.add_button.visible
        assert "a data source" in editor._start_card._steps.object
