"""The PDF writer: the HTML writer's page printed by WeasyPrint, or by Chromium through Playwright."""

from __future__ import annotations

import concurrent.futures

from panel_flowdash.export.build import ExportUnavailable
from panel_flowdash.export.document import ExportDocument
from panel_flowdash.export.html import write_html

ENGINES = ("weasyprint", "chromium")

UNAVAILABLE = (
    "PDF export needs WeasyPrint (pip install 'panel-flowdash[pdf]') or Playwright with "
    "Chromium (pip install playwright, then playwright install chromium)."
)


def _weasyprint(page: str) -> bytes:
    from weasyprint import HTML

    # The page embeds its images, so WeasyPrint fetches nothing.
    def refuse(url, *args, **kwargs):
        raise ValueError(f"Exports fetch no URLs: {url[:80]}")

    return HTML(string=page, url_fetcher=refuse).write_pdf()


def _chromium(page: str) -> bytes:
    from playwright.sync_api import sync_playwright

    def run() -> bytes:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context(java_script_enabled=False, offline=True)
                tab = context.new_page()
                tab.set_content(page, wait_until="load")
                return tab.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()

    # Playwright's sync API refuses to run on a thread with an event loop, as a server's has.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result()


def available_engine() -> str | None:
    """Return the PDF engine that can run here, WeasyPrint first, or None."""
    try:
        import weasyprint  # noqa: F401

        return "weasyprint"
    except Exception:
        pass
    try:
        import playwright.sync_api  # noqa: F401

        return "chromium"
    except ImportError:
        return None


def write_pdf(document: ExportDocument, engine: str | None = None) -> bytes:
    """Return *document* as a PDF, printed from its HTML with *engine*, by default the first available."""
    engine = engine or available_engine()
    if engine not in ENGINES:
        raise ExportUnavailable(UNAVAILABLE)
    page = write_html(document)
    try:
        return _weasyprint(page) if engine == "weasyprint" else _chromium(page)
    except ImportError:
        raise ExportUnavailable(UNAVAILABLE) from None
    except Exception as exc:
        if engine == "chromium" and "Executable doesn't exist" in str(exc):
            raise ExportUnavailable(UNAVAILABLE) from None
        raise
