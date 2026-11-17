# Dashboard YAML Format

A dashboard can be written as YAML: a readable form of the file **Download** writes,
meant for people and language models to read, review and edit. It holds the same
content as the JSON export, keyed by readable node names instead of generated ids,
and loading it goes through the same path as importing a downloaded file, so the
same permission checks apply.

This page specifies version 1 of the format. Every file starts with `flowdash: 1`,
the integer 1. Every change to the format, including a new optional key, comes with a
new version number. A release reads the versions it knows and rejects a file with a
later version, or with keys it does not know, saying that a newer FlowDash may have
written it.

```yaml
flowdash: 1
title: State Deep Dive
settings:
  refresh_period: 0
  capacity_unit: MW
nodes:
  turbines:
    component: Sources/turbines
    position: [0, 280]
  state:
    component: Controls/state_picker
    position: [0, 0]
    config:
      initial_state: TX
  filter:
    component: Transforms/filter_turbines
    position: [520, 120]
  kpi_modern:
    component: Views/kpi
    position: [1040, 0]
    config:
      metric: turbines
      label: Turbines of 3 MW and up
edges:
  - from: turbines.turbines
    to: filter.turbines
  - from: state.state
    to: filter.state
  - from: filter.filtered
    to: kpi_modern.turbines
    transform:
      - op: filter
        column: t_cap
        comparison: ">="
        value: 3000
layout:
  tiles:
    - node: kpi_modern
      width: 33.33
      height: 130
  reference_width: 1200
  breakpoints: [768, 1200]
```

## Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `flowdash` | yes | The format version, `1`. |
| `title` | no | The dashboard's title. Loading a file into an existing dashboard keeps that dashboard's title. |
| `settings` | no | Dashboard-wide settings, keyed by the parameter names of the app's `DashboardSettings` class, e.g. `description`, `refresh_period`, `tile_cards`. |
| `nodes` | no | The placed components, a mapping from node name to node. |
| `edges` | no | The connections between ports, a list. |
| `layout` | no | How the tiles are arranged on the dashboard. |

Keys not listed here are rejected, except keys starting with `x-`, which are accepted
and ignored in every mapping of the format (the top level, nodes, edges, the layout
and tiles, but not `config` or `settings`), for tools that annotate dashboard files.
The file never holds the dashboard's id, owner, sharing rules or revision: those
belong to the dashboard it is loaded into.

## Values

A file holds only what JSON can: strings, finite numbers, `true`/`false`, `null`,
lists and mappings with string keys. Dates such as `2024-01-01` are read as strings.
A file is refused if it is larger than 1,000,000 characters, nests deeper than 32
levels, defines a key twice in one mapping, or uses anchors and aliases (`&`, `*`) or
tags such as `!!set` and `!!binary`. Comments are ignored.

## Nodes

A node name is unique within the file, starts with a letter or underscore and
contains only letters, digits, `_` and `-`. Node order matters: the controls bar
shows control components (registered with `sidebar=True`) in node order.

A node's name comes from its id alone, so it is the same for everyone who reads the
dashboard, does not change when its component is renamed and is never given to
another node. Nodes placed in the editor have generated ids such as
`Views__kpi_3fa2c1` and are named after their last part, `kpi_3fa2c1`; a node created
from a file keeps the name the file gave it. New names may not contain `__`, which
marks generated ids.

| Key | Required | Meaning |
| --- | --- | --- |
| `component` | yes | The id of a registered component, e.g. `Views/kpi`. A former id the component declares in `aliases` also works. |
| `config` | no | Design-time configuration, keyed by the component's config fields. Omitted fields take the component's defaults. |
| `position` | no | `[x, y]` position on the Dataflow canvas. Nodes without one are laid out in columns following the dataflow. |

A component the person exporting the file may not use is written as
`component: flowdash:restricted` without its config. Loading the file back into the
same dashboard keeps the stored component unchanged; anywhere else it is left out.
Validated against the dashboard it is loaded into, a file must keep each restricted
node as a stand-in with its connections: it cannot replace one with another component,
leave one out, or add a stand-in for a node the dashboard does not have.

## Edges

| Key | Required | Meaning |
| --- | --- | --- |
| `from` | yes | The output, as `<node>.<port>`. |
| `to` | yes | The input, as `<node>.<port>`. |
| `transform` | no | Steps applied to every value carried along the edge, each a mapping with an `op` and its arguments, see [Transform Edges](../how-to/transform-edges.md). |

An input takes one edge, unless it accepts a list, in which case every edge
connected to it contributes one item.

The connections of a restricted node are written with the node's name alone on its
end, e.g. `from: restricted_3fa2c1` and `to: table.data`, so the file shows that
`table.data` is taken without revealing the restricted node's port. They are kept
as stored when the file is loaded, cannot be added or changed, and an input they
feed takes no other edge.

## Layout

| Key | Required | Meaning |
| --- | --- | --- |
| `tiles` | no | The tiles in the order they are shown, a list. |
| `reference_width` | no | The grid width, in pixels, the widths were laid out at. |
| `breakpoints` | no | Screen widths, in pixels, at which the grid switches to a narrower layout. |
| `responsive` | no | Layouts for narrower screens, keyed by breakpoint, each a list like `tiles`. |

Each tile has a `node` naming its node, plus any of `width` (percent of the row,
greater than 0 and at most 100), `height` (pixels) and `visible` (`false` hides the
tile while keeping its component in the dataflow). Other keys are rejected, except
`x-` keys, which are ignored. Nodes without a tile entry get
their component's default size, after the listed tiles. Control components have no
tile.

## Not in version 1

A file describes what a dashboard is built from, not the state it is in:

- **Control values.** Which components appear in the controls bar follows their
  registration (`sidebar=True`) and node order; the value a viewer picks in a control
  is not stored. A control's starting value is part of its `config`, where the
  component has one.
- **Publishing.** FlowDash has no published or draft state for a dashboard; who may
  see it is its sharing, which belongs to the dashboard a file is loaded into.

## Loading a file

Loading YAML replaces the dashboard's contents, as importing a downloaded file does:

```python
from panel_flowdash.agent import dashboard_to_yaml

text = dashboard_to_yaml(editor)
editor.import_dashboard(text)
```

Node names map back to the ids of the nodes they were exported from, so loading a
file exported from the same dashboard keeps every node's identity. A new node name
becomes the new node's id.

`dashboard_schema()` returns the [JSON Schema](https://json-schema.org/) of the format
for a set of components, including each component's config fields, and
`validate_dashboard()` checks a file against the components, ports, port types and
transforms the editor offers before it is loaded.
