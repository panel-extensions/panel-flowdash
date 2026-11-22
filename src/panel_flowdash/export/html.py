"""The static HTML writer: one self-contained file with a print stylesheet, which the PDF writer prints."""

from __future__ import annotations

import base64
import html

from panel_flowdash.export.document import (
    KPI,
    Block,
    Chart,
    Code,
    ExportDocument,
    Heading,
    Image,
    Placeholder,
    Section,
    Table,
    Text,
)

STYLESHEET = """
:root {
  color-scheme: light;
  --fd-text: #1f2328;
  --fd-muted: #5f6670;
  --fd-rule: #d8dce1;
  --fd-panel: #f6f8fa;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #fff;
  color: var(--fd-text);
  font: 15px/1.6 Roboto, "Helvetica Neue", Arial, sans-serif;
}
main {
  max-width: 860px;
  margin: 0 auto;
  padding: 48px 32px 64px;
}
h1, h2, h3, h4 { font-weight: 500; line-height: 1.25; margin: 1.6em 0 0.5em; }
h1.fd-title { font-size: 2rem; font-weight: 400; margin-top: 0; }
h2 { font-size: 1.45rem; }
h3 { font-size: 1.2rem; }
h4 { font-size: 1.05rem; }
p { margin: 0.6em 0; }
a { color: #0b62c4; }
.fd-provenance {
  color: var(--fd-muted);
  font-size: 0.85rem;
  border-bottom: 1px solid var(--fd-rule);
  padding-bottom: 12px;
  margin-bottom: 24px;
}
.fd-provenance dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 6px 0 0; }
.fd-provenance dt { font-weight: 500; }
.fd-provenance dd { margin: 0; }
.fd-block { margin: 18px 0; break-inside: avoid; }
figure { margin: 18px 0; break-inside: avoid; }
figure img { max-width: 100%; height: auto; display: block; }
figcaption, .fd-caption { color: var(--fd-muted); font-size: 0.85rem; margin-top: 6px; }
.fd-kpis { display: flex; flex-wrap: wrap; gap: 12px; margin: 18px 0; }
.fd-kpi {
  flex: 1 1 160px;
  border: 1px solid var(--fd-rule);
  border-radius: 8px;
  padding: 12px 16px;
  break-inside: avoid;
}
.fd-kpi-label { color: var(--fd-muted); font-size: 0.85rem; }
.fd-kpi-value { font-size: 1.8rem; line-height: 1.2; }
.fd-kpi-detail { color: var(--fd-muted); font-size: 0.85rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { text-align: left; padding: 4px 10px; border-bottom: 1px solid var(--fd-rule); vertical-align: top; }
th { font-weight: 500; border-bottom-width: 2px; }
.fd-table { overflow-x: auto; }
pre { background: var(--fd-panel); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; }
.fd-placeholder {
  border: 1px dashed var(--fd-rule);
  border-radius: 8px;
  padding: 12px 16px;
  color: var(--fd-muted);
}
.fd-placeholder strong { color: var(--fd-text); font-weight: 500; }
.fd-page-break { break-before: page; }
@page { size: A4; margin: 18mm 16mm; }
@media print {
  body { font-size: 11pt; }
  main { max-width: none; padding: 0; }
  a { color: inherit; text-decoration: none; }
  .fd-table { overflow: visible; }
  h2, h3, h4 { break-after: avoid; }
}
"""


def markdown_html(text: str) -> str:
    """Render Markdown as HTML, raw HTML shown as text and unsafe link targets dropped."""
    from panel_flowdash.text_components import _MARKDOWN

    return _MARKDOWN.render(text or "")


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _figure(src: str, alt: str, title: str, caption: str) -> str:
    parts = [f"<h4>{_e(title)}</h4>" if title else "", f'<img src="{_e(src)}" alt="{_e(alt)}">']
    if caption:
        parts.append(f"<figcaption>{_e(caption)}</figcaption>")
    return f"<figure>{''.join(parts)}</figure>"


def _kpi(block: KPI) -> str:
    detail = f'<div class="fd-kpi-detail">{_e(block.detail)}</div>' if block.detail else ""
    return (
        f'<div class="fd-kpi"><div class="fd-kpi-label">{_e(block.label)}</div>'
        f'<div class="fd-kpi-value">{_e(block.value)}</div>{detail}</div>'
    )


