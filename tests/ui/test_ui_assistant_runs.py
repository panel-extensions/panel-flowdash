"""UI test: a question sent from the launcher through the drawer gets an answer or an error."""

import re

import pytest

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _ask(page, text):
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill(text)
    chat.press("Enter")


def test_the_assistant_answers_a_question_asked_on_the_launcher(page):
    store, _ = _store_with("Plant", "Grid")

    async def stream(messages, info):
        results = [
            p
            for m in messages
            if not isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        if not results:
            yield {0: DeltaToolCall(name="list_dashboards", json_args="{}", tool_call_id="c1")}
            return
        titles = [t for t in ("Grid", "Plant") if t in str(results[0].content)]
        yield f"There are {len(titles)} dashboards: {' and '.join(titles)}."

    agent = Agent(
        FunctionModel(stream_function=stream),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _serve_app(page, store, "/", agent=agent)
    page.get_by_text("Grid").first.wait_for()

    _ask(page, "What dashboards are there?")

    expect(page.get_by_text("There are 2 dashboards: Grid and Plant.")).to_be_visible(
        timeout=20000
    )
    assert errors == []


def test_a_failed_request_shows_its_error_in_the_drawer(page):
    store, _ = _store_with("Plant")

    async def stream(messages, info):
        raise RuntimeError("the provider refused the request")
        yield ""

    agent = Agent(
        FunctionModel(stream_function=stream),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    _serve_app(page, store, "/", agent=agent)
    page.get_by_text("Plant").first.wait_for()

    _ask(page, "What dashboards are there?")

    expect(page.get_by_text(re.compile("the provider refused the request"))).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_placeholder(re.compile("^Ask the agent"))).to_be_enabled()
