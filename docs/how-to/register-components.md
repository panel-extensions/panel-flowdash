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
    icon="filter_alt",
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
| `sidebar` | `bool` | `False` | Render this component's view in the controls bar above the tiles instead of in the tile grid. See [Layout & sizing](layout-and-sizing.md). |
| `title` | `str` | Module name | Human-readable display title. |
| `icon` | `str` | `None` | [Material icon](https://fonts.google.com/icons?icon.set=Material+Icons) name shown in the component palette, e.g. `"filter_alt"`. |
| `description` | `str` | `None` | Short description shown in tooltips and matched by the palette search. |
| `tags` | `list[str]` | `[]` | Keywords matched by the palette search. A `source`, `transform`, `view` or `widget` tag also picks the palette group. See [The component palette](#the-component-palette). |
| `provides` | `list` | `[]` | Output port declarations. See [Define ports](define-ports.md). |
| `requires` | `list` | `[]` | Input port declarations. See [Define ports](define-ports.md). |
| `placeholder` | `str` | `None` | Message a tile shows while a blocking input is unset, leading the "This tile needs ..." text. See [Define ports](define-ports.md#required-inputs). |
| `default_size` | `dict` | `None` | Size of a new tile in pixels, e.g. `{"width": 600, "height": 400}`. See [Layout & sizing](layout-and-sizing.md#tile-sizing-hints). |
| `min_size` | `dict` | `None` | Smallest size a tile can be resized to, in pixels. |
| `max_size` | `dict` | `None` | Largest size a tile can be resized to, in pixels. |
| `framed` | `bool \| None` | `None` | Whether the tile is drawn as a card in view mode. `None` follows the dashboard's `tile_cards` setting. See [Layout & sizing](layout-and-sizing.md#tile-frames). |
| `singleton` | `bool` | `False` | Only allow one instance on a dashboard. |
| `config_schema` | `type \| dict` | `None` | Design-time configuration definition: a `param.Parameterized` subclass, a Pydantic model, or a JSON-Schema-like dict. See [Configure nodes](configure-nodes.md). |
| `config` | `list[str]` | `[]` | For Viewer components, names the params that are design-time configuration rather than input ports. See [Configure nodes](configure-nodes.md). |
| `config_editor` | `callable` | `None` | Custom editor callable for the node's configuration form. See [Configure nodes](configure-nodes.md). |
| `check_config` | `callable` | `None` | Checks a node's config, with its live inputs, before it is applied and again when the node is rewired. Returns problems that refuse the config, or a `CheckWarning(message)` for one it could not verify, which is applied and shown with the warning. See [Configure nodes](configure-nodes.md#4-check-a-config-with-check_config). |
| `config_choices` | `callable` | `None` | Returns the values each config field may take, `{field: choices}`, offered in a dropdown. See [Configure nodes](configure-nodes.md#5-offer-choices-for-a-field). |
| `config_completions` | `callable` | `None` | Returns the words each code field completes while typing, `{field: words}`. Without it a code field completes the columns of the node's tabular inputs. See [Configure nodes](configure-nodes.md#6-edit-code-sql-or-markdown-in-a-code-editor). |
| `palette_presets` | `callable` | `None` | Returns further palette entries that place the component with a config filled in, from the editor's `context`. See [Preset entries](#preset-entries). |
| `aliases` | `list[str]` | `[]` | Ids the component was registered under before. See [Renaming components and ports](#renaming-components-and-ports). |
| `port_aliases` | `dict[str, str]` | `{}` | Former port or config names mapped to the current ones. See [Renaming components and ports](#renaming-components-and-ports). |

---

## The component palette

The editor lists components in a palette next to the canvas, grouped by the
role their ports suggest:

| Group | Components |
|-------|------------|
| Data sources | Outputs but no inputs |
| Transforms | Inputs and outputs |
| Views | No outputs, including the built-in Markdown, Notes, Heading, Divider, KPI and Image |
| Widgets | The built-in controls listed below |

A `source`, `transform`, `view` or `widget` tag overrides this, for example
for a data source whose inputs only tune the query:

```python
@register(component=True, title="Wind Turbines Source", icon="storage", tags=["source"])
class WindTurbines(pn.viewable.Viewer):
    n_rows = param.Integer(default=1000)

    @param.output(param.DataFrame)
    def dataset(self):
        ...
```

Each entry shows the icon, title and a summary of its ports, such as
`in: dataset, state, out: filtered (DataFrame)`, and is marked *Placed* once it
is on the canvas. The search field above the list (press `/` to focus it)
matches the title, description, tags and port names and types.

Project components are imported lazily. Until a module has been imported the
palette uses the `provides` and `requires` declared on `@register`, and a
Viewer without them is listed under *Components* until its ports are known.

### Preset entries

A component can offer ready-configured entries beside its own, for example
one per data source the host provides, so a node placed from one works
without typing its config. `palette_presets` takes the editor's `context` and
returns a list of dicts with a `title` and a `config`, and optionally a
`description`, a `tooltip`, `tags` and an `icon`:

```python
def table_presets(context):
    return [
        {
            "title": f"{table} ({ref})",
            "tooltip": f"Source {ref}, table {table}",
            "config": {"ref": ref, "table": table},
        }
        for ref, source in context.get("sources", {}).items()
        for table in source.tables
    ]


@register(component=True, title="Table", config=["ref", "table"], palette_presets=table_presets)
class Table(pn.viewable.Viewer):
    ...
```

Preset entries sit after their component with its icon and port summary, are
searched by their title and tags, and can be clicked, dragged or dropped on a
port like any entry. The command palette (`Ctrl`/`Cmd`+`K`) offers them too
while a dashboard is edited. The editor asks the callable again only when its
`context` changes, including a value added to or removed from a dict in it,
and searching filters the entries it has. A preset's config must be
JSON-serializable; give a preset an `id` when its title or config may change
while it should stay the same entry. A failing callable offers no presets.
Components found by scanning a project directory show their presets once
imported, which the app does when a dashboard is opened for editing.

### Built-in widgets and views

Every editor offers these components under the `Widgets/` id prefix, beside
the text views under `Text/`, unless `include_builtin_components=False`. Inputs are optional: a control uses its
configured options or bounds until something is wired to them.

| Component | Inputs | Output | Notes |
|-----------|--------|--------|-------|
| Select | `options` (List) | `selected` | Dropdown, optionally searchable |
| MultiChoice | `options` (List) | `selected` (List) | Any number of values |
| Radio Group | `options` (List) | `selected` | Radio buttons or a row of toggle buttons |
| Slider | `start`, `end` (Number) | `selected` (Number) | Integer or float, from the bounds and step |
| Range Slider | `start`, `end` (Number) | `selected` (Range) | Starts on the whole range and follows the bounds while it is whole |
| Number Input | `start`, `end` (Number) | `selected` (Number) | None while the field is empty |
| Date Range | `start`, `end` (Date) | `selected` (DateRange) | A tuple of two dates; see below |
| Toggle | | `checked` (Boolean) | A switch or a checkbox |
| Text Input | | `text` (String) | Sent on Enter or blur, or on every keystroke with `update_on: typing` |
| KPI | `value`, `reference` (Number) | | A headline number, its change from the reference and threshold colors |
| Image | `url` (String) | | An http(s), relative or base64 image data URL; other schemes are not shown |

Dashboards store config as JSON, so the Date Range keeps its dates as ISO
strings (`YYYY-MM-DD`). Its `default_period` is either relative to the day the
dashboard is opened (`last_7_days`, `last_30_days`, `last_90_days`,
`last_365_days`, `month_to_date`, `year_to_date`), `all` for the span between
its bounds, or `custom` for `custom_start` and `custom_end`. The KPI's `format`
is a Python format spec such as `,.0f` or `.1%`, and `thresholds` is a list of
ascending `[limit, color]` pairs. The inspector edits the format with a list of
named formats (whole number, one decimal, percent, ...) plus a checked custom
spec, and the thresholds as rows of a limit and a theme or custom color. Your own
components get the same editors by marking a param with
`metadata={"flowdash": {"widget": "format"}}` (a text param) or
`{"widget": "thresholds"}` (a list param).

---

## Slow components and the event loop

A page's `app` function is called in a worker thread, but a component's is not.
FlowDash calls a sync component function on the server's event loop when the
node is placed, so the outputs it publishes and the watchers it sets up on
`config` exist before saved edges are reconnected, and the dataflow graph, which
is not thread-safe, is only ever touched from one thread. While the function
runs, every session served by the process waits.

Keep sync component functions quick and move slow work (queries, file reads,
model loading) out of them:

- make the function `async def` and `await` the slow call, for example with
  `await asyncio.to_thread(load_data, ticker)`; FlowDash awaits it on the event
  loop when the tile renders,
- or bind the slow part to the inputs with `pn.bind` and let it run per update
  from an async function or generator.

Publish outputs (assign to `config`) from the event loop, not from a thread you
start yourself.

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

A Parameter can also be an output. Mark it with
`metadata={"flowdash": {"output": True}}` and it is published as an output port
instead of listed as an input. See
[Outputs from Parameters](define-ports.md#outputs-from-parameters).

### Updating a view in place

When a control changes, FlowDash sends the updates of every tile downstream to
the browser together, so they all change in the same frame. Each tile then
changes as much as its component's view does. A view that builds new panes on
every update, by returning them from a `pn.bind` or `param.depends` function,
is replaced as a whole: the tile empties for a moment and its charts, maps and
tables are drawn again from scratch. Build the panes once and bind what changes
to their parameters instead:

```python
class Summary(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    def _count(self, df):
        return "Waiting for data" if df is None else f"{len(df):,} turbines"

    def _spec(self, df):
        return None if df is None else {"data": {"values": df}, "mark": "bar"}

    def __panel__(self):
        return pn.Column(
            pn.pane.Markdown(pn.bind(self._count, self.param.turbines)),
            pn.pane.Vega(pn.bind(self._spec, self.param.turbines)),
        )
```

A Vega pane given a spec in which only the data values changed passes the new
values to the chart it already draws, and a Tabulator or deck.gl pane updates
its data the same way. Toggle `visible` to swap in a message while there is
nothing to show. The views of the
[wind energy example](../examples/wind-energy.md) follow this pattern.

Sending the updates together means the tiles fed by one output wait for each
other. When the change does not come from a widget in the browser, for example
from a `Viewer` control's parameter, an automatic refresh or a server started
with `--num-threads`, a slow sync consumer delays every other tile fed by the
same output until it returns, and its loading spinner never shows, since the
spinner is turned on and off before anything is sent. Make components that do
slow work per update `async`, as described in
[Slow components and the event loop](#slow-components-and-the-event-loop), so
the fast tiles update at once and the slow one shows its spinner while it
works. This applies per output: tiles fed by different outputs, or by one
component setting several outputs at once, are sent output by output.

### Previewing a component on the canvas

When the dashboard's `node_previews` setting is on (see
[Dashboard Settings](dashboard-settings.md#settings)), or the previews button
under the canvas is pressed, each card on the wiring canvas previews its
component's current value below its ports. It previews the first
output holding a value, or for a component without outputs the first input
holding one: a DataFrame as its first rows and columns, numbers as a
sparkline, a list as chips, a dict as a few `key: value` lines and anything
else as one line of text. Charts are named rather than drawn, e.g.
"HoloViews Curve of y over x" or "Bokeh figure".

A `Viewer` component can supply its own preview by defining
`__flowdash_preview__`:

```python
class Note(pn.viewable.Viewer):
    text = param.String(default="")

    def __flowdash_preview__(self):
        return self.text

    def __panel__(self):
        return pn.pane.Markdown(self.param.text)
```

The hook takes no arguments and is called on the live component instance. It
returns one of:

- `None` or a blank string, to keep the default preview of the component's ports.
- A `str`, shown on the card as plain text, up to three lines, and in the
  inspector's Data tab and the code panel's result as a caption. It is never
  rendered as HTML. On the card it is not rendered as Markdown either; in the
  Data tab and the code panel, text with Markdown structure (a heading, a list,
  a table, bold text, a link or inline code) is.
- Any other value, previewed as if an output held it: a DataFrame as a table,
  a list of numbers as a sparkline and so on.

The preview is refreshed when the component's ports or any of its parameters
change. Several changes in quick succession, such as a slider being dragged,
lead to one call shortly after the first, and the hook is not called at all
while previews are off. Keep it cheap and free of side effects. If it raises,
the error is logged and the default preview is shown. Function components
have no instance and so no hook.

### Showing what defines a component in the code panel

The code panel, under the canvas or in the sidecar's Spec tab, edits a node's
code or spec field. A component
without one, such as a data source configured by a name, can show what defines
it instead, read only, by defining `__flowdash_definition__`:

```python
class FileSource(pn.viewable.Viewer):
    path = param.String(default="data.csv")

    def __flowdash_definition__(self):
        return "yaml", f"type: file\ntables:\n  data: {self.path}\n"
```

The hook takes no arguments and returns a `(language, text)` tuple, where the
language is an Ace mode such as `"yaml"`, `"json"` or `"sql"`, or `None` to
keep the panel's "Nothing to edit" state. The text is shown with highlighting,
selectable and copyable, and is asked for again whenever the component's
outputs change. If the hook raises or returns anything else, the error is
logged and nothing is shown.

### Exporting a component in a report

**Export...** in the Report view writes the report as HTML, PDF, Word, Markdown or a
Jupyter notebook, with each node as it is now. Without help a node is read from the
panes its tile shows: Vega and Vega-Lite charts become images (through
`vl-convert-python`), tables and DataFrames their first 50 rows, Markdown and text
their text, and anything else, such as a map, a note saying it is left out. A
component can say what it exports by defining `__export__`:

```python
from panel_flowdash.export import KPI


class Revenue(pn.viewable.Viewer):
    total = param.Number(default=0)

    def __export__(self, ctx):
        return KPI(label="Revenue", value=f"${self.total:,.0f}")

    def __panel__(self):
        return pn.indicators.Number(value=self.param.total, name="Revenue")
```

The hook is called on the live instance once its inputs have settled, and returns one
block of `panel_flowdash.export` (`Heading`, `Text`, `KPI`, `Chart`, `Table`, `Image`,
`Code` or `Placeholder`), a list of them, or `None` to be read from its view.
`ctx.chart(spec)` draws a Vega-Lite spec as the format needs and `ctx.table(frame)`
takes a DataFrame's first `ctx.max_rows` rows; `ctx.format` names the format being
written. Write values as text yourself: the export never writes an object's `repr`,
and a hook that raises is left out with a note, its message never shown.

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
sole output, regardless of any other `@param.output` decorators or Parameters
declared as outputs in metadata.

---

## Opening a dialog from a component

A component that needs a modal, such as a confirmation before saving or a details view, should not create a `Dialog` inside its own tile. `panel_flowdash.util` provides one dialog per session instead, shared by every component on the page:

```python
import panel as pn
import panel_material_ui as pmui
from panel_flowdash import register
from panel_flowdash.util import confirm, open_dialog


@register(component=True, page=False)
def editor(config):
    def save(_event):
        ...

    def ask_to_save(_event):
        confirm("Save changes?", pn.pane.Markdown("This overwrites the stored copy."), on_confirm=save)

    def show_details(_event):
        open_dialog("Details", pn.Column(...))

    return pn.Row(
        pmui.Button(label="Save", on_click=ask_to_save),
        pmui.Button(label="Details", on_click=show_details),
    )
```

`open_dialog(title, content)` shows `content` and replaces whatever the dialog showed before. The user can dismiss it with the close button, the Escape key or a click outside it, and `close_dialog()` dismisses it from code.

`confirm(title, content=None, *, on_confirm, on_cancel=None)` asks a question. Opening it never runs anything. *Confirm* closes the dialog and then calls `on_confirm(event)`, so the handler can open another dialog. *Cancel* closes it and calls `on_cancel(event)` if given. A confirmation can only be answered with its buttons, and only the first click counts, so a double click does not run the action twice. Handlers may be coroutine functions. Set `confirm_label` and `cancel_label` to rename the buttons, and `width` (`"xs"` to `"xl"`) on either function to change the dialog's maximum width.

If a second `open_dialog` or `confirm` replaces an unanswered confirmation, neither of its handlers is called.

`FlowDashApp` places the dialog on every page and closes it when the user navigates to another page, which drops an unanswered confirmation. Outside a served session (a plain script, a unit test, a notebook) there is no dialog to show: `open_dialog` and `confirm` log a warning and do nothing, as `notify` falls back to the logger, so a `confirm` handler never runs without an answer. To embed the editor in your own page, see [Embed the Editor](embed-the-editor.md#where-the-page-dialog-goes).

---

## Knowing why a component is created

FlowDash creates a component whenever a node appears on the canvas, not only
when someone adds one. `placement_context()` tells the component why, from its
`__init__` or function body:

| Value       | When                                                                              |
|-------------|-----------------------------------------------------------------------------------|
| `"new"`     | The node was just added: from the palette, by `add_component`, or by the agent    |
| `"load"`    | A dashboard is being opened, imported or replaced, so the node has saved settings |
| `"restore"` | Undo, redo or restoring a version brings the node back as it was                  |
| `None`      | FlowDash is not creating the component, e.g. it is rendered on its own            |

A component that fills in a starting point of its own, such as a default
query, gives it to the node with `FlowDash.set_default`, not by setting its own
param: a param set on the instance only changes what that instance shows,
while `set_default` writes the node's config, which is saved with the
dashboard and comes back when it is reopened:

```python
import panel as pn
import param

from panel_flowdash import current_dashboard, current_node_id, placement_context, register


@register(component=True, config=["sql"])
class Query(pn.viewable.Viewer):
    sql = param.String(default="")

    def __init__(self, **params):
        super().__init__(**params)
        if placement_context() == "new":
            current_dashboard().set_default(
                current_node_id(), {"sql": "SELECT * FROM sales LIMIT 100"}
            )

    def __panel__(self):
        return pn.pane.Markdown(self.param.sql)
```

The rule is a default only for a `"new"` node whose config is still empty.
Nodes the agent adds, duplicates and nodes copied from Explore are `"new"` too
but arrive configured, so `set_default` only fills fields still at their
declared default. A node that `replace_yaml` adds reports `"load"`, and
`set_default` changes nothing while a dashboard is loaded or a version
restored, nor for viewers or in a preview. Its return value says whether a
field took the default.

The default is part of the edit that brought it about, so one undo takes back
both. Set in `__init__` or a function body, it belongs to the placement. A
component can also wait for its inputs, e.g. to derive a default from the
columns of a table it is connected to, and call
`set_default(editor.node_id(self), ...)` when they arrive: during a connection
the default is recorded with it, and when an input arrives on its own the
default is recorded as a config edit once the session's current event is
handled.

`FlowDash.code_panel_node` holds the id of the node the code panel shows, under
the canvas or in the sidecar's Spec tab, or `None`; bind to `editor.param.code_panel_node` to, say, hide
an "Edit spec" button of yours while the panel already shows the node.

The reason belongs to the creation of that one node: a node the component adds
while it is created is `"new"` again, and placements in other sessions and
threads have their own. It is a context variable, so code running elsewhere
sees `None`: a thread the component starts, and work handed to
`loop.run_in_executor` or a `ThreadPoolExecutor`, which do not copy the
context. `asyncio.to_thread` copies it, so a function it runs sees the reason.
When in doubt, read it once in `__init__` and pass the value on. An async
function body sees it while it runs, but a callback or an output that runs
later does not.

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
- the server session the editor was created in ends, or `FlowDash.dispose()`
  is called
- placing the node fails after the instance was created, e.g. because
  `__panel__` raised

By the time it runs, the node's view has left the canvas and the tile grid, and
the node is disconnected from the dataflow graph: its inputs no longer receive
values and anything it sets on its outputs no longer reaches downstream nodes. It may be `async`, in which case it is awaited
on the event loop. An exception raised by `dispose()` is logged and does not
stop the node, or any other node, from being removed.

Changing a node's configuration updates the existing instance rather than
replacing it, so it does not trigger `dispose()`. The hook only applies to
Viewer components; a decorated function is called rather than instantiated, so
FlowDash holds no object of its own to dispose.

### When the session ends

An editor created inside a server session registers itself with that session
and disposes its components when the session ends. At that point Bokeh has
already destroyed the session's Document and `pn.state.curdoc` is `None`, so
`dispose()` should only release resources and not touch the UI. Periodic
callbacks registered with the document through `pn.state.add_periodic_callback`
have already been stopped by Panel by then.

The editor is tied to the session it was created in. An editor built inside one
session and shared with others, e.g. through `pn.state.as_cached`, has its
components disposed when that first session ends. An editor meant to be shared
across sessions must be built outside a session, or its owner must call
`FlowDash.dispose()`.

Nothing is disposed automatically in a notebook, under pyodide, or for an editor
built outside a server session. Call `FlowDash.dispose()` yourself there when
the editor is no longer needed; it clears the canvas and disposes every placed
component, and leaves an empty editor that can still be used.

---

## Renaming components and ports

Saved dashboards refer to components by id (`<Section>/<name>`, derived from
the module path) and to ports and config fields by name. Moving a module,
renaming a component or renaming a port would leave old dashboards pointing
at names that no longer exist. Declare the former names instead and old
dashboards keep loading:

```python
@register(
    component=True,
    aliases=["Finance/stock_filter"],   # the id before the module moved
    port_aliases={"ticker": "symbol"},  # the input used to be called "ticker"
    requires=[{"key": "symbol", "type": "str"}],
)
def app(config):
    ...
```

A Viewer can also declare a former name on the Parameter itself:

```python
class Report(pn.viewable.Viewer):
    symbol = param.String(metadata={"flowdash": {"aliases": ["ticker"]}})
```

Former names are resolved when a dashboard is loaded or imported: component
ids on its items, port names on its edges and keys of the item's config. The
next save writes the current names. A registered id or a live port name always
wins over an alias, so an alias can never redirect something that still
exists. Keep `aliases` literal (a list of strings) so that a project directory
scan can see it before the module is imported.

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
- Modules are imported as `Section.module_name`, or as
  `package.Section.module_name` when the project is served as an importable
  package (see [Serving a package](serve-project.md#serving-a-package)).

---

## Components from an installed library

A package can ship FlowDash components for other projects to use. Name the
library and the editor offers its components next to the project's own:

```python
# my_project/__init__.py, read by `flowdash serve`
component_libraries = ["my_library"]
```

```bash
flowdash serve my_project/ --component-library my_library
```

```python
FlowDashApp(project_dir="my_project/", component_libraries=["my_library"])
```

Each entry is a module name or `module:attribute`. A module offers what its
`__flowdash_components__` attribute holds, or, without one, every object it
exports that `@register` marks as a component: the names in its `__all__`, or
without that, the public names defined in the module or its submodules, so
components it merely imports from elsewhere are not offered again. A facade
module that only re-exports components lists them in `__all__` or
`__flowdash_components__`. `module:attribute` names a mapping of component id
to component, a list of components or a single one. Ids come from the
mapping's keys, or else are `<module>/<name>`, where `<module>` is the last
part of the name of the module that defines the component.

To publish components from your own package, decorate them with
`@register(component=True)` and declare the ids you want saved dashboards to
store:

```python
# my_library/__init__.py
from .views import RevenueChart, top_customers

__flowdash_components__ = {
    "Revenue/chart": RevenueChart,
    "Revenue/top_customers": top_customers,
}
```

Libraries add components only: a component registered with `page=True` is not
served as a page. Permissions from `@register` and the editor's
`authorize_component` apply to them as to the project's components. Every
object a library names must carry `@register` metadata, and the app refuses to
start when a library does not import, offers nothing, gives two components one
id, or offers an id that the project, the built-in components or another
library already uses, since letting one replace the other would change what
saved dashboards load. The same goes for
[former ids](#renaming-components-and-ports): a library component's `aliases`
must not name an id or former id of another component, and its id must not be
another component's former id. To resolve a clash, give the library's
components other ids through a mapping, for example `"my_library:COMPONENTS"`.

`flowdash serve` puts the project directory first on `sys.path`, so a module in
the project with the same name as a library, such as `my_project/my_library.py`,
is imported in its place.
