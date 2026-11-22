"""Tests for the component gallery at /components."""

import sys

import panel as pn
import param
import pytest

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import COMPONENTS_ROUTE, DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.auth import Permission
from panel_flowdash.component_library import normalize_components
from panel_flowdash.ui.gallery import (
    ComponentGallery,
    DashboardRef,
    GalleryEntry,
    GalleryPort,
    card_html,
    gallery_entry,
    ports_html,
    usage_index,
)
from panel_flowdash.ui.launcher import component_role as launcher_role
from panel_flowdash.ui.palette import palette_entry
from panel_flowdash.ui.roles import component_role
from panel_flowdash.widgets import BUILTIN_COMPONENTS
from tests.test_navigation import FakeLocation

SOURCE = "Test/source"
FILTER = "Test/filter"
CHART = "Test/chart"
LEDGER = "Test/ledger"
NOTES = "Test/notes"
SELECT = "Widgets/Select"


@register(
    page=False,
    component=True,
    title="Turbines",
    description="Loads the turbine table.",
    provides=[{"key": "table", "type": "DataFrame"}],
)
def source(config):
    return "source"


@register(
    page=False,
    component=True,
    title="State Filter",
    description="Keeps one state.",
    tags=["geo"],
    aliases=["Old/filter"],
    requires=[{"key": "table", "type": "DataFrame", "label": "The turbines"}],
    provides=[{"key": "filtered", "type": "DataFrame"}],
)
def state_filter(config):
    return "filter"


@register(
    page=True,
    component=True,
    title="Capacity <Chart>",
    requires=[{"key": "filtered", "type": "DataFrame", "required": False}],
)
def chart(config):
    return "chart"


@register(page=False, component=True, title="Ledger", allow_groups=["finance"])
def ledger(config):
    return "ledger"


@register(page=False, component=True, title="Notes")
class Notes(pn.viewable.Viewer):
    """Free text notes.

    Second paragraph, not shown.
    """

    text = param.String(default="")

    def __panel__(self):
        return pn.pane.Markdown(self.text)


REGISTRY = normalize_components(
    {SOURCE: source, FILTER: state_filter, CHART: chart, LEDGER: ledger, NOTES: Notes}
)


def _seed(store, owner, title, components, **kwargs):
    model = store.create_dashboard(owner, title)
    model.items = [
        DashboardItem(instance_id=f"n{i}", component_id=cid) for i, cid in enumerate(components)
    ]
    for key, value in kwargs.items():
        setattr(model, key, value)
    store.save_dashboard(model)
    return model


@pytest.fixture
def store():
    return MemoryDashboardStore()


@pytest.fixture
async def app(store):
    previous = pn.state._location
    pn.state._location = FakeLocation(pathname=COMPONENTS_ROUTE)
    try:
        instance = FlowDashApp(registry=REGISTRY, store=store, notifications=False)
        yield instance
    finally:
        pn.state._location = previous


def _gallery(app):
    gallery = ComponentGallery(app)
    return gallery


def _visible(gallery):
    return [e.component_id for e in gallery.visible_entries()]


class TestRoles:
    def test_ports_decide_the_role(self):
        entries = {cid: gallery_entry(REGISTRY[cid]) for cid in (SOURCE, FILTER, CHART)}
        assert {cid: e.role for cid, e in entries.items()} == {
            SOURCE: "source",
            FILTER: "transform",
            CHART: "view",
        }

    def test_undeclared_ports_are_unknown_until_imported(self):
        assert gallery_entry(REGISTRY[NOTES]).role == "other"

    @pytest.mark.parametrize(("tag", "role"), [("Data source", "source"), ("widget", "widget")])
    def test_a_role_tag_wins(self, tag, role):
        @register(page=False, component=True, tags=[tag], requires=["a"], provides=["b"])
        def tagged(config):
            return ""

        entry = normalize_components({"T/tagged": tagged})["T/tagged"]
        assert component_role(entry, ["a"], ["b"]) == role

    def test_palette_and_launcher_agree(self):
        names = {"source": "Data source", "transform": "Transform", "view": "View"}
        for entry in REGISTRY.values():
            role = gallery_entry(entry).role
            assert palette_entry(entry).role == role, entry.app_id
            assert launcher_role(entry) == names.get(role), entry.app_id

    async def test_builtin_widgets_are_widgets(self, app):
        gallery = _gallery(app)
        entry = next(e for e in gallery.entries if e.component_id == SELECT)
        assert entry.role == "widget"
        assert entry.builtin
        assert entry.usage == "Built in"


