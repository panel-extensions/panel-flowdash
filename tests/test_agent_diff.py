"""Describing the change between two dashboard versions in the dashboard's own terms."""

from panel_flowdash.agent.diff import dashboard_changes

OLD = """\
flowdash: 1
title: Fleet
settings: {refresh_period: 0}
nodes:
  turbines: {component: Sources/turbines, position: [0, 0]}
  kpi: {component: Views/kpi, config: {metric: turbines}, position: [400, 0]}
  table: {component: Views/data_table, position: [400, 200]}
edges:
  - {from: turbines.turbines, to: kpi.turbines}
  - {from: turbines.turbines, to: table.table}
layout:
  tiles:
    - {node: kpi, width: 50}
    - {node: table, width: 50}
"""

NEW = """\
flowdash: 1
title: Fleet overview
settings: {refresh_period: 60}
nodes:
  turbines: {component: Sources/turbines, position: [0, 0]}
  kpi: {component: Views/kpi, config: {metric: capacity}, position: [400, 0]}
  map: {component: Views/turbine_map, position: [400, 400]}
edges:
  - {from: turbines.turbines, to: kpi.turbines, transform: [{op: head, n: 5}]}
  - {from: turbines.turbines, to: map.turbines}
layout:
  tiles:
    - {node: kpi, width: 100}
"""


def test_changes_are_labelled_in_dashboard_terms():
    labels = [c.label for c in dashboard_changes(OLD, NEW)]
    assert labels == [
        "Renamed to Fleet overview",
        "Added map (Views/turbine_map)",
        "Removed table (Views/data_table)",
        "kpi: metric: turbines → capacity",
        "Connected turbines.turbines → map.turbines",
        "Disconnected turbines.turbines → table.table",
        "turbines.turbines → kpi.turbines: transform set to 1 step",
        "Tile kpi laid out differently",
        "Tile table laid out differently",
        "Setting refresh_period: 0 → 60",
    ]


def test_identical_versions_have_no_changes():
    assert dashboard_changes(OLD, OLD) == []


def test_paths_point_into_the_yaml():
    paths = {c.label: c.path for c in dashboard_changes(OLD, NEW)}
    assert paths["kpi: metric: turbines → capacity"] == ("nodes", "kpi", "config", "metric")
    assert paths["Setting refresh_period: 0 → 60"] == ("settings", "refresh_period")
