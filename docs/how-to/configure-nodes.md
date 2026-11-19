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

**A JSON-Schema-like dict** - `properties` map to fields typed from their
`type` (`string`, `integer`, `number`, `boolean`, `array`, `object`, or a
`["integer", "null"]` pair for an optional one), so each gets the matching
widget. An `enum` becomes a dropdown, with an empty option when it allows
null, an array of an `enum` a multi-select, `minimum` and `maximum` bound a
number, and `default`, `title` and `description` are honored. A property
without a type, or whose default does not fit its type, accepts any value and
is edited as a Python literal:

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

**A Pydantic model** - its fields are typed from the model's JSON schema the
same way as the dict form: `Literal` and `Enum` fields are dropdowns,
`Optional` ones can be cleared, and `list[Literal[...]]` is a multi-select.
Defaults are stored as JSON data, as a saved dashboard holds them: a nested
model's default is a dict, an `Enum`'s its value and a tuple a list.

Optional fields of any kind can be left empty in the inspector: an optional
number shows empty rather than 0, and a dropdown for a field that allows None
offers "Not set".

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

A check that could not verify the config, for example because a probe query
timed out on a slow database, returns a `CheckWarning` in place of a problem,
alone or in its list:

```python
from panel_flowdash import CheckWarning


def check(config, inputs):
    try:
        probe(config["query"], timeout=3)
    except ProbeTimeout:
        return CheckWarning("Could not verify the query: the probe timed out.")
    except QueryError as exc:
        return str(exc)
```

A warning does not refuse the config: it is applied, and the warning shown
with it, as a warning alert in the inspector, in the assistant's tool result
("Updated the config of 'q', but: ...") and as a `warning` issue at
`nodes.<name>.config` from `validate` and `replace_yaml`. `DashboardOps`
leaves what the latest `add_node`, `set_config`, `connect` or `disconnect`
found in `last_check`, a list of the problems with the warnings in its
`warnings`. `CheckWarning` is a `str`, so a list mixing problems and warnings
is still a list of strings.

The check may be a plain function or an `async` function. A plain one runs in
a worker thread, so it may block, for example on a database, but it must not
change the editor: `current_dashboard()` returns the editor while the check
runs, for reading its settings or context only.

Plain checks run on a pool of 8 threads of their own. Set
`panel_flowdash.config_check.CHECK_WORKERS` before the first check runs to
change that. When every thread is busy a check waits for a free one, for at
most its timeout, and its 10 seconds only start once it runs; a check that
finds no free thread in time counts as failed. The synchronous methods
(`DashboardOps.add_node`, `set_config`, `connect`, `disconnect`, `validate`,
`preview` and `replace_yaml`) wait for each check in turn, so called on the
event loop they block it for up to the timeout per check. Code running on the
event loop, such as your own agent tools, should call the `_async` variants
(`add_node_async`, `set_config_async`, `connect_async`, `disconnect_async`,
`preview_async`, `replace_yaml_async`) instead.

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
- `DashboardOps.connect` and `disconnect`, their `_async` variants and the
  assistant's tools check the target node again with its new inputs and
  return what the check found. The connection is made all the same; the
  assistant's tool result lists the problems ("Connected data.source to
  q.source. q's config fails its check: ..."), and `preview` and `validate`
  report the node as above until its config is fixed.
- In the canvas, connecting or disconnecting an input of the node the
  inspector shows checks it again and updates its alert. A node that is not
  selected is checked when it is next selected. A held back draft that now
  passes is applied, as the person's edit even when the assistant made the
  connection.
- Checks after a rewire, and the inspector's draft checks, are coalesced per
  node: while one runs, later requests share a single check queued after it,
  which reads the node's latest config and inputs once it starts, and is
  skipped when those are what the running check read. A node then has at
  most one check running and one queued, counting checks of a draft with
  pending edits apart. A sync check that times out still holds its thread
  until it returns, so slow checks can hold more threads than that.
- `DashboardOps.replace_yaml` loads the dashboard and returns the problems as
  errors at `nodes.<name>.config`, `preview` reports such a node as
  `invalid` while still running it, and `validate` lists the problems as
  errors (`validate(checks=False)` skips the checks). Applying an earlier
  version of the dashboard from the assistant's history goes through
  `replace_yaml_async`; the restore goes ahead, and a notification lists the
  nodes whose checks fail.

A check does not run when a new value arrives over an existing connection,
when an upstream node is removed or when an edge's transform changes (the
inspector does check the selected node again on the latter). Only `preview`
and `validate` check against the inputs as they are at that moment.

### 5. Offer choices for a field

A field that names a column, a table or another value the node's inputs
determine is easier to fill from a list than from memory. A param can declare
that it names a column of one of the node's inputs:

```python
@register(component=True, config=["column"])
class Histogram(Viewer):
    data = param.DataFrame(default=None, allow_None=True)
    column = param.String(default="", metadata={"flowdash": {"choices": "columns", "of": "data"}})
```

Once `data` is wired the inspector shows `column` as a dropdown of the
DataFrame's columns, each with its type (`year (integer)`). A list field gets a
multi-select. In a JSON schema the same declaration is
`"x-flowdash": {"choices": "columns", "of": "data"}` on the property, and in a
Pydantic model `Field(json_schema_extra={"x-flowdash": {...}})`.

For anything else, pass `config_choices`, a callable that is called like
`check_config`, as `choices(config, inputs)`, sync or async, and returns the
values per field:

```python
from panel_flowdash import Choices

def choices(config, inputs):
    source = inputs.get("source")
    if source is None:
        return {}
    return {
        "table": source.list_tables(),
        "title": Choices(["Totals", "Counts"], restrict=False),
    }

@register(component=True, config=["table", "title"], config_choices=choices)
class TableView(Viewer):
    source = param.Parameter()
    table = param.String(default="")
    title = param.String(default="")
```

Each value is a list, a `{label: value}` dict or a `Choices(options, restrict=True,
types=None)`. A restricted field shows a dropdown, with a "Not set" option when
the field may be empty (any text field, or a field that allows None);
`restrict=False` keeps a text field that suggests the values and accepts
others. Declared column choices show as soon as the input is wired, and a
thin progress bar under the form shows while `config_choices` runs. Choices from the callable
replace the declared ones of the same field. The inspector asks again when the
node's inputs or config change, and offers nothing for a field while the
callable fails, times out (10 seconds) or returns no choices for it, so the
plain field is always there. A value that is not among the choices, for
example one set by the assistant, stays and is marked "(not available)";
showing the form never changes the config.

Columns of a DataFrame are read directly. For another tabular value, such as a
lazy query, register a `columns` reader with its port type, which the
declaration then uses for inputs of that type and its subtypes. A
`config_choices` callable can list columns the same way with
`columns_of(value, port_type=None)`, which returns `(name, type)` pairs, or
None for a value that is not tabular:

```python
register_port_type("mylib.Query", columns=lambda query: query.schema)  # {name: type}
```

---

### 6. Edit code, SQL or Markdown in a code editor

A text field holding a query, code or Markdown can ask for a code editor
instead of a one-line text box:

```python
sql = param.String(default="", metadata={"flowdash": {"widget": "code", "language": "sql"}})
```

The inspector shows a few lines with highlighting for the `language` (any
language Panel's `CodeEditor` knows, e.g. `sql`, `python`, `markdown` or
`json`; `text` without one) and a button that opens the same text in a larger
editor. The text is applied when the editor loses focus, not on every key, so a
`check_config` runs once per edit. In a JSON schema the same declaration is
`"x-flowdash": {"widget": "code", "language": "sql"}` on the property.

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
