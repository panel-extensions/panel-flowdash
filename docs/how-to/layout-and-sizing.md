# Layout & Sizing

Once components are wired together, the editor has two views of the same set of
nodes:

- **Dataflow** (`mode="wiring"`) - the ReactFlow canvas where you add
  components and connect ports.
- **Layout** (`mode="dashboard"`) - a responsive tile grid that renders each
  component's live view, the layout your users actually see.

Switch between them with the *Dataflow* and *Layout* tabs in the dashboard bar
while editing. To see the dashboard exactly as it will appear when served, press
*Done* (or choose *View*): view mode is the preview.

![Dashboard mode showing tiles in the grid, sidebar filters, and the breakpoint toolbar](../assets/images/dashboard-mode.png)

The breakpoint toolbar (XS / SM / MD / AUTO) appears at the top of the layout
tab; the sidebar filters on the left are the components
marked `sidebar=True`, described next.

## Arranging tiles

In the *Layout* tab every tile has a dashed frame, a drag handle at the top
and a resize grip in its bottom-right corner. Drag a tile by its handle to
move it, and drag the grip to resize it.

The panel on the right edits one tile at a time:

- **Selected tile** picks a tile (it starts with the component selected on the
  *Dataflow* canvas, if any) and sets its **Width** in columns of a 12-column
  grid and its **Height** in pixels, or *Automatic* to size it to its content.
  The heights offered stay within the component's `min_size` and `max_size`.
  **Hide tile** takes the tile off the dashboard without removing the
  component from the dataflow.
- **Not on the dashboard** lists the placed components that have no tile and
  why: *in controls bar* for `sidebar=True` components (*in sidebar* when
  `controls_bar` is off), and *hidden by you* for tiles you hid, with a
  **Show** action that puts the tile back.

The status bar under the grid summarizes it, e.g. "12-column grid, 4 tiles,
2 components hidden". Changes made in the panel edit the authored layout (see
[Responsive layouts](#responsive-layouts)) and, like dragging, mark the
dashboard as having unsaved changes. Fold the panel with its arrow button to
give the grid more room.

---

## Sidebar components

Not every component belongs in the tile grid. Filters, data-source selectors,
and other controls often read better in a persistent sidebar. Mark such a
component with `sidebar=True`:

```python
@register(component=True, sidebar=True, title="Capacity Filter")
class app(pn.viewable.Viewer):
    min_capacity = param.Number(default=0, bounds=(0, 10000), step=100)

    @param.output(param.String)
    @param.depends("min_capacity")
    def filter_expr(self):
        return f"t_cap >= {self.min_capacity}" if self.min_capacity > 0 else ""

    def __panel__(self):
        return pmui.IntInput.from_param(self.param.min_capacity)
```

In view mode and on the Layout tab, a `sidebar=True` component's view is rendered
in a controls bar above the tiles instead of in the tile grid. The controls sit in
a row that wraps on narrow screens, and the bar is hidden while it has nothing to
show. In view mode it also holds a pipeline strip, source to transforms to views
with a status dot per stage. The component still participates in the dataflow
exactly like any other node, its ports wire up the same way; only its placement
changes. Pass `controls_bar=False` (through `editor_options` for a `FlowDashApp`)
to render these views in the page sidebar instead.

The `complex_dataflow` example uses this for its data source and all three
filters, keeping the main grid for the table, chart, and map.

---

## Tile sizing hints

Components can declare their preferred size on the grid through `@register`:

```python
@register(
    component=True,
    default_size={"width": 600, "height": 400},
    min_size={"width": 300, "height": 200},
    max_size={"width": 1200, "height": 800},
)
class app(pn.viewable.Viewer):
    ...
```

Each hint is a dict with a `width`, a `height` or both, in pixels. Any other
key is ignored with a warning.

- `default_size` is the size a tile starts at when the dashboard has no layout
  entry for it, e.g. when it was just added. The layout stores widths as a
  percentage of the row, so the default width is converted at the grid's
  reference width (see [Responsive layouts](#responsive-layouts)): a 600px tile
  on a dashboard authored at 1200px takes half the row.
- `min_size` and `max_size` bound the tile while you resize it in dashboard
  mode, and apply to stored layouts when a dashboard is loaded, so a layout
  saved before the hints existed is brought within them. On narrower screens
  the minimum width still holds: a tile wraps onto a new line rather than
  shrinking below it.

Within these bounds the author can resize tiles freely, and the arrangement
they settle on is what gets persisted.

---

## Responsive layouts

Arrange the tiles once, at the width you are working at, and the grid adapts the arrangement to narrower screens. The grid records the width you arranged the tiles at as `reference_width`. On a narrower screen each tile may shrink to half its authored width; once it would shrink further, it wraps onto a new line instead. Tiles in a row that no longer fits are split into evenly balanced lines, and a tile alone on a line takes the full width. In the `complex_dataflow` example the chart and map share a row on a desktop and stack above the table on a laptop with both side panels open.

The grid's `breakpoints` divide the viewport into bands; the default `[768, 1200]` yields `xs` below 768px, `sm` between, and `md` above 1200px. In edit mode a toolbar lets you preview each band:

- The band containing `reference_width` is marked **base**. Editing while it's shown changes the authored layout.
- Other bands show the generated layout. Editing one saves a custom layout for that band, marked **custom**, and **Reset** discards it again.
- **AUTO** returns to the natural width.

In AUTO the band follows the grid's own width, which the side panels of the
editor narrow. To change the authored layout on a smaller screen, pick the
**base** chip first, or fold the layout panel.

Custom layouts are stored in `responsive_layouts`, a mapping from band label to a list of tile entries. Each entry records a tile's index, width (as a percentage of the row), height in pixels, and visibility:

```json
{
  "xs": [
    {"index": 0, "width": 100, "height": 440, "visible": true},
    {"index": 1, "width": 100, "height": 450, "visible": true}
  ]
}
```

---

## What gets persisted

When you save a dashboard, the grid arrangement and responsive settings are stored alongside the nodes and edges:

- `tile_layout` - the authored arrangement.
- `reference_width` - the grid width in pixels the arrangement was authored at.
- `breakpoints` - the pixel thresholds in use.
- `responsive_layouts` - the custom per-band arrangements described above.

On load these are restored so the dashboard reopens with the same layout at every width. Dashboards saved before `reference_width` existed are treated as authored at the largest breakpoint. See [Persist dashboards](persist-dashboards.md) for the storage model and CRUD API.

---

## Related

- [Register components](register-components.md) - `sidebar`, `default_size`, `min_size`, `max_size`
- [Configure nodes](configure-nodes.md) - design-time configuration for each node
- [Persist dashboards](persist-dashboards.md) - how layouts are saved and loaded
