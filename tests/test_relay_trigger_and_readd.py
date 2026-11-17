"""Edges forward triggered source values, and a node re-added under its old id is followed afresh."""

from panel_flowdash import ComponentSpec, InputPort, OutputPort
from panel_flowdash.dataflow_engine import DataflowGraph


def _spec(cid, inputs=(), outputs=()):
    return ComponentSpec(
        component_id=cid,
        title=cid,
        description=None,
        icon=None,
        tags=[],
        outputs=list(outputs),
        inputs=list(inputs),
        default_size=None,
    )


SPECS = {
    "src": _spec("src", outputs=[OutputPort(name="out", type=None)]),
    "sink": _spec("sink", inputs=[InputPort(name="inp", type=None)]),
    "lst": _spec("lst", inputs=[InputPort(name="items", type="list")]),
}


def test_trigger_after_in_place_mutation_reaches_every_consumer():
    graph = DataflowGraph(SPECS)
    source = graph.add_node("a", "src")
    source.out = [1]
    for sink in ("b", "c"):
        graph.add_node(sink, "sink")
        graph.add_edge("a", "out", sink, "inp")
    seen = {"b": [], "c": []}
    for sink, values in seen.items():
        graph.get_state(sink).param.watch(
            lambda e, values=values: values.append(list(e.new)), "inp", onlychanged=False
        )

    source.out.append(2)
    source.param.trigger("out")

    assert seen == {"b": [[1, 2]], "c": [[1, 2]]}


def test_node_removed_and_added_again_in_a_batch_is_followed():
    graph = DataflowGraph(SPECS)
    graph.add_node("a", "src")
    graph.add_node("l", "lst")
    graph.add_edge("a", "out", "l", "items")
    with graph.batch():
        graph.set_edge_transform("a", "out", "l", "items", [])
        graph.remove_node("a")
        fresh = graph.add_node("a", "src")
        graph.add_edge("a", "out", "l", "items")

    fresh.out = "fresh"

    assert graph.get_state("l").items == ["fresh"]
    assert len(graph._relays) == 1
