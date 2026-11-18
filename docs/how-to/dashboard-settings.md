# Dashboard Settings and Refresh

Component config is stored per tile. Some values belong to the dashboard as a
whole instead, such as how often live data refreshes. FlowDash keeps these in
`FlowDash.settings`, saves them with the dashboard, and offers every component a
shared `refresh` signal.

---

## Settings

`FlowDash.settings` is a `param.Parameterized` instance filled from the loaded
dashboard. Its built-in parameters include `description`, a sentence or two on
what the dashboard shows (empty by default); `refresh_period`, the number of
seconds between automatic refreshes (the default of 0 turns automatic refreshing
off); and `viewers_can_inspect`, which opens output inspection to people who may
only view the dashboard (see [Inspecting outputs](embed-the-editor.md#inspecting-outputs)).

Two more control how tiles look in view mode. `tile_cards` (on by default) draws
each tile as a framed card; turn it off to render tiles as the components' own
views. `tile_titles` (off by default) adds a row with the tile's title and the
component's description to each card. Without it a tile's actions take no space:
Inspect, Expand and, when there is something to put in it, a More menu float
over the tile's top-right corner on hover or keyboard focus. Set the defaults
for dashboards that have not saved these settings with
`FlowDash(tile_cards=..., tile_titles=...)`, or through `editor_options` in a
`FlowDashApp`. `controls_position` (`"top"` or `"left"`) places the dashboard's
controls above the tiles or in a panel to their left, defaulting to the editor's
`controls_position` (see [Layout & sizing](layout-and-sizing.md#controls)).
`header_layout` (`"compact"` or `"full"`) lays out the dashboard's title, health
and actions on one line, in a `FlowDashApp`'s top bar, or in a row of their own
with a large title over the description and the refresh time, defaulting to the
editor's `header_layout` (see [Serve a Project](serve-project.md#navigation)).
`node_previews` (off by default) previews each component's current value inside
its card on the wiring canvas, such as the first rows of a table or a sparkline
of a series; without it a card summarizes the value in one line. It defaults to
the editor's `node_previews`. The button beside the minimap button under the
canvas shows or hides the previews for the current session only, starting from
the setting, so it never marks the dashboard as changed (see
[Previewing a component on the canvas](register-components.md#previewing-a-component-on-the-canvas)).

```python
editor = FlowDash(components, store="dashboards.db")
editor.load("sales")
editor.settings.refresh_period  # 30.0, if the saved dashboard sets it
editor.settings.refresh_period = 10  # marks the dashboard as having unsaved changes
editor.save()  # writes the current values with the dashboard
```

### The settings dialog

In edit mode the dashboard bar has a **Settings** button that opens the
**Dashboard settings** dialog; in view mode it is in the bar's overflow menu, and
in a `FlowDashApp` the **Share** button opens the same dialog on its Sharing
section. A list on the left switches between its sections:

- **General**: the dashboard's name, its description, **Header**
  (`header_layout`), **Controls** (`controls_position`), **Preview values on the
  canvas** (`node_previews`), **Frame tiles** and **Show tile titles** (`tile_cards`
  and `tile_titles`), any
  [settings of your own](#add-your-own-settings), and **Viewers can inspect
  outputs**, which is only offered when the editor has `inspect_outputs` on. Only
  people who may administer the dashboard can rename it; for everyone else, and
  for a standalone `FlowDash`, the name is read-only.
- **Refresh**: presets for the refresh period (Off, 1 min, 5 min, 15 min, 1 hour)
  and **Custom** for any number of seconds. Below them the dialog says when the
  next automatic refresh runs and how long the last refresh took, once it knows.
- **Sharing** and **Delete**, for people who may administer the dashboard in a
  `FlowDashApp` (see [Manage Permissions](manage-permissions.md#restricting-dashboards)).

The dialog edits a copy of the settings. **Cancel** discards the changes and
**Save settings** applies them to `editor.settings`. When nothing else on the
dashboard is unsaved, it also saves the dashboard right away, so a setting changed
on its own does not wait for the toolbar's **Save**. When the canvas has unsaved
changes, the settings join them and are saved with the next **Save**; the dialog
says so in its footer. Settings is not offered to people who may only view the
dashboard.

Loading a dashboard applies its stored values. A parameter the dashboard does not
store takes its default, so one dashboard's settings never carry over into the
next. A stored key with no matching parameter is ignored (and logged at debug
level), and saving keeps it, so a dashboard authored with extra settings survives
being opened and saved by an editor that does not know them. A stored value that
the parameter rejects is logged as a warning and replaced by the default.

### Add your own settings

Subclass `DashboardSettings` and hand the class to the editor:

```python
import param
from panel_flowdash import DashboardSettings, DashboardStore, FlowDash
from panel_flowdash.app import FlowDashApp


class SalesSettings(DashboardSettings):
    currency = param.Selector(default="EUR", objects=["EUR", "USD", "GBP"])


editor = FlowDash(components, settings_class=SalesSettings)
app = FlowDashApp(
    project_dir="my_project",
    store=DashboardStore("my_project/dashboards.db"),
    settings_class=SalesSettings,
)
```

The new parameters appear in the General section of the settings dialog and are
stored with the dashboard.
Values must be JSON serializable, since the stores write them as JSON. For
`flowdash serve`, define the class in the project's `__init__.py` and assign it to
`settings_class` there:

```python
# my_project/__init__.py
settings_class = SalesSettings
```

See [Project configuration](serve-project.md#project-configuration) for the other
options a served project can set this way.

---

## Refresh

`FlowDash.refresh` is a `param.Event`. It fires:

- every `refresh_period` seconds while a dashboard is shown and the period is above 0,
- when the user clicks **Refresh**, and
- whenever you call `editor.param.trigger("refresh")`.

The timer is a single periodic callback per editor. It starts once the page has
loaded, restarts when the period changes, stops at 0, and stops when the dashboard
is unloaded, the editor is disposed or the session ends. In a `FlowDashApp`,
navigating to a page or the launcher unloads the dashboard, and a dashboard whose
load is overtaken by a newer navigation never applies its settings.

In view mode the dashboard bar shows a **Refresh** split button whose tooltip
says "Refreshed HH:MM, auto-refresh every 15 min" (the full header layout also
shows it under the title). The button is shown whether or
not a period is set, because a dashboard that does not poll can still be refreshed
by hand. Its menu picks the period (Off, every minute, 5 min, 15 min or hour) for
the current session: it restarts the timer without marking the dashboard as
changed, and is only saved if someone saves the dashboard afterwards.

Next to the dashboard's title an info button shows the `description` setting,
when set (the full header layout shows the text under the title), and a health
chip says "5/5 up to date", or gives a count such as
"1 failed · 1 waiting" in red or amber. A tile has failed while one of its inputs
holds a failure carried from upstream (see `propagate_failures`), and waits
while a required input is unset. The chip opens a popover with the pipeline,
source to transforms to views with a status dot per stage, and, with any such
tile, how many are affected and a **Show issues** button that scrolls to them
and outlines them briefly.

---

## Use settings and refresh from a component

A component calls `current_dashboard()` while it is being created, in `__init__` for a
`Viewer` or in the function body for a function component. It returns the editor
showing the component, so `settings` and `refresh` are reached without importing
anything from the application. Outside an editor, for example when the component
is rendered on its own, it returns `None`, so a component that must also run
standalone has to check for that.

```python
import pandas as pd
import panel as pn
from panel_flowdash import current_dashboard, register


def load_orders() -> pd.DataFrame:
    ...  # query the database


@register(component=True, page=False, title="Orders")
class app(pn.viewable.Viewer):
    def __init__(self, **params):
        super().__init__(**params)
        refresh = current_dashboard().param.refresh
        self._table = pn.widgets.Tabulator(pn.bind(self._fetch, refresh), disabled=True)

    def _fetch(self, _refresh):
        return load_orders()

    def __panel__(self):
        return self._table
```

The table loads once when the tile is created and again on every refresh. Panel
re-runs `_fetch` because the event is bound to the widget.

Whatever a component subscribes to on the editor or on `settings` while it is
being created is released when its tile is removed, the canvas is cleared or
another dashboard is loaded. The body of an `async` function component counts as
creation too, including the parts that run after an `await`. A subscription added later, for example from a
button handler, is yours to remove with `dashboard.param.unwatch(...)`.

Settings are applied before a dashboard's components are created, so a component
can read `current_dashboard().settings` in `__init__`. Read them again, or bind to
`dashboard.settings.param.<name>`, if the component should follow later changes.

After creation, `current_dashboard()` still finds the editor for the current
session, which is the latest one created in it. Prefer to capture the editor when the
component is created if a page holds more than one.

## Hand resources to components

A host that owns resources components need, such as database connections or data
sources, passes them to the editor as its `context`, a dict keyed by name. Prefix
the keys with your package's name so hosts and libraries do not collide; Lumen, for
example, keeps its source bindings under `"lumen.sources"`. A component reads them
with `dashboard_context()`, which returns a read-only view of the context of the
editor showing it, or an empty one outside an editor:

```python
from panel_flowdash import FlowDash, dashboard_context, register


@register(component=True, page=False, title="Tables", provides=["tables"])
def tables(config):
    source = dashboard_context().get("my_app.source")
    config.tables = source.get_tables() if source is not None else []
    return "Tables"


editor = FlowDash([tables], context={"my_app.source": my_source})
```

The editor keeps a shallow copy of the dict it is given. Add to `editor.context`
to make more resources available later; the view a component holds follows such
additions. Previews of the assistant run in a separate editor with a copy of the
context, so the components they create find the same resources. `FlowDashApp` passes
`editor_options={"context": ...}` on to its editor.
