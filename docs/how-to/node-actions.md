# Add Node Actions and Provenance Labels

A component library can add entries to a node's ⋮ menu and say where a
node's numbers come from. The first is a **node action**: an entry in the
menu of the sidecar's header (the node menu once a node is selected on the
canvas) and of the node's tile. The second is a **provenance label**: a chip
such as "Metric: capacity" or "Ad hoc" on every view that reads the node.

---

## 1. Register a node action

A `NodeAction` names its menu entry, says which nodes it `applies` to and
what to `run`. Both get a `NodeActionContext` for the node it was picked on.

```python
from panel_flowdash import NodeAction, register_node_action


def is_query(ctx):
    return ctx.component() == "Shop/query"


def save_as_report(ctx):
    sql = ctx.config()["sql"]
    rows = ctx.output("data")
    ctx.notify("success", f"Saved {len(rows)} rows of {ctx.node}.")


register_node_action(NodeAction(
    "shop.save_as_report", "Save as report…", run=save_as_report, applies=is_query,
    icon="save", admin_only=True,
))
```

Register actions when your library is imported, e.g. in the module that
holds its components. `applies` runs whenever a menu is built, so keep it
cheap: read the node's component, config and edges, not its data.

### Who sees it, and when

- An action is offered in edit mode only, while the dashboard may be
  edited. Pass `modes={"edit", "view"}` for one that changes nothing and
  also suits view mode, or `modes={"view"}`. Outside its modes it is
  neither shown nor run.
- `admin_only=True` offers it only to admins of the project, as the
  editor's `authorize_admin` says. `FlowDashApp` supplies one; an embedded
  `FlowDash` without it has no admins. Before the action runs, the editor
  asks again with the project file read afresh, so someone removed as an
  admin meanwhile is refused.
- Restricted nodes and placeholders get no actions.

## 2. Read and change the dashboard from the action

The context reads the node and its neighbours by their names in the
dashboard YAML: `component()`, `config()`, `output(port)`,
`input_value(port)`, `inputs()` and `outputs()` (edges as `{"from", "to",
"transform"}`), `tile()` and `nodes()`; each takes another node's name too.
It changes the dashboard through `ctx.ops`, a `NodeActionOps` with the
[`DashboardOps`](../reference/api/agent.md) methods that read the open
dashboard and change its nodes, edges, tiles, groups, filters and
parameters, with the rights of the person using the editor. It leaves out
saving, opening, renaming or replacing dashboards, their settings and
report, so an action stays an edit of the open dashboard the person can
undo; one that needs more offers its own UI for it.

To make several changes one step that one undo reverts, wrap them in
`ctx.edit()`. If the block raises, the dashboard goes back to how it was
before the block, also in an editor whose edit history is off, and the
failed changes never reach undo or redo.
`ctx.replace_node(name, component, config, rewire=...)` replaces a node by
another one wired to everything it fed, in its tile slot, as one step:

```python
def use_cached_query(ctx):
    def rewire(edge):
        port = edge["from"].rsplit(".", 1)[1]
        return ("rows" if port == "data" else port), edge["transform"]

    ctx.replace_node(ctx.node, "Shop/cached_query", {"key": ctx.node}, rewire=rewire)
```

`ctx.project` is the app's project file, to save a section with
`Project.set` (see [Add a Project File Section](project-sections.md)),
`ctx.context` the resources the host gave the editor, and `ctx.notify`,
`ctx.open_dialog` and `ctx.close_dialog` talk to the person.

## 3. Report provenance

A component says where its numbers come from with
`__flowdash_provenance__`, returning a `Provenance` or None:

```python
import param
from panel.viewable import Viewer

from panel_flowdash import Provenance


class Metric(Viewer):
    name_ = param.String()

    def __flowdash_provenance__(self):
        if not self.name_:
            return None
        return Provenance(f"Metric: {self.name_}", "Revenue as finance defines it.", kind="metric")
```

A view shows the label of the nearest node up its inputs that reports one:
as a chip on its tile and above it in the top bar or sidebar while the
dashboard is edited, under the sidecar's header, and first in "How was this
made?". The `detail` shows on hover. View mode and preview show no chip
unless the editor's `provenance_labels` is on. A node that returns None
passes the question on to its own inputs, so return None where your label
would be noise, e.g. on rows a chart reads rather than a single number.

### Labels through edges

An edge can change the numbers on the way: its transform may pick a
column or aggregate rows. A hook that takes a `via` argument is told the
edges between its node and the view asking, nearest its node first, each
`{"from", "to", "transform"}` as `ctx.inputs()` gives them (empty when the
node reports on itself). Use it so a view does not claim a named number it
does not show:

```python
class Revenue(Metric):
    def __flowdash_provenance__(self, via=()):
        steps = [step for edge in via for step in edge["transform"]]
        aggregate = next((s for s in steps if s.get("op") == "aggregate"), None)
        if aggregate is not None and aggregate.get("how") != "last":
            return Provenance(
                "Derived", f"The {aggregate['how']} of revenue's rows.", kind="derived"
            )
        return Provenance("Metric: revenue", "Revenue as finance defines it.", kind="metric")
```

The walk itself does not stop at a transform, since only your component
knows whether its rows survive one unchanged.
