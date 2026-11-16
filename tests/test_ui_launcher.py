"""Tests for the launcher page and the dashboard actions it offers."""

import json
import sys
from datetime import datetime, timedelta, timezone

import panel as pn
import param
import pytest

from panel_flowdash import register
from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.auth import AuthConfig, Permission
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    DashboardStoreError,
    MemoryDashboardStore,
)
from panel_flowdash.editor import export_model
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.launcher import (
    Launcher,
    _tile_boxes,
    component_icon,
    component_role,
    copy_dashboard,
    parse_dashboard_file,
    relative_time,
    thumbnail_html,
    unique_title,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class FakeLocation(param.Parameterized):
    pathname = param.String(default="/")
    search = param.String(default="")


def _create_project(tmp_path):
    section = tmp_path / "Analytics"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "source.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, title='Source')\n"
        "def app(config):\n"
        "    return 'source'\n"
    )
    (section / "chart.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(component=True, title='Chart', icon='bar_chart')\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )
    (section / "overview.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=True, title='Overview', icon='wind_power')\n"
        "def app():\n"
        "    return 'page content'\n"
    )


@pytest.fixture
def location():
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        yield pn.state._location
    finally:
        pn.state._location = previous


@pytest.fixture
async def app(tmp_path, location):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        yield FlowDashApp(
            project_dir=tmp_path, store=DashboardStore(tmp_path / "test.db"), notifications=False
        )
    finally:
        sys.path.remove(str(tmp_path))


def _dashboard(app, title, owner=None, items=(), **fields):
    model = app.store.create_dashboard(owner or app._user_id, title)
    model.items = [
        DashboardItem(instance_id=f"{cid}_{i}", component_id=cid) for i, cid in enumerate(items)
    ]
    for name, value in fields.items():
        setattr(model, name, value)
    app.store.save_dashboard(model)
    return model


def _titles(launcher):
    return [launcher._menus[did].description for did in launcher._menus]


def _card_titles(launcher):
    return [title.removeprefix("More actions for ") for title in _titles(launcher)]


def _labels(items):
    return ["---" if item is None else item["label"] for item in items]


def _click(menu, label):
    index = next(i for i, item in enumerate(menu.items) if item and item["label"] == label)
    menu._handle_msg({"type": "click", "item": index})


class TestRelativeTime:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=20), "just now"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(minutes=12), "12 minutes ago"),
            (timedelta(hours=1), "1 hour ago"),
            (timedelta(hours=5), "5 hours ago"),
            (timedelta(days=3), "3 days ago"),
        ],
    )
    def test_recent(self, delta, expected):
        assert relative_time(NOW - delta, NOW) == expected

    def test_older_than_a_week_names_the_date(self):
        when = NOW - timedelta(days=30)
        local = when.astimezone()
        assert relative_time(when, NOW) == f"on {local:%b} {local.day}"

    def test_previous_year_includes_the_year(self):
        when = NOW - timedelta(days=400)
        assert relative_time(when, NOW).endswith(f", {when.astimezone().year}")

    def test_missing_timestamp(self):
        assert relative_time(None, NOW) is None


class TestStoreTimestamps:
    def test_sqlite_store_reports_last_write(self, tmp_path):
        store = DashboardStore(tmp_path / "t.db")
        model = store.create_dashboard("alice", "A")
        loaded = store.load_dashboard("alice", model.dashboard_id)
        assert loaded.updated_at is not None
        assert loaded.updated_at.tzinfo is timezone.utc
        assert abs(datetime.now(timezone.utc) - loaded.updated_at) < timedelta(minutes=1)

    def test_json_store_reports_file_time(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path / "dashboards")
        model = store.create_dashboard("alice", "A")
        (loaded,) = store.list_dashboards("alice")
        assert loaded.dashboard_id == model.dashboard_id
        assert abs(datetime.now(timezone.utc) - loaded.updated_at) < timedelta(minutes=1)

    def test_memory_store_stamps_saves(self):
        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "A")
        assert store.load_dashboard("alice", model.dashboard_id).updated_at is not None

    def test_timestamp_is_not_content(self):
        a = DashboardModel(dashboard_id="d", user_id="u", title="T", updated_at=NOW)
        b = DashboardModel(dashboard_id="d", user_id="u", title="T")
        assert a == b
        assert "updated_at" not in a.to_dict()


