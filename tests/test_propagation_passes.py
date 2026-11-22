"""A node whose inputs change together in one propagation pass runs once, with all of them."""

import panel as pn
import param
import pytest

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash, current_dashboard

RUNS: dict[str, list] = {}


@register(page=False, component=True, title="Number", provides=[{"key": "value", "type": "int"}])
class Number(pn.viewable.Viewer):
    value = param.Integer(default=1)

    def __panel__(self):
        return pn.pane.Markdown("number")


@register(page=False, component=True, title="Refreshed")
class Refreshed(pn.viewable.Viewer):
    """A source that reads again on every refresh, like a live query."""

    reads = param.Integer(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        if (editor := current_dashboard()) is not None:
            editor.param.watch(self._read, "refresh")

    def _read(self, *_):
        self.reads += 1

    @param.output(param.Integer)
    def value(self):
        return self.reads

    def __panel__(self):
        return pn.pane.Markdown("refreshed")


@register(page=False, component=True, title="Add one")
class AddOne(pn.viewable.Viewer):
    x = param.Integer(default=0)

    @param.output(param.Integer)
    def out(self):
        return self.x + 1

    def __panel__(self):
        return pn.pane.Markdown("add one")


@register(page=False, component=True, title="Double")
class Double(pn.viewable.Viewer):
    x = param.Integer(default=0)

    @param.output(param.Integer)
    def out(self):
        return self.x * 2

    def __panel__(self):
        return pn.pane.Markdown("double")


@register(page=False, component=True, title="Join")
class Join(pn.viewable.Viewer):
    a = param.Integer(default=0)
    b = param.Integer(default=0)

    @param.output(param.Integer)
    def total(self):
        RUNS.setdefault("join", []).append((self.a, self.b))
        return self.a + self.b

    def __panel__(self):
        return pn.pane.Markdown("join")


def join_function(config):
    return pn.pane.Markdown(
        pn.bind(
            lambda a, b: RUNS.setdefault("function", []).append((a, b)) or f"{a} {b}",
            config.param.a,
            config.param.b,
        )
    )


register(
    page=False,
    component=True,
    title="Function join",
    requires=[{"key": "a", "type": "int"}, {"key": "b", "type": "int"}],
)(join_function)


@register(page=False, component=True, title="Fragile", config=["c", "d"])
class Fragile(pn.viewable.Viewer):
    """A component whose own watcher of its first input, and of its first setting, raises."""

    a = param.Integer(default=0)
    b = param.Integer(default=0)
    c = param.Integer(default=0)
    d = param.Integer(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        self.param.watch(self._fail, ["a", "c"])
        self.param.watch(
            lambda event: RUNS.setdefault("fragile", []).append(event.name), ["b", "d"]
        )

    def _fail(self, event):
        raise RuntimeError(f"{event.name} failed")

    def __panel__(self):
        return pn.pane.Markdown("fragile")


COMPONENTS = {
    "t/fragile": Fragile,
    "t/number": Number,
    "t/refreshed": Refreshed,
    "t/add": AddOne,
    "t/double": Double,
    "t/join": Join,
    "t/function": join_function,
}


@pytest.fixture(autouse=True)
def _runs():
    RUNS.clear()
    yield
    RUNS.clear()


def _editor(items, edges):
    editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
    model = DashboardModel(
        dashboard_id="d",
        user_id="u",
        title="Passes",
        items=[DashboardItem(iid, f"t/{kind}") for iid, kind in items],
        edges=[DashboardEdge(*edge) for edge in edges],
    )
    editor.load_model(model)
    RUNS.clear()
    return editor


@pytest.mark.parametrize("sink", ["join", "function"])
def test_diamond_sink_runs_once_per_change_without_mixed_inputs(sink):
    editor = _editor(
        [("a", "number"), ("b", "add"), ("c", "double"), ("d", sink)],
        [
            ("a", "value", "b", "x"),
            ("a", "value", "c", "x"),
            ("b", "out", "d", "a"),
            ("c", "out", "d", "b"),
        ],
    )
    source = editor._component_instances["a"]

    source.value = 5
    assert RUNS[sink] == [(6, 10)]

    source.value = 7
    assert RUNS[sink] == [(6, 10), (8, 14)]
    if sink == "join":
        assert editor.graph.get_state("d").total == 22
    editor.dispose()


def test_two_sources_changed_by_one_refresh_run_their_consumer_once():
    editor = _editor(
        [("x", "refreshed"), ("y", "refreshed"), ("j", "join")],
        [("x", "value", "j", "a"), ("y", "value", "j", "b")],
    )

    editor.trigger_refresh()
    assert RUNS["join"] == [(1, 1)]

    editor.trigger_refresh()
    assert RUNS["join"] == [(1, 1), (2, 2)]
    editor.dispose()


def test_single_input_change_is_delivered_immediately():
    editor = _editor([("a", "number"), ("b", "add")], [("a", "value", "b", "x")])
    source, consumer = editor._component_instances["a"], editor._component_instances["b"]

    source.value = 3
    assert consumer.x == 3
    assert editor.graph.get_state("b").out == 4
    editor.dispose()


def _graph_specs():
    from panel_flowdash import ComponentSpec, InputPort, OutputPort

    def spec(cid, inputs=(), outputs=()):
        return ComponentSpec(
            component_id=cid,
            title=cid,
            description=None,
            icon=None,
            tags=[],
            outputs=[OutputPort(name=n) for n in outputs],
            inputs=[InputPort(name=n) for n in inputs],
            default_size=None,
        )

    return {
        "src": spec("src", outputs=["out", "other"]),
        "pass": spec("pass", inputs=["inp"], outputs=["out"]),
        "pair": spec("pair", inputs=["a", "b"], outputs=["out"]),
    }


def _pair(graph, node_id, runs):
    state = graph.add_node(node_id, "pair")

    def combine(a, b):
        runs.append((a, b))
        return (a, b)

    state.out = param.bind(combine, state.param.a, state.param.b)
    return state


def test_graph_diamond_delivers_both_inputs_in_one_update():
    from panel_flowdash.dataflow_engine import DataflowGraph

    graph = DataflowGraph(_graph_specs())
    source = graph.add_node("a", "src")
    source.out = 0
    for node_id, step in (("b", 1), ("c", 10)):
        state = graph.add_node(node_id, "pass")
        state.out = param.bind(
            lambda inp, step=step: None if inp is None else inp + step, state.param.inp
        )
        graph.add_edge("a", "out", node_id, "inp")
    runs = []
    sink = _pair(graph, "d", runs)
    graph.add_edge("b", "out", "d", "a")
    graph.add_edge("c", "out", "d", "b")
    runs.clear()

    source.out = 5
    assert runs == [(6, 15)]
    assert sink.out == (6, 15)


def test_graph_outputs_set_together_reach_a_consumer_together():
    from panel_flowdash.dataflow_engine import DataflowGraph

    graph = DataflowGraph(_graph_specs())
    source = graph.add_node("s", "src")
    source.param.update(out=0, other=0)
    runs = []
    _pair(graph, "j", runs)
    graph.add_edge("s", "out", "j", "a")
    graph.add_edge("s", "other", "j", "b")
    runs.clear()

    source.param.update(out=1, other=2)
    assert runs == [(1, 2)]

    source.out = 3
    assert runs == [(1, 2), (3, 2)]


def test_graph_feedback_within_a_pass_is_reported_instead_of_looping():
    from panel_flowdash.dataflow_engine import _MAX_DELIVERIES, DataflowGraph

    errors = []
    graph = DataflowGraph(_graph_specs(), on_error=lambda *args: errors.append(args))
    source = graph.add_node("s", "src")
    source.out = 0
    sink = graph.add_node("n", "pass")
    graph.add_edge("s", "out", "n", "inp")
    runs = []

    def feed_back(event):
        # A consumer that sets its source again, as two linked widgets might.
        runs.append(event.new)
        source.out = event.new + 1

    sink.param.watch(feed_back, "inp")
    source.out = 1
    assert len(runs) == _MAX_DELIVERIES
    assert {error[2:4] for error in errors} == {("n", "inp")}
    assert isinstance(errors[0][4], RecursionError)


def test_input_watcher_raising_does_not_keep_the_other_inputs_from_the_component():
    editor = _editor([("f", "fragile")], [])
    errors = []
    editor._report_input_error = lambda node_id, port, exc: errors.append((node_id, port))

    editor.graph.get_state("f").param.update(a=1, b=2)
    instance = editor._component_instances["f"]
    assert (instance.a, instance.b) == (1, 2)
    assert RUNS["fragile"] == ["b"]
    assert errors == [("f", "a")]
    editor.dispose()


def test_config_watcher_raising_does_not_keep_the_other_settings_from_the_component():
    editor = _editor([("f", "fragile")], [])

    editor.graph.get_config_state("f").param.update(c=1, d=2)
    instance = editor._component_instances["f"]
    assert (instance.c, instance.d) == (1, 2)
    assert RUNS["fragile"] == ["d"]
    editor.dispose()
