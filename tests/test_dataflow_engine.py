"""Tests for DataflowGraph validation: type checking, cycle detection, single-source."""

import param
from panel.viewable import Viewer

from panel_flowdash import (
    ComponentSpec,
    DataflowGraph,
    InputPort,
    OutputPort,
    PanelAppMetadata,
    RegistryEntry,
    build_component_spec,
    build_node_state_class,
    register,
)


def make_spec(component_id, inputs=None, outputs=None):
    return ComponentSpec(
        component_id=component_id,
        title=component_id,
        description=None,
        icon=None,
        tags=[],
        outputs=outputs or [],
        inputs=inputs or [],
        default_size=None,
    )


def _entry(app, component_id):
    metadata = PanelAppMetadata.from_app(app)
    return RegistryEntry(
        app_id=component_id,
        section="test",
        name=component_id.split("/")[-1],
        page_path=f"/{component_id}",
        module_name=f"test.{component_id}",
        app=app,
        metadata=metadata,
    )


class TestBasicWiring:
    def setup_method(self):
        specs = {
            "producer": make_spec(
                "producer",
                outputs=[OutputPort(name="value", type="str")],
            ),
            "consumer": make_spec(
                "consumer",
                inputs=[InputPort(name="value", type="str")],
            ),
        }
        self.graph = DataflowGraph(specs)
        self.graph.add_node("p1", "producer")
        self.graph.add_node("c1", "consumer")

    def test_valid_edge(self):
        result = self.graph.add_edge("p1", "value", "c1", "value")
        assert result is True

    def test_propagation(self):
        self.graph.add_edge("p1", "value", "c1", "value")
        source = self.graph.get_state("p1")
        target = self.graph.get_state("c1")
        source.value = "hello"
        assert target.value == "hello"

    def test_nonexistent_source_node(self):
        result = self.graph.add_edge("missing", "value", "c1", "value")
        assert isinstance(result, str)
        assert "not found" in result

    def test_nonexistent_target_node(self):
        result = self.graph.add_edge("p1", "value", "missing", "value")
        assert isinstance(result, str)
        assert "not found" in result

    def test_nonexistent_source_port(self):
        result = self.graph.add_edge("p1", "nope", "c1", "value")
        assert isinstance(result, str)
        assert "does not exist" in result

    def test_nonexistent_target_port(self):
        result = self.graph.add_edge("p1", "value", "c1", "nope")
        assert isinstance(result, str)
        assert "does not exist" in result


