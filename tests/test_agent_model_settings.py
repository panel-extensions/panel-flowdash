"""The default agent limits max_tokens, and FlowDashApp's agent_model_settings reach every request."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.assistant import DEFAULT_MAX_TOKENS, MAX_TOKENS_ENV, create_agent

from .test_agent_tools import _app
from .test_serve_config import _serve, _write_init, project  # noqa: F401


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)
    monkeypatch.delenv(MAX_TOKENS_ENV, raising=False)


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


async def test_the_default_agent_limits_max_tokens(monkeypatch):
    settings = await _settings_of_a_request(monkeypatch)
    assert settings["max_tokens"] == DEFAULT_MAX_TOKENS == 4096


async def test_the_environment_overrides_max_tokens(monkeypatch):
    monkeypatch.setenv(MAX_TOKENS_ENV, "1000")
    assert (await _settings_of_a_request(monkeypatch))["max_tokens"] == 1000


def test_an_invalid_max_tokens_says_so(monkeypatch):
    monkeypatch.setenv(MAX_TOKENS_ENV, "lots")
    with pytest.raises(ValueError, match=MAX_TOKENS_ENV):
        create_agent("test")


async def test_create_agent_merges_model_settings_over_the_defaults():
    agent = create_agent("test", model_settings={"temperature": 0})
    assert agent.model_settings == {"max_tokens": DEFAULT_MAX_TOKENS, "temperature": 0}


async def test_agent_model_settings_reach_every_request(monkeypatch):
    settings = await _settings_of_a_request(
        monkeypatch, agent_model_settings={"max_tokens": 512, "temperature": 0}
    )
    assert settings["max_tokens"] == 512 and settings["temperature"] == 0


async def test_agent_model_settings_apply_to_a_projects_own_agent(monkeypatch):
    seen = []
    agent = Agent(_recording_model(seen))
    app = _app(
        MemoryDashboardStore(), monkeypatch, agent=agent, agent_model_settings={"max_tokens": 256}
    )
    await app._agent_session.runner.run("Hi")
    assert seen[0]["max_tokens"] == 256


def test_agent_model_settings_can_be_set_in_the_project(project, monkeypatch):  # noqa: F811
    _write_init(project, 'agent_model_settings = {"max_tokens": 2048}\n')
    assert _serve(project, monkeypatch)["routes"]["agent_model_settings"] == {"max_tokens": 2048}
