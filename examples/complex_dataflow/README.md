# Complex Dataflow Example

A multi-component dataflow dashboard analyzing US wind turbine data.

## Components

- **Wind Turbines Source**: loads the HoloViz wind turbines parquet dataset
- **State Filter**: filters by US state, outputs a filtered DataFrame
- **Data Table**: displays filtered data in a Tabulator widget
- **Capacity by Year**: bar chart of installed capacity per year
- **Location Map**: scatter plot of turbine locations colored by capacity

## Dataflow

```
Wind Turbines Source
        |
        | dataset
        v
   State Filter
        |
        | filtered
        v
   +---------+---------+
   |         |         |
Data Table  Capacity  Location
            by Year   Map
```

## Run

```bash
flowdash serve examples/complex_dataflow/
```

Open the Components route, add all five components, then wire:

1. `data_source.dataset` -> `state_filter.dataset`
2. `state_filter.filtered` -> `table_view.filtered`
3. `state_filter.filtered` -> `capacity_plot.filtered`
4. `state_filter.filtered` -> `scatter_map.filtered`

## Requirements

```bash
pip install pandas hvplot
```
