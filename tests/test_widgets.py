"""The built-in widgets and views: ports, config, values and persistence."""

import datetime as dt
import json

import panel_material_ui as pmui
import pytest

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.widgets import BUILTIN_COMPONENTS, safe_image_url

TODAY = dt.date.today()


@register(page=False, component=True, provides=[{"key": "values", "type": "List"}])
def option_source(config):
    return None


@register(page=False, component=True, provides=[{"key": "bound", "type": "Number"}])
def bound_source(config):
    return None


@register(page=False, component=True, provides=[{"key": "day", "type": "Date"}])
def date_source(config):
    return None


@register(page=False, component=True, provides=[{"key": "text", "type": "str"}])
def text_source(config):
    return None


SOURCES = {
    "Test/options": option_source,
    "Test/bound": bound_source,
    "Test/date": date_source,
    "Test/text": text_source,
}


@pytest.fixture
def editor():
    editor = FlowDash(SOURCES, notifications=False)
    yield editor
    editor.dispose()


def _place(editor, component_id, **config):
    instance_id = editor.add_component(component_id, config=config)
    return instance_id, editor._component_instances[instance_id]


def _source(editor, component_id, port, value):
    instance_id = editor.add_component(component_id)
    setattr(editor.graph.get_state(instance_id), port, value)
    return instance_id


def test_every_builtin_has_a_description_for_the_agent(editor):
    for component_id in BUILTIN_COMPONENTS:
        assert len(editor.component_specs[component_id].description) > 40


@pytest.mark.parametrize(
    ("component_id", "outputs", "inputs"),
    [
        ("Widgets/RadioGroup", {"selected": None}, {"options": "List"}),
        ("Widgets/RangeSlider", {"selected": "Range"}, {"start": "Number", "end": "Number"}),
        ("Widgets/NumberInput", {"selected": "Number"}, {"start": "Number", "end": "Number"}),
        ("Widgets/DateRange", {"selected": "DateRange"}, {"start": "Date", "end": "Date"}),
        ("Widgets/Toggle", {"checked": "Boolean"}, {}),
        ("Widgets/TextInput", {"text": "String"}, {}),
        ("Widgets/KPI", {}, {"value": "Number", "reference": "Number"}),
        ("Widgets/Image", {}, {"url": "String"}),
    ],
)
def test_ports(editor, component_id, outputs, inputs):
    spec = editor.component_specs[component_id]
    assert {p.name: p.type for p in spec.outputs} == outputs
    assert {p.name: p.type for p in spec.inputs} == inputs
    assert not any(p.required for p in spec.inputs)


class TestRadioGroup:
    def test_options_follow_config_and_port(self, editor):
        node, radio = _place(editor, "Widgets/RadioGroup", default_options=["Day", "Week"])
        assert radio._widget.options == ["Day", "Week"]
        assert editor.graph.get_state(node).selected == "Day"
        radio._widget.value = "Week"
        assert editor.graph.get_state(node).selected == "Week"

        src = _source(editor, "Test/options", "values", ["Week", "Month"])
        assert editor.connect(src, "values", node, "options") is True
        assert radio._widget.options == ["Week", "Month"]
        assert editor.graph.get_state(node).selected == "Week"

    def test_style_swaps_the_widget_in_place(self, editor):
        node, radio = _place(editor, "Widgets/RadioGroup", default_options=["A", "B"])
        view = editor._tile_objects[-1]
        assert isinstance(radio._widget, pmui.RadioBoxGroup)
        assert radio._widget.inline
        radio._widget.value = "B"

        config = editor.graph.get_config_state(node)
        config.param.update(style="buttons", orientation="vertical", label="Period")
        widget = radio._widget
        assert isinstance(widget, pmui.RadioButtonGroup)
        assert view.objects == [widget]
        assert (widget.value, widget.options, widget.label) == ("B", ["A", "B"], "Period")
        assert widget.orientation == "vertical"
        widget.value = "A"
        assert editor.graph.get_state(node).selected == "A"