class TestCopyDashboard:
    def test_copies_contents_under_new_identity(self):
        store = MemoryDashboardStore()
        source = store.create_dashboard("alice", "Sales")
        source.items = [DashboardItem(instance_id="n1", component_id="A/source")]
        source.edges = [DashboardEdge("n1", "out", "n2", "in")]
        source.tile_layout = [{"instance_id": "n1", "width": 50, "height": 300}]
        source.settings = {"refresh_period": 5}
        source.permission = Permission.from_spec(allow_users=["carol"])
        store.save_dashboard(source)

        copy = copy_dashboard(
            store, store.load_dashboard("alice", source.dashboard_id), "bob", "X"
        )

        stored = store.load_dashboard("bob", copy.dashboard_id)
        assert copy.dashboard_id != source.dashboard_id
        assert (stored.user_id, stored.title) == ("bob", "X")
        assert stored.permission == Permission()
        assert stored.items == source.items
        assert stored.edges == source.edges
        assert stored.tile_layout == source.tile_layout
        assert stored.settings == source.settings
        assert store.load_dashboard("alice", source.dashboard_id).title == "Sales"

    def test_json_store_copy(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path / "dashboards")
        source = store.create_dashboard("alice", "Sales")
        copy = copy_dashboard(store, source, "alice", "Sales copy")
        assert copy.dashboard_id == "sales-copy"
        assert store.load_dashboard("alice", "sales-copy").title == "Sales copy"

    def test_failed_save_leaves_no_empty_dashboard(self):
        class FailingStore(MemoryDashboardStore):
            def save_dashboard(self, dashboard):
                if dashboard.items:
                    raise DashboardStoreError("disk full")
                super().save_dashboard(dashboard)

        store = FailingStore()
        source = DashboardModel(
            dashboard_id="s",
            user_id="alice",
            title="S",
            items=[DashboardItem(instance_id="n1", component_id="A/source")],
        )
        with pytest.raises(DashboardStoreError):
            copy_dashboard(store, source, "alice", "S copy")
        assert store.list_dashboards("alice") == []

    def test_unique_title_numbers_taken_titles(self):
        store = MemoryDashboardStore()
        assert unique_title(store, "alice", "Sales copy") == "Sales copy"
        store.create_dashboard("alice", "Sales copy")
        store.create_dashboard("alice", "Sales copy 2")
        store.create_dashboard("bob", "Sales copy 3")
        assert unique_title(store, "alice", "Sales copy") == "Sales copy 3"


class TestParseDashboardFile:
    def test_round_trips_an_export(self):
        model = DashboardModel(
            dashboard_id="d",
            user_id="alice",
            title="Sales",
            items=[DashboardItem(instance_id="n1", component_id="A/source")],
        )
        parsed = parse_dashboard_file(json.dumps(export_model(model)), "bob")
        assert (parsed.user_id, parsed.title) == ("bob", "Sales")
        assert parsed.items == model.items
        assert parsed.revision is None

    def test_untitled_file(self):
        assert parse_dashboard_file('{"items": []}', "bob").title == "Untitled"

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("not json", "Not valid JSON"),
            ("[]", "Not a FlowDash dashboard export"),
            ('{"title": "x"}', "Not a FlowDash dashboard export"),
            ('{"items": [{"component_id": "a"}]}', "Malformed dashboard export"),
        ],
    )
    def test_rejects_invalid_files(self, content, message):
        with pytest.raises(ValueError, match=message):
            parse_dashboard_file(content, "bob")


