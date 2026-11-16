# Wind Energy Explorer

A complete project around the US Wind Turbine Database that uses every FlowDash
feature, with three dashboards ready to open. It lives in
[`examples/wind_energy`](https://github.com/panel-extensions/panel-flowdash/tree/main/examples/wind_energy)
and runs offline; the turbine and population data ship with it.

```bash
flowdash serve examples/wind_energy
```

The launcher lists three dashboards, imported into the project's store on first
start:

- **Fleet Overview**: KPIs, a map of all 72,000 turbines, capacity added per year,
  the largest manufacturers and the ten largest projects, filtered by a
  commissioning-year range in the sidebar.
- **State Deep Dive**: one state at a time, with a project picker whose choice
  fills a project detail tile, and a ranking of states by watts per person that
  joins the turbines with Census population figures.
- **Data Quality**: a quality gate that fails for small selections, so the tiles
  behind it show the failure until a larger state is picked.

Two pages, *About the Data* and *Fleet Growth*, sit next to them.

## What it shows

| Feature | Where in the example | How-to |
| --- | --- | --- |
| Project options in `__init__.py` | `title`, `store`, `settings_class`, `propagate_failures`, `editor_options`, `user_groups` | [Serve a project](../how-to/serve-project.md#project-configuration) |
| Dashboard settings and refresh | `WindSettings` (capacity unit, colour scheme); *Fleet Overview* refreshes every 15 minutes | [Dashboard settings](../how-to/dashboard-settings.md) |
| Sidebar widgets | *Year Range*, *State Picker*, *Project Picker* | [Layout & sizing](../how-to/layout-and-sizing.md#sidebar-components) |
| Outputs on Parameters | `state`, `years`, `project` | [Define ports](../how-to/define-ports.md#outputs-from-parameters) |
| Port types | `Turbines` and `StatePopulation` registered as DataFrame subtypes | [Define ports](../how-to/define-ports.md#registering-your-own-types) |
| Edge transforms | sort and head into *Largest projects*; a filter into a KPI | [Transform edges](../how-to/transform-edges.md) |
| Required inputs | *Project Detail* waits for a project | [Define ports](../how-to/define-ports.md#required-inputs) |
| Failure propagation | *Quality Gate* and *Gate Status* (`handles_failures`) | [Wire the dataflow](../how-to/wire-dataflow.md#carry-failures-downstream) |
| Size hints | `default_size`, `min_size`, `max_size` on every component | [Layout & sizing](../how-to/layout-and-sizing.md#tile-sizing-hints) |
| Component permissions | *Raw Data Export* is limited to the `analysts` group | [Manage permissions](../how-to/manage-permissions.md#restricting-components) |

The example's README lists the files behind each feature and the sources and
licenses of the data.
