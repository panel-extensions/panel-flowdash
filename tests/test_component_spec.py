"""Tests for ComponentSpec port extraction from different component styles."""

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
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

    async def test_provides_as_strings(self):
        @register(component=True, provides=["company", "date_range"])
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert len(spec.outputs) == 2
        assert spec.outputs[0].name == "company"
        assert spec.outputs[1].name == "date_range"
        assert spec.outputs[0].type is None
        assert spec.outputs[0].label is None

    async def test_requires_as_strings(self):
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

    async def test_provides_as_dicts(self):
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

    async def test_requires_as_dicts(self):
        @register(
            component=True,
            requires=[
                {
                    "key": "company",
                    "type": "str",
                    "label": "Company Name",
                    "required": True,
                    "blocking": True,
                },
                {
                    "key": "date",
                    "type": "date",
                    "label": "Filter Date",
                    "required": False,
                    "blocking": False,
                    "fallback": "2024-01-01",
                },
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

    async def test_mixed_provides_and_requires(self):
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

    async def test_param_output_extraction(self):
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

    async def test_param_inputs_extraction(self):
        class MyComponent(Viewer):
            revenue = param.Number(default=0)
            costs = param.Number(default=0)

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "revenue" in input_names
        assert "costs" in input_names

    async def test_base_params_excluded(self):
        class MyComponent(Viewer):
            custom_param = param.String(default="")

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "custom_param" in input_names
        assert "name" not in input_names

    async def test_private_params_excluded(self):
        class MyComponent(Viewer):
            visible_param = param.String(default="")
            _hidden = param.String(default="internal")

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        input_names = [i.name for i in spec.inputs]
        assert "visible_param" in input_names
        assert "_hidden" not in input_names


class TestParameterOutputs:
    """Viewer Parameters named in ``provides`` are ports, with no output method."""

    async def test_output_is_typed_after_its_parameter(self):
        @register(component=True, provides=["picks"])
        class MyComponent(Viewer):
            picks = param.List(default=[])

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        assert [(o.name, o.type) for o in spec.outputs] == [("picks", "List")]

    async def test_declared_type_and_label_win(self):
        @register(component=True, provides=[{"key": "picks", "type": "Items", "label": "Picks"}])
        class MyComponent(Viewer):
            picks = param.List(default=[])

            def __panel__(self):
                return "test"

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert (output.type, output.label) == ("Items", "Picks")

    async def test_base_parameter_leaves_the_port_untyped(self):
        @register(component=True, provides=["picked"])
        class MyComponent(Viewer):
            picked = param.Parameter()

            def __panel__(self):
                return "test"

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert output.type is None

    async def test_output_parameter_is_not_also_an_input(self):
        @register(component=True, provides=["picks"])
        class MyComponent(Viewer):
            picks = param.List(default=[])
            threshold = param.Number(default=0)

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        assert [i.name for i in spec.inputs] == ["threshold"]

    async def test_explicit_requires_are_left_alone(self):
        @register(component=True, provides=["picks"], requires=["picks"])
        class MyComponent(Viewer):
            picks = param.List(default=[])

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        assert [i.name for i in spec.inputs] == ["picks"]

    async def test_name_that_is_no_parameter_stays_untyped(self):
        @register(component=True, provides=["custom_output"])
        class MyComponent(Viewer):
            def __panel__(self):
                return "test"

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert (output.name, output.type) == ("custom_output", None)


def output_meta(**declared):
    return {"flowdash": {"output": True, **declared}}


class TestMetadataOutputs:
    """A Parameter declares itself an output through ``metadata["flowdash"]``."""

    async def test_output_is_typed_after_its_parameter(self):
        @register(component=True)
        class MyComponent(Viewer):
            picks = param.List(default=[], metadata=output_meta())

            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(MyComponent))
        assert [(o.name, o.type, o.label) for o in spec.outputs] == [("picks", "List", None)]

    async def test_label_is_carried_to_the_port(self):
        class MyComponent(Viewer):
            region = param.Selector(objects=["EU", "US"], metadata=output_meta(label="Region"))

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert (output.name, output.type, output.label) == ("region", "Selector", "Region")

    async def test_base_parameter_leaves_the_port_untyped(self):
        class MyComponent(Viewer):
            picked = param.Parameter(metadata=output_meta())

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert output.type is None

    async def test_output_parameter_is_not_also_an_input(self):
        class MyComponent(Viewer):
            picks = param.List(default=[], metadata=output_meta())
            threshold = param.Number(default=0)

        spec = build_component_spec(make_entry(MyComponent))
        assert [i.name for i in spec.inputs] == ["threshold"]

    async def test_inherited_parameter_is_an_output(self):
        class Base(Viewer):
            company = param.String(default="ACME", metadata=output_meta())

        class Child(Base):
            threshold = param.Number(default=0)

        spec = build_component_spec(make_entry(Child))
        assert [(o.name, o.type) for o in spec.outputs] == [("company", "String")]
        assert [i.name for i in spec.inputs] == ["threshold"]

    async def test_subclass_can_turn_an_inherited_output_off(self):
        """Param inherits unset slots, so a redeclared Parameter keeps the metadata."""

        class Base(Viewer):
            company = param.String(default="ACME", metadata=output_meta())

        class Inherits(Base):
            company = param.String(default="Other")

        class Child(Base):
            company = param.String(default="ACME", metadata={"flowdash": {"output": False}})

        assert [o.name for o in build_component_spec(make_entry(Inherits)).outputs] == ["company"]

        spec = build_component_spec(make_entry(Child))
        assert spec.outputs == []
        assert [i.name for i in spec.inputs] == ["company"]

    async def test_parameters_added_by_a_class_factory(self):
        def make_selector(name):
            return type(
                name,
                (Viewer,),
                {
                    "company": param.String(default="ACME", metadata=output_meta()),
                    "__panel__": lambda self: "test",
                },
            )

        spec = build_component_spec(make_entry(make_selector("Selector")))
        assert [(o.name, o.type) for o in spec.outputs] == [("company", "String")]

    async def test_viewer_is_not_instantiated(self):
        class MyComponent(Viewer):
            picks = param.List(default=[], metadata=output_meta())

            def __init__(self, **params):
                raise AssertionError("introspection must not instantiate the Viewer")

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert output.name == "picks"

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"other": {"output": True}},
            {"flowdash": "output"},
            {"flowdash": None},
            {"flowdash": {}},
            {"flowdash": {"output": False}},
            {"flowdash": {"required": True}},
        ],
        ids=[
            "none",
            "empty",
            "other_namespace",
            "non_dict_namespace",
            "none_namespace",
            "empty_namespace",
            "output_false",
            "unrelated_key",
        ],
    )
    async def test_other_metadata_does_not_declare_an_output(self, metadata):
        class MyComponent(Viewer):
            picks = param.List(default=[], metadata=metadata)

        spec = build_component_spec(make_entry(MyComponent))
        assert spec.outputs == []
        assert [i.name for i in spec.inputs] == ["picks"]

    async def test_other_keys_are_tolerated_beside_output(self):
        class MyComponent(Viewer):
            picks = param.List(
                default=[],
                metadata={"flowdash": {"output": True, "required": "yes"}, "other": 1},
            )

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert output.name == "picks"

    async def test_combines_with_param_output_methods(self):
        class MyComponent(Viewer):
            pick = param.String(default="a", metadata=output_meta())

            @param.output(param.Integer)
            @param.depends("pick")
            def size(self):
                return len(self.pick)

        spec = build_component_spec(make_entry(MyComponent))
        assert [(o.name, o.type) for o in spec.outputs] == [
            ("size", "Integer"),
            ("pick", "String"),
        ]
        assert spec.inputs == []

    async def test_provides_replaces_metadata_outputs(self):
        """``provides`` replaces every introspected output, metadata ones included."""

        @register(component=True, provides=["extra"])
        class MyComponent(Viewer):
            pick = param.String(default="a", metadata=output_meta())
            extra = param.Integer(default=1)

        spec = build_component_spec(make_entry(MyComponent))
        assert [o.name for o in spec.outputs] == ["extra"]
        # The declared output stays out of the inputs, so it is not reachable at all.
        assert [i.name for i in spec.inputs] == []

    async def test_provides_can_relist_a_metadata_output(self):
        @register(component=True, provides=["pick", "size"])
        class MyComponent(Viewer):
            pick = param.String(default="a", metadata=output_meta())

            @param.output(param.Integer)
            @param.depends("pick")
            def size(self):
                return len(self.pick)

        spec = build_component_spec(make_entry(MyComponent))
        assert [(o.name, o.type) for o in spec.outputs] == [("pick", "String"), ("size", None)]

    async def test_provides_entry_wins_over_the_metadata_label(self):
        @register(component=True, provides=[{"key": "pick", "label": "Choice"}])
        class MyComponent(Viewer):
            pick = param.String(default="a", metadata=output_meta(label="Pick"))

        (output,) = build_component_spec(make_entry(MyComponent)).outputs
        assert (output.type, output.label) == ("String", "Choice")

    @pytest.mark.parametrize("name", ["width", "height", "visible", "loading"])
    async def test_viewable_parameter_name_is_rejected(self, name):
        class MyComponent(Viewer):
            locals()[name] = param.Parameter(metadata=output_meta())

        with pytest.raises(ValueError, match=rf"'{name}'.*MyComponent"):
            build_component_spec(make_entry(MyComponent))

    async def test_underscore_name_is_rejected(self):
        class MyComponent(Viewer):
            _hidden = param.Parameter(metadata=output_meta())

        with pytest.raises(ValueError, match="'_hidden'"):
            build_component_spec(make_entry(MyComponent))

    async def test_base_name_without_the_declaration_is_not_an_error(self):
        class MyComponent(Viewer):
            width = param.Integer(default=10, metadata={"flowdash": {"label": "W"}})

        assert build_component_spec(make_entry(MyComponent)).outputs == []


class TestViewerWithDecoratorOverride:
    """Viewer subclass where decorator metadata overrides introspected ports."""

    async def test_decorator_provides_overrides_param_output(self):
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

    async def test_decorator_requires_overrides_params(self):
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
    async def test_no_provides_no_requires(self):
        @register(component=True)
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.outputs == []
        assert spec.inputs == []

    async def test_viewer_no_custom_params_no_outputs(self):
        class Bare(Viewer):
            def __panel__(self):
                return "test"

        spec = build_component_spec(make_entry(Bare))
        assert spec.outputs == []
        assert spec.inputs == []


class TestSpecMetadata:
    async def test_title_from_decorator(self):
        @register(component=True, title="My Widget")
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.title == "My Widget"

    async def test_tags_and_icon(self):
        @register(component=True, tags=["analytics", "kpi"], icon="bar_chart")
        def app():
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.tags == ["analytics", "kpi"]
        assert spec.icon == "bar_chart"
