"""A failed request to the assistant shows in its chat and in the server log."""

import logging

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.session import AgentSession
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from .test_agent_tools import _app

ERROR = "the provider refused the request"


def _refusing_agent():
    async def stream(messages, info):
        raise RuntimeError(ERROR)
        yield ""

    return Agent(
        FunctionModel(stream_function=stream),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )


def _feed_texts(session):
    return [str(getattr(m.object, "object", "")) for m in session.drawer.chat.feed.objects]


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)


@pytest.mark.parametrize("on_dashboard", [False, True])
async def test_a_failed_request_shows_in_the_chat_and_the_log(monkeypatch, caplog, on_dashboard):
    store = MemoryDashboardStore()
    app = _app(store, monkeypatch)
    if on_dashboard:
        app._load_dashboard_sync(store.create_dashboard("alice", "Live").dashboard_id)
    session = AgentSession(app, _refusing_agent())

    with caplog.at_level(logging.ERROR):
        assert await session.runner.run("What dashboards are there?") is None

    error = session.runner.error
    assert isinstance(error, RuntimeError)
    texts = _feed_texts(session)
    assert texts[0] == "What dashboards are there?"
    assert [t for t in texts if ERROR in t] == texts[1:] and len(texts) == 2
    assert [r.exc_info[1] for r in caplog.records if r.exc_info] == [error]
    assert not session.runner.state.running
