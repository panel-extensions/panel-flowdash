# Persist Dashboards

FlowDash uses SQLite to store dashboard configurations: which components are
placed, how they are connected, and their grid layout. The `DashboardStore`
handles all CRUD operations.

---

## Create a store

```python
from panel_flowdash import DashboardStore

store = DashboardStore("path/to/dashboards.db")
```

The database file is created automatically with the required schema. WAL mode
is enabled for concurrent read access.

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

---

## CRUD operations

### Create

```python
dashboard = store.create_dashboard(user_id="user1", title="Sales Overview")
```

Generates a random dashboard ID and persists an empty dashboard.

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

Uses upsert semantics (INSERT ... ON CONFLICT DO UPDATE).

### Rename

```python
store.rename_dashboard("user1", "abc123", "New Title")
```

### Delete

```python
store.delete_dashboard("user1", "abc123")
```

Returns `True` if a row was deleted, `False` if not found.
