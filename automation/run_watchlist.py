"""Daily watchlist entry point.

    python -m automation.run_watchlist [--dry-run] [--ticker X] [--date YYYY-MM-DD]
                                       [--preset NAME] [--force]

Runs every watchlist ticker through the graph, sends a Telegram message per
decision plus a run summary, and appends one line per ticker to
automation/logs/decisions.jsonl. Exit codes: 0 all ok (or non-trading day),
1 at least one ticker failed, 2 config/lock error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

from automation import settings
from automation.calendar_check import is_trading_day
from automation.runlog import get_logger
from automation.settings import TickerSpec, Watchlist, WatchlistError

LOCK_STALE_SECONDS = 6 * 3600

log = get_logger(__name__)


def acquire_lock() -> bool:
    settings.LOCKFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if settings.LOCKFILE_PATH.exists():
        age = time.time() - settings.LOCKFILE_PATH.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            return False
        log.warning("Removing stale lockfile (age %.0f min)", age / 60)
    settings.LOCKFILE_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    settings.LOCKFILE_PATH.unlink(missing_ok=True)


def append_decision(record: dict) -> None:
    settings.DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"recorded_at": _dt.datetime.now().isoformat(timespec="seconds"), **record}
    with open(settings.DECISIONS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def notify(watchlist: Watchlist, text: str, *, is_failure: bool = False) -> None:
    if not watchlist.telegram_enabled:
        return
    if is_failure and not watchlist.telegram_notify_on_failure:
        return
    from automation.notify_telegram import send_message
    if not send_message(text):
        log.warning("Telegram notification failed (run continues)")


def format_result_message(result) -> str:
    if result.ok:
        return (
            f"📈 {result.ticker} — {result.rating} ({result.date})\n\n"
            f"{result.rationale}\n\n"
            f"Report: {result.report_dir}\n"
            f"({result.duration_seconds:.0f}s, preset {result.preset})"
        )
    return f"⚠️ {result.ticker} failed ({result.date}): {result.error}"


def select_tickers(watchlist: Watchlist, args) -> list[TickerSpec]:
    tickers = watchlist.tickers
    if args.ticker:
        symbol = args.ticker.strip().upper()
        match = next((t for t in tickers if t.symbol == symbol), None)
        tickers = [match or TickerSpec(symbol=symbol, preset=watchlist.preset)]
    if args.preset:
        tickers = [TickerSpec(t.symbol, args.preset, t.asset_type) for t in tickers]
    return tickers


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run TradingAgents over the watchlist")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate config and trading-day logic; no LLM calls")
    parser.add_argument("--ticker", help="run a single ticker (added if not in watchlist)")
    parser.add_argument("--date", default=_dt.date.today().isoformat(),
                        help="analysis date YYYY-MM-DD (default: today)")
    parser.add_argument("--preset", choices=sorted(settings.PRESETS),
                        help="override preset for all selected tickers")
    parser.add_argument("--force", action="store_true",
                        help="run even on weekends/holidays")
    args = parser.parse_args(argv)

    try:
        _dt.date.fromisoformat(args.date)
        watchlist = settings.load_watchlist()
    except (WatchlistError, ValueError) as exc:
        log.error("Config error: %s", exc)
        return 2

    tickers = select_tickers(watchlist, args)

    trading_day = is_trading_day(args.date, skip_dates=watchlist.skip_dates)
    if not trading_day and not args.force:
        log.info("%s is not a trading day — nothing to do (use --force to override)",
                 args.date)
        return 0

    if args.dry_run:
        log.info("Dry run OK: %s on %s (trading day: %s)",
                 ", ".join(f"{t.symbol}[{t.preset}]" for t in tickers),
                 args.date, trading_day)
        return 0

    if not acquire_lock():
        log.error("Another run appears to be in progress (lockfile %s)",
                  settings.LOCKFILE_PATH)
        return 2

    from automation.runner import run_one_ticker

    results = []
    consecutive_failures = 0
    try:
        for spec in tickers:
            log.info("Running %s (%s, preset %s)…", spec.symbol, args.date, spec.preset)
            result = run_one_ticker(spec, args.date)
            results.append(result)
            append_decision(result.to_record())
            if result.ok:
                consecutive_failures = 0
                log.info("%s → %s (%.0fs)", spec.symbol, result.rating,
                         result.duration_seconds)
            else:
                consecutive_failures += 1
                log.error("%s failed: %s", spec.symbol, result.error)
            notify(watchlist, format_result_message(result), is_failure=not result.ok)

            if not result.ok and not watchlist.continue_on_error:
                log.error("continue_on_error=false — stopping")
                break
            if consecutive_failures >= watchlist.max_consecutive_failures:
                log.error("%d consecutive failures — aborting run",
                          consecutive_failures)
                break
    finally:
        release_lock()

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    summary = f"Watchlist run {args.date}: {len(ok)} ok, {len(failed)} failed"
    if failed:
        summary += "\n" + "\n".join(f"• {r.ticker} — {r.error}" for r in failed)
    if ok:
        summary += "\n" + "\n".join(f"• {r.ticker} — {r.rating}" for r in ok)
    log.info("%s", summary.replace("\n", " | "))
    if len(results) > 1:
        notify(watchlist, summary, is_failure=bool(failed))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