class TestRangeSlider:
    def test_bounds_follow_config_and_ports(self, editor):
        node, rng = _place(editor, "Widgets/RangeSlider", default_start=10, default_end=30)
        widget = rng._widget
        state = editor.graph.get_state(node)
        assert (widget.start, widget.end, widget.value) == (10, 30, (10, 30))
        assert state.selected == (10, 30)
        assert all(type(v) is int for v in state.selected)

        start = _source(editor, "Test/bound", "bound", 15)
        assert editor.connect(start, "bound", node, "start") is True
        assert (widget.start, widget.value) == (15, (15, 30))

        widget.value = (18, 22)
        assert state.selected == (18, 22)
        editor.graph.get_state(start).bound = 20
        assert state.selected == (20, 22)

        editor.graph.get_state(start).bound = 0.5
        assert widget.format == pmui.FloatSlider.param.format.default
        assert state.selected == (20.0, 22.0)
        assert all(type(v) is float for v in state.selected)

    def test_whole_range_follows_the_bounds(self, editor):
        node, rng = _place(editor, "Widgets/RangeSlider")
        assert rng._widget.value == (0, 100)
        editor.graph.get_config_state(node).default_end = 500
        assert editor.graph.get_state(node).selected == (0, 500)

    def test_disabled_while_bounds_are_inverted(self, editor):
        node, rng = _place(editor, "Widgets/RangeSlider", default_start=5, default_end=1)
        assert rng._widget.disabled
        editor.graph.get_config_state(node).default_end = 9
        assert not rng._widget.disabled
        assert rng._widget.value == (5, 9)


class TestNumberInput:
    def test_number_type_and_bounds(self, editor):
        node, number = _place(editor, "Widgets/NumberInput", default_value=5)
        widget = number._widget
        state = editor.graph.get_state(node)
        assert (widget.value, widget.step, widget.format) == (5, 1, "0,0")
        assert type(state.selected) is int

        end = _source(editor, "Test/bound", "bound", 3)
        assert editor.connect(end, "bound", node, "end") is True
        assert (widget.end, state.selected) == (3, 3)

        editor.graph.get_config_state(node).step = 0.25
        widget.value = 2.5
        assert state.selected == 2.5
        assert widget.format == "0,0.0[000]"

    def test_cleared_input_outputs_none(self, editor):
        node, number = _place(editor, "Widgets/NumberInput")
        number._widget.value = None
        assert editor.graph.get_state(node).selected is None


class TestToggle:
    def test_default_value_and_style(self, editor):
        node, toggle = _place(editor, "Widgets/Toggle", default_value=True, label="Outliers")
        state = editor.graph.get_state(node)
        assert isinstance(toggle._widget, pmui.Switch)
        assert (toggle._widget.value, toggle._widget.label, state.checked) == (
            True,
            "Outliers",
            True,
        )

        editor.graph.get_config_state(node).style = "checkbox"
        assert isinstance(toggle._widget, pmui.Checkbox)
        assert toggle._widget.value is True
        toggle._widget.value = False
        assert state.checked is False


class TestTextInput:
    def test_sends_on_enter_by_default(self, editor):
        node, text = _place(editor, "Widgets/TextInput", placeholder="Search", default_value="x")
        widget = text._widget
        state = editor.graph.get_state(node)
        assert (widget.placeholder, widget.value, state.text) == ("Search", "x", "x")

        widget._process_events({"value_input": "wind"})
        assert state.text == "x"
        widget._process_events({"value": "wind"})
        assert state.text == "wind"

    def test_sends_while_typing(self, editor):
        node, text = _place(editor, "Widgets/TextInput", update_on="typing")
        text._widget._process_events({"value_input": "wi"})
        assert editor.graph.get_state(node).text == "wi"


