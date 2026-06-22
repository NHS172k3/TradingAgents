"""Tests for the on-demand job queue: cancellation and run caching."""

from __future__ import annotations

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


def test_cancel_last_for_user_removes_only_that_users_most_recent_queued_job(tmp_path):
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

        queue.submit(_make_job(123, "NVDA"))
        queue.submit(_make_job(123, "AAPL"))
        queue.submit(_make_job(456, "TSLA"))

        cancelled = queue.cancel_last_for_user(123)
        assert cancelled is not None
        assert cancelled.spec.symbol == "AAPL"

        assert queue.cancel_last_for_user(789) is None  # nothing queued for this user

        still_queued = queue.cancel_last_for_user(123)
        assert still_queued is not None
        assert still_queued.spec.symbol == "NVDA"
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