class TestEntries:
    def test_search_matches_title_description_tags_and_ports(self):
        entry = gallery_entry(REGISTRY[FILTER])
        for query in ("state", "one STATE", "geo", "filtered", "dataframe", "turbines"):
            assert entry.matches(query), query
        assert not entry.matches("ledger")

    def test_accepts_input_types_only(self):
        entry = gallery_entry(REGISTRY[FILTER])
        assert entry.accepts(None)
        assert entry.accepts("DataFrame")
        assert entry.accepts("dataframe")
        assert not gallery_entry(REGISTRY[SOURCE]).accepts("DataFrame")

    def test_ports(self):
        entry = gallery_entry(REGISTRY[FILTER])
        assert entry.inputs == (GalleryPort("table", "DataFrame", "The turbines", True),)
        assert entry.outputs == (GalleryPort("filtered", "DataFrame", None, None),)

    def test_description_falls_back_to_the_first_docstring_paragraph(self):
        assert gallery_entry(REGISTRY[NOTES]).description == "Free text notes."

    def test_module_label(self):
        assert gallery_entry(REGISTRY[FILTER]).module == REGISTRY[FILTER].module_name

    def test_usage_line(self):
        refs = (DashboardRef("a", "A"), DashboardRef("b", "B"))
        assert GalleryEntry("x", "X", "view", used_in=refs[:1]).usage == "In 1 dashboard"
        assert GalleryEntry("x", "X", "view", used_in=refs).usage == "In 2 dashboards"
        assert GalleryEntry("x", "X", "view").usage == "Not used yet"
        assert GalleryEntry("x", "X", "widget", builtin=True, used_in=refs).usage == (
            "Built in, in 2 dashboards"
        )

    def test_card_escapes_text(self):
        html = card_html(gallery_entry(REGISTRY[CHART]))
        assert "Capacity &lt;Chart&gt;" in html
        assert "<Chart>" not in html
        assert "in: filtered" in html

    def test_ports_table(self):
        html = ports_html(gallery_entry(REGISTRY[FILTER]))
        assert "The turbines" in html
        assert "Required" in html
        assert "<th>Label</th>" in html
        assert "<th>Label</th>" not in ports_html(gallery_entry(REGISTRY[CHART]))
        assert "Optional" in ports_html(gallery_entry(REGISTRY[CHART]))


class TestUsage:
    def test_counts_each_dashboard_once_and_resolves_aliases(self, store):
        a = _seed(store, "alice", "A", [FILTER, FILTER, SOURCE])
        b = _seed(store, "alice", "B", ["Old/filter"])
        usage = usage_index([a, b], REGISTRY)
        assert usage[FILTER] == [
            DashboardRef(a.dashboard_id, "A"),
            DashboardRef(b.dashboard_id, "B"),
        ]
        assert usage[SOURCE] == [DashboardRef(a.dashboard_id, "A")]
        assert DashboardRef("x", "X").path == f"{DASH_ROUTE_PREFIX}x"

    async def test_counts_only_accessible_dashboards(self, app, store):
        user = app._identity.user
        _seed(store, user, "Mine", [FILTER])
        _seed(
            store,
            "someone-else",
            "Private",
            [FILTER],
            permission=Permission.from_spec(allow_users=["someone-else"]),
        )
        gallery = _gallery(app)
        entry = gallery._entries[FILTER]
        assert [ref.title for ref in entry.used_in] == ["Mine"]

    async def test_one_listing_and_no_imports(self, app, store, monkeypatch):
        calls, loads = [], []
        original = store.list_accessible
        monkeypatch.setattr(
            store, "list_accessible", lambda *a, **k: calls.append(1) or original(*a, **k)
        )
        monkeypatch.setattr(app, "_ensure_components_loaded", lambda *a: loads.append(a))
        _gallery(app)
        assert len(calls) == 1
        assert loads == []


