"""Reading, checking and editing dashboards as YAML, for people and AI agents.

The YAML format, its JSON Schema, validation and the operations API need
nothing beyond panel-flowdash itself. See the dashboard YAML format reference
for the specification. The agent itself (`create_agent`, `FlowDashDeps`,
`dashboard_toolset`, `palette_actions`) needs the ``panel-flowdash[agent]``
extra and is imported on first use.
"""

from panel_flowdash.agent.ops import DashboardOps, NotFound, OperationError, read_revision
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
    "FlowDashDeps": "tools",
    "approval_policy": "tools",
    "create_agent": "assistant",
    "dashboard_toolset": "tools",
    "palette_actions": "actions",
}


def __getattr__(name):
    module = _AGENT_NAMES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


__all__ = [
    "FORMAT_VERSION",
    "SCHEMA_ID",
    "DashboardOps",
    "DashboardYAMLError",
    "FlowDashDeps",
    "Issue",
    "NotFound",
    "OperationError",
    "approval_policy",
    "component_schema",
    "create_agent",
    "dashboard_from_yaml",
    "dashboard_schema",
    "dashboard_to_yaml",
    "dashboard_toolset",
    "palette_actions",
    "read_revision",
    "validate_dashboard",
]
