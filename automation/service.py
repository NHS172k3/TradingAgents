"""Composition root for the on-demand bot + report-hosting service.

Wires together :class:`automation.config.ServiceConfig`,
:class:`automation.store.Store`, :class:`automation.job_queue.JobQueue`, the
FastAPI report host (:mod:`automation.web.server`), and the Telegram bot
loop (:mod:`automation.bot`). Run with ``python -m automation.service``.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

import uvicorn
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from automation.bot import run_bot
from automation.config import ServiceConfig
from automation.job_queue import JobQueue
from automation.runlog import get_logger
from automation.store import Store
from automation.web.server import create_app

log = get_logger(__name__)

WEB_SHUTDOWN_TIMEOUT_SECONDS = 10


def main() -> None:
    config = ServiceConfig.from_env()

    store = Store(config.db_path)
    store.init_db()

    job_queue = JobQueue(store, config.report_cache_ttl_seconds)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())

    uvicorn_config = uvicorn.Config(
        create_app(store, config),
        host=config.web_host,
        port=config.web_port,
        log_level="info",
    )
    web_server = uvicorn.Server(uvicorn_config)
    web_thread = threading.Thread(target=web_server.run, daemon=True)
    web_thread.start()
    log.info("Report server listening on %s:%s", config.web_host, config.web_port)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("Received signal %s; shutting down", signum)
        stop_event.set()
        web_server.should_exit = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("Bot starting (preset=%s, daily_cap=%s)", config.preset, config.daily_cap)
    run_bot(config, store, job_queue, rate_limiter, stop_event=stop_event)

    web_server.should_exit = True
    web_thread.join(timeout=WEB_SHUTDOWN_TIMEOUT_SECONDS)
    store.close()
    log.info("Service stopped")


if __name__ == "__main__":
    main()
