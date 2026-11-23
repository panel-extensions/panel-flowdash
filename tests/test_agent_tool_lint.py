"""The tool sets and the instructions agree: every tool a role's instructions name is offered, and every tool is documented."""

import difflib
import inspect
import re

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from panel_flowdash.agent import DashboardOps, FlowDashDeps
from panel_flowdash.agent.assistant import EXPLORE_TOOLS, create_agent, create_explore_agent
from panel_flowdash.agent.tools import LAYOUT_TOOLS, REPORT_TOOLS, TOOLS
from panel_flowdash.editor import FlowDash

# Added by an AgentSession beside the dashboard tools, see panel_flowdash.agent.actions.
COMMAND_TOOLS = ("list_commands", "run_command")

ALL_TOOLS = (*(func.__name__ for func in TOOLS), *COMMAND_TOOLS)

ROLES = {
    "assistant": lambda model: create_agent(model, catalogue="none"),
    "explore": lambda model: create_explore_agent(model, catalogue="none"),
    "layout": lambda model: create_agent(
        model, tools=LAYOUT_TOOLS, default_instructions=False, catalogue="none"
    ),
    "report": lambda model: create_agent(
        model, tools=REPORT_TOOLS, default_instructions=False, catalogue="none"
    ),
}

# Tools a role's instructions may name although the role's own toolset does not offer them.
OFFERED_BESIDES = {"assistant": COMMAND_TOOLS}

# snake_case words the instructions use that are not tools, e.g. example node names.
NON_TOOL_WORDS = {"snake_case", "total_revenue", "revenue_by_region"}

# Every snake_case word, bare, backticked or written like a call, e.g. `add_node(`.
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def instruction_problems(text: str, offered) -> list[str]:
    """Return a line for each tool-like word in *text* that is not a tool in *offered*."""
    problems = []
    for word in sorted(set(SNAKE.findall(text)) - NON_TOOL_WORDS):
        if word in offered:
            continue
        if word in ALL_TOOLS:
            problems.append(f"{word}: a tool this role does not offer")
            continue
        close = difflib.get_close_matches(word, ALL_TOOLS, n=1)
        hint = f"did you mean {close[0]}? " if close else ""
        problems.append(
            f"{word}: no such tool ({hint}add it to NON_TOOL_WORDS if it is not meant as one)"
        )
    return problems


async def _seen(role):
    seen = {}

    def respond(messages, info):
        seen["tools"] = [tool.name for tool in info.function_tools]
        seen["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("ok")])

    editor = FlowDash()
    try:
        await ROLES[role](FunctionModel(respond)).run(
            "Hi", deps=FlowDashDeps(DashboardOps(editor))
        )
    finally:
        editor.dispose()
    return seen


@pytest.mark.parametrize("role", ROLES)
async def test_every_tool_the_instructions_name_is_offered(role):
    seen = await _seen(role)
    offered = {*seen["tools"], *OFFERED_BESIDES.get(role, ())}
    problems = instruction_problems(seen["instructions"], offered)
    listed = "\n".join(f"- {p}" for p in problems)
    assert not problems, f"The {role!r} instructions name tools it cannot call:\n{listed}"


@pytest.mark.parametrize(
    "text, problem",
    [
        (
            "Then call add_fliter for each column.",
            "add_fliter: no such tool (did you mean add_filter?",
        ),
        ("Use `rename_dashboard` to name it.", "rename_dashboard: no such tool"),
        ("Rename it with rename_dashboard(title).", "rename_dashboard: no such tool"),
        ("Lay them out with arrange_canvas.", "arrange_canvas: a tool this role does not offer"),
    ],
)
def test_a_typo_or_a_tool_not_offered_is_reported(text, problem):
    (found,) = instruction_problems(text, {"add_filter", "set_title"})
    assert found.startswith(problem)


def _undocumented(func) -> list[str]:
    doc = inspect.getdoc(func) or ""
    if not doc.strip():
        return ["no docstring"]
    # The first parameter is the run context.
    params = list(inspect.signature(func).parameters)[1:]
    if not params:
        return []
    if "\nArgs:\n" not in f"\n{doc}":
        return [f"no Args section for {', '.join(params)}"]
    args = doc.split("Args:\n", 1)[1]
    return [f"{p} is not under Args" for p in params if not re.search(rf"^\s+{p}:", args, re.M)]


def test_every_tool_has_a_docstring_with_its_args():
    problems = {func.__name__: _undocumented(func) for func in TOOLS}
    problems = {name: found for name, found in problems.items() if found}
    listed = "\n".join(f"- {name}: {'; '.join(found)}" for name, found in sorted(problems.items()))
    assert not problems, f"Tools whose docstring does not document them:\n{listed}"


@pytest.mark.parametrize("tools", [EXPLORE_TOOLS, LAYOUT_TOOLS, REPORT_TOOLS])
def test_the_named_sets_name_existing_tools(tools):
    unknown = sorted(set(tools) - set(ALL_TOOLS))
    assert not unknown, f"Unknown tools in the set: {', '.join(unknown)}"


def test_the_layout_set_has_the_report_title_and_filter_tools():
    assert {"set_title", "add_filter", "remove_filter", *REPORT_TOOLS} <= set(LAYOUT_TOOLS)
