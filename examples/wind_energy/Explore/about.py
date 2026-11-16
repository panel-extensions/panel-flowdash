"""A page describing the bundled datasets."""

from pathlib import Path

import pandas as pd
import panel as pn
import panel_material_ui as pmui

from panel_flowdash import register

DATA_DIR = Path(__file__).parents[1] / "_data"

TEXT = """
The dashboards in this project are built on two public datasets that ship with the
example, so it runs without a network connection.

**US Wind Turbine Database (USWTDB).** The location and specifications of every
utility-scale wind turbine in the United States, maintained by the U.S. Geological
Survey, Lawrence Berkeley National Laboratory and the American Clean Power
Association. The bundled extract keeps all {turbines:,} turbines and the columns the
components use. The USWTDB is a USGS data release in the public domain.
[eerscmap.usgs.gov/uswtdb](https://eerscmap.usgs.gov/uswtdb/)

**State population estimates.** Resident population of each state, Washington DC and
Puerto Rico for 2020 and 2023 from the U.S. Census Bureau's Vintage 2023
estimates, with each state's Census region. Census Bureau data is in the public
domain. [census.gov/programs-surveys/popest](https://www.census.gov/programs-surveys/popest.html)
"""

COLUMNS = {
    "t_state, t_county": "State code and county of the turbine",
    "p_name, p_year": "Project name and the year it began operating",
    "p_cap": "Project capacity in MW",
    "t_manu, t_model": "Turbine manufacturer and model",
    "t_cap": "Rated turbine capacity in kW",
    "t_hh, t_rd": "Hub height and rotor diameter in metres",
    "t_conf_loc": "Location confidence: 1 low, 2 partial, 3 full",
    "xlong, ylat": "Longitude and latitude (WGS84)",
}


@register(
    page=True, title="About the Data", icon="info", description="Sources, licenses and columns."
)
def app():
    turbines = pd.read_csv(DATA_DIR / "turbines.csv.gz", usecols=["case_id"])
    glossary = "| Column | Meaning |\n| --- | --- |\n" + "\n".join(
        f"| `{column}` | {meaning} |" for column, meaning in COLUMNS.items()
    )
    return pmui.Container(
        pmui.Typography("About the data", variant="h4", margin=(16, 0, 8, 0)),
        pn.pane.Markdown(TEXT.format(turbines=len(turbines)), sizing_mode="stretch_width"),
        pmui.Typography("Columns", variant="h6", margin=(16, 0, 4, 0)),
        pn.pane.Markdown(glossary, sizing_mode="stretch_width"),
        width_option="md",
    )
