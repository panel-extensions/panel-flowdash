"""A new dashboard opens with Ask on: the assistant's drawer, with questions to start from."""

import asyncio
import logging

import param
import pytest

pytest.importorskip("pydantic_ai")

from panel.viewable import Viewer
from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.agent import FORMAT_VERSION, DashboardOps
from panel_flowdash.agent.starters import (
    STARTER_LIMIT,
    data_tables,
    default_starter_questions,
)
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


def test_the_default_questions_take_the_tables_in_turn():
    editor = _editor()
    questions = default_starter_questions(DashboardOps(editor))
    assert questions == DEFAULT


def test_one_table_is_asked_about_in_each_way():
    ops = DashboardOps(_editor({"S/sales": sales, "S/chart": chart}))
    assert default_starter_questions(ops) == [
        "What's in Sales?",
        "Add a table of Sales",
        "Chart the key numbers in Sales",
    ]


def test_without_data_sources_there_are_no_questions():
    ops = DashboardOps(_editor({"S/chart": chart}))
    assert default_starter_questions(ops) == []


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
    app = _session_app(store, monkeypatch)
    session = app._agent_session
    created = app._new_dashboard_from_document({"flowdash": FORMAT_VERSION}, "From template")
    assert not session.starters.shown
    app._load_dashboard_sync(created.dashboard_id, edit=True)
    assert app._editor.ask and session.drawer.open
    assert session.starters.questions == DEFAULT
    # Only once: opening it again later is an ordinary open.
    app._show_view_mode()
    app._load_dashboard_sync(created.dashboard_id, edit=True)
    assert not session.starters.shown


async def test_the_hook_supplies_the_starters_and_at_most_three_show(store, monkeypatch):
    seen = []

    def starters(ops):
        seen.append(ops)
        return ["One?", " ", "Two?", 3, "Three?", "Four?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    app._create_new_dashboard("Fresh")
    assert seen == [app._agent_session.ops]
    assert app._agent_session.starters.questions == ["One?", "Two?", "Three?"]
    assert STARTER_LIMIT == 3


async def test_a_hook_may_await_its_starters(store, monkeypatch):
    async def starters(ops):
        await asyncio.sleep(0)
        return ["From the catalogue?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    session = app._agent_session
    app._create_new_dashboard("Fresh")
    assert not session.starters.shown
    task = session.starters._task
    await task
    await asyncio.sleep(0)
    assert session.starters.questions == ["From the catalogue?"]
    assert session.starters.view.visible


async def test_awaited_starters_that_arrive_late_are_dropped(store, monkeypatch):
    gate = asyncio.Event()

    async def starters(ops):
        await gate.wait()
        return ["Too late?"]

    app = _session_app(store, monkeypatch, agent_starters=starters)
    session = app._agent_session
    app._create_new_dashboard("Fresh")
    task = session.starters._task
    app._show_view_mode()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.starters.shown


async def test_a_failing_hook_offers_no_starters(store, monkeypatch, caplog):
    def starters(ops):
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
    async def starters(ops):
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
