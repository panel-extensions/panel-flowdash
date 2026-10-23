# Define Ports

Ports are the typed connection points on a component node. Output ports produce
values; input ports consume them. The dataflow engine uses port types for
compile-time validation and `param.watch` for runtime propagation.

---

## Port declarations in `@register`

### String shorthand

The simplest form uses plain strings as port names (untyped):

```python
@register(
    component=True,
    provides=["ticker", "date_range"],
    requires=["market"],
)
def app(config):
    ...
```

### Dict form with types

For typed ports, use dictionaries with `key` and `type`:

```python
@register(
    component=True,
    provides=[
        {"key": "filtered_data", "type": "DataFrame", "label": "Filtered Data"},
    ],
    requires=[
        {"key": "ticker", "type": "str", "label": "Ticker Symbol"},
        {"key": "start_date", "type": "Date", "required": True, "blocking": True},
    ],
)
def app(config):
    ...
```

### Output port fields

| Field | Type | Description |
|-------|------|-------------|
| `key` | `str` | Port identifier (used in wiring). |
| `type` | `str` | Type name for compatibility checking. |
| `label` | `str` | Display label in the UI. |

### Input port fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `key` | `str` | | Port identifier (used in wiring). |
| `type` | `str` | `None` | Expected type (case-insensitive match). |
| `label` | `str` | `None` | Display label. |
| `required` | `bool` | `True` | Whether the port must be connected. |
| `blocking` | `bool` | `True` | Whether missing input blocks execution. |
| `fallback` | `any` | `None` | Default value when disconnected. |

---

## Automatic introspection from Viewer classes

When using a Viewer subclass without explicit `provides`/`requires`, FlowDash
introspects ports from param:

### Outputs from `@param.output`

```python
class MyComponent(pn.viewable.Viewer):
    @param.output(param.Number)
    def score(self):
        return self._compute_score()

    @param.output(param.DataFrame)
    def table(self):
        return self._data
```

Each `@param.output` becomes an `OutputPort` with its type derived from the
param type class name (e.g. `"Number"`, `"DataFrame"`).

### Outputs from Parameters

A Viewer that already holds the value on a Parameter does not need a method to
publish it. Name the Parameter in `provides`:

```python
@register(component=True, provides=["selection"])
class Picker(pn.viewable.Viewer):
    selection = param.List(default=[])

    def __panel__(self):
        return self._table
```

Whatever the component assigns to `self.selection` flows to every connected
input. The port is typed after the Parameter (`"List"` here) unless `provides`
gives a `type`; a plain `param.Parameter` leaves the port untyped so it can
connect to any input.

A Parameter published this way is an output only, so it is left out of the
component's input ports. `provides` replaces the outputs introspected from
`@param.output`, so list those methods in it too if the component has both.

### Inputs from param declarations

```python
class MyComponent(pn.viewable.Viewer):
    threshold = param.Number(default=0.5)
    category = param.Selector(objects=["A", "B", "C"])
```

Each user-defined param (excluding base class params like `name`, `loading`,
etc.) becomes an `InputPort`. The type is the param class name (e.g. `"Number"`,
`"Selector"`).

!!! note "Config params are not ports"
    Params named in the `config` argument (or covered by a `config_schema`) are
    treated as design-time configuration and excluded from the input ports. See
    [Configure nodes](configure-nodes.md).

---

## Type compatibility rules

When connecting an output to an input, the dataflow engine checks type
compatibility:

1. If either port is untyped (`type=None`), the connection is allowed.
2. Types are compared case-insensitively: `"dataframe"` matches `"DataFrame"`.
3. Mismatched types produce an error message explaining the incompatibility.

```python
# This works: both typed as "str"
provides=[{"key": "ticker", "type": "str"}]
requires=[{"key": "symbol", "type": "str"}]

# This works: source is untyped
provides=["value"]
requires=[{"key": "input", "type": "str"}]

# This fails: "int" != "str"
provides=[{"key": "count", "type": "int"}]
requires=[{"key": "name", "type": "str"}]
```

---

## The ComponentSpec dataclass

Internally, FlowDash converts registry entries into `ComponentSpec` objects:

```python
from panel_flowdash import ComponentSpec, OutputPort, InputPort

spec = ComponentSpec(
    component_id="Analytics/filter",
    title="Data Filter",
    description="Filters data by criteria",
    icon="filter",
    tags=["data"],
    outputs=[OutputPort(name="filtered", type="DataFrame")],
    inputs=[InputPort(name="source", type="DataFrame", required=True)],
    default_size={"w": 4, "h": 3},
)
```

You rarely need to construct these manually. `build_component_spec()` and
`build_component_specs()` handle this from the registry.
