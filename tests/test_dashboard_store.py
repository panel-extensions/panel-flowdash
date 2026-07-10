"""Tests for DashboardStore persistence, especially tile_layout round-tripping."""

import pytest

from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
)


@pytest.fixture
def store(tmp_path):
    return DashboardStore(tmp_path / "test.db")


@pytest.fixture
def sample_dashboard():
    return DashboardModel(
        dashboard_id="d1",
        user_id="user1",
        title="Test Dashboard",
        items=[
            DashboardItem(instance_id="n1", component_id="Comp/selector", x=100, y=200),
            DashboardItem(instance_id="n2", component_id="Comp/chart", x=400, y=200),
        ],
        edges=[
            DashboardEdge(source="n1", source_port="company", target="n2", target_port="company"),
        ],
        tile_layout=[
            {"i": "n1", "x": 0, "y": 0, "w": 4, "h": 3},
            {"i": "n2", "x": 4, "y": 0, "w": 8, "h": 3},
        ],
    )


class TestDashboardStoreBasic:
    async def test_create_and_load(self, store):
        dashboard = store.create_dashboard("user1", "My Dashboard")
        loaded = store.load_dashboard("user1", dashboard.dashboard_id)
        assert loaded is not None
        assert loaded.title == "My Dashboard"
        assert loaded.user_id == "user1"

    async def test_list_dashboards(self, store):
        store.create_dashboard("user1", "First")
        store.create_dashboard("user1", "Second")
        store.create_dashboard("user2", "Other User")

        user1_dashboards = store.list_dashboards("user1")
        assert len(user1_dashboards) == 2
        assert all(d.user_id == "user1" for d in user1_dashboards)

    async def test_delete_dashboard(self, store):
        dashboard = store.create_dashboard("user1", "To Delete")
        assert store.delete_dashboard("user1", dashboard.dashboard_id)
        assert store.load_dashboard("user1", dashboard.dashboard_id) is None

    async def test_rename_dashboard(self, store):
        dashboard = store.create_dashboard("user1", "Original")
        store.rename_dashboard("user1", dashboard.dashboard_id, "Renamed")
        loaded = store.load_dashboard("user1", dashboard.dashboard_id)
        assert loaded.title == "Renamed"

    async def test_load_nonexistent_returns_none(self, store):
        assert store.load_dashboard("user1", "does_not_exist") is None


class TestTileLayoutPersistence:
    async def test_tile_layout_round_trip(self, store, sample_dashboard):
        store.save_dashboard(sample_dashboard)
        loaded = store.load_dashboard("user1", "d1")
        assert loaded.tile_layout == sample_dashboard.tile_layout

    async def test_empty_tile_layout_preserved(self, store):
        dashboard = store.create_dashboard("user1", "Empty Layout")
        loaded = store.load_dashboard("user1", dashboard.dashboard_id)
        assert loaded.tile_layout == []

    async def test_tile_layout_update(self, store, sample_dashboard):
        store.save_dashboard(sample_dashboard)

        sample_dashboard.tile_layout = [
            {"i": "n1", "x": 0, "y": 0, "w": 6, "h": 4},
            {"i": "n2", "x": 6, "y": 0, "w": 6, "h": 4},
        ]
        store.save_dashboard(sample_dashboard)

        loaded = store.load_dashboard("user1", "d1")
        assert loaded.tile_layout == sample_dashboard.tile_layout

    async def test_tile_layout_not_clobbered_on_resave_without_change(
        self, store, sample_dashboard
    ):
        """Saving again with the same layout does not lose it."""
        store.save_dashboard(sample_dashboard)
        original_layout = list(sample_dashboard.tile_layout)

        loaded = store.load_dashboard("user1", "d1")
        store.save_dashboard(loaded)

        reloaded = store.load_dashboard("user1", "d1")
        assert reloaded.tile_layout == original_layout


