"""Render saved analysis reports to static, shareable HTML.

Converts the markdown report already written by ``upstream.save_reports``
into a single self-contained HTML file (dark theme, no external assets).
The HTML is rendered once and cached on disk; the web server
(``automation/web/server.py``) then serves it as a static file with no
per-request markdown work.
"""

from __future__ import annotations

import html
from pathlib import Path

import bleach
import markdown

from automation.runlog import get_logger

log = get_logger(__name__)

COMPLETE_REPORT_FILENAME = "complete_report.md"
RENDERED_REPORT_FILENAME = "report.html"

# Tags/attributes allowed in the rendered output. Markdown from
# ``markdown.markdown`` only ever produces these tags for the report
# sections we generate, but the source text comes from an LLM, so it is
# treated as untrusted and sanitized before being written to disk.
_ALLOWED_TAGS = [
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "strong", "em", "b", "i", "code", "pre", "blockquote",
    "a", "table", "thead", "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
}

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{
    background: #0f1117;
    color: #e6e6e6;
    font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    max-width: 860px;
    margin: 2rem auto;
    padding: 0 1.5rem;
  }}
  h1, h2, h3, h4, h5, h6 {{ color: #f5f5f5; border-bottom: 1px solid #2a2d36; padding-bottom: 0.3rem; }}
  a {{ color: #6cb6ff; }}
  code, pre {{ background: #1c1f2a; border-radius: 4px; }}
  pre {{ padding: 0.75rem; overflow-x: auto; }}
  code {{ padding: 0.15rem 0.3rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #2a2d36; padding: 0.4rem 0.6rem; text-align: left; }}
  blockquote {{ border-left: 3px solid #2a2d36; margin-left: 0; padding-left: 1rem; color: #b0b0b0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def render_to_html(report_dir: Path) -> Path:
    """Render ``complete_report.md`` in ``report_dir`` to a static HTML file.

    Reads ``{report_dir}/complete_report.md``, converts it to HTML via the
    ``markdown`` library, sanitizes the result with ``bleach`` (the source
    text comes from an LLM and is untrusted), and wraps it in a minimal
    self-contained dark-theme template. The result is written to
    ``{report_dir}/report.html`` and overwritten on each call (idempotent).

    Args:
        report_dir: Directory containing ``complete_report.md``, as produced
            by ``automation.upstream.save_reports``.

    Returns:
        Path to the written ``report.html`` file.

    Raises:
        FileNotFoundError: if ``complete_report.md`` does not exist.
    """
    source_path = report_dir / COMPLETE_REPORT_FILENAME
    output_path = report_dir / RENDERED_REPORT_FILENAME

    markdown_text = source_path.read_text(encoding="utf-8")

    raw_html = markdown.markdown(markdown_text, extensions=["extra"])
    safe_html = bleach.clean(raw_html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES)

    title_line = markdown_text.splitlines()[0].lstrip("# ").strip() if markdown_text else "Report"
    title = html.escape(title_line, quote=True)
    document = _HTML_TEMPLATE.format(title=title, body=safe_html)

    output_path.write_text(document, encoding="utf-8")
    log.info("Rendered report HTML: %s", output_path)
    return output_path
