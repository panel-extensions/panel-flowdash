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


@register(component=True, provides=[{"key": "selection", "type": SELECTION_TYPE}])
class BrushChart(Viewer):
    data = param.DataFrame()

    selection = param.ClassSelector(class_=Selection, default=EMPTY)

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.Vega({
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "params": [{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
            "mark": "bar",
            "data": {"values": self.data},
            "encoding": {
                "x": {"field": "category", "type": "nominal"},
                "y": {"aggregate": "sum", "field": "price", "type": "quantitative"},
            },
        })
        self._link = link_selection(self, self._pane)

    def __panel__(self):
        return self._pane
```

If the component builds a new pane, for example when its data changes, pass
the link back, `link_selection(self, new_pane, link=self._link)`, so the
output keeps following one chart; the new chart starts with nothing selected.

For HoloViews, pass the stream and the element it selects on:
`link_selection(self, hv.streams.BoundsXY(source=points), element=points)`.

## How selections are read

- **Vega-Lite interval**: a range per encoded field on a continuous scale, and
  the covered values on a discrete one. Temporal values become datetimes.
- **Vega-Lite point**: the selected values of its `fields`.
- **Time units**: a brush on a field with a `timeUnit` keeps the whole units
  Vega highlights, those whose start the brush covers, as a range closed on
  the left. Vega reports such a field as `yearmonth_date`; the selection names
  `date`.
- **Binned fields**: with a fixed `bin.step` the brush keeps the whole bins
  whose midpoint it covers. Without a step, Vega derives the bins from the
  data in the browser, so the brushed range is kept as it is and partly
  covered bins are cut. Give selectable histograms a `step`.
- **Aggregated charts** select their grouping field: brushing a bar chart of
  the sum per category gives `category in [...]`, which a filter applies to the
  raw rows.
- **HoloViews**: `BoundsXY`, `BoundsX` and `BoundsY` give ranges on the
  element's dimensions; `Selection1D`, which Bokeh's box, lasso and tap tools
  fill, gives the selected values of a key column, by default the element's
  first key dimension when its values are unique, as for `Bars`.

## Dragging

A Vega pane sends a selection once the drag pauses for the link's
`debounce`, 150 milliseconds by default, rather than on every mouse move.
Equal selections in a row are reported once, and while one is being reported,
for example while the filters it drives run their queries, newer selections
wait and only the latest is reported next.
