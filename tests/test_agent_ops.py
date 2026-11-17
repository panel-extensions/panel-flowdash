"""The dashboard operations API: editing through the editor, previews and the user's rights."""

import param
import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem


@register(
    page=False,
    component=True,
    title="Number",
    description="A fixed number.",
    tags=["source"],
    provides=[{"key": "value", "type": "int"}],
)
def number(config):
    config.value = 21
    return "number"


@register(
    page=False,
    component=True,
    title="Doubler",
    provides=[{"key": "doubled", "type": "int"}],
    requires=[{"key": "value", "type": "int", "required": True}],
)
def doubler(config):
    def update(*_):
        config.doubled = None if config.value is None else config.value * 2

    config.param.watch(update, "value")
    update()
    return "doubler"


class Scaled(param.Parameterized):
    factor = param.Integer(default=1, bounds=(1, 10))


@register(
    page=False,
    component=True,
    title="Scaled",
    config_schema=Scaled,
    provides=[{"key": "out", "type": "int"}],
    requires=[{"key": "value", "type": "int", "required": False}],
)
def scaled(config):
    return "scaled"


@register(
    page=False,
    component=True,
    title="Secret source",
    allow_groups=["finance"],
    provides=[{"key": "value", "type": "int"}],
)
def secret(config):
    config.value = 1_000_000
    return "secret"


COMPONENTS = {
    "T/number": number,
    "T/doubler": doubler,
    "T/scaled": scaled,
    "T/secret": secret,
}


def _editor(store=None, *, finance=False, access=None, **params):
    """An editor for alice; *access* maps a dashboard id to the access she has: view or edit."""

    def authorize_dashboard(model, needed):
        granted = (access or {}).get(model.dashboard_id, "edit")
        return granted == "edit" or needed == granted

    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        authorize_component=lambda entry: finance or entry.app_id != "T/secret",
        authorize_dashboard=authorize_dashboard,
        **params,
    )


@pytest.fixture
def store():
    store = MemoryDashboardStore()
    model = store.create_dashboard("bob", "Finance")
    model.items = [
        DashboardItem("secret", "T/secret", config={"query": "SELECT salary"}),
        DashboardItem("double", "T/doubler"),
    ]
    model.edges = [DashboardEdge("secret", "value", "double", "value")]
    store.save_dashboard(model)
    return store


@pytest.fixture
def ops():
    editor = _editor()
    editor.new_dashboard("Mine")
    yield DashboardOps(editor)
    editor.dispose()