class TestThumbnail:
    def _model(self, layout, items=("a", "b", "c"), edges=()):
        return DashboardModel(
            dashboard_id="d",
            user_id="u",
            title="T",
            items=[DashboardItem(instance_id=i, component_id=f"S/{i}") for i in items],
            edges=list(edges),
            tile_layout=layout,
        )

    def test_tiles_flow_into_rows_in_index_order(self):
        model = self._model(
            [
                {"instance_id": "a", "index": 2, "width": 100, "height": 200},
                {"instance_id": "b", "index": 0, "width": 60, "height": 300},
                {"instance_id": "c", "index": 1, "width": 40, "height": 250},
            ]
        )
        assert _tile_boxes(model) == [
            (0.0, 0.0, 60.0, 300.0, "b"),
            (60.0, 0.0, 40.0, 250.0, "c"),
            (0.0, 300.0, 100.0, 200.0, "a"),
        ]

    def test_hidden_tiles_are_skipped_and_missing_entries_use_defaults(self):
        model = self._model([{"instance_id": "a", "visible": False}])
        assert [box[4] for box in _tile_boxes(model)] == ["b", "c"]
        assert _tile_boxes(model)[0][2:4] == (50.0, 300.0)

    def test_empty_dashboard(self):
        html = thumbnail_html(self._model([], items=()), {})
        assert "No components yet" in html

    def test_icons_from_metadata_then_wiring(self):
        model = self._model(
            [],
            edges=[DashboardEdge("a", "out", "b", "in"), DashboardEdge("b", "out", "c", "in")],
        )
        html = thumbnail_html(model, {"S/a": None, "S/b": None, "S/c": "map"})
        assert [icon in html for icon in ("storage", "filter_alt", "map")] == [True] * 3
        assert "help_outline" in thumbnail_html(model, {})


def _entry(**metadata):
    return RegistryEntry.from_app(
        register(page=False, component=True, **metadata)(lambda config: None), app_id="Demo/x"
    )


class TestComponentRole:
    """The launcher labels components by the rules the palette and gallery group them by."""

    @pytest.mark.parametrize(
        ("metadata", "role", "icon"),
        [
            ({"provides": ["rows"]}, "Data source", "storage"),
            ({"provides": ["rows"], "requires": ["query"]}, "Transform", "filter_alt"),
            ({"requires": ["rows"]}, "View", "insert_chart"),
            (
                {"requires": ["query"], "provides": ["rows"], "tags": ["source"]},
                "Data source",
                "storage",
            ),
        ],
    )
    def test_declared_ports_and_tags(self, metadata, role, icon):
        entry = _entry(**metadata)
        assert component_role(entry) == role
        assert component_icon(entry) == icon

    def test_unknown_ports_get_no_label(self):
        entry = _entry()
        assert component_role(entry) is None
        assert component_icon(entry) is None
        assert component_icon(_entry(icon="map")) == "map"


