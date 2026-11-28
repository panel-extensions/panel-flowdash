"""The assistant's refused, failed and retried steps, shown in the chat and named to the model.

FlowDash's tools refuse a request they cannot or may not carry out by
returning a message that starts with one of :data:`REFUSAL_PREFIXES`, so the
model reads why and may try again. To panel-agents such a call succeeded, so
its step would read as done and fold away with the others. :func:`mark_refusals`
shows it as failed with the reason, and says on it whether it was retried.

:func:`unresolved_failures` reads the same from the model's messages, for the
instructions that ask the model to name the steps it did not get done.
"""

from __future__ import annotations

import re
import typing as t
import weakref

if t.TYPE_CHECKING:
    from panel_agents import RunState
    from panel_agents.state import ToolCallState

#: How a tool's result starts when the tool refused the request.
REFUSAL_PREFIXES = ("Refused:", "Not applied:", "Not available:")

#: The longest reason shown on a step, in characters.
MAX_REASON = 200

RETRYING = "Retried in a later step."
RETRIED = "Retried in a later step, which succeeded."
RETRY_FAILED = "Retried in a later step, which did not succeed either."

_SENTENCE = re.compile(r"(?<=[.!?])\s")


def short_reason(text: str, limit: int = MAX_REASON) -> str:
    """Return the first sentence of *text*, cut to *limit* characters."""
    text = " ".join(str(text).split())
    text = _SENTENCE.split(text, maxsplit=1)[0]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def refusal(result: t.Any) -> str | None:
    """Return a refusal's prefix and short reason, e.g. ``"Refused: ..."``, or None if *result* is not one."""
    if not isinstance(result, str):
        return None
    text = result.lstrip()
    for prefix in REFUSAL_PREFIXES:
        if text.startswith(prefix):
            return f"{prefix} {short_reason(text[len(prefix) :])}"
    return None


# The states marked so far, so a state shown by several chats is marked once.
_MARKED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def mark_refusals(state: RunState) -> None:
    """Show the refused calls of *state* as failed steps that say whether they were retried.

    A call whose result is a refusal (see :data:`REFUSAL_PREFIXES`) is marked
    ``failed`` with the prefix and the first sentence of the reason as its
    error, so its step stays out of the folded group of steps and shows the
    reason. A later call of the same tool in the same run is its retry: the
    failed call, refused or raised, then says so, and whether the retry
    succeeded. Calling this again for the same state does nothing.
    """
    if state in _MARKED:
        return
    marker = _Marker(state)
    _MARKED[state] = marker
    for call in list(state.tool_calls.values()):
        marker.started(call)
        marker.ended(call)
    state.subscribe(marker.on_event)


class _Marker:
    def __init__(self, state: RunState):
        self.state = state
        # The failed call each retry follows up, by the retry's tool call id.
        self.retry_of: dict[str, ToolCallState] = {}
        # The calls whose retry has started, by tool call id.
        self.retried: set[str] = set()
        # The error each failed call had before a note about its retry was added.
        self.errors: dict[str, str] = {}

    def on_event(self, event: t.Any) -> None:
        call = self.state.tool_calls.get(getattr(event, "tool_call_id", None) or "")
        if call is None:
            return
        if event.type == "TOOL_CALL_START":
            self.started(call)
        elif event.type == "TOOL_CALL_RESULT":
            self.ended(call)

    def started(self, call: ToolCallState) -> None:
        if call.tool_call_id in self.retry_of:
            return
        for earlier in reversed(list(self.state.tool_calls.values())):
            if earlier is call:
                continue
            if (
                earlier.run_id == call.run_id
                and earlier.tool_name == call.tool_name
                and earlier.status == "failed"
                and earlier.tool_call_id not in self.retried
            ):
                self.retried.add(earlier.tool_call_id)
                self.retry_of[call.tool_call_id] = earlier
                self._note(earlier, RETRYING)
                if call.status in ("success", "failed"):
                    self.ended(call)
                return

    def ended(self, call: ToolCallState) -> None:
        if call.status == "success" and (reason := refusal(call.result)) is not None:
            call.param.update(status="failed", error=reason)
        earlier = self.retry_of.get(call.tool_call_id)
        if earlier is None:
            return
        if call.status == "success":
            self._note(earlier, RETRIED)
        elif call.status in ("failed", "denied"):
            self._note(earlier, RETRY_FAILED)

    def _note(self, call: ToolCallState, note: str) -> None:
        error = self.errors.setdefault(call.tool_call_id, call.error or "The call failed.")
        call.error = f"{error}\n{note}"


def unresolved_failures(messages: t.Sequence[t.Any]) -> list[tuple[str, str]]:
    """Return the tools refused or failed since the last user prompt, with the reason, unless a later call succeeded.

    *messages* are pydantic-ai messages, e.g. ``RunContext.messages``. A tool
    counts as done again once a later call of it returns a result that is not
    a refusal.
    """
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    start = 0
    for i, message in enumerate(messages):
        if isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in message.parts
        ):
            start = i
    failures: dict[str, str] = {}
    for message in messages[start:]:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            tool = getattr(part, "tool_name", None)
            if not tool or part.part_kind not in ("tool-return", "retry-prompt"):
                continue
            # A failed call is a return part with another outcome; before pydantic-ai 2.65, a retry prompt.
            if (
                part.part_kind == "retry-prompt"
                or getattr(part, "outcome", "success") != "success"
            ):
                text = (
                    part.content
                    if isinstance(part.content, str)
                    else "its arguments were not valid"
                )
                failures[tool] = f"Failed: {short_reason(text)}"
            elif (reason := refusal(part.content)) is not None:
                failures[tool] = reason
            else:
                failures.pop(tool, None)
    return list(failures.items())


def failure_instructions(messages: t.Sequence[t.Any]) -> str:
    """Return the lines telling the model which steps of this request it has not got done, or ``""``."""
    failures = unresolved_failures(messages)
    if not failures:
        return ""
    steps = "; ".join(f"{name} ({reason})" for name, reason in failures)
    return (
        f"These steps of this request were refused or failed and have not succeeded since: {steps}.\n"
        "Unless you redo them, name them in your answer; do not say everything was done."
    )
