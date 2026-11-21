"""Reading, checking and editing dashboards as YAML, for people and AI agents.

The YAML format, its JSON Schema, validation and the operations API need
nothing beyond panel-flowdash itself. See the dashboard YAML format reference
for the specification. `palette_actions` builds on panel-agents, which
FlowDash always installs. The agent itself (`create_agent`, `FlowDashDeps`,
`dashboard_toolset`, `approval_policy`) runs on pydantic-ai, so it needs the
``panel-flowdash[agent]`` extra; all of these are imported on first use.
"""

from panel_flowdash.agent.ops import (
    DashboardOps,
    NotFound,
    OperationError,
    OutputUnavailable,
    read_revision,
)
from panel_flowdash.agent.schema import SCHEMA_ID, component_schema, dashboard_schema
from panel_flowdash.agent.validation import Issue, validate_dashboard
from panel_flowdash.agent.yaml_format import (
    FORMAT_VERSION,
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_yaml,
)

# The names that need the agent extra, by the module that defines them.
_AGENT_NAMES = {
    "ExplorationOps": "exploration",
    "FlowDashDeps": "tools",
    "approval_policy": "tools",
    "create_agent": "assistant",
    "create_explore_agent": "assistant",
    "dashboard_toolset": "tools",
}


def __getattr__(name):
    import importlib

    if name == "palette_actions":
        return importlib.import_module(f"{__name__}.actions").palette_actions
    module = _AGENT_NAMES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        return getattr(importlib.import_module(f"{__name__}.{module}"), name)
    except ImportError as exc:
        raise ImportError(
            f"panel_flowdash.agent.{name} needs the agent extra: "
            "pip install 'panel-flowdash[agent]'"
        ) from exc


__all__ = [
    "FORMAT_VERSION",
    "SCHEMA_ID",
    "DashboardOps",
    "DashboardYAMLError",
    "ExplorationOps",
    "FlowDashDeps",
    "Issue",
    "NotFound",
    "OperationError",
    "OutputUnavailable",
    "approval_policy",
    "component_schema",
    "create_agent",
    "create_explore_agent",
    "dashboard_from_yaml",
    "dashboard_schema",
    "dashboard_to_yaml",
    "dashboard_toolset",
    "palette_actions",
    "read_revision",
    "validate_dashboard",
]
