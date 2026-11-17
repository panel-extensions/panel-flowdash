"""The dashboard operations as pydantic-ai tools, publishing each edit as a document artifact.

Needs the ``panel-flowdash[agent]`` extra (panel-agents and pydantic-ai).
"""

from __future__ import annotations

import functools
import inspect
import json
import typing as t
from dataclasses import dataclass

import yaml
from panel_agents.adapters.pydantic_ai import artifact_return
from panel_agents.artifacts import Change, StructuredDiff, register_diff
from panel_agents.policy import ApprovalPolicy
from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from .diff import dashboard_changes
from .live import LiveDashboardOps
from .ops import DashboardOps, OperationError

# The artifact kind the dashboard YAML is published under, with its own diff.
ARTIFACT_KIND = "flowdash-dashboard"

# Tools that change what is stored or who can see it: the user approves each call.
APPROVAL_TOOLS = ("save_dashboard", "delete_dashboard", "share_dashboard")


@dataclass
class FlowDashDeps:
    """What the dashboard tools run against: the operations of one user's session."""

    ops: DashboardOps


def approval_policy() -> ApprovalPolicy:
    """Return the policy asking the user before every save, delete and sharing change."""
    return ApprovalPolicy(list(APPROVAL_TOOLS))


@register_diff(ARTIFACT_KIND)
def dashboard_diff(old: t.Any, new: t.Any, **options) -> StructuredDiff:
    """Describe the change between two dashboard versions as nodes, config, edges and tiles."""
    changes = [
        Change(op=c.op, path=c.path, old=c.old, new=c.new, label=c.label)
        for c in dashboard_changes(old, new)
    ]
    return StructuredDiff(
        kind=ARTIFACT_KIND,
        identical=not changes,
        changes=changes,
        old_text=old if isinstance(old, str) else yaml.safe_dump(old or {}, sort_keys=False),
        new_text=new if isinstance(new, str) else yaml.safe_dump(new or {}, sort_keys=False),
    )


def _artifact(ops: DashboardOps, message: str):
    """Return *message* to the model and the dashboard as it now is to the user."""
    editor = ops.editor
    current = editor.dashboard
    artifact_id = f"dashboard:{current.dashboard_id}" if current else "dashboard:unsaved"
    title = current.title if current else "Dashboard"
    return artifact_return(
        message,
        artifact_id,
        ops.get_dashboard(),
        title=title,
        kind=ARTIFACT_KIND,
        applied=True,
    )


