"""Reports: the report key of the model and the YAML, its integrity, the derived report and live values."""

import json
import sqlite3
from contextlib import closing

import param
import pytest
import yaml

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
)
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.report import (
    ReportError,
    check_report,
    fill_report_text,
    placeholder_text,
    render_report_text,
    report_blocks,
)
from panel_flowdash.turns import change_labels

SECRET = "sk-live-0123456789abcdef"


class Credentials:
    """An object whose every textual form gives its secret away."""

    def __str__(self):
        return SECRET

    __repr__ = __str__

    def __format__(self, spec):
        return SECRET


class SecretStr(str):
    def __format__(self, spec):
        return SECRET

    def __str__(self):
        return SECRET


class MetricsConfig(param.Parameterized):
    total = param.Number(default=1234567.891)
    label = param.String(default="<b>EMEA</b>")


@register(
    page=False,
    component=True,
    title="Metrics",
    config_schema=MetricsConfig,
    provides=[
        {"key": "total", "type": "float"},
        {"key": "info", "type": "any"},
        {"key": "label", "type": "str"},
        {"key": "secret", "type": "any"},
        {"key": "nested", "type": "any"},
    ],
)
def metrics(config, instance_config):
    config.total = instance_config.total
    config.info = {"share": 0.256, "region": "EMEA"}
    config.label = instance_config.label
    config.secret = Credentials()
    config.nested = {"rows": [1, Credentials()], "name": SecretStr("x")}
    return "metrics"


@register(
    page=False,
    component=True,
    title="Chart",
    requires=[{"key": "value", "type": "any", "required": False}],
)
def chart(config):
    return "chart"


@register(page=False, component=True, title="Note", tags=["text"])
def note(config):
    return "note"


@register(
    page=False,
    component=True,
    sidebar=True,
    tags=["widget"],
    title="Region",
    provides=[{"key": "region", "type": "str"}],
)
def region(config):
    config.region = "EMEA"
    return "region"


COMPONENTS = {"R/metrics": metrics, "R/chart": chart, "R/note": note, "R/region": region}

DOC = """\
flowdash: 1
title: Revenue
turns:
- {id: t1, actor: user, at: '2026-11-22T10:00:00Z'}
- {id: t2, status: pending, question: Which products drove the drop}
groups:
  regional: {label: By region}
nodes:
  m: {component: R/metrics, origin: t1, position: [0, 0]}
  intro: {component: R/note, position: [0, 200]}
  east: {component: R/chart, group: regional, position: [300, 0]}
  west: {component: R/chart, group: regional, position: [300, 200]}
  solo: {component: R/chart, position: [600, 0]}
  pick: {component: R/region, position: [600, 200]}
edges:
- {from: m.total, to: east.value}
layout:
  tiles:
  - {node: intro}
  - {node: solo}
  - {node: west}
  - {node: east}
  - {node: m, visible: false}
"""

REPORT = """\
report:
  title: Q3 review
  blocks:
  - heading: Summary
    level: 1
  - text: Revenue is {{ m.total:,.0f }}.
    origin: t1
  - node: solo
    show: table
    caption: Solo, as a table
    height: 300
    page_break: true
  - group: regional
  - turn: t2
"""


def _editor(**params):
    return FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        user="alice",
        history_coalesce=0,
        **params,
    )


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Revenue")
    editor.import_dashboard(DOC + REPORT)
    yield editor
    editor.dispose()


@pytest.fixture
def plain_editor():
    editor = _editor()
    editor.new_dashboard("Revenue")
    editor.import_dashboard(DOC)
    yield editor
    editor.dispose()


def _blocks(editor):
    return editor.report["blocks"]


