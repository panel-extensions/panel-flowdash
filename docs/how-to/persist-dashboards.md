# Persist Dashboards

FlowDash stores dashboard configurations: which components are placed, how they
are connected, and their grid layout. A store handles all CRUD operations.
Three backends are provided: `DashboardStore` (SQLite, the default),
`JSONFolderDashboardStore` (one JSON file per dashboard) and
`MemoryDashboardStore` (nothing persisted, for notebooks and tests). They all
implement `BaseDashboardStore`, so you can also bring your own.

---

## Create a store

```python
from panel_flowdash import DashboardStore

store = DashboardStore("path/to/dashboards.db")
```

The database file is created automatically with the required schema. WAL mode
is enabled for concurrent read access. The store keeps one connection open per
thread and closes the connections of threads that have exited; call
`store.close()` to release the rest. A server that forks worker processes after
building the store (`flowdash serve --num-procs`) is safe: each process opens
its own connections and never closes the ones it inherited. A row that cannot be decoded is
skipped and logged once instead of breaking the listing.

---

## Keep dashboards as JSON files

```python
from panel_flowdash import JSONFolderDashboardStore

store = JSONFolderDashboardStore("path/to/dashboards")
```

Each dashboard is saved as `<dashboard_id>.json` in the folder. The files are
indented, human-readable JSON, so you can review changes in a pull request, keep
them in version control next to the project that serves them, or edit them by
hand. The folder is scanned on every request, so files added or changed outside
the application appear without a restart. Only files whose size, modification
time or inode changed since the last listing are parsed again, so the models a
listing returns are shared with that cache and should be treated as read-only;
`load_dashboard` and `load_for_access` return a private copy.

Dashboards created through the store get an id derived from their title
(`Sales Overview` becomes `sales-overview`, then `sales-overview-2`), which keeps
file names and `/dash/<id>` URLs readable. Ids must be usable as file names:
letters, digits, `.`, `_` and `-`, starting with a letter or digit.

A file that cannot be parsed (invalid JSON or UTF-8, nesting too deep to decode,
an id, owner or title that is not a string), or whose `dashboard_id` differs from
its file name, is skipped and logged once, so one bad file does not take the
others down.

To ship a fixed set of dashboards, open the folder read-only:

```python
store = JSONFolderDashboardStore("dashboards", read_only=True)
```

Writes then raise `PermissionError` (also a `DashboardStoreError`, see below),
and nobody can administer a dashboard, so the editor offers no way to modify,
rename, share or delete one.

Files are written atomically. Several server processes can share one folder:
creating a dashboard claims its id exclusively, so two users creating a
dashboard with the same title at the same moment get `sales` and `sales-2`, and
saving refuses (with a `DashboardStoreError`) to replace a file that belongs to
another user, which also covers ids that differ only in case on a
case-insensitive file system. Two saves of the same dashboard at the same moment
still resolve last-writer-wins.

---

## Dashboard data model

A persisted dashboard consists of three parts:

### DashboardItem

Each placed component instance:

```python
from panel_flowdash import DashboardItem

item = DashboardItem(
    instance_id="filter_1",
    component_id="Analytics/filter",
    x=100,      # ReactFlow canvas x position
    y=200,      # ReactFlow canvas y position
    config={},  # Instance-specific configuration
)
```

`x` and `y` store the node editor canvas position. Grid layout (widths,
heights, visibility) is stored separately in `tile_layout`.

### DashboardEdge

Each connection between component ports:

```python
from panel_flowdash import DashboardEdge

edge = DashboardEdge(
    source="filter_1",
    source_port="filtered",
    target="chart_1",
    target_port="data",
)
```

