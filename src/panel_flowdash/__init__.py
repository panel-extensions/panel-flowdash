"""panel-flowdash: Dataflow and draggable grid based dashboard editor for Panel."""

import importlib.metadata
import warnings

from panel_flowdash.component_spec import (
    ComponentSpec,
    InputPort,
    OutputPort,
    build_component_spec,
    build_component_specs,
)
from panel_flowdash.dashboard_store import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    DashboardStore,
)
from panel_flowdash.dataflow_engine import DataflowGraph, build_node_state_class
from panel_flowdash.registry import PanelAppMetadata, RegistryEntry, panel_app, register
from panel_flowdash.session_state import build_session_state_class, check_requirements

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError as e:  # pragma: no cover
    warnings.warn(f"Could not determine version of {__name__}\n{e!s}", stacklevel=2)
    __version__ = "unknown"

__all__: list[str] = [
    "ComponentSpec",
    "DashboardEdge",
    "DashboardItem",
    "DashboardModel",
    "DashboardStore",
    "DataflowGraph",
    "InputPort",
    "OutputPort",
    "PanelAppMetadata",
    "RegistryEntry",
    "__version__",
    "build_component_spec",
    "build_component_specs",
    "build_node_state_class",
    "build_session_state_class",
    "check_requirements",
    "panel_app",
    "register",
]
