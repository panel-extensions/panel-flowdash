"""Pick one wind project; nothing is selected until the user chooses."""

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash import register


@register(
    component=True,
    page=False,
    sidebar=True,
    title="Project Picker",
    icon="search",
    description="Search the projects in the current selection by name. Starts empty.",
    tags=["widget", "filter"],
)
class app(pn.viewable.Viewer):
    projects = param.DataFrame(default=None, allow_None=True, doc="Project summary to pick from.")

    project = param.String(
        default=None,
        allow_None=True,
        metadata={"flowdash": {"output": True, "label": "Project"}},
    )

    _names = param.List(default=[])

    @param.depends("projects", watch=True, on_init=True)
    def _update_names(self):
        names = []
        if self.projects is not None and "project" in self.projects:
            names = sorted(self.projects["project"].dropna().unique().tolist())
        self._names = names
        if self.project not in names:
            self.project = None

    def __panel__(self):
        return pmui.AutocompleteInput.from_param(
            self.param.project,
            options=self.param._names,
            label="Project",
            placeholder="Type a project name",
            search_strategy="includes",
            min_characters=0,
            restrict=True,
            sizing_mode="stretch_width",
        )
