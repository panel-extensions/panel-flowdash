# Multi-step Pipeline

This example builds a three-node pipeline: a data source, a transformer, and
a display. It demonstrates chained propagation and runtime error handling.

---

## Components

The three modules live in a `Pipeline` section of a project directory:

```
my_project/
    Pipeline/
        __init__.py
        source.py
        transform.py
        display.py
```

### Data Source

Produces the list of numbers from 1 to the chosen count:

```python title="Pipeline/source.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Number Source",
    provides=[{"key": "numbers", "type": "list"}],
)
def app(config):
    import panel as pn
    import panel_material_ui as pmui

    count = pmui.IntInput(label="Count", value=5, start=1, end=100)
    config.numbers = pn.bind(lambda n: list(range(1, n + 1)), count)
    return count
```

Assigning a bound function to the output keeps it up to date: every time the
count changes, the list is recomputed and published.

### Transformer

Receives a list, squares every number and publishes the result:

```python title="Pipeline/transform.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Squarer",
    requires=[{"key": "numbers", "type": "list", "multiple": False}],
    provides=[{"key": "result", "type": "list"}],
)
def app(config):
    import panel as pn

    def square(numbers):
        return [x**2 for x in numbers or []]

    config.result = pn.bind(square, config.param.numbers)
    return pn.pane.Str(config.param.result)
```

An input typed `list` normally collects the values of every edge connected to
it into one list, which suits a summary fed by several sources. Here each list
arrives over a single edge and is used as it is, so the inputs set
`"multiple": False`.

### Display

Shows the sum of the result:

```python title="Pipeline/display.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Result Display",
    requires=[{"key": "result", "type": "list", "multiple": False}],
)
def app(config):
    import panel as pn

    def total(result):
        return f"**Sum:** {sum(result or [])}"

    return pn.pane.Markdown(pn.bind(total, config.param.result))
```

A reduction this simple can also happen on the edge itself, with an
*Aggregate* step, see [Transform Edges](../how-to/transform-edges.md).

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
        inputs=[InputPort(name="numbers", type="list", multiple=False)],
        default_size=None,
    ),
    "display": ComponentSpec(
        component_id="display",
        title="Display",
        description=None, icon=None, tags=[],
        outputs=[],
        inputs=[InputPort(name="result", type="list", multiple=False)],
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

# The value arrives on the transformer's input:
tfm_state = graph.get_state("tfm_1")
assert tfm_state.numbers == [1, 2, 3, 4, 5]
```

---

## Error handling

Invalid connections are refused when they are wired, and `add_edge` returns
the reason instead of `True`:

```python
result = graph.add_edge("src_1", "numbers", "dsp_1", "result")
assert result == "Input 'result' already has a connection. Disconnect it first."
```

Errors raised while a value propagates, such as an input rejecting it, are
passed to the `on_error` callback instead of raising, so one failing edge does
not stop the others:

```python
assert errors == []  # nothing has failed so far
```

---

## Serving the pipeline

```bash
flowdash serve my_project/ --title "Data Pipeline"
```
