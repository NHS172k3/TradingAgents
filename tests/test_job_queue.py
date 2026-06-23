"""Tests for the on-demand job queue: cancellation and run caching."""

from __future__ import annotations

import threading
import time

from automation.job_queue import Job, JobQueue
from automation.runner import RunResult
from automation.settings import TickerSpec
from automation.store import Store


def _noop() -> None:
    pass


def _make_job(user_id: int, symbol: str) -> Job:
    return Job(
        user_id=user_id,
        chat_id="chat",
        spec=TickerSpec(symbol=symbol, preset="cost_saver", asset_type="stock"),
        date="2026-06-15",
        on_start=_noop,
        on_complete=lambda result, report_id: None,
    )


def test_cancel_for_user_removes_only_that_users_most_recent_queued_job_and_refunds_usage(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        queue = JobQueue(store, cache_ttl_seconds=0)
        # Block the worker on a long-running first job so subsequent submits stay queued.
        block_started = []

        def slow_on_start() -> None:
            block_started.append(True)
            time.sleep(2)

        blocker = Job(
            user_id=999,
            chat_id="chat",
            spec=TickerSpec(symbol="BLOCK", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=slow_on_start,
            on_complete=lambda result, report_id: None,
        )
        queue.submit(blocker)
        while not block_started:
            time.sleep(0.01)

        store.check_and_increment_usage(123, 5)
        store.check_and_increment_usage(123, 5)
        store.check_and_increment_usage(456, 5)
        queue.submit(_make_job(123, "NVDA"))
        queue.submit(_make_job(123, "AAPL"))
        queue.submit(_make_job(456, "TSLA"))

        cancelled = queue.cancel_for_user(123)
        assert cancelled is not None
        assert cancelled.symbol == "AAPL"
        assert cancelled.was_active is False

        assert queue.cancel_for_user(789) is None  # nothing queued for this user

        still_queued = queue.cancel_for_user(123)
        assert still_queued is not None
        assert still_queued.symbol == "NVDA"
        assert still_queued.was_active is False

        usage = dict(store.usage_today())
        assert usage[123] == 0  # both of user 123's queued jobs were cancelled and refunded
        assert usage[456] == 1  # untouched
    finally:
        store.close()


def test_cancel_for_user_on_an_active_job_suppresses_the_result_and_refunds_usage(tmp_path, monkeypatch):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.check_and_increment_usage(123, 5)

        def _fake_run_one_ticker(spec, date):
            time.sleep(0.2)
            return RunResult(
                ticker=spec.symbol, date=date, preset=spec.preset,
                rating="Buy", rationale="ok", duration_seconds=0.2,
            )

        monkeypatch.setattr("automation.job_queue.run_one_ticker", _fake_run_one_ticker)
        monkeypatch.setattr("automation.job_queue.append_decision", lambda record: None)

        queue = JobQueue(store, cache_ttl_seconds=0)
        started = threading.Event()
        results = []
        job = Job(
            user_id=123,
            chat_id="chat",
            spec=TickerSpec(symbol="NVDA", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=started.set,
            on_complete=lambda result, report_id: results.append((result, report_id)),
        )
        queue.submit(job)
        assert started.wait(timeout=5)

        cancelled = queue.cancel_for_user(123)
        assert cancelled is not None
        assert cancelled.symbol == "NVDA"
        assert cancelled.was_active is True

        deadline = time.monotonic() + 5
        while queue.qsize() > 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert results == []  # on_complete never called
        assert dict(store.usage_today())[123] == 0  # refunded
    finally:
        store.close()


def test_cancel_for_user_with_no_queued_or_active_job_returns_none(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        queue = JobQueue(store, cache_ttl_seconds=0)
        assert queue.cancel_for_user(123) is None
    finally:
        store.close()


def test_process_uses_cache_hit_and_skips_pipeline(tmp_path, monkeypatch):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        html_path = tmp_path / "report.html"
        html_path.write_text("<html></html>", encoding="utf-8")
        store.upsert_cached_run(
            "NVDA", "2026-06-15", "cost_saver", "Buy", "cached rationale", 12.0, str(html_path)
        )

        queue = JobQueue(store, cache_ttl_seconds=7200)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("run_one_ticker should not be called on a cache hit")

        monkeypatch.setattr("automation.job_queue.run_one_ticker", _fail_if_called)

        results = []
        job = Job(
            user_id=123,
            chat_id="chat",
            spec=TickerSpec(symbol="NVDA", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=_noop,
            on_complete=lambda result, report_id: results.append((result, report_id)),
        )
        queue.submit(job)

        deadline = time.monotonic() + 5
        while not results and time.monotonic() < deadline:
            time.sleep(0.01)

        assert len(results) == 1
        result, report_id = results[0]
        assert result.rating == "Buy"
        assert report_id is not None
        assert store.get_report(report_id).html_path == str(html_path)
    finally:
        store.close()
