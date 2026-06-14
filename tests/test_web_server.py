"""Tests for the token-gated report web server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from automation.store import Store
from automation.web.server import create_app


def test_report_route_requires_matching_user_token(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        token = store.unlock(123, "alice")
        html_path = tmp_path / "report.html"
        html_path.write_text("<html><body>report</body></html>", encoding="utf-8")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(html_path))
        client = TestClient(create_app(store))

        assert client.get(f"/report/{report_id}").status_code == 403
        assert client.get(f"/report/{report_id}?token=wrong").status_code == 403

        ok = client.get(f"/report/{report_id}?token={token}")
        assert ok.status_code == 200
        assert ok.text == "<html><body>report</body></html>"
        assert ok.headers["cache-control"] == "private, max-age=3600"
    finally:
        store.close()


def test_report_route_returns_gone_when_file_is_missing(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        token = store.unlock(123, "alice")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "missing.html"))
        client = TestClient(create_app(store))

        response = client.get(f"/report/{report_id}?token={token}")

        assert response.status_code == 410
    finally:
        store.close()
