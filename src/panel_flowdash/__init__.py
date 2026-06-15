"""panel-flowdash: Dataflow and draggable grid based dashboard editor for Panel."""

import importlib.metadata
import warnings

from panel_flowdash.component_spec import ComponentSpec
from panel_flowdash.component_spec import InputPort
from panel_flowdash.component_spec import OutputPort
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.component_spec import build_component_specs
from panel_flowdash.dashboard_store import DashboardEdge
from panel_flowdash.dashboard_store import DashboardItem
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.dataflow_engine import DataflowGraph
from panel_flowdash.dataflow_engine import build_node_state_class
from panel_flowdash.registry import PanelAppMetadata
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.registry import panel_app
from panel_flowdash.registry import register
from panel_flowdash.session_state import build_session_state_class
from panel_flowdash.session_state import check_requirements

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError as e:  # pragma: no cover
    warnings.warn(f"Could not determine version of {__name__}\n{e!s}", stacklevel=2)
    __version__ = "unknown"

__all__: list[str] = [
    "__version__",
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
    "build_component_spec",
    "build_component_specs",
    "build_node_state_class",
    "build_session_state_class",
    "check_requirements",
    "panel_app",
    "register",
]