class TestFilters:
    async def test_restricted_components_are_hidden(self, app):
        gallery = _gallery(app)
        assert LEDGER not in gallery._entries
        assert {SOURCE, FILTER, CHART, NOTES, SELECT} <= set(gallery._entries)

    async def test_role_filter_and_counts(self, app):
        gallery = _gallery(app)
        counts = gallery.role_counts()
        roles = [
            component_role(entry, (), ())
            for entry in normalize_components(BUILTIN_COMPONENTS).values()
        ]
        views = [
            app_id
            for app_id, entry in normalize_components(BUILTIN_COMPONENTS).items()
            if component_role(entry, (), ()) == "view"
        ]
        assert counts["source"] == 1
        assert counts["transform"] == 1 + roles.count("transform")
        assert counts["view"] == 2 + len(views)
        assert counts["widget"] == roles.count("widget")
        assert counts["all"] == len(gallery.entries)
        gallery.role = "view"
        assert sorted(_visible(gallery)) == sorted([CHART, NOTES, *views])
        chip = gallery.role_chips["view"]
        assert chip.label == f"Views {2 + len(views)}"
        assert chip.variant == "filled"
        assert gallery.role_chips["all"].variant == "outlined"

    async def test_search_updates_cards_and_counts(self, app):
        gallery = _gallery(app)
        gallery.search._process_events({"value_input": "geo"})
        assert gallery.query == "geo"
        assert _visible(gallery) == [FILTER]
        assert gallery._cards[FILTER].visible
        assert not gallery._cards[SOURCE].visible
        assert gallery.role_chips["all"].label == "All 1"
        assert not gallery.role_chips["view"].visible

    async def test_no_match_message(self, app):
        gallery = _gallery(app)
        gallery.query = "nothing like this"
        assert gallery._empty.visible
        assert gallery._empty.object == "No components match."

    async def test_accepts_menu_lists_input_types(self, app):
        gallery = _gallery(app)
        labels = [item["label"] for item in gallery.accepts_menu.items]
        assert labels[0] == "Any type"
        assert "DataFrame" in labels
        gallery._on_accepts(
            next(i for i in gallery.accepts_menu.items if i["label"] == "DataFrame")
        )
        assert set(_visible(gallery)) == {
            FILTER,
            CHART,
            "Data/FilterBySelection",
            "Text/Markdown",
            "Text/Heading",
        }
        assert gallery.accepts_menu.label == "Accepts DataFrame"
        gallery._on_accepts(gallery.accepts_menu.items[0])
        assert gallery.accepts is None
        assert gallery.accepts_menu.label == "Accepts"


