"""Long-poll Telegram bot for on-demand ticker analysis.

Dispatches incoming messages: ``/start <code>`` unlocks the sender via the
invite code, ``/help`` shows usage, plain-text tickers are queued for
analysis (subject to the daily cap), and ``/status`` gives the admin a
queue/usage/log snapshot. Reuses :mod:`automation.job_queue` for execution
and :mod:`automation.telegram_api` for I/O (including 4096-char chunking).
"""

from __future__ import annotations

import datetime as _dt
import hmac
import json
import re
import threading
import time
from typing import Optional

from automation import settings, telegram_api
from automation.calendar_check import is_trading_day
from automation.config import ServiceConfig
from automation.job_queue import Job, JobQueue
from automation.runlog import get_logger
from automation.runner import RunResult
from automation.settings import TickerSpec
from automation.store import Store

log = get_logger(__name__)

POLL_TIMEOUT_SECONDS = 30
MAX_SYMBOLS_PER_MESSAGE = 3
LOG_TAIL_LINES = 20
HISTORY_DEFAULT_LIMIT = 5
HISTORY_MAX_LIMIT = 10
WATCHLIST_MAX_SIZE = 10

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Registered with Telegram via setMyCommands so they show up in the client's
# "/" menu. /status is admin-only but still listed here per the agreed
# design — non-admins get a polite refusal if they try it.
_COMMANDS: list[tuple[str, str]] = [
    ("start", "Unlock access with an invite code"),
    ("help", "Show usage instructions"),
    ("cancel", "Cancel your most recently queued analysis"),
    ("history", "Show your recent analyses"),
    ("watchlist", "Manage your personal ticker watchlist"),
    ("status", "Admin only: queue/usage/log snapshot"),
]


