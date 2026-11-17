"""The default agent sends no max_tokens of its own, and FlowDashApp's agent_model_settings reach every request."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.assistant import create_agent

from .test_agent_tools import _app
from .test_serve_config import _serve, _write_init, project  # noqa: F401


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)


def _recording_model(seen):
    async def stream(messages, info):
        seen.append(dict(info.model_settings or {}))
        yield "ok"

    return FunctionModel(stream_function=stream)


async def _settings_of_a_request(monkeypatch, **params):
    seen = []
    app = _app(
        MemoryDashboardStore(), monkeypatch, agent=create_agent(_recording_model(seen)), **params
    )
    await app._agent_session.runner.run("Hi")
    assert app._agent_session.runner.error is None
    (settings,) = seen
    return settings


async def test_the_default_agent_sends_no_max_tokens(monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MAX_TOKENS", "1000")
    settings = await _settings_of_a_request(monkeypatch)
    assert "max_tokens" not in settings


async def test_create_agent_passes_model_settings_on():
    agent = create_agent("test", model_settings={"temperature": 0})
    assert agent.model_settings == {"temperature": 0}


async def test_agent_model_settings_reach_every_request(monkeypatch):
    settings = await _settings_of_a_request(
        monkeypatch, agent_model_settings={"max_tokens": 512, "temperature": 0}
    )
    assert settings["max_tokens"] == 512 and settings["temperature"] == 0


async def test_agent_model_settings_apply_to_a_projects_own_agent(monkeypatch):
    seen = []
    agent = Agent(_recording_model(seen))
    app = _app(
        MemoryDashboardStore(), monkeypatch, agent=agent, agent_model_settings={"temperature": 0.5}
    )
    await app._agent_session.runner.run("Hi")
    assert seen[0]["temperature"] == 0.5


def test_agent_model_settings_can_be_set_in_the_project(project, monkeypatch):  # noqa: F811
    _write_init(project, 'agent_model_settings = {"temperature": 0}\n')
    assert _serve(project, monkeypatch)["routes"]["agent_model_settings"] == {"temperature": 0}
