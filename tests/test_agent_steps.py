"""Refused, failed and retried steps: how the chat shows them and what the model is told."""

import pytest

pytest.importorskip("pydantic_ai")

from panel_agents import RunState, events as ev
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from panel_flowdash.agent import create_agent
from panel_flowdash.agent.chrome import TOOL_LABELS, register_tool_labels
from panel_flowdash.agent.steps import (
    RETRIED,
    RETRY_FAILED,
    RETRYING,
    failure_instructions,
    mark_refusals,
    refusal,
    short_reason,
    unresolved_failures,
)
from panel_flowdash.agent.tools import FlowDashDeps

from .test_agent_tools import ops, store  # noqa: F401


def test_a_refusal_is_read_from_its_prefix():
    assert refusal("Refused: dashboards can only be deleted from the app.") == (
        "Refused: dashboards can only be deleted from the app."
    )
    assert refusal("Not applied: no node named 'x'. Call get_dashboard.") == (
        "Not applied: no node named 'x'."
    )
    assert refusal("Not available: the output is not computed yet.") is not None
    assert refusal("Added number_1.") is None
    assert refusal({"error": "Refused: no"}) is None


def test_a_reason_is_its_first_sentence_cut_short():
    assert short_reason("One.  Two.") == "One."
    assert short_reason("a\nb") == "a b"
    long = short_reason("x" * 500)
    assert len(long) == 200 and long.endswith("…")


def _call(state, call_id, tool, run="run-1"):
    state.apply(ev.ToolCallStart(tool_call_id=call_id, tool_name=tool, run_id=run))
    state.apply(ev.ToolCallEnd(tool_call_id=call_id, run_id=run))


def _result(state, call_id, content, is_error=False, run="run-1"):
    state.apply(
        ev.ToolCallResult(tool_call_id=call_id, content=content, is_error=is_error, run_id=run)
    )


def _state():
    state = RunState(flush_interval=0)
    mark_refusals(state)
    return state


def test_a_refused_call_shows_as_failed_with_its_reason():
    state = _state()
    _call(state, "a", "remove_node")
    _result(state, "a", "Refused: the user switched to another dashboard. Stop and say so.")
    call = state.tool_calls["a"]
    assert call.status == "failed"
    assert call.error == "Refused: the user switched to another dashboard."


def test_a_call_that_succeeds_is_left_alone():
    state = _state()
    _call(state, "a", "add_node")
    _result(state, "a", "Added number_1.")
    assert state.tool_calls["a"].status == "success"
    assert state.tool_calls["a"].error is None


def test_a_retry_that_succeeds_is_noted_on_the_refused_call():
    state = _state()
    _call(state, "a", "set_config")
    _result(state, "a", "Not applied: no field 'colour'.")
    _call(state, "b", "set_config")
    assert state.tool_calls["a"].error == f"Not applied: no field 'colour'.\n{RETRYING}"
    _result(state, "b", "Changed chart_1.")
    assert state.tool_calls["a"].error == f"Not applied: no field 'colour'.\n{RETRIED}"
    assert state.tool_calls["b"].status == "success"


def test_a_retry_after_a_raised_error_is_noted_and_can_fail_too():
    state = _state()
    _call(state, "a", "connect")
    _result(state, "a", "No port 'valu' on number_1.", is_error=True)
    _call(state, "b", "connect")
    _result(state, "b", "Refused: no.")
    assert state.tool_calls["a"].error == f"No port 'valu' on number_1.\n{RETRY_FAILED}"
    # The failed retry is retried in turn.
    _call(state, "c", "connect")
    _result(state, "c", "Connected.")
    assert state.tool_calls["b"].error == f"Refused: no.\n{RETRIED}"
    assert state.tool_calls["a"].error.endswith(RETRY_FAILED)


def test_calls_of_other_tools_or_runs_are_not_retries():
    state = _state()
    _call(state, "a", "set_config")
    _result(state, "a", "Refused: no.")
    _call(state, "b", "add_node")
    _call(state, "c", "set_config", run="run-2")
    assert state.tool_calls["a"].error == "Refused: no."


def test_marking_a_state_twice_marks_once():
    state = _state()
    mark_refusals(state)
    _call(state, "a", "set_config")
    _result(state, "a", "Refused: no.")
    _call(state, "b", "set_config")
    assert state.tool_calls["a"].error == f"Refused: no.\n{RETRYING}"


def test_a_state_marked_after_its_calls_ran_is_marked_too():
    state = RunState(flush_interval=0)
    _call(state, "a", "set_config")
    _result(state, "a", "Refused: no.")
    _call(state, "b", "set_config")
    _result(state, "b", "Changed.")
    mark_refusals(state)
    assert state.tool_calls["a"].status == "failed"
    assert state.tool_calls["a"].error == f"Refused: no.\n{RETRIED}"


def _turn(*parts):
    return [ModelRequest(parts=[UserPromptPart("do it")]), ModelRequest(parts=list(parts))]


def test_unresolved_failures_are_those_not_redone():
    messages = _turn(
        ToolReturnPart("set_config", "Refused: no. More.", tool_call_id="a"),
        ToolReturnPart("connect", "No port 'valu'.", tool_call_id="b", outcome="retried"),
        ToolReturnPart("add_node", "Not applied: unknown component.", tool_call_id="c"),
        ToolReturnPart("add_node", "Added number_1.", tool_call_id="d"),
    )
    assert unresolved_failures(messages) == [
        ("set_config", "Refused: no."),
        ("connect", "Failed: No port 'valu'."),
    ]


def test_failures_of_an_earlier_request_are_not_named():
    earlier = _turn(ToolReturnPart("set_config", "Refused: no.", tool_call_id="a"))
    messages = [*earlier, ModelResponse(parts=[TextPart("Sorry.")]), *_turn()]
    assert unresolved_failures(messages) == []
    assert failure_instructions(messages) == ""


def test_the_failure_instructions_name_the_steps():
    text = failure_instructions(
        _turn(ToolReturnPart("set_config", "Refused: no.", tool_call_id="a"))
    )
    assert "set_config (Refused: no.)" in text
    assert len(text.splitlines()) == 2


async def test_the_agent_is_told_of_steps_that_were_refused_or_failed(ops):  # noqa: F811
    seen = []

    def model(messages, info):
        seen.append(info.instructions or "")
        returned = [
            p
            for m in messages
            for p in m.parts
            if hasattr(p, "tool_call_id") and not isinstance(p, ToolCallPart)
        ]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "delete_dashboard", {"dashboard_id": "x", "title": "X"}, tool_call_id="a"
                    ),
                    ToolCallPart("remove_node", {"name": "nope"}, tool_call_id="b"),
                ]
            )
        return ModelResponse(parts=[TextPart("Done.")])

    agent = create_agent(FunctionModel(model), catalogue="none")
    await agent.run("tidy up", deps=FlowDashDeps(ops))
    assert "refused or failed" not in seen[0]
    assert "delete_dashboard (Refused: dashboards can only be deleted from the app.)" in seen[1]
    assert "remove_node (Failed:" in seen[1]


def test_hosts_register_labels_for_their_tools(monkeypatch):
    monkeypatch.setattr("panel_flowdash.agent.chrome.TOOL_LABELS", dict(TOOL_LABELS))
    from panel_flowdash.agent import chrome

    register_tool_labels({"run_sql": "Query the data", "add_node": "Add a step"})
    assert chrome.TOOL_LABELS["run_sql"] == "Query the data"
    assert chrome.TOOL_LABELS["add_node"] == "Add a step"
    assert "add_node" in TOOL_LABELS and callable(TOOL_LABELS["add_node"])
