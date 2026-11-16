"""Readers for the files bundled in ``_data/``, cached once per server process."""

from pathlib import Path

import pandas as pd
import panel as pn

DATA_DIR = Path(__file__).parents[1] / "_data"
TURBINES_FILE = DATA_DIR / "turbines.csv.gz"
POPULATION_FILE = DATA_DIR / "state_population.csv.gz"

_TURBINE_DTYPES = {
    "t_state": "string",
    "t_county": "string",
    "p_name": "string",
    "p_year": "Int64",
    "t_manu": "string",
    "t_model": "string",
    "t_cap": "Int64",
}


def _read_turbines() -> pd.DataFrame:
    return pd.read_csv(TURBINES_FILE, dtype=_TURBINE_DTYPES)


def _read_population() -> pd.DataFrame:
    return pd.read_csv(POPULATION_FILE, dtype={"state": "string", "region": "string"})


def load_turbines() -> pd.DataFrame:
    """Return a copy of the US Wind Turbine Database extract."""
    return pn.state.as_cached("wind_energy_turbines", _read_turbines).copy()


def load_population() -> pd.DataFrame:
    """Return a copy of the Census Bureau state population estimates."""
    return pn.state.as_cached("wind_energy_population", _read_population).copy()
