"""The default FlowDash agent: instructions built from the project's components.

Needs the ``panel-flowdash[agent]`` extra.
"""

from __future__ import annotations

import os
import typing as t

from pydantic_ai import Agent, RunContext

from .ops import DashboardOps
from .tools import FlowDashDeps, dashboard_toolset

# Names the default agent's model, e.g. ``openai:gpt-5``.
MODEL_ENV = "FLOWDASH_AGENT_MODEL"

INSTRUCTIONS = """\
You help people build and change FlowDash dashboards in the app they are using.

A dashboard is a dataflow: nodes place components, edges carry values from an
output port of one node to an input port of another, optionally through
transform steps, and the layout arranges the nodes' tiles. You read and write
dashboards in the FlowDash YAML format (`flowdash: 1`): `nodes` keyed by name,
each with a `component` id, optional `config` and `position`; `edges` with
`from: <node>.<port>` and `to: <node>.<port>` and optional `transform` steps;
`layout.tiles` listing `node`, `width` (percent of the row) and `height`
(pixels) in display order; and dashboard `settings`.

How to work:
- Start with get_dashboard to see the open dashboard, and list_components to
  find components; use only component ids, ports and config fields that exist.
- Make small changes with add_node, connect, set_config, place_tile and the
  other operations. For larger rewrites write the whole YAML, check it with
  validate_dashboard and load it with replace_dashboard.
- Connect an output only to an input of a compatible type; each input takes
  one edge unless it accepts a list.
- After changing the dataflow, run preview_dashboard and fix nodes that failed
  or still wait for input.
- Your edits show on the user's screen as unsaved changes. Save only when the
  user asks to; saving, deleting and sharing wait for the user's approval.
- list_commands and run_command do what the command palette offers, e.g.
  opening a dashboard, refreshing or switching between view and edit mode.
- If a tool refuses because the user lacks the rights, say so; do not try to
  work around it.
"""


def default_model() -> str | None:
    """Return the model the default agent uses, from ``FLOWDASH_AGENT_MODEL``, or None."""
    return os.environ.get(MODEL_ENV) or None


def _signature(component: dict[str, t.Any]) -> str:
    def ports(entries):
        return ", ".join(
            f"{p['name']}: {p['type']}" + ("" if p.get("required", True) else "?") for p in entries
        )

    fields = component["config"]["properties"]
    config = ", ".join(
        f"{name}={schema['default']!r}" if "default" in schema else name
        for name, schema in fields.items()
    )
    line = f"- {component['component']} ({component['title']})"
    if component["control"]:
        line += " [control]"
    if component["description"]:
        line += f": {' '.join(component['description'].split())}"
    details = []
    if component["inputs"]:
        details.append(f"in({ports(component['inputs'])})")
    if component["outputs"]:
        details.append(f"out({ports(component['outputs'])})")
    if config:
        details.append(f"config({config})")
    return line + ("\n  " + " ".join(details) if details else "")


def catalogue(ops: DashboardOps) -> str:
    """Describe the components the user may place, one or two lines each.

    Inputs marked ``?`` are optional; ``[control]`` components are shown in the
    controls bar rather than as tiles.
    """
    lines = [_signature(c) for c in ops.list_components()]
    return "Components you may place:\n" + "\n".join(lines)


def create_agent(
    model: t.Any = None,
    *,
    instructions: str | None = None,
    **kwargs: t.Any,
) -> Agent[FlowDashDeps, str]:
    """Create the default FlowDash agent.

    It has the dashboard tools, and its instructions describe the YAML format,
    how to work on a dashboard and, at every run, the components the user may
    place. Run it with ``deps=FlowDashDeps(ops)``.

    Parameters
    ----------
    model
        A pydantic-ai model or model name, e.g. ``"openai:gpt-5"``. Defaults
        to ``FLOWDASH_AGENT_MODEL``.
    instructions
        Extra instructions appended to the defaults, e.g. about the project's data.
    **kwargs
        Passed on to ``pydantic_ai.Agent``.
    """
    model = model if model is not None else default_model()
    if model is None:
        raise ValueError(
            f"No model for the FlowDash agent: pass one, or set {MODEL_ENV}, "
            "e.g. to 'openai:gpt-5'."
        )
    agent = Agent(
        model,
        deps_type=FlowDashDeps,
        name="FlowDash",
        instructions=INSTRUCTIONS + (f"\n{instructions}" if instructions else ""),
        toolsets=[dashboard_toolset(), *kwargs.pop("toolsets", [])],
        **kwargs,
    )

    @agent.instructions
    def components(ctx: RunContext[FlowDashDeps]) -> str:
        return catalogue(ctx.deps.ops)

    return agent
