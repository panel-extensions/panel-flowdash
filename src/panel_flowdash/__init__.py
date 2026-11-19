"""panel-flowdash: Dataflow and draggable grid based dashboard editor for Panel."""

import importlib.metadata
import warnings

from panel_flowdash.auth import (
    AuthConfig,
    Identity,
    Permission,
    can_administer,
    can_edit,
    is_authorized,
    resolve_identity,
)
from panel_flowdash.component_library import normalize_components
from panel_flowdash.component_spec import (
    ComponentSpec,
    ConfigField,
    InputPort,
    OutputPort,
    build_component_spec,
    build_component_specs,
)
from panel_flowdash.config_check import CheckWarning
from panel_flowdash.dashboard_store import (
    BaseDashboardStore,
    DashboardConflictError,
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
    DashboardStoreError,
    MemoryDashboardStore,
)
from panel_flowdash.dataflow_engine import DataflowGraph, build_node_state_class
from panel_flowdash.editor import FlowDash, current_dashboard, dashboard_context
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.parameters import (
    ContractContext,
    ContractError,
    ContractProblem,
    DashboardParameter,
    ParameterContract,
    ParameterError,
)
from panel_flowdash.port_types import (
    normalize_port_type,
    port_types_compatible,
    register_port_type,
)
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry, panel_app, register
from panel_flowdash.session_state import build_session_state_class, check_requirements
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.transforms import Transform
from panel_flowdash.turns import DashboardTurn, agent_edits

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError as e:  # pragma: no cover
    warnings.warn(f"Could not determine version of {__name__}\n{e!s}", stacklevel=2)
    __version__ = "unknown"

__all__: list[str] = [
    "AuthConfig",
    "BaseDashboardStore",
    "CheckWarning",
    "ComponentSpec",
    "ConfigField",
    "ContractContext",
    "ContractError",
    "ContractProblem",
    "DashboardConflictError",
    "DashboardEdge",
    "DashboardItem",
    "DashboardModel",
    "DashboardParameter",
    "DashboardSettings",
    "DashboardStore",
    "DashboardStoreError",
    "DashboardTurn",
    "DataflowGraph",
    "FlowDash",
    "Identity",
    "InputPort",
    "JSONFolderDashboardStore",
    "MemoryDashboardStore",
    "OutputPort",
    "PanelAppMetadata",
    "ParameterContract",
    "ParameterError",
    "Permission",
    "RegistryEntry",
    "Transform",
    "__version__",
    "agent_edits",
    "build_component_spec",
    "build_component_specs",
    "build_node_state_class",
    "build_session_state_class",
    "can_administer",
    "can_edit",
    "check_requirements",
    "current_dashboard",
    "dashboard_context",
    "is_authorized",
    "normalize_components",
    "normalize_port_type",
    "panel_app",
    "port_types_compatible",
    "register",
    "register_port_type",
    "resolve_identity",
]