def _guarded(func: t.Callable) -> t.Callable:
    """Turn refusals into messages: a fixable request is retried, a forbidden one is reported."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except (OperationError, KeyError) as exc:
            raise ModelRetry(str(exc).strip("'\"")) from exc
        except PermissionError as exc:
            return f"Refused: {exc}"

    return wrapper


def _text(value: t.Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, indent=1)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def list_components(ctx: RunContext[FlowDashDeps], query: str = "") -> str:
    """List the components you may place, with their inputs, outputs and config fields.

    Args:
        query: Only list components matching all of these words, e.g. "map" or "table".
    """
    return _text(ctx.deps.ops.list_components(query))


def list_dashboards(ctx: RunContext[FlowDashDeps]) -> str:
    """List the stored dashboards the user may open, with their ids."""
    return _text(ctx.deps.ops.list_dashboards())


def get_dashboard(ctx: RunContext[FlowDashDeps], dashboard_id: str | None = None) -> str:
    """Return a dashboard as YAML. Without an id, the open dashboard with its unsaved changes.

    Args:
        dashboard_id: The id of a stored dashboard, from list_dashboards.
    """
    return ctx.deps.ops.get_dashboard(dashboard_id)


async def open_dashboard(ctx: RunContext[FlowDashDeps], dashboard_id: str) -> str:
    """Open a stored dashboard for the user and return it as YAML.

    Args:
        dashboard_id: The id of a stored dashboard, from list_dashboards.
    """
    ops = ctx.deps.ops
    if isinstance(ops, LiveDashboardOps):
        return await ops.open_dashboard_async(dashboard_id)
    return ops.open_dashboard(dashboard_id)


def validate_dashboard(ctx: RunContext[FlowDashDeps], dashboard_yaml: str | None = None) -> str:
    """Check dashboard YAML, or the open dashboard, and list its problems.

    Args:
        dashboard_yaml: A whole dashboard in the YAML format.
    """
    issues = ctx.deps.ops.validate(dashboard_yaml)
    return "\n".join(f"{i.severity}: {i}" for i in issues) or "No problems found."


def preview_dashboard(ctx: RunContext[FlowDashDeps], dashboard_yaml: str | None = None) -> str:
    """Run a dashboard without showing it and report each node's status and output samples.

    Args:
        dashboard_yaml: A whole dashboard to try out, leaving the open one unchanged.
            Without it, the open dashboard is previewed.
    """
    return _text(ctx.deps.ops.preview(dashboard_yaml))


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------


def add_node(
    ctx: RunContext[FlowDashDeps],
    component: str,
    config: dict[str, t.Any] | None = None,
) -> t.Any:
    """Place a component on the open dashboard and return the name of its node.

    Args:
        component: A component id from list_components, e.g. "Views/kpi".
        config: Values for some of its config fields.
    """
    ops = ctx.deps.ops
    name = ops.add_node(component, config)
    return _artifact(ops, f"Added node '{name}'.")


def remove_node(ctx: RunContext[FlowDashDeps], name: str) -> t.Any:
    """Remove a node, with its edges and tile, from the open dashboard.

    Args:
        name: The node's name in the dashboard YAML.
    """
    ops = ctx.deps.ops
    ops.remove_node(name)
    return _artifact(ops, f"Removed '{name}'.")


def set_config(ctx: RunContext[FlowDashDeps], name: str, config: dict[str, t.Any]) -> t.Any:
    """Change some config fields of a node.

    Args:
        name: The node's name in the dashboard YAML.
        config: The fields to change and their new values.
    """
    ops = ctx.deps.ops
    ops.set_config(name, config)
    return _artifact(ops, f"Updated the config of '{name}'.")


def connect(
    ctx: RunContext[FlowDashDeps],
    source: str,
    target: str,
    transform: list[dict[str, t.Any]] | None = None,
) -> t.Any:
    """Connect an output to an input.

    Args:
        source: The output, as "<node>.<port>".
        target: The input, as "<node>.<port>".
        transform: Steps applied along the edge, e.g. [{"op": "head", "n": 10}].
    """
    ops = ctx.deps.ops
    ops.connect(source, target, transform)
    return _artifact(ops, f"Connected {source} to {target}.")


def disconnect(ctx: RunContext[FlowDashDeps], source: str, target: str) -> t.Any:
    """Remove the edge between an output and an input.

    Args:
        source: The output, as "<node>.<port>".
        target: The input, as "<node>.<port>".
    """
    ops = ctx.deps.ops
    ops.disconnect(source, target)
    return _artifact(ops, f"Disconnected {source} from {target}.")


def set_transform(
    ctx: RunContext[FlowDashDeps],
    source: str,
    target: str,
    transform: list[dict[str, t.Any]],
) -> t.Any:
    """Replace the transform steps of an edge; an empty list removes them.

    Args:
        source: The edge's output, as "<node>.<port>".
        target: The edge's input, as "<node>.<port>".
        transform: The steps, each a mapping with an "op" and its arguments.
    """
    ops = ctx.deps.ops
    ops.set_transform(source, target, transform)
    return _artifact(ops, f"Set the transform of {source} → {target}.")


def place_tile(
    ctx: RunContext[FlowDashDeps],
    name: str,
    width: float | None = None,
    height: float | None = None,
    visible: bool | None = None,
    index: int | None = None,
) -> t.Any:
    """Change how a node's tile is laid out on the dashboard.

    Args:
        name: The node's name in the dashboard YAML.
        width: Percent of the row, greater than 0 and at most 100.
        height: Height in pixels.
        visible: False hides the tile; the component keeps running.
        index: Position among the tiles, 0 for the first.
    """
    ops = ctx.deps.ops
    ops.place_tile(name, width=width, height=height, visible=visible, index=index)
    return _artifact(ops, f"Laid out the tile of '{name}'.")


def set_settings(ctx: RunContext[FlowDashDeps], settings: dict[str, t.Any]) -> t.Any:
    """Change some dashboard settings, e.g. {"description": "...", "refresh_period": 60}.

    Args:
        settings: The settings to change and their new values.
    """
    ops = ctx.deps.ops
    ops.set_settings(settings)
    return _artifact(ops, "Updated the settings.")


def replace_dashboard(ctx: RunContext[FlowDashDeps], dashboard_yaml: str) -> t.Any:
    """Replace the whole open dashboard with YAML; use it for large changes.

    Args:
        dashboard_yaml: The whole dashboard in the YAML format, as get_dashboard returns it.
    """
    ops = ctx.deps.ops
    warnings = ops.replace_yaml(dashboard_yaml)
    message = "Replaced the dashboard."
    if warnings:
        message += " Warnings:\n" + "\n".join(f"- {w}" for w in warnings)
    return _artifact(ops, message)


def save_dashboard(ctx: RunContext[FlowDashDeps]) -> str:
    """Save the open dashboard. The user approves each save."""
    return f"Saved dashboard '{ctx.deps.ops.save()}'."


def delete_dashboard(ctx: RunContext[FlowDashDeps], dashboard_id: str) -> str:
    """Delete a stored dashboard. The user approves each deletion.

    Args:
        dashboard_id: The id of the dashboard, from list_dashboards.
    """
    ops = ctx.deps.ops
    if not isinstance(ops, LiveDashboardOps):
        return "Refused: dashboards can only be deleted from the app."
    ops.delete_dashboard(dashboard_id)
    return f"Deleted dashboard '{dashboard_id}'."


def share_dashboard(
    ctx: RunContext[FlowDashDeps],
    dashboard_id: str,
    view_users: list[str] | None = None,
    view_groups: list[str] | None = None,
    edit_users: list[str] | None = None,
    edit_groups: list[str] | None = None,
) -> str:
    """Replace who a dashboard is shared with. The user approves each change.

    Args:
        dashboard_id: The id of the dashboard, from list_dashboards.
        view_users: Users who may view it.
        view_groups: Groups whose members may view it.
        edit_users: Users who may edit it.
        edit_groups: Groups whose members may edit it.
    """
    ops = ctx.deps.ops
    if not isinstance(ops, LiveDashboardOps):
        return "Refused: sharing can only be changed from the app."
    ops.share_dashboard(
        dashboard_id,
        view_users=view_users or (),
        view_groups=view_groups or (),
        edit_users=edit_users or (),
        edit_groups=edit_groups or (),
    )
    return f"Updated who '{dashboard_id}' is shared with."


TOOLS = (
    list_components,
    list_dashboards,
    get_dashboard,
    open_dashboard,
    validate_dashboard,
    preview_dashboard,
    add_node,
    remove_node,
    set_config,
    connect,
    disconnect,
    set_transform,
    place_tile,
    set_settings,
    replace_dashboard,
    save_dashboard,
    delete_dashboard,
    share_dashboard,
)


def dashboard_toolset() -> FunctionToolset[FlowDashDeps]:
    """Return the dashboard tools, which run against ``FlowDashDeps.ops`` of each run's deps."""
    toolset = FunctionToolset[FlowDashDeps]()
    for func in TOOLS:
        toolset.add_function(_guarded(func), takes_ctx=True, name=func.__name__)
    return toolset
