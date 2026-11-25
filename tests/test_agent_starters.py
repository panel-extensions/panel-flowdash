"""A new dashboard opens with Ask on: the assistant's drawer, with questions to start from."""

import asyncio
import logging

import param
import pytest

pytest.importorskip("pydantic_ai")

from panel.viewable import Viewer
from pydantic_ai import Agent

from panel_flowdash import Identity, MemoryDashboardStore, register
from panel_flowdash.agent import (
    FORMAT_VERSION,
    StarterContext,
    default_starter_questions,
    starters as starters_module,
)
from panel_flowdash.agent.starters import STARTER_LIMIT, data_tables, starter_questions
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.editor import FlowDash

from .test_agent_tools import _app, scripted


@register(
    page=False,
    component=True,
    title="Sales",
    provides=[{"key": "rows", "type": "DataFrame"}],
)
def sales(config):
    return "sales"


def warehouse_tables(context):
    return [{"title": title, "config": {"table": title.lower()}} for title in ("Orders", "Stock")]


@register(
    page=False,
    component=True,
    title="Warehouse",
    config=["table"],
    palette_presets=warehouse_tables,
)
class Warehouse(Viewer):
    table = param.String(default="")

    @param.output(param.DataFrame)
    def rows(self):
        return None

    def __panel__(self):
        return self.table


@register(
    page=False,
    component=True,
    title="Chart",
    requires=[{"key": "rows", "type": "DataFrame"}],
)
def chart(config):
    return "chart"


@register(
    page=False,
    component=True,
    title="Filter",
    provides=[{"key": "out", "type": "DataFrame"}],
    requires=[{"key": "rows", "type": "DataFrame"}],
)
def filter_rows(config):
    return "filter"


COMPONENTS = {
    "S/sales": sales,
    "S/warehouse": Warehouse,
    "S/chart": chart,
    "S/filter": filter_rows,
}

DEFAULT = ["What's in Sales?", "Add a table of Orders", "Chart the key numbers in Stock"]


def _editor(components=COMPONENTS, **params):
    return FlowDash(components, notifications=False, include_builtin_components=False, **params)


def test_the_tables_are_the_data_sources_and_their_presets():
    assert data_tables(_editor()) == ["Sales", "Orders", "Stock"]


def test_tables_the_person_may_not_place_are_left_out():
    editor = _editor(authorize_component=lambda entry: entry.app_id != "S/sales")
    assert data_tables(editor) == ["Orders", "Stock"]


def test_tables_refused_by_the_projects_placement_rules_are_left_out():
    editor = _editor(
        refuse_placement=lambda component_id: (
            "turned off by an admin" if component_id == "S/warehouse" else None
        )
    )
    assert data_tables(editor) == ["Sales"]


def _context(tables=("Sales", "Orders", "Stock"), origin="blank"):
    return StarterContext(
        identity=Identity(user="alice"),
        dashboard_id="d",
        title="Fresh",
        origin=origin,
        template=None if origin == "blank" else "kpi",
        tables=tuple(tables),
    )


def test_the_default_questions_take_the_tables_in_turn():
    assert default_starter_questions(_context()) == DEFAULT


def test_one_table_is_asked_about_in_each_way():
    assert default_starter_questions(_context(["Sales"])) == [
        "What's in Sales?",
        "Add a table of Sales",
        "Chart the key numbers in Sales",
    ]


def test_a_dashboard_from_a_template_is_not_offered_to_add_a_table():
    assert default_starter_questions(_context(["Sales"], origin="template")) == [
        "What's in Sales?",
        "Chart the key numbers in Sales",
    ]


def test_without_data_sources_there_are_no_questions():
    assert default_starter_questions(_context([])) == []


def _broken():
    yield "Fine?"
    raise RuntimeError("catalogue offline")


