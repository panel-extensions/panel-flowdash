"""Listing the models a provider offers, for the model pickers."""

import json

import pytest

from panel_flowdash import ai
from panel_flowdash.ai import list_models

KEY = "sk-test-key-not-real-1234"


def _fetcher(responses, seen=None):
    async def fetch(url, headers):
        if seen is not None:
            seen.append((url, dict(headers)))
        status, body = responses[url.split("?")[0]]
        return status, body if isinstance(body, str) else json.dumps(body)

    return fetch


@pytest.mark.parametrize(
    ("name", "url", "body", "expected"),
    [
        (
            "openai",
            "https://api.openai.com/v1/models",
            {
                "data": [
                    {"id": "gpt-old", "created": 1},
                    {"id": "gpt-new", "created": 3},
                    {"id": "text-embedding-3-large", "created": 4},
                    {"id": "gpt-realtime", "created": 5},
                    {"id": "gpt-image-2", "created": 6},
                ]
            },
            ["openai:gpt-new", "openai:gpt-old"],
        ),
        (
            "anthropic",
            "https://api.anthropic.com/v1/models",
            {
                "data": [
                    {"id": "claude-a", "created_at": "2026-01-01T00:00:00Z"},
                    {"id": "claude-b", "created_at": "2026-06-01T00:00:00Z"},
                ]
            },
            ["anthropic:claude-b", "anthropic:claude-a"],
        ),
        (
            "google-gla",
            "https://generativelanguage.googleapis.com/v1beta/models",
            {
                "models": [
                    {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]},
                ]
            },
            ["google-gla:gemini-x"],
        ),
        (
            "mistral",
            "https://api.mistral.ai/v1/models",
            {
                "data": [
                    {"id": "mistral-large", "capabilities": {"completion_chat": True}},
                    {"id": "mistral-embed", "capabilities": {"completion_chat": False}},
                ]
            },
            ["mistral:mistral-large"],
        ),
        (
            "openrouter",
            "https://openrouter.ai/api/v1/models",
            {"data": [{"id": "openai/gpt-x", "created": 2}, {"id": "anthropic/claude-x"}]},
            ["openrouter:openai/gpt-x", "openrouter:anthropic/claude-x"],
        ),
    ],
)
async def test_a_provider_lists_its_chat_models_newest_first(name, url, body, expected):
    seen = []
    result = await list_models(name, KEY, fetch=_fetcher({url: (200, body)}, seen))
    assert list(result.ids) == expected and result.message == ""
    assert KEY in " ".join(seen[0][1].values())


async def test_ollama_lists_the_models_on_its_server():
    fetch = _fetcher({"http://gpu:11434/api/tags": (200, {"models": [{"name": "qwen3:14b"}]})})
    result = await list_models("ollama", base_url="http://gpu:11434/v1", fetch=fetch)
    assert result.ids == ("ollama:qwen3:14b",)


async def test_a_refused_or_unreadable_list_says_so_without_the_key():
    url = "https://api.openai.com/v1/models"
    refused = await list_models("openai", KEY, fetch=_fetcher({url: (401, "")}))
    assert refused.ids == () and "(401)" in refused.message and KEY not in refused.message
    garbled = await list_models("openai", KEY + "2", fetch=_fetcher({url: (200, "<html>")}))
    assert garbled.ids == () and "cannot read" in garbled.message
    unreachable = await list_models("openai", KEY + "3")
    assert unreachable.ids == () and "could not be reached" in unreachable.message
    assert (await list_models("openai")).message == "No key is set."


async def test_a_list_is_reused_for_the_same_key_and_never_keyed_by_it():
    url = "https://api.openai.com/v1/models"
    seen = []
    fetch = _fetcher({url: (200, {"data": [{"id": "gpt-x"}]})}, seen)
    await list_models("openai", KEY, fetch=fetch)
    await list_models("openai", KEY, fetch=fetch)
    assert len(seen) == 1
    await list_models("openai", KEY + "other", fetch=fetch)
    assert len(seen) == 2
    assert KEY not in repr(ai._MODEL_LISTS)
