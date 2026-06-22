"""Tests for the token-gated, rate-limited report web server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from automation.config import ServiceConfig
from automation.store import Store
from automation.tokens import sign_report_token
from automation.web.server import create_app

SIGNING_KEY = "test-signing-key"


def _config(**overrides) -> ServiceConfig:
    defaults = {
        "bot_token": "test-token",
        "invite_code": "test-invite",
        "public_base_url": "https://example.invalid",
        "reports_signing_key": SIGNING_KEY,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)


def test_report_route_serves_html_for_a_valid_signed_token(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        html_path = tmp_path / "report.html"
        html_path.write_text("<html><body>report</body></html>", encoding="utf-8")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(html_path))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={token}")

        assert response.status_code == 200
        assert response.text == "<html><body>report</body></html>"
        assert response.headers["cache-control"] == "private, max-age=3600"
    finally:
        store.close()


def test_report_route_rejects_missing_or_garbage_token(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        client = TestClient(create_app(store, _config()))

        assert client.get(f"/report/{report_id}").status_code == 403
        assert client.get(f"/report/{report_id}?token=garbage").status_code == 403
    finally:
        store.close()


def test_report_route_rejects_token_signed_for_a_different_report(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        other_token = sign_report_token(SIGNING_KEY, 123, "some-other-report-id")
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={other_token}")

        assert response.status_code == 403
    finally:
        store.close()


def test_report_route_rejects_token_signed_for_a_different_user(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        wrong_user_token = sign_report_token(SIGNING_KEY, 456, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={wrong_user_token}")

        assert response.status_code == 403
    finally:
        store.close()


def test_report_route_returns_not_found_for_unknown_report_id(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        client = TestClient(create_app(store, _config()))
        token = sign_report_token(SIGNING_KEY, 123, "unknown-id")

        response = client.get(f"/report/unknown-id?token={token}")

        assert response.status_code == 404
    finally:
        store.close()


def test_report_route_returns_gone_when_file_is_missing(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "missing.html"))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={token}")

        assert response.status_code == 410
    finally:
        store.close()


def test_report_route_is_rate_limited_per_client(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        html_path = tmp_path / "report.html"
        html_path.write_text("<html></html>", encoding="utf-8")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(html_path))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))
        url = f"/report/{report_id}?token={token}"

        statuses = [client.get(url).status_code for _ in range(31)]

        assert statuses[:30] == [200] * 30
        assert statuses[30] == 429
    finally:
        store.close()
