"""Tests for the on-demand service SQLite store."""

from __future__ import annotations

from automation.store import Store


def test_unlock_is_idempotent_and_allows_user(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        first_token = store.unlock(123, "alice")
        second_token = store.unlock(123, "alice-renamed")

        assert first_token == second_token
        assert store.is_allowed(123)
        assert store.get_token(123) == first_token
        assert not store.is_allowed(456)
    finally:
        store.close()


def test_usage_cap_is_enforced_per_day(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        assert store.check_and_increment_usage(123, 2, today="2026-06-15")
        assert store.check_and_increment_usage(123, 2, today="2026-06-15")
        assert not store.check_and_increment_usage(123, 2, today="2026-06-15")
        assert store.usage_today(today="2026-06-15") == [(123, 2)]

        assert store.check_and_increment_usage(123, 2, today="2026-06-16")
    finally:
        store.close()


def test_report_records_round_trip(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", "/tmp/report.html")
        report = store.get_report(report_id)

        assert report is not None
        assert report.report_id == report_id
        assert report.user_id == 123
        assert report.ticker == "NVDA"
        assert report.date == "2026-06-15"
        assert report.html_path == "/tmp/report.html"
    finally:
        store.close()
