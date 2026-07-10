"""Tests that dashboard save preserves tile_layout regardless of editor mode."""

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


class TestSavePreservesTileLayout:
    """Regression tests: tile_layout must not be lost when saving from wiring mode."""

    async def test_save_from_wiring_mode_preserves_existing_layout(self, store):
        """When user saves in wiring mode, the previously loaded tile_layout is kept."""
        layout = [{"i": "n1", "x": 0, "y": 0, "w": 6, "h": 3}]
        dashboard = DashboardModel(
            dashboard_id="d1",
            user_id="user1",
            title="Test",
            items=[DashboardItem(instance_id="n1", component_id="Comp/a", x=100, y=50)],
            edges=[],
            tile_layout=layout,
        )
        store.save_dashboard(dashboard)

        loaded = store.load_dashboard("user1", "d1")
        assert loaded.tile_layout == layout

        loaded.items = [DashboardItem(instance_id="n1", component_id="Comp/a", x=200, y=100)]
        store.save_dashboard(loaded)

        reloaded = store.load_dashboard("user1", "d1")
        assert reloaded.tile_layout == layout
        assert reloaded.items[0].x == 200

    async def test_save_with_empty_layout_does_not_clobber(self, store):
        """If tile_layout was never set (new dashboard), empty list is fine to persist."""
        dashboard = DashboardModel(
            dashboard_id="d2",
            user_id="user1",
            title="New",
            items=[],
            edges=[],
            tile_layout=[],
        )
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("user1", "d2")
        assert loaded.tile_layout == []

    async def test_full_round_trip_with_edges_and_layout(self, store):
        """Complete round-trip: items + edges + tile_layout all survive."""
        original = DashboardModel(
            dashboard_id="d3",
            user_id="user1",
            title="Full",
            items=[
                DashboardItem(instance_id="sel", component_id="C/sel", x=0, y=0),
                DashboardItem(instance_id="chart", component_id="C/chart", x=300, y=0),
            ],
            edges=[
                DashboardEdge(
                    source="sel", source_port="company", target="chart", target_port="company"
                ),
            ],
            tile_layout=[
                {"i": "sel", "x": 0, "y": 0, "w": 4, "h": 2},
                {"i": "chart", "x": 4, "y": 0, "w": 8, "h": 4},
            ],
        )
        store.save_dashboard(original)

        loaded = store.load_dashboard("user1", "d3")
        assert len(loaded.items) == 2
        assert len(loaded.edges) == 1
        assert loaded.tile_layout == original.tile_layout
        assert loaded.edges[0].source == "sel"
        assert loaded.edges[0].target_port == "company"