class TestEditing:
    def test_build_a_dashboard_from_small_operations(self, ops):
        source = ops.add_node("T/number")
        target = ops.add_node("T/doubler")
        scaled = ops.add_node("T/scaled", {"factor": 2})
        ops.connect(f"{source}.value", f"{target}.value")
        ops.connect(f"{target}.doubled", f"{scaled}.value", [{"op": "round", "digits": 0}])
        ops.set_config(scaled, {"factor": 3})
        ops.place_tile(scaled, width=50, height=200, index=0)
        ops.set_settings({"description": "Doubles a number"})
        doc = yaml.safe_load(ops.get_dashboard())
        assert list(doc["nodes"]) == ["number", "doubler", "scaled"]
        assert doc["nodes"]["scaled"]["config"] == {"factor": 3}
        assert doc["edges"][1]["transform"] == [{"op": "round", "digits": 0}]
        assert doc["layout"]["tiles"][0] == {"node": "scaled", "width": 50, "height": 200}
        assert doc["settings"]["description"] == "Doubles a number"
        assert ops.editor.dirty

        ops.set_transform("doubler.doubled", "scaled.value", [])
        ops.disconnect("number.value", "doubler.value")
        ops.remove_node("number")
        doc = yaml.safe_load(ops.get_dashboard())
        assert list(doc["nodes"]) == ["doubler", "scaled"]
        assert doc["edges"] == [{"from": "doubler.doubled", "to": "scaled.value"}]

    def test_refusals_say_how_to_fix_them(self, ops):
        node = ops.add_node("T/number")
        doubler = ops.add_node("T/doubler")
        with pytest.raises(OperationError, match=r"Unknown component 'T/numbers'\. Did you mean"):
            ops.add_node("T/numbers")
        with pytest.raises(OperationError, match="'Scaled' has no config field 'size'"):
            ops.add_node("T/scaled", {"size": 3})
        with pytest.raises(OperationError, match="Config 'factor'"):
            ops.add_node("T/scaled", {"factor": 50})
        with pytest.raises(OperationError, match="'number' \\(Number\\) has no input 'value'"):
            ops.connect(f"{doubler}.doubled", f"{node}.value")
        with pytest.raises(OperationError, match=r"No node 'doubl'\. Did you mean 'doubler'"):
            ops.connect(f"{node}.value", "doubl.value")
        with pytest.raises(OperationError, match="There is no edge"):
            ops.disconnect(f"{node}.value", f"{doubler}.value")
        with pytest.raises(OperationError, match="No setting 'colour'"):
            ops.set_settings({"colour": "red"})
        with pytest.raises(OperationError, match="width is a percentage"):
            ops.place_tile(node, width=150)

    def test_cycles_are_refused(self, ops):
        a = ops.add_node("T/scaled")
        b = ops.add_node("T/scaled")
        ops.connect(f"{a}.out", f"{b}.value")
        with pytest.raises(OperationError, match="cycle"):
            ops.connect(f"{b}.out", f"{a}.value")

    def test_replace_yaml_keeps_node_identity_and_reports_warnings(self, ops):
        number = ops.add_node("T/number")
        instance_id = ops.editor._tile_items[0]["instance_id"]
        warnings = ops.replace_yaml(
            f"flowdash: 1\nnodes:\n  {number}: {{component: T/number}}\n"
            "  waiting: {component: T/doubler}\n"
        )
        assert ops.editor._tile_items[0]["instance_id"] == instance_id
        assert [w.path for w in warnings] == ["nodes.waiting"]

    def test_replace_yaml_with_errors_changes_nothing(self, ops):
        ops.add_node("T/number")
        before = ops.get_dashboard()
        with pytest.raises(OperationError, match=r"nodes\.x\.component: Unknown component"):
            ops.replace_yaml("flowdash: 1\nnodes:\n  x: {component: T/nope}\n")
        assert ops.get_dashboard() == before

    def test_list_components_filters_by_query(self, ops):
        listed = ops.list_components("fixed number")
        assert [c["component"] for c in listed] == ["T/number"]
        assert listed[0]["outputs"] == [{"name": "value", "type": "int"}]


class TestPreview:
    def test_reports_health_and_samples(self, ops):
        number = ops.add_node("T/number")
        doubler = ops.add_node("T/doubler")
        ops.add_node("T/doubler")
        ops.connect(f"{number}.value", f"{doubler}.value")
        report = ops.preview()
        assert report["status"] == "waiting"
        assert report["nodes"]["doubler"]["outputs"] == {"doubled": {"type": "int", "value": 42}}
        assert report["nodes"]["doubler_2"]["status"] == "waiting"

    def test_previewing_yaml_leaves_the_editor_alone(self, ops):
        report = ops.preview(
            "flowdash: 1\nnodes:\n  n: {component: T/number}\n  d: {component: T/doubler}\n"
            "edges:\n  - {from: n.value, to: d.value}\n"
        )
        assert report["status"] == "ok"
        assert report["nodes"]["d"]["outputs"]["doubled"]["value"] == 42
        assert ops.editor._tile_items == []

    def test_dataframe_samples_show_shape_columns_and_head(self):
        pd = pytest.importorskip("pandas")
        from panel_flowdash.agent.preview import sample_value

        sample = sample_value(pd.DataFrame({"a": range(10), "b": list("abcdefghij")}), rows=2)
        assert sample == {
            "type": "DataFrame",
            "rows": 10,
            "columns": {"a": "int64", "b": sample["columns"]["b"]},
            "head": [{"a": 0, "b": "a"}, {"a": 1, "b": "b"}],
        }