class TestLauncher:
    async def test_build_launcher_reuses_one_instance(self, app):
        first = app._build_launcher()
        assert isinstance(first, Launcher)
        assert app._build_launcher() is first

    async def test_cards_sorted_by_last_edit(self, app):
        _dashboard(app, "Older")
        _dashboard(app, "Newer")
        with app.store._get_conn() as conn:
            conn.execute("UPDATE dashboards SET updated_at = datetime('now', '-1 day')")
            conn.execute(
                "UPDATE dashboards SET updated_at = datetime('now') WHERE title = 'Newer'"
            )
        assert _card_titles(app._build_launcher()) == ["Newer", "Older"]

    async def test_sort_by_name(self, app):
        for title in ("beta", "Alpha", "gamma"):
            _dashboard(app, title)
        launcher = app._build_launcher()
        launcher.sort = "name"
        assert _card_titles(launcher) == ["Alpha", "beta", "gamma"]
        assert launcher._sort_menu.label == "Name"

    async def test_sort_menu_sets_sort(self, app):
        _dashboard(app, "A")
        launcher = app._build_launcher()
        _click(launcher._sort_menu, "Name")
        assert launcher.sort == "name"

    async def test_filters(self, app):
        _dashboard(app, "Mine")
        _dashboard(app, "Theirs", owner="maya")
        launcher = app._build_launcher()
        assert sorted(_card_titles(launcher)) == ["Mine", "Theirs"]
        assert launcher._chips["all"].label == "All 2"

        launcher._chips["owned"].clicks += 1
        assert launcher.filter == "owned"
        assert _card_titles(launcher) == ["Mine"]
        assert launcher._chips["owned"].variant == "filled"
        assert launcher._chips["all"].variant == "outlined"

        launcher._chips["shared"].clicks += 1
        assert _card_titles(launcher) == ["Theirs"]

    async def test_filter_without_matches_says_so(self, app):
        _dashboard(app, "Mine")
        launcher = app._build_launcher()
        launcher.filter = "shared"
        assert launcher._menus == {}
        assert "No dashboards match" in launcher._cards[0].object

    async def test_filter_survives_refresh(self, app):
        _dashboard(app, "Mine")
        launcher = app._build_launcher()
        launcher.filter = "owned"
        assert app._build_launcher().filter == "owned"

    async def test_list_view(self, app):
        _dashboard(app, "Mine")
        launcher = app._build_launcher()
        launcher._view_toggle.value = "list"
        assert launcher.view_mode == "list"
        assert '<div class="card row">' in _card_html(launcher)

    async def test_card_text(self, app):
        _dashboard(app, "Theirs", owner="maya", items=["Analytics/chart"])
        _dashboard(app, "Empty one")
        html = _card_html(app._build_launcher())
        assert "Edited just now by you" in html
        assert "Edited just now by maya" in html
        assert ">Empty<" in html
        assert "1 component<" in html

    async def test_title_is_escaped(self, app):
        _dashboard(app, "<b>Bold</b>")
        html = _card_html(app._build_launcher())
        assert "&lt;b&gt;Bold&lt;/b&gt;" in html
        assert "<b>Bold" not in html

    @pytest.mark.parametrize(
        ("permission", "default_allow", "expected"),
        [
            (
                Permission.from_spec(allow_users=["ak"], allow_groups=["ops"]),
                True,
                "Shared with 2",
            ),
            (Permission(), True, "Everyone"),
            (Permission(), False, "Only you"),
        ],
    )
    async def test_sharing_indicator(self, app, permission, default_allow, expected):
        _dashboard(app, "Mine", permission=permission)
        app.auth_config = AuthConfig(default_allow=default_allow)
        assert expected in _card_html(app._build_launcher())

    async def test_admin_menu(self, app):
        _dashboard(app, "Mine")
        (menu,) = app._build_launcher()._menus.values()
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
        assert menu.stylesheets == [
            ".MuiMenu-list > :nth-child(8) { color: var(--mui-palette-error-main); }"
        ]

    async def test_viewer_menu(self, app):
        _dashboard(app, "Theirs", owner="maya")
        (menu,) = app._build_launcher()._menus.values()
        assert _labels(menu.items) == ["Open", "Duplicate", "Download as file"]
        assert menu.stylesheets == []

    async def test_nav_menus_have_no_dividers(self, app):
        # pmui's MenuList fails to render item actions that contain a divider.
        _dashboard(app, "Mine")
        (item,) = [i for i in app._get_dashboard_menu_items() if i["label"] == "Mine"]
        assert None not in item["actions"]
        assert [a["label"] for a in item["actions"]][-1] == "Delete"

    async def test_editor_menu(self, app):
        _dashboard(app, "Theirs", owner="maya")
        app._can_edit_dashboard = lambda dashboard: True
        (menu,) = app._build_launcher()._menus.values()
        assert _labels(menu.items) == ["Open", "Edit", "Duplicate", "Download as file"]

    async def test_card_click_opens_dashboard(self, app, location):
        model = _dashboard(app, "Mine")
        app._load_page_layout = lambda: None
        launcher = app._build_launcher()
        card = launcher._cards[0][0]
        card[0].clicks += 1
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        assert location.search == ""

    async def test_menu_open_navigates(self, app, location):
        model = _dashboard(app, "Mine")
        app._load_page_layout = lambda: None
        _click(app._build_launcher()._menus[model.dashboard_id], "Open")
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"

    async def test_menu_share_opens_share_dialog(self, app):
        model = _dashboard(app, "Mine")
        _click(app._build_launcher()._menus[model.dashboard_id], "Share")
        assert app._share_dialog.open is True
        assert app._share_context == {"dashboard_id": model.dashboard_id}

    async def test_menu_delete_opens_delete_dialog(self, app):
        model = _dashboard(app, "Mine")
        _click(app._build_launcher()._menus[model.dashboard_id], "Delete")
        assert app._dialog.open is True
        assert app._dialog_context == {"action": "delete", "dashboard_id": model.dashboard_id}

    async def test_rename_refreshes_open_launcher(self, app):
        model = _dashboard(app, "Old")
        launcher = app._build_launcher()
        assert app._rename_dashboard(model.dashboard_id, "New")
        assert _card_titles(launcher) == ["New"]

    async def test_pages_section_lists_pages_but_not_components(self, app):
        _dashboard(app, "Mine")
        launcher = app._build_launcher()
        pages = _section(launcher, "flowdash-launcher-pages")
        html = " ".join(card[0].object.object for card in pages[1])
        assert "Overview" in html
        assert "Chart" not in html

    async def test_components_section_features_most_used(self, app):
        _dashboard(app, "One", items=["Analytics/chart"])
        _dashboard(app, "Two", items=["Analytics/chart", "Analytics/source"])
        launcher = app._build_launcher()
        components = _section(launcher, "flowdash-launcher-components")
        heading, grid = components
        captions = [card[0].object.object for card in grid]
        assert "Chart" in captions[0] and "Used in 2 dashboards" in captions[0]
        assert "Source" in captions[1] and "Used in 1 dashboard<" in captions[1]
        browse = heading[-1]
        assert browse.label == f"Browse all {len(app._component_entries)}"
        assert "Select" not in " ".join(captions)

    async def test_restricted_components_are_not_shown(self, app):
        _dashboard(app, "One", items=["Analytics/chart"])
        app._can_use_component = lambda entry: entry.title != "Chart"
        launcher = app._build_launcher()
        heading, grid = _section(launcher, "flowdash-launcher-components")
        captions = " ".join(card[0].object.object for card in grid)
        assert "Chart" not in captions
        assert heading[-1].label == f"Browse all {len(app._component_entries) - 1}"

    async def test_browse_components_navigates(self, app, location):
        _dashboard(app, "One")
        app._load_page_layout = lambda: None
        heading, _ = _section(app._build_launcher(), "flowdash-launcher-components")
        heading[-1].clicks += 1
        assert location.pathname == "/components"