class TestSingleSourcePerInput:
    def test_rejects_second_edge_to_same_input(self):
        specs = {
            "producer": make_spec("producer", outputs=[OutputPort(name="value", type="str")]),
            "producer2": make_spec("producer2", outputs=[OutputPort(name="value", type="str")]),
            "consumer": make_spec("consumer", inputs=[InputPort(name="value", type="str")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("p1", "producer")
        graph.add_node("p2", "producer2")
        graph.add_node("c1", "consumer")

        result1 = graph.add_edge("p1", "value", "c1", "value")
        assert result1 is True

        result2 = graph.add_edge("p2", "value", "c1", "value")
        assert isinstance(result2, str)
        assert "already has a connection" in result2

    def test_allows_after_disconnect(self):
        specs = {
            "producer": make_spec("producer", outputs=[OutputPort(name="value", type="str")]),
            "producer2": make_spec("producer2", outputs=[OutputPort(name="value", type="str")]),
            "consumer": make_spec("consumer", inputs=[InputPort(name="value", type="str")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("p1", "producer")
        graph.add_node("p2", "producer2")
        graph.add_node("c1", "consumer")

        graph.add_edge("p1", "value", "c1", "value")
        graph.remove_edge("p1", "value", "c1", "value")

        result = graph.add_edge("p2", "value", "c1", "value")
        assert result is True


class TestCycleDetection:
    def test_self_loop(self):
        specs = {
            "node": make_spec(
                "node",
                inputs=[InputPort(name="x")],
                outputs=[OutputPort(name="x")],
            ),
        }
        graph = DataflowGraph(specs)
        graph.add_node("n1", "node")
        result = graph.add_edge("n1", "x", "n1", "x")
        assert isinstance(result, str)
        assert "cycle" in result.lower()

    def test_direct_cycle(self):
        specs = {
            "node": make_spec(
                "node",
                inputs=[InputPort(name="in_val")],
                outputs=[OutputPort(name="out_val")],
            ),
        }
        graph = DataflowGraph(specs)
        graph.add_node("a", "node")
        graph.add_node("b", "node")

        result1 = graph.add_edge("a", "out_val", "b", "in_val")
        assert result1 is True

        result2 = graph.add_edge("b", "out_val", "a", "in_val")
        assert isinstance(result2, str)
        assert "cycle" in result2.lower()

    def test_indirect_cycle(self):
        specs = {
            "node": make_spec(
                "node",
                inputs=[InputPort(name="in_val")],
                outputs=[OutputPort(name="out_val")],
            ),
        }
        graph = DataflowGraph(specs)
        graph.add_node("a", "node")
        graph.add_node("b", "node")
        graph.add_node("c", "node")

        assert graph.add_edge("a", "out_val", "b", "in_val") is True
        assert graph.add_edge("b", "out_val", "c", "in_val") is True

        result = graph.add_edge("c", "out_val", "a", "in_val")
        assert isinstance(result, str)
        assert "cycle" in result.lower()

    def test_non_cycle_diamond(self):
        """Diamond shape (A->B, A->C, B->D, C->D) is not a cycle."""
        specs = {
            "node": make_spec(
                "node",
                inputs=[InputPort(name="in1"), InputPort(name="in2")],
                outputs=[OutputPort(name="out1"), OutputPort(name="out2")],
            ),
        }
        graph = DataflowGraph(specs)
        graph.add_node("a", "node")
        graph.add_node("b", "node")
        graph.add_node("c", "node")
        graph.add_node("d", "node")

        assert graph.add_edge("a", "out1", "b", "in1") is True
        assert graph.add_edge("a", "out2", "c", "in1") is True
        assert graph.add_edge("b", "out1", "d", "in1") is True
        assert graph.add_edge("c", "out1", "d", "in2") is True


class TestTypeCompatibility:
    def test_matching_types(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type="str")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type="str")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        assert graph.add_edge("s", "val", "t", "val") is True

    def test_mismatched_types(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type="int")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type="str")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        result = graph.add_edge("s", "val", "t", "val")
        assert isinstance(result, str)
        assert "Type mismatch" in result

    def test_untyped_source_allows_connection(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type=None)]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type="str")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        assert graph.add_edge("s", "val", "t", "val") is True

    def test_untyped_target_allows_connection(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type="int")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type=None)]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        assert graph.add_edge("s", "val", "t", "val") is True

    def test_both_untyped_allows_connection(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type=None)]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type=None)]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        assert graph.add_edge("s", "val", "t", "val") is True

    def test_case_insensitive_match(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val", type="String")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", type="string")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        assert graph.add_edge("s", "val", "t", "val") is True


class TestNodeStateCreation:
    def test_creates_params_for_inputs_and_outputs(self):
        spec = make_spec(
            "test",
            inputs=[InputPort(name="x", default=42)],
            outputs=[OutputPort(name="y")],
        )
        cls = build_node_state_class(spec)
        instance = cls()
        assert hasattr(instance, "x")
        assert hasattr(instance, "y")
        assert instance.x == 42
        assert instance.y is None

    def test_shared_port_name_uses_single_param(self):
        spec = make_spec(
            "passthrough",
            inputs=[InputPort(name="val")],
            outputs=[OutputPort(name="val")],
        )
        cls = build_node_state_class(spec)
        instance = cls()
        instance.val = "test"
        assert instance.val == "test"

    def test_allows_refs(self):
        spec = make_spec(
            "test",
            inputs=[InputPort(name="x")],
            outputs=[OutputPort(name="y")],
        )
        cls = build_node_state_class(spec)
        instance = cls()
        assert instance.param.x.allow_refs is True


