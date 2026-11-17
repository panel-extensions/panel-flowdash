"""Turbine locations on a deck.gl map, coloured by commissioning year or size."""

import numpy as np
import panel as pn
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message

COLOR_COLUMNS = {"year": "p_year", "capacity": "t_cap"}

BASEMAPS = {
    "light": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    "dark": "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    "none": None,
}

# Viridis stops, sampled at 0, 0.25, 0.5, 0.75 and 1.
_RAMP = np.array(
    [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]], dtype=float
)


def _colors(values):
    norm = np.nan_to_num(
        (values - np.nanmin(values)) / max(np.nanmax(values) - np.nanmin(values), 1)
    )
    position = norm * (len(_RAMP) - 1)
    lower = np.floor(position).astype(int).clip(0, len(_RAMP) - 2)
    frac = (position - lower)[:, None]
    return (_RAMP[lower] * (1 - frac) + _RAMP[lower + 1] * frac).astype(int)


@register(
    component=True,
    page=False,
    title="Turbine Map",
    icon="map",
    description="Each turbine as a point on a map, coloured by commissioning year or rated capacity.",
    tags=["view", "map"],
    config=["color_by", "basemap", "max_points"],
    extensions=["deckgl"],
    default_size={"width": 760, "height": 460},
    min_size={"width": 360, "height": 300},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    color_by = param.Selector(
        default="year", objects=list(COLOR_COLUMNS), doc="What the point colour shows."
    )

    basemap = param.Selector(default="light", objects=list(BASEMAPS), doc="Background map tiles.")

    max_points = param.Integer(
        default=40000, bounds=(1000, 80000), doc="Larger selections are sampled down to this many."
    )

    def _points(self, df):
        df = df.dropna(subset=["xlong", "ylat"])
        if len(df) > self.max_points:
            df = df.sample(self.max_points, random_state=0)
        values = df[COLOR_COLUMNS[self.color_by]].astype(float).to_numpy()
        rgb = _colors(values)
        return df.assign(
            r=rgb[:, 0],
            g=rgb[:, 1],
            b=rgb[:, 2],
            p_name=df["p_name"].astype(object).fillna(""),
            t_manu=df["t_manu"].astype(object).fillna("unknown"),
            p_year=df["p_year"].astype(float).fillna(0).astype(int),
            t_cap=df["t_cap"].astype(float).fillna(0).astype(int),
        )[["xlong", "ylat", "r", "g", "b", "p_name", "t_manu", "p_year", "t_cap"]]

    @param.depends("turbines", "color_by", "basemap", "max_points")
    def _spec(self):
        df = self.turbines
        if is_empty(df):
            return None
        points = self._points(df)
        # Frame the middle 98% of the points, so a few outliers (Alaska, Hawaii,
        # Guam) do not zoom a single state's map out to the whole Pacific.
        lon = points["xlong"].quantile([0.01, 0.99])
        lat = points["ylat"].quantile([0.01, 0.99])
        span = max(lon.iat[1] - lon.iat[0], (lat.iat[1] - lat.iat[0]) * 1.6, 0.5)
        return {
            "initialViewState": {
                "longitude": float(lon.mean()),
                "latitude": float(lat.mean()),
                "zoom": float(np.clip(np.log2(360 / span) - 0.3, 2.5, 9)),
                "pitch": 0,
            },
            "layers": [
                {
                    "@@type": "ScatterplotLayer",
                    "data": points,
                    "getPosition": "@@=[xlong, ylat]",
                    "getFillColor": "@@=[r, g, b, 200]",
                    "getRadius": 600,
                    "radiusMinPixels": 1.5,
                    "radiusMaxPixels": 8,
                    "pickable": True,
                }
            ],
            "mapStyle": BASEMAPS[self.basemap],
        }

    def _caption(self, turbines, color_by):
        if turbines is None:
            return ""
        return f"{len(turbines):,} turbines, coloured by {color_by} from purple (lowest) to yellow (highest)"

    def __panel__(self):
        turbines = self.param.turbines
        return pn.Column(
            heading("Turbine locations", pn.bind(self._caption, turbines, self.param.color_by)),
            pn.pane.DeckGL(
                self._spec,
                tooltips={
                    "html": "<b>{p_name}</b><br>{t_manu}, {t_cap} kW<br>Commissioned {p_year}"
                },
                sizing_mode="stretch_both",
                min_height=240,
                margin=(0, 10, 10, 10),
                visible=pn.bind(has_rows, turbines),
            ),
            message("No turbines to show.", visible=pn.bind(is_empty, turbines)),
            sizing_mode="stretch_both",
        )
