# Wire the Dataflow

The `DataflowGraph` engine manages node instances and edge connections. It
validates connections at wire-time (types, cycles, single-source) and catches
runtime errors via `param.watch`.

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

Removing a node clears all connected edges automatically.

---

## Query state

```python
state = graph.get_state("filter_1")
current_value = state.filtered

all_edges = graph.edges
all_node_ids = graph.node_ids
```
