"""Thin Telegram Bot API client.

Generalizes the raw-``requests`` pattern used by ``notify_telegram`` so the
on-demand bot (``automation/bot.py``) can both send messages and long-poll
for updates. Fail-soft on send (never raises); long-polling raises on
network errors so the bot loop can log and retry.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from automation.runlog import get_logger

log = get_logger(__name__)

TELEGRAM_MAX_CHARS = 4096
GET_UPDATES_TIMEOUT_PADDING = 5


def send_message(
    text: str,
    chat_id: str,
    *,
    token: Optional[str] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    """Send a message to a Telegram chat.

    Splits text at newline boundaries if it exceeds 4096 chars. Returns True
    only if all chunks are sent successfully (HTTP 200 with "ok": true).

    Fail-soft: returns False and logs a warning on missing token, network
    errors, or API errors. Never raises.

    Args:
        text: Message body.
        chat_id: Target chat id (string or numeric id as a string).
        token: Bot token; defaults to the ``TELEGRAM_BOT_TOKEN`` env var.
        parse_mode: Optional Telegram parse mode (e.g. "Markdown", "HTML").

    Returns:
        True if all chunks sent successfully; False otherwise.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("Telegram send skipped: no bot token configured")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for chunk in _split_message(text, TELEGRAM_MAX_CHARS):
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = requests.post(url, json=payload, timeout=15)
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


def set_my_commands(
    commands: list[tuple[str, str]],
    *,
    token: Optional[str] = None,
) -> bool:
    """Register the bot's command menu with Telegram (``setMyCommands``).

    ``commands`` is a list of ``(name, description)`` pairs (name without
    the leading ``/``). Fail-soft like :func:`send_message`: logs and
    returns False on any error, never raises.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("setMyCommands skipped: no bot token configured")
        return False

    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    payload = {"commands": [{"command": name, "description": desc} for name, desc in commands]}

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            log.warning(
                "setMyCommands returned %d: %s", response.status_code, response.text[:200]
            )
            return False
        data = response.json()
        if not data.get("ok"):
            log.warning("setMyCommands returned ok=false: %s", data.get("description"))
            return False
    except requests.RequestException as exc:
        log.warning("setMyCommands request failed: %s", exc)
        return False

    return True


def get_updates(
    offset: int,
    timeout: int,
    *,
    token: Optional[str] = None,
) -> list[dict]:
    """Long-poll Telegram for new updates.

    Args:
        offset: Only return updates with ``update_id`` >= offset (Telegram's
            ``getUpdates`` semantics — pass ``last_update_id + 1``).
        timeout: Long-poll timeout in seconds, passed to Telegram.
        token: Bot token; defaults to the ``TELEGRAM_BOT_TOKEN`` env var.

    Returns:
        The ``result`` list from Telegram's response (possibly empty).

    Raises:
        RuntimeError: if no bot token is configured, or Telegram returns
            ``ok: false``.
        requests.RequestException: on network errors (caller decides how to
            retry/back off).
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"offset": offset, "timeout": timeout}
    response = requests.get(
        url, params=params, timeout=timeout + GET_UPDATES_TIMEOUT_PADDING
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates returned ok=false: {data.get('description')}")
    return data.get("result", [])


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
        if len(line) > max_chars:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
                current_chunk = ""
            chunks.extend(_split_long_line(line, max_chars))
        elif len(current_chunk) + len(line) + 1 <= max_chars:
            current_chunk += line + "\n"
        else:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
            current_chunk = line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks


def _split_long_line(line: str, max_chars: int) -> list[str]:
    """Split one line into hard chunks that fit Telegram's size limit."""
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]
