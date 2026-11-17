"""The editor against panel-reactflow's graph validation: unique node ids and node data schemas."""

import pytest

from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from tests.test_editor import COMPONENTS, DISPOSABLE, SELECTOR, Disposable, _place_disposable


@pytest.fixture
def editor():
    return FlowDash({**COMPONENTS, DISPOSABLE: Disposable}, notifications=False)


class TestReplacingAnInstanceId:
    async def test_one_node_and_one_tile_remain(self, editor):
        instance_id, previous = _place_disposable(editor)
        editor._place(DISPOSABLE, instance_id, {})
        assert [n["id"] for n in editor._flow.nodes] == [instance_id]
        assert [i["instance_id"] for i in editor._tile_items] == [instance_id]
        assert len(editor._tile_objects) == 1
        assert previous.disposals == 1

    async def test_replacing_while_loading_keeps_one_node(self, editor):
        model = DashboardModel(
            dashboard_id="d1",
            user_id="alice",
            title="Duplicate ids",
            items=[
                DashboardItem(instance_id="n1", component_id=DISPOSABLE),
                DashboardItem(instance_id="n1", component_id=DISPOSABLE),
            ],
        )
        editor.load_model(model)
        assert [n["id"] for n in editor._flow.nodes] == ["n1"]
        assert [i["instance_id"] for i in editor._tile_items] == ["n1"]


class TestInvalidNodeData:
    async def test_nothing_is_created_for_rejected_data(self, editor):
        pytest.importorskip("jsonschema")
        editor._flow.node_types = {
            "Demo__selector": {
                "type": "Demo__selector",
                "schema": {"type": "object", "required": ["x"]},
            }
        }
        with pytest.raises(ValueError, match="'x' is a required property"):
            editor._place(SELECTOR, "n1", {})
        assert "n1" not in editor.graph.node_ids
        assert "n1" not in editor._component_instances
        assert editor._flow.nodes == []
        assert editor._tile_items == []
