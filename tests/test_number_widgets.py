"""The KPI's format picker and thresholds editor."""

import panel as pn
import param
import pytest

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.choice_widgets import plain_widget
from panel_flowdash.ui.number_widgets import (
    FormatInput,
    ThresholdsInput,
    format_example,
    format_problem,
)
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.widgets import KPI, check_kpi

from .test_config_choices import _settle


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
        assert format_example(",.1%") == "12.3%"
        assert format_example("") == "1,234.57"
        assert format_example(",d") == "1,235"
        assert format_problem(",.2f") is None
        assert format_problem(" .2f") is None
        assert format_problem(",d") is None
        assert (
            format_problem(".3q")
            == "'.3q' is not a number format; try e.g. ',.2f', '.1%' or ',d'."
        )

    def test_the_kpi_fields_get_their_editors(self):
        assert plain_widget(KPI.param.format, "") == {"type": FormatInput}
        assert plain_widget(KPI.param.thresholds, []) == {"type": ThresholdsInput}
        assert plain_widget(param.String(), "") is None

    def test_a_preset_sets_its_spec(self):
        kpi = KPI()
        field, _ = _widgets(kpi)
        assert field._preset.value == "" and not field._custom.visible
        field._preset.value = ",.1%"
        assert kpi.format == ",.1%"

    def test_a_custom_spec_is_checked_before_it_applies(self):
        kpi = KPI(format=",.1f")
        field, _ = _widgets(kpi)
        field._preset.value = "custom"
        assert field._custom.visible and field._custom.value == ",.1f"
        field._custom.value = ".3q"
        assert kpi.format == ",.1f"
        assert "is not a number format" in field._note.object
        field._custom.value = ",.3f"
        assert kpi.format == ",.3f" and field._note.object == "Shows like 1,234.568"
        field._custom.value = " .2f"
        assert kpi.format == " .2f", "a leading space is part of the spec"

    def test_a_stored_custom_spec_opens_as_custom(self):
        field, _ = _widgets(KPI(format="+,.0f"))
        assert field._preset.value == "custom" and field._custom.value == "+,.0f"


class TestThresholds:
    def test_rows_add_edit_and_remove_pairs(self):
        kpi = KPI()
        _, editor = _widgets(kpi)
        assert len(editor._rows) == 0 and not editor._hint.visible
        for _ in range(3):
            editor._add.clicks += 1
        assert kpi.thresholds == [[100, "error"], [200, "warning"], [300, "success"]]
        (limit, color, _), picker = editor._rows[0]
        limit.value = 50.0
        assert kpi.thresholds[0] == [50, "error"] and isinstance(kpi.thresholds[0][0], int)
        limit.value = 50.5
        assert kpi.thresholds[0] == [50.5, "error"]
        color.value = "custom"
        assert picker.visible and kpi.thresholds[0] == [50.5, "error"], "nothing stored yet"
        picker.value = "#ff0000"
        assert kpi.thresholds[0] == [50.5, "#ff0000"]
        editor._rows[1][0][2].clicks += 1
        assert kpi.thresholds == [[50.5, "#ff0000"], [300, "success"]]

    def test_colors_it_does_not_offer_are_kept(self):
        kpi = KPI(
            thresholds=[[50, "red"], [80, "rgb(0, 128, 0)"], [90, "danger"], [100, "#00aa00"]]
        )
        _, editor = _widgets(kpi)
        colors = [row[0][1].value for row in editor._rows]
        assert colors == ["red", "rgb(0, 128, 0)", "danger", "#00aa00"]
        assert "red" in editor._rows[0][0][1].options
        editor._rows[1][0][0].value = 85.0
        assert kpi.thresholds[1] == [85, "rgb(0, 128, 0)"]
        assert kpi.thresholds[0] == [50, "red"]

    def test_pairs_it_cannot_read_are_kept(self):
        kpi = KPI(thresholds=[[50, "error"], [2], "junk", [80, "success"]])
        _, editor = _widgets(kpi)
        assert len(editor._rows) == 2
        editor._rows[1][0][0].value = 90.0
        assert kpi.thresholds == [[50, "error"], [2], "junk", [90, "success"]]

    def test_a_value_set_elsewhere_redraws_the_rows(self):
        kpi = KPI()
        _, editor = _widgets(kpi)
        kpi.thresholds = [[10, "info"]]
        assert len(editor._rows) == 1 and editor._rows[0][0][1].value == "info"

    def test_the_fields_keep_their_doc_tooltips(self):
        field, editor = _widgets(KPI())
        assert "format spec" in field._preset.description
        assert "[limit, color]" in editor._label[1].value


class TestCheck:
    def test_the_kpi_refuses_what_its_fields_refuse(self):
        problems = check_kpi(
            {"format": "garbage", "thresholds": [[1, "nope;}"], [1, 2, 3], [2], ["a", "red"]]},
            {},
        )
        assert problems == [
            "'garbage' is not a number format; try e.g. ',.2f', '.1%' or ',d'.",
            "Threshold 1: 'nope;}' is not a color; use a theme color (primary, secondary, "
            "success, warning, error, info) or a CSS color like 'red' or '#22aa66'.",
            "Threshold 2 is not a [limit, color] pair: [1, 2, 3].",
            "Threshold 3 is not a [limit, color] pair: [2].",
            "Threshold 4 has no number as its limit: 'a'.",
        ]
        assert check_kpi({"format": ",d", "thresholds": [[1, "red"], [2, "danger"]]}, {}) == []

    def test_the_agent_is_refused_too(self):
        from panel_flowdash.agent import DashboardOps, OperationError

        editor = FlowDash(notifications=False)
        editor.new_dashboard("KPIs")
        ops = DashboardOps(editor)
        name = ops.add_node("Widgets/KPI", name="total")
        with pytest.raises(OperationError, match="not a number format"):
            ops.set_config(name, {"format": "garbage"})
        with pytest.raises(OperationError, match="not a color"):
            ops.set_config(name, {"thresholds": [[1, "nope;}"]]})
        ops.set_config(name, {"format": ",.1f", "thresholds": [[1, "red"]]})
        editor.dispose()


async def test_the_inspector_edits_a_placed_kpi():
    editor = FlowDash(notifications=False, history_coalesce=0)
    node = editor.add_component("Widgets/KPI")
    select_on_canvas(editor._flow, node_id=node)
    body = pn.Column(*editor._inspector._body)
    (field,) = body.select(FormatInput)
    (thresholds,) = body.select(ThresholdsInput)
    await _settle()
    field._preset.value = ",.0f"
    await _settle()
    thresholds._add.clicks += 1
    await _settle()
    config = editor._tile_items[0]["config"]
    assert config["format"] == ",.0f" and config["thresholds"] == [[100, "error"]]
    assert editor._component_instances[node].thresholds == [[100, "error"]]
    editor.dispose()
