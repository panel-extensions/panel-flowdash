"""Filters the dataset by US state."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


@register(component=True, title="State Filter")
class app(pn.viewable.Viewer):
    """Filters a DataFrame to rows matching the selected state."""

    dataset = param.DataFrame()
    state = param.Selector(default="Texas", objects=["Texas"])

    @param.output(param.DataFrame)
    @param.depends("dataset", "state")
    def filtered(self):
        if self.dataset is None:
            return pd.DataFrame()
        return self.dataset[self.dataset["t_state"] == self.state]

    def __init__(self, **params):
        super().__init__(**params)
        self._update_states()

    @param.depends("dataset", watch=True)
    def _update_states(self):
        if self.dataset is not None and "t_state" in self.dataset.columns:
            states = sorted(self.dataset["t_state"].dropna().unique().tolist())
            self.param.state.objects = states
            if self.state not in states and states:
                self.state = states[0]

    def __panel__(self):
        return pn.Column(
            pmui.Select.from_param(self.param.state, label="State"),
            pn.pane.Markdown(pn.rx("**{n}** turbines in {s}").format(
                n=pn.rx(self.filtered).rx.pipe(len),
                s=self.param.state,
            )),
        )