def _table(block: Table) -> str:
    head = "".join(f"<th>{_e(c)}</th>" for c in block.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in block.rows
    )
    notes = []
    if block.caption:
        notes.append(_e(block.caption))
    if block.total_rows > len(block.rows):
        notes.append(f"First {len(block.rows):,} of {block.total_rows:,} rows.")
    note = f'<div class="fd-caption">{" ".join(notes)}</div>' if notes else ""
    return (
        f'<div class="fd-block fd-table"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>{note}</div>"
    )


def _block(block: Block, level: int) -> str:
    if isinstance(block, Heading):
        tag = f"h{min(block.level + 1, 4)}"
        return f"<{tag}>{_e(block.text)}</{tag}>"
    if isinstance(block, Text):
        return f'<div class="fd-block fd-text">{markdown_html(block.markdown)}</div>'
    if isinstance(block, KPI):
        return f'<div class="fd-kpis">{_kpi(block)}</div>'
    if isinstance(block, Chart):
        if block.svg:
            src = _data_url("image/svg+xml", block.svg.encode("utf-8"))
        elif block.png:
            src = _data_url("image/png", block.png)
        else:
            return ""
        return _figure(src, block.title or "Chart", block.title, block.caption)
    if isinstance(block, Image):
        if block.data:
            src = _data_url(block.mime, block.data)
        elif block.url:
            src = block.url
        else:
            return ""
        return _figure(src, block.alt, "", block.caption)
    if isinstance(block, Table):
        return _table(block)
    if isinstance(block, Code):
        title = f"<h4>{_e(block.title)}</h4>" if block.title else ""
        language = f' class="language-{_e(block.language)}"' if block.language else ""
        return f'<div class="fd-block">{title}<pre><code{language}>{_e(block.source)}</code></pre></div>'
    if isinstance(block, Placeholder):
        title = f"<strong>{_e(block.title)}</strong><br>" if block.title else ""
        return f'<div class="fd-block fd-placeholder">{title}{_e(block.reason)}</div>'
    if isinstance(block, Section):
        return _section(block, level)
    return ""


def _blocks(blocks: list[Block], level: int) -> str:
    """Render blocks, setting runs of KPIs side by side as the dashboard does."""
    parts: list[str] = []
    run: list[KPI] = []
    for block in blocks:
        if isinstance(block, KPI):
            run.append(block)
            continue
        if run:
            parts.append(f'<div class="fd-kpis">{"".join(_kpi(k) for k in run)}</div>')
            run = []
        parts.append(_block(block, level))
    if run:
        parts.append(f'<div class="fd-kpis">{"".join(_kpi(k) for k in run)}</div>')
    return "\n".join(p for p in parts if p)


def _section(section: Section, level: int) -> str:
    classes = "fd-section fd-page-break" if section.page_break_before else "fd-section"
    tag = f"h{min(level + 1, 4)}"
    title = f"<{tag}>{_e(section.title)}</{tag}>" if section.title else ""
    return f'<section class="{classes}">{title}\n{_blocks(section.blocks, level + 1)}</section>'


def provenance_html(document: ExportDocument) -> str:
    """Return the provenance line and list under the title."""
    p = document.provenance
    rows = [(label, value) for label, value in p.filters]
    rows += [(name, value) for name, value in p.parameters]
    details = ""
    if rows:
        details = "<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows) + "</dl>"
    source = f" from the dashboard {_e(p.dashboard)}" if p.dashboard != document.title else ""
    generator = f" by {_e(p.generator)}" if p.generator else ""
    return (
        f'<div class="fd-provenance">Generated {_e(p.generated_text)}{source}{generator}. '
        f"Values are as they were then.{details}</div>"
    )


def write_html(document: ExportDocument) -> str:
    """Return *document* as one self-contained HTML page, with a print stylesheet."""
    body = _blocks(document.blocks, 1)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="generator" content="{_e(document.provenance.generator)}">\n'
        f"<title>{_e(document.title)}</title>\n<style>{STYLESHEET}</style>\n</head>\n"
        f'<body>\n<main>\n<h1 class="fd-title">{_e(document.title)}</h1>\n'
        f"{provenance_html(document)}\n{body}\n</main>\n</body>\n</html>\n"
    )
