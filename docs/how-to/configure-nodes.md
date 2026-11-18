# Configure Nodes

Ports carry *runtime* values that flow between components. Configuration is
different: it is *design-time* state that the dashboard author sets on a node
while building the dashboard, not something wired from another component. Titles,
page sizes, display toggles, color schemes, and similar knobs are all
configuration.

Each node therefore has two pieces of state:

- **Node state** - one param per input/output port, updated by the dataflow
  engine as edges propagate values.
- **Config state** - one param per configuration field, edited in the
  inspector and saved with the dashboard.

This guide covers how to declare configuration, how it appears in the editor,
and how it reaches your component at instantiation time.

---

## Declaring configuration

There are three ways to declare configuration, all through `@register`.

### 1. Name Viewer params with `config`

For a `Viewer` component, the simplest option is to point at params that already
exist on the class and mark them as configuration rather than input ports. Any
name listed in `config` is removed from the component's input ports:

```python
import panel as pn
import param
from panel_flowdash import register

@register(component=True, title="Data Table", config=["title", "page_size"])
class app(pn.viewable.Viewer):
    filtered = param.DataFrame()          # stays an input port

    title = param.String(default="")      # becomes configuration
    page_size = param.Integer(default=10, bounds=(5, 100))

    def __panel__(self):
        ...
```

Here `filtered` remains a wired input, while `title` and `page_size` become
config fields. The param's `default`, `label`, and (for numeric/selector params)
bounds carry over to the generated editor.

### 2. Provide a config schema with `config_schema`

When configuration is unrelated to the component's own params, or the component
is a plain function, pass a `config_schema`. It accepts three forms:

**A `param.Parameterized` subclass** - every non-base param becomes a field:

```python
class TableConfig(param.Parameterized):
    title = param.String(default="")
    page_size = param.Integer(default=10, bounds=(5, 100))

@register(component=True, config_schema=TableConfig)
def app(config, instance_config):
    ...
```

**A JSON-Schema-like dict** - `properties` map to fields; an `enum` becomes a
selector, `default` and `title` are honored:

```python
schema = {
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["compact", "comfortable"],
            "default": "compact",
            "title": "Density",
        }
    }
}

@register(component=True, config_schema=schema)
def app(config, instance_config):
    ...
```

**A Pydantic model** - its fields and defaults are read the same way as the dict
form.

### 3. Supply a custom editor with `config_editor`

By default FlowDash renders an auto-generated form from the config fields. To
take full control of the editing UI, pass a `config_editor` callable. The
inspector invokes it when the node is selected, with the current config data,
the schema, and callbacks:

```python
def editor(data, schema, *, id, type, on_patch):
    # build and return a custom editor view; call on_patch({...}) to apply changes
    ...

@register(component=True, config=["title"], config_editor=editor)
class app(pn.viewable.Viewer):
    ...
```

### 4. Check a config with `check_config`

Field types and bounds catch some mistakes, but not a SQL query that does not
parse or a column that is not in the incoming table. Pass `check_config` to
check the config as a whole before it is applied:

```python
def check(config, inputs):
    table = inputs.get("table")
    if table is not None and config["column"] not in table.columns:
        return f"There is no column {config['column']!r}; pick one of {list(table.columns)}."
    try:
        parse_sql(config["query"])
    except SyntaxError as exc:
        raise ValueError(f"The query does not parse: {exc}") from exc


@register(component=True, config=["query", "column"], check_config=check)
class Query(pn.viewable.Viewer):
    ...
```

The check is called as `check(config, inputs)`. `config` is the node's whole
config with the change applied, defaults included. `inputs` holds the node's
input values that are set and not failed, so it is `{}` for a node that is
being placed and lacks inputs it is not yet wired to.

The check returns `None`, a string, or an iterable of strings such as a list.
Each non-empty string is a problem, so `""` and `[]` pass like `None`. It may
also raise `ValueError` with the problem as its message. Any other result
(a dict, a number, a list holding something other than strings) and any other
exception are logged and count as a failed check, as does a check that takes
longer than 10 seconds. A `TimeoutError` the check raises itself is a failed
check like any other exception, not the 10 second limit.

