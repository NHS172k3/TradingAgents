"""Send notifications to Telegram.

Provides a fail-soft send_message function that posts to Telegram's bot API.
Respects message length limits by chunking long messages at newlines.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

# Load .env from repo root if python-dotenv is available
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

import requests

from automation.runlog import get_logger

log = get_logger(__name__)

TELEGRAM_MAX_CHARS = 4096


def send_message(text: str) -> bool:
    """Send a message to Telegram.

    Splits text at newline boundaries if it exceeds 4096 chars. Returns True
    only if all chunks are sent successfully (HTTP 200 with "ok": true).

    Fail-soft: returns False and logs a warning if env vars are missing,
    network errors occur, or the API returns an error. Never raises.

    Args:
        text: Message body (plain text).

    Returns:
        True if all chunks sent successfully; False otherwise.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.warning(
            "Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID "
            "not set"
        )
        return False

    # Split text into chunks if needed
    chunks = _split_message(text, TELEGRAM_MAX_CHARS)

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for chunk in chunks:
        try:
            response = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )
            if response.status_code != 200:
                log.warning(
                    "Telegram API returned %d: %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            data = response.json()
            if not data.get("ok"):
                log.warning("Telegram API returned ok=false: %s", data.get("description"))
                return False
        except requests.RequestException as exc:
            log.warning("Telegram request failed: %s", exc)
            return False

    return True


def _split_message(text: str, max_chars: int) -> list[str]:
    """Split a message at newline boundaries if it exceeds max_chars.

    Args:
        text: The message to split.
        max_chars: Maximum characters per chunk.

    Returns:
        A list of chunks, each <= max_chars.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = ""

    for line in lines:
        # If a single line exceeds max_chars, it must be sent as-is
        if len(line) > max_chars:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
                current_chunk = ""
            chunks.append(line)
        elif len(current_chunk) + len(line) + 1 <= max_chars:
            # Append line with newline
            current_chunk += line + "\n"
        else:
            # Flush current chunk and start a new one
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
            current_chunk = line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks


if __name__ == "__main__":
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    test_msg = f"TradingAgents test notification ✅ {now}"
    success = send_message(test_msg)
    print(f"Telegram notification: {'SUCCESS' if success else 'FAILED'}")
    sys.exit(0 if success else 1)
