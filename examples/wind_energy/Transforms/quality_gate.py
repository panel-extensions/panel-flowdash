"""Stop the dataflow when a selection is too small or too incomplete to trust."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

CHECKED_FIELDS = {"p_year": "year", "t_cap": "capacity", "t_manu": "manufacturer"}


class QualityGateError(ValueError):
    """The selection did not pass the quality gate."""


def check(df: pd.DataFrame, min_turbines: int, max_missing: float) -> list[tuple[str, bool]]:
    """Return each check as (description, passed)."""
    results = [(f"At least {min_turbines:,} turbines ({len(df):,})", len(df) >= min_turbines)]
    for column, label in CHECKED_FIELDS.items():
        missing = float(df[column].isna().mean()) if len(df) else 1.0
        results.append(
            (f"{label.capitalize()} known for {1 - missing:.0%}", missing <= max_missing)
        )
    return results


@register(
    component=True,
    page=False,
    title="Quality Gate",
    icon="verified",
    description="Passes the turbines on only when the selection is large and complete enough; fails otherwise.",
    tags=["transform", "quality"],
    config=["min_turbines", "max_missing"],
    requires=[{"key": "turbines", "type": "Turbines", "label": "Turbines"}],
    provides=[{"key": "checked", "type": "Turbines", "label": "Checked"}],
    default_size={"width": 360, "height": 200},
    min_size={"width": 280, "height": 160},
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True)

    min_turbines = param.Integer(default=100, bounds=(1, None), doc="Smallest selection to pass.")

    max_missing = param.Number(
        default=0.1, bounds=(0, 1), step=0.05, doc="Largest share of missing values per field."
    )

    @param.output(param.DataFrame)
    @param.depends("turbines", "min_turbines", "max_missing")
    def checked(self):
        if self.turbines is None:
            return None
        failed = [
            name
            for name, ok in check(self.turbines, self.min_turbines, self.max_missing)
            if not ok
        ]
        if failed:
            raise QualityGateError("Selection failed the quality gate: " + "; ".join(failed))
        return self.turbines

    def _checks(self, turbines, min_turbines, max_missing):
        if turbines is None:
            return pn.pane.Markdown("Waiting for turbines", margin=(0, 10))
        rows = [
            pmui.Typography(
                f"{'✓' if ok else '✗'}  {name}",
                variant="body2",
                color="success" if ok else "error",
                margin=(0, 10),
            )
            for name, ok in check(turbines, min_turbines, max_missing)
        ]
        return pn.Column(*rows, margin=0)

    def __panel__(self):
        return pn.Column(
            pmui.Typography("Quality checks", variant="subtitle2", margin=(4, 10, 0, 10)),
            pn.bind(
                self._checks, self.param.turbines, self.param.min_turbines, self.param.max_missing
            ),
        )
