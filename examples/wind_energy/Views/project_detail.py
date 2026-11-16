"""Facts about one wind project; waits until a project is picked."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._common import heading, message


def _fact(label, value):
    return pn.Column(
        pmui.Typography(label, variant="caption", sx={"color": "text.secondary"}, margin=0),
        pmui.Typography(value, variant="h6", margin=0),
        margin=(4, 16, 4, 10),
    )


def _median(series, unit):
    series = series.dropna()
    return "n/a" if series.empty else f"{series.median():,.0f} {unit}"


@register(
    component=True,
    page=False,
    title="Project Detail",
    icon="description",
    description="Location, size and turbine models of the project picked in the Project Picker.",
    tags=["view", "detail"],
    placeholder="Pick a project in the sidebar to see its turbines",
    extensions=["tabulator"],
    default_size={"width": 560, "height": 360},
    min_size={"width": 360, "height": 260},
)
class app(pn.viewable.Viewer):
    project = param.String(
        default=None,
        allow_None=True,
        metadata={"flowdash": {"required": True, "label": "Project"}},
    )

    turbines = param.DataFrame(default=None, allow_None=True)

    @param.depends("project", "turbines")
    def _view(self):
        df = self.turbines
        if df is None or self.project is None:
            return message("Waiting for turbines.")
        rows = df[df["p_name"] == self.project]
        if rows.empty:
            return message(f"{self.project} is not in the current selection.")
        first = rows.iloc[0]
        years = rows["p_year"].dropna()
        models = (
            rows.groupby([rows["t_manu"].fillna("Unknown"), rows["t_model"].fillna("Unknown")])
            .size()
            .rename("Turbines")
            .rename_axis(["Manufacturer", "Model"])
            .reset_index()
            .sort_values("Turbines", ascending=False)
        )
        facts = pn.FlexBox(
            _fact("Turbines", f"{len(rows):,}"),
            _fact("Capacity", f"{rows['t_cap'].sum() / 1000:,.1f} MW"),
            _fact("Commissioned", f"{years.min()}" if not years.empty else "n/a"),
            _fact("Hub height", _median(rows["t_hh"], "m")),
            _fact("Rotor", _median(rows["t_rd"], "m")),
            margin=0,
        )
        return pn.Column(
            heading(self.project, f"{first['t_county']}, {first['t_state']}"),
            facts,
            pn.widgets.Tabulator(
                models,
                disabled=True,
                show_index=False,
                layout="fit_columns",
                theme="simple",
                sizing_mode="stretch_both",
                margin=(0, 10, 10, 10),
            ),
            sizing_mode="stretch_both",
        )

    def __panel__(self):
        return pn.panel(self._view, sizing_mode="stretch_both")
