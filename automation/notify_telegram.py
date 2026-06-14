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

from automation.runlog import get_logger
from automation.telegram_api import send_message as _api_send_message

log = get_logger(__name__)


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

    return _api_send_message(text, chat_id, token=token)


if __name__ == "__main__":
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    test_msg = f"TradingAgents test notification ✅ {now}"
    success = send_message(test_msg)
    print(f"Telegram notification: {'SUCCESS' if success else 'FAILED'}")
    sys.exit(0 if success else 1)