class TestEmptyState:
    async def test_first_run(self, app):
        launcher = app._build_launcher()
        (empty,) = [o for o in launcher._layout if "flowdash-launcher-empty" in o.css_classes]
        texts = " ".join(str(getattr(o, "object", "")) for o in empty)
        assert "Build your first dashboard" in texts
        assert "Connect components like Chart and Source" in texts
        assert "Select" not in texts
        actions = empty[-1]
        assert [o.label for o in actions] == [
            "New dashboard",
            "Import a file",
            f"Browse {len(app._component_entries)} components",
        ]
        assert _section(launcher, "flowdash-launcher-components") is None
        assert _section(launcher, "flowdash-launcher-pages") is not None

    async def test_new_dashboard_opens_create_dialog(self, app):
        launcher = app._build_launcher()
        launcher._new_button.clicks += 1
        assert app._dialog.open is True
        assert app._dialog_context == {"action": "create"}

    async def test_first_dashboard_replaces_empty_state(self, app):
        launcher = app._build_launcher()
        _dashboard(app, "First")
        app._refresh_sidebar_dashboards()
        assert _card_titles(launcher) == ["First"]
        assert not [o for o in launcher._layout if "flowdash-launcher-empty" in o.css_classes]


class TestDuplicate:
    async def test_duplicate_from_card(self, app):
        source = _dashboard(app, "Sales", items=["Analytics/chart"])
        launcher = app._build_launcher()
        _click(launcher._menus[source.dashboard_id], "Duplicate")
        (copy,) = [m for m in app.store.list_dashboards(app._user_id) if m.title == "Sales copy"]
        assert copy.items == source.items
        assert sorted(_card_titles(launcher)) == ["Sales", "Sales copy"]

    async def test_duplicate_twice_numbers_the_copy(self, app):
        source = _dashboard(app, "Sales")
        app._duplicate_dashboard(source.dashboard_id)
        app._duplicate_dashboard(source.dashboard_id)
        titles = sorted(m.title for m in app.store.list_dashboards(app._user_id))
        assert titles == ["Sales", "Sales copy", "Sales copy 2"]

    async def test_viewer_duplicates_into_own_unshared_dashboard(self, app):
        source = _dashboard(app, "Theirs", owner="maya")
        copy = app._duplicate_dashboard(source.dashboard_id)
        assert copy.user_id == app._user_id
        assert copy.permission == Permission()

    async def test_duplicate_inaccessible_dashboard_is_refused(self, app):
        source = _dashboard(app, "Private", owner="maya")
        app.store.set_permission(source.dashboard_id, Permission.from_spec(allow_users=["x"]))
        assert app._duplicate_dashboard(source.dashboard_id) is None
        assert app.store.list_dashboards(app._user_id) == []

    async def test_store_failure_is_reported(self, app):
        source = _dashboard(app, "Sales")
        messages = []
        app._notify = lambda severity, message, duration=3000: messages.append(message)

        def refuse(user_id, title):
            raise DashboardStoreError("Quota reached.")

        app.store.create_dashboard = refuse
        assert app._duplicate_dashboard(source.dashboard_id) is None
        assert messages == ["Quota reached."]

    async def test_duplicate_from_nav_drawer(self, app):
        source = _dashboard(app, "Sales")
        app._refresh_sidebar_dashboards()
        app._on_nav_menu_action(
            "Duplicate", {"label": "Sales", "path": f"{DASH_ROUTE_PREFIX}{source.dashboard_id}"}
        )
        assert any(m.title == "Sales copy" for m in app.store.list_dashboards(app._user_id))