class TestDetail:
    async def test_hidden_until_a_card_is_selected(self, app):
        gallery = _gallery(app)
        assert not gallery.detail.visible
        gallery.selected = FILTER
        assert gallery.detail.visible
        assert "selected" in gallery._cards[FILTER].css_classes
        gallery.close_button.param.trigger("clicks")
        assert gallery.selected is None
        assert not gallery.detail.visible

    async def test_add_menu_lists_editable_dashboards_and_new(self, app, store):
        user = app._identity.user
        _seed(store, user, "Mine", [])
        _seed(store, "someone-else", "Shared view", [])
        _seed(
            store,
            "someone-else",
            "Shared edit",
            [],
            permission=Permission.from_spec(allow_edit_users=[user]),
        )
        gallery = _gallery(app)
        gallery.selected = SOURCE
        labels = [item["label"] if item else None for item in gallery.add_menu.items]
        assert labels == ["Mine", "Shared edit", None, "New dashboard"]

    async def test_open_as_page_only_for_routable_pages(self, app):
        gallery = _gallery(app)
        gallery.selected = SOURCE
        assert not gallery.page_button.visible
        gallery.selected = CHART
        assert gallery.page_button.visible
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)
        gallery.page_button.param.trigger("clicks")
        assert calls == [f"/{CHART}"]

    async def test_used_in_links(self, app, store):
        model = _seed(store, app._identity.user, "Mine", [FILTER])
        gallery = _gallery(app)
        gallery.selected = FILTER
        assert gallery.used_in.items[0]["label"] == "Mine"
        calls = []
        app._navigate_to = lambda path, search="": calls.append(path)
        gallery._on_used_in(gallery.used_in.items[0])
        assert calls == [f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"]


class TestAddToDashboard:
    async def test_places_the_component_in_an_existing_dashboard(self, app, store):
        model = _seed(store, app._identity.user, "Mine", [SOURCE])
        gallery = _gallery(app)
        instance_id = await gallery.add_to_dashboard(FILTER, model.dashboard_id)
        assert instance_id is not None
        assert app._current_dashboard.dashboard_id == model.dashboard_id
        assert app._editor.editable
        assert app._editor.dirty
        placed = {item["component_id"] for item in app._editor._tile_items}
        assert placed == {SOURCE, FILTER}
        assert pn.state.location.pathname == f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        assert pn.state.location.search == "?edit=true"
        # Not saved until the user saves.
        assert len(store._load_any(model.dashboard_id).items) == 1

    async def test_new_dashboard(self, app, store):
        _seed(store, app._identity.user, "Turbines dashboard", [])
        gallery = _gallery(app)
        instance_id = await gallery.add_to_dashboard(SOURCE)
        assert instance_id is not None
        assert app._current_dashboard.title == "Turbines dashboard 2"
        assert [item["component_id"] for item in app._editor._tile_items] == [SOURCE]

    async def test_refuses_a_view_only_dashboard(self, app, store):
        model = _seed(store, "someone-else", "Shared view", [])
        gallery = _gallery(app)
        assert await gallery.add_to_dashboard(FILTER, model.dashboard_id) is None
        assert app._editor._tile_items == []


class TestRoute:
    async def test_components_route_shows_the_gallery(self, app):
        routed = []
        app.configure_layout = lambda _app, content, route: routed.append((content, route))
        await app._load_page_layout()
        assert isinstance(routed[0][0], ComponentGallery)
        assert routed[0][1] == COMPONENTS_ROUTE
        assert app._shell.crumbs()[-1].label == "Components"


@pytest.fixture
async def project_app(tmp_path):
    """An app over a scanned project recording which component modules are imported."""
    section = tmp_path / "Gallery_lazy"
    section.mkdir()
    (section / "__init__.py").write_text("")
    for name, register_args in (
        ("declared", "provides=['company']"),
        ("undeclared", ""),
    ):
        (section / f"{name}.py").write_text(
            "import pathlib\n"
            "import param\n"
            "import panel as pn\n"
            "from panel_flowdash import register\n\n"
            f"pathlib.Path({str(tmp_path)!r}, '{name}.imported').touch()\n\n"
            f"@register(page=False, component=True, {register_args})\n"
            "class app(pn.viewable.Viewer):\n"
            "    company = param.String()\n\n"
            "    def __panel__(self):\n"
            "        return pn.pane.Str(self.company)\n"
        )
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation(pathname=COMPONENTS_ROUTE)
    try:
        yield FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore(), notifications=False)
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


async def test_route_imports_only_components_without_declared_ports(project_app, tmp_path):
    routed = []
    project_app.configure_layout = lambda _app, content, _route: routed.append(content)
    await project_app._load_page_layout()

    assert (tmp_path / "undeclared.imported").exists()
    assert not (tmp_path / "declared.imported").exists()
    gallery = routed[0]
    entry = gallery._entries["Gallery_lazy/undeclared"]
    assert entry.ports_known
    assert entry.role == "view"
    assert [p.name for p in entry.inputs] == ["company"]
