# Build Dataflow Dashboards in Python

**panel-flowdash is a framework for building visual, dataflow-driven dashboards
with Panel.** Define components with typed ports, wire them together, and persist
layouts to SQLite.

!!! tip "Quick Demo"
    New to FlowDash? **[Try the quickstart &rarr;](quickstart.md)**

## Why FlowDash?

- **Component-first architecture**: define reusable building blocks with `@register` or as Viewer subclasses.
- **Typed dataflow**: ports carry type information for compile-time and runtime validation.
- **Visual wiring**: connect components in a node editor, with cycle detection and single-source enforcement.
- **Persistence**: dashboard layouts, edges, and grid positions are stored in SQLite.
- **One command to serve**: `flowdash serve my_project/` scans, registers, and launches.

## Quickstart

```python
from panel_flowdash import register

@register(component=True, provides=[{"key": "ticker", "type": "str"}])
def app(config):
    import panel_material_ui as pmui
    return pmui.Select(name="Ticker", options=["AAPL", "GOOG", "MSFT"])
```

```bash
flowdash serve my_project/ --port 5006
```

## How-to guides

- [Register Components](how-to/register-components.md) - the `@register` decorator and Viewer classes
- [Define Ports](how-to/define-ports.md) - typed inputs and outputs for dataflow wiring
- [Wire the Dataflow](how-to/wire-dataflow.md) - connecting components with validation
- [Persist Dashboards](how-to/persist-dashboards.md) - SQLite storage for layouts and edges
- [Serve a Project](how-to/serve-project.md) - the `flowdash serve` CLI

## Examples

- [Examples gallery](examples/index.md)

## Reference

- [API reference](reference/panel_flowdash.md)
- [Release notes](releases.md)