@pytest.mark.parametrize(
    ("found", "questions"),
    [
        (None, DEFAULT),
        ([], []),
        (["One?", " ", "Two?", 3, None, "Three?", "Four?"], ["One?", "Two?", "Three?"]),
        (("A?", "B?"), ["A?", "B?"]),
        ("What's in Sales?", []),
        (b"What?", []),
        (42, []),
        (_broken(), []),
        (iter(lambda: "Again?", None), ["Again?", "Again?", "Again?"]),
    ],
    ids=[
        "none",
        "empty",
        "mixed",
        "tuple",
        "string",
        "bytes",
        "number",
        "raising",
        "endless",
    ],
)
def test_a_hooks_result_offers_valid_questions_only(found, questions, caplog):
    with caplog.at_level(logging.ERROR, logger="panel_flowdash.agent.starters"):
        assert starter_questions(found, _context()) == questions


@pytest.fixture
def store():
    return MemoryDashboardStore()


def _agent():
    return Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


def _session_app(store, monkeypatch, **params):
    return _app(store, monkeypatch, components=COMPONENTS, agent=_agent(), **params)


async def test_a_new_dashboard_opens_in_edit_mode_with_ask_and_the_starters(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    assert app._create_new_dashboard("Fresh")
    editor = app._editor
    assert editor.editable and editor.ask and session.drawer.open
    assert session.starters.questions == DEFAULT
    assert session.starters.view.visible
    assert [b.label for b in session.starters._list.objects] == DEFAULT


async def test_ask_already_on_keeps_the_drawer_open_with_the_starters(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    app._editor.ask = True
    assert app._create_new_dashboard("Fresh")
    assert app._agent_session.drawer.open
    assert app._agent_session.starters.questions == DEFAULT


@pytest.mark.parametrize("ask", [False, True])
async def test_view_mode_keeps_its_own_ask_after_a_new_dashboard(store, monkeypatch, ask):
    model = store.create_dashboard("alice", "Plant")
    app = _session_app(store, monkeypatch)
    app._load_dashboard_sync(model.dashboard_id)
    app._editor.ask = ask
    assert app._create_new_dashboard("Fresh")
    assert app._editor.ask and app._agent_session.drawer.open
    app._show_view_mode()
    assert app._editor.ask is ask and not app._agent_session.drawer.open
    assert not app._agent_session.starters.shown


async def test_a_new_dashboard_from_the_explore_view_opens_the_assistant(store, monkeypatch):
    model = store.create_dashboard("alice", "Plant")
    app = _session_app(store, monkeypatch)
    app._load_dashboard_sync(model.dashboard_id)
    app._editor.surface = "explore"
    assert app._create_new_dashboard("Fresh")
    assert app._editor.surface == "workspace" and not app._editor.explore
    assert app._agent_session.drawer.open and app._agent_session.starters.view.visible


async def test_a_new_dashboard_opens_the_drawer_on_its_chat(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    session.drawer.tab = "artifacts"
    assert app._create_new_dashboard("Fresh")
    assert session.drawer.tab == "chat" and session.starters.view.visible


async def test_without_the_assistant_any_more_a_new_dashboard_offers_none(store, monkeypatch):
    called = []
    app = _session_app(store, monkeypatch, agent_starters=lambda context: called.append(1))
    monkeypatch.setattr(app, "_assistant_still_allowed", lambda: False)
    assert app._create_new_dashboard("Fresh")
    assert not app._editor.ask and not app._agent_session.drawer.open
    assert not app._agent_session.starters.shown and called == []


async def test_a_phone_keeps_the_drawer_closed_with_the_starters_in_it(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    app._editor._dashboard_bar._phone_query.phone = True
    assert app._create_new_dashboard("Fresh")
    assert app._editor.editable and not app._editor.ask and not session.drawer.open
    assert session.starters.questions == DEFAULT
    # The menu's Assistant opens the drawer, where they wait.
    app._editor._dashboard_bar._show_assistant(True)
    assert session.drawer.open and session.starters.view.visible


async def test_a_starter_asks_the_assistant(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    asked = []
    monkeypatch.setattr(session.chrome, "ask", asked.append)
    app._create_new_dashboard("Fresh")
    session.starters._list.objects[1].clicks += 1
    assert asked == ["Add a table of Orders"]
    assert not session.starters.shown and not session.starters.view.visible


async def test_the_first_request_ends_the_starters(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    app._create_new_dashboard("Fresh")
    await session.drawer.chat.submit("Add a table of sales")
    assert not session.starters.shown and not session.starters.view.visible


async def test_leaving_edit_mode_ends_the_starters(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    app._create_new_dashboard("Fresh")
    app._show_view_mode()
    assert not app._agent_session.starters.shown


async def test_another_dashboard_ends_the_starters(store, monkeypatch):
    other = store.create_dashboard("alice", "Other")
    app = _session_app(store, monkeypatch)
    app._create_new_dashboard("Fresh")
    app._load_dashboard_sync(other.dashboard_id, edit=True)
    assert not app._agent_session.starters.shown


async def test_the_starters_show_on_the_drawers_chat_only(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    app._create_new_dashboard("Fresh")
    session.drawer.tab = "artifacts"
    assert session.starters.shown and not session.starters.view.visible
    session.drawer.tab = "chat"
    assert session.starters.view.visible


async def test_an_existing_dashboard_opens_without_starters(store, monkeypatch):
    model = store.create_dashboard("alice", "Plant")
    app = _session_app(store, monkeypatch)
    app._load_dashboard_sync(model.dashboard_id, edit=True)
    assert not app._editor.ask and not app._agent_session.starters.shown


async def test_a_dashboard_from_a_template_opens_with_ask_once_loaded(store, monkeypatch):
    seen = []
    app = _session_app(store, monkeypatch, agent_starters=lambda context: seen.append(context))
    session = app._agent_session
    created = app._new_dashboard_from_document(
        {"flowdash": FORMAT_VERSION}, "From template", "kpi"
    )
    assert not session.starters.shown
    await app._load_dashboard(created.dashboard_id, edit=True)
    assert app._editor.ask and session.drawer.open
    (context,) = seen
    assert (context.origin, context.template, context.dashboard_id) == (
        "template",
        "kpi",
        created.dashboard_id,
    )
    assert session.starters.questions == default_starter_questions(context)
    # Only once: opening it again later is an ordinary open.
    app._show_view_mode()
    app._load_dashboard_sync(created.dashboard_id, edit=True)
    assert not session.starters.shown


async def test_a_template_dashboard_left_before_it_loaded_opens_later_as_any_other(
    store, monkeypatch
):
    app = _session_app(store, monkeypatch)
    created = app._new_dashboard_from_document({"flowdash": FORMAT_VERSION}, "From template")
    app._leave_dashboard()
    app._load_dashboard_sync(created.dashboard_id, edit=True)
    assert not app._editor.ask and not app._agent_session.starters.shown


async def test_a_component_added_to_a_new_dashboard_from_the_gallery_gets_no_starters(
    store, monkeypatch
):
    from panel_flowdash.ui.gallery import ComponentGallery

    app = _session_app(store, monkeypatch)
    gallery = ComponentGallery(app)
    await gallery.load()
    placed = await gallery.add_to_dashboard("S/chart")
    assert placed is not None and app._editor.editable
    assert not app._editor.ask and not app._agent_session.starters.shown


async def test_the_hook_is_told_who_asks_about_which_dashboard(store, monkeypatch):
    seen = []

    def starters(context):
        seen.append(context)
        return ["One?", " ", "Two?", 3, "Three?", "Four?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    app._create_new_dashboard("Fresh")
    (context,) = seen
    assert context.identity.user == "alice" and context.identity is app._identity
    assert (context.title, context.origin, context.template) == ("Fresh", "blank", None)
    assert context.dashboard_id == app._editor.dashboard.dashboard_id
    assert context.tables == ("Sales", "Orders", "Stock")
    assert app._agent_session.starters.questions == ["One?", "Two?", "Three?"]
    assert STARTER_LIMIT == 3
    with pytest.raises(AttributeError):
        context.title = "Other"


async def test_a_hook_returning_none_offers_the_defaults_and_an_empty_list_none(
    store, monkeypatch
):
    app = _session_app(store, monkeypatch, agent_starters=lambda context: None)
    app._create_new_dashboard("Fresh")
    assert app._agent_session.starters.questions == DEFAULT
    app.agent_starters = lambda context: []
    app._create_new_dashboard("Fresh again")
    assert not app._agent_session.starters.shown and app._agent_session.drawer.open


@pytest.mark.parametrize("found", ["What's in Sales?", 42, _broken], ids=["str", "int", "raises"])
async def test_a_malformed_hook_result_offers_nothing_and_creates_the_dashboard(
    store, monkeypatch, found, caplog
):
    hook = (lambda context: found()) if callable(found) else (lambda context: found)
    app = _session_app(store, monkeypatch, agent_starters=hook)
    with caplog.at_level(logging.ERROR, logger="panel_flowdash.agent.starters"):
        assert app._create_new_dashboard("Fresh")
    assert app._editor.editable and app._agent_session.drawer.open
    assert not app._agent_session.starters.shown
    assert "starter questions hook" in caplog.text


async def test_a_hook_may_await_its_starters(store, monkeypatch):
    async def starters(context):
        await asyncio.sleep(0)
        return ["From the catalogue?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    session = app._agent_session
    app._create_new_dashboard("Fresh")
    assert not session.starters.shown
    await session.starters._task
    await asyncio.sleep(0)
    assert session.starters.questions == ["From the catalogue?"]
    assert session.starters.view.visible


async def _waiting(store, monkeypatch, gate):
    async def starters(context):
        await gate.wait()
        return ["Too late?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    app._create_new_dashboard("Fresh")
    return app, app._agent_session.starters._task


async def test_awaited_starters_are_given_up_on_leaving_edit_mode(store, monkeypatch):
    gate = asyncio.Event()
    app, task = await _waiting(store, monkeypatch, gate)
    app._show_view_mode()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app._agent_session.starters.shown


async def test_awaited_starters_are_given_up_on_another_dashboard(store, monkeypatch):
    other = store.create_dashboard("alice", "Other")
    gate = asyncio.Event()
    app, task = await _waiting(store, monkeypatch, gate)
    app._load_dashboard_sync(other.dashboard_id, edit=True)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app._agent_session.starters.shown


@pytest.mark.parametrize("how", ["typing", "sending"])
async def test_awaited_starters_are_dropped_once_the_person_types_or_asks(store, monkeypatch, how):
    gate = asyncio.Event()
    app, task = await _waiting(store, monkeypatch, gate)
    widget = app._agent_session.drawer.chat.feed.active_widget
    # As the browser reports them.
    widget._process_events({"value_input" if how == "typing" else "value": "Show me"})
    gate.set()
    await task
    await asyncio.sleep(0)
    assert not app._agent_session.starters.shown


async def test_awaited_starters_are_dropped_once_a_request_runs(store, monkeypatch):
    from panel_agents import events as ev

    gate = asyncio.Event()
    app, task = await _waiting(store, monkeypatch, gate)
    state = app._agent_session.runner.state
    # Running clears the starters, and the wait with them.
    state.apply(ev.RunStarted(run_id="r"))
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app._agent_session.starters.shown


async def test_a_slow_hook_is_given_up(store, monkeypatch, caplog):
    monkeypatch.setattr(starters_module, "STARTER_TIMEOUT", 0.01)
    gate = asyncio.Event()
    app, task = await _waiting(store, monkeypatch, gate)
    with caplog.at_level(logging.WARNING, logger="panel_flowdash.agent.starters"):
        with pytest.raises(TimeoutError):
            await task
        await asyncio.sleep(0)
    assert not app._agent_session.starters.shown
    assert "were given up" in caplog.text


async def test_the_wait_is_given_up_when_the_session_ends(store, monkeypatch):
    gate = asyncio.Event()
    app = _session_app(store, monkeypatch)
    starters = app._agent_session.starters
    destroyed = []

    class Doc:
        session_context = object()

        def on_session_destroyed(self, callback):
            destroyed.append(callback)

        def add_next_tick_callback(self, callback):
            callback()

    from types import SimpleNamespace

    monkeypatch.setattr(
        starters_module, "pn", SimpleNamespace(state=SimpleNamespace(curdoc=Doc()))
    )

    async def slow(context):
        await gate.wait()
        return ["Too late?"]

    app.agent_starters = slow
    app._create_new_dashboard("Fresh")
    task = starters._task
    (callback,) = destroyed
    callback(None)
    with pytest.raises(asyncio.CancelledError):
        await task


def test_an_awaitable_without_a_running_loop_offers_nothing(store, monkeypatch, caplog):
    async def starters(context):
        return ["Never?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level(logging.ERROR, logger="panel_flowdash.agent.starters"):
            assert app._create_new_dashboard("Fresh")
    assert not app._agent_session.starters.shown
    assert "without a running event loop" in caplog.text


async def test_a_failing_hook_offers_no_starters(store, monkeypatch, caplog):
    def starters(context):
        raise RuntimeError("catalogue offline")

    app = _session_app(store, monkeypatch, agent_starters=starters)
    with caplog.at_level(logging.ERROR, logger="panel_flowdash.agent.starters"):
        assert app._create_new_dashboard("Fresh")
    assert app._editor.ask and app._agent_session.drawer.open
    assert not app._agent_session.starters.shown
    assert "starter questions hook failed" in caplog.text


async def test_without_an_assistant_a_new_dashboard_leaves_ask_off(store, monkeypatch):
    app = _app(store, monkeypatch, components=COMPONENTS, agent=False)
    assert app._create_new_dashboard("Fresh")
    assert app._editor.editable and not app._editor.ask


async def test_a_hook_whose_awaited_starters_fail_offers_none(store, monkeypatch, caplog):
    async def starters(context):
        raise RuntimeError("catalogue offline")

    app = _session_app(store, monkeypatch, agent_starters=starters)
    session = app._agent_session
    with caplog.at_level(logging.ERROR, logger="panel_flowdash.agent.starters"):
        app._create_new_dashboard("Fresh")
        with pytest.raises(RuntimeError):
            await session.starters._task
        await asyncio.sleep(0)
    assert not session.starters.shown
    assert "starter questions hook failed" in caplog.text


async def test_a_dashboard_the_assistant_creates_mid_request_gets_no_starters(store, monkeypatch):
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    created = app._new_dashboard_from_document({"flowdash": FORMAT_VERSION}, "By the assistant")
    from panel_agents import events as ev

    session.runner.state.apply(ev.RunStarted(run_id="r"))
    assert session.runner.state.running
    app._load_dashboard_sync(created.dashboard_id, edit=True)
    assert app._editor.ask and not session.starters.shown


SCANNED = """\
from panel_flowdash import register


def presets(context):
    return [{"title": f"{name} feed", "config": {"name": name}} for name in context.get("feeds", [])]


@register(page=False, component=True, title="Feed", provides=["rows"], palette_presets=presets)
def app(config):
    return "feed"
"""


@pytest.fixture
def scanned_app(tmp_path, monkeypatch, store):
    import sys

    import panel as pn

    from panel_flowdash import AuthConfig
    from panel_flowdash.app import FlowDashApp

    section = tmp_path / "Data"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "feed.py").write_text(SCANNED)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(pn.state, "_location", None, raising=False)
    identity = Identity(user="alice", oauth_user="alice")
    monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
    app = FlowDashApp(
        project_dir=tmp_path,
        store=store,
        notifications=False,
        auth_config=AuthConfig(),
        agent=_agent(),
    )
    app._editor.context = {"feeds": ["north", "south"]}
    yield app
    sys.modules.pop("Data.feed", None)
    sys.modules.pop("Data", None)


async def test_a_new_dashboard_asks_about_scanned_presets_once_they_load(scanned_app):
    session = scanned_app._agent_session
    assert scanned_app._create_new_dashboard("Fresh")
    # The presets load in the background; the starters wait for them.
    assert scanned_app._preset_tasks and not session.starters.shown
    await session.starters._task
    await asyncio.sleep(0)
    assert session.starters.questions == [
        "What's in north feed?",
        "Add a table of south feed",
        "Chart the key numbers in north feed",
    ]
