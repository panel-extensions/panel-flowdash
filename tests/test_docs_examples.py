"""Run the code examples of the README and the docs, so they keep working."""

import json
import re
import sys
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import pytest

from panel_flowdash import DashboardEdge, FlowDash, MemoryDashboardStore, register
from panel_flowdash.transforms import TRANSFORMS, parse_transform
from panel_flowdash.util import ReactiveError

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
PAGES = sorted([ROOT / "README.md", *DOCS.rglob("*.md")])

_BLOCK = re.compile(r"^```(?P<lang>\w+)(?P<info>[^\n]*)\n(?P<code>.*?)^```", re.S | re.M)
_TITLE = re.compile(r'title="(?P<title>[^"]+)"')
_FILE_COMMENT = re.compile(r"#\s*(?P<title>[\w/]+\.py)\s*$")


def code_blocks(page: Path, lang: str = "python") -> list[tuple[str | None, str]]:
    """Return ``(title, code)`` for each block, titled by its ``title=`` or a leading file comment."""
    blocks = []
    for match in _BLOCK.finditer(page.read_text(encoding="utf-8")):
        if match["lang"] != lang:
            continue
        code = match["code"]
        title = _TITLE.search(match["info"])
        if title is None:
            title = _FILE_COMMENT.match(code.splitlines()[0]) if code.strip() else None
        blocks.append((title["title"] if title else None, code))
    return blocks


def snippets(page: Path) -> list[str]:
    """Return the blocks that are not modules of a project, in page order."""
    return [code for title, code in code_blocks(page) if title is None or "/" not in title]


def run(code: str, namespace: dict | None = None) -> dict:
    namespace = {"__name__": "docs_example"} if namespace is None else namespace
    exec(compile(code, "<docs>", "exec"), namespace)
    return namespace


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Write the modules a page shows into ``my_project/`` and make it the working directory."""
    monkeypatch.chdir(tmp_path)
    # FlowDash puts a scanned project on sys.path, and its sections import as top-level packages.
    monkeypatch.setattr(sys, "path", list(sys.path))
    sections: set[str] = set()

    def forget():
        for name in list(sys.modules):
            if name.split(".")[0] in sections:
                del sys.modules[name]

    def write(page: Path) -> Path:
        for title, code in code_blocks(page):
            if title is None or title.count("/") != 1:
                continue
            section, module = title.split("/")
            folder = tmp_path / "my_project" / section
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "__init__.py").touch()
            (folder / module).write_text(code, encoding="utf-8")
            sections.add(section)
        forget()
        return tmp_path / "my_project"

    yield write
    forget()


@pytest.fixture
def transforms():
    saved = dict(TRANSFORMS)
    yield
    TRANSFORMS.clear()
    TRANSFORMS.update(saved)


def tile_view(editor: FlowDash, instance_id: str):
    index = [item["instance_id"] for item in editor._tile_items].index(instance_id)
    return editor._tile_objects[index]


def markdown_text(view) -> str:
    return "\n".join(str(pane.object) for pane in view.select(pn.pane.Markdown))


@pytest.mark.parametrize("page", PAGES, ids=lambda page: str(page.relative_to(ROOT)))
def test_python_blocks_compile(page):
    for _, code in code_blocks(page):
        compile(code, str(page), "exec")


@pytest.mark.parametrize("page", ["docs/quickstart.md", "README.md"])
def test_quickstart_selector_drives_chart(project, page):
    editor = FlowDash(str(project(ROOT / page)), store=MemoryDashboardStore())
    selector = editor.add_component("Analytics/selector")
    chart = editor.add_component("Analytics/chart")

    assert editor.connect(selector, "company", chart, "company") is True
    assert editor.graph.get_state(chart).company == "ACME"
    assert "ACME" in markdown_text(tile_view(editor, chart))

    (select,) = tile_view(editor, selector).select(pmui.Select)
    select.value = "Globex"

    assert editor.graph.get_state(chart).company == "Globex"
    assert "Globex" in markdown_text(tile_view(editor, chart))


def test_quickstart_wiring_in_python(project):
    page = DOCS / "quickstart.md"
    project(page)
    (code,) = snippets(page)
    namespace = run(code)
    assert namespace["editor"].graph.edges


def test_index_component_publishes_its_selection():
    namespace = run(snippets(DOCS / "index.md")[0])
    editor = FlowDash({"Market/ticker": namespace["app"]})
    ticker = editor.add_component("Market/ticker")
    assert editor.graph.get_state(ticker).ticker == "AAPL"

    (select,) = tile_view(editor, ticker).select(pmui.Select)
    select.value = "MSFT"
    assert editor.graph.get_state(ticker).ticker == "MSFT"


def test_pipeline_components(project):
    page = DOCS / "examples" / "pipeline.md"
    editor = FlowDash(str(project(page)), store=MemoryDashboardStore())
    source = editor.add_component("Pipeline/source")
    squarer = editor.add_component("Pipeline/transform")
    display = editor.add_component("Pipeline/display")
    assert editor.connect(source, "numbers", squarer, "numbers") is True
    assert editor.connect(squarer, "result", display, "result") is True

    assert editor.graph.get_state(display).result == [1, 4, 9, 16, 25]
    assert "**Sum:** 55" in markdown_text(tile_view(editor, display))

    (count,) = tile_view(editor, source).select(pmui.IntInput)
    count.value = 3
    assert editor.graph.get_state(display).result == [1, 4, 9]
    assert "**Sum:** 14" in markdown_text(tile_view(editor, display))


@pytest.mark.parametrize("page", ["examples/pipeline.md", "examples/viewer-components.md"])
def test_standalone_graph_examples(page):
    namespace = {"__name__": "docs_example"}
    for code in snippets(DOCS / page):
        run(code, namespace)
    assert namespace["graph"].edges


def test_transform_edges_examples(transforms):
    namespace = {"__name__": "docs_example"}
    for code in snippets(DOCS / "how-to" / "transform-edges.md"):
        run(code, namespace)
    assert TRANSFORMS["pick"] is namespace["PickKey"]


def test_transform_edges_stored_edge():
    (_, stored), *_ = code_blocks(DOCS / "how-to" / "transform-edges.md", lang="json")
    edge = DashboardEdge.from_dict(json.loads(stored))
    assert [step.to_dict() for step in parse_transform(edge.transform)] == edge.transform


async def test_flowdash_app_examples(tmp_path, monkeypatch):
    """The FlowDashApp snippets construct, given the names the surrounding text defines."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "my_project").mkdir()

    @register(component=True, page=False, provides=["ticker"])
    def ticker(config):
        return "ticker"

    pages = [
        "how-to/embed-the-editor.md",
        "how-to/wire-dataflow.md",
        "how-to/dashboard-settings.md",
    ]
    examples = [
        code
        for page in pages
        for code in snippets(DOCS / page)
        if "FlowDashApp(" in code
        and "\napp = " in code
        # Failure propagation needs Param 2.5.
        and (ReactiveError is not None or "propagate_failures" not in code)
    ]
    assert len(examples) >= (3 if ReactiveError is None else 4)
    for code in examples:
        namespace = run(
            code, {"__name__": "docs_example", "components": [ticker], "summarize": repr}
        )
        assert namespace["app"].store is not None