The check may be a plain function or an `async` function. A plain one runs in
a worker thread, so it may block, for example on a database, but it must not
change the editor: `current_dashboard()` returns the editor while the check
runs, for reading its settings or context only.

Plain checks run on a pool of 8 threads of their own. Set
`panel_flowdash.config_check.CHECK_WORKERS` before the first check runs to
change that. When every thread is busy a check waits for a free one, for at
most its timeout, and its 10 seconds only start once it runs; a check that
finds no free thread in time counts as failed. The synchronous methods
(`DashboardOps.add_node`, `set_config`, `validate`, `preview` and
`replace_yaml`) wait for each check in turn, so called on the event loop they
block it for up to the timeout per check; use the `_async` variants there.

Where a check runs:

- In the inspector the form edits a draft. Each change runs the check, the
  latest change winning, and the draft is applied once it passes; otherwise
  the problems show below the form. A `config_editor`'s `on_patch` goes the
  same way. Selecting a node also checks its current config, so a node
  placed from the palette or the gallery with defaults its check refuses
  shows the problems; it is placed all the same.
- `DashboardOps.add_node` and `set_config`, and the assistant's tools, refuse
  a config with problems with an `OperationError` whose message lists them,
  which the assistant gets back to fix. `add_node_async` and
  `set_config_async` run the check without blocking the event loop, check
  again if the node's config changed meanwhile, and change nothing if
  another dashboard was opened meanwhile. The assistant's `run_command`
  refuses to place a component whose check refuses its defaults.
- `DashboardOps.replace_yaml` loads the dashboard and returns the problems as
  errors at `nodes.<name>.config`, `preview` reports such a node as
  `invalid` while still running it, and `validate` lists the problems as
  errors (`validate(checks=False)` skips the checks). Applying an earlier
  version of the dashboard from the assistant's history goes through
  `replace_yaml_async`; the restore goes ahead, and a notification lists the
  nodes whose checks fail.

A check runs when the config changes, not when the inputs do: rewiring a node
or a new upstream value does not run it again. Only `preview` and `validate`
check against the inputs as they are at that moment.

---

## How configuration reaches your component

The way config is delivered depends on the component style.

**Viewer components.** Config-state params are synced onto the Viewer instance
when the node is created, and stay synced: editing a config field in the
inspector updates the corresponding param on the live instance. You do not need to
accept any extra argument, just declare the params and use them in
`__panel__`.

A config param is synced the other way too when it declares
`metadata={"flowdash": {"two_way": True}}`. Use it for a value the component
edits itself, like the built-in Notes component does with text typed into its
tile: setting the param on the instance updates the node's config and marks the
dashboard as unsaved, so the edit is saved with it. While the dashboard is in view
mode, `read_only` or previewed, the param is set back to the config's value instead.

**Function components.** Declare an `instance_config` parameter to receive the
node's config-state object (a `param.Parameterized` instance). The existing
`config` argument still carries the shared session state, so the two are
distinct:

```python
@register(component=True, config_schema=TableConfig)
def app(config, instance_config):
    # instance_config.title, instance_config.page_size ...
    return pn.pane.Markdown(instance_config.param.title.rx())
```

`instance_config` is only injected when the function's signature includes it, so
existing components keep working unchanged.

---

## In the editor and on disk

Configuration fields declared via `config`/`config_schema` are edited in the
inspector beside the flow canvas: select a node and its *Settings* tab shows an
editable form (or your custom `config_editor`) for them.

Edits are applied to the node's config state immediately and marked as unsaved.

When a dashboard is saved, each node's current configuration is written into its
stored item; on load it is overlaid back onto a fresh config-state object before
the component is instantiated, so nodes come back with the settings the author
chose. Unknown or rejected values are skipped with a warning rather than failing
the load.

---

## Related

- [Register components](register-components.md) - the full `@register` reference
- [Define ports](define-ports.md) - the runtime side of node state
- [Layout & sizing](layout-and-sizing.md) - controlling how tiles are placed
