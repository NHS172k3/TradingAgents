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
from dataclasses import dataclass
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
    worker.
    """

    user_id: int
    chat_id: str
    spec: TickerSpec
    date: str
    on_start: Callable[[], None]
    on_complete: Callable[[RunResult, Optional[str]], None]


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

    def cancel_last_for_user(self, user_id: int) -> Optional[Job]:
        """Remove and return the most recently queued job for ``user_id``.

        Only matches jobs still waiting in the queue — a job already picked
        up by the worker (in progress) is not cancellable.
        """
        with self._lock:
            for i in range(len(self._jobs) - 1, -1, -1):
                if self._jobs[i].user_id == user_id:
                    return self._jobs.pop(i)
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


def _safe_call(func: Callable, *args) -> None:
    try:
        func(*args)
    except Exception:
        log.exception("Job callback raised")


def _cache_age_minutes(created_at: str) -> float:
    age = _dt.datetime.now() - _dt.datetime.fromisoformat(created_at)
    return age.total_seconds() / 60
