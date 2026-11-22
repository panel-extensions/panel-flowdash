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
    insert_block,
    move_block,
    placeholder_text,
    remove_block,
    render_report_text,
    report_blocks,
    set_title,
    update_block,
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
        doc["report"]["blocks"][0] = {"heading": " "}
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


class TestOps:
    def test_get_dashboard_round_trips_through_replace_yaml(self, editor):
        from panel_flowdash.agent import DashboardOps

        ops = DashboardOps(editor)
        text = ops.get_dashboard()
        assert "turn: t2" in text and "origin: t1" in text
        assert [i for i in ops.validate(text) if i.severity == "error"] == []
        ops.replace_yaml(text)
        assert editor.report == yaml.safe_load(REPORT)["report"]

    def test_block_origins_from_yaml_are_ignored(self, editor):
        from panel_flowdash.agent import DashboardOps

        ops = DashboardOps(editor)
        text = ops.get_dashboard().replace("origin: t1", "origin: t9")
        ops.replace_yaml(text)
        assert editor.report["blocks"][1]["origin"] == "t1"
        ops.replace_yaml(ops.get_dashboard().replace("Revenue is", "Sales are"))
        assert "origin" not in editor.report["blocks"][1]

    def test_a_turn_block_naming_no_turn_is_refused(self, editor):
        from panel_flowdash.agent import DashboardOps

        ops = DashboardOps(editor)
        text = ops.get_dashboard().replace("turn: t2", "turn: t9")
        issues = ops.validate(text)
        assert any("names no turn" in i.message for i in issues if i.severity == "error")

    def test_missing_placeholders_and_tables_are_warnings(self, editor):
        from panel_flowdash.agent import DashboardOps

        ops = DashboardOps(editor)
        doc = yaml.safe_load(ops.get_dashboard())
        doc["report"]["blocks"] += [
            {"text": "{{ nope.total }} {{ m.nope }} {{ m.total }}"},
            {"node": "intro", "show": "table"},
        ]
        issues = ops.validate(yaml.safe_dump(doc))
        warnings = [(i.path, i.message) for i in issues if i.severity == "warning"]
        assert [i for i in issues if i.severity == "error"] == []
        assert any("nope.total" in m and "names no node" in m for _, m in warnings)
        assert any("has no output 'nope'" in m for _, m in warnings)
        assert not any("m.total }}'" in m for _, m in warnings)
        assert (
            "report.blocks[6].show",
            "'intro' has no output to show as a table, so it shows nothing.",
        ) in warnings


class TestReportView:
    def test_values_are_marked_as_chips_and_stay_literal(self, editor):
        html = render_report_text(editor, "In {{ m.label }} x", value_class="v")
        assert html == '<p>In <span class="v">&lt;b&gt;EMEA&lt;/b&gt;</span> x</p>\n'

    def test_surface_and_explore_follow_each_other(self, editor):
        editor.surface = "report"
        assert not editor.explore and editor.report_view.shown
        editor.explore = True
        assert editor.surface == "explore" and not editor.report_view.shown
        editor.explore = False
        assert editor.surface == "workspace"

    def test_authored_report_blocks(self, editor):
        editor.editable = False
        editor.surface = "report"
        view = editor.report_view
        classes = [obj.css_classes for obj in view._blocks.objects]
        assert any("fd-report-pending" in c for c in classes)
        assert not view._notice.visible
        assert [item["label"] for item in view._contents.items] == [
            "Summary",
            "By region",
            "Which products drove the drop",
        ]
        assert view._contents.items[-1]["badge"] == "Not run"
        assert view._texts[0][0].object == (
            '<p>Revenue is <span class="fd-report-value">1,234,568</span>.</p>\n'
        )

    def test_derived_report_shows_the_notice(self, plain_editor):
        plain_editor.surface = "report"
        view = plain_editor.report_view
        assert view._notice.visible and view._customize.visible
        assert view._filters == ["pick"]
        plain_editor.editable = False
        assert view._notice.visible and not view._customize.visible
        assert "Edit the dashboard" in view._notice_text.object


class TestProseValues:
    def test_a_pair_reads_as_a_range_and_years_have_no_separator(self):
        assert placeholder_text((1981, 2022)) == "1981 to 2022"
        assert placeholder_text([0.25, 0.5], ".0%") == "25% to 50%"
        assert placeholder_text(2022) == "2022"
        assert placeholder_text(2022, ",") == "2,022"
        assert placeholder_text(12345) == "12,345"
        assert placeholder_text(True) == placeholder_text(True, "")
        assert placeholder_text([1, 2, 3]) == "1, 2, 3"