class TestYAML:
    def test_round_trip(self, editor):
        doc = dashboard_to_dict(editor)
        assert doc["report"] == yaml.safe_load(REPORT)["report"]
        assert list(doc)[-1] == "report"

    def test_reads_names_as_ids(self):
        base = dashboard_from_yaml(DOC)
        base["items"][4]["instance_id"] = "R__chart_3fa2c1"
        text = (DOC + REPORT).replace("solo", "chart_3fa2c1")
        data = dashboard_from_yaml(text, base=base)
        assert data["report"]["blocks"][2]["node"] == "R__chart_3fa2c1"

    def test_extension_keys_are_accepted_and_dropped(self):
        doc = yaml.safe_load(DOC + REPORT)
        doc["report"]["x-tool"] = 1
        doc["report"]["blocks"][0]["x-note"] = "kept out"
        data = dashboard_from_yaml(yaml.safe_dump(doc))
        assert "x-tool" not in data["report"]
        assert data["report"]["blocks"][0] == {"heading": "Summary", "level": 1}

    def test_no_report_key_is_no_report(self):
        assert "report" not in dashboard_from_yaml(DOC)

    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            (lambda r: r.update(pages=[]), r"report: unknown key 'pages'"),
            (lambda r: r.update(blocks={}), r"report.blocks: must be a list"),
            (lambda r: r.update(title=3), r"report.title: must be a string"),
            (lambda r: r["blocks"].append({"node": "nope"}), r"blocks\[5\].node: names no node"),
            (
                lambda r: r["blocks"].append({"group": "nope"}),
                r"blocks\[5\].group: names no group",
            ),
            (lambda r: r["blocks"].append({"turn": "t9"}), r"blocks\[5\].turn: names no turn"),
            (
                lambda r: r["blocks"][0].update(origin="t9"),
                r"blocks\[0\].origin: names no turn",
            ),
            (
                lambda r: r["blocks"].append({"heading": "A", "text": "B"}),
                r"blocks\[5\]: must have exactly one of",
            ),
            (lambda r: r["blocks"].append({"level": 2}), r"blocks\[5\]: must have exactly one"),
            (lambda r: r["blocks"].append("text"), r"blocks\[5\]: must be a mapping"),
            (
                lambda r: r["blocks"][0].update(show="table"),
                r"blocks\[0\]: unknown key 'show'",
            ),
            (lambda r: r["blocks"][0].update(level=4), r"blocks\[0\].level: must be 1, 2 or 3"),
            (lambda r: r["blocks"][2].update(show="chart"), r"blocks\[2\].show: must be view"),
            (
                lambda r: r["blocks"][2].update(height=-1),
                r"blocks\[2\].height: must be a positive",
            ),
            (lambda r: r["blocks"][2].update(page_break="yes"), r"page_break: must be true"),
            (lambda r: r["blocks"][1].update(text=None), r"blocks\[1\].text: must be a string"),
        ],
    )
    def test_refuses_malformed_reports(self, edit, message):
        doc = yaml.safe_load(DOC + REPORT)
        edit(doc["report"])
        with pytest.raises(DashboardYAMLError, match=message):
            dashboard_from_yaml(yaml.safe_dump(doc))

    def test_a_restricted_or_missing_reference_is_left_out_on_export(self, editor):
        data = editor.export_dashboard()
        data["report"]["blocks"].append({"node": "gone"})
        doc = dashboard_to_dict(data)
        assert {"node": "gone"} not in doc["report"]["blocks"]

    def test_the_schema_accepts_reports(self, editor):
        jsonschema = pytest.importorskip("jsonschema")
        from panel_flowdash.agent import dashboard_schema

        schema = dashboard_schema(editor.component_specs)
        doc = dashboard_to_dict(editor)
        jsonschema.validate(doc, schema)
        doc["report"]["blocks"][0]["show"] = "table"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


