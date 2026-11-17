"""The transform editor shows stored step arguments and column names as typed, without math."""

import pandas as pd
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.transform_editor import TransformEditor
from panel_flowdash.ui.text import escape_markdown

PAYLOAD = r"$$\href{https://evil.test/href}{CLICKME}$$ \(x\) C:\Users *a* `b`"


@register(
    page=False, component=True, title="Rows", provides=[{"key": "rows", "type": "DataFrame"}]
)
class Rows(Viewer):
    frame = param.DataFrame(default=pd.DataFrame({"state": ["TX", PAYLOAD], PAYLOAD: [1, 2]}))

    @param.output(param.DataFrame)
    @param.depends("frame")
    def rows(self):
        return self.frame

    def __panel__(self):
        return "rows"


@register(
    page=False, component=True, title="Table", requires=[{"key": "rows", "type": "DataFrame"}]
)
class Table(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return "table"


def transform_editor(transform):
    editor = FlowDash(
        {"t/rows": Rows, "t/table": Table}, include_builtin_components=False, notifications=False
    )
    editor.load_model(
        DashboardModel(
            dashboard_id="d",
            user_id="u",
            title="T",
            items=[DashboardItem("a", "t/rows"), DashboardItem("b", "t/table")],
            edges=[DashboardEdge("a", "rows", "b", "rows", transform=transform)],
        )
    )
    view = TransformEditor(editor, next(iter(editor._edge_id_map)))
    view.__panel__()
    return view


def _typographies(view):
    return list(view._body.select(pmui.Typography))


STEPS = [
    {"op": "filter", "column": "state", "comparison": "==", "value": PAYLOAD},
    {"op": "sort", "column": PAYLOAD, "ascending": True},
]


async def test_step_arguments_are_escaped_like_the_rest_of_the_app():
    texts = [str(t.object) for t in _typographies(transform_editor(STEPS))]
    titles = [text for text in texts if escape_markdown(PAYLOAD) in text]
    assert len(titles) == 2
    assert not any("$$" in text or "\\\\" in text for text in texts)


async def test_every_typography_has_math_off():
    typographies = _typographies(transform_editor(STEPS))
    assert typographies
    assert all(t.disable_math for t in typographies)
