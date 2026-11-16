"""A standalone page charting how turbines grew over four decades."""

from pathlib import Path

import pandas as pd
import panel as pn
import panel_material_ui as pmui

from panel_flowdash import register

DATA_DIR = Path(__file__).parents[1] / "_data"

MEASURES = {
    "t_cap": "Capacity (kW)",
    "t_rd": "Rotor (m)",
    "t_hh": "Hub height (m)",
}


def _yearly(measure):
    df = pd.read_csv(DATA_DIR / "turbines.csv.gz", usecols=["p_year", measure])
    by_year = df.dropna().groupby("p_year")[measure].agg(["median", "max"]).reset_index()
    by_year["p_year"] = by_year["p_year"].astype(int)
    return by_year.round(1)


def _chart(measure):
    frame = _yearly(measure)
    label = MEASURES[measure]
    muted = "#8a8f98"
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": frame.to_dict(orient="records")},
        "width": "container",
        "height": "container",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": {
            "background": None,
            "view": {"stroke": None},
            "axis": {
                "labelColor": muted,
                "titleColor": muted,
                "gridColor": "rgba(138,143,152,0.25)",
            },
            "legend": {"labelColor": muted, "titleColor": muted},
        },
        "transform": [{"fold": ["median", "max"], "as": ["statistic", "value"]}],
        "mark": {"type": "line", "point": True, "strokeWidth": 2},
        "encoding": {
            "x": {
                "field": "p_year",
                "type": "quantitative",
                "title": None,
                "scale": {"zero": False, "nice": False},
                "axis": {"format": "d", "tickCount": 10},
            },
            "y": {"field": "value", "type": "quantitative", "title": label},
            "color": {
                "field": "statistic",
                "type": "nominal",
                "scale": {"domain": ["median", "max"], "range": ["#2a9d8f", "#e76f51"]},
                "legend": {"orient": "top", "title": None},
            },
            "tooltip": [
                {"field": "p_year", "title": "Year"},
                {"field": "statistic", "title": "Statistic"},
                {"field": "value", "title": label},
            ],
        },
    }
    return pn.pane.Vega(spec, sizing_mode="stretch_width", height=420)


@register(
    page=True,
    title="Fleet Growth",
    icon="trending_up",
    description="How turbine capacity, rotor diameter and hub height grew since 1981.",
)
def app():
    measure = pmui.RadioButtonGroup(
        options={label: key for key, label in MEASURES.items()}, value="t_cap"
    )
    return pmui.Container(
        pmui.Typography("Fleet growth", variant="h4", margin=(16, 0, 4, 0)),
        pmui.Typography(
            "Median and largest turbine commissioned each year. A standalone page, "
            "outside any dashboard.",
            variant="body1",
            sx={"color": "text.secondary"},
        ),
        measure,
        pn.bind(_chart, measure),
        width_option="md",
    )