class TestDownload:
    async def test_download_sends_the_editor_export(self, app):
        source = _dashboard(app, "Sales Q3/Q4", items=["Analytics/chart"])
        _click(app._build_launcher()._menus[source.dashboard_id], "Download as file")
        downloader = app._downloader
        assert downloader in app._page_dialogs
        assert downloader.filename == "Sales_Q3_Q4.json"
        assert downloader.data is not None
        payload = json.loads(downloader.callback().getvalue())
        stored = app.store.load_dashboard(app._user_id, source.dashboard_id)
        assert payload == export_model(stored)
        assert "dashboard_id" not in payload
        assert payload["items"][0]["component_id"] == "Analytics/chart"

    async def test_download_inaccessible_dashboard_is_refused(self, app):
        source = _dashboard(app, "Private", owner="maya")
        app.store.set_permission(source.dashboard_id, Permission.from_spec(allow_users=["x"]))
        assert app._download_dashboard(source.dashboard_id) is False
        assert app._downloader.data is None


class TestImport:
    def _export(self, title="Imported", items=("Analytics/chart",)):
        model = DashboardModel(
            dashboard_id="x",
            user_id="someone",
            title=title,
            items=[
                DashboardItem(instance_id=f"n{i}", component_id=c) for i, c in enumerate(items)
            ],
        )
        return json.dumps(export_model(model)).encode()

    async def test_import_creates_and_opens_dashboard(self, app, location):
        app._load_page_layout = lambda: None
        launcher = app._build_launcher()
        launcher._import_input.param.update(filename="sales.json", value=self._export())
        (model,) = app.store.list_dashboards(app._user_id)
        assert model.title == "Imported"
        assert [item.component_id for item in model.items] == ["Analytics/chart"]
        assert location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        assert launcher._import_input.value is None

    async def test_import_keeps_titles_unique(self, app):
        _dashboard(app, "Imported")
        app._load_page_layout = lambda: None
        app._import_dashboard_file("x.json", self._export())
        titles = sorted(m.title for m in app.store.list_dashboards(app._user_id))
        assert titles == ["Imported", "Imported 2"]

    async def test_invalid_file_is_reported(self, app):
        messages = []
        app._notify = lambda severity, message, duration=3000: messages.append(message)
        assert app._import_dashboard_file("notes.json", b"not json") is None
        assert messages and messages[0].startswith("Could not import notes.json: Not valid JSON")
        assert app.store.list_dashboards(app._user_id) == []


def _card_html(launcher):
    cards = launcher._cards[0]
    return "".join(card[0].object.object for card in cards)


def _section(launcher, css_class):
    return next((o for o in launcher._layout if css_class in o.css_classes), None)
