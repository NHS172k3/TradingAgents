"""Single-worker job queue for on-demand ticker analyses.

Ollama-backed runs are effectively serial, so one daemon worker thread
processes jobs from a plain ``queue.Queue``. Each job runs the existing
``runner.run_one_ticker`` pipeline, renders the resulting report to HTML,
records it in the store, and appends it to the shared decisions log so it
shows up in the dashboard and weekly digest alongside scheduled runs.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from automation import reports
from automation.run_watchlist import append_decision
from automation.runner import RunResult, run_one_ticker
from automation.runlog import get_logger
from automation.settings import TickerSpec
from automation.store import Store

log = get_logger(__name__)


@dataclass(frozen=True)
class Job:
    """One on-demand analysis request.

    ``on_start`` is called (with no arguments) when the worker picks the job
    up. ``on_complete`` is called with the finished ``RunResult`` and the
    ``report_id`` (``None`` if the run failed or rendering failed). Both
    callbacks are best-effort: exceptions are logged and never stop the
    worker. ``cancel_requested`` is set by ``JobQueue.cancel_for_user`` when
    this job is already active (no queue removal is possible at that
    point) — the worker checks it after the run completes to decide
    whether to suppress the result and refund usage instead of delivering
    it normally.
    """

    user_id: int
    chat_id: str
    spec: TickerSpec
    date: str
    on_start: Callable[[], None]
    on_complete: Callable[[RunResult, Optional[str]], None]
    cancel_requested: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class CancelResult:
    """Outcome of ``JobQueue.cancel_for_user``."""

    symbol: str
    was_active: bool


class JobQueue:
    """FIFO queue processed by a single daemon worker thread."""

    def __init__(self, store: Store, cache_ttl_seconds: int) -> None:
        self._store = store
        self._cache_ttl_seconds = cache_ttl_seconds
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._active_job: Optional[Job] = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> int:
        """Enqueue a job and return its position in the queue (1 = next)."""
        with self._not_empty:
            self._jobs.append(job)
            position = len(self._jobs)
            self._not_empty.notify()
        return position

    def qsize(self) -> int:
        """Return the current number of jobs waiting (including any in progress)."""
        with self._lock:
            active = 1 if self._active_job is not None else 0
            return len(self._jobs) + active

    def cancel_for_user(self, user_id: int) -> Optional[CancelResult]:
        """Cancel the calling user's most recent job, queued or active.

        If the job is still waiting in the queue, it is removed outright
        and its daily-cap usage charge is refunded immediately. If the job
        is the one currently executing, it cannot be removed or
        interrupted (there is no interruption point inside the pipeline
        call) — instead it is flagged so the worker suppresses the result
        message and refunds usage once the run finishes.

        Returns ``None`` if the user has neither a queued nor an active job.
        """
        with self._lock:
            if self._active_job is not None and self._active_job.user_id == user_id:
                self._active_job.cancel_requested.set()
                return CancelResult(symbol=self._active_job.spec.symbol, was_active=True)
            for i in range(len(self._jobs) - 1, -1, -1):
                if self._jobs[i].user_id == user_id:
                    job = self._jobs.pop(i)
                    self._store.decrement_usage(job.user_id)
                    return CancelResult(symbol=job.spec.symbol, was_active=False)
        return None

    def _worker(self) -> None:
        while True:
            with self._not_empty:
                while not self._jobs:
                    self._not_empty.wait()
                job = self._jobs.pop(0)
                self._active_job = job
            try:
                self._process(job)
            except Exception:
                log.exception("Unhandled error processing job for %s", job.spec.symbol)
            finally:
                with self._lock:
                    self._active_job = None

    def _process(self, job: Job) -> None:
        _safe_call(job.on_start)

        cached = self._store.get_cached_run(
            job.spec.symbol, job.date, job.spec.preset, self._cache_ttl_seconds
        )
        if cached and Path(cached.html_path).exists():
            result = RunResult(
                ticker=job.spec.symbol,
                date=job.date,
                preset=job.spec.preset,
                rating=cached.rating,
                rationale=cached.rationale,
                duration_seconds=0.0,
            )
            if job.cancel_requested.is_set():
                self._finish_cancelled(job, result)
                return
            report_id: Optional[str] = None
            try:
                report_id = self._store.add_report(
                    job.user_id, result.ticker, result.date, cached.html_path
                )
            except Exception:
                log.exception("Failed to record cached report for %s", result.ticker)
            age_minutes = _cache_age_minutes(cached.created_at)
            log.info("%s → cache hit (age %.0fm)", result.ticker, age_minutes)
            _safe_call(job.on_complete, result, report_id)
            return

        log.info("Running %s (%s, preset %s)…", job.spec.symbol, job.date, job.spec.preset)
        result = run_one_ticker(job.spec, job.date)

        if job.cancel_requested.is_set():
            self._finish_cancelled(job, result)
            return

        report_id = None
        if result.ok and result.report_dir:
            try:
                html_path = reports.render_to_html(Path(result.report_dir))
                self._store.upsert_cached_run(
                    job.spec.symbol,
                    job.date,
                    job.spec.preset,
                    result.rating,
                    result.rationale,
                    result.duration_seconds,
                    str(html_path),
                )
                report_id = self._store.add_report(
                    job.user_id, result.ticker, result.date, str(html_path)
                )
            except Exception:
                log.exception("Failed to render/record report for %s", result.ticker)

        append_decision(result.to_record())

        if result.ok:
            log.info("%s → %s (%.0fs)", result.ticker, result.rating, result.duration_seconds)
        else:
            log.error("%s failed: %s", result.ticker, result.error)

        _safe_call(job.on_complete, result, report_id)

    def _finish_cancelled(self, job: Job, result: RunResult) -> None:
        """Refund usage and suppress the result for a job cancelled while active."""
        self._store.decrement_usage(job.user_id)
        log.info("%s → cancelled by user, suppressing result and refunding usage", result.ticker)


def _safe_call(func: Callable, *args) -> None:
    try:
        func(*args)
    except Exception:
        log.exception("Job callback raised")


def _cache_age_minutes(created_at: str) -> float:
    age = _dt.datetime.now() - _dt.datetime.fromisoformat(created_at)
    return age.total_seconds() / 60
