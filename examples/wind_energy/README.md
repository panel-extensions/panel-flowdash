# Wind Energy Explorer

A FlowDash project built around the US Wind Turbine Database: 20 components,
two pages and three ready-made dashboards that between them use every feature
FlowDash has. It needs no network connection; both datasets ship with the
example.

## Run it

```bash
flowdash serve examples/wind_energy
```

On first start the three dashboards in `_dashboards/` are imported into the
project's store, owned by the account running the server. A dashboard that is
already in the store is left alone, so your edits survive restarts; delete
`dashboards.db` to start over. Set `WIND_ENERGY_DB` to keep the database
somewhere else (the project defines its own `store`, so `--db-path` does not
apply).

Failure propagation needs Param 2.5 or later; with an older Param the quality
gate's failures are only logged.

## What to look at

| Feature | Where |
| --- | --- |
| Seeded dashboards in the launcher | *Fleet Overview*, *State Deep Dive*, *Data Quality*; imported by `seed_dashboards` in `__init__.py` |
| Project configuration from `__init__.py` | `title`, `store`, `settings_class`, `propagate_failures`, `editor_options` (`inspect_outputs`), `user_groups`, `page_options` |
| Dashboard settings and refresh | `WindSettings` adds a capacity unit and a colour scheme, which charts and KPIs follow; *Fleet Overview* refreshes every 15 minutes and the turbine source reloads on every refresh |
| Sidebar widgets as controls | *Year Range*, *State Picker* and *Project Picker* (`sidebar=True`) |
| Outputs declared on a Parameter | `state`, `years` and `project` use `metadata={"flowdash": {"output": True}}` |
| Port types | `register_port_type` in `Sources/__init__.py`: a `Turbines` table feeds any DataFrame input, but only `StatePopulation` feeds *Join State Population*'s population input |
| Multi-input wiring | *Join State Population* joins the turbines with the Census population table into watts per person (*State Deep Dive*, State ranking) |
| Built-in edge transforms | *Fleet Overview*: sort by capacity and keep the first 10 rows into *Largest projects*; *State Deep Dive*: filter turbines of 3 MW and up into a KPI |
| Node configuration | KPI `metric` and `label`, map `color_by` and `basemap`, gate thresholds, table `columns`; one KPI component placed several times with different settings |
| Required input and waiting placeholder | *Project Detail* waits for a project with its own `placeholder` text (*State Deep Dive*) |
| Failure propagation and recovery | *Data Quality*: pick CT in the State Picker and the *Quality Gate* fails, the tiles behind it show the failure, *Gate Status* receives it through `handles_failures`; pick TX to recover |
| Output inspection | Every tile with outputs has the inspect button (`inspect_outputs=True`) |
| Size hints | `default_size`, `min_size` and `max_size` on every component; KPIs are short and capped in height, the map is wide |
| Palette grouping | `source`, `transform`, `view` and `widget` tags, a `description` and a Material icon on every component |
| Component permissions | *Raw Data Export* is limited to the `analysts` group. Locally you are in it; set `WIND_ENERGY_ANALYSTS` to a comma-separated list of users (empty for nobody) to see it locked |
| Pages | *About the Data* and *Fleet Growth* in `Explore/` are ordinary pages next to the dashboards |

## Layout

```
wind_energy/
    __init__.py         project configuration, settings and dashboard seeding
    Sources/            US Wind Turbines, State Population (+ _data.py readers)
    Controls/           Year Range, State Picker, Project Picker (sidebar widgets)
    Transforms/         Filter Turbines, Project Summary, Join State Population, Quality Gate
    Views/              KPI, Turbine Map, Capacity Over Time, Manufacturers, Turbine Models,
                        Data Table, Project Detail, State Ranking, Data Completeness,
                        Gate Status, Raw Data Export
    Explore/            About the Data, Fleet Growth (pages)
    _data/              bundled datasets
    _dashboards/        dashboard exports imported on first start
```

Each file in `_dashboards/` is a dashboard export, the same format the editor's
Download button writes, and its file name is the dashboard id. Download a
dashboard you have changed and save it there to ship it with the project.

## Data

**US Wind Turbine Database (USWTDB).** U.S. Geological Survey, Lawrence Berkeley
National Laboratory and American Clean Power Association,
<https://eerscmap.usgs.gov/uswtdb/>. Public domain. `_data/turbines.csv.gz`
(0.7 MB) holds all 72,357 turbines with 14 of the columns, from the copy
HoloViz publishes at `datasets.holoviz.org/windturbines/v1/windturbines.parquet`,
with coordinates rounded to four decimals.

**State population estimates.** U.S. Census Bureau, Vintage 2023 population
estimates (NST-EST2023-ALLDATA),
<https://www.census.gov/programs-surveys/popest.html>. Public domain.
`_data/state_population.csv.gz` (1 KB) holds the 2020 base and 2023 estimate and
the Census region for the 50 states, Washington DC and Puerto Rico.
