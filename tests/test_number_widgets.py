"""The KPI's format picker and thresholds editor."""

import panel as pn
import param

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.choice_widgets import plain_widget
from panel_flowdash.ui.number_widgets import (
    FormatInput,
    ThresholdsInput,
    format_example,
    format_problem,
)
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.widgets import KPI


def _widgets(kpi):
    form = pn.Param(
        kpi,
        parameters=["format", "thresholds"],
        widgets={
            "format": plain_widget(KPI.param.format, kpi.format),
            "thresholds": plain_widget(KPI.param.thresholds, kpi.thresholds),
        },
        show_name=False,
    )
    return form.layout[0], form.layout[1]


class TestFormat:
    def test_examples_and_problems(self):
        assert format_example(",.0f") == "1,235"
        assert format_example(".1%") == "12.3%"
        assert format_example("") == "1,234.57"
        assert format_problem(",.2f") is None
        assert "Unknown format code 'q'" in format_problem(".3q")

    def test_the_kpi_fields_get_their_editors(self):
        assert plain_widget(KPI.param.format, "") == {"type": FormatInput}
        assert plain_widget(KPI.param.thresholds, []) == {"type": ThresholdsInput}
        assert plain_widget(param.String(), "") is None

    def test_a_preset_sets_its_spec(self):
        kpi = KPI()
        field, _ = _widgets(kpi)
        assert field._preset.value == "" and not field._custom.visible
        field._preset.value = ".1%"
        assert kpi.format == ".1%"

    def test_a_custom_spec_is_checked_before_it_applies(self):
        kpi = KPI(format=",.1f")
        field, _ = _widgets(kpi)
        field._preset.value = "custom"
        assert field._custom.visible and field._custom.value == ",.1f"
        field._custom.value = ".3q"
        assert kpi.format == ",.1f"
        assert field._note.object.startswith("Not a number format")
        field._custom.value = ",.3f"
        assert kpi.format == ",.3f" and field._note.object == "Shows like 1,234.568"

    def test_a_stored_custom_spec_opens_as_custom(self):
        field, _ = _widgets(KPI(format="+,.0f"))
        assert field._preset.value == "custom" and field._custom.value == "+,.0f"


class TestThresholds:
    def test_rows_add_edit_and_remove_pairs(self):
        kpi = KPI()
        _, editor = _widgets(kpi)
        assert len(editor._rows) == 0 and not editor._hint.visible
        editor._add.clicks += 1
        editor._add.clicks += 1
        assert kpi.thresholds == [[100, "error"], [200, "warning"]]
        (limit, color, _), picker = editor._rows[0][0], editor._rows[0][1]
        limit.value = 50
        assert kpi.thresholds[0] == [50, "error"]
        color.value = "custom"
        assert picker.visible
        picker.value = "#ff0000"
        assert kpi.thresholds[0] == [50, "#ff0000"]
        editor._rows[1][0][2].clicks += 1
        assert kpi.thresholds == [[50, "#ff0000"]]

    def test_stored_pairs_show_as_rows(self):
        _, editor = _widgets(KPI(thresholds=[[50, "error"], [80, "#00aa00"], "junk"]))
        assert len(editor._rows) == 2
        assert editor._rows[0][0][1].value == "error" and not editor._rows[0][1].visible
        assert editor._rows[1][0][1].value == "custom" and editor._rows[1][1].value == "#00aa00"

    def test_a_value_set_elsewhere_redraws_the_rows(self):
        kpi = KPI()
        _, editor = _widgets(kpi)
        kpi.thresholds = [[10, "info"]]
        assert len(editor._rows) == 1 and editor._rows[0][0][1].value == "info"


async def test_the_inspector_edits_a_placed_kpi():
    editor = FlowDash(notifications=False, history_coalesce=0)
    node = editor.add_component("Widgets/KPI")
    select_on_canvas(editor._flow, node_id=node)
    body = pn.Column(*editor._inspector._body)
    (field,) = body.select(FormatInput)
    (thresholds,) = body.select(ThresholdsInput)
    field._preset.value = ",.0f"
    thresholds._add.clicks += 1
    config = editor._tile_items[0]["config"]
    assert config["format"] == ",.0f" and config["thresholds"] == [[100, "error"]]
    assert editor._component_instances[node].thresholds == [[100, "error"]]
    editor.dispose()
