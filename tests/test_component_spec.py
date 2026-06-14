"""Tests for ComponentSpec port extraction from different component styles."""

import param
from panel.viewable import Viewer

from panel_flowdash import (
    ComponentSpec,
    InputPort,
    OutputPort,
    PanelAppMetadata,
    RegistryEntry,
    build_component_spec,
    register,
)


def make_entry(app, *, component_id="test/comp"):
    metadata = PanelAppMetadata.from_app(app)
    return RegistryEntry(
        app_id=component_id,
        section="test",
        name="comp",
        page_path="/test/comp",
        module_name="test.comp",
        app=app,
        metadata=metadata,
    )


class TestDecoratorStringPorts:
    """Components that use simple string lists for provides/requires."""

    def test_provides_as_strings(self):
        @register(component=True, provides=["company", "date_range"])
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.outputs) == 2
        assert spec.outputs[0].name == "company"
        assert spec.outputs[1].name == "date_range"
        assert spec.outputs[0].type is None
        assert spec.outputs[0].label is None

    def test_requires_as_strings(self):
        @register(component=True, requires=["company", "date_start"])
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.inputs) == 2
        assert spec.inputs[0].name == "company"
        assert spec.inputs[1].name == "date_start"
        assert spec.inputs[0].required is True
        assert spec.inputs[0].blocking is True


class TestDecoratorDictPorts:
    """Components that use rich dict format for provides/requires."""

    def test_provides_as_dicts(self):
        @register(
            component=True,
            provides=[
                {"key": "revenue", "type": "float", "label": "Total Revenue"},
                {"key": "costs", "type": "float", "label": "Total Costs"},
            ],
        )
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.outputs) == 2
        assert spec.outputs[0].name == "revenue"
        assert spec.outputs[0].type == "float"
        assert spec.outputs[0].label == "Total Revenue"

    def test_requires_as_dicts(self):
        @register(
            component=True,
            requires=[
                {"key": "company", "type": "str", "label": "Company Name", "required": True, "blocking": True},
                {"key": "date", "type": "date", "label": "Filter Date", "required": False, "blocking": False, "fallback": "2024-01-01"},
            ],
        )
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.inputs) == 2
        assert spec.inputs[0].name == "company"
        assert spec.inputs[0].type == "str"
        assert spec.inputs[0].label == "Company Name"
        assert spec.inputs[0].required is True
        assert spec.inputs[0].blocking is True

        assert spec.inputs[1].name == "date"
        assert spec.inputs[1].required is False
        assert spec.inputs[1].blocking is False
        assert spec.inputs[1].default == "2024-01-01"

    def test_mixed_provides_and_requires(self):
        @register(
            component=True,
            provides=["profit"],
            requires=[{"key": "revenue", "blocking": True}, {"key": "costs", "blocking": True}],
        )
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.outputs) == 1
        assert spec.outputs[0].name == "profit"
        assert len(spec.inputs) == 2


class TestViewerSubclass:
    """Components defined as Viewer subclasses with param.output."""

    def test_param_output_extraction(self):
        class MyComponent(Viewer):
            company = param.String(default="")

            @param.output(param.String)
            def selected_company(self):
                return self.company

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        output_names = [o.name for o in spec.outputs]
        assert "selected_company" in output_names

    def test_param_inputs_extraction(self):
        class MyComponent(Viewer):
            revenue = param.Number(default=0)
            costs = param.Number(default=0)

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "revenue" in input_names
        assert "costs" in input_names

    def test_base_params_excluded(self):
        class MyComponent(Viewer):
            custom_param = param.String(default="")

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "custom_param" in input_names
        assert "name" not in input_names

    def test_private_params_excluded(self):
        class MyComponent(Viewer):
            visible_param = param.String(default="")
            _hidden = param.String(default="internal")

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "visible_param" in input_names
        assert "_hidden" not in input_names


class TestViewerWithDecoratorOverride:
    """Viewer subclass where decorator metadata overrides introspected ports."""

    def test_decorator_provides_overrides_param_output(self):
        @register(component=True, provides=["custom_output"])
        class MyComponent(Viewer):
            val = param.String()

            @param.output(param.String)
            def introspected_output(self):
                return self.val

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        output_names = [o.name for o in spec.outputs]
        assert output_names == ["custom_output"]

    def test_decorator_requires_overrides_params(self):
        @register(component=True, requires=[{"key": "company", "type": "str"}])
        class MyComponent(Viewer):
            company = param.String()
            ignored_param = param.Number()

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert input_names == ["company"]


class TestEmptyPorts:
    def test_no_provides_no_requires(self):
        @register(component=True)
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.outputs == []
        assert spec.inputs == []

    def test_viewer_no_custom_params_no_outputs(self):
        class Bare(Viewer):
            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(Bare))
        assert spec.outputs == []
        assert spec.inputs == []


class TestSpecMetadata:
    def test_title_from_decorator(self):
        @register(component=True, title="My Widget")
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.title == "My Widget"

    def test_tags_and_icon(self):
        @register(component=True, tags=["analytics", "kpi"], icon="bar_chart")
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.tags == ["analytics", "kpi"]
        assert spec.icon == "bar_chart"
