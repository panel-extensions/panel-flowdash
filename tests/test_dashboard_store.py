"""Tests for dashboard persistence, especially tile_layout round-tripping.

Every test here runs against both store backends, so the in-memory store is held
to exactly the same contract as the SQLite one.
"""

import pytest

from panel_flowdash.auth import Identity, Permission
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    MemoryDashboardStore,
)


def _identity(user, *, groups=()):
    return Identity(user=user, oauth_user=user, groups=frozenset(groups))


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return DashboardStore(tmp_path / "test.db")
    return MemoryDashboardStore()


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


class TestFindByIdOrTitle:
    async def test_resolves_by_id(self, store):
        dashboard = store.create_dashboard("user1", "By Id")
        found = store.find_by_id_or_title(dashboard.dashboard_id)
        assert found is not None
        assert found.dashboard_id == dashboard.dashboard_id

    async def test_resolves_by_title(self, store):
        dashboard = store.create_dashboard("user1", "By Title")
        found = store.find_by_id_or_title("By Title")
        assert found is not None
        assert found.dashboard_id == dashboard.dashboard_id

    async def test_missing_returns_none(self, store):
        assert store.find_by_id_or_title("nope") is None

    async def test_id_takes_precedence_over_title(self, store):
        target = store.create_dashboard("user1", "First")
        # Give another dashboard a title equal to the target's id.
        other = store.create_dashboard("user2", target.dashboard_id)
        found = store.find_by_id_or_title(target.dashboard_id)
        assert found.dashboard_id == target.dashboard_id
        assert found.dashboard_id != other.dashboard_id


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


class TestPermissionPersistence:
    async def test_permission_round_trip(self, store):
        dashboard = store.create_dashboard("alice", "Shared")
        dashboard.permission = Permission.from_spec(allow_groups=["finance"])
        store.save_dashboard(dashboard)
        loaded = store.load_dashboard("alice", dashboard.dashboard_id)
        assert loaded.permission.allow_groups == frozenset({"finance"})

    async def test_default_permission_empty(self, store):
        dashboard = store.create_dashboard("alice", "Private")
        loaded = store.load_dashboard("alice", dashboard.dashboard_id)
        assert loaded.permission.is_empty

    async def test_set_permission(self, store):
        dashboard = store.create_dashboard("alice", "D")
        assert store.set_permission(
            dashboard.dashboard_id, Permission.from_spec(allow_users=["bob"])
        )
        loaded = store._load_any(dashboard.dashboard_id)
        assert loaded.permission.allow_users == frozenset({"bob"})


class TestAccessControl:
    async def test_owner_sees_own_and_shared(self, store):
        d1 = store.create_dashboard("alice", "Alice One")
        d2 = store.create_dashboard("bob", "Bob Shared")
        store.set_permission(d2.dashboard_id, Permission.from_spec(allow_users=["alice"]))
        store.create_dashboard("bob", "Bob Private")

        accessible = store.list_accessible(_identity("alice"), default_allow=False)
        ids = {d.dashboard_id for d in accessible}
        assert d1.dashboard_id in ids
        assert d2.dashboard_id in ids
        assert len(accessible) == 2

    async def test_owned_sort_first(self, store):
        shared = store.create_dashboard("bob", "Shared")
        store.set_permission(shared.dashboard_id, Permission.from_spec(allow_groups=["eng"]))
        owned = store.create_dashboard("alice", "Owned")

        accessible = store.list_accessible(_identity("alice", groups=["eng"]))
        assert accessible[0].dashboard_id == owned.dashboard_id

    async def test_group_grant(self, store):
        d = store.create_dashboard("bob", "Finance Dash")
        store.set_permission(d.dashboard_id, Permission.from_spec(allow_groups=["finance"]))

        assert store.load_for_access(_identity("alice", groups=["finance"]), d.dashboard_id)
        assert store.load_for_access(_identity("alice", groups=["eng"]), d.dashboard_id) is None

    async def test_deny_wins(self, store):
        d = store.create_dashboard("bob", "Dash")
        store.set_permission(
            d.dashboard_id,
            Permission.from_spec(allow_groups=["finance"], deny_users=["alice"]),
        )
        assert (
            store.load_for_access(_identity("alice", groups=["finance"]), d.dashboard_id) is None
        )

    async def test_owner_always_accesses(self, store):
        d = store.create_dashboard("alice", "Dash")
        store.set_permission(d.dashboard_id, Permission.from_spec(allow_groups=["nobody"]))
        assert store.load_for_access(_identity("alice"), d.dashboard_id)

    async def test_default_deny_hides_unshared(self, store):
        d = store.create_dashboard("bob", "Private")
        assert (
            store.load_for_access(_identity("alice"), d.dashboard_id, default_allow=False) is None
        )

    async def test_default_allow_shows_unrestricted(self, store):
        d = store.create_dashboard("bob", "Open")
        assert store.load_for_access(_identity("alice"), d.dashboard_id, default_allow=True)

    async def test_can_administer_unrestricted_without_admin_groups(self, store):
        # With no admin groups configured administration is unrestricted.
        d = store.create_dashboard("alice", "Dash")
        assert store.can_administer(_identity("alice"), d.dashboard_id)
        assert store.can_administer(_identity("bob"), d.dashboard_id)

    async def test_can_administer_owner_with_admin_groups(self, store):
        d = store.create_dashboard("alice", "Dash")
        admins = frozenset({"admins"})
        assert store.can_administer(_identity("alice"), d.dashboard_id, admins)
        assert not store.can_administer(_identity("bob"), d.dashboard_id, admins)

    async def test_can_administer_admin_group(self, store):
        d = store.create_dashboard("alice", "Dash")
        admin = _identity("carol", groups=["admins"])
        assert store.can_administer(admin, d.dashboard_id, frozenset({"admins"}))


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
