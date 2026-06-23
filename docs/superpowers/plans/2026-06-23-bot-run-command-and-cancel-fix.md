# Bot /run command + cancel fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require an explicit `/run` command for ticker analyses (rejecting all other free-form text), and fix `/cancel` so it works whether the user's most recent job is queued or already running, refunding daily-cap usage in both cases.

**Architecture:** Three sequential tasks, each touching one production file plus its existing test file: (1) `Store.decrement_usage` for refunding the daily cap, (2) `JobQueue.cancel_for_user` replacing `cancel_last_for_user` with active-job + queued-job handling, (3) `bot.py` wiring — a new `/run` command, removal of the bare-ticker fallback, and an updated `/cancel` reply.

**Tech Stack:** Python, sqlite3 (stdlib), pytest, `threading.Event` for in-process cancellation signaling.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-bot-run-command-and-cancel-fix-design.md`.
- `/run TICKER [TICKER ...]` accepts 1-3 symbols, same parsing/limits as today (`_parse_symbols`, `MAX_SYMBOLS_PER_MESSAGE`).
- Bare ticker text with no command (e.g. `NVDA` alone) must be rejected with a message pointing at `/run` and `/help` — no implicit ticker parsing.
- `/watchlist run` is unchanged.
- Active-job cancellation never interrupts the running pipeline call — it only suppresses the eventual result message and refunds usage. No subprocess/kill mechanism.
- Queued-job cancellation keeps today's LIFO (most-recently-queued-first) behavior; `/cancel` cancels at most one job per invocation.
- Usage refund uses `Store.decrement_usage`, which must floor at 0 and no-op (not error) if there's no usage row for the day.
- Follow existing test patterns exactly: real `Store` against `tmp_path` SQLite, fakes only for `telegram_api`, no mocking of internals being tested.

---

### Task 1: `Store.decrement_usage`

**Files:**
- Modify: `automation/store.py` (add method after `usage_today`, around line 194)
- Test: `tests/test_store.py` (add test after `test_usage_cap_is_enforced_per_day`)

**Interfaces:**
- Produces: `Store.decrement_usage(self, user_id: int, *, today: str | None = None) -> None` — undoes one increment from `check_and_increment_usage` for the given day (default: today). Floors at 0. No-op if no usage row exists for that `(user_id, day)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`, after `test_usage_cap_is_enforced_per_day`:

```python
def test_decrement_usage_undoes_one_increment_and_floors_at_zero(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.check_and_increment_usage(123, 5, today="2026-06-15")
        store.check_and_increment_usage(123, 5, today="2026-06-15")
        store.decrement_usage(123, today="2026-06-15")
        assert store.usage_today(today="2026-06-15") == [(123, 1)]

        store.decrement_usage(123, today="2026-06-15")
        store.decrement_usage(123, today="2026-06-15")  # already at 0, stays at 0
        assert store.usage_today(today="2026-06-15") == [(123, 0)]

        store.decrement_usage(999, today="2026-06-15")  # no row for this user/day: no-op, no error
        assert store.usage_today(today="2026-06-15") == [(123, 0)]
    finally:
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd automation && python -m pytest ../tests/test_store.py::test_decrement_usage_undoes_one_increment_and_floors_at_zero -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'decrement_usage'`

- [ ] **Step 3: Implement `decrement_usage`**

In `automation/store.py`, add this method to the `Store` class, directly after `usage_today` (after line 194, before the `# -- reports --` comment):

```python
    def decrement_usage(self, user_id: int, *, today: str | None = None) -> None:
        """Undo one increment from check_and_increment_usage for today.

        Floors at 0 (never goes negative). No-op if there is no usage row
        for this (user_id, day) — e.g. nothing was ever charged.
        """
        day = today or _dt.date.today().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE usage SET count = MAX(count - 1, 0) WHERE user_id = ? AND day = ?",
                (user_id, day),
            )
            self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd automation && python -m pytest ../tests/test_store.py::test_decrement_usage_undoes_one_increment_and_floors_at_zero -v`
Expected: PASS

- [ ] **Step 5: Run the full store test file to confirm no regressions**

Run: `cd automation && python -m pytest ../tests/test_store.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add automation/store.py tests/test_store.py
git commit -m "feat(automation): add Store.decrement_usage for cancel refunds"
```

---

### Task 2: `JobQueue.cancel_for_user`

**Files:**
- Modify: `automation/job_queue.py`
- Test: `tests/test_job_queue.py`

**Interfaces:**
- Consumes: `Store.decrement_usage(user_id: int, *, today: str | None = None) -> None` (Task 1).
- Produces:
  - `Job` dataclass gains a `cancel_requested: threading.Event` field (default-constructed, mutable despite the frozen dataclass — only the attribute *reference* is frozen, not the `Event` object it points to).
  - `CancelResult` frozen dataclass: `symbol: str`, `was_active: bool`.
  - `JobQueue.cancel_for_user(self, user_id: int) -> Optional[CancelResult]` replaces `cancel_last_for_user`. Returns `None` if the user has neither a queued nor an active job.

This task replaces `cancel_last_for_user` entirely — there are no other callers in this codebase besides `bot.py` (updated in Task 3) and the existing test (updated below).

- [ ] **Step 1: Write the failing tests**

Open `tests/test_job_queue.py`. Replace the existing `test_cancel_last_for_user_removes_only_that_users_most_recent_queued_job` test (it calls a method this task removes) with the following, and add the two new tests after it. Add `import threading` to the existing imports at the top of the file (alongside `import time`).

Replace:
```python
def test_cancel_last_for_user_removes_only_that_users_most_recent_queued_job(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        queue = JobQueue(store, cache_ttl_seconds=0)
        # Block the worker on a long-running first job so subsequent submits stay queued.
        block_started = []

        def slow_on_start() -> None:
            block_started.append(True)
            time.sleep(2)

        blocker = Job(
            user_id=999,
            chat_id="chat",
            spec=TickerSpec(symbol="BLOCK", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=slow_on_start,
            on_complete=lambda result, report_id: None,
        )
        queue.submit(blocker)
        while not block_started:
            time.sleep(0.01)

        queue.submit(_make_job(123, "NVDA"))
        queue.submit(_make_job(123, "AAPL"))
        queue.submit(_make_job(456, "TSLA"))

        cancelled = queue.cancel_last_for_user(123)
        assert cancelled is not None
        assert cancelled.spec.symbol == "AAPL"

        assert queue.cancel_last_for_user(789) is None  # nothing queued for this user

        still_queued = queue.cancel_last_for_user(123)
        assert still_queued is not None
        assert still_queued.spec.symbol == "NVDA"
    finally:
        store.close()
```

With:
```python
def test_cancel_for_user_removes_only_that_users_most_recent_queued_job_and_refunds_usage(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        queue = JobQueue(store, cache_ttl_seconds=0)
        # Block the worker on a long-running first job so subsequent submits stay queued.
        block_started = []

        def slow_on_start() -> None:
            block_started.append(True)
            time.sleep(2)

        blocker = Job(
            user_id=999,
            chat_id="chat",
            spec=TickerSpec(symbol="BLOCK", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=slow_on_start,
            on_complete=lambda result, report_id: None,
        )
        queue.submit(blocker)
        while not block_started:
            time.sleep(0.01)

        store.check_and_increment_usage(123, 5)
        store.check_and_increment_usage(123, 5)
        store.check_and_increment_usage(456, 5)
        queue.submit(_make_job(123, "NVDA"))
        queue.submit(_make_job(123, "AAPL"))
        queue.submit(_make_job(456, "TSLA"))

        cancelled = queue.cancel_for_user(123)
        assert cancelled is not None
        assert cancelled.symbol == "AAPL"
        assert cancelled.was_active is False

        assert queue.cancel_for_user(789) is None  # nothing queued for this user

        still_queued = queue.cancel_for_user(123)
        assert still_queued is not None
        assert still_queued.symbol == "NVDA"
        assert still_queued.was_active is False

        usage = dict(store.usage_today())
        assert usage[123] == 0  # both of user 123's queued jobs were cancelled and refunded
        assert usage[456] == 1  # untouched
    finally:
        store.close()


def test_cancel_for_user_on_an_active_job_suppresses_the_result_and_refunds_usage(tmp_path, monkeypatch):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.check_and_increment_usage(123, 5)

        def _fake_run_one_ticker(spec, date):
            time.sleep(0.2)
            return RunResult(
                ticker=spec.symbol, date=date, preset=spec.preset,
                rating="Buy", rationale="ok", duration_seconds=0.2,
            )

        monkeypatch.setattr("automation.job_queue.run_one_ticker", _fake_run_one_ticker)
        monkeypatch.setattr("automation.job_queue.append_decision", lambda record: None)

        queue = JobQueue(store, cache_ttl_seconds=0)
        started = threading.Event()
        results = []
        job = Job(
            user_id=123,
            chat_id="chat",
            spec=TickerSpec(symbol="NVDA", preset="cost_saver", asset_type="stock"),
            date="2026-06-15",
            on_start=started.set,
            on_complete=lambda result, report_id: results.append((result, report_id)),
        )
        queue.submit(job)
        assert started.wait(timeout=5)

        cancelled = queue.cancel_for_user(123)
        assert cancelled is not None
        assert cancelled.symbol == "NVDA"
        assert cancelled.was_active is True

        deadline = time.monotonic() + 5
        while queue.qsize() > 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert results == []  # on_complete never called
        assert dict(store.usage_today())[123] == 0  # refunded
    finally:
        store.close()


def test_cancel_for_user_with_no_queued_or_active_job_returns_none(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        queue = JobQueue(store, cache_ttl_seconds=0)
        assert queue.cancel_for_user(123) is None
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd automation && python -m pytest ../tests/test_job_queue.py -v`
Expected: FAIL — `cancel_for_user` / `CancelResult` don't exist yet, and `Job(...)` calls still work (the new `cancel_requested` field will have a default once added in Step 3, so these tests fail on the missing method, not on `Job` construction).

- [ ] **Step 3: Implement `Job.cancel_requested`, `CancelResult`, and `cancel_for_user`**

In `automation/job_queue.py`, the top of the file already has `import threading` (used by `JobQueue.__init__`'s lock). Only change the dataclasses import line from:
```python
from dataclasses import dataclass
```
to:
```python
from dataclasses import dataclass, field
```

Update the `Job` dataclass (add one field at the end):
```python
@dataclass(frozen=True)
class Job:
    """One on-demand analysis request.

    ``on_start`` is called (with no arguments) when the worker picks the job
    up. ``on_complete`` is called with the finished ``RunResult`` and the
    ``report_id`` (``None`` if the run failed or rendering failed). Both
    callbacks are best-effort: exceptions are logged and never stop the
    worker. ``cancel_requested`` is set by ``JobQueue.cancel_for_user`` when
    this job is already active (no queue removal is possible at that
    point) — the worker checks it after the run completes to decide
    whether to suppress the result and refund usage instead of delivering
    it normally.
    """

    user_id: int
    chat_id: str
    spec: TickerSpec
    date: str
    on_start: Callable[[], None]
    on_complete: Callable[[RunResult, Optional[str]], None]
    cancel_requested: threading.Event = field(default_factory=threading.Event)
```

Add a `CancelResult` dataclass directly below `Job`:
```python
@dataclass(frozen=True)
class CancelResult:
    """Outcome of ``JobQueue.cancel_for_user``."""

    symbol: str
    was_active: bool
```

Replace `cancel_last_for_user` with:
```python
    def cancel_for_user(self, user_id: int) -> Optional[CancelResult]:
        """Cancel the calling user's most recent job, queued or active.

        If the job is still waiting in the queue, it is removed outright
        and its daily-cap usage charge is refunded immediately. If the job
        is the one currently executing, it cannot be removed or
        interrupted (there is no interruption point inside the pipeline
        call) — instead it is flagged so the worker suppresses the result
        message and refunds usage once the run finishes.

        Returns ``None`` if the user has neither a queued nor an active job.
        """
        with self._lock:
            if self._active_job is not None and self._active_job.user_id == user_id:
                self._active_job.cancel_requested.set()
                return CancelResult(symbol=self._active_job.spec.symbol, was_active=True)
            for i in range(len(self._jobs) - 1, -1, -1):
                if self._jobs[i].user_id == user_id:
                    job = self._jobs.pop(i)
                    self._store.decrement_usage(job.user_id)
                    return CancelResult(symbol=job.spec.symbol, was_active=False)
        return None
```

Now update `_process` to check cancellation after the (potentially long) work completes, before recording or notifying. Replace the whole `_process` method with:
```python
    def _process(self, job: Job) -> None:
        _safe_call(job.on_start)

        cached = self._store.get_cached_run(
            job.spec.symbol, job.date, job.spec.preset, self._cache_ttl_seconds
        )
        if cached and Path(cached.html_path).exists():
            result = RunResult(
                ticker=job.spec.symbol,
                date=job.date,
                preset=job.spec.preset,
                rating=cached.rating,
                rationale=cached.rationale,
                duration_seconds=0.0,
            )
            if job.cancel_requested.is_set():
                self._finish_cancelled(job, result)
                return
            report_id: Optional[str] = None
            try:
                report_id = self._store.add_report(
                    job.user_id, result.ticker, result.date, cached.html_path
                )
            except Exception:
                log.exception("Failed to record cached report for %s", result.ticker)
            age_minutes = _cache_age_minutes(cached.created_at)
            log.info("%s → cache hit (age %.0fm)", result.ticker, age_minutes)
            _safe_call(job.on_complete, result, report_id)
            return

        log.info("Running %s (%s, preset %s)…", job.spec.symbol, job.date, job.spec.preset)
        result = run_one_ticker(job.spec, job.date)

        if job.cancel_requested.is_set():
            self._finish_cancelled(job, result)
            return

        report_id = None
        if result.ok and result.report_dir:
            try:
                html_path = reports.render_to_html(Path(result.report_dir))
                self._store.upsert_cached_run(
                    job.spec.symbol,
                    job.date,
                    job.spec.preset,
                    result.rating,
                    result.rationale,
                    result.duration_seconds,
                    str(html_path),
                )
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

    def _finish_cancelled(self, job: Job, result: RunResult) -> None:
        """Refund usage and suppress the result for a job cancelled while active."""
        self._store.decrement_usage(job.user_id)
        log.info("%s → cancelled by user, suppressing result and refunding usage", result.ticker)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd automation && python -m pytest ../tests/test_job_queue.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add automation/job_queue.py tests/test_job_queue.py
git commit -m "fix(automation): cancel active jobs too, refunding usage in both cases"
```

---

### Task 3: `bot.py` — explicit `/run` command and updated `/cancel` reply

**Files:**
- Modify: `automation/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `JobQueue.cancel_for_user(user_id: int) -> Optional[CancelResult]` (Task 2), where `CancelResult` has `.symbol: str` and `.was_active: bool`.
- Produces: `_handle_run(text, user_id, chat_id, config, store, job_queue, rate_limiter) -> None` (renamed from `_handle_ticker_message`, same behavior — parses 1-3 tickers out of `text` via the existing `_parse_symbols`, which already ignores a leading `/run` token since it doesn't match the ticker regex).

- [ ] **Step 1: Write the failing tests**

In `tests/test_bot.py`, rename every call site of `bot._handle_ticker_message(...)` to `bot._handle_run(...)` (same arguments — `test_second_ticker_message_within_the_throttle_window_is_rejected` and `test_throttle_is_scoped_per_user`). No other change to those two tests.

Add these new tests after `test_history_reply_includes_a_freshly_signed_link_and_expiry`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd automation && python -m pytest ../tests/test_bot.py -v`
Expected: FAIL — `_handle_run` doesn't exist yet (`AttributeError`), `/run` isn't dispatched, bare tickers still get parsed, `_handle_cancel` doesn't use `CancelResult`.

- [ ] **Step 3: Implement the `/run` command and rejection fallback**

In `automation/bot.py`:

Rename the function `_handle_ticker_message` to `_handle_run` (signature and body unchanged — `_parse_symbols` already strips out a leading `/run` token since it fails the `_SYMBOL_RE` match, so no parsing logic changes are needed):

```python
def _handle_run(
    text: str,
    user_id: int,
    chat_id: str,
    config: ServiceConfig,
    store: Store,
    job_queue: JobQueue,
    rate_limiter,
) -> None:
    if not store.is_allowed(user_id):
        _reply(chat_id, "You need an invite code first. Send /start <code> to get started.", config)
        return

    symbols = _parse_symbols(text)
    if not symbols:
        _reply(
            chat_id,
            "I couldn't find a ticker after /run. Usage: /run NVDA [AAPL ...]\n\n" + _help_text(config),
            config,
        )
        return

    _enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue, rate_limiter)
```

Update `_handle_update`'s dispatch chain: add a `/run` branch before the final `else`, and replace the bare-ticker fallback with a rejection reply:

```python
    elif text == "/watchlist" or text.startswith("/watchlist "):
        _handle_watchlist(text, user_id, chat_id, config, store, job_queue, rate_limiter)
    elif text == "/run" or text.startswith("/run "):
        _handle_run(text, user_id, chat_id, config, store, job_queue, rate_limiter)
    else:
        _reply(
            chat_id,
            "I only respond to commands. Send /run TICKER to analyze a stock, or /help for the full list.",
            config,
        )
```

Update `_COMMANDS` to register `/run` (insert right after `("cancel", ...)`):

```python
_COMMANDS: list[tuple[str, str]] = [
    ("start", "Unlock access with an invite code"),
    ("help", "Show usage instructions"),
    ("run", "Run an analysis for one or more tickers"),
    ("cancel", "Cancel your most recently queued analysis"),
    ("history", "Show your recent analyses"),
    ("watchlist", "Manage your personal ticker watchlist"),
    ("invite", "Admin only: generate a new invite code"),
    ("status", "Admin only: queue/usage/log snapshot"),
]
```

Update `_help_text`'s opening line to reflect the new required command:

```python
def _help_text(config: ServiceConfig) -> str:
    command_lines = "\n".join(
        f"/{name} - {desc}" for name, desc in _COMMANDS if name not in ("status", "invite")
    )
    return (
        "Send /run TICKER [TICKER ...] (up to 3, e.g. /run NVDA or /run NVDA AAPL) "
        "and I'll run an analysis and reply with a summary and a link to the full report.\n\n"
        f"You get {config.daily_cap} analyses per day.\n\n"
        "Commands:\n"
        f"{command_lines}\n\n"
        "Watchlist usage: /watchlist [list|add SYM...|remove SYM...|run]"
    )
```

Update the welcome message in `_handle_start` to match:

```python
        _reply(
            chat_id,
            "✅ You're in! Send /run NVDA to run an analysis.\n\n" + _help_text(config),
            config,
        )
```

- [ ] **Step 4: Implement the updated `/cancel` reply**

Replace `_handle_cancel`:

```python
def _handle_cancel(user_id: int, chat_id: str, job_queue: JobQueue, config: ServiceConfig) -> None:
    cancelled = job_queue.cancel_for_user(user_id)
    if cancelled is None:
        _reply(chat_id, "You don't have anything queued to cancel.", config)
    elif cancelled.was_active:
        _reply(
            chat_id,
            f"⏳ {html.escape(cancelled.symbol)} is already running and can't be killed early, "
            "but you won't be charged for it and won't get a result message.",
            config,
        )
    else:
        _reply(chat_id, f"❌ Cancelled {html.escape(cancelled.symbol)} (removed from queue).", config)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd automation && python -m pytest ../tests/test_bot.py -v`
Expected: all PASS

- [ ] **Step 6: Run the full automation test suite to confirm no regressions**

Run: `cd automation && python -m pytest ../tests/test_bot.py ../tests/test_job_queue.py ../tests/test_store.py ../tests/test_web_server.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add automation/bot.py tests/test_bot.py
git commit -m "feat(automation): require explicit /run command, reject free-form ticker text"
```