class TestDateRange:
    def test_relative_default_period(self, editor):
        node, picker = _place(editor, "Widgets/DateRange")
        expected = (TODAY - dt.timedelta(days=29), TODAY)
        assert picker._widget.value == expected
        assert editor.graph.get_state(node).selected == expected

        editor.graph.get_config_state(node).default_period = "year_to_date"
        assert editor.graph.get_state(node).selected == (TODAY.replace(month=1, day=1), TODAY)

    def test_custom_period_within_bounds(self, editor):
        node, picker = _place(
            editor,
            "Widgets/DateRange",
            default_period="custom",
            custom_start="2024-01-10",
            custom_end="2024-03-01",
            default_start="2024-02-01",
            default_end="2024-12-31",
        )
        widget = picker._widget
        assert (widget.start, widget.end) == (dt.date(2024, 2, 1), dt.date(2024, 12, 31))
        assert editor.graph.get_state(node).selected == (dt.date(2024, 2, 1), dt.date(2024, 3, 1))

        widget.value = (dt.date(2024, 5, 1), dt.date(2024, 6, 1))
        assert editor.graph.get_state(node).selected == (dt.date(2024, 5, 1), dt.date(2024, 6, 1))

    def test_period_outside_wired_bounds_selects_them_all(self, editor):
        node, _ = _place(editor, "Widgets/DateRange", default_period="last_7_days")
        start = _source(editor, "Test/date", "day", dt.datetime(2020, 1, 1, 12))
        end = _source(editor, "Test/date", "day", dt.date(2020, 6, 30))
        assert editor.connect(start, "day", node, "start") is True
        assert editor.connect(end, "day", node, "end") is True
        assert editor.graph.get_state(node).selected == (dt.date(2020, 1, 1), dt.date(2020, 6, 30))

    def test_new_wired_bounds_keep_the_selection(self, editor):
        node, picker = _place(editor, "Widgets/DateRange", default_period="all")
        end = _source(editor, "Test/date", "day", dt.date(2020, 12, 31))
        start = _source(editor, "Test/date", "day", dt.date(2020, 1, 1))
        assert editor.connect(start, "day", node, "start") is True
        assert editor.connect(end, "day", node, "end") is True
        picker._widget.value = (dt.date(2020, 3, 1), dt.date(2020, 9, 1))

        editor.graph.get_state(end).day = dt.date(2020, 6, 30)
        assert editor.graph.get_state(node).selected == (dt.date(2020, 3, 1), dt.date(2020, 6, 30))

    def test_all_without_bounds_selects_nothing(self, editor):
        node, _ = _place(editor, "Widgets/DateRange", default_period="all")
        assert editor.graph.get_state(node).selected is None

    def test_dates_must_be_iso_strings(self, editor):
        node, _ = _place(editor, "Widgets/DateRange")
        config = editor.graph.get_config_state(node)
        with pytest.raises(ValueError):
            config.custom_start = "01/02/2024"


class TestKPI:
    def _html(self, editor, **config):
        _, kpi = _place(editor, "Widgets/KPI", **config)
        return kpi, lambda: kpi._pane.object

    def test_value_with_format_prefix_and_suffix(self, editor):
        kpi, html = self._html(editor, label="Revenue", prefix="$", suffix="k", format=",.1f")
        assert "&ndash;" in html()
        kpi.value = 12345.67
        assert ">Revenue<" in html()
        assert ">$12,345.7k<" in html()

    def test_default_format(self, editor):
        kpi, html = self._html(editor)
        kpi.value = 1234
        assert ">1,234<" in html()
        kpi.value = 0.5
        assert ">0.50<" in html()

    def test_delta_against_reference(self, editor):
        kpi, html = self._html(editor, caption="vs last year")
        kpi.param.update(value=110, reference=100)
        assert "fd-kpi-good" in html()
        assert "▲ 10.0%</span> vs last year" in html()

        kpi.param.update(increase_is_good=False, delta="absolute")
        assert "fd-kpi-bad" in html()
        assert "▲ 10<" in html()

        kpi.value = 90
        assert "▼ 10<" in html()

    def test_thresholds_color_the_value(self, editor):
        thresholds = [[50, "error"], [80, "#ff9900"], [100, "url(javascript:x)"]]
        kpi, html = self._html(editor, thresholds=thresholds)
        kpi.value = 40
        assert "color: var(--mui-palette-error-main, #d84a3a)" in html()
        kpi.value = 70
        assert "color: #ff9900" in html()
        kpi.value = 90
        assert "style=" not in html()

    def test_text_is_escaped(self, editor):
        kpi, html = self._html(editor, label="<b>x</b>", prefix="<img src=x>")
        kpi.value = 1
        assert "<b>" not in html()
        assert "<img" not in html()

    def test_wired_value(self, editor):
        node, kpi = _place(editor, "Widgets/KPI")
        src = _source(editor, "Test/bound", "bound", 42)
        assert editor.connect(src, "bound", node, "value") is True
        assert ">42<" in kpi._pane.object


