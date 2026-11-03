# Dashboard Settings and Refresh

Component config is stored per tile. Some values belong to the dashboard as a
whole instead, such as how often live data refreshes. FlowDash keeps these in
`FlowDash.settings`, saves them with the dashboard, and offers every component a
shared `refresh` signal.

---

## Settings

`FlowDash.settings` is a `param.Parameterized` instance filled from the loaded
dashboard. It has one built-in parameter, `refresh_period`, the number of seconds
between automatic refreshes. The default of 0 turns automatic refreshing off.

```python
editor = FlowDash(components)
editor.load("sales")
editor.settings.refresh_period  # 30.0, if the saved dashboard sets it
editor.settings.refresh_period = 10  # marks the dashboard as having unsaved changes
editor.save()  # writes the current values with the dashboard
```

In edit mode the toolbar has a **Settings** button that opens a form generated from
the settings object. Changes mark the dashboard dirty like any other edit. The
button is not shown in view mode and is disabled when the dashboard cannot be
saved.

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
from panel_flowdash import DashboardSettings, FlowDash, FlowDashApp


class SalesSettings(DashboardSettings):
    currency = param.Selector(default="EUR", objects=["EUR", "USD", "GBP"])


editor = FlowDash(components, settings_class=SalesSettings)
app = FlowDashApp(project_dir="my_project", settings_class=SalesSettings)
```

The new parameters appear in the settings form and are stored with the dashboard.
Values must be JSON serializable, since the stores write them as JSON. For
`flowdash serve`, pass the class through `FlowDashApp.build_routes(...,
settings_class=SalesSettings)` in your own launcher.

---

## Refresh

`FlowDash.refresh` is a `param.Event`. It fires:

- every `refresh_period` seconds while a dashboard is shown and the period is above 0,
- when the user clicks **Refresh**, and
- whenever you call `editor.param.trigger("refresh")`.

The timer is a single periodic callback per editor. It starts once the page has
loaded, restarts when the period changes, stops at 0, and stops when the dashboard
is unloaded or the session ends.

In view mode, and in the edit-mode dashboard preview, a **Refresh** button and a
"Refreshed HH:MM:SS" indicator sit above the grid. The button is shown whether or
not a period is set, because a dashboard that does not poll can still be refreshed
by hand.

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
another dashboard is loaded. A subscription added later, for example from a
button handler, is yours to remove with `dashboard.param.unwatch(...)`.

Settings are applied before a dashboard's components are created, so a component
can read `current_dashboard().settings` in `__init__`. Read them again, or bind to
`dashboard.settings.param.<name>`, if the component should follow later changes.

After creation, `current_dashboard()` still finds the editor for the current
session, which is the latest one created in it. Prefer to capture the editor when the
component is created if a page holds more than one.
