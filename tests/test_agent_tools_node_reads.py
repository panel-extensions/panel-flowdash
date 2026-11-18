"""A tool reading an output that is not available reports it instead of ending the run."""

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("panel_agents")

from panel_agents import RunState
from panel_agents.adapters.pydantic_ai import AgentRunner
from pydantic_ai import Agent, FunctionToolset, RunContext

from panel_flowdash import FlowDash
from panel_flowdash.agent import DashboardOps, FlowDashDeps
from panel_flowdash.agent.tools import _guarded

from .test_agent_ops_node_reads import frame, needs
from .test_agent_tools import COMPONENTS, scripted
from .test_editor import FAILING_COMPONENTS


def read_output(ctx: RunContext[FlowDashDeps], ref: str) -> str:
    """Read an output."""
    return repr(ctx.deps.ops.node_output(ref))


def _run(ops, *calls):
    toolset = FunctionToolset[FlowDashDeps]()
    toolset.add_function(_guarded(read_output), takes_ctx=True, name="read_output")
    agent = Agent(scripted(*calls), deps_type=FlowDashDeps, toolsets=[toolset])
    return AgentRunner(agent, RunState(flush_interval=0), deps=FlowDashDeps(ops))


async def test_waiting_output_is_reported_to_the_model():
    editor = FlowDash(
        components={**COMPONENTS, "T/frame": frame, "T/needs": needs},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Mine")
    ops = DashboardOps(editor)
    ops.add_node("T/needs", name="waits")
    runner = _run(ops, ("read_output", {"ref": "waits.out"}))
    output = await runner.run("Read it")
    assert runner.error is None
    assert output == "Done: Not available: 'waits' is waiting: Waiting for x."
    editor.dispose()


@pytest.mark.failures
async def test_failed_output_is_reported_to_the_model():
    editor = FlowDash(
        FAILING_COMPONENTS,
        notifications=False,
        propagate_failures=True,
        include_builtin_components=False,
    )
    ops = DashboardOps(editor)
    ops.add_node("Demo/inverse", name="src")
    editor.graph.get_state(editor.export_dashboard()["items"][0]["instance_id"]).value = 0
    runner = _run(ops, ("read_output", {"ref": "src.inverse"}))
    output = await runner.run("Read it")
    assert runner.error is None
    assert output.startswith("Done: Not available: 'src' has failed:")
    editor.dispose()
