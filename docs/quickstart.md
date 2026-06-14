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
    return pmui.Select(name="Company", options=["ACME", "Globex", "Initech"])
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
    company = config.get("company", "...")
    return pn.pane.Markdown(f"## Revenue chart for {company}")
```

The `provides` list declares output ports and `requires` declares input ports.
When wired together, changing the selector's output automatically updates the
chart's input.

## Serve

```bash
flowdash serve my_project/
```

This scans `my_project/`, discovers the two components, builds the application,
and serves it on port 5006. Open `http://localhost:5006` in your browser.

## What happens under the hood

1. `build_registry()` imports each module and extracts `@register` metadata.
2. `build_component_specs()` creates typed port definitions for each component.
3. `DataflowGraph` manages wiring between component node states.
4. `DashboardStore` persists the layout to `my_project/dashboards.db`.

## Next steps

- [Register Components](how-to/register-components.md) for the full decorator API
- [Define Ports](how-to/define-ports.md) for typed ports and Viewer introspection
- [Wire the Dataflow](how-to/wire-dataflow.md) for validation rules and runtime error handling
