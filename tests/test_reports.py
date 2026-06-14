"""Tests for hosted report rendering."""

from __future__ import annotations

from automation.reports import render_to_html


def test_render_to_html_sanitizes_body_and_title(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "complete_report.md").write_text(
        "# <script>alert('title')</script>\n\n"
        "Safe text.\n\n"
        "<script>alert('body')</script>\n\n"
        "[bad](javascript:alert('x'))\n",
        encoding="utf-8",
    )

    html_path = render_to_html(report_dir)
    rendered = html_path.read_text(encoding="utf-8")

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(&#x27;title&#x27;)&lt;/script&gt;" in rendered
    assert "javascript:" not in rendered
    assert "Safe text." in rendered
