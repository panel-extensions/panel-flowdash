# Register Components

Every component or page in a FlowDash project is a Python module that exports
an `app` object annotated with `@register`. The decorator attaches metadata
without altering runtime behavior.

---

## The `@register` decorator

```python
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Stock Filter",
    icon="chart-line",
    description="Filters stock data by ticker symbol.",
    tags=["finance", "filter"],
    provides=[{"key": "filtered", "type": "DataFrame"}],
    requires=[{"key": "ticker", "type": "str"}],
)
def app(config):
    ...
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | `bool` | `True` | Register as a routable page. |
| `component` | `bool` | `False` | Register as a dataflow component (tile). |
| `sidebar` | `bool` | `False` | In dashboard mode, render this component's view in the Page sidebar instead of the tile grid. See [Layout & sizing](layout-and-sizing.md). |
| `title` | `str` | Module name | Human-readable display title. |
| `icon` | `str` | `None` | Icon identifier for the component palette. |
| `description` | `str` | `None` | Short description shown in tooltips. |
| `tags` | `list[str]` | `[]` | Tags for filtering in the component palette. |
| `provides` | `list` | `[]` | Output port declarations. See [Define ports](define-ports.md). |
| `requires` | `list` | `[]` | Input port declarations. See [Define ports](define-ports.md). |
| `default_size` | `dict` | `None` | Default tile size hint, e.g. `{"w": 4, "h": 3}`. See [Layout & sizing](layout-and-sizing.md). |
| `min_size` | `dict` | `None` | Minimum tile size hint. |
| `max_size` | `dict` | `None` | Maximum tile size hint. |
| `singleton` | `bool` | `False` | Only allow one instance on a dashboard. |
| `config_schema` | `type \| dict` | `None` | Design-time configuration definition: a `param.Parameterized` subclass, a Pydantic model, or a JSON-Schema-like dict. See [Configure nodes](configure-nodes.md). |
| `config` | `list[str]` | `[]` | For Viewer components, names the params that are design-time configuration rather than input ports. See [Configure nodes](configure-nodes.md). |
| `config_editor` | `callable` | `None` | Custom editor callable for the node's configuration form. See [Configure nodes](configure-nodes.md). |

---

## Using Viewer subclasses

Panel `Viewer` subclasses work as components without needing explicit port
declarations. FlowDash introspects params and `@param.output` decorators
automatically:

```python
import param
import panel as pn
from panel_flowdash import register

@register(component=True)
class StockFilter(pn.viewable.Viewer):
    ticker = param.String(default="AAPL")
    start_date = param.Date()

    @param.output(param.DataFrame)
    def filtered_data(self):
        ...

    def __panel__(self):
        return pn.Column(
            pn.widgets.TextInput.from_param(self.param.ticker),
            pn.widgets.DatePicker.from_param(self.param.start_date),
        )
```

In this example:

- **Inputs**: `ticker` (String), `start_date` (Date) are discovered from params
- **Outputs**: `filtered_data` (DataFrame) is discovered from `@param.output`

Base Parameterized and Viewable params (`name`, `loading`, etc.) are excluded
automatically.

---

## Decorator overrides on Viewer classes

When you decorate a Viewer with explicit `provides` or `requires`, the decorator
values take precedence over introspection:

```python
@register(
    component=True,
    provides=[{"key": "result", "type": "DataFrame"}],
    requires=[{"key": "query", "type": "str"}],
)
class QueryRunner(pn.viewable.Viewer):
    query = param.String()
    timeout = param.Integer(default=30)

    @param.output(param.DataFrame)
    def result(self):
        ...
```

Here only `query` is exposed as an input (not `timeout`), and `result` is the
sole output, regardless of any other `@param.output` decorators.

---

## Releasing resources when a component is removed

FlowDash creates one instance of a Viewer component per node. When that node
goes away, FlowDash calls the instance's `dispose()` method if it defines one,
so a component holding a periodic callback, a connection or a watcher on some
long-lived object can let go of it:

```python
@register(component=True)
class LivePrice(pn.viewable.Viewer):
    ticker = param.String(default="AAPL")
    price = param.Number()

    def __init__(self, **params):
        super().__init__(**params)
        self._poll = pn.state.add_periodic_callback(self._refresh, period=1000)

    def _refresh(self):
        self.price = fetch_price(self.ticker)

    def dispose(self):
        self._poll.stop()

    def __panel__(self):
        return pn.indicators.Number.from_param(self.param.price, name=self.ticker)
```

`dispose()` is called once per instance, whichever way the node leaves:

- it is deleted on the canvas or removed with `FlowDash.remove_component()`
- the canvas is cleared, or replaced by loading, importing or creating a dashboard
- the server session the editor belongs to ends, or `FlowDash.dispose()` is called
- placing the node fails after the instance was created, e.g. because
  `__panel__` raised

By the time it runs, the node is already disconnected from the dataflow graph:
its inputs no longer receive values and anything it sets on its outputs no
longer reaches downstream nodes. It may be `async`, in which case it is awaited
on the event loop. An exception raised by `dispose()` is logged and does not
stop the node, or any other node, from being removed.

Changing a node's configuration updates the existing instance rather than
replacing it, so it does not trigger `dispose()`. The hook only applies to
Viewer components; a decorated function is called rather than instantiated, so
FlowDash holds no object of its own to dispose.

---

## Project directory layout

FlowDash scans one level of subdirectories as "sections":

```
my_project/
    Section_A/
        __init__.py
        component1.py    # exports `app`
        component2.py
    Section_B/
        page1.py
```

Rules:

- Directories starting with `.` or `_` are skipped.
- Files starting with `_` are skipped.
- Each module must export an `app` attribute.
- The `app_id` is `"Section/module_name"` (e.g. `"Section_A/component1"`).
