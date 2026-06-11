"""Weekly Gmail digest summarizing watchlist decisions and resolved outcomes.

    python -m automation.weekly_email [--dry-run] [--days N]

Builds a plaintext + HTML digest covering:
  - Decisions recorded in ``automation/logs/decisions.jsonl`` over the last
    ``--days`` days (default 7).
  - Memory-log entries (from :func:`automation.upstream.read_memory_entries`)
    that have resolved (non-pending) within the last 30 days.

``--dry-run`` prints the plaintext digest and sends nothing. Otherwise the
digest is emailed via Gmail SMTP using GMAIL_ADDRESS / GMAIL_APP_PASSWORD.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import smtplib
import sys
from email.message import EmailMessage

# Load .env from repo root if python-dotenv is available
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from automation import settings
from automation.runlog import get_logger
from automation.upstream import read_memory_entries

log = get_logger(__name__)

# Memory-log entries have no "resolved at" timestamp, only the original
# decision date. A 30-day decision-date window is used as an approximation
# of "recently resolved" outcomes.
RESOLVED_WINDOW_DAYS = 30
REFLECTION_EXCERPT_CHARS = 200


def _read_decisions(days: int, *, now: _dt.datetime) -> list[dict]:
    """Read decisions.jsonl, tolerantly, filtered to the last `days` days.

    Skips unparseable lines; missing file returns an empty list.
    """
    path = settings.DECISIONS_PATH
    if not path.exists():
        return []

    cutoff = now - _dt.timedelta(days=days)
    decisions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        recorded_at = record.get("recorded_at")
        try:
            recorded = _dt.datetime.fromisoformat(recorded_at)
        except (TypeError, ValueError):
            continue
        if recorded >= cutoff:
            decisions.append(record)
    return decisions


def _read_resolved_outcomes(*, today: _dt.date) -> list[dict]:
    """Non-pending memory-log entries whose decision date is recent.

    See RESOLVED_WINDOW_DAYS for why this is an approximation.
    """
    cutoff = today - _dt.timedelta(days=RESOLVED_WINDOW_DAYS)
    resolved = []
    for entry in read_memory_entries():
        if entry.get("pending"):
            continue
        try:
            entry_date = _dt.date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            continue
        if entry_date >= cutoff:
            resolved.append(entry)
    return resolved


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_digest(days: int = 7) -> tuple[str, str]:
    """Build the weekly digest as (plaintext, html).

    Args:
        days: Size of the "decisions this week" window, in days.

    Returns:
        A (plaintext, html) tuple. Both render sensibly with zero data.
    """
    now = _dt.datetime.now()
    today = now.date()
    start_date = (now - _dt.timedelta(days=days)).date()

    decisions = sorted(
        _read_decisions(days, now=now),
        key=lambda r: r.get("recorded_at", ""),
    )
    resolved = sorted(
        _read_resolved_outcomes(today=today),
        key=lambda e: e.get("date", ""),
    )

    date_range = f"{start_date.isoformat()} to {today.isoformat()}"

    # ---- Plaintext ----
    lines = [
        f"TradingAgents weekly digest — {date_range}",
        f"{len(decisions)} decision(s) this week, {len(resolved)} resolved outcome(s) in "
        f"the last {RESOLVED_WINDOW_DAYS} days.",
        "",
        "Decisions this week",
        "--------------------",
    ]
    if decisions:
        for record in decisions:
            ticker = record.get("ticker", "?")
            date = record.get("date", "?")
            preset = record.get("preset", "?")
            error = record.get("error")
            rating = f"FAILED ({error})" if error else record.get("rating", "?")
            lines.append(f"- {ticker} {date}: {rating} (preset {preset})")
    else:
        lines.append("No decisions recorded this week.")

    lines += [
        "",
        "Resolved outcomes",
        "-----------------",
    ]
    if resolved:
        for entry in resolved:
            ticker = entry.get("ticker", "?")
            date = entry.get("date", "?")
            rating = entry.get("rating", "?")
            raw = entry.get("raw") or "?"
            alpha = entry.get("alpha") or "?"
            holding = entry.get("holding") or "?"
            reflection = (entry.get("reflection") or "")[:REFLECTION_EXCERPT_CHARS]
            lines.append(
                f"- {ticker} {date}: {rating}, return {raw}, alpha {alpha}, "
                f"held {holding}"
            )
            if reflection:
                lines.append(f"    {reflection}")
    else:
        lines.append("No resolved outcomes in the last 30 days.")

    plaintext = "\n".join(lines) + "\n"

    # ---- HTML ----
    table_style = (
        'style="border-collapse: collapse; width: 100%; '
        'font-family: Arial, sans-serif; font-size: 13px;"'
    )
    th_style = (
        'style="text-align: left; border-bottom: 2px solid #ccc; '
        'padding: 4px 8px;"'
    )
    td_style = 'style="border-bottom: 1px solid #eee; padding: 4px 8px;"'

    html_parts = [
        '<html><body style="font-family: Arial, sans-serif;">',
        f"<h2>TradingAgents weekly digest — {_escape_html(date_range)}</h2>",
        f"<p>{len(decisions)} decision(s) this week, {len(resolved)} resolved "
        f"outcome(s) in the last {RESOLVED_WINDOW_DAYS} days.</p>",
        "<h3>Decisions this week</h3>",
    ]
    if decisions:
        html_parts.append(f"<table {table_style}>")
        html_parts.append(
            f"<tr><th {th_style}>Ticker</th><th {th_style}>Date</th>"
            f"<th {th_style}>Rating</th><th {th_style}>Preset</th></tr>"
        )
        for record in decisions:
            ticker = _escape_html(str(record.get("ticker", "?")))
            date = _escape_html(str(record.get("date", "?")))
            preset = _escape_html(str(record.get("preset", "?")))
            error = record.get("error")
            if error:
                rating = f'<span style="color: #c0392b;">FAILED ({_escape_html(str(error))})</span>'
            else:
                rating = _escape_html(str(record.get("rating", "?")))
            html_parts.append(
                f"<tr><td {td_style}>{ticker}</td><td {td_style}>{date}</td>"
                f"<td {td_style}>{rating}</td><td {td_style}>{preset}</td></tr>"
            )
        html_parts.append("</table>")
    else:
        html_parts.append("<p>No decisions recorded this week.</p>")

    html_parts.append("<h3>Resolved outcomes</h3>")
    if resolved:
        html_parts.append(f"<table {table_style}>")
        html_parts.append(
            f"<tr><th {th_style}>Ticker</th><th {th_style}>Date</th>"
            f"<th {th_style}>Rating</th><th {th_style}>Return</th>"
            f"<th {th_style}>Alpha</th><th {th_style}>Held</th>"
            f"<th {th_style}>Reflection</th></tr>"
        )
        for entry in resolved:
            ticker = _escape_html(str(entry.get("ticker", "?")))
            date = _escape_html(str(entry.get("date", "?")))
            rating = _escape_html(str(entry.get("rating", "?")))
            raw = _escape_html(str(entry.get("raw") or "?"))
            alpha = _escape_html(str(entry.get("alpha") or "?"))
            holding = _escape_html(str(entry.get("holding") or "?"))
            reflection = _escape_html(
                (entry.get("reflection") or "")[:REFLECTION_EXCERPT_CHARS]
            )
            html_parts.append(
                f"<tr><td {td_style}>{ticker}</td><td {td_style}>{date}</td>"
                f"<td {td_style}>{rating}</td><td {td_style}>{raw}</td>"
                f"<td {td_style}>{alpha}</td><td {td_style}>{holding}</td>"
                f"<td {td_style}>{reflection}</td></tr>"
            )
        html_parts.append("</table>")
    else:
        html_parts.append(f"<p>No resolved outcomes in the last {RESOLVED_WINDOW_DAYS} days.</p>")

    html_parts.append("</body></html>")
    html = "\n".join(html_parts)

    return plaintext, html


def send_email(subject: str, plaintext: str, html: str, to_addr: str) -> None:
    """Send a multipart (plaintext + HTML) email via Gmail SMTP.

    Reads GMAIL_ADDRESS / GMAIL_APP_PASSWORD from the environment. Raises on
    any failure (missing credentials, SMTP errors); the caller handles it.
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_addr
    msg.set_content(plaintext)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_password)
        smtp.send_message(msg)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send the TradingAgents weekly email digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest instead of sending email")
    parser.add_argument("--days", type=int, default=7,
                        help="size of the 'decisions this week' window (default: 7)")
    args = parser.parse_args(argv)

    plaintext, html = build_digest(args.days)

    if args.dry_run:
        print(plaintext)
        return 0

    try:
        watchlist = settings.load_watchlist()
    except settings.WatchlistError as exc:
        log.error("Config error: %s", exc)
        return 1

    if not watchlist.email_enabled:
        log.info("Email disabled in watchlist.yaml — nothing to send")
        return 0

    to_addr = watchlist.email_to or os.environ.get("GMAIL_ADDRESS")
    if not to_addr:
        log.error("No recipient configured (set email.to in watchlist.yaml or GMAIL_ADDRESS)")
        return 1

    today = _dt.date.today()
    start_date = today - _dt.timedelta(days=args.days)
    subject = f"TradingAgents weekly digest — {start_date.isoformat()} to {today.isoformat()}"

    try:
        send_email(subject, plaintext, html, to_addr)
    except KeyError as exc:
        log.error("Missing environment variable: %s", exc)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Failed to send weekly digest email: %s", exc)
        return 1

    log.info("Weekly digest sent to %s", to_addr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
