# Embed the Editor

`FlowDashApp` is the full application: routing, navigation, pages, a launcher,
authorization. Sometimes you want only the interesting part, the wiring canvas
and the layout editor, dropped into an app you are already building. That is
`FlowDash`.

```python
import panel as pn
from panel_flowdash import FlowDash, register


@register(component=True, page=False, title="Ticker", provides=[{"key": "ticker", "type": "str"}])
def ticker_select(config):
    ...


@register(component=True, page=False, title="Chart", requires=[{"key": "ticker", "type": "str"}])
def price_chart(config):
    ...


editor = FlowDash([ticker_select, price_chart])
editor.servable()
```

No project directory, no `flowdash serve`, no routes. `FlowDash` is a Panel
`Viewer`, so it composes into any layout, template or notebook.

---

## Declaring components

The `components` argument accepts whatever is convenient:

```python
# A single component
FlowDash(ticker_select)

# A list, mixing decorated functions and Viewer subclasses
FlowDash([ticker_select, price_chart, MyViewerComponent])

# Explicit ids, when you care what gets persisted
FlowDash({"Market/ticker": ticker_select, "Market/chart": price_chart})

# A project directory, scanned the way `flowdash serve` scans it
FlowDash("my_project")

# Any mix of the above
FlowDash(["my_project", ticker_select, {"Market/chart": price_chart}])
```

Components need not be decorated at all. A plain `Viewer` subclass registers as
a component, with its params as input ports and its `@param.output()` methods as
output ports:

```python
import param
from panel.viewable import Viewer


class Shouter(Viewer):
    ticker = param.String()

    @param.output(param.String)
    def shouted(self):
        return self.ticker.upper()

    def __panel__(self):
        return self.ticker
```

Without explicit ids, a component's id is derived from the module that defines
it, so `ticker_select` in `market.py` becomes `market/ticker_select`. Ids are
persisted with the dashboard, so pass a mapping of explicit ids if the defining
module might move.

---

## Building a dashboard in code

Everything the canvas does by drag-and-drop is available as a method:

```python
editor = FlowDash({"Market/ticker": ticker_select, "Market/chart": price_chart})

src = editor.add_component("Market/ticker", position=(0, 0))
dst = editor.add_component("Market/chart", position=(350, 0))
editor.connect(src, "ticker", dst, "ticker")
```

`add_component` returns the new instance's id, which is what `connect`,
`disconnect` and `remove_component` take. `connect` returns `True` on success,
or a message explaining the rejection, so wiring mistakes surface as values
rather than exceptions:

```python
result = editor.connect(src, "ticker", dst, "ticker")
if result is not True:
    print(f"Rejected: {result}")   # unknown port, type mismatch, cycle, occupied input
```

The live dataflow is reachable through `editor.graph`, which is useful in tests
and for driving the dashboard from outside:

```python
editor.graph.get_state(src).ticker = "AAPL"
```

---

## Persistence

With no `store`, the editor is ephemeral: `save` builds the model and hands it
back for you to persist however you like.

```python
model = editor.save(title="Sales Overview")
```

Pass a `store` and the editor persists for you. A path is coerced into a SQLite
store; `MemoryDashboardStore` keeps everything in process, which is what you
want in tests.

```python
from panel_flowdash import FlowDash, MemoryDashboardStore

editor = FlowDash(components, store="dashboards.db", user="alice")
editor = FlowDash(components, store=MemoryDashboardStore())
```

Then the usual lifecycle applies:

```python
editor.new_dashboard("Sales Overview")   # create and start empty
editor.save()                            # persist the canvas
editor.load("Sales Overview")            # by id or title
```

To move a dashboard between editors, or to persist to something that is not a
`BaseDashboardStore` at all, go through the model:

```python
model = editor.to_model(title="Snapshot")
other_editor.load_model(model)
```

`to_model` returns a detached `DashboardModel`, so later edits to the canvas do
not mutate it. Components a model references but the editor does not offer are
skipped with a warning rather than aborting the load.

Watch `dirty` to prompt before discarding work, and `saved` to react to a
successful save:

```python
editor.param.watch(lambda e: print("unsaved changes" if e.new else "clean"), "dirty")
editor.param.watch(lambda e: print("saved"), "saved")
```

---

## Editing, viewing and previewing

Three params control what the user sees:

| Param | Default | Effect |
|-------|---------|--------|
| `mode` | `"wiring"` | `"wiring"` shows the ReactFlow canvas, `"dashboard"` the tile grid. |
| `editable` | `True` | When `False` the toolbar is hidden and the grid is locked, giving a pure dashboard view. |
| `preview` | `False` | Locks the grid without leaving edit mode, to see the dashboard as an end user does. |

So a read-only dashboard viewer is just:

```python
FlowDash(components, store="dashboards.db", dashboard="Sales Overview", editable=False)
```

Passing `dashboard=` at construction loads it immediately, either as a
`DashboardModel` or, when a store is configured, as an id or title.

Set `read_only=True` to refuse saves while still letting the user rearrange the
canvas; `save` then raises `RuntimeError`. This is the seam for your own
authorization logic, and it is exactly what `FlowDashApp` uses to enforce
per-dashboard permissions.

---

## Fitting it into your own layout

The built-in toolbar can be hidden with `toolbar=False`, or extended with your
own controls through `toolbar_extra`:

```python
import panel_material_ui as pmui

share = pmui.Button(icon="share", variant="outlined")
editor = FlowDash(components, toolbar_extra=[share])
```

Components registered with `sidebar=True` are kept out of the tile grid and
published on the `sidebar` param instead, for you to render wherever your layout
wants them:

```python
sidebar = pn.Column()
editor.param.watch(lambda e: sidebar.param.update(objects=list(e.new)), "sidebar")

pmui.Page(main=[editor], sidebar=[sidebar]).servable()
```

Set `notifications=False` to route the editor's user-facing messages to the
logger instead of Panel notifications, which is what you want in a notebook or
under test.

---

## Loading components off the event loop

Components handed over as live objects need no import, so the editor is usable
the moment it is constructed. Components discovered by scanning a directory are
imported lazily on first use, which on a live server would block the event loop.
Await them explicitly during startup instead:

```python
editor = FlowDash("my_project")
pn.state.onload(editor.ensure_components_loaded_async)
```
