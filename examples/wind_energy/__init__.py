import json
import logging
import os
from pathlib import Path

import panel as pn
import param

from panel_flowdash import (
    DashboardModel,
    DashboardSettings,
    DashboardStore,
    register_port_type,
    resolve_identity,
)

pn.extension("tabulator", "vega", "deckgl")

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent
SEED_DIR = PROJECT_DIR / "_dashboards"

# A turbine table feeds any DataFrame input, but only a turbine table feeds an
# input typed "Turbines", and the population table only feeds "StatePopulation".
# Registered here, before any component is loaded: in a section package the types
# would only exist once a component of that section was placed.
register_port_type("Turbines", subtype_of=["DataFrame"])
register_port_type("StatePopulation", subtype_of=["DataFrame"])


class WindSettings(DashboardSettings):
    capacity_unit = param.Selector(
        default="GW", objects=["GW", "MW"], doc="Unit for capacities in KPIs and charts."
    )

    color_scheme = param.Selector(
        default="teals",
        objects=["teals", "blues", "greens", "purples", "viridis"],
        doc="Vega colour scheme of the bar charts.",
    )


def seed_dashboards(store, owner, seed_dir=SEED_DIR):
    """Import the dashboards in *seed_dir* that *store* does not have yet.

    Each file is a dashboard export; its file name is the dashboard id, so a
    dashboard that was already imported, and possibly edited since, is left alone.
    Returns the ids that were imported.
    """
    imported = []
    for path in sorted(seed_dir.glob("*.json")):
        dashboard_id = path.stem
        if store.find_by_id_or_title(dashboard_id) is not None:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        model = DashboardModel.from_dict({**data, "dashboard_id": dashboard_id, "user_id": owner})
        store.save_dashboard(model)
        imported.append(dashboard_id)
    return imported


store = DashboardStore(os.environ.get("WIND_ENERGY_DB") or PROJECT_DIR / "dashboards.db")
# Outside a session the identity is the account running the server, which then
# owns the example dashboards.
for dashboard_id in seed_dashboards(store, resolve_identity().user):
    log.info("Imported the example dashboard %r", dashboard_id)

# Only members of "analysts" may use the Raw Data Export component. By default
# that is the account running the server, i.e. you on a local, auth-less run;
# WIND_ENERGY_ANALYSTS lists other users instead (empty for nobody).
_analysts = os.environ.get("WIND_ENERGY_ANALYSTS")
_analysts = [resolve_identity().user] if _analysts is None else _analysts.split(",")
user_groups = {name.strip(): ["analysts"] for name in _analysts if name.strip()}

title = "Wind Energy Explorer"
# Carrying failures along edges needs Param 2.5; older releases only log them.
propagate_failures = hasattr(param, "ReactiveError")
settings_class = WindSettings
editor_options = {"inspect_outputs": True}
# The project has no contextbar; a persistent one would still reserve its width
# beside the dashboard while closed.
page_options = {"contextbar_variant": "temporary"}
