"""UI tests of the assistant's model picker."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_agents import ModelChoice
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def answering(name):
    async def stream(messages, info):
        yield f"Answered by {name}."

    return FunctionModel(stream_function=stream, model_name=name)


def _ask(page, text):
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill(text)
    chat.press("Enter")


def _open(page, **params):
    store, (dash,) = _store_with("Plant")
    apps = []
    agent = Agent(answering("fast-model"), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent, **params)
    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    return apps


def test_one_model_is_named_without_a_picker(page):
    from panel_agents import ModelPicker

    _open(page)
    label = page.locator(".pa-model-fixed")
    # panel-agents releases before the read-only label hide the picker with a single model.
    if ModelPicker(models=["fast-model"])._menu.visible:
        expect(label).to_have_text("Model: fast-model")
        expect(label).to_have_attribute("title", "function:fast-model")
    else:
        expect(label).to_have_count(0)
    _ask(page, "Hello")
    expect(page.get_by_text("Answered by fast-model.")).to_be_visible(timeout=20000)
    expect(page.get_by_role("button", name=re.compile("^Model:"))).to_have_count(0)


MODELS = [
    ModelChoice("fast-model", label="Fast", model=answering("fast-model")),
    ModelChoice("deep-model", label="Deep", description="Slower", model=answering("deep-model")),
    {"id": "bogus:model", "label": "Broken"},
]


def test_a_picked_model_answers_the_next_request_and_its_details_name_it(page):
    apps = _open(page, agent_models=MODELS)
    picker = page.get_by_role("button", name="Model: Fast")
    expect(picker).to_have_text("Fast")
    _ask(page, "First")
    expect(page.get_by_text("Answered by fast-model.")).to_be_visible(timeout=20000)

    picker.click()
    listbox = page.get_by_role("listbox", name="Model")
    broken = listbox.get_by_role("option", name=re.compile("^Broken"))
    expect(broken).to_have_attribute("aria-disabled", "true")
    expect(broken.get_by_text("Unavailable: Unknown model: bogus:model")).to_be_visible()
    expect(
        listbox.get_by_role("option", name=re.compile("^Deep")).get_by_text("Slower")
    ).to_be_visible()
    listbox.get_by_role("option", name=re.compile("^Deep")).click()
    expect(page.get_by_role("button", name="Model: Deep")).to_be_visible()
    _ask(page, "Second")
    expect(page.get_by_text("Answered by deep-model.")).to_be_visible(timeout=20000)
    runs = apps[0]._agent_session.runner.state.runs.values()
    wait_until(lambda: [run.models for run in runs] == [["fast-model"], ["deep-model"]], page)

    expect(page.get_by_text("Details", exact=True)).to_have_count(2, timeout=15000)
    page.get_by_text("Details", exact=True).last.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("deep-model").first).to_be_visible()
    expect(dialog.get_by_text("fast-model")).to_have_count(0)


def test_the_keyboard_opens_the_picker_in_the_drawer(page):
    apps = _open(page, agent_models=MODELS)
    button = page.get_by_role("button", name="Model: Fast")
    button.focus()
    page.keyboard.press("Enter")
    listbox = page.get_by_role("listbox", name="Model")
    expect(listbox).to_be_visible()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Model: Deep")).to_be_focused()
    wait_until(lambda: apps[0]._agent_session.runner.model is MODELS[1].model, page)
    assert not apps[0]._agent_session.runner.state.runs


def test_the_browser_remembers_the_pick(page):
    apps = _open(page, agent_models=MODELS)
    page.get_by_role("button", name="Model: Fast").click()
    page.get_by_role("option", name=re.compile("^Deep")).click()
    wait_until(
        lambda: (
            page.evaluate("() => localStorage.getItem('flowdash-agent-model')") == "deep-model"
        ),
        page,
    )
    page.reload()
    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    expect(page.get_by_role("button", name="Model: Deep")).to_be_visible()
    _ask(page, "Hi")
    expect(page.get_by_text("Answered by deep-model.")).to_be_visible(timeout=20000)
    assert len(apps) == 2


def test_a_viewer_without_the_assistant_sees_no_picker(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(answering("fast-model"), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    _serve_app(
        page,
        store,
        f"/dash/{dash}",
        agent=agent,
        agent_models=MODELS,
        agent_authorize=lambda _: False,
    )
    page.get_by_text("note tile").first.wait_for()
    expect(page.get_by_role("button", name="Assistant", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Model:"))).to_have_count(0)


def test_explore_offers_the_same_models_and_shares_the_pick(page):
    store, (dash,) = _store_with("Plant")
    apps = []
    agent = Agent(answering("fast-model"), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent, agent_models=MODELS)
    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Explore", exact=True).click()
    explore = page.locator(".flowdash-explore")
    explore.get_by_role("button", name="Model: Fast").click()
    page.get_by_role("listbox", name="Model").get_by_role(
        "option", name=re.compile("^Deep")
    ).click()
    expect(explore.get_by_role("button", name="Model: Deep")).to_be_visible()
    composer = page.get_by_placeholder("Ask a question about this dashboard…")
    composer.fill("Hello")
    composer.press("Enter")
    expect(explore.get_by_text("Answered by deep-model.")).to_be_visible(timeout=20000)
    session = apps[0]._agent_session
    assert session.drawer.chat.model_id == "deep-model"
    page.get_by_role("button", name="View", exact=True).click()
    page.get_by_role("button", name="Assistant", exact=True).click()
    expect(page.get_by_role("button", name="Model: Deep")).to_be_visible()