An edge that reshapes its value also carries a `transform`, the list of steps it
applies. See [Transform Edges](transform-edges.md#how-transforms-are-stored).

### DashboardModel

The complete dashboard:

```python
from panel_flowdash import DashboardModel

model = DashboardModel(
    dashboard_id="abc123",
    user_id="user@example.com",
    title="My Dashboard",
    items=[item],
    edges=[edge],
    tile_layout=[
        {"id": "filter_1", "w": 4, "h": 3, "x": 0, "y": 0},
        {"id": "chart_1", "w": 8, "h": 4, "x": 4, "y": 0},
    ],
)
```

The `tile_layout` list stores the grid positions and sizes for the rendered
dashboard view (separate from the node editor canvas positions in items).
`reference_width`, `breakpoints` and `responsive_layouts` store how that layout adapts to other screen sizes; see [Layout & sizing](layout-and-sizing.md#responsive-layouts).

`settings` is a dictionary of dashboard-wide values, such as `{"refresh_period": 30}`. Every
store persists it, and a dashboard saved before settings existed loads with an empty
dictionary. An existing SQLite database gains the column the first time it is opened.
Settings that are not a dictionary with string keys are dropped with a warning, so the
dashboard itself still loads. See [Dashboard settings and refresh](dashboard-settings.md).

---

## CRUD operations

### Create

```python
dashboard = store.create_dashboard(user_id="user1", title="Sales Overview")
```

Generates a dashboard ID and persists an empty dashboard.

### Load

```python
dashboard = store.load_dashboard(user_id="user1", dashboard_id="abc123")
```

Returns `None` if not found.

### List

```python
dashboards = store.list_dashboards(user_id="user1")
```

Returns all dashboards for a user, ordered by most recently updated.

### Save

```python
dashboard.items.append(new_item)
dashboard.edges.append(new_edge)
store.save_dashboard(dashboard)
```

Inserts the dashboard or replaces the stored one with the same id.

### Rename

```python
store.rename_dashboard("user1", "abc123", "New Title")
```

### Delete

```python
store.delete_dashboard("user1", "abc123")
```

Returns `True` if a dashboard was deleted, `False` if not found.

---

## Decide who administers a dashboard

`store.can_administer(identity, dashboard_id, admin_groups)` decides whether a
user may edit, rename, share or delete a dashboard. Listings already hold the
models, so the app calls `store.can_administer_model(identity, model,
admin_groups)` for them instead of loading every dashboard again, and
`can_administer` delegates to it. To change the rule in a custom store, override
`can_administer_model`.

---

## Refuse an operation from a custom store

A store of your own can turn down a request, for example a duplicate title, a
name the backend cannot hold or a user without write access, by raising
`DashboardStoreError` from `create_dashboard`, `save_dashboard`,
`rename_dashboard`, `delete_dashboard` or `set_permission`:

```python
from panel_flowdash import DashboardStoreError, MemoryDashboardStore


class UniqueTitleStore(MemoryDashboardStore):
    def create_dashboard(self, user_id, title):
        if any(m.title == title for m in self._all_dashboards()):
            raise DashboardStoreError(f'A dashboard called "{title}" already exists.')
        return super().create_dashboard(user_id, title)
```

The app shows the message to the user as written, logs it at `INFO` and leaves
the open dashboard, canvas and menu as they were, so phrase it for an end user.
The create and rename dialogs stay open with the message under the name field.
Returning `False` from `rename_dashboard`, `delete_dashboard` or
`set_permission` is reported as a generic failure. Any other exception is
treated as a bug or an infrastructure fault: it is logged with its traceback and
the user sees a generic message such as "Could not save the dashboard. The error
has been logged.", never the exception text.

---

## Export and import dashboard files

In edit mode, **Download** in the side panel saves the canvas as a JSON file named after the dashboard. Dropping that file onto the wiring canvas recreates its components, connections and tile layout. If the canvas already has components, the editor asks before replacing them. Components the editor doesn't offer are skipped with a warning.

The file holds the dashboard contents but not its `dashboard_id`, `user_id` or `permission`. An imported file takes the identity of the dashboard currently loaded, so saving writes the imported contents to that dashboard and cannot overwrite another one or change who it is shared with.

The same round trip is available from Python:

```python
data = editor.export_dashboard()  # a JSON-serializable dict
other_editor.import_dashboard(data)  # also accepts the JSON string
```

`import_dashboard` raises `ValueError` for anything that isn't a dashboard export and marks the canvas dirty.
