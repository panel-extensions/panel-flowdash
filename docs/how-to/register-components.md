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
| `singleton` | `bool` | `False` | Only allow one instance on a dashboard. |
| `config_schema` | `type \| dict` | `None` | Design-time configuration definition: a `param.Parameterized` subclass, a Pydantic model, or a JSON-Schema-like dict. See [Configure nodes](configure-nodes.md). |
| `config` | `list[str]` | `[]` | For Viewer components, names the params that are design-time configuration rather than input ports. See [Configure nodes](configure-nodes.md). |
| `config_editor` | `callable` | `None` | Custom editor callable for the node's configuration form. See [Configure nodes](configure-nodes.md). |
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
| Views | No outputs, including the built-in KPI and Image |
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

### Built-in widgets and views

Every editor offers these components under the `Widgets/` id prefix, unless
`include_builtin_components=False`. Inputs are optional: a control uses its
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
ascending `[limit, color]` pairs.

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