class TestImage:
    @pytest.mark.parametrize(
        "url",
        ["https://example.com/a.png", "/assets/logo.svg", "logo.png", "data:image/png;base64,AA"],
    )
    def test_accepts_image_urls(self, url):
        assert safe_image_url(url) == url

    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "data:text/html;base64,AA", "vbscript:x", " "],
    )
    def test_rejects_other_urls(self, url):
        assert safe_image_url(url) is None

    def test_config_and_wired_url(self, editor):
        node, image = _place(
            editor, "Widgets/Image", default_url="logo.png", alt='A "logo"', fit="cover"
        )
        assert '<img src="logo.png" alt="A &quot;logo&quot;" style="object-fit: cover">' in (
            image._pane.object
        )
        src = _source(editor, "Test/text", "text", "javascript:alert(1)")
        assert editor.connect(src, "text", node, "url") is True
        assert "<img" not in image._pane.object
        assert "No image" in image._pane.object


CONFIGS = {
    "Widgets/RadioGroup": {"default_options": ["A", "B"], "style": "buttons"},
    "Widgets/RangeSlider": {"default_start": 1, "default_end": 9, "step": 0.5, "color": "danger"},
    "Widgets/NumberInput": {"default_value": 3, "number_type": "float", "format": "0.00"},
    "Widgets/DateRange": {
        "default_period": "custom",
        "custom_start": "2024-01-01",
        "custom_end": "2024-01-31",
        "default_end": "2024-12-31",
    },
    "Widgets/Toggle": {"default_value": True, "style": "checkbox"},
    "Widgets/TextInput": {"placeholder": "Search", "update_on": "typing"},
    "Widgets/KPI": {"label": "Sales", "thresholds": [[10, "error"]], "delta": "absolute"},
    "Widgets/Image": {"default_url": "https://example.com/a.png", "fit": "cover"},
}


def test_config_round_trips_through_json(editor):
    for component_id, config in CONFIGS.items():
        editor.add_component(component_id, config=config)
    model = json.loads(json.dumps(editor.to_model(title="Widgets").to_dict()))

    restored = FlowDash(SOURCES, notifications=False)
    try:
        restored.load_model(DashboardModel.from_dict(model))
        for item, (component_id, config) in zip(
            restored._tile_items, CONFIGS.items(), strict=True
        ):
            state = restored.graph.get_config_state(item["instance_id"])
            assert item["component_id"] == component_id
            assert {k: getattr(state, k) for k in config} == config
    finally:
        restored.dispose()


def test_config_round_trips_through_yaml(editor):
    agent = pytest.importorskip("panel_flowdash.agent")
    for component_id, config in CONFIGS.items():
        editor.add_component(component_id, config=config)
    exported = editor.export_dashboard()
    text = agent.dashboard_to_yaml(exported)
    issues = agent.validate_dashboard(text, editor.component_specs)
    assert [str(i) for i in issues if i.severity == "error"] == []
    editor.import_dashboard(text)
    assert editor.export_dashboard() == exported
