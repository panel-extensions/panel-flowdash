# Dashboard YAML Format

A dashboard can be written as YAML: a readable form of the file **Download** writes,
meant for people and language models to read, review and edit. It holds the same
content as the JSON export, keyed by readable node names instead of generated ids,
and loading it goes through the same path as importing a downloaded file, so the
same permission checks apply.

This page specifies version 1 of the format. Every file starts with `flowdash: 1`,
the integer 1. A release that changes the format gives it a new version number. A
release reads the versions it knows and rejects a file with a later version, or with
keys it does not know, saying that a newer FlowDash may have written it. Files from
older pre-releases of panel-flowdash still load.

```yaml
flowdash: 1
title: State Deep Dive
settings:
  refresh_period: 0
  capacity_unit: MW
parameters:
  state:
    node: state
    field: value
    type: string
    description: The state to look at
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
      framed: false
  reference_width: 1200
  breakpoints: [768, 1200]
```

## Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `flowdash` | yes | The format version, `1`. |
| `title` | no | The dashboard's title. Loading a file into an existing dashboard keeps that dashboard's title. |
| `settings` | no | Dashboard-wide settings, keyed by the parameter names of the app's `DashboardSettings` class, e.g. `description`, `refresh_period`, `tile_cards`. |
| `parameters` | no | The config fields the dashboard can be run with, a mapping from parameter name to parameter. |
| `turns` | no | Who changed the dashboard and why, a list, oldest first. |
| `groups` | no | Nodes framed together on the canvas, a mapping from group name to group. |
| `nodes` | no | The placed components, a mapping from node name to node. |
| `edges` | no | The connections between ports, a list. |
| `layout` | no | How the tiles are arranged on the dashboard. |
| `report` | no | The dashboard read as a document: a title and an ordered list of blocks. |

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
under the same rules. Flow-style YAML (`{flowdash: 1, ...}`) is read too.

## Parameters

A parameter names one config field of one node, so the dashboard can be run again
with another value there: other data for a source, another starting value for a
widget. Its default is whatever the field holds; binding a value writes it into the
field before anything computes. Config values never hold placeholders.

| Key | Required | Meaning |
| --- | --- | --- |
| `node` | yes | The name of the node. |
| `field` | yes | The config field, or a dotted path into one, e.g. `spec.tables.orders`; a number picks a list item. The first part must be a config field of the node's component. |
| `type` | no | How a value is read: `string`, `number`, `integer`, `boolean`, `date`, `datetime`, `value` (the default: any JSON, text read as JSON if it parses) or `source`, `table`, `file` and `uri`, which are text whose meaning the app gives them. |
| `description` | no | What the parameter is, for whoever binds it. |
| `expects` | no | What the dashboard relies on of a bound value, written by the contract of the node's component. |

Parameter names start with a letter or underscore and hold up to 64 letters, digits,
`_` and `-`. A parameter whose node is removed is removed with it.

A component can declare a contract for the parameters on its config
(`register(contract=...)`, see `panel_flowdash.parameters.ParameterContract`). The
contract writes `expects` when the parameter is declared, when a file that gives none
is loaded and when the dashboard is saved, and checks each value bound to the
parameter against it before anything changes, naming the nodes that would fail. Only
the editor writes `expects`: YAML handed to `DashboardOps.replace_yaml` keeps what the
canvas recorded for a parameter that points where it did, and has it computed again
otherwise.

Bind values with `FlowDash.bind_parameters(values)` or
`DashboardOps.bind_parameters(values)`, which apply them as one edit, or with
`panel_flowdash.parameters.bind_document(doc, values)`, which writes them into a copy
of a parsed file for a run without an editor.

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
| `summary` | no | What the turn changed, a few words per change. It names nodes, and connections by their nodes, but never ports or config values. |
| `status` | no | `pending` for a planned turn that has not run; left out for every other turn. |

An agent's turn ends with its request, whether the request finished, failed or was
cancelled; a turn of a request that failed is written like any other, with the edits it
made before failing in its summary.

Each node records the turn that created it as its `origin`. Which later turns changed
it is not stored in the file. A dashboard keeps its last 200 turns, and older turns
are only dropped when no node names them as its origin and the report does not refer
to them.

Turns record the question by default, since it says why a node exists. Exports can
leave the questions out: `dashboard_to_yaml(..., questions=False)`,
`FlowDash.export_dashboard(questions=False)` and `export_model(..., questions=False)`.

Only the editor writes turns and origins. YAML handed to `DashboardOps.replace_yaml`,
`validate` and `preview`, which is what the assistant writes, has its `turns` and
node `origin` keys dropped before it is read: the dashboard keeps its turns, nodes
already on it keep their origin, and new nodes get the turn of whoever made the
change. Nodes are matched by name, so a node given another component under the same
name keeps its origin, while a renamed node is new. `replace_yaml` warns when the YAML
changes the turns or the origin of a node already on the dashboard, and YAML that
changes nothing opens no turn. Importing a file
(`FlowDash.import_dashboard`, dropping a file on the canvas or importing it as a new
dashboard) takes the file's turns and origins as they are, and the import itself is
a turn of the person who made it, which becomes the origin of nodes the file gives none.

## Groups

A group frames nodes together on the Dataflow canvas, and can be collapsed into one
node that shows the ports of the connections crossing its border. It changes nothing
else: the nodes keep their config, connections and tiles. Each node names its group
with `group`; a node is in at most one group, groups do not nest, and every group
has at least one node.

```yaml
groups:
  revenue: {label: Paid revenue by region, collapsed: true}
nodes:
  revenue_sql: {component: Lumen/sql, group: revenue, config: {...}}
  revenue_chart: {component: Lumen/view, group: revenue, config: {...}}
```

