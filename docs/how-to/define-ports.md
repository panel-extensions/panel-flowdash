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
| `type` | `str` | `None` | Expected type. See [Type compatibility rules](#type-compatibility-rules). |
| `label` | `str` | `None` | Display label. |
| `required` | `bool` | `True` | Whether the port must hold a value. |
| `blocking` | `bool` | `True` | Whether a missing value blocks the component. A port blocks when it is both `required` and `blocking`. |
| `fallback` | `any` | `None` | Default value when disconnected. A fallback counts as a value. |

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
publish it. Declare the Parameter as an output in its `metadata`, under the
`"flowdash"` key:

```python
class Picker(pn.viewable.Viewer):
    company = param.String(default="ACME", metadata={"flowdash": {"output": True}})
    region = param.Selector(
        objects=["EU", "US"],
        metadata={"flowdash": {"output": True, "label": "Region"}},
    )

    def __panel__(self):
        return self._table
```

Whatever the component assigns to `self.company` or `self.region`, in
`__init__` or later, flows to every connected input. The port is typed after
the Parameter (`"String"` and `"Selector"` here); a plain `param.Parameter`
leaves the port untyped so it can connect to any input. `label` is optional.
Other keys in the `"flowdash"` dict, and other metadata namespaces, are ignored.

The declaration lives on the Parameter, so it is found on the class without
instantiating it. Subclasses inherit it, and Parameters added to a class built
with `type(...)` count too, which lets a class factory declare its outputs
without repeating them in `@register`. A redeclared Parameter inherits the
metadata of the one it overrides; set `{"flowdash": {"output": False}}` to turn
an inherited output off.

A Parameter published this way is an output only, so it is left out of the
component's input ports. It must be a port-eligible name: a base Viewable
Parameter such as `width`, or a name starting with an underscore, raises a
`ValueError` when the component's spec is built.

Metadata outputs combine with `@param.output` methods, which are listed first.
To publish a Parameter without touching its definition, name it in `provides`
instead:

```python
@register(component=True, provides=["selection"])
class Picker(pn.viewable.Viewer):
    selection = param.List(default=[])
```

The port is typed after the Parameter unless `provides` gives a `type`.
`provides` replaces every introspected output, `@param.output` methods and
metadata outputs alike, so list those in it too if the component has them. A
Parameter declared as an output in metadata but missing from `provides` is not
published and does not become an input either.

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

### Inputs that receive failures

With `propagate_failures=True` on the editor, an upstream failure normally leaves
an input on its last value and covers the tile with a message. Name an input in
`handles_failures` to receive the `param.ReactiveError` instead, see
[Wire the dataflow](wire-dataflow.md#carry-failures-downstream):

```python
@register(component=True, handles_failures=["data"])
class Table(pn.viewable.Viewer):
    data = param.Parameter()
```

---

## Required inputs

A component often cannot show anything useful until an input arrives. A dashboard
tile shows a placeholder instead of the component's view while any of its
blocking inputs is `None`, and swaps to the view as soon as every blocking input
holds a value. If an input returns to `None`, because it is disconnected or the
upstream value is cleared, the placeholder returns. This works the same in the
wiring canvas and in the dashboard.

Function components declare blocking inputs with `requires`, with the same
`required` and `blocking` fields that page mode uses. Both default to `True`, so
every input in `requires` blocks unless it sets `"required": False` or
`"blocking": False`.

A Viewer declares them on the Parameter with `metadata`:

```python
@register(component=True, placeholder="Pick a company to start")
class Report(pn.viewable.Viewer):
    company = param.String(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )
    notes = param.String(default="")  # optional, never blocks

    def __panel__(self):
        return pn.pane.Markdown(pn.bind("Report for {}".format, self.param.company))
```

`blocking` defaults to the value of `required`, so `{"required": True}` is
enough. Viewer params without `metadata["flowdash"]` stay optional, other keys
in the `"flowdash"` dict are ignored here, and `requires` entries on `@register`
replace the introspected inputs as usual. A Parameter declared as an output
with `{"output": True}` is not an input, so `required` and `blocking` on it are
ignored.

The placeholder reads "Waiting for Company", naming the labels of the inputs that
are still missing. Pass `placeholder` to `@register` to replace the text.

The component is still created while it waits, so it can subscribe to
parameters and publish outputs; only what the tile displays changes, and its
view is built once rather than on every swap. An input that has a `default` or
`fallback` already holds a value and does not block.

---

## Type compatibility rules

When connecting an output to an input, the dataflow engine checks that the
output's type can feed the input's type. Type names are normalized first, so
the Python names used in `@register` and the Param class names that Viewer
ports are introspected with interoperate:

| Canonical type | Also written as |
|----------------|-----------------|
| `str` | `String`, `Color`, `Path`, `Filename`, `Foldername`, `FileSelector` |
| `int` | `Integer` |
| `number` | `Number`, `float`, `Magnitude` |
| `bool` | `Boolean`, `Event` |
| `list` | `List`, `ListSelector`, `MultiFileSelector` |
| `tuple` | `Tuple`, `NumericTuple`, `Range`, `XYCoordinates`, `DateRange` |
| `dict` | `Dict` |
| `date` | `Date`, `CalendarDate`, `datetime` |
| `DataFrame` | `pd.DataFrame`, `pandas.DataFrame` |
| `Series` | `pd.Series`, `pandas.Series` |
| `Array` | `ndarray`, `np.ndarray`, `numpy.ndarray` |
| `any` | `object`, `Any`, `Parameter`, `Selector`, `ObjectSelector`, `ClassSelector` |

Names are matched case-insensitively, and a name not in the table matches
itself. A connection is allowed when:

1. Either port is untyped or typed `any`. A `Selector` or `ClassSelector`
   does not say what type its value has, so it counts as `any`.
2. Both normalize to the same type, e.g. a `param.String` output and a
   `"str"` input.
3. The output's type is a subtype of the input's. `int` is a subtype of
   `number`, so an `Integer` output feeds a `Number` or `"float"` input but not
   the other way round. `bool` is deliberately not a subtype of `int`, because
   `param.Integer` and `param.Number` refuse booleans.

Anything else is rejected with a "Type mismatch" message.

```python
# Allowed: String and str are one type
provides=[{"key": "ticker", "type": "String"}]
requires=[{"key": "symbol", "type": "str"}]

# Allowed: an int output feeds a number input
provides=[{"key": "count", "type": "int"}]
requires=[{"key": "amount", "type": "Number"}]

# Rejected: int is not a str
provides=[{"key": "count", "type": "int"}]
requires=[{"key": "name", "type": "str"}]
```

The check only rules out connections that can never work. The input's
Parameter still validates each value it receives, so a `Selector` output can
connect to an `Integer` input but a value the `Integer` refuses is reported
when it arrives, and connecting is refused if the output already holds one. An
edge with a [transform](wire-dataflow.md) carries the transformed value rather
than the output's type, so it skips the static check and relies on this
runtime check alone.

### Registering your own types

Use `register_port_type` to add spellings and subtype rules for your own types,
typically next to the components that use them:

```python
from panel_flowdash import register_port_type

register_port_type("Person")
register_port_type("Customer", aliases=["crm.Customer"], subtype_of=["Person"])
register_port_type("Ticker", subtype_of=["str"])
```

A `Customer` output now feeds a `Person` input, and a `Ticker` output feeds any
string input. Subtyping is transitive. Registering a name again adds to its
aliases and supertypes, and an alias that already names a different type
raises a `ValueError`. `normalize_port_type(name)` and
`port_types_compatible(source, target)` expose the same rules, e.g. for tests.

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
    default_size={"width": 400, "height": 300},
)
```

You rarely need to construct these manually. `build_component_spec()` and
`build_component_specs()` handle this from the registry.
