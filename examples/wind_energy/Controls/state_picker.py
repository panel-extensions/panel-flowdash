"""A state selector whose choice is published from a Parameter."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register

ALL = "All"


@register(
    component=True,
    page=False,
    sidebar=True,
    title="State Picker",
    icon="place",
    description="Pick one state, or all of them. The options list how many turbines each state has.",
    tags=["widget", "filter"],
    config=["initial_state"],
)
class app(pn.viewable.Viewer):
    turbines = param.DataFrame(default=None, allow_None=True, doc="Turbines to count per state.")

    initial_state = param.String(default=ALL, doc="State code selected when the dashboard opens.")

    state = param.Selector(
        default=ALL,
        objects={"All states": ALL},
        metadata={"flowdash": {"output": True, "label": "State"}},
    )

    def __init__(self, **params):
        super().__init__(**params)
        # The turbines arrive after the component is created, so the initial
        # state is applied once it is among the options.
        self._pending_state = self.initial_state
        self._update_options()

    @param.depends("turbines", watch=True)
    def _update_options(self):
        options = {"All states": ALL}
        if self.turbines is not None and "t_state" in self.turbines:
            counts = self.turbines["t_state"].value_counts().sort_index()
            options.update(
                {
                    f"{code} ({n:,} turbine{'' if n == 1 else 's'})": code
                    for code, n in counts.items()
                }
            )
        self.param.state.objects = options
        if self._pending_state in options.values():
            self.state, self._pending_state = self._pending_state, None
        elif self.state not in options.values():
            self.state = ALL

    @param.depends("initial_state", watch=True)
    def _apply_initial_state(self):
        self._pending_state = self.initial_state
        self._update_options()

    def __panel__(self):
        return pmui.Select.from_param(
            self.param.state, label="State", searchable=True, sizing_mode="stretch_width"
        )
