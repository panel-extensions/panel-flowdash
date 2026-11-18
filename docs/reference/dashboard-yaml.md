# Dashboard YAML Format

A dashboard can be written as YAML: a readable form of the file **Download** writes,
meant for people and language models to read, review and edit. It holds the same
content as the JSON export, keyed by readable node names instead of generated ids,
and loading it goes through the same path as importing a downloaded file, so the
same permission checks apply.

This page specifies version 2 of the format. Every file starts with `flowdash: 2`,
the integer 2. Every change to the format, including a new optional key, comes with a
new version number. A release reads the versions it knows and rejects a file with a
later version, or with keys it does not know, saying that a newer FlowDash may have
written it. This release writes version 2 and reads versions 1 and 2, see
[Version 1](#version-1).

```yaml
flowdash: 2
title: State Deep Dive
settings:
  refresh_period: 0
  capacity_unit: MW
turns:
  - id: t3fa2c1d0
    actor: user
    at: '2026-11-17T20:10:12Z'
    summary: Added turbines. Added state. Added filter.
  - id: t81c0b2e4
    actor: agent
    at: '2026-11-17T20:14:03Z'
    run: run-5d1e7a
    question: How many modern turbines are there?
    summary: Added kpi_modern. Connected filter.filtered to kpi_modern.turbines.
nodes:
  turbines:
    component: Sources/turbines
    origin: t3fa2c1d0
    position: [0, 280]
  state:
    component: Controls/state_picker
    origin: t3fa2c1d0
    position: [0, 0]
    config:
      initial_state: TX
  filter:
    component: Transforms/filter_turbines
    origin: t3fa2c1d0
    position: [520, 120]
  kpi_modern:
    component: Views/kpi
    origin: t81c0b2e4
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
| `flowdash` | yes | The format version, `2`. |
| `title` | no | The dashboard's title. Loading a file into an existing dashboard keeps that dashboard's title. |
| `settings` | no | Dashboard-wide settings, keyed by the parameter names of the app's `DashboardSettings` class, e.g. `description`, `refresh_period`, `tile_cards`. |
| `turns` | no | Who changed the dashboard and why, a list, oldest first. |
| `nodes` | no | The placed components, a mapping from node name to node. |
| `edges` | no | The connections between ports, a list. |
| `layout` | no | How the tiles are arranged on the dashboard. |

Keys not listed here are rejected, except keys starting with `x-`, which are accepted
and ignored in every mapping of the format (the top level, nodes, edges, the layout
and tiles, but not `config` or `settings`), for tools that annotate dashboard files.
The file never holds the dashboard's id, owner, sharing rules or revision: those
belong to the dashboard it is loaded into.

## Values

A file holds only what JSON can: valid Unicode strings, finite numbers,
`true`/`false`, `null`, lists and mappings with string keys. A file is refused if it is larger than
1,000,000 characters (or bytes), nests deeper than 32 levels, defines a key twice in
one mapping, or uses anchors and aliases (`&`, `*`) or tags such as `!!set` and
`!!binary`. Comments are ignored. `DashboardOps.get_dashboard` starts the YAML of an
open canvas with a `# revision: <token>` comment naming the canvas it was read from
(not the stored dashboard's revision); `replace_yaml` refuses text whose first line
names a canvas that has changed since.

Unquoted values are typed by the YAML 1.2 core schema, with these integers:

| Written | Read as |
| --- | --- |
| `true`, `false` (also `True`, `TRUE`, ...) | a boolean |
| `null`, `~` or nothing | null |
| `12`, `-3`, `0o17`, `0x1f` | an integer |
| `3.5`, `.5`, `1e3`, `.inf`, `.nan` | a number (`.inf` and `.nan` are refused where a number is expected) |
| anything else, e.g. `yes`, `no`, `017`, `12:30`, `1_000`, `2024-01-01` | a string |

Quote a value to make it a string whatever it looks like. The writer quotes every
string another YAML reader could take for a different type.

A file in this format may also be written as JSON with the same keys; it is read
under the same rules. Flow-style YAML (`{flowdash: 2, ...}`) is read too.

## Turns

A turn is one request to the assistant, or one batch of a person's edits made between
requests. The editor writes them as edits happen: the first edit of a request opens
the agent's turn for it, and edits the person makes between requests form a turn of
their own. An edit belongs to the agent when an agent's tool makes it, so an edit the
person makes while the agent works becomes a turn between two edits of the agent's.

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique within the file: 1 to 64 letters, digits, `_` and `-`, starting with a letter or digit. The editor makes `t` followed by 8 hex digits and never reuses an id. |
| `actor` | yes, unless pending | `agent` or `user`. |
| `at` | yes, unless pending | When the turn's first edit was made, as an ISO 8601 date and time, e.g. `'2026-11-17T20:14:03Z'` (quoted, so other YAML readers keep it a string). |
| `run` | no | The id of the assistant's run that made the turn. |
| `question` | no; yes when pending | What the person asked the assistant. |
| `summary` | no | What the turn changed, a few words per change. It names nodes and connections but never config values. |
| `status` | no | `pending` for a planned turn that has not run; left out for every other turn. |

Each node records the turn that created it as its `origin`. Which later turns changed
it is not stored in the file. A dashboard keeps its last 200 turns, and older turns
are only dropped when no node names them as its origin.

Turns record the question by default, since it says why a node exists. Exports can
leave the questions out: `dashboard_to_yaml(..., questions=False)`,
`FlowDash.export_dashboard(questions=False)` and `export_model(..., questions=False)`.

Only the editor writes turns and origins. YAML handed to `DashboardOps.replace_yaml`,
`validate` and `preview`, which is what the assistant writes, has its `turns` and
node `origin` keys dropped before it is read: the dashboard keeps its turns, nodes
already on it keep their origin, and new nodes get the turn of whoever made the
change. `replace_yaml` warns when the YAML tried to change them. Importing a file
(`FlowDash.import_dashboard`, dropping a file on the canvas or importing it as a new
dashboard) takes the file's turns and origins as they are, and the import itself is
a turn of the person who made it, which becomes the origin of nodes the file gives none.

## Nodes

A node name is unique within the file, starts with a letter or underscore and
contains only letters, digits, `_` and `-`. Node order matters: the controls bar
shows control components (registered with `sidebar=True`) in node order.

A node's name comes from its id alone, so it is the same for everyone who reads the
dashboard, does not change when its component is renamed and is never given to
another node. Nodes placed in the editor have generated ids such as
`Views__kpi_3fa2c1` and are named after their last part, `kpi_3fa2c1` (prefixed with
`n_` when it starts with a digit); a node created from a file keeps the name the file
gave it, and so does a node placed under a chosen name with
`DashboardOps.add_node(..., name="paid_by_region")` or
`FlowDash.add_component(..., instance_id="paid_by_region")`, which makes the name its
id. New names may not contain `__`, which marks generated ids, and names the
format writes never do, so a file always loads into another dashboard. The editor
picks new ids whose names no node has; if two ids that came from elsewhere, such as
an imported JSON file, still want the same name, the one that sorts first keeps it
and the other gets a short hash of its id appended.

| Key | Required | Meaning |
| --- | --- | --- |
| `component` | yes | The id of a registered component, e.g. `Views/kpi`. A former id the component declares in `aliases` also works. |
| `origin` | no | The `id` of the turn that created the node. It must name a turn of the file. |
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
as stored when the file is loaded, with their stored transform (a `transform` on such
an edge is refused), cannot be added or changed, and an input they feed takes no
other edge, in the editor too.

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

## Version 1

Version 1 files start with `flowdash: 1` and may not hold `turns` or a node `origin`.
They load as a dashboard without turns whose nodes have no origin, and exporting it
again writes version 2.

## Not in version 2

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
