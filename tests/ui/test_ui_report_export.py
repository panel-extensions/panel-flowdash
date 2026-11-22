"""UI tests of exporting the report from the Report view."""

import pytest

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from .test_ui_report import _editor, _report
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _open_dialog(page):
    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    expect(report.get_by_text("Plant review")).to_be_visible()
    report.locator(".fd-report-export").click()
    dialog = page.get_by_role("dialog", name="Export report")
    expect(dialog).to_be_visible()
    return dialog


def test_export_markdown_with_pending_questions(page, tmp_path):
    editor = _editor()
    serve_component(page, editor)
    dialog = _open_dialog(page)
    dialog.get_by_text("Markdown", exact=True).click()
    # The report has a question not run yet, so the dialog offers to list it.
    dialog.get_by_text("Include questions not answered yet").click()
    with page.expect_download() as download:
        dialog.get_by_role("button", name="Export", exact=True).click()
    assert download.value.suggested_filename == "Plant_review.md"
    text = download.value.path().read_text()
    assert text.startswith("# Plant review\n")
    assert "The plant runs at 40 today." in text
    assert "**Output:** 40 MW" in text
    assert "> **Which month was the best?**: *Not answered yet.*" in text
    expect(dialog).to_be_hidden()


def test_export_pdf_through_chromium(page):
    pytest.importorskip("playwright.sync_api")
    editor = _editor()
    serve_component(page, editor)
    dialog = _open_dialog(page)
    dialog.get_by_text("PDF", exact=True).click()
    with page.expect_download(timeout=60_000) as download:
        dialog.get_by_role("button", name="Export", exact=True).click()
    assert download.value.suggested_filename == "Plant_review.pdf"
    assert download.value.path().read_bytes().startswith(b"%PDF")