| Key | Required | Meaning |
| --- | --- | --- |
| `label` | no | Shown in the group's header; the group's name is shown without one. |
| `collapsed` | no | `true` draws the group as one node. |

Group names follow the rules of node names. Node positions stay positions on the
canvas, whether or not the node is in a group; the frame fits its nodes. A collapsed
group shows one input per output outside it that feeds it, connected to every inner
port that output feeds, and one output per inner output read from outside it.

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
| `group` | no | The name of the group the node is in. It must name a group of the file. |
| `config` | no | Design-time configuration, keyed by the component's config fields. Omitted fields take the component's defaults. |
| `inputs` | no | Values for inputs nothing is connected to, keyed by input name, e.g. `{reference: 1200}`. A connection to the input wins over its value, which applies again once the connection is removed. Only inputs whose values a file can hold take one: numbers, text, booleans, lists and mappings, not e.g. a DataFrame. |
| `position` | no | `[x, y]` position on the Dataflow canvas. Nodes without one are laid out in columns following the dataflow, beside the nodes that have one; a node already on the canvas keeps its position. |

A component the person exporting the file may not use is written as
`component: flowdash:restricted` without its config and inputs. Loading the file back into the
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
| `tiles` | no | The tiles in the order they are shown, a list. New nodes without an entry get one by the kind of their tile, see [Automatic arrangement](../how-to/layout-and-sizing.md#automatic-arrangement). |
| `reference_width` | no | The grid width, in pixels, the widths were laid out at. |
| `breakpoints` | no | Screen widths, in pixels, at which the grid switches to a narrower layout. |
| `responsive` | no | Layouts for narrower screens, keyed by breakpoint, each a list like `tiles`. |

Each tile has a `node` naming its node, plus any of `width` (percent of the row,
greater than 0 and at most 100), `height` (pixels), `visible` (`false` hides the
tile while keeping its component in the dataflow) and `framed` (`true` or `false`
draws the tile as a card in view mode or not; without it, or with `null`, the tile
follows its component's `framed` hint, then the dashboard's `tile_cards` setting) and
`area` (`grid`, `top` or `sidebar`: where the node is shown). Without an `area`, control
components (registered with `sidebar=True`) go where the dashboard's `controls_position`
setting puts them and every other node gets a tile on the grid. Controls, KPIs and text
may be placed in the `top` bar or the `sidebar`; an `area` that puts a chart, table or
data preview beside the grid is ignored and the node stays on the grid. Entries in the
top bar and the sidebar keep their `width` and `height` for when the node is moved back
to the grid, and their order in the list is their order in their area. A tile in a
`responsive` layout may not set `framed` or `area`, since a tile's frame and area are
the same at every screen size. Other keys are rejected, except `x-` keys, which are
ignored. Nodes without a tile entry get their component's default size, after the
listed tiles.

## Report

A report is the dashboard read as a document: headings and prose around the nodes'
live outputs, in an order of its own. A dashboard has at most one. Its blocks point
at nodes, groups and turns by name and hold text, never data, so the report always
shows what the nodes compute now.

```yaml
report:
  title: Q3 revenue review
  blocks:
  - heading: Summary
  - text: |
      Revenue in EMEA is {{ total_revenue.value:,.0f }} this quarter.
  - node: revenue_kpi
  - group: regional
  - node: churn_table
    show: table
    caption: Accounts lost, by month
  - turn: t9f8e7d6c
```

| Key | Required | Meaning |
| --- | --- | --- |
| `title` | no | The report's title; the dashboard's title is used without one. |
| `blocks` | no | The blocks in reading order, a list. |

Each block has exactly one of the keys below, which gives its kind, plus the keys of
that kind:

| Block | Keys | Shows |
| --- | --- | --- |
| `heading` | `level` (1, 2 or 3) | A heading that starts a section. |
| `text` | | Markdown prose, see below for live values. |
| `node` | `show` (`view` or `table`), `caption`, `height` (pixels) | The node's live output, as its tile shows it, or its first output as a table. |
| `group` | | A section: the group's label, then its shown nodes in the order of their tiles. It follows the group as nodes join or leave it. |
| `turn` | | A question the report asks: the turn's question, then the nodes it made; a `pending` turn is a question not answered yet. |

Every block may also have `origin`, the turn that wrote it, and `page_break: true`,
which starts it on a new page when the report is printed or exported. `node`, `group`,
`turn` and `origin` must name a node, group or turn of the file. Other keys are
rejected, except `x-` keys, which are ignored.

A node in the report need not have a tile: a node may be on the dashboard and in the
report, only on the dashboard, or only in the report. Removing a node removes the
blocks that show it in the same edit, so one Undo brings both back, and removing a
group removes its blocks.

Text blocks may use `{{ <node>.<port> }}` placeholders, which show the current value of
that output and follow it as it changes. As in the Markdown component, `{{
<node>.<port>.<key> }}` shows a key of a dict or a pandas Series and a format spec
after a colon formats a number, e.g. `{{ total.value:,.0f }}`. Values are filled in
after the Markdown is read, so they are always literal text, and never into a link or
image URL. Only text, numbers, dates and times, and lists and mappings of them are
written out; any other object shows only its type's name, so an object holding a
connection or a credential never shows what it holds. A placeholder naming no node or
output, a node of a component the reader may not use, or a failed output shows
nothing.

A dashboard without a `report` reads as a report derived from its layout: a group
block for each group with a shown tile, where its first tile is, and a node block for
every other tile, headings and Markdown included, in the order of the top bar, the
sidebar and the grid. Controls are not blocks; they are listed as the filters the
report's values depend on.

## Not in the format

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