class TestEdgePersistence:
    async def test_edges_round_trip(self, store, sample_dashboard):
        store.save_dashboard(sample_dashboard)
        loaded = store.load_dashboard("user1", "d1")
        assert len(loaded.edges) == 1
        edge = loaded.edges[0]
        assert edge.source == "n1"
        assert edge.source_port == "company"
        assert edge.target == "n2"
        assert edge.target_port == "company"

    async def test_multiple_edges(self, store):
        dashboard = DashboardModel(
            dashboard_id="d2",
            user_id="user1",
            title="Multi-edge",
            items=[
                DashboardItem(instance_id="a", component_id="X/a"),
                DashboardItem(instance_id="b", component_id="X/b"),
                DashboardItem(instance_id="c", component_id="X/c"),
            ],
            edges=[
                DashboardEdge(source="a", source_port="out1", target="b", target_port="in1"),
                DashboardEdge(source="a", source_port="out2", target="c", target_port="in1"),
                DashboardEdge(source="b", source_port="out1", target="c", target_port="in2"),
            ],
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d2")
        assert len(loaded.edges) == 3

    async def test_no_edges(self, store):
        dashboard = DashboardModel(
            dashboard_id="d3",
            user_id="user1",
            title="No edges",
            items=[DashboardItem(instance_id="x", component_id="X/x")],
            edges=[],
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d3")
        assert loaded.edges == []


class TestTitleUniqueness:
    async def test_title_exists_returns_true_for_duplicate(self, store):
        store.create_dashboard("user1", "My Dashboard")
        assert store.title_exists("user1", "My Dashboard") is True

    async def test_title_exists_returns_false_for_unique(self, store):
        store.create_dashboard("user1", "My Dashboard")
        assert store.title_exists("user1", "Other Name") is False

    async def test_title_exists_scoped_to_user(self, store):
        store.create_dashboard("user1", "Shared Name")
        assert store.title_exists("user2", "Shared Name") is False

    async def test_title_exists_exclude_id(self, store):
        dashboard = store.create_dashboard("user1", "Original")
        assert store.title_exists("user1", "Original", exclude_id=dashboard.dashboard_id) is False

    async def test_title_exists_exclude_id_still_catches_other(self, store):
        d1 = store.create_dashboard("user1", "First")
        store.create_dashboard("user1", "Second")
        assert store.title_exists("user1", "Second", exclude_id=d1.dashboard_id) is True


class TestResponsiveLayoutPersistence:
    async def test_breakpoints_round_trip(self, store):
        dashboard = DashboardModel(
            dashboard_id="d5",
            user_id="user1",
            title="Responsive",
            items=[DashboardItem(instance_id="n1", component_id="Comp/a")],
            breakpoints=[768, 1200],
            responsive_layouts={
                "xs": [{"width": 100, "height": 120, "visible": True}],
                "sm": [{"width": 50, "height": 150, "visible": True}],
            },
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d5")
        assert loaded.breakpoints == [768, 1200]
        assert loaded.responsive_layouts == {
            "xs": [{"width": 100, "height": 120, "visible": True}],
            "sm": [{"width": 50, "height": 150, "visible": True}],
        }

    async def test_empty_responsive_layouts_preserved(self, store):
        dashboard = store.create_dashboard("user1", "No Responsive")
        loaded = store.load_dashboard("user1", dashboard.dashboard_id)
        assert loaded.breakpoints == []
        assert loaded.responsive_layouts == {}

    async def test_responsive_layouts_update(self, store):
        dashboard = DashboardModel(
            dashboard_id="d6",
            user_id="user1",
            title="Update Responsive",
            items=[DashboardItem(instance_id="n1", component_id="Comp/a")],
            breakpoints=[768],
            responsive_layouts={
                "xs": [{"width": 100, "height": 100, "visible": True}],
            },
        )
        store.save_dashboard(dashboard)

        dashboard.breakpoints = [768, 1200]
        dashboard.responsive_layouts["sm"] = [{"width": 50, "height": 150, "visible": True}]
        store.save_dashboard(dashboard)

        loaded = store.load_dashboard("user1", "d6")
        assert loaded.breakpoints == [768, 1200]
        assert "sm" in loaded.responsive_layouts

    async def test_responsive_layouts_not_clobbered_on_resave(self, store):
        dashboard = DashboardModel(
            dashboard_id="d7",
            user_id="user1",
            title="No Clobber",
            items=[],
            breakpoints=[768],
            responsive_layouts={"xs": [{"width": 100, "height": 200, "visible": True}]},
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d7")
        store.save_dashboard(loaded)
        reloaded = store.load_dashboard("user1", "d7")
        assert reloaded.breakpoints == [768]
        assert reloaded.responsive_layouts == {
            "xs": [{"width": 100, "height": 200, "visible": True}]
        }


class TestItemPersistence:
    async def test_items_round_trip(self, store, sample_dashboard):
        store.save_dashboard(sample_dashboard)
        loaded = store.load_dashboard("user1", "d1")
        assert len(loaded.items) == 2
        item = loaded.items[0]
        assert item.instance_id == "n1"
        assert item.component_id == "Comp/selector"
        assert item.x == 100
        assert item.y == 200

    async def test_item_positions_preserved(self, store, sample_dashboard):
        store.save_dashboard(sample_dashboard)
        loaded = store.load_dashboard("user1", "d1")
        for original, loaded_item in zip(sample_dashboard.items, loaded.items, strict=True):
            assert original.x == loaded_item.x
            assert original.y == loaded_item.y

    async def test_item_config_preserved(self, store):
        dashboard = DashboardModel(
            dashboard_id="d4",
            user_id="user1",
            title="With config",
            items=[
                DashboardItem(
                    instance_id="n1",
                    component_id="X/comp",
                    config={"color": "blue", "threshold": 42},
                )
            ],
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d4")
        assert loaded.items[0].config == {"color": "blue", "threshold": 42}
