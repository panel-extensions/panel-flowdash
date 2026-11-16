# Transform Edges

An edge normally hands its target exactly the value the source produced. A
transform reshapes that value on the way: it picks a column of a DataFrame,
keeps the rows that match a condition, sorts them, sums a column into a single
number or formats it as text. Each edge has its own transform, so one output can
feed a table with the full DataFrame and, over a second edge, a KPI tile with
the total of one column, without writing a component for the glue in between.

---

## Add a transform in the editor

On the wiring canvas, click an edge to select it. A **Transform** panel opens
for that edge, headed with the ports it connects (`data → total`) and a preview
of the value the source currently carries, such as
`DataFrame · 1,000 rows × 5 cols`.

- **Add step** lists the steps that accept the value at that point, so after a
  *Column* step on a DataFrame it offers the steps that work on a Series. It is
  disabled while the source has not published a value yet.
- Each step is a card with its arguments. A `column` argument is a dropdown of
  the DataFrame's columns, and a new step that takes a column starts on the
  first one.
- Below every step a preview line shows the value it produces, so you can see a
  filter emptying a table or an aggregate turning a Series into a number before
  the result reaches the target.
- The **×** on a card removes that step.

Every change is applied to the edge straight away, and the panel confirms it
with "✓ Applied to total". If the target input rejects the result, for example
because a step fails on the current value or the input is typed and the result
does not match, the panel shows why under "Not applied" and the edge keeps its
previous transform. Selecting the edge again discards the rejected draft.

A transformed edge is drawn dashed in the accent color, with a label
summarizing its steps, for example `capacity › sum`. Remove every step to turn
the edge back into a plain connection.

---

## Built-in steps