BLOCKS_REPORT = {"title": "T", "blocks": [{"heading": "A"}, {"text": "b"}, {"node": "c"}]}


class TestBlockOperations:
    def test_insert_move_remove_and_update_return_copies(self):
        report = BLOCKS_REPORT
        assert insert_block(report, 1, {"heading": "New"})["blocks"][1] == {"heading": "New"}
        assert insert_block(report, None, {"text": ""})["blocks"][-1] == {"text": ""}
        assert [next(iter(b)) for b in move_block(report, 0, 2)["blocks"]] == [
            "text",
            "node",
            "heading",
        ]
        assert remove_block(report, 1)["blocks"] == [{"heading": "A"}, {"node": "c"}]
        updated = update_block(report, 2, show="table", caption="Hi", page_break=True)
        assert updated["blocks"][2] == {
            "node": "c",
            "show": "table",
            "caption": "Hi",
            "page_break": True,
        }
        assert update_block(updated, 2, caption=None)["blocks"][2].get("caption") is None
        assert report == {"title": "T", "blocks": [{"heading": "A"}, {"text": "b"}, {"node": "c"}]}

    def test_titles(self):
        assert set_title(BLOCKS_REPORT, "  New  ")["title"] == "New"
        assert "title" not in set_title(BLOCKS_REPORT, "")
        assert "title" not in set_title(BLOCKS_REPORT, None)

    @pytest.mark.parametrize(
        "change",
        [
            lambda r: insert_block(r, 4, {"text": ""}),
            lambda r: insert_block(r, 0, {"nope": 1}),
            lambda r: move_block(r, 0, 3),
            lambda r: remove_block(r, -1),
            lambda r: update_block(r, 0, show="table"),
            lambda r: update_block(r, 0, heading=""),
        ],
    )
    def test_malformed_changes_are_refused(self, change):
        with pytest.raises(ReportError):
            change(BLOCKS_REPORT)


class TestAuthoring:
    def test_customize_writes_the_derived_blocks_as_one_edit(self, plain_editor):
        derived = report_blocks(plain_editor).blocks
        versions = len(plain_editor._history.versions)
        assert plain_editor.customize_report()
        assert plain_editor.report == {"blocks": derived}
        assert len(plain_editor._history.versions) == versions + 1
        assert not plain_editor.customize_report()
        assert plain_editor.undo(whole_runs=False)
        assert plain_editor.report is None
        assert plain_editor.redo()
        assert plain_editor.report == {"blocks": derived}

    def test_asking_adds_a_pending_turn_and_its_block_in_one_edit(self, editor):
        versions = len(editor._history.versions)
        turn_id = editor.ask_in_report("  Why did March drop?  ", 1)
        assert _blocks(editor)[1] == {"turn": turn_id}
        turn = next(t for t in editor.turns if t.id == turn_id)
        assert (turn.status, turn.question) == ("pending", "Why did March drop?")
        assert len(editor._history.versions) == versions + 1
        assert editor.undo(whole_runs=False)
        assert all(t.id != turn_id for t in editor.turns)
        assert {"turn": turn_id} not in _blocks(editor)
        assert editor.redo()
        assert {"turn": turn_id} in _blocks(editor)

    def test_asking_nothing_or_out_of_place_changes_nothing(self, editor):
        turns = [t.id for t in editor.turns]
        with pytest.raises(ValueError):
            editor.ask_in_report("   ")
        with pytest.raises(ValueError):
            editor.ask_in_report("Why?", 99)
        assert [t.id for t in editor.turns] == turns

    def test_asking_in_a_derived_report_customizes_it(self, plain_editor):
        derived = report_blocks(plain_editor).blocks
        turn_id = plain_editor.ask_in_report("Why?")
        assert _blocks(plain_editor) == [*derived, {"turn": turn_id}]

    def test_removing_a_pending_question_drops_its_turn(self, editor):
        report = editor.report
        index = report["blocks"].index({"turn": "t2"})
        editor.set_report(remove_block(report, index))
        assert all(t.id != "t2" for t in editor.turns)
        assert editor.undo(whole_runs=False)
        assert any(t.id == "t2" and t.status == "pending" for t in editor.turns)

    def test_labels_name_the_gesture(self, editor):
        editor.set_report(move_block(editor.report, 0, 1), label="Moved a block in the report")
        assert editor.edit_history.undo_label.endswith("Moved a block in the report")


