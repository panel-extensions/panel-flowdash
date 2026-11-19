# Link Selections

A brush, click or lasso on a chart can drive the rest of a dashboard: the
chart outputs what is selected, a filter keeps the matching rows, and KPIs,
tables and other charts read the filter. The selection travels as one value,
a `Selection`, on ports of type `Selection`.

---

## The pattern

The chart you select on reads the **unfiltered** data and draws its own
selection, as Vega-Lite and HoloViews do by dimming what is not selected. Only
the other views read the filtered data:

```
data --> chart (selection) --> filter <-- data
                                  |
                                  +--> KPI, table, other charts
```

Feeding the chart from the filter its own selection drives would be a cycle,
and the editor refuses that connection.

## The value

A `Selection` holds predicates on fields of the chart's data and the id of
the node it was made on:

```python
from panel_flowdash import Predicate, Selection

Selection(
    (Predicate("category", "in", ("A", "B")), Predicate("price", "range", (10, 20))),
    source="chart",
)
```

A `range` predicate keeps rows within `(start, end)`, both included, or only
the start with `closed="left"`. An `in` predicate keeps rows equal to one of
its values. A selection without predicates is empty and keeps every row.
`Selection.filter(df)` applies it to a DataFrame.

A selection that names rows by their position in the chart, such as a
Vega-Lite point selection without `fields`, has `index` set instead of
predicates. It cannot be applied to other data, so filters refuse it: select
on fields instead.

## Give a component a selection output

`link_selection` follows a `pn.pane.Vega` or a HoloViews stream and writes
the selection to a parameter of the component, stamped with its node id:

```python
import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash import SELECTION_TYPE, Selection, link_selection, register
from panel_flowdash.selection import EMPTY


@register(
    component=True,
    page=False,
    requires=[{"key": "data", "type": "DataFrame", "required": True}],
    provides=[{"key": "selection", "type": SELECTION_TYPE}],
)
class BrushChart(Viewer):
    data = param.DataFrame()

    selection = param.ClassSelector(class_=Selection, default=EMPTY)

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.Vega(self._spec())
        self._link = link_selection(self, self._pane)

    def _spec(self):
        return {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
            "mark": "bar",
            "data": {"values": self.data},
            "encoding": {
                "x": {"field": "category", "type": "nominal"},
                "y": {"aggregate": "sum", "field": "price", "type": "quantitative"},
            },
        }

    @param.depends("data", watch=True)
    def _update(self):
        self._pane.object = self._spec()

    def __panel__(self):
        return self._pane
```

Giving the pane a new chart, as `_update` does when the data changes, clears
the selection. A component that builds a new pane instead passes the link
back, `link_selection(self, new_pane, link=self._link)`, so the output keeps
following one chart. The editor unlinks the component's links when its node
is removed.

For HoloViews, pass the stream and the element it selects on:
`link_selection(self, hv.streams.BoundsXY(source=points), element=points)`.

## How selections are read

- **Vega-Lite interval**: a range per encoded field on a continuous scale, and
  the covered values on a discrete one. Temporal values become naive datetimes
  in UTC, as Panel sends naive datetimes.
- **Vega-Lite point**: the selected values of its `fields`. A point selection
  without fields names rows by position, which filters refuse. Point
  selections on temporal fields do not reach Python: Panel cannot read them.
- **Time units**: a brush on a field with a time unit that starts with the year,
  such as `yearmonth` or `yearweek`, keeps the whole units Vega highlights,
  those whose start the brush covers, as a range closed on the left. Vega
  reports the field as `yearmonth_date`; the selection names `date`. Local
  units are cut in the browser's timezone and `utc` units in UTC, as Vega cuts
  them. A repeating unit such as `month`, `monthdate` or `hours` puts the same
  month or hour of every year together, which a range of dates cannot express,
  so such a brush is unsupported.
- **Binned fields**: on a chart that aggregates, such as a histogram, a fixed
  `bin.step` lets the brush keep the whole bins whose midpoint it covers.
  Without a step Vega derives the bins from the data in the browser, so the
  brushed range is kept as it is and partly covered bins are cut. Give
  selectable histograms a `step`.
- **Aggregated charts** select their grouping field: brushing a bar chart of
  the sum per category gives `category in [...]`, which a filter applies to the
  raw rows.
- **HoloViews**: `BoundsXY`, `BoundsX` and `BoundsY` give ranges on the
  element's dimensions; a box on a categorical axis is unsupported, since its
  bounds are positions rather than values. `Selection1D`, which Bokeh's box,
  lasso and tap tools fill, gives the selected values of a key column: by
  default the element's first key dimension when it is categorical and its
  values are unique, as for `Bars`, and row positions otherwise. A `Lasso`
  stream's polygon cannot filter data and is unsupported; use Bokeh's lasso
  tool with `Selection1D` and a key column instead. For a `DynamicMap`, the
  element it shows last is used.

A selection that cannot filter data says why in `Selection.unsupported`, and
filters refuse it with that message.

## Dragging

A Vega pane sends a selection once the drag pauses for the link's
`debounce`, 150 milliseconds by default, rather than on every mouse move; a
`debounce` set on the pane itself is kept. Equal selections in a row are
reported once. Selections that arrive while one is being reported, for
example while the filters it drives run their queries, replace each other,
and only the newest is reported next. After a slow report the link waits half
as long, up to 200 milliseconds, before the next one, so a drag's selections
collect rather than each running its own queries.
