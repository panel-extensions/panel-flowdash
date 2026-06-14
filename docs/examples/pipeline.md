# Multi-step Pipeline

This example builds a three-node pipeline: a data source, a transformer, and
a display. It demonstrates chained propagation and runtime error handling.

---

## Components

### Data Source

Produces a list of numbers:

```python title="Pipeline/source.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Number Source",
    provides=[{"key": "numbers", "type": "list"}],
)
def app(config):
    import panel_material_ui as pmui
    return pmui.IntInput(name="Count", value=5, start=1, end=100)
```

### Transformer

Receives a list and applies a transformation:

```python title="Pipeline/transform.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Squarer",
    requires=[{"key": "numbers", "type": "list"}],
    provides=[{"key": "result", "type": "list"}],
)
def app(config):
    import panel as pn
    numbers = config.get("numbers", [])
    squared = [x**2 for x in numbers]
    return pn.pane.Str(str(squared))
```

### Display

Shows the final result:

```python title="Pipeline/display.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Result Display",
    requires=[{"key": "result", "type": "list"}],
)
def app(config):
    import panel as pn
    result = config.get("result", [])
    return pn.pane.Markdown(f"**Sum:** {sum(result)}")
```

---

## Wiring with the DataflowGraph

```python
from panel_flowdash import DataflowGraph, ComponentSpec, OutputPort, InputPort

specs = {
    "source": ComponentSpec(
        component_id="source",
        title="Source",
        description=None, icon=None, tags=[],
        outputs=[OutputPort(name="numbers", type="list")],
        inputs=[],
        default_size=None,
    ),
    "transform": ComponentSpec(
        component_id="transform",
        title="Transform",
        description=None, icon=None, tags=[],
        outputs=[OutputPort(name="result", type="list")],
        inputs=[InputPort(name="numbers", type="list")],
        default_size=None,
    ),
    "display": ComponentSpec(
        component_id="display",
        title="Display",
        description=None, icon=None, tags=[],
        outputs=[],
        inputs=[InputPort(name="result", type="list")],
        default_size=None,
    ),
}

errors = []
graph = DataflowGraph(specs, on_error=lambda *args: errors.append(args))

graph.add_node("src_1", "source")
graph.add_node("tfm_1", "transform")
graph.add_node("dsp_1", "display")

# Wire: source -> transform -> display
assert graph.add_edge("src_1", "numbers", "tfm_1", "numbers") is True
assert graph.add_edge("tfm_1", "result", "dsp_1", "result") is True

# Propagate values through the chain
src_state = graph.get_state("src_1")
src_state.numbers = [1, 2, 3, 4, 5]

# After propagation:
tfm_state = graph.get_state("tfm_1")
assert tfm_state.numbers == [1, 2, 3, 4, 5]
```

---

## Error handling

If a type mismatch slips past compile-time checking (e.g. untyped ports), the
runtime validation catches it:

```python
# Attempting to connect incompatible typed ports fails at wire time:
result = graph.add_edge("src_1", "numbers", "dsp_1", "result")
# Returns error: "Input 'result' already has a connection..."

# Runtime errors on value propagation are caught by on_error:
assert len(errors) == 0  # no errors if types are compatible
```

---

## Serving the pipeline

```bash
flowdash serve Pipeline/ --title "Data Pipeline"
```
