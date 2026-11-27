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
    NotApplied,
    NotFound,
    OperationError,
    OutputUnavailable,
    read_revision,
)
from panel_flowdash.agent.schema import SCHEMA_ID, component_schema, dashboard_schema
from panel_flowdash.agent.starters import StarterContext, default_starter_questions
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
    "LAYOUT_TOOLS": "tools",
    "MCPApprovalRule": "mcp",
    "REPORT_TOOLS": "tools",
    "ResilientMCPToolset": "mcp",
    "ToolAuthorizeCapability": "mcp",
    "approval_policy": "tools",
    "build_mcp_toolset": "mcp",
    "build_mcp_toolsets": "mcp",
    "create_agent": "assistant",
    "create_explore_agent": "assistant",
    "dashboard_toolset": "tools",
    "mcp_approval_rules": "mcp",
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
    "LAYOUT_TOOLS",
    "REPORT_TOOLS",
    "SCHEMA_ID",
    "DashboardOps",
    "DashboardYAMLError",
    "ExplorationOps",
    "FlowDashDeps",
    "Issue",
    "MCPApprovalRule",
    "NotApplied",
    "NotFound",
    "OperationError",
    "OutputUnavailable",
    "ResilientMCPToolset",
    "StarterContext",
    "ToolAuthorizeCapability",
    "approval_policy",
    "build_mcp_toolset",
    "build_mcp_toolsets",
    "component_schema",
    "create_agent",
    "create_explore_agent",
    "dashboard_from_yaml",
    "dashboard_schema",
    "dashboard_to_yaml",
    "dashboard_toolset",
    "default_starter_questions",
    "mcp_approval_rules",
    "palette_actions",
    "read_revision",
    "validate_dashboard",
]
