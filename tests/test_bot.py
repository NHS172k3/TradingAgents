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
    defaults = {
        "bot_token": "test-token",
        "invite_code": "test-invite",
        "public_base_url": "https://example.invalid",
        "reports_signing_key": "test-signing-key",
    }
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
        bot._handle_run("NVDA", 123, "chat-1", config, store, job_queue, rate_limiter)
        bot._handle_run("AAPL", 123, "chat-1", config, store, job_queue, rate_limiter)

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
        bot._handle_run("NVDA", 123, "chat-1", config, store, job_queue, rate_limiter)
        bot._handle_run("AAPL", 456, "chat-2", config, store, job_queue, rate_limiter)

        replies = [text for text, _chat, _mode in fake.sent]
        assert not any("wait" in r.lower() for r in replies)
        assert job_queue.qsize() == 2
    finally:
        store.close()


def test_invite_command_is_admin_only(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config(admin_user_id=999)
    try:
        bot._handle_invite("/invite", 123, "chat-1", config, store)

        replies = [text for text, _chat, _mode in fake.sent]
        assert any("restricted" in r.lower() for r in replies)
    finally:
        store.close()


def test_invite_command_generates_a_code_admin_can_share(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config(admin_user_id=999)
    try:
        bot._handle_invite("/invite 2 48", 999, "chat-1", config, store)

        replies = [text for text, _chat, _mode in fake.sent]
        assert len(replies) == 1
        assert "invite code" in replies[0].lower()
    finally:
        store.close()


def test_start_unlocks_with_a_db_issued_invite_code(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config()
    code, _expires_at = store.create_invite(max_uses=1, ttl_hours=1)
    try:
        bot._handle_start(f"/start {code}", 123, "alice", "chat-1", config, store)

        assert store.is_allowed(123)
        replies = [text for text, _chat, _mode in fake.sent]
        assert any("you're in" in r.lower() for r in replies)
    finally:
        store.close()


def test_start_still_accepts_the_bootstrap_env_invite_code(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config(invite_code="bootstrap-secret")
    try:
        bot._handle_start("/start bootstrap-secret", 999, "owner", "chat-1", config, store)

        assert store.is_allowed(999)
    finally:
        store.close()


def test_start_rejects_an_unknown_code(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config()
    try:
        bot._handle_start("/start nope", 123, "alice", "chat-1", config, store)

        assert not store.is_allowed(123)
        replies = [text for text, _chat, _mode in fake.sent]
        assert any("isn't valid" in r.lower() for r in replies)
    finally:
        store.close()


def test_format_result_escapes_html_and_shows_expiry(tmp_path):
    from automation.runner import RunResult
    from automation.tokens import report_token_expiry_date

    config = _config()
    result = RunResult(
        ticker="NVDA",
        date="2026-06-15",
        preset="cost_saver",
        rating="Buy",
        rationale="Strong <fake-tag> momentum & upside",
        duration_seconds=12.0,
    )

    text = bot._format_result(result, "report-abc", 123, config)

    assert "<b>NVDA</b>" in text
    assert "&lt;fake-tag&gt;" in text  # escaped, not rendered as a tag
    assert "&amp;" in text
    assert f"expires {report_token_expiry_date()}" in text
    assert 'href="https://example.invalid/report/report-abc?token=' in text


def test_format_result_omits_link_section_when_no_report_id(tmp_path):
    from automation.runner import RunResult

    config = _config()
    result = RunResult(
        ticker="NVDA", date="2026-06-15", preset="cost_saver",
        rating="Buy", rationale="ok", duration_seconds=1.0,
    )

    text = bot._format_result(result, None, 123, config)

    assert "Full report" not in text
    assert "expires" not in text


def test_history_reply_includes_a_freshly_signed_link_and_expiry(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    config = _config()
    store.add_report(123, "NVDA", "2026-06-15", "/tmp/r.html")
    try:
        bot._handle_history("/history", 123, "chat-1", store, config)

        text, _chat, mode = fake.sent[-1]
        assert mode == "HTML"
        assert "<b>NVDA</b>" in text
        assert "token=" in text
        assert "expires" in text
    finally:
        store.close()


def test_run_command_enqueues_a_ticker(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    config = _config()
    job_queue = JobQueue(store, cache_ttl_seconds=0)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    try:
        bot._handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "chat-1"},
                    "from": {"id": 123, "username": "alice"},
                    "text": "/run NVDA",
                },
            },
            config,
            store,
            job_queue,
            rate_limiter,
        )

        replies = [text for text, _chat, _mode in fake.sent]
        assert any("queued" in r.lower() for r in replies)
        assert job_queue.qsize() == 1
    finally:
        store.close()


def test_bare_ticker_with_no_command_is_rejected(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    config = _config()
    job_queue = JobQueue(store, cache_ttl_seconds=0)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    try:
        bot._handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "chat-1"},
                    "from": {"id": 123, "username": "alice"},
                    "text": "NVDA",
                },
            },
            config,
            store,
            job_queue,
            rate_limiter,
        )

        replies = [text for text, _chat, _mode in fake.sent]
        assert any("/run" in r for r in replies)
        assert job_queue.qsize() == 0
    finally:
        store.close()


def test_unrecognized_slash_command_is_rejected(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    store.unlock(123, "alice")
    config = _config()
    job_queue = JobQueue(store, cache_ttl_seconds=0)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    try:
        bot._handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "chat-1"},
                    "from": {"id": 123, "username": "alice"},
                    "text": "/foo",
                },
            },
            config,
            store,
            job_queue,
            rate_limiter,
        )

        replies = [text for text, _chat, _mode in fake.sent]
        assert any("/run" in r for r in replies)
        assert job_queue.qsize() == 0
    finally:
        store.close()


def test_cancel_on_an_active_job_replies_that_it_cannot_be_killed_early(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config()

    class _FakeQueue:
        def cancel_for_user(self, user_id):
            from automation.job_queue import CancelResult
            return CancelResult(symbol="NVDA", was_active=True)

    try:
        bot._handle_cancel(123, "chat-1", _FakeQueue(), config)

        replies = [text for text, _chat, _mode in fake.sent]
        assert len(replies) == 1
        assert "nvda" in replies[0].lower()
        assert "can't be killed" in replies[0].lower() or "cannot be killed" in replies[0].lower()
    finally:
        store.close()


def test_cancel_on_a_queued_job_replies_that_it_was_removed(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config()

    class _FakeQueue:
        def cancel_for_user(self, user_id):
            from automation.job_queue import CancelResult
            return CancelResult(symbol="NVDA", was_active=False)

    try:
        bot._handle_cancel(123, "chat-1", _FakeQueue(), config)

        replies = [text for text, _chat, _mode in fake.sent]
        assert len(replies) == 1
        assert "cancelled nvda" in replies[0].lower()
    finally:
        store.close()


def test_cancel_with_nothing_to_cancel_replies_accordingly(tmp_path, monkeypatch):
    fake = _RecordingTelegram()
    monkeypatch.setattr(bot, "telegram_api", fake)
    store = _store(tmp_path)
    config = _config()

    class _FakeQueue:
        def cancel_for_user(self, user_id):
            return None

    try:
        bot._handle_cancel(123, "chat-1", _FakeQueue(), config)

        replies = [text for text, _chat, _mode in fake.sent]
        assert len(replies) == 1
        assert "nothing queued" in replies[0].lower()
    finally:
        store.close()
