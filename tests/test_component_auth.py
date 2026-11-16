"""Tests for component-level authorization in the palette, on placement and on load."""

import pytest

from panel_flowdash import register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    MemoryDashboardStore,
)
from panel_flowdash.editor import FlowDash


@register(page=False, component=True, title="Open", provides=[{"key": "value", "type": "int"}])
def open_source(config):
    return "open tile"


@register(
    page=False,
    component=True,
    title="Secret",
    allow_groups=["finance"],
    provides=[{"key": "value", "type": "int"}],
)
def secret_source(config):
    CREATED.append("secret")
    return "secret tile"


@register(page=False, component=True, title="Sink", requires=[{"key": "value", "type": "int"}])
def sink(config):
    return "sink tile"


CREATED: list[str] = []

OPEN = "Auth/open"
SECRET = "Auth/secret"
SINK = "Auth/sink"
COMPONENTS = {OPEN: open_source, SECRET: secret_source, SINK: sink}

ALICE = Identity(user="alice", oauth_user="alice", groups=frozenset({"finance"}))
BOB = Identity(user="bob", oauth_user="bob")


def _finance_only(entry):
    return entry.app_id != SECRET


def _palette_ids(editor):
    return [
        item["component_id"] for section in editor._palette_items() for item in section["items"]
    ]


class TestEditor:
    async def test_palette_leaves_out_refused_components(self):
        editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
        assert sorted(_palette_ids(editor)) == [OPEN, SECRET, SINK]
        editor.authorize_component = _finance_only
        assert _palette_ids(editor) == [OPEN, SINK]
        palette = [i["component_id"] for s in editor._palette.items for i in s["items"]]
        assert palette == [OPEN, SINK]

    async def test_add_component_refuses(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            authorize_component=_finance_only,
        )
        with pytest.raises(PermissionError, match="not authorized to use 'Secret'"):
            editor.add_component(SECRET)
        assert editor._tile_items == []
        assert editor.add_component(OPEN)

    async def test_placing_from_the_palette_reports_a_refusal(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            authorize_component=_finance_only,
        )
        assert editor._add_from_ui(SECRET) is None
        assert editor._tile_items == []
        assert not editor.dirty

    async def test_failing_callback_denies(self):
        def broken(entry):
            raise RuntimeError("boom")

        editor = FlowDash(COMPONENTS, notifications=False, authorize_component=broken)
        assert _palette_ids(editor) == []
        with pytest.raises(PermissionError):
            editor.add_component(OPEN)


def _app(identity, **params):
    app = FlowDashApp(
        registry=normalize_components(COMPONENTS),
        store=MemoryDashboardStore(),
        notifications=False,
        include_builtin_components=False,
        **params,
    )
    app._identity = identity
    return app


class TestApp:
    async def test_app_applies_component_rules_to_its_editor(self):
        assert sorted(_palette_ids(_app(ALICE)._editor)) == [OPEN, SECRET, SINK]
        editor = _app(BOB)._editor
        assert _palette_ids(editor) == [OPEN, SINK]
        with pytest.raises(PermissionError):
            editor.add_component(SECRET)

    async def test_components_without_rules_ignore_a_default_deny_policy(self):
        app = _app(BOB, auth_config=AuthConfig(default_allow=False))
        assert _palette_ids(app._editor) == [OPEN, SINK]

    async def test_editor_options_may_not_replace_the_check(self):
        with pytest.raises(ValueError, match="authorize_component"):
            FlowDashApp(
                registry=normalize_components(COMPONENTS),
                store=MemoryDashboardStore(),
                editor_options={"authorize_component": None},
            )


def _shared_model():
    return DashboardModel(
        dashboard_id="d1",
        user_id="alice",
        title="Payroll",
        items=[
            DashboardItem(
                instance_id="s1", component_id=SECRET, x=10, y=20, config={"q": "salaries"}
            ),
            DashboardItem(instance_id="k1", component_id=SINK, x=300, y=20),
        ],
        edges=[DashboardEdge(source="s1", source_port="value", target="k1", target_port="value")],
        tile_layout=[{"i": "s1", "x": 0, "y": 0, "w": 6, "h": 4}],
    )


@pytest.fixture(autouse=True)
def _reset_created():
    CREATED.clear()


class TestOnLoad:
    def _editor(self, **params):
        return FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            authorize_component=_finance_only,
            **params,
        )

    async def test_refused_item_is_a_private_placeholder_never_created(self):
        editor = self._editor()
        editor.load_model(_shared_model())
        assert CREATED == []
        placeholder = editor._placeholders()["s1"]
        assert placeholder.reason == "denied"
        assert placeholder.private
        assert "s1" not in editor._component_instances
        assert "salaries" not in repr(editor._flow.nodes)

    async def test_refused_item_and_its_edges_round_trip(self):
        original = _shared_model()
        editor = self._editor()
        editor.load_model(_shared_model())
        model = editor.to_model()
        (secret,) = [item for item in model.items if item.instance_id == "s1"]
        assert secret.to_dict() == original.items[0].to_dict()
        assert [edge.to_dict() for edge in model.edges] == [original.edges[0].to_dict()]

    async def test_unauthorized_editor_saving_keeps_the_item(self):
        store = MemoryDashboardStore()
        store.save_dashboard(_shared_model())
        editor = self._editor(store=store, user="bob")
        editor.load("d1")
        editor.add_component(OPEN)
        editor.save()
        saved = store._load_any("d1")
        assert [item.instance_id for item in saved.items][:2] == ["s1", "k1"]
        assert saved.items[0].config == {"q": "salaries"}
        assert saved.edges == _shared_model().edges

    async def test_placeholder_cannot_be_inspected(self):
        editor = self._editor(inspect_outputs=True)
        editor.load_model(_shared_model())
        editor.editable = False
        assert not editor._inspect_tiles["s1"].inspect_button.visible

    async def test_authorized_user_gets_the_component(self):
        editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
        editor.load_model(_shared_model())
        assert CREATED == ["secret"]
        assert editor._placeholders() == {}

    async def test_app_shows_refused_items_as_placeholders(self):
        app = _app(BOB)
        app.store.save_dashboard(_shared_model())
        await app._load_dashboard("d1")
        assert CREATED == []
        assert app._editor._placeholders()["s1"].reason == "denied"

        app = _app(ALICE)
        app.store.save_dashboard(_shared_model())
        await app._load_dashboard("d1")
        assert CREATED == ["secret"]
