"""Tests for the on-demand service SQLite store."""

from __future__ import annotations

import sqlite3

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


def test_list_reports_for_user_orders_most_recent_first_and_respects_limit(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.add_report(123, "NVDA", "2026-06-14", "/tmp/nvda.html")
        store.add_report(123, "AAPL", "2026-06-15", "/tmp/aapl.html")
        store.add_report(456, "TSLA", "2026-06-15", "/tmp/tsla.html")

        reports = store.list_reports_for_user(123, limit=1)

        assert len(reports) == 1
        assert reports[0].ticker == "AAPL"
    finally:
        store.close()


def test_watchlist_add_remove_list_and_cap(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        assert store.watchlist_add(123, "NVDA", max_size=2)
        assert store.watchlist_add(123, "AAPL", max_size=2)
        assert not store.watchlist_add(123, "NVDA", max_size=2)  # duplicate
        assert not store.watchlist_add(123, "TSLA", max_size=2)  # cap hit

        assert store.watchlist_list(123) == ["NVDA", "AAPL"]
        assert store.watchlist_list(456) == []

        assert store.watchlist_remove(123, "NVDA")
        assert not store.watchlist_remove(123, "NVDA")  # already removed
        assert store.watchlist_list(123) == ["AAPL"]
    finally:
        store.close()


def test_run_cache_hits_within_ttl_and_misses_after_expiry(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.upsert_cached_run("NVDA", "2026-06-15", "cost_saver", "Buy", "looks good", 42.0, "/tmp/r.html")

        cached = store.get_cached_run("NVDA", "2026-06-15", "cost_saver", max_age_seconds=3600)
        assert cached is not None
        assert cached.rating == "Buy"
        assert cached.html_path == "/tmp/r.html"

        assert store.get_cached_run("NVDA", "2026-06-15", "cost_saver", max_age_seconds=0) is None
        assert store.get_cached_run("AAPL", "2026-06-15", "cost_saver", max_age_seconds=3600) is None
    finally:
        store.close()

    # Backdate the row (via a separate connection, after closing the store's)
    # and confirm a now-stale cache entry is treated as a miss.
    db_path = tmp_path / "service.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE run_cache SET created_at = '2020-01-01T00:00:00' WHERE ticker = 'NVDA'"
    )
    conn.commit()
    conn.close()

    store2 = Store(db_path)
    try:
        assert store2.get_cached_run("NVDA", "2026-06-15", "cost_saver", max_age_seconds=3600) is None
    finally:
        store2.close()
