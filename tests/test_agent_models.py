"""Configuring the models a person may pick for the assistant."""

import pytest
from panel_agents import ModelChoice

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.models import configured_models, parse_models, validate_models

from .test_agent_tools import _app


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("FLOWDASH_AGENT_MODEL", raising=False)
    monkeypatch.delenv("FLOWDASH_AGENT_MODELS", raising=False)


@pytest.fixture
def store():
    return MemoryDashboardStore()


def test_models_are_read_as_ids_with_optional_labels():
    assert parse_models(" openai:a = Fast , anthropic:b,, ") == [
        {"id": "openai:a", "label": "Fast"},
        {"id": "anthropic:b"},
    ]
    assert parse_models(["openai:a=A"]) == [{"id": "openai:a", "label": "A"}]
    assert parse_models(None) == parse_models("") == []
    assert parse_models(["a,b=B", "c"]) == [{"id": "a"}, {"id": "b", "label": "B"}, {"id": "c"}]
    for bad in ("=Fast", " = ", "a,=B"):
        with pytest.raises(ValueError, match="names no model"):
            parse_models(bad, "--agent-model")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"label": "x"}, r"agent_models\[1\] \(\{'label': 'x'\}\) needs an 'id'"),
        ({"id": "a", "price": 1}, "unknown keys 'price'"),
        ({"id": "a", "label": 3}, "'label' must be a string"),
        ("", "empty model id"),
        (3, "must be a model id"),
    ],
)
def test_invalid_entries_are_refused_by_name(entry, message):
    with pytest.raises(ValueError, match=message):
        validate_models(["test", entry])


def test_ids_are_offered_once(monkeypatch):
    models, _ = configured_models(["test", {"id": "test", "label": "Again"}, "openai:b"], None)
    assert [c.id for c in models] == ["test", "openai:b"] and models[0].label is None
    monkeypatch.setenv("FLOWDASH_AGENT_MODELS", "a=A,a=B")
    assert configured_models([], None) == ([ModelChoice("a", label="A")], "a")


def test_the_default_is_the_model_or_the_first_listed(monkeypatch):
    assert configured_models([], None) == ([], None)
    assert configured_models(["test", "openai:b"], None) == (
        [ModelChoice("test"), ModelChoice("openai:b")],
        "test",
    )
    models, model = configured_models([{"id": "openai:b", "label": "B"}], "test")
    assert model == "test" and [c.id for c in models] == ["test", "openai:b"]

    monkeypatch.setenv("FLOWDASH_AGENT_MODELS", "openai:a=A,openai:b")
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "openai:b")
    models, model = configured_models([], None)
    assert model == "openai:b" and models == [
        ModelChoice("openai:a", label="A"),
        ModelChoice("openai:b"),
    ]
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "openai:c")
    assert [c.id for c in configured_models([], None)[0]] == ["openai:c", "openai:a", "openai:b"]


pytest.importorskip("pydantic_ai")


async def test_the_session_offers_the_configured_models(store, monkeypatch):
    app = _app(store, monkeypatch, agent_models=["test", {"id": "bogus:x", "label": "X"}])
    picker = app._agent_session.drawer.chat.model_picker
    assert [c.id for c in picker.choices] == ["test", "bogus:x"]
    assert picker.value == "test" and picker.unavailable == {"bogus:x": "Unknown model: bogus:x"}
    assert picker.remember == "flowdash-agent-model"
    assert app._agent_session.runner.model == "test"


async def test_the_environment_lists_the_models(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODELS", "bogus:x=X,test=Test")
    session = _app(store, monkeypatch)._agent_session
    picker = session.drawer.chat.model_picker
    assert picker.value == "test" and session.runner.model == "test"
    assert picker._menu.visible


async def test_one_model_offers_no_picker(store, monkeypatch):
    monkeypatch.setenv("FLOWDASH_AGENT_MODEL", "test")
    session = _app(store, monkeypatch)._agent_session
    chat = session.drawer.chat
    assert chat.model_picker.choices == [] and session.runner.model is None
    assert chat.remember_model is None
    session = _app(store, monkeypatch, agent_models=["test", "test"])._agent_session
    assert (
        session.drawer.chat.model_picker.choices == []
        and session.drawer.chat.remember_model is None
    )


async def test_the_app_refuses_an_invalid_model_list(store, monkeypatch):
    with pytest.raises(ValueError, match=r"agent_models\[1\]"):
        _app(store, monkeypatch, agent_models=["test", {"label": "x"}])


async def test_viewers_without_the_assistant_get_no_picker(store, monkeypatch):
    app = _app(
        store,
        monkeypatch,
        agent_models=["test", "openai:b"],
        agent_authorize=lambda identity: False,
    )
    assert app._agent_session is None
