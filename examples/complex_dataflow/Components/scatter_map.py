"""Scatter plot of turbine locations colored by capacity using DeckGL."""

import panel as pn
import param

from panel_flowdash import register


@register(component=True, title="Location Map")
class app(pn.viewable.Viewer):
    """Scatter plot of turbine lat/lon colored by capacity using DeckGL."""

    filtered = param.DataFrame()

    def _transform(self, df):
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
                    "radius": 3000 + norm * 7000,
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
                "zoom": 4,
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
        return self.param.filtered.rx.pipe(self._transform)
