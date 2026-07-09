"""Scatter plot of turbine locations colored by capacity using DeckGL."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Location Map", config=["zoom", "radius_scale"])
class app(pn.viewable.Viewer):
    """Scatter plot of turbine lat/lon colored by capacity using DeckGL.

    ``zoom`` and ``radius_scale`` are set in the node editor rather than wired.
    """

    filtered = param.DataFrame()

    zoom = param.Number(default=4, bounds=(1, 12), step=0.5, doc="Initial map zoom level.")

    radius_scale = param.Number(
        default=1.0, bounds=(0.25, 4), step=0.25, doc="Multiplier for point radii."
    )

    @param.depends("filtered", "zoom", "radius_scale")
    def _transform(self):
        df = self.filtered
        if df is None or df.empty or "xlong" not in df.columns:
            return pn.pane.Markdown("*No location data available.*")

        df = df.dropna(subset=["xlong", "ylat", "t_cap"])
        if df.empty:
            return pn.pane.Markdown("*No location data available.*")

        cap = df["t_cap"]
        cap_min, cap_max = cap.min(), cap.max()
        cap_range = cap_max - cap_min if cap_max != cap_min else 1

        data = []
        for _, row in df.iterrows():
            norm = (row["t_cap"] - cap_min) / cap_range
            r = int(68 + norm * (253 - 68))
            g = int(1 + norm * (231 - 1))
            b = int(84 + norm * (37 - 84))
            data.append(
                {
                    "position": [row["xlong"], row["ylat"]],
                    "color": [r, g, b, 180],
                    "radius": (3000 + norm * 7000) * self.radius_scale,
                    "name": row.get("p_name", ""),
                    "capacity": row["t_cap"],
                    "year": row.get("p_year", ""),
                }
            )

        center_lon = df["xlong"].mean()
        center_lat = df["ylat"].mean()

        spec = {
            "initialViewState": {
                "longitude": center_lon,
                "latitude": center_lat,
                "zoom": self.zoom,
                "pitch": 0,
            },
            "layers": [
                {
                    "@@type": "ScatterplotLayer",
                    "data": data,
                    "getPosition": "@@=position",
                    "getFillColor": "@@=color",
                    "getRadius": "@@=radius",
                    "pickable": True,
                    "opacity": 0.8,
                    "radiusMinPixels": 2,
                    "radiusMaxPixels": 20,
                }
            ],
            "mapStyle": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        }

        return pn.pane.DeckGL(spec, sizing_mode="stretch_both", height=400, min_width=300)

    def __panel__(self):
        return pn.panel(self._transform)
