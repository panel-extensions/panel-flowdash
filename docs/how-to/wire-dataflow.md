# Wire the Dataflow

The `DataflowGraph` engine manages node instances and edge connections. It
validates connections at wire-time (types, cycles, single-source) and catches
runtime errors via `param.watch`.

In the editor you build this graph visually: each component becomes a node whose
ports are the handles on its left (inputs) and right (outputs), and dragging
between handles creates an edge. The example below wires three sidebar filters
into a data source that feeds a table, a chart, and a map.

![Components wired together on the flow canvas](../assets/images/wiring-canvas.png)

---

## Create a graph

```python
from panel_flowdash import DataflowGraph, build_component_specs

specs = build_component_specs(registry)
graph = DataflowGraph(specs)
```

The `on_error` callback is invoked whenever a runtime value propagation fails:

```python
def handle_error(source_id, source_port, target_id, target_port, exception):
    import panel as pn
    pn.state.notifications.error(
        f"Error: {source_port} -> {target_port}: {exception}"
    )

graph = DataflowGraph(specs, on_error=handle_error)
```

---

## Add nodes

Each node is an instance of a component. The instance ID is unique on the
dashboard; the component ID references the spec:

```python
graph.add_node("filter_1", "Analytics/filter")
graph.add_node("chart_1", "Analytics/chart")
```

This creates a `NodeState` parameterized instance with one param per port.

---

## Connect edges

```python
result = graph.add_edge("filter_1", "filtered", "chart_1", "data")
```

`add_edge` returns `True` on success, or an error message string on failure.
Use this for user-facing notifications:

```python
result = graph.add_edge(src_id, src_port, tgt_id, tgt_port)
if result is not True:
    pn.state.notifications.warning(result)
```

---

## Validation rules

The engine enforces these rules before creating a connection:

### Node and port existence

Both source and target nodes must exist, and the named ports must be present
on their respective node states.

### Single source per input

Each input port accepts at most one incoming connection. Attempting a second
connection returns:

> Input 'data' already has a connection. Disconnect it first.

### Cycle detection

The engine uses BFS to verify that adding the edge would not create a cycle.
Self-loops are rejected immediately.

### Type compatibility

If both the source output and target input have declared types, they must
match (case-insensitive). Untyped ports accept any connection.

---

## Runtime validation

After an edge is wired, value changes propagate via `param.watch`. If the
target raises an exception on assignment (e.g. a param validation error), the
`on_error` callback fires rather than crashing the application:

```python
errors = []

def collect_errors(*args):
    errors.append(args)

graph = DataflowGraph(specs, on_error=collect_errors)
graph.add_node("a", "comp_a")
graph.add_node("b", "comp_b")
graph.add_edge("a", "output", "b", "input")

# If setting b.input raises, on_error is called
state_a = graph.get_state("a")
state_a.output = "invalid_value"
```

### Shared values

An edge hands its target the very object the source produced, without copying
it, and every other edge from that output receives the same object. Treat the
values arriving on inputs as read-only: a component that edits a DataFrame in
place, e.g. `df["a"] = 0`, changes it for the source and all of its siblings
without any of them being notified. Copy the value first (`df = df.copy()`) if
a component needs to modify it. Transform steps never return their input or a
view of it, so the value of an edge with a transform is the target's own.

### Long chains

Values propagate synchronously, one nested call per node, so a long enough
chain of nodes would exhaust Python's recursion limit. When an update reaches
half of that limit and an event loop is running, as it is in a Panel server
or a notebook, the rest of the chain updates from the next tick of the loop.
Without an event loop the update instead stops shortly before the limit and
`on_error` receives a `RecursionError` for the input it stopped at.

### Fan-in and diamonds

A node is updated once for every path a change reaches it through. When `a`
feeds both `b` and `c`, which both feed `d`, a change to `a` updates `d` twice,
first with the new value from `b` and the old one from `c`, then with both new
values. Likewise a list input fed by N edges from one upstream node is
recomputed N times. Components that are expensive to recompute or that reject
mixed old and new inputs should be placed with this in mind.

To wire many edges at once, e.g. when restoring a saved graph, wrap the calls in
`graph.batch()`. List inputs are then bound once at the end of the block instead
of once per edge:

```python
with graph.batch():
    for source_id in sources:
        graph.add_edge(source_id, "value", "summary", "items")
```

---

## Remove edges and nodes

```python
graph.remove_edge("filter_1", "filtered", "chart_1", "data")
```

Removing an edge unsubscribes the watcher and resets the target port to its
default value.

```python
graph.remove_node("filter_1")
```

Removing a node clears all connected edges automatically and detaches its
state from everything it was bound to, so it keeps its last values but no
longer receives new ones.

---

## Query state

```python
state = graph.get_state("filter_1")
current_value = state.filtered

all_edges = graph.edges
all_node_ids = graph.node_ids
```