class TestEdgeRemoval:
    def test_remove_resets_to_default(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", default="fallback")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        graph.add_edge("s", "val", "t", "val")

        graph.get_state("s").val = "connected"
        assert graph.get_state("t").val == "connected"

        graph.remove_edge("s", "val", "t", "val")
        assert graph.get_state("t").val == "fallback"

    def test_remove_node_clears_edges(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        graph.add_edge("s", "val", "t", "val")

        graph.remove_node("s")
        assert len(graph.edges) == 0
        assert "s" not in graph.node_ids


class TestViewerComponents:
    """End-to-end tests using real Viewer subclasses through spec extraction and wiring."""

    def test_viewer_output_propagates_to_viewer_input(self):
        class Producer(Viewer):
            company = param.String(default="")

            @param.output(param.String)
            def selected_company(self):
                return self.company

            def __panel__(self):
                return "producer"

        class Consumer(Viewer):
            company = param.String(default="")

            def __panel__(self):
                return f"Company: {self.company}"

        producer_spec = build_component_spec(_entry(Producer, "test/producer"))
        consumer_spec = build_component_spec(_entry(Consumer, "test/consumer"))

        specs = {
            "test/producer": producer_spec,
            "test/consumer": consumer_spec,
        }
        graph = DataflowGraph(specs)
        graph.add_node("p1", "test/producer")
        graph.add_node("c1", "test/consumer")

        result = graph.add_edge("p1", "selected_company", "c1", "company")
        assert result is True

        graph.get_state("p1").selected_company = "Acme Corp"
        assert graph.get_state("c1").company == "Acme Corp"

    def test_viewer_chain_three_nodes(self):
        """Multi-hop propagation: each hop fires when the source port is set."""

        class Selector(Viewer):
            value = param.String(default="")

            @param.output(param.String)
            def selected(self):
                return self.value

            def __panel__(self):
                return "selector"

        class Transformer(Viewer):
            input_val = param.String(default="")

            @param.output(param.String)
            def output_val(self):
                return self.input_val.upper() if self.input_val else ""

            def __panel__(self):
                return "transformer"

        class Display(Viewer):
            text = param.String(default="")

            def __panel__(self):
                return self.text

        selector_spec = build_component_spec(_entry(Selector, "test/selector"))
        transformer_spec = build_component_spec(_entry(Transformer, "test/transformer"))
        display_spec = build_component_spec(_entry(Display, "test/display"))

        specs = {
            "test/selector": selector_spec,
            "test/transformer": transformer_spec,
            "test/display": display_spec,
        }
        graph = DataflowGraph(specs)
        graph.add_node("s1", "test/selector")
        graph.add_node("t1", "test/transformer")
        graph.add_node("d1", "test/display")

        assert graph.add_edge("s1", "selected", "t1", "input_val") is True
        assert graph.add_edge("t1", "output_val", "d1", "text") is True

        # First hop: selector output propagates to transformer input
        graph.get_state("s1").selected = "hello"
        assert graph.get_state("t1").input_val == "hello"

        # Second hop: transformer writes its output, which propagates to display
        graph.get_state("t1").output_val = "HELLO"
        assert graph.get_state("d1").text == "HELLO"

    def test_viewer_type_mismatch_rejected(self):
        @register(
            component=True,
            provides=[{"key": "count", "type": "int"}],
        )
        class Counter(Viewer):
            count = param.Integer(default=0)

            def __panel__(self):
                return str(self.count)

        @register(
            component=True,
            requires=[{"key": "label", "type": "str"}],
        )
        class LabelDisplay(Viewer):
            label = param.String(default="")

            def __panel__(self):
                return self.label

        counter_spec = build_component_spec(_entry(Counter, "test/counter"))
        display_spec = build_component_spec(_entry(LabelDisplay, "test/display"))

        specs = {"test/counter": counter_spec, "test/display": display_spec}
        graph = DataflowGraph(specs)
        graph.add_node("c1", "test/counter")
        graph.add_node("d1", "test/display")

        result = graph.add_edge("c1", "count", "d1", "label")
        assert isinstance(result, str)
        assert "Type mismatch" in result

    def test_viewer_cycle_rejected(self):
        class Node(Viewer):
            input_val = param.String(default="")

            @param.output(param.String)
            def output_val(self):
                return self.input_val

            def __panel__(self):
                return "node"

        spec = build_component_spec(_entry(Node, "test/node"))
        specs = {"test/node": spec}
        graph = DataflowGraph(specs)
        graph.add_node("a", "test/node")
        graph.add_node("b", "test/node")

        assert graph.add_edge("a", "output_val", "b", "input_val") is True
        result = graph.add_edge("b", "output_val", "a", "input_val")
        assert isinstance(result, str)
        assert "cycle" in result.lower()

    def test_viewer_single_source_rejected(self):
        class Source(Viewer):
            @param.output(param.String)
            def value(self):
                return ""

            def __panel__(self):
                return "source"

        class Sink(Viewer):
            value = param.String(default="")

            def __panel__(self):
                return "sink"

        source_spec = build_component_spec(_entry(Source, "test/source"))
        sink_spec = build_component_spec(_entry(Sink, "test/sink"))

        specs = {"test/source": source_spec, "test/sink": sink_spec}
        graph = DataflowGraph(specs)
        graph.add_node("s1", "test/source")
        graph.add_node("s2", "test/source")
        graph.add_node("t1", "test/sink")

        assert graph.add_edge("s1", "value", "t1", "value") is True
        result = graph.add_edge("s2", "value", "t1", "value")
        assert isinstance(result, str)
        assert "already has a connection" in result

    def test_decorator_function_component_wiring(self):
        """Decorator-based function components wired through the graph."""

        @register(component=True, provides=["company"])
        def selector(config):
            return "selector widget"

        @register(component=True, requires=["company"])
        def chart(config):
            return "chart"

        selector_spec = build_component_spec(_entry(selector, "test/selector"))
        chart_spec = build_component_spec(_entry(chart, "test/chart"))

        specs = {"test/selector": selector_spec, "test/chart": chart_spec}
        graph = DataflowGraph(specs)
        graph.add_node("sel1", "test/selector")
        graph.add_node("ch1", "test/chart")

        assert graph.add_edge("sel1", "company", "ch1", "company") is True

        graph.get_state("sel1").company = "Weiss AG"
        assert graph.get_state("ch1").company == "Weiss AG"


class TestRuntimeValidation:
    """Tests that runtime type errors during propagation are caught and reported."""

    def test_on_error_called_on_assignment_failure(self):
        """When target param rejects a value, on_error fires instead of crashing."""

        class TypedTarget(param.Parameterized):
            count = param.Integer(default=0, allow_None=True)

        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="count")]),
        }

        errors = []

        def capture_error(src_id, src_port, tgt_id, tgt_port, exc):
            errors.append((src_id, src_port, tgt_id, tgt_port, exc))

        graph = DataflowGraph(specs, on_error=capture_error)
        graph.add_node("s", "src")

        typed_state = TypedTarget(name="t")
        graph._nodes["t"] = typed_state
        graph._node_specs["t"] = specs["tgt"]

        graph.add_edge("s", "val", "t", "count")

        graph.get_state("s").val = "hello"
        assert len(errors) == 1
        assert errors[0][0] == "s"
        assert errors[0][1] == "val"
        assert errors[0][2] == "t"
        assert errors[0][3] == "count"
        assert isinstance(errors[0][4], Exception)

    def test_on_error_with_typed_node_state(self):
        """Use a custom NodeState with a typed param to trigger validation."""

        class TypedState(param.Parameterized):
            count = param.Integer(default=0, allow_None=True)

        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="count")]),
        }

        errors = []
        graph = DataflowGraph(specs, on_error=lambda *args: errors.append(args))
        graph.add_node("s", "src")

        typed_state = TypedState(name="t")
        graph._nodes["t"] = typed_state
        graph._node_specs["t"] = specs["tgt"]

        graph.add_edge("s", "val", "t", "count")

        graph.get_state("s").val = "not_an_int"
        assert len(errors) == 1
        assert "count" in errors[0][3]

    def test_no_error_on_valid_assignment(self):
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val")]),
        }
        errors = []
        graph = DataflowGraph(specs, on_error=lambda *args: errors.append(args))
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        graph.add_edge("s", "val", "t", "val")

        graph.get_state("s").val = "perfectly fine"
        assert graph.get_state("t").val == "perfectly fine"
        assert len(errors) == 0

    def test_on_error_not_set_still_works(self):
        """Without on_error callback, bad assignments are silently swallowed."""

        class TypedState(param.Parameterized):
            count = param.Integer(default=0, allow_None=True)

        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="count")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")

        typed_state = TypedState(name="t")
        graph._nodes["t"] = typed_state
        graph._node_specs["t"] = specs["tgt"]

        graph.add_edge("s", "val", "t", "count")

        graph.get_state("s").val = "not_an_int"
        assert typed_state.count == 0

    def test_error_on_initial_propagation(self):
        """If source already has a value that the target rejects, on_error fires at wire time."""

        class TypedState(param.Parameterized):
            count = param.Integer(default=0, allow_None=True)

        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="count")]),
        }
        errors = []
        graph = DataflowGraph(specs, on_error=lambda *args: errors.append(args))
        graph.add_node("s", "src")

        typed_state = TypedState(name="t")
        graph._nodes["t"] = typed_state
        graph._node_specs["t"] = specs["tgt"]

        graph.get_state("s").val = "bad_value"

        graph.add_edge("s", "val", "t", "count")
        assert len(errors) == 1

    def test_watcher_removed_on_edge_disconnect(self):
        """After removing an edge, source changes no longer propagate."""
        specs = {
            "src": make_spec("src", outputs=[OutputPort(name="val")]),
            "tgt": make_spec("tgt", inputs=[InputPort(name="val", default="default")]),
        }
        graph = DataflowGraph(specs)
        graph.add_node("s", "src")
        graph.add_node("t", "tgt")
        graph.add_edge("s", "val", "t", "val")

        graph.get_state("s").val = "connected"
        assert graph.get_state("t").val == "connected"

        graph.remove_edge("s", "val", "t", "val")
        assert graph.get_state("t").val == "default"

        graph.get_state("s").val = "after_disconnect"
        assert graph.get_state("t").val == "default"
