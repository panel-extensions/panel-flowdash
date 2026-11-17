"""Facts about one wind project; waits until a project is picked."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

from ._common import has_rows, heading, is_empty, message


def _fact(label, value):
    return pn.Column(
        pmui.Typography(label, variant="caption", sx={"color": "text.secondary"}, margin=0),
        pmui.Typography(value, variant="h6", margin=0),
        margin=(4, 16, 4, 10),
    )


def _median(series, unit):
    series = series.dropna()
    return "n/a" if series.empty else f"{series.median():,.0f} {unit}"


def _commissioned(rows):
    years = rows["p_year"].dropna()
    return "n/a" if years.empty else f"{years.min()}"


def _models(rows):
    return (
        rows.groupby([rows["t_manu"].fillna("Unknown"), rows["t_model"].fillna("Unknown")])
        .size()
        .rename("Turbines")
        .rename_axis(["Manufacturer", "Model"])
        .reset_index()
        .sort_values("Turbines", ascending=False)
    )


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

    # The turbines of the picked project, empty when it is not in the selection.
    _rows = param.DataFrame(default=None, allow_None=True)

    @param.depends("project", "turbines", watch=True, on_init=True)
    def _select(self):
        df = self.turbines
        self._rows = (
            None if df is None or self.project is None else df[df["p_name"] == self.project]
        )

    def _bind(self, describe):
        """Bind *describe* to the picked project's turbines, while there are any."""
        return pn.bind(lambda rows: None if is_empty(rows) else describe(rows), self.param._rows)

    def _message(self, project, rows):
        if rows is None:
            return "Waiting for turbines."
        return f"{project} is not in the current selection."

    def __panel__(self):
        rows = self.param._rows
        facts = pn.FlexBox(
            _fact("Turbines", self._bind(lambda rows: f"{len(rows):,}")),
            _fact("Capacity", self._bind(lambda rows: f"{rows['t_cap'].sum() / 1000:,.1f} MW")),
            _fact("Commissioned", self._bind(_commissioned)),
            _fact("Hub height", self._bind(lambda rows: _median(rows["t_hh"], "m"))),
            _fact("Rotor", self._bind(lambda rows: _median(rows["t_rd"], "m"))),
            margin=0,
        )
        place = self._bind(lambda rows: f"{rows.iloc[0]['t_county']}, {rows.iloc[0]['t_state']}")
        return pn.Column(
            pn.Column(
                heading(self.param.project, place),
                facts,
                pn.widgets.Tabulator(
                    self._bind(_models),
                    disabled=True,
                    show_index=False,
                    layout="fit_columns",
                    theme="simple",
                    sizing_mode="stretch_both",
                    margin=(0, 10, 10, 10),
                ),
                sizing_mode="stretch_both",
                visible=pn.bind(has_rows, rows),
            ),
            message(
                pn.bind(self._message, self.param.project, rows),
                visible=pn.bind(is_empty, rows),
            ),
            sizing_mode="stretch_both",
        )
