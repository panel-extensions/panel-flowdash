"""Tests for design-time component configuration extraction and wiring."""

import param
from panel.viewable import Viewer

from panel_flowdash import (
    DataflowGraph,
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


class TestConfigSchemaExtraction:
    def test_param_class_schema(self):
        class Cfg(param.Parameterized):
            title = param.String(default="hi")
            count = param.Integer(default=3, bounds=(0, 10))

        @register(component=True, config_schema=Cfg)
        def app(config, instance_config):
            pass

        spec = build_component_spec(make_entry(app))
        names = [f.name for f in spec.config]
        assert names == ["title", "count"]
        assert spec.config[0].default == "hi"
        assert spec.config[1].type == "Integer"
        assert spec.config_state_class is not None
        assert spec.config_state_class().count == 3

    def test_dict_schema_with_enum(self):
        schema = {
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["a", "b"],
                    "default": "a",
                    "title": "Mode",
                }
            }
        }

        @register(component=True, config_schema=schema)
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert [f.name for f in spec.config] == ["mode"]
        assert spec.config[0].label == "Mode"
        assert spec.config_state_class().mode == "a"

    def test_no_config(self):
        @register(component=True, provides=["x"])
        def app(config):
            pass

        spec = build_component_spec(make_entry(app))
        assert spec.config == []
        assert spec.config_state_class is None


class TestViewerConfigNames:
    def test_config_params_excluded_from_inputs(self):
        @register(component=True, config=["show_labels"])
        class app(Viewer):
            start = param.Integer(default=2000)
            show_labels = param.Boolean(default=True)

            @param.output(param.String)
            def expr(self):
                return ""

        spec = build_component_spec(make_entry(app))
        input_names = [p.name for p in spec.inputs]
        config_names = [f.name for f in spec.config]
        assert "show_labels" not in input_names
        assert "start" in input_names
        assert config_names == ["show_labels"]

    def test_config_state_carries_default(self):
        @register(component=True, config=["show_labels"])
        class app(Viewer):
            show_labels = param.Boolean(default=True)

        spec = build_component_spec(make_entry(app))
        assert spec.config_state_class().show_labels is True


class TestConfigEditor:
    def test_editor_passed_through(self):
        def editor(data, schema, *, id, type, on_patch):
            return None

        @register(component=True, config=["x"], config_editor=editor)
        class app(Viewer):
            x = param.Integer(default=1)

        spec = build_component_spec(make_entry(app))
        assert spec.config_editor is editor


class TestConfigStateInGraph:
    def test_graph_creates_config_state(self):
        class Cfg(param.Parameterized):
            title = param.String(default="hi")

        @register(component=True, config_schema=Cfg)
        def app(config, instance_config):
            pass

        spec = build_component_spec(make_entry(app, component_id="test/cfg"))
        graph = DataflowGraph({"test/cfg": spec})
        graph.add_node("n1", "test/cfg")

        config_state = graph.get_config_state("n1")
        assert config_state is not None
        assert config_state.title == "hi"

    def test_config_state_removed_with_node(self):
        class Cfg(param.Parameterized):
            title = param.String(default="hi")

        @register(component=True, config_schema=Cfg)
        def app(config, instance_config):
            pass

        spec = build_component_spec(make_entry(app, component_id="test/cfg"))
        graph = DataflowGraph({"test/cfg": spec})
        graph.add_node("n1", "test/cfg")
        graph.remove_node("n1")
        assert graph.get_config_state("n1") is None

    def test_no_config_state_when_no_config(self):
        @register(component=True, provides=["x"])
        def app(config):
            pass

        spec = build_component_spec(make_entry(app, component_id="test/plain"))
        graph = DataflowGraph({"test/plain": spec})
        graph.add_node("n1", "test/plain")
        assert graph.get_config_state("n1") is None
