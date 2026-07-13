# Serve a Project

The `flowdash serve` command scans a project directory, discovers components
and pages, and launches a Panel server with all routes configured.

---

## Basic usage

```bash
flowdash serve my_project/
```

This:

1. Adds `my_project/` to `sys.path`
2. Scans subdirectories for modules with `@register`-decorated `app` exports
3. Builds the component registry and specs
4. Creates a `DashboardStore` at `my_project/dashboards.db`
5. Serves the app on `http://0.0.0.0:5006`

The landing page lists the project's pages and any saved dashboards. From here
you can open a dashboard, or jump into the editor to build a new one.

![The FlowDash launcher listing saved dashboards under Custom Apps](../assets/images/launcher.png)

---

## CLI options

```bash
flowdash serve <directory> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `5006` | Port to serve on. |
| `--address` | `0.0.0.0` | Address to bind to. |
| `--title` | `FlowDash` | Application title in the browser tab. |
| `--db-path` | `<dir>/dashboards.db` | Path to the SQLite database. |
| `--warm` | off | Import all modules at startup instead of lazily on first visit. |
| `--dev` | off | Enable autoreload for development. |
| `--admin` | off | Enable the Panel admin interface. |
| `--profiler` | none | Profiler to use (pyinstrument, snakeviz). |
| `--num-threads` | auto | Thread pool size. |
| `--allow-websocket-origin` | all | Restrict allowed websocket origins. |
| `--no-notifications` | off | Disable Panel notifications. |

---

## Development mode

```bash
flowdash serve my_project/ --dev --port 8080
```

With `--dev`, the server reloads automatically when source files change.

---

## Custom database location

By default, the dashboard database is created inside the project directory.
Override this for shared deployments:

```bash
flowdash serve my_project/ --db-path /var/data/flowdash.db
```

---

## Running as a module

You can also run FlowDash without the entry point script:

```bash
python -m panel_flowdash.command serve my_project/
```

---

## Lazy loading and warm start

By default, page and component modules are imported lazily — on the first visit
to that page or on first open of the component editor. This keeps startup fast
even when individual modules have heavy dependencies.

Pass `--warm` to import all modules at startup instead:

```bash
flowdash serve my_project/ --warm
```

Import errors are logged as warnings; they do not abort startup.

---

## Project initialization file

If `my_project/__init__.py` exists, it is executed during server startup —
before registry scanning and before Panel launches. Use it for any
pre-configuration that the rest of the project depends on:

```python
# my_project/__init__.py
import os
import panel as pn

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/mydb")

# Register Panel extensions needed by your components (e.g. tabulator, vega, deckgl).
# FlowDash only calls pn.extension() with notifications enabled by default,
# so any additional extensions must be loaded here.
pn.extension("tabulator", "vega", sizing_mode="stretch_width")
```

The file runs with `my_project/` already on `sys.path` and as the working
directory, so local relative imports work normally.

---

## Project structure requirements

```
my_project/
    __init__.py             # optional startup hook (runs before server launch)
    SectionA/
        __init__.py         # optional
        component1.py       # must export `app`
        component2.py
    SectionB/
        page1.py
    .hidden/                # ignored (starts with .)
    _private/               # ignored (starts with _)
```

Each Python file must export an `app` attribute decorated with `@register`.
Files starting with `_` are skipped. Directories starting with `.` or `_` are
skipped.
