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

# A project directory that is also an importable package
from pathlib import Path
from panel_flowdash.registry import build_registry
FlowDash(build_registry(Path("catalogue"), package="catalogue"))

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
| `editable` | `True` | When `False` the side panel is hidden and the grid is locked, giving a pure dashboard view. |
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

## Inspecting outputs

On the wiring canvas, clicking or hovering a port opens a popup with the value it currently carries. A viewer of a dashboard has no canvas, which is when someone asks where a number comes from. Set `inspect_outputs=True` to give each tile in the dashboard grid a small info button in its top-right corner:

```python
editor = FlowDash(
    components, store="dashboards.db", dashboard="Sales Overview", editable=False, inspect_outputs=True
)
```

In a `FlowDashApp`, pass it through `editor_options={"inspect_outputs": True}`. It is off by default, so existing dashboards look the same.

The dialog shows values the tile itself may not display, such as the full DataFrame behind a chart, so by default only people who may edit the dashboard get the button. The editor treats a `read_only` session as a viewer and hides the button there; `FlowDashApp` sets `read_only` for everyone who is not the owner, an editor or an administrator of the dashboard (see [Manage Permissions](manage-permissions.md)). To open inspection to viewers of one dashboard, turn on its **Viewers can inspect outputs** setting, `settings.viewers_can_inspect`, which is saved with the dashboard like any other [setting](dashboard-settings.md). A standalone `FlowDash` that is not `read_only` shows the button to everyone who sees it.

The button appears on tiles whose component has at least one output port, and opens one dialog per editor, titled with the component's title. The dialog lists every output with its label, type and current value. The values are read when the button is clicked, so the dialog is a snapshot; click again to refresh it. Values are rendered exactly as in the port popups: through `value_repr` when you set one, otherwise as a table preview for a DataFrame, as JSON for other values, and as a placeholder when nothing has been published yet.

```python
editor = FlowDash(
    components,
    inspect_outputs=True,
    value_repr=lambda value: f"{value:,.2f}" if isinstance(value, float) else value,
)
```

The button is hidden while tiles are being arranged, because it would sit under the tile's close handle, and shown in view mode and in preview. Edit mode keeps its port popups. Components marked `sidebar=True` are not tiles and get no button. A tile still showing its placeholder because a required input is missing keeps its button, and the dialog then starts with a note naming the inputs it is waiting for.

### Explaining an output

A `Viewer` component can add its own explanation, such as the inputs and the function that produced a value, by defining `__flowdash_inspect__`:

```python
class Total(pn.viewable.Viewer):
    rows = param.DataFrame()

    @param.output(param.Number)
    @param.depends("rows")
    def total(self):
        return self.rows["amount"].sum()

    def __flowdash_inspect__(self, port):
        if port == "total":
            return f"Sum of `amount` over {len(self.rows)} rows."
        return None

    def __panel__(self):
        ...
```

The hook is called once per output port while the dialog is built, on the live component instance. Return anything `pn.panel` accepts, or `None` for an output you have nothing to add to. The result is shown below the value, which always stays visible. If the hook raises, the error is logged and a short error line replaces the explanation for that output, without affecting the others. Function components have no instance and so no hook.

---

## Fitting it into your own layout

The dashboard bar runs across the top of the editor. In view mode (`editable=False`) it shows the dashboard title, when it was last refreshed, a *Refresh* button, a *View*/*Edit* control (unless the editor is `read_only`) and a menu with *Download*. In edit mode it shows the title with the save state (*Unsaved changes*, *Saved 16:24* or *Read-only*), the *Dataflow*/*Layout* tabs that switch `mode`, *Settings*, a menu with *Download*, *Import file* and *Clear canvas*, and *Discard* (reloads the saved dashboard after asking), *Save* (enabled while there are unsaved changes) and *Done*, which returns to view mode and asks first when there are unsaved changes. Hide the bar with `toolbar=False`, leaving only the component palette, or extend it with your own controls through `toolbar_extra`, which places them in the bar's actions in both modes. Small text buttons match the built-in actions:

```python
import panel_material_ui as pmui

share = pmui.Button(label="Share", icon="share", variant="text", size="small")
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

### Where the page dialog goes

Components open modals with `open_dialog` and `confirm` from `panel_flowdash.util` (see [Register Components](register-components.md#opening-a-dialog-from-a-component)). Both use one `Dialog` per session, which `FlowDash` lays out in its own workspace, so an embedded editor needs nothing more.

If your page already shows the dialog, or you want it somewhere else, pass `include_page_dialog=False` to the editor and place `page_dialog()` yourself. A dialog can appear in only one layout, so do this for all but one editor on a page. Keep it out of a `Page` header, where Material UI recolors its children for the header:

```python
from panel_flowdash.util import page_dialog


def app():
    editor = FlowDash(components, include_page_dialog=False)
    return pmui.Page(main=[editor, page_dialog()])
```

The dialog belongs to the session that is current when the editor is built, so construct `FlowDash` inside the function Panel calls per session, not at import time.

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

---

## Configuring the editor inside FlowDashApp

`FlowDashApp` builds one `FlowDash` per session and reuses it for every
dashboard. Pass any `FlowDash` parameter to it through `editor_options`, the
editor counterpart of `page_options`:

```python
from panel_flowdash import DashboardStore
from panel_flowdash.app import FlowDashApp

app = FlowDashApp(
    project_dir="my_project",
    store=DashboardStore("my_project/dashboards.db"),
    editor_options={"value_repr": summarize, "popup_trigger": "click"},
)
```

Entries override the defaults the app derives from its own parameters, such as
`breakpoints` and `notifications`. `toolbar_extra` items are placed after the
app's *Share* button rather than replacing it. `components`, `store`, `user`
and `include_page_dialog` are always supplied by the app (which lays out the
[page dialog](#where-the-page-dialog-goes) itself) and raise a `ValueError` if
given here, while
`dashboard`, `editable` and `read_only` are accepted but then driven by the
app's routing and permissions. Both options are read once at construction, so
changing them afterwards has no effect.

Set `notifications` and `include_builtin_components` on `FlowDashApp` rather
than in `editor_options`: the app reads its own values too, and overriding them
only on the editor leaves the two out of sync. When the app is served through
`build_routes`, every session receives the same `editor_options` dict, so any
Panel object in it (for example a `toolbar_extra` button) would be shared by
all sessions; create such objects in an `editor_class` subclass instead, as
shown below.

To substitute your own editor, subclass `FlowDash` and pass it as
`editor_class`; `editor_options` is then passed to that class:

```python
from panel_flowdash import DashboardStore, FlowDash
from panel_flowdash.app import FlowDashApp


class MyFlowDash(FlowDash):
    ...


app = FlowDashApp(
    project_dir="my_project",
    store=DashboardStore("my_project/dashboards.db"),
    editor_class=MyFlowDash,
)
```

A project served with `flowdash serve` sets `editor_class` and `editor_options`
as module attributes in its `__init__.py`, see
[Project configuration](serve-project.md#project-configuration).