class TestRights:
    """The agent can never do more than the user it acts for."""

    def _opened(self, store, **params):
        editor = _editor(store, **params)
        (model,) = store._all_dashboards()
        ops = DashboardOps(editor)
        ops.open_dashboard(model.dashboard_id)
        return ops, model.dashboard_id

    def test_restricted_components_are_neither_listed_nor_placeable(self, ops):
        assert "T/secret" not in [c["component"] for c in ops.list_components()]
        with pytest.raises(OperationError, match="Unknown component 'T/secret'"):
            ops.add_node("T/secret")

    def test_restricted_nodes_cannot_be_read_or_changed(self, store):
        ops, _ = self._opened(store)
        text = ops.get_dashboard()
        assert "salary" not in text
        assert yaml.safe_load(text)["nodes"]["secret"]["component"] == "flowdash:restricted"
        with pytest.raises(PermissionError, match="restricted"):
            ops.set_config("secret", {"query": "DROP"})
        with pytest.raises(PermissionError, match="restricted"):
            ops.connect("secret.value", "double.value")
        assert "outputs" not in ops.preview()["nodes"]["secret"]

    def test_replace_yaml_cannot_change_or_add_restricted_nodes(self, store):
        ops, did = self._opened(store)
        ops.replace_yaml(
            "flowdash: 1\nnodes:\n  secret: {component: 'flowdash:restricted'}\n"
            "  double: {component: T/doubler}\n"
        )
        ops.save()
        saved = store._load_any(did)
        first = saved.items[0]
        assert (first.instance_id, first.component_id, first.config) == (
            "secret",
            "T/secret",
            {"query": "SELECT salary"},
        )
        with pytest.raises(OperationError, match="Unknown component 'T/secret'"):
            ops.replace_yaml("flowdash: 1\nnodes:\n  mine: {component: T/secret}\n")

    def test_a_user_with_access_sees_everything(self, store):
        ops, _ = self._opened(store, finance=True)
        doc = yaml.safe_load(ops.get_dashboard())
        assert doc["nodes"]["secret"]["config"] == {"query": "SELECT salary"}
        assert ops.preview()["nodes"]["double"]["outputs"]["doubled"]["value"] == 2_000_000

    def test_viewers_cannot_change_or_save(self, store):
        (model,) = store._all_dashboards()
        ops, did = self._opened(store, access={model.dashboard_id: "view"})
        assert ops.editor.read_only
        for call in (
            lambda: ops.add_node("T/number"),
            lambda: ops.remove_node("double"),
            lambda: ops.set_settings({"description": "x"}),
            lambda: ops.replace_yaml("flowdash: 1\n"),
            lambda: ops.place_tile("double", width=50),
            ops.save,
        ):
            with pytest.raises(PermissionError):
                call()
        assert store._load_any(did) == model

    def test_edit_rights_revoked_after_opening_stop_further_changes(self, store):
        access = {}
        ops, did = self._opened(store, access=access)
        ops.add_node("T/number")
        access[did] = "view"
        with pytest.raises(PermissionError, match="no longer have edit access"):
            ops.add_node("T/number")
        with pytest.raises(PermissionError):
            ops.save()

    def test_dashboards_the_user_may_not_open_look_missing(self, store):
        (model,) = store._all_dashboards()
        ops = DashboardOps(_editor(store, access={model.dashboard_id: "none"}))
        assert ops.list_dashboards() == []
        with pytest.raises(KeyError, match="No dashboard"):
            ops.get_dashboard(model.dashboard_id)
        with pytest.raises(KeyError, match="No dashboard"):
            ops.open_dashboard(model.dashboard_id)
        missing = pytest.raises(KeyError, match="No dashboard")
        with missing:
            ops.get_dashboard("does-not-exist")

    def test_viewers_do_not_see_what_the_dashboard_hides(self, store):
        (model,) = store._all_dashboards()
        access = {model.dashboard_id: "view"}
        ops, _ = self._opened(store, finance=True, access=access, inspect_outputs=True)
        with pytest.raises(PermissionError, match="Only people who can edit"):
            ops.get_dashboard(model.dashboard_id)
        assert "outputs" not in ops.preview()["nodes"]["double"]

        model.settings["viewers_can_inspect"] = True
        store.save_dashboard(model)
        ops, _ = self._opened(store, finance=True, access=access, inspect_outputs=True)
        assert "double" in ops.get_dashboard(model.dashboard_id)
        assert "outputs" in ops.preview()["nodes"]["double"]
