# panel-flowdash

[![CI](https://img.shields.io/github/actions/workflow/status/panel-extensions/panel-flowdash/ci.yml?style=flat-square&branch=main)](https://github.com/panel-extensions/panel-flowdash/actions/workflows/ci.yml)
[![conda-forge](https://img.shields.io/conda/vn/conda-forge/panel-flowdash?logoColor=white&logo=conda-forge&style=flat-square)](https://prefix.dev/channels/conda-forge/packages/panel-flowdash)
[![pypi-version](https://img.shields.io/pypi/v/panel-flowdash.svg?logo=pypi&logoColor=white&style=flat-square)](https://pypi.org/project/panel-flowdash)
[![python-version](https://img.shields.io/pypi/pyversions/panel-flowdash?logoColor=white&logo=python&style=flat-square)](https://pypi.org/project/panel-flowdash)

A dataflow-driven dashboard builder for [Panel](https://panel.holoviz.org). Define reusable components with typed input/output ports, wire them together visually, and persist layouts to SQLite.

## Features

- **Component registry** with the `@register` decorator for metadata-driven discovery
- **Typed ports** with automatic introspection from `param.output` decorators and Viewer params
- **Dataflow engine** with cycle detection, type checking, single-source-per-input validation, and runtime error reporting
- **SQLite persistence** for dashboard layouts, edges, and tile configurations
- **CLI** (`flowdash serve`) to launch a project directory as a full dashboard app

## Installation

```bash
pip install panel-flowdash
```

## Quickstart

Create a project directory with component modules:

```
my_project/
    Analytics/
        selector.py
        chart.py
```

Define components with typed ports:

```python
# Analytics/selector.py
from panel_flowdash import register

@register(component=True, provides=[{"key": "company", "type": "str"}])
def app(config):
    import panel_material_ui as pmui
    return pmui.Select(name="Company", options=["ACME", "Globex"])
```

```python
# Analytics/chart.py
from panel_flowdash import register

@register(component=True, requires=[{"key": "company", "type": "str"}])
def app(config):
    import panel as pn
    return pn.pane.Markdown(f"# Chart for {config.get('company', '...')}")
```

Serve it:

```bash
flowdash serve my_project/
```

## Using Viewer classes

Components can also be Panel `Viewer` subclasses. Ports are introspected automatically from params and `@param.output` decorators:

```python
import param
import panel as pn
from panel_flowdash import register

@register(component=True)
class StockFilter(pn.viewable.Viewer):
    ticker = param.String(default="AAPL")

    @param.output(param.DataFrame)
    def filtered_data(self):
        ...

    def __panel__(self):
        return pn.widgets.TextInput.from_param(self.param.ticker)
```

## CLI

```bash
flowdash serve <project-dir> [options]
```

| Option | Description |
|--------|-------------|
| `--port` | Port to serve on (default: 5006) |
| `--db-path` | SQLite database path (default: `<project-dir>/dashboards.db`) |
| `--title` | Browser tab title |
| `--dev` | Enable autoreload |
| `--admin` | Enable Panel admin interface |

Run `flowdash serve --help` for the full list.

## Development

```bash
git clone https://github.com/panel-extensions/panel-flowdash
cd panel-flowdash
pip install -e ".[dev]"
pytest tests
```

## Contributing

Contributions are welcome! Please fork the repository, create a feature branch, and open a pull request.
