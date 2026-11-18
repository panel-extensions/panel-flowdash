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
case-insensitive file system. Saves, renames and permission changes hold an
advisory lock on the folder (on Windows, on a `.flowdash.lock` file inside it)
while they check the file they replace, so two saves of the same dashboard
cannot both succeed; see [Concurrent edits](#concurrent-edits). The lock needs
a file system that supports it; where it does not, as on some network shares,
a warning is logged and only the writers within one process are serialized.

Each file stores the dashboard's `revision`. If you edit a file by hand while
the server runs, increment its `revision` too, or a session that opened the old
contents can still save over your edit. A file without a `revision` is treated
as revision 0.

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

`turns` lists the requests to the assistant and the batches of manual edits that
changed the dashboard, as `DashboardTurn` objects, and each item's `origin` names the
turn that created it. The editor records them as edits happen (see the
[dashboard YAML format](../reference/dashboard-yaml.md#turns)). Every store persists
them; dashboards saved before turns existed (model version 3 and older) load without
turns, an existing SQLite database gains a `turns_json` column the first time it is
opened, and malformed stored turns are dropped with a warning like malformed settings.

`updated_at` is when the store last wrote the dashboard, as a timezone-aware UTC
`datetime`. Stores fill it in when they load a dashboard (the SQLite store from its
`updated_at` column, the JSON folder store from the file's modification time) and the
launcher shows it as "Edited 12 minutes ago". It is not part of the dashboard's
contents, so it is not saved, exported or compared. A custom store that leaves it
`None` still works; its launcher cards then name only the owner.

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

Inserts the dashboard or replaces the stored one with the same id. If the
dashboard was changed since it was loaded, the save raises
`DashboardConflictError` instead; see [Concurrent edits](#concurrent-edits).

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

## Concurrent edits

Every stored dashboard has a `revision`, a number the store increments on each
save. A model loaded from a store carries the revision it was loaded at, and
`save_dashboard` only writes it if that is still the stored revision. Otherwise
another session saved the dashboard in the meantime, and the store raises
`DashboardConflictError` (a `DashboardStoreError`) without writing anything.
The check and the write are one atomic step in every built-in store, also when
several server processes share a SQLite database or a JSON folder. On success
the store sets `dashboard.revision` to the new revision, so the same model can
be saved again.

```python
from panel_flowdash import DashboardConflictError

first = store.load_dashboard("user1", "abc123")
second = store.load_dashboard("user1", "abc123")
store.save_dashboard(first)
try:
    store.save_dashboard(second)
except DashboardConflictError as exc:
    print(exc.expected, exc.current)  # 1 2
```

A model whose `revision` is `None`, such as one you build yourself, is saved
unconditionally. Renaming a dashboard or changing who it is shared with does
not change its revision, because a save keeps the stored title and sharing
rules anyway. An existing SQLite database gains the `revision` column the first
time it is opened, with every dashboard at revision 0.

In the editor, a rejected save leaves the canvas as it was and opens a dialog
explaining that someone else saved the dashboard. **Reload** discards the local
changes and loads the latest version. **Save as copy** saves the canvas as a new
dashboard owned by the current user, titled after the original with `(copy)`
appended, and switches to it; in a `FlowDashApp` the URL and dashboard menu
follow. **Keep editing** closes the dialog. The same applies to saving dashboard
settings, which are part of the saved dashboard. From Python, `FlowDash.save()`
raises `DashboardConflictError` and `FlowDash.save_as(title)` saves a copy.

A store of your own can support this too: compare `dashboard.revision` with the
stored revision under the same lock or transaction as the write, raise
`DashboardConflictError` on a mismatch, and set `dashboard.revision` to the
stored value afterwards. The helper `panel_flowdash.dashboard_store.check_revision`
does the comparison and returns the next revision. A store that ignores
`revision` keeps working, with saves resolving last-writer-wins.

---

## Decide who administers and edits a dashboard

`store.can_administer(identity, dashboard_id, admin_groups)` decides whether a
user may rename, share or delete a dashboard, and
`store.can_edit(identity, dashboard_id, admin_groups)` whether they may edit and
save its contents and settings. Listings already hold the models, so the app
calls `store.can_administer_model(identity, model, admin_groups)` and
`store.can_edit_model(...)` for them instead of loading every dashboard again,
and the id-based methods delegate to these. To change a rule in a custom store,
override the `_model` method. The default `can_edit_model` allows everyone
`can_administer_model` allows, plus the editors named on the dashboard's
permission.

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

## Items that cannot be restored

A saved dashboard can outlive the code it was built with. When a dashboard is
loaded or imported, an item whose component is no longer offered, or whose
component raises while it is built, is shown as a placeholder node on the
wiring canvas and a placeholder tile on the dashboard, in the slot it had. An
edge whose port no longer exists, whose types no longer fit, or which touches
a placeholder is listed under the wiring canvas instead of being drawn.

Nothing is dropped: placeholders and broken edges are saved back exactly as
they were stored, so the dashboard is whole again as soon as the component
returns. Remove a placeholder node to drop the item and its edges, or click
**Discard** under the listing to drop the broken edges. Someone who can save
the dashboard is told once, when it is opened for editing, how many items and
connections could not be restored.

To keep dashboards loading after you rename a component or a port, declare the
former names, see
[Renaming components and ports](register-components.md#renaming-components-and-ports).

## Export and import dashboard files

In edit mode, **Download** in the side panel saves the canvas as a JSON file named after the dashboard, and **Download as YAML** saves it in the readable [YAML format](../reference/dashboard-yaml.md). Dropping either file onto the wiring canvas recreates its components, connections and tile layout. If the canvas already has components, the editor asks before replacing them. Components the editor doesn't offer are kept as placeholders, see [Items that cannot be restored](#items-that-cannot-be-restored).

The file holds the dashboard contents but not its `dashboard_id`, `user_id`, `permission` or `revision`. An imported file takes the identity of the dashboard currently loaded, so saving writes the imported contents to that dashboard and cannot overwrite another one or change who it is shared with. It also keeps the revision the dashboard was loaded at, so importing a file does not hide a save made by another session since then.

The launcher offers the same files for any dashboard you can open: **Download as file** and **Download as YAML** in the card's menu save the stored dashboard without opening it, and **Import file** creates a new dashboard of yours from a downloaded JSON or YAML file (titled after the file's dashboard, numbered if you already have one with that title) and opens it. **Duplicate** copies a dashboard the same way, titled "Sales copy" for a dashboard called "Sales", owned by you and not shared with anyone. A copy is yours to edit and inspect, so when the app has `inspect_outputs` on and a dashboard keeps **Viewers can inspect outputs** off, only its editors and administrators get **Duplicate** and **Download as file**; viewers would otherwise get around the restriction by copying it. The same goes for **Download as YAML**.

The same round trip is available from Python:

```python
data = editor.export_dashboard()  # a JSON-serializable dict
other_editor.import_dashboard(data)  # also accepts the JSON string
```

`import_dashboard` raises `ValueError` for anything that isn't a dashboard export and marks the canvas dirty.

## Undo, redo and the edit history

The canvas keeps an edit history: adding, removing and reconfiguring nodes, wiring and transforms, moving nodes, the tile layout, settings, clearing, importing a file and the assistant's edits all become versions of it, batched so the history reads naturally. Each request to the assistant is one version, made when the request ends, however many edits it took; if the person edits or saves while the assistant works, the assistant's edits so far become a version there, the person's edits their own, and its later edits another version of the same request. A person's edits are batched by burst: edits with no pause longer than `FlowDash.history_coalesce` seconds (3 by default) between them, such as a drag, typing into a config field or adding and wiring a node, become one version, and an edit of the assistant, a save, undo or redo ends the batch. `history_coalesce=0` records each of the person's edits on its own. The defaults live in `panel_flowdash.history` (`BURST_SECONDS`).

In edit mode, **Undo** and **Redo** in the command palette, or Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z (Ctrl+Y on Windows), step through it. From Python:

```python
editor.undo()      # True if something was undone
editor.redo()      # redoes everything the last undo undid
editor.can_undo, editor.can_redo
```

Undo steps back one version at a time, so it undoes a request to the assistant as a whole, back to the dashboard as it was before the request. When the person edited while the request ran, undo steps back through the request's versions and the person's one at a time instead, so nothing the person did is lost; `editor.undo(whole_runs=False)` always steps back one version. Redo lasts until the next edit. Undoing does not delete anything: it applies an earlier version and records that it did, so a later redo, or the history in the assistant's drawer, can get back to any version. Restoring a version changes only what differs from the canvas, so nodes that stay keep running.

Undo restores the dashboard's turns and node origins as they were at that version, so it never credits a node to someone who did not create it. It also never brings back what you may not use. A node whose component you are not authorized for, or that the app does not offer, keeps its config and connections as the canvas holds them, or as they were when you opened the dashboard if you removed it, so you can undo removing it. A version that holds such a node you never had on the canvas is out of reach: `can_undo` is False and `undo` raises `PermissionError` (the palette shows the message), and nothing changes. People who may only view a dashboard record no history and cannot undo.

The store keeps the history of each dashboard, so undo reaches edits of earlier sessions. When a dashboard is opened, undo steps back along the edits that made it as saved, never into edits that were undone or that a session made but did not save, also after an upgrade that changes how the dashboard's YAML reads. Undo waits while the assistant is working on a request. The history holds the dashboard YAML as its author saw it; when another person loads it, nodes they may not use are shown as restricted stand-ins, as in the YAML they download. Retention drops the oldest versions past `FlowDash.history_limit` (500 by default) and those older than `FlowDash.history_max_days` (90 by default) when the history is first used after opening a dashboard, and a long session keeps the content of only about as many versions in memory; deleting a dashboard deletes its history. `history=False` turns it all off. In a `FlowDashApp`, set these through `editor_options`.

`DashboardStore` keeps the history in a `dashboard_history` table, which it adds to existing databases on open; `JSONFolderDashboardStore` keeps one JSON Lines file per dashboard in a `.history` folder inside the dashboards folder, which you may want to leave out of version control; `MemoryDashboardStore` keeps it in memory. A custom store keeps it by implementing `load_history`, `append_history`, `replace_history` and `delete_history`, overriding `rewrite_history` to read and write in one transaction when several sessions share it, and dropping the history in `delete_dashboard`; without them undo still works within a session. `editor.edit_history.state` is the panel-agents `RunState` holding the versions, for showing them in an `ArtifactView` or sharing with your own `AgentRunner`.
