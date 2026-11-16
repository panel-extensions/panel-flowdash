# Quickstart

Get a FlowDash project running with two wired components and a CLI-served
dashboard. By the end of this page you will have a working dataflow app.

## Install

```bash
pip install panel-flowdash
```

## Create a project

FlowDash expects a directory with section folders containing Python modules.
Each module exports an `app` object decorated with `@register`:

```
my_project/
    Analytics/
        __init__.py
        selector.py
        chart.py
```

## Define components

```python title="Analytics/selector.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    provides=[{"key": "company", "type": "str"}],
    title="Company Selector",
)
def app(config):
    import panel_material_ui as pmui

    select = pmui.Select(label="Company", options=["ACME", "Globex", "Initech"])
    config.company = select.param.value
    return select
```

```python title="Analytics/chart.py"
from panel_flowdash import register

@register(
    page=False,
    component=True,
    requires=[{"key": "company", "type": "str"}],
    title="Company Chart",
)
def app(config):
    import panel as pn

    def title(company):
        return f"## Revenue chart for {company}"

    return pn.pane.Markdown(pn.bind(title, config.param.company))
```

The `provides` list declares output ports and `requires` declares input ports.
Each placed component gets its own `config` object, a `param.Parameterized`
with one parameter per port. A component publishes an output by assigning to
it: assigning the widget's `value` parameter, rather than its current value,
keeps the output following the selection. An input is read the same way, here
through `config.param.company` so the Markdown updates whenever a new value
arrives. When the two are wired together, picking a company in the selector
updates the chart.

Until the chart's `company` input receives a value its tile shows a "Waiting
for company" placeholder, because inputs are required by default. See
[Define Ports](how-to/define-ports.md) for optional inputs and fallbacks.

## Serve

```bash
flowdash serve my_project/
```

This scans `my_project/`, discovers the two components, builds the application,
and serves it on port 5006. Open `http://localhost:5006` in your browser,
create a dashboard, drop both components onto the wiring canvas and drag from
the selector's `company` output to the chart's `company` input.

## Wire it in Python

The same dashboard can be built without the browser, which is also how you
would test it. `FlowDash` scans the project the way `flowdash serve` does:

```python
from panel_flowdash import FlowDash, MemoryDashboardStore

editor = FlowDash("my_project", store=MemoryDashboardStore())
selector = editor.add_component("Analytics/selector")
chart = editor.add_component("Analytics/chart")
assert editor.connect(selector, "company", chart, "company") is True

assert editor.graph.get_state(chart).company == "ACME"
```

See [Embed the Editor](how-to/embed-the-editor.md) for everything the editor
can do from code.

## What happens under the hood

1. `build_registry()` imports each module and extracts `@register` metadata.
2. `build_component_specs()` creates typed port definitions for each component.
3. `DataflowGraph` manages wiring between component node states.
4. `DashboardStore` persists the layout to `my_project/dashboards.db`.

## Next steps

- [Register Components](how-to/register-components.md) for the full decorator API
- [Define Ports](how-to/define-ports.md) for typed ports and Viewer introspection
- [Wire the Dataflow](how-to/wire-dataflow.md) for validation rules and runtime error handling