@pytest.fixture
def author(editor):
    editor.editable = True
    editor.surface = "report"
    return editor


class TestAuthoringView:
    def _kinds(self, editor):
        return [next(iter(b)) for b in _blocks(editor)]

    def test_blocks_get_handles_menus_and_places_to_insert(self, author):
        view = author.report_view
        assert [item.kind for item in view._edit_items] == self._kinds(author)
        assert view._article.objects[1] is view._edit_blocks
        assert view._title_input.visible and view._title_input.value == "Q3 review"
        slots = [o for o in view._edit_blocks.objects if "fd-report-insert" in o.css_classes]
        assert len(slots) == len(_blocks(author)) + 1
        node = view._edit_items[2]
        labels = [entry["label"] for entry in node.menu.items]
        assert labels == [
            "Show as chart",
            "Remove caption",
            "Remove page break",
            "Select on canvas",
            "Move up",
            "Move down",
            "Remove from report",
        ]

    def test_readers_and_view_mode_get_the_document(self, author):
        view = author.report_view
        author.read_only = True
        assert not view._edit_items and view._article.objects[1] is view._blocks
        author.read_only = False
        author.editable = False
        assert not view._edit_items and not view._title_input.visible

    def test_gestures_are_one_version_each_and_undo(self, author):
        view = author.report_view
        history = author._history
        start = self._kinds(author)

        versions = len(history.versions)
        view._move(0, 3)
        assert self._kinds(author) == [start[1], start[2], start[3], start[0], start[4]]
        assert len(history.versions) == versions + 1
        assert author.undo(whole_runs=False)
        assert self._kinds(author) == start

        view._open_insert(1)
        assert view._insert_bar.__panel__() in view._edit_blocks.objects
        view._insert({"heading": "Notes"})
        assert _blocks(author)[1] == {"heading": "Notes"}
        assert view._insert_bar.__panel__() not in view._edit_blocks.objects

        view._open_insert(0)
        view._insert({"text": ""})
        text = view._edit_items[0].text
        assert text.area.active
        text.area.value = "Total is {{ m.total }}"
        assert _blocks(author)[0] == {"text": "Total is {{ m.total }}"}
        assert "1,234,567.891" in text.area.html

        view._on_menu(view._edit_items[0], {"action": "remove"})
        assert _blocks(author)[0] == {"heading": "Summary", "level": 1}
        assert author.undo(whole_runs=False)
        assert _blocks(author)[0]["text"] == "Total is {{ m.total }}"

    def test_menu_actions(self, author):
        view = author.report_view
        node = next(item for item in view._edit_items if item.kind == "node")
        view._on_menu(node, {"action": "view"})
        assert "show" not in _blocks(author)[node.index]
        view._on_menu(node, {"action": "uncaption"})
        assert "caption" not in _blocks(author)[node.index]
        view._on_menu(node, {"action": "caption"})
        assert node.caption.visible
        node.caption.value = "Fresh"
        assert _blocks(author)[node.index]["caption"] == "Fresh"
        view._on_menu(node, {"action": "unbreak"})
        assert "page_break" not in _blocks(author)[node.index]
        assert not node.page_break.visible
        index = node.index
        view._on_menu(node, {"action": "up"})
        assert _blocks(author)[index - 1]["node"] == "solo"

    def test_title_and_headings_edit_in_place(self, author):
        view = author.report_view
        view._title_input.value = "Revenue"
        # The dashboard's own title is no title of the report's.
        assert "title" not in author.report
        view._title_input.value = "Board pack"
        assert author.report["title"] == "Board pack"
        heading = view._edit_items[0]
        heading.heading.value = "Overview"
        assert _blocks(author)[0]["heading"] == "Overview"
        heading.heading.value = "  "
        assert _blocks(author)[0]["heading"] == "Overview"
        assert heading.heading.value == "Overview"

    def test_the_pickers_offer_what_the_report_does_not_show(self, author):
        view = author.report_view
        view._open_insert(0)
        nodes = {node["id"]: node for node in view._insert_bar.nodes}
        # Nodes the report shows, by themselves or in a group, are not offered again.
        assert not {"solo", "east", "west"} & set(nodes)
        assert nodes["pick"]["on_dashboard"] and not nodes["m"]["on_dashboard"]
        assert view._insert_bar.groups == []
        refs = [value["ref"] for value in view._value_choices()]
        assert "m.total" in refs
        view._ask("Where next?")
        turn_id = _blocks(author)[0]["turn"]
        assert next(t for t in author.turns if t.id == turn_id).status == "pending"
