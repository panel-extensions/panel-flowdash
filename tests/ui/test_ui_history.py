"""UI tests of the edit history in the assistant's drawer: reverting to an earlier version."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _opened_row(page):
    """The history row of the dashboard as opened, not the tooltip showing its detail."""
    return page.locator(".MuiListItemText-root", has_text="Opened the dashboard")


def test_reverting_from_the_drawer_restores_the_canvas(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Add another note")
    chat.press("Enter")
    editor = apps[0]._editor
    wait_until(lambda: len(editor._tile_items) == 2, page, timeout=20000)
    wait_until(lambda: not editor.edit_history.busy, page, timeout=20000)
    wait_until(lambda: len(editor.edit_history.versions) == 2, page, timeout=20000)

    page.get_by_role("button", name="Artifacts").click()
    # One item for the request, with the dashboard as opened folded below it.
    expect(page.get_by_text("Add another note")).to_be_visible()
    page.get_by_text(re.compile(r" edited$")).click()
    _opened_row(page).click()
    revert = page.get_by_role("button", name="Revert to v1")
    revert.click()
    wait_until(lambda: len(editor._tile_items) == 1, page, timeout=20000)
    expect(page.get_by_text(re.compile(r"^v1\b.*\bapplied\b"))).to_be_visible(timeout=10000)
    # The button stops spinning: v1 is applied, so it is gone, and nothing else is loading.
    expect(revert).to_have_count(0)
    expect(page.locator(".MuiButton-loading")).to_have_count(0)
    assert editor.edit_history.artifact.applied == 1
    assert errors == []


def test_the_history_is_there_after_a_reload_and_reverts(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=agent)

    page.get_by_role("button", name="Done").wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Add another note")
    chat.press("Enter")
    editor = apps[0]._editor
    wait_until(lambda: len(editor._tile_items) == 2, page, timeout=20000)
    wait_until(lambda: not editor.edit_history.busy, page, timeout=20000)
    page.get_by_role("button", name="Save", exact=True).click()
    wait_until(lambda: not editor.dirty, page, timeout=10000)

    page.reload()
    page.get_by_role("button", name="Done").wait_for()
    wait_until(lambda: len(apps) == 2, page, timeout=10000)
    editor = apps[1]._editor
    assert len(editor._tile_items) == 2
    page.get_by_role("button", name="Assistant", exact=True).click()
    page.get_by_role("button", name="Artifacts").click()
    expect(page.locator(".MuiListItemText-primary", has_text="Add another note")).to_be_visible(
        timeout=10000
    )
    page.get_by_text(re.compile(r" edited$")).click()
    _opened_row(page).click()
    revert = page.get_by_role("button", name="Revert to v1")
    revert.click()
    wait_until(lambda: len(editor._tile_items) == 1, page, timeout=20000)
    expect(page.get_by_text(re.compile(r"^v1\b.*\bapplied\b"))).to_be_visible(timeout=10000)
    expect(page.locator(".MuiButton-loading")).to_have_count(0)
    assert errors == []


def test_a_history_of_a_newer_format_is_named_in_the_drawer(page):
    store, (dash,) = _store_with("Plant")
    store.append_history(dash, {"flowdash_history": 99})
    agent = Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    _serve_app(page, store, f"/dash/{dash}?edit=true", agent=agent)
    page.get_by_role("button", name="Done").wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    expect(page.get_by_text("History was written by a newer FlowDash")).to_be_visible(
        timeout=10000
    )


def test_a_refused_revert_says_why_and_stops_spinning(page, monkeypatch):
    from panel_flowdash.history import EditHistory

    def refuse(self, artifact, version):
        raise PermissionError("That would bring back 'secret', which you may not use here.")

    monkeypatch.setattr(EditHistory, "_on_apply", refuse)
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)
    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Add another note")
    chat.press("Enter")
    editor = apps[0]._editor
    wait_until(lambda: len(editor.edit_history.versions) == 2, page, timeout=20000)

    page.get_by_role("button", name="Artifacts").click()
    page.get_by_text(re.compile(r" edited$")).click()
    _opened_row(page).click()
    revert = page.get_by_role("button", name="Revert to v1")
    revert.click()
    expect(page.get_by_text("which you may not use here").first).to_be_visible(timeout=10000)
    expect(revert).to_be_enabled()
    expect(page.locator(".MuiButton-loading")).to_have_count(0)
    assert len(editor._tile_items) == 2 and editor.edit_history.artifact.applied == 2


@pytest.fixture
def wind_app(page, tmp_path, monkeypatch):
    import importlib.util
    import sys

    from panel_flowdash import MemoryDashboardStore
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.auth import resolve_identity
    from panel_flowdash.registry import build_registry

    from .test_ui_example_wind_energy import EXAMPLE, EXAMPLE_LOAD_TIMEOUT
    from .util import serve_component

    if not (EXAMPLE / "_data" / "turbines.csv.gz").exists():
        pytest.skip("Example data not available.")
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    project.store.close()
    store = MemoryDashboardStore()
    project.seed_dashboards(store, resolve_identity().user)
    registry = build_registry(EXAMPLE)
    apps = []

    def app():
        agent = Agent(
            scripted(("set_settings", {"settings": {"description": "Edited by the agent"}})),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset()],
        )
        apps.append(
            FlowDashApp(
                registry=registry,
                store=store,
                home_dashboard="wind-overview",
                notifications=False,
                propagate_failures=project.propagate_failures,
                settings_class=project.settings_class,
                editor_options=project.editor_options,
                page_options=project.page_options,
                agent=agent,
            )
        )
        return apps[-1]

    serve_component(page, {"/": app, "/dash/[^/]+": app}, suffix="/dash/wind-overview?edit=true")
    page.get_by_role("button", name="Done").wait_for(timeout=EXAMPLE_LOAD_TIMEOUT)
    yield apps
    for name in list(sys.modules):
        if name.split(".")[0] in {"Sources", "Controls", "Transforms", "Views", "Explore"}:
            del sys.modules[name]


@pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index")
def test_reverting_a_large_dashboard_ends_quickly(page, wind_app, monkeypatch):
    import time

    from panel_flowdash.editor import FlowDash

    full_loads = []
    load = FlowDash._hydrate_model

    def counted(self, *args, **kwargs):
        if self._history_quiet:
            full_loads.append(args)
        return load(self, *args, **kwargs)

    monkeypatch.setattr(FlowDash, "_hydrate_model", counted)

    page.keyboard.press("Control+k")
    search = page.get_by_role("combobox", name=re.compile("^Search dashboards"))
    search.press_sequentially("markdown")
    search.press("Enter")
    editor = wind_app[0]._editor
    wait_until(lambda: any(i["component_id"] == "Text/Markdown" for i in editor._tile_items), page)
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill("Describe it")
    chat.press("Enter")
    wait_until(lambda: editor.settings.description == "Edited by the agent", page, timeout=20000)
    wait_until(lambda: not editor.edit_history.busy, page, timeout=20000)

    # The browser renders this dashboard slowly on busy runners, so the steps get its load timeout.
    from .test_ui_example_wind_energy import EXAMPLE_LOAD_TIMEOUT

    page.get_by_role("button", name="Artifacts").click(timeout=EXAMPLE_LOAD_TIMEOUT)
    page.get_by_text(re.compile(r" edited$")).click(timeout=EXAMPLE_LOAD_TIMEOUT)
    _opened_row(page).click(timeout=EXAMPLE_LOAD_TIMEOUT)
    start = time.monotonic()
    page.get_by_role("button", name="Revert to v1").click(timeout=EXAMPLE_LOAD_TIMEOUT)
    wait_until(lambda: editor.edit_history.artifact.applied == 1, page, timeout=10000)
    restored = time.monotonic() - start
    expect(page.get_by_text(re.compile(r"^v1\b.*\bapplied\b"))).to_be_visible(
        timeout=EXAMPLE_LOAD_TIMEOUT
    )
    expect(page.locator(".MuiButton-loading")).to_have_count(0, timeout=EXAMPLE_LOAD_TIMEOUT)
    # Only what changed is patched: the dashboard is not loaded again.
    assert full_loads == []
    assert restored < 5
    assert not any(i["component_id"] == "Text/Markdown" for i in editor._tile_items)
    assert editor.settings.description == ""