def run_bot(
    config: ServiceConfig,
    store: Store,
    job_queue: JobQueue,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Long-poll Telegram and dispatch incoming messages.

    Runs until ``stop_event`` is set (checked between long-polls), or
    forever if ``stop_event`` is ``None``. Network errors are logged and
    retried; a single update's failure never aborts the loop.
    """
    telegram_api.set_my_commands(_COMMANDS, token=config.bot_token)

    offset = 0
    while stop_event is None or not stop_event.is_set():
        try:
            updates = telegram_api.get_updates(
                offset, POLL_TIMEOUT_SECONDS, token=config.bot_token
            )
        except Exception:
            log.exception("get_updates failed; retrying")
            time.sleep(POLL_TIMEOUT_SECONDS)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                _handle_update(update, config, store, job_queue)
            except Exception:
                log.exception("Failed to handle update %s", update.get("update_id"))


def _handle_update(update: dict, config: ServiceConfig, store: Store, job_queue: JobQueue) -> None:
    message = update.get("message")
    if not message or "text" not in message:
        return

    chat_id = str(message["chat"]["id"])
    user_id = message["from"]["id"]
    user_name = message["from"].get("username") or message["from"].get("first_name") or "there"
    text = message["text"].strip()

    if text.startswith("/start"):
        _handle_start(text, user_id, user_name, chat_id, config, store)
    elif text == "/help":
        _reply(chat_id, _help_text(config), config)
    elif text == "/status":
        _handle_status(user_id, chat_id, config, store, job_queue)
    elif text == "/cancel":
        _handle_cancel(user_id, chat_id, job_queue, config)
    elif text == "/history" or text.startswith("/history "):
        _handle_history(text, user_id, chat_id, store, config)
    elif text == "/watchlist" or text.startswith("/watchlist "):
        _handle_watchlist(text, user_id, chat_id, config, store, job_queue)
    else:
        _handle_ticker_message(text, user_id, chat_id, config, store, job_queue)


def _handle_start(
    text: str, user_id: int, user_name: str, chat_id: str, config: ServiceConfig, store: Store
) -> None:
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""

    if not code:
        _reply(chat_id, _help_text(config), config)
        return

    if hmac.compare_digest(code, config.invite_code):
        store.unlock(user_id, user_name)
        _reply(
            chat_id,
            "✅ You're in! Send a ticker (e.g. NVDA) to run an analysis.\n\n" + _help_text(config),
            config,
        )
    else:
        _reply(chat_id, "❌ That invite code isn't valid. Ask the owner for an invite code.", config)


def _handle_status(user_id: int, chat_id: str, config: ServiceConfig, store: Store, job_queue: JobQueue) -> None:
    if config.admin_user_id is None or user_id != config.admin_user_id:
        _reply(chat_id, "This command is restricted to the service admin.", config)
        return

    lines = [f"Queue depth: {job_queue.qsize()}"]

    usage = store.usage_today()
    if usage:
        lines.append("Today's usage: " + ", ".join(f"{uid}: {count}" for uid, count in usage))
    else:
        lines.append("Today's usage: none yet")

    last_run = _last_decision()
    if last_run:
        lines.append(f"Last run: {last_run}")

    log_tail = _tail_log()
    if log_tail:
        lines.append("Recent log:\n" + log_tail)

    _reply(chat_id, "\n".join(lines), config)


def _handle_ticker_message(
    text: str, user_id: int, chat_id: str, config: ServiceConfig, store: Store, job_queue: JobQueue
) -> None:
    if not store.is_allowed(user_id):
        _reply(chat_id, "You need an invite code first. Send /start <code> to get started.", config)
        return

    symbols = _parse_symbols(text)
    if not symbols:
        _reply(chat_id, "I couldn't find a ticker in that message.\n\n" + _help_text(config), config)
        return

    _enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue)


def _enqueue_symbols(
    symbols: list[str], user_id: int, chat_id: str, config: ServiceConfig, store: Store, job_queue: JobQueue
) -> None:
    date = _default_date()
    for symbol in symbols:
        if not store.check_and_increment_usage(user_id, config.daily_cap):
            _reply(
                chat_id,
                f"You've reached your limit of {config.daily_cap} analyses today. Try again tomorrow.",
                config,
            )
            break

        spec = TickerSpec(symbol=symbol, preset=config.preset, asset_type="stock")
        job = _build_job(symbol, user_id, chat_id, spec, date, config, store)
        position = job_queue.submit(job)
        _reply(chat_id, f"📥 {symbol} queued (position {position}). I'll message you when it's done.", config)


def _handle_cancel(user_id: int, chat_id: str, job_queue: JobQueue, config: ServiceConfig) -> None:
    job = job_queue.cancel_last_for_user(user_id)
    if job:
        _reply(chat_id, f"❌ Cancelled {job.spec.symbol} (removed from queue).", config)
    else:
        _reply(chat_id, "You don't have anything queued to cancel.", config)


def _handle_history(text: str, user_id: int, chat_id: str, store: Store, config: ServiceConfig) -> None:
    if not store.is_allowed(user_id):
        _reply(chat_id, "You need an invite code first. Send /start <code> to get started.", config)
        return

    parts = text.split(maxsplit=1)
    limit = HISTORY_DEFAULT_LIMIT
    if len(parts) > 1 and parts[1].strip().isdigit():
        limit = max(1, min(HISTORY_MAX_LIMIT, int(parts[1].strip())))

    reports_list = store.list_reports_for_user(user_id, limit)
    if not reports_list:
        _reply(chat_id, "You haven't run any analyses yet.", config)
        return

    token = store.get_token(user_id)
    header = f"📚 Your last {len(reports_list)} analyses:"
    entries = [
        f"{r.ticker} — {r.date}\n📄 {config.public_base_url}/report/{r.report_id}?token={token}"
        for r in reports_list
    ]
    _reply(chat_id, header + "\n\n" + "\n\n".join(entries), config)


def _handle_watchlist(
    text: str, user_id: int, chat_id: str, config: ServiceConfig, store: Store, job_queue: JobQueue
) -> None:
    if not store.is_allowed(user_id):
        _reply(chat_id, "You need an invite code first. Send /start <code> to get started.", config)
        return

    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"

    if sub == "list":
        symbols = store.watchlist_list(user_id)
        if symbols:
            _reply(chat_id, "⭐ Your watchlist:\n" + ", ".join(symbols), config)
        else:
            _reply(chat_id, "Your watchlist is empty. Add tickers: /watchlist add NVDA", config)

    elif sub == "add":
        candidates = [c.upper() for c in parts[2:]]
        valid = [c for c in candidates if _SYMBOL_RE.match(c)]
        if not valid:
            _reply(chat_id, "Usage: /watchlist add NVDA [AAPL ...]", config)
            return
        added = [s for s in valid if store.watchlist_add(user_id, s, max_size=WATCHLIST_MAX_SIZE)]
        skipped = [s for s in valid if s not in added]
        lines = []
        if added:
            lines.append(f"✅ Added: {', '.join(added)}")
        if skipped:
            lines.append(
                f"⚠️ Skipped (already saved or watchlist full at {WATCHLIST_MAX_SIZE}): "
                + ", ".join(skipped)
            )
        _reply(chat_id, "\n".join(lines), config)

    elif sub == "remove":
        candidates = [c.upper() for c in parts[2:]]
        if not candidates:
            _reply(chat_id, "Usage: /watchlist remove NVDA", config)
            return
        removed = [s for s in candidates if store.watchlist_remove(user_id, s)]
        missing = [s for s in candidates if s not in removed]
        lines = []
        if removed:
            lines.append(f"🗑️ Removed: {', '.join(removed)}")
        if missing:
            lines.append("Not found in your watchlist: " + ", ".join(missing))
        _reply(chat_id, "\n".join(lines), config)

    elif sub == "run":
        symbols = store.watchlist_list(user_id)
        if not symbols:
            _reply(chat_id, "Your watchlist is empty. Add tickers first: /watchlist add NVDA", config)
            return
        _enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue)

    else:
        _reply(chat_id, "Usage: /watchlist [list|add SYM...|remove SYM...|run]", config)


def _build_job(
    symbol: str, user_id: int, chat_id: str, spec: TickerSpec, date: str, config: ServiceConfig, store: Store
) -> Job:
    def on_start() -> None:
        telegram_api.send_message(f"⏳ Running {symbol}…", chat_id, token=config.bot_token)

    def on_complete(result: RunResult, report_id: Optional[str]) -> None:
        text = _format_result(result, report_id, store.get_token(user_id), config)
        telegram_api.send_message(text, chat_id, token=config.bot_token)

    return Job(user_id=user_id, chat_id=chat_id, spec=spec, date=date, on_start=on_start, on_complete=on_complete)


def _format_result(
    result: RunResult, report_id: Optional[str], user_token: Optional[str], config: ServiceConfig
) -> str:
    if not result.ok:
        return f"⚠️ {result.ticker} failed: {result.error}"

    lines = [f"📈 {result.ticker} — {result.rating or 'N/A'} ({result.date})"]
    if result.rationale:
        lines.append(result.rationale)
    if report_id and user_token:
        lines.append(f"📄 Full report: {config.public_base_url}/report/{report_id}?token={user_token}")
    lines.append(f"({result.duration_seconds:.0f}s, preset {result.preset})")
    return "\n".join(lines)


def _parse_symbols(text: str) -> list[str]:
    """Extract 1-3 uppercased ticker symbols from free-form text."""
    candidates = re.split(r"[\s,]+", text.strip().upper())
    symbols = [c for c in candidates if _SYMBOL_RE.match(c)]
    return symbols[:MAX_SYMBOLS_PER_MESSAGE]


def _default_date() -> str:
    """Most recent trading day on or before today (YYYY-MM-DD)."""
    date = _dt.date.today()
    while not is_trading_day(date.isoformat()):
        date -= _dt.timedelta(days=1)
    return date.isoformat()


def _last_decision() -> Optional[str]:
    if not settings.DECISIONS_PATH.exists():
        return None
    lines = settings.DECISIONS_PATH.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    record = json.loads(lines[-1])
    return f"{record.get('ticker')} -> {record.get('rating') or 'N/A'} ({record.get('date')}), recorded {record.get('recorded_at')}"


def _tail_log() -> Optional[str]:
    log_path = settings.LOGS_DIR / f"run_{_dt.date.today().isoformat()}.log"
    if not log_path.exists():
        return None
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _help_text(config: ServiceConfig) -> str:
    command_lines = "\n".join(f"/{name} - {desc}" for name, desc in _COMMANDS if name != "status")
    return (
        "Send me 1-3 stock tickers (e.g. NVDA or NVDA AAPL) and I'll run an "
        "analysis and reply with a summary and a link to the full report.\n\n"
        f"You get {config.daily_cap} analyses per day.\n\n"
        "Commands:\n"
        f"{command_lines}\n\n"
        "Watchlist usage: /watchlist [list|add SYM...|remove SYM...|run]"
    )


def _reply(chat_id: str, text: str, config: ServiceConfig) -> None:
    telegram_api.send_message(text, chat_id, token=config.bot_token)


if __name__ == "__main__":
    cfg = ServiceConfig.from_env()
    store = Store(cfg.db_path)
    store.init_db()
    jobs = JobQueue(store, cfg.report_cache_ttl_seconds)
    run_bot(cfg, store, jobs)