class TestModel:
    def test_check_report(self):
        assert check_report({"blocks": [{"text": ""}]}) == {"blocks": [{"text": ""}]}
        with pytest.raises(ReportError, match="heading: must not be empty"):
            check_report({"blocks": [{"heading": " "}]})

    def test_a_version_3_model_loads_without_a_report(self):
        data = {"dashboard_id": "d", "user_id": "u", "title": "T", "version": 3, "items": []}
        assert DashboardModel.from_dict(data).report is None

    def test_a_version_3_database_loads_and_saves_a_report(self, tmp_path):
        from .test_turns import _V3_SCHEMA

        path = tmp_path / "v3.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(_V3_SCHEMA)
            conn.execute(
                "INSERT INTO dashboards (dashboard_id, user_id, title, version) "
                "VALUES ('old', 'u', 'Old', 3)"
            )
        store = DashboardStore(path)
        model = store.load_dashboard("u", "old")
        assert model.report is None
        model.report = {"blocks": [{"heading": "Hi"}]}
        store.save_dashboard(model)
        assert DashboardStore(path).load_dashboard("u", "old").report == model.report

    def _model(self):
        model = DashboardModel(dashboard_id="d1", user_id="alice", title="T")
        model.report = {"title": "R", "blocks": [{"text": "Hello"}, {"node": "n1"}]}
        return model

    def test_memory_store(self):
        store = MemoryDashboardStore()
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").report == self._model().report

    def test_sqlite_store_drops_a_malformed_report(self, tmp_path):
        store = DashboardStore(tmp_path / "db.sqlite")
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").report == self._model().report
        with closing(sqlite3.connect(tmp_path / "db.sqlite")) as conn, conn:
            conn.execute('UPDATE dashboards SET report_json = \'{"blocks": [{"node": 1}]}\'')
        assert store.load_dashboard("alice", "d1").report is None

    def test_json_store_drops_a_malformed_report(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").report == self._model().report
        path = next(tmp_path.glob("*.json"))
        data = json.loads(path.read_text())
        data["report"] = {"blocks": "bad"}
        path.write_text(json.dumps(data))
        loaded = store.load_dashboard("alice", "d1")
        assert loaded is not None and loaded.report is None


class TestEditor:
    def test_saved_and_loaded(self, editor):
        editor.store = MemoryDashboardStore()
        model = editor.save()
        other = _editor(store=editor.store)
        other.load(model.dashboard_id)
        assert other.report == editor.report == model.report
        other.dispose()

    def test_removing_a_node_removes_its_blocks_and_one_undo_restores_both(self, editor):
        editor.add_component("R/chart", instance_id="extra")
        editor.set_report({"blocks": [*_blocks(editor), {"node": "extra"}, {"node": "solo"}]})
        versions = len(editor._history.versions)
        editor.remove_component("solo")
        assert len(editor._history.versions) == versions + 1
        assert all(block.get("node") != "solo" for block in _blocks(editor))
        assert {"node": "extra"} in _blocks(editor)
        assert all(block.get("node") != "solo" for block in editor.to_model().report["blocks"])
        assert editor.undo(whole_runs=False)
        assert any(i["instance_id"] == "solo" for i in editor._tile_items)
        assert [b.get("node") for b in _blocks(editor)].count("solo") == 2

    def test_a_removal_is_labelled_by_the_node_alone(self, editor):
        before = dashboard_to_dict(editor)
        editor.remove_component("solo")
        assert change_labels(before, dashboard_to_dict(editor)) == ["Removed solo"]

    def test_replacing_a_component_keeps_its_blocks(self, editor):
        editor._place("R/chart", "solo", {}, {"x": 0, "y": 0})
        assert _blocks(editor)[2]["node"] == "solo"

    def test_removing_a_group_drops_its_block(self, editor):
        editor.ungroup("regional")
        assert all("group" not in block for block in _blocks(editor))

    def test_set_report_is_an_undoable_edit_and_checks_references(self, editor):
        editor.set_report({"title": "Short", "blocks": [{"node": "solo"}]})
        assert editor.report == {"title": "Short", "blocks": [{"node": "solo"}]}
        labels = editor.turns[-1].summary
        assert "Changed the report" in labels
        with pytest.raises(ValueError, match=r"blocks\[0\]"):
            editor.set_report({"blocks": [{"node": "nope"}]})
        with pytest.raises(ValueError, match="exactly one"):
            editor.set_report({"blocks": [{}]})
        assert editor.undo(whole_runs=False)
        assert editor.report["title"] == "Q3 review"
        editor.set_report(None)
        assert editor.report is None and "report" not in dashboard_to_dict(editor)

    def test_turns_the_report_refers_to_are_kept_from_trimming(self, editor, monkeypatch):
        from panel_flowdash import turns

        monkeypatch.setattr(turns, "MAX_TURNS", 3)
        editor.remove_component("m")
        for i in range(4):
            editor.end_turn()
            editor.settings.refresh_period = i + 1
        ids = [turn.id for turn in editor.turns]
        # The import's turn is the origin of nodes, and the last turn is still open.
        assert ids[:2] == ["t1", "t2"] and len(ids) == 4
        assert {"turn": "t2"} in _blocks(editor)

    def test_an_origin_keeps_its_turn(self, editor, monkeypatch):
        from panel_flowdash import turns

        monkeypatch.setattr(turns, "MAX_TURNS", 2)
        editor.remove_component("m")
        editor.set_report({"blocks": [{"text": "Hi", "origin": "t1"}]})
        for i in range(3):
            editor.end_turn()
            editor.settings.refresh_period = i + 1
        ids = [turn.id for turn in editor.turns]
        assert ids[0] == "t1" and "t2" not in ids and len(ids) == 3


class TestDerivedReport:
    def test_groups_tiles_and_text_in_layout_order_with_controls_as_filters(self, plain_editor):
        resolved = report_blocks(plain_editor)
        assert resolved.derived and resolved.title == "Revenue"
        assert resolved.blocks == [{"node": "intro"}, {"node": "solo"}, {"group": "regional"}]
        assert resolved.filters == ["pick"]

    def test_authored_blocks(self, editor):
        resolved = report_blocks(editor)
        assert not resolved.derived
        assert resolved.title == "Q3 review"
        assert resolved.blocks == yaml.safe_load(REPORT)["report"]["blocks"]
        assert resolved.filters == ["pick"]

    def test_a_report_only_node_is_kept(self, editor):
        from panel_flowdash.ui import tile_layout

        tile_layout.update_tile(editor, "solo", visible=False)
        assert {"node": "solo"} not in report_blocks(editor).blocks
        assert _blocks(editor)[2]["node"] == "solo"


class TestPlaceholders:
    def test_fills_values_from_outputs(self, editor):
        html = render_report_text(
            editor,
            "Total **{{ m.total:,.0f }}**, share {{ m.info.share:.1% }} in {{ m.info.region }}.",
        )
        assert html == "<p>Total <strong>1,234,568</strong>, share 25.6% in EMEA.</p>\n"

    def test_values_are_literal_text(self, editor):
        html = render_report_text(editor, "In {{ m.label }}")
        assert "&lt;b&gt;EMEA&lt;/b&gt;" in html and "<b>" not in html
        assert fill_report_text(editor, "In {{ m.label }}") == "In &lt;b&gt;EMEA&lt;/b&gt;"

    def test_unknown_references_are_empty(self, editor):
        assert render_report_text(editor, "[{{ nope.total }}][{{ m.nope }}]") == "<p>[][]</p>\n"

    def test_a_secret_bearing_object_never_shows(self, editor):
        text = "{{ m.secret }} {{ m.secret:s }} {{ m.nested }} {{ m.nested.rows }} {{ m.nested.name }}"
        for rendered in (render_report_text(editor, text), fill_report_text(editor, text)):
            assert SECRET not in rendered
        assert render_report_text(editor, "{{ m.secret }}") == "<p>Credentials</p>\n"
        assert render_report_text(editor, "{{ m.nested.rows }}") == "<p>list</p>\n"
        assert render_report_text(editor, "{{ m.nested.name }}") == "<p>SecretStr</p>\n"

    def test_placeholder_text(self):
        np = pytest.importorskip("numpy")
        assert placeholder_text(np.float64(0.5), ".0%") == "50%"
        assert placeholder_text([1, 2.5, "a"]) == "1, 2.5, a"
        assert placeholder_text({"a": 1}) == "a: 1"
        assert placeholder_text(None) == ""
        assert placeholder_text(Credentials()) == "Credentials"
        assert placeholder_text({"k": Credentials()}) == "dict"

    def test_a_restricted_node_shows_nothing(self, editor, monkeypatch):
        monkeypatch.setattr(type(editor), "_node_restricted", lambda self, iid: iid == "m")
        assert render_report_text(editor, "{{ m.total }}") == "<p></p>\n"
