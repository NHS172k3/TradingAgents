"""Single-worker job queue for on-demand ticker analyses.

Ollama-backed runs are effectively serial, so one daemon worker thread
processes jobs from a plain ``queue.Queue``. Each job runs the existing
``runner.run_one_ticker`` pipeline, renders the resulting report to HTML,
records it in the store, and appends it to the shared decisions log so it
shows up in the dashboard and weekly digest alongside scheduled runs.
"""

from __future__ import annotations

import queue
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

    def __init__(self, store: Store) -> None:
        self._store = store
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._state_lock = threading.Lock()
        self._active = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> int:
        """Enqueue a job and return its position in the queue (1 = next)."""
        self._queue.put(job)
        return self._queue.qsize()

    def qsize(self) -> int:
        """Return the current number of jobs waiting (including any in progress)."""
        with self._state_lock:
            active = 1 if self._active else 0
        return self._queue.qsize() + active

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            with self._state_lock:
                self._active = True
            try:
                self._process(job)
            except Exception:
                log.exception("Unhandled error processing job for %s", job.spec.symbol)
            finally:
                with self._state_lock:
                    self._active = False
                self._queue.task_done()

    def _process(self, job: Job) -> None:
        _safe_call(job.on_start)

        log.info("Running %s (%s, preset %s)…", job.spec.symbol, job.date, job.spec.preset)
        result = run_one_ticker(job.spec, job.date)

        report_id: Optional[str] = None
        if result.ok and result.report_dir:
            try:
                html_path = reports.render_to_html(Path(result.report_dir))
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
