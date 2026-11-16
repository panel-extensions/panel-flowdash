"""Tests for component-level authorization in the palette, on placement and on load."""

import pytest

from panel_flowdash import register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import MemoryDashboardStore
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
    return "secret tile"


OPEN = "Auth/open"
SECRET = "Auth/secret"
COMPONENTS = {OPEN: open_source, SECRET: secret_source}

ALICE = Identity(user="alice", oauth_user="alice", groups=frozenset({"finance"}))
BOB = Identity(user="bob", oauth_user="bob")


def _finance_only(entry):
    return entry.app_id != SECRET


def _palette_ids(editor):
    return [
        item["component_id"] for section in editor._palette_items() for item in section["items"]
    ]


class TestEditor:
    def test_palette_leaves_out_refused_components(self):
        editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
        assert sorted(_palette_ids(editor)) == [OPEN, SECRET]
        editor.authorize_component = _finance_only
        assert _palette_ids(editor) == [OPEN]
        assert [i["component_id"] for s in editor._palette.items for i in s["items"]] == [OPEN]

    def test_add_component_refuses(self):
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

    def test_placing_from_the_palette_reports_a_refusal(self):
        editor = FlowDash(
            COMPONENTS,
            notifications=False,
            include_builtin_components=False,
            authorize_component=_finance_only,
        )
        assert editor._add_from_ui(SECRET) is None
        assert editor._tile_items == []
        assert not editor.dirty

    def test_failing_callback_denies(self):
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
    def test_app_applies_component_rules_to_its_editor(self):
        assert sorted(_palette_ids(_app(ALICE)._editor)) == [OPEN, SECRET]
        editor = _app(BOB)._editor
        assert _palette_ids(editor) == [OPEN]
        with pytest.raises(PermissionError):
            editor.add_component(SECRET)

    def test_components_without_rules_ignore_a_default_deny_policy(self):
        app = _app(BOB, auth_config=AuthConfig(default_allow=False))
        assert _palette_ids(app._editor) == [OPEN]

    def test_editor_options_may_not_replace_the_check(self):
        with pytest.raises(ValueError, match="authorize_component"):
            FlowDashApp(
                registry=normalize_components(COMPONENTS),
                store=MemoryDashboardStore(),
                editor_options={"authorize_component": None},
            )
