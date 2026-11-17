"""Reading, checking and editing dashboards as YAML, for people and AI agents.

The YAML format, its JSON Schema and validation need nothing beyond
panel-flowdash itself. See the dashboard YAML format reference for the
specification.
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

__all__ = [
    "FORMAT_VERSION",
    "SCHEMA_ID",
    "DashboardOps",
    "DashboardYAMLError",
    "Issue",
    "NotFound",
    "OperationError",
    "component_schema",
    "dashboard_from_yaml",
    "dashboard_schema",
    "dashboard_to_yaml",
    "read_revision",
    "validate_dashboard",
]
