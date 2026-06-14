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
| `sidebar` | `bool` | `False` | Show in the sidebar navigation. |
| `title` | `str` | Module name | Human-readable display title. |
| `icon` | `str` | `None` | Icon identifier for the component palette. |
| `description` | `str` | `None` | Short description shown in tooltips. |
| `tags` | `list[str]` | `[]` | Tags for filtering in the component palette. |
| `provides` | `list` | `[]` | Output port declarations. |
| `requires` | `list` | `[]` | Input port declarations. |
| `default_size` | `dict` | `None` | Default grid tile size `{"w": 4, "h": 3}`. |
| `singleton` | `bool` | `False` | Only allow one instance on a dashboard. |
| `config_schema` | `dict` | `None` | JSON Schema for configuration form generation. |

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