| Step | `op` | Applies to | Arguments | Produces |
|------|------|------------|-----------|----------|
| Column | `column` | DataFrame | `column`: the column to select | that column as a Series |
| Filter rows | `filter` | DataFrame | `column`, `comparison` (`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`), `value` | the matching rows |
| Sort | `sort` | DataFrame, Series, list or tuple | `column` (DataFrames only), `ascending` (default `True`) | the sorted value; a tuple becomes a list |
| First rows | `head` | DataFrame, Series, list or tuple | `n` (default 5) | the first `n` rows or items |
| Aggregate | `aggregate` | Series, list or tuple | `how`: `sum`, `mean`, `min`, `max`, `count` or `nunique` | a single Python number |
| Unique values | `unique` | Series, list or tuple | none | a list of the distinct values in order of appearance, without missing values for a Series |
| Round | `round` | a number or a Series | `digits` (default 0) | the rounded value; an `int` for a number rounded to 0 digits |
| Format | `format` | anything | `spec`, a [format spec](https://docs.python.org/3/library/string.html#formatspec) such as `,.2f` or `.0%`, and `prefix` and `suffix` strings | a string |

A filter without a `value` keeps every row, so a freshly added filter does not
empty the edge before you have typed anything. For `in`, the value is a list,
which the editor reads from comma-separated text. When the filtered column is
numeric, the editor turns what you type into a number.

Steps never hand on their input or a view of it, so the value arriving over a
transformed edge belongs to the target and editing it in place cannot change
the source (see [Shared values](wire-dataflow.md#shared-values)). An unset
value passes through every step unchanged, and so does a failure when
[failures propagate](wire-dataflow.md#carry-failures-downstream).

---

## Transforms in Python

A transform is a list of steps, each a dict with an `op` and that step's
arguments. Pass it to `connect` to wire a transformed edge, and use
`set_edge_transform` to change it later:

```python
import pandas as pd
import panel as pn
from panel_flowdash import FlowDash, register


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
def plants(config):
    config.data = pd.DataFrame(
        {"state": ["CA", "TX", "CA"], "capacity": [120.5, 80.0, 42.25]}
    )
    return pn.pane.Markdown("Power plants")


@register(component=True, page=False, requires=[{"key": "value", "label": "Value"}])
def kpi(config):
    return pn.pane.Markdown(pn.bind(lambda value: f"## {value}", config.param.value))


editor = FlowDash({"Energy/plants": plants, "Energy/kpi": kpi})
source = editor.add_component("Energy/plants")
target = editor.add_component("Energy/kpi")

total_capacity = [
    {"op": "column", "column": "capacity"},
    {"op": "aggregate", "how": "sum"},
]
assert editor.connect(source, "data", target, "value", transform=total_capacity) is True
assert editor.graph.get_state(target).value == 242.75

california = [
    {"op": "filter", "column": "state", "comparison": "==", "value": "CA"},
    {"op": "column", "column": "capacity"},
    {"op": "aggregate", "how": "sum"},
    {"op": "format", "spec": ",.1f", "suffix": " MW"},
]
assert editor.set_edge_transform(source, "data", target, "value", california) is True
assert editor.graph.get_state(target).value == "162.8 MW"
```

`editor.graph.get_edge_transform(source, "data", target, "value")` returns the
steps as stored, and an empty list passed to `set_edge_transform` removes the
transform. `DataflowGraph.add_edge` and `DataflowGraph.set_edge_transform` take
the same lists when you use the engine on its own.

Both methods return `True`, or a message explaining why the transform was
refused, and a refused transform leaves the edge as it was:

```python
result = editor.set_edge_transform(source, "data", target, "value", [{"op": "pivot"}])
assert result == "Invalid transform: Unknown transform 'pivot'."
```

A transform is refused when a step names an unknown `op`, passes an argument the
step does not have or a value its parameter rejects, or fails on the value the
source holds right now. Because a transform changes what the edge carries, the
declared type of the source output is not checked against the input; the
transformed value is checked instead. A step that fails later, on a new value,
is reported like any other runtime error: the input keeps its last value, or
receives the failure when failures propagate. See
[Wire the Dataflow](wire-dataflow.md#runtime-validation).

---

## How transforms are stored

Transforms are saved with the dashboard, on the edge they belong to, as the
same list of step dicts. In a `DashboardModel` it is `DashboardEdge.transform`,
and in an exported dashboard file or a JSON folder store an edge looks like this:

```json
{
  "source": "plants_1",
  "source_port": "data",
  "target": "kpi_1",
  "target_port": "value",
  "transform": [
    {"op": "column", "column": "capacity"},
    {"op": "aggregate", "how": "sum"}
  ]
}
```

An edge without a transform has no `transform` key. Each step stores every
argument of its operation, defaults included, so you can also write or review
transforms by hand.

---

## Add your own step

Every step is a subclass of `panel_flowdash.Transform`. Setting `op` on a
subclass registers it under that name, after which it is offered in the
editor's **Add step** menu and accepted in stored transforms. Its parameters
are the step's arguments:

```python
import copy

import param
from panel_flowdash import Transform


class PickKey(Transform):
    """Pick one entry of a dict."""

    op = "pick"
    label = "Pick key"

    key = param.String(default="", doc="Key of the entry to pick.")

    @classmethod
    def accepts(cls, value):
        return isinstance(value, dict)

    def apply(self, value):
        return copy.deepcopy(value[self.key])

    def summary(self):
        return f"[{self.key}]"
```

```python
step = PickKey(key="total")
assert step({"total": 3, "count": 2}) == 3
assert step.to_dict() == {"op": "pick", "key": "total"}
```

- `op` is the name a stored step refers to, and `label` is the name shown in the
  editor.
- `accepts(value)` decides which values the step is offered for. Applying it to
  anything else raises an error naming the step, so `apply` only ever sees
  values it accepts, and never `None` or a failure, which are passed on before
  `apply` is called.
- `apply(value)` returns the new value. Return a new object rather than the
  input, a view of it or a mutable part of it, for the reason given in
  [Shared values](wire-dataflow.md#shared-values).
- `summary()` is the text the step contributes to the edge label. It defaults
  to the label.
- Every parameter except `name` is stored, so its values must be JSON
  serializable. In the editor a `param.Selector` becomes a dropdown, a
  `param.Boolean` a switch, a `param.Integer` a number field and a parameter
  named `column` a dropdown of DataFrame columns. Any other parameter gets a text
  field whose string is assigned to it, so use `param.String` for free-form
  arguments and convert inside `apply`.

### Registration is process-wide

The registry, `panel_flowdash.transforms.TRANSFORMS`, is a single dict filled
when a subclass is created. A step is therefore available to every editor and
every session in the Python process once its module has been imported, and a
subclass that reuses an existing `op` replaces that step everywhere, built-in
ones included. Pick an `op` unlikely to collide, for instance prefixed with your
project's name.

Import the module that defines your steps before any dashboard is loaded. For
`flowdash serve`, define or import them in the project's `__init__.py`, which
runs at startup; a component module is only imported when the component is
first used. A dashboard saved with a custom step can only be wired where that
step is registered: elsewhere the edge is refused with "Unknown transform" when
the dashboard loads.

### Steps are data, not code

A stored transform names a registered step and lists its arguments. Loading a
dashboard, or importing a dashboard file someone dropped onto the canvas, only
looks up `op` in the registry and assigns the arguments to the step's
parameters, which validate them; an unknown `op` or argument is refused. So a
dashboard file cannot make the server run anything beyond the steps your
process registered.

Anyone who can edit a dashboard controls the arguments, so keep your own steps
that way too. Treat every argument as untrusted input: never evaluate it as
code, a template or an import path, and bound anything that costs time or
memory. The built-in *Format* step, for example, accepts only a format spec
rather than a template such as `{value.__class__}`, which could reach
attributes of the value, and caps the width and precision a spec may ask for.
