"""Tests for automation.bot's dispatch logic.

Uses fakes for telegram_api (no network) and a real Store/JobQueue against
a tmp_path SQLite file, matching the rest of the automation test suite's
dependency-injection style.
"""

from __future__ import annotations

from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from automation import bot
from automation.config import ServiceConfig
from automation.job_queue import JobQueue
from automation.store import Store


def _config(**overrides) -> ServiceConfig:
    defaults = dict(
        bot_token="test-token",
        invite_code="test-invite",
        public_base_url="https://example.invalid",
        reports_signing_key="test-signing-key",
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


class _RecordingTelegram:
    """Fake automation.telegram_api — records sent messages instead of
    hitting the network."""

    def __init__(self):
        self.sent: list[tuple[str, str, str | None]] = []

    def send_message(self, text, chat_id, *, token=None, parse_mode=None):
        self.sent.append((text, chat_id, parse_mode))
        return True

    def set_my_commands(self, commands, *, token=None):
        return True


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "service.db")
    store.init_db()
    return store


def test_second_ticker_message_within_the_throttle_window_is_rejected(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    config = _config()
    job_queue = JobQueue(store, cache_ttl_seconds=0)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    try:
        bot._handle_ticker_message("NVDA", 123, "chat-1", config, store, job_queue, rate_limiter)
        bot._handle_ticker_message("AAPL", 123, "chat-1", config, store, job_queue, rate_limiter)

        replies = [text for text, _chat, _mode in fake.sent]
        assert any("queued" in r.lower() for r in replies)
        assert any("wait" in r.lower() for r in replies)
        # Only the first message actually reached the queue.
        assert job_queue.qsize() == 1
    finally:
        store.close()


def test_throttle_is_scoped_per_user(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    store.unlock(456, "bob")
    config = _config()
    job_queue = JobQueue(store, cache_ttl_seconds=0)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    try:
        bot._handle_ticker_message("NVDA", 123, "chat-1", config, store, job_queue, rate_limiter)
        bot._handle_ticker_message("AAPL", 456, "chat-2", config, store, job_queue, rate_limiter)

        replies = [text for text, _chat, _mode in fake.sent]
        assert not any("wait" in r.lower() for r in replies)
        assert job_queue.qsize() == 2
    finally:
        store.close()
