# Filter Dashboards

A dashboard can filter its views in two ways. **Global filters** sit in a bar
above the tiles and filter every view whose data has the filtered column, with
no wiring. **Filter nodes** are ordinary nodes on the canvas that filter what
they feed, for one view or a group of views, for filters driven by a chart's
selection, or for side-by-side comparisons such as this year against last
year. Both can apply to the same view: it gets the global filters and its own.

---

## Global filters

In edit mode, **Add filter** in the filter bar lists the columns of the data the
dashboard's views read. Choosing one adds a filter on that column: a range
slider for numbers and dates, a multi-select for anything else. Viewers then
pick values in the bar, and every view the dashboard shows whose data has a
column of that name keeps only the matching rows. Their choices are not saved
with the dashboard; a filter's starting value is, see
[the `filters` key](../reference/dashboard-yaml.md#filters).

Matching is by column name, as Rill matches canvas filters to the dimensions
of its metrics views. Global filters reach the views the dashboard shows,
meaning tiles on the grid and components in the top bar or the sidebar. They
never reach sources and transforms, so a Filter node upstream of a view keeps
seeing all the rows.

Two notes on tiles say what applies where:

- A chip for each Filter node feeding the view, directly or further upstream.
  A Filter node is a transform tagged `filter`, such as the built-in
  *Filter by selection*. A Filter node feeding every view is shown once, in
  the filter bar, as *Filter Turbines · all views*, rather than on each tile.
- *Not filtered by State*, while a global filter has a value that does not
  reach the view: its data has no such column, the column holds values the
  filter's cannot be compared with (a range of numbers on a column of text,
  or a range on unordered categories), or the view opted out. Hovering over
  it says which.

Since columns match by name only, two views can hold a column of the same name
with different types. Values are converted where that is safe: ISO strings to
dates for a column of dates, numeric strings to numbers for a column of
numbers, and numbers to strings for a column of text. A view whose column
still cannot take the filter is left unfiltered by it, and says so, while the
other views are filtered as usual.

Global filters reach the views as they are, not the data upstream of them: a
view fed by an aggregate that has no such column shows the hint rather than
being filtered.

To opt a view out of a filter, for instance a chart of the whole fleet kept for
reference, select it and turn the filter off under **Global filters** in the
sidecar's **Tile** tab.

From code, the editor offers the same:

```python
editor.set_filters([{"column": "t_state", "label": "State"}])
editor.set_filter_value("t_state", ["TX", "OK"])
editor.exclude_from_filter("t_state", "fleet_total")
```

## How components take part

A filter's value is a [`Predicate`](link-selections.md#the-value) on its
column, so a component is handed `{column: predicate}`. There are two ways to
take part.

**Do nothing.** FlowDash filters the pandas DataFrames arriving at the inputs
of a `Viewer` component itself, keeping the rows that match every predicate on
a column the frame has. The view's columns are those of its DataFrame inputs.
Function components are not filtered, since they read their inputs from the
node directly.

**Filter yourself.** A component that can apply the predicates better, for
example by pushing them into a SQL query instead of loading every row,
implements the `FilterTarget` protocol. FlowDash then leaves its inputs as
they are:

```python
import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash import register


@register(component=True, page=False, tags=["view"])
class SalesTable(Viewer):
    table = param.String(default="sales")
    filters = param.Dict(default={})

    def __flowdash_columns__(self):
        # The columns this view can filter on, or None while it does not know.
        return ["region", "year", "product"]

    def __flowdash_apply_filters__(self, filters):
        # The predicates that apply to this view, by column; empty clears them.
        self.filters = dict(filters)

    def __flowdash_column_values__(self, column):
        # Optional: what a select filter offers, or (min, max) for a range.
        return run_query(f"SELECT DISTINCT {column} FROM {self.table}")

    @param.depends("table", "filters")
    def _rows(self):
        where = " AND ".join(to_sql(p) for p in self.filters.values()) or "TRUE"
        return run_query(f"SELECT * FROM {self.table} WHERE {where}")

    def __panel__(self):
        return pn.widgets.Tabulator(self._rows)
```

Only the filters on a column the component declares reach it, and none it
opted out of. `__flowdash_apply_filters__` is called again whenever that set
changes, including with an empty mapping when the last one goes. A predicate
is either `in`, with `values` the values to keep, or `range`, with `values`
the `(start, end)` bounds, either of which may be `None`; dates arrive as
`datetime` objects.

`panel_flowdash.global_filters` has the pieces as functions: `filter_frame`
applies predicates to a DataFrame, `resolve_view` says which filters reach a
view and why the others do not, and `check_filters` checks the `filters` of a
dashboard.
