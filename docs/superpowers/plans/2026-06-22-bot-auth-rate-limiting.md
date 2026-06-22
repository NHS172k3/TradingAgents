# Bot Auth + Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stored per-user bearer token with stateless signed report-link tokens, add a DB-backed expiring/capped-use invite system, add rate limiting on both the web report endpoint and bot ticker submissions, lock down the SQLite file's permissions, and switch bot replies to properly rendered (HTML) Telegram messages with visible link-expiry text.

**Architecture:** No new services. Three new small pieces slot into the existing `automation/` layer: `automation/tokens.py` (stateless HMAC-signed report tokens via `itsdangerous`), an `invites` table in the existing `store.py` SQLite file, and a `limits`-backed in-memory rate limiter instance threaded through `bot.py` the same way `store`/`job_queue` already are (constructed once in `service.py`, passed as a parameter — no module-level mutable state). `web/server.py` gains `slowapi` for HTTP-level rate limiting. Everything else (job queue, report rendering, scheduled-run stack) is untouched.

**Tech Stack:** `itsdangerous` (signed tokens), `slowapi` + `limits` (rate limiting), existing `sqlite3`/`FastAPI`/`python-telegram` HTTP client already in the repo.

## Global Constraints

- Report-link tokens expire 7 days after signing (`REPORT_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 3600` in `automation/tokens.py`).
- Web rate limit: 30 requests/minute per client key on `GET /report/{id}` (`slowapi`).
- Bot rate limit: 1 ticker-analysis submission per 10 seconds per `user_id` (`limits`, `RateLimitItemPerSecond(1, 10)`).
- Default invite issued via `/invite` with no args: `max_uses=1`, `ttl_hours=72`.
- All bot replies use Telegram `parse_mode="HTML"`; every piece of dynamic/untrusted text (ticker, date, LLM rationale, invite code, error text) is passed through `html.escape()` before interpolation; every `<b>`/`<i>`/`<a>` tag opens and closes on the same line (so `telegram_api._split_message`'s line-based chunking can never bisect a tag).
- `users.access_token` column stays in the `users` table schema (still `NOT NULL UNIQUE`, still populated with a random throwaway value on insert) but is never read again anywhere in the codebase after this plan — no destructive `ALTER TABLE`, since there's no migration framework yet (a separate, not-yet-planned concern).
- Every task that touches a file with existing tests must leave `pytest -q` green before moving to the next task.
- Run tests with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` set (this repo's venv has a ROS plugin that pollutes pytest otherwise): `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q <path>`.

---

### Task 1: `automation/tokens.py` — stateless signed report tokens

**Files:**
- Create: `automation/tokens.py`
- Modify: `automation/requirements.txt`
- Test: `tests/test_tokens.py`

**Interfaces:**
- Produces: `sign_report_token(secret_key: str, user_id: int, report_id: str) -> str`, `verify_report_token(secret_key: str, token: str, report_id: str, *, max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS) -> Optional[int]`, `report_token_expiry_date(*, max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS) -> str`, constant `REPORT_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 3600`.

- [ ] **Step 1: Add the dependency**

Edit `automation/requirements.txt`, add after the `python-dotenv>=1.0` line:

```
itsdangerous>=2.1.0
```

And update the file's header comment block to add a line explaining it (the file already has a comment style for this — append, don't replace):

```
# itsdangerous signs report-link tokens (automation/tokens.py) — the
# de facto standard for exactly this (Flask's own session/reset-link
# library), avoids hand-rolling HMAC+timestamp parsing.
```

Install it: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/pip install itsdangerous>=2.1.0`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_tokens.py`:

```python
"""Tests for automation.tokens — stateless signed report-link tokens."""

from __future__ import annotations

import time

from automation.tokens import (
    REPORT_TOKEN_MAX_AGE_SECONDS,
    report_token_expiry_date,
    sign_report_token,
    verify_report_token,
)

SECRET = "test-signing-key"


def test_sign_and_verify_round_trip():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token(SECRET, token, "report-abc") == 123


def test_verify_rejects_token_for_a_different_report_id():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token(SECRET, token, "report-xyz") is None


def test_verify_rejects_tampered_token():
    token = sign_report_token(SECRET, 123, "report-abc")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    assert verify_report_token(SECRET, tampered, "report-abc") is None


def test_verify_rejects_token_signed_with_a_different_key():
    token = sign_report_token(SECRET, 123, "report-abc")

    assert verify_report_token("a-different-key", token, "report-abc") is None


def test_verify_rejects_expired_token():
    token = sign_report_token(SECRET, 123, "report-abc")
    time.sleep(1.1)

    assert verify_report_token(SECRET, token, "report-abc", max_age_seconds=1) is None


def test_report_token_expiry_date_is_max_age_seconds_in_the_future():
    import datetime as _dt

    expected = (_dt.datetime.now() + _dt.timedelta(seconds=REPORT_TOKEN_MAX_AGE_SECONDS)).date()

    assert report_token_expiry_date() == expected.isoformat()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_tokens.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'automation.tokens'`

- [ ] **Step 4: Write the implementation**

Create `automation/tokens.py`:

```python
"""Stateless, signed tokens for report links.

Report links carry no stored bearer secret. The token is an HMAC
signature (via ``itsdangerous``) over ``(user_id, report_id)``, verified
by recomputing the signature with one server-wide secret key
(``REPORTS_SIGNING_KEY``). There is nothing to look up or leak from the
database; rotating the secret key invalidates every outstanding link.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

REPORT_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
_SALT = "report-token"


def sign_report_token(secret_key: str, user_id: int, report_id: str) -> str:
    """Sign a (user_id, report_id) pair into an opaque, URL-safe token."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"user_id": user_id, "report_id": report_id})


def verify_report_token(
    secret_key: str,
    token: str,
    report_id: str,
    *,
    max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS,
) -> Optional[int]:
    """Return the signed user_id if `token` is valid, unexpired, and matches
    `report_id`; otherwise None. Never raises."""
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or payload.get("report_id") != report_id:
        return None
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, int) else None


def report_token_expiry_date(*, max_age_seconds: int = REPORT_TOKEN_MAX_AGE_SECONDS) -> str:
    """Human-readable expiry date (YYYY-MM-DD) for a token signed right now."""
    expiry = _dt.datetime.now() + _dt.timedelta(seconds=max_age_seconds)
    return expiry.date().isoformat()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_tokens.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/tokens.py automation/requirements.txt tests/test_tokens.py
git commit -m "feat(automation): add stateless signed report-link tokens"
```

---

### Task 2: `config.py` — `REPORTS_SIGNING_KEY` required env var

**Files:**
- Modify: `automation/config.py`
- Modify: `automation/env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ServiceConfig.reports_signing_key: str` (required, no default — inserted right after `public_base_url` in the dataclass field order).

- [ ] **Step 1: Write the failing tests**

Edit `tests/test_config.py`. Update `_SERVICE_ENV_VARS`:

```python
_SERVICE_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_INVITE_CODE",
    "REPORTS_PUBLIC_BASE_URL",
    "REPORTS_SIGNING_KEY",
    "BOT_PRESET",
    "BOT_DAILY_CAP",
    "REPORTS_WEB_HOST",
    "REPORTS_WEB_PORT",
    "BOT_ADMIN_USER_ID",
    "BOT_DB_PATH",
)
```

Update `_set_required_env`:

```python
def _set_required_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "test-invite")
    monkeypatch.setenv("REPORTS_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("REPORTS_SIGNING_KEY", "test-signing-key")
```

Add an assertion to `test_from_env_parses_defaults_with_only_required_vars_set` (after the `config.public_base_url` assertion):

```python
    assert config.reports_signing_key == "test-signing-key"
```

Add `"REPORTS_SIGNING_KEY"` to the assertions in `test_from_env_raises_config_error_listing_all_missing_required_vars`:

```python
    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_INVITE_CODE" in message
    assert "REPORTS_PUBLIC_BASE_URL" in message
    assert "REPORTS_SIGNING_KEY" in message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'ServiceConfig' object has no attribute 'reports_signing_key'` and the missing-vars test fails because `"REPORTS_SIGNING_KEY"` isn't in the error message yet.

- [ ] **Step 3: Write the implementation**

In `automation/config.py`, add the field to the dataclass (insert right after `public_base_url: str`):

```python
@dataclass(frozen=True)
class ServiceConfig:
    """Validated configuration for the on-demand bot + report-hosting service."""

    bot_token: str
    invite_code: str
    public_base_url: str
    reports_signing_key: str
    daily_cap: int = DEFAULT_DAILY_CAP
    preset: str = DEFAULT_PRESET
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    db_path: Path = DEFAULT_DB_PATH
    admin_user_id: Optional[int] = None
    report_cache_ttl_seconds: int = DEFAULT_REPORT_CACHE_TTL_SECONDS
```

In `from_env()`, add the read and validation right after `public_base_url`'s read:

```python
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        invite_code = os.environ.get("TELEGRAM_INVITE_CODE", "").strip()
        public_base_url = os.environ.get("REPORTS_PUBLIC_BASE_URL", "").strip()
        reports_signing_key = os.environ.get("REPORTS_SIGNING_KEY", "").strip()

        missing = []
        if not bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not invite_code:
            missing.append("TELEGRAM_INVITE_CODE")
        if not public_base_url:
            missing.append("REPORTS_PUBLIC_BASE_URL")
        if not reports_signing_key:
            missing.append("REPORTS_SIGNING_KEY")
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
```

And pass it through in the final `return ServiceConfig(...)`:

```python
        return ServiceConfig(
            bot_token=bot_token,
            invite_code=invite_code,
            public_base_url=public_base_url.rstrip("/"),
            reports_signing_key=reports_signing_key,
            daily_cap=daily_cap,
            preset=preset,
            web_host=web_host,
            web_port=web_port,
            db_path=db_path,
            admin_user_id=admin_user_id,
            report_cache_ttl_seconds=report_cache_ttl_seconds,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_config.py -v`
Expected: all passed

- [ ] **Step 5: Update env.example**

Edit `automation/env.example`, insert right after the `REPORTS_PUBLIC_BASE_URL=` line and its comment block:

```
# Secret key used to sign report-link tokens (HMAC, via itsdangerous).
# Generate once with: python -c "import secrets; print(secrets.token_urlsafe(32))"
# Rotating this immediately invalidates every outstanding report link.
REPORTS_SIGNING_KEY=
```

- [ ] **Step 6: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/config.py automation/env.example tests/test_config.py
git commit -m "feat(automation): add required REPORTS_SIGNING_KEY config"
```

---

### Task 3: `store.py` — lock down the SQLite file's permissions

**Files:**
- Modify: `automation/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no API change — `Store.__init__` now chmods the DB file to `0o600` as a side effect.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py` (after the imports, anywhere among the other tests):

```python
def test_db_file_is_created_with_owner_only_permissions(tmp_path):
    db_path = tmp_path / "service.db"
    store = Store(db_path)
    try:
        mode = db_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py::test_db_file_is_created_with_owner_only_permissions -v`
Expected: FAIL — actual mode will be whatever the process umask produced (commonly `0o644`).

- [ ] **Step 3: Write the implementation**

In `automation/store.py`, add `import os` to the top-of-file imports:

```python
from __future__ import annotations

import datetime as _dt
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
```

Update `Store.__init__`:

```python
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        os.chmod(db_path, 0o600)
        self._lock = threading.Lock()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/store.py tests/test_store.py
git commit -m "fix(automation): lock down service.db to owner-only permissions"
```

---

### Task 4: `store.py` — invite codes (`invites` table)

**Files:**
- Modify: `automation/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Store.create_invite(max_uses: int, ttl_hours: int) -> tuple[str, str]` (returns `(code, expires_at_iso)`), `Store.consume_invite(code: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_create_invite_and_consume_invite_within_limits(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        code, expires_at = store.create_invite(max_uses=2, ttl_hours=24)

        assert len(code) > 0
        assert expires_at  # an ISO timestamp string
        assert store.consume_invite(code)
        assert store.consume_invite(code)  # second of 2 allowed uses
        assert not store.consume_invite(code)  # third use exceeds max_uses
    finally:
        store.close()


def test_consume_invite_rejects_unknown_code(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        assert not store.consume_invite("not-a-real-code")
    finally:
        store.close()


def test_consume_invite_rejects_expired_code(tmp_path):
    import datetime as _dt
    import sqlite3

    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        code, _ = store.create_invite(max_uses=1, ttl_hours=1)
    finally:
        store.close()

    # Backdate the expiry via a separate connection, after closing the store's.
    db_path = tmp_path / "service.db"
    conn = sqlite3.connect(str(db_path))
    past = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE invites SET expires_at = ? WHERE code = ?", (past, code))
    conn.commit()
    conn.close()

    store2 = Store(db_path)
    try:
        assert not store2.consume_invite(code)
    finally:
        store2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py -k invite -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'create_invite'`

- [ ] **Step 3: Write the implementation**

In `automation/store.py`, add the table to `_SCHEMA` (after the `run_cache` table, before the closing `"""`):

```python
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
```

Add a constant near `TOKEN_BYTES`/`REPORT_ID_BYTES`:

```python
TOKEN_BYTES = 16
REPORT_ID_BYTES = 8
INVITE_CODE_BYTES = 8
```

Add the two methods at the end of the `Store` class (after `upsert_cached_run`):

```python
    # -- invites ----------------------------------------------------------

    def create_invite(self, max_uses: int, ttl_hours: int) -> tuple[str, str]:
        """Generate and store a new invite code. Returns (code, expires_at)."""
        code = secrets.token_urlsafe(INVITE_CODE_BYTES)
        created_at = _dt.datetime.now().isoformat(timespec="seconds")
        expires_at = (
            _dt.datetime.now() + _dt.timedelta(hours=ttl_hours)
        ).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (code, max_uses, uses, expires_at, created_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (code, max_uses, expires_at, created_at),
            )
            self._conn.commit()
        return code, expires_at

    def consume_invite(self, code: str) -> bool:
        """Atomically validate and consume one use of an invite code.

        Returns True (and increments ``uses``) if the code exists, has not
        expired, and has remaining uses; returns False otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT max_uses, uses, expires_at FROM invites WHERE code = ?",
                (code,),
            ).fetchone()
            if not row:
                return False
            max_uses, uses, expires_at = row
            if uses >= max_uses:
                return False
            if _dt.datetime.now() > _dt.datetime.fromisoformat(expires_at):
                return False
            self._conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE code = ?", (code,)
            )
            self._conn.commit()
            return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/store.py tests/test_store.py
git commit -m "feat(automation): add DB-backed expiring, capped-use invite codes"
```

---

### Task 5: `store.py` — stop exposing `access_token`

**Files:**
- Modify: `automation/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Store.unlock(user_id: int, telegram_name: str) -> None` (return type changes from `str` to `None`). `Store.get_token` is removed.

- [ ] **Step 1: Update the test**

In `tests/test_store.py`, replace `test_unlock_is_idempotent_and_allows_user`:

```python
def test_unlock_is_idempotent_and_allows_user(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        store.unlock(123, "alice")
        store.unlock(123, "alice-renamed")  # idempotent: no error, no duplicate row

        assert store.is_allowed(123)
        assert not store.is_allowed(456)
    finally:
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py::test_unlock_is_idempotent_and_allows_user -v`
Expected: PASS already (the test no longer asserts on tokens, and `unlock` still works) — this step is here to confirm the *test* change alone doesn't break anything before the implementation change. If it fails for any other reason, stop and investigate before continuing.

- [ ] **Step 3: Write the implementation**

In `automation/store.py`, replace `unlock` and remove `get_token`:

```python
    def unlock(self, user_id: int, telegram_name: str) -> None:
        """Add user_id to the allowlist (idempotent)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                return
            placeholder_token = secrets.token_urlsafe(TOKEN_BYTES)
            created_at = _dt.datetime.now().isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO users (user_id, access_token, telegram_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, placeholder_token, telegram_name, created_at),
            )
            self._conn.commit()
```

(Delete the `get_token` method entirely — it directly followed `unlock` in the `# -- allowlist` section.)

- [ ] **Step 4: Run the full store test suite**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_store.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/store.py tests/test_store.py
git commit -m "refactor(automation): stop exposing per-user access tokens from Store"
```

**Note for the next task:** `automation/bot.py` and `automation/web/server.py` still call `store.get_token(...)` at this point in the plan — that's expected and fixed in Tasks 6 and 9. `tests/test_web_server.py` will fail to import/run correctly until Task 6. Do not run the *full* `pytest -q` repo-wide between Task 5 and Task 6; run only the files each task's steps name.

---

### Task 6: `web/server.py` — signed-token auth + rate limiting

**Files:**
- Modify: `automation/web/server.py`
- Modify: `automation/service.py`
- Modify: `automation/bot.py:360-365` (the `__main__` block only, for its `create_app` call)
- Modify: `automation/requirements.txt`
- Test: `tests/test_web_server.py` (full rewrite)

**Interfaces:**
- Consumes: `automation.tokens.sign_report_token`/`verify_report_token` (Task 1), `ServiceConfig.reports_signing_key` (Task 2).
- Produces: `create_app(store: Store, config: ServiceConfig) -> FastAPI` (signature changes — was `create_app(store: Store)`).

- [ ] **Step 1: Add dependencies**

Edit `automation/requirements.txt`, add after `itsdangerous>=2.1.0`:

```
slowapi>=0.1.9
limits>=3.0
```

With a comment:

```
# slowapi (+ its own dependency, limits) rate-limits the public report
# endpoint (web/server.py) and, used directly, the bot's per-user ticker
# throttle (bot.py) — one shared, tested rate-limit implementation
# instead of two hand-rolled ones.
```

Install: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/pip install "slowapi>=0.1.9" "limits>=3.0"`

- [ ] **Step 2: Write the failing tests**

Replace the full contents of `tests/test_web_server.py`:

```python
"""Tests for the token-gated, rate-limited report web server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from automation.config import ServiceConfig
from automation.store import Store
from automation.tokens import sign_report_token
from automation.web.server import create_app

SIGNING_KEY = "test-signing-key"


def _config(**overrides) -> ServiceConfig:
    defaults = dict(
        bot_token="test-token",
        invite_code="test-invite",
        public_base_url="https://example.invalid",
        reports_signing_key=SIGNING_KEY,
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


def test_report_route_serves_html_for_a_valid_signed_token(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        html_path = tmp_path / "report.html"
        html_path.write_text("<html><body>report</body></html>", encoding="utf-8")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(html_path))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={token}")

        assert response.status_code == 200
        assert response.text == "<html><body>report</body></html>"
        assert response.headers["cache-control"] == "private, max-age=3600"
    finally:
        store.close()


def test_report_route_rejects_missing_or_garbage_token(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        client = TestClient(create_app(store, _config()))

        assert client.get(f"/report/{report_id}").status_code == 403
        assert client.get(f"/report/{report_id}?token=garbage").status_code == 403
    finally:
        store.close()


def test_report_route_rejects_token_signed_for_a_different_report(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        other_token = sign_report_token(SIGNING_KEY, 123, "some-other-report-id")
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={other_token}")

        assert response.status_code == 403
    finally:
        store.close()


def test_report_route_rejects_token_signed_for_a_different_user(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "r.html"))
        wrong_user_token = sign_report_token(SIGNING_KEY, 456, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={wrong_user_token}")

        assert response.status_code == 403
    finally:
        store.close()


def test_report_route_returns_not_found_for_unknown_report_id(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        client = TestClient(create_app(store, _config()))
        token = sign_report_token(SIGNING_KEY, 123, "unknown-id")

        response = client.get(f"/report/unknown-id?token={token}")

        assert response.status_code == 404
    finally:
        store.close()


def test_report_route_returns_gone_when_file_is_missing(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(tmp_path / "missing.html"))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))

        response = client.get(f"/report/{report_id}?token={token}")

        assert response.status_code == 410
    finally:
        store.close()


def test_report_route_is_rate_limited_per_client(tmp_path):
    store = Store(tmp_path / "service.db")
    store.init_db()
    try:
        html_path = tmp_path / "report.html"
        html_path.write_text("<html></html>", encoding="utf-8")
        report_id = store.add_report(123, "NVDA", "2026-06-15", str(html_path))
        token = sign_report_token(SIGNING_KEY, 123, report_id)
        client = TestClient(create_app(store, _config()))
        url = f"/report/{report_id}?token={token}"

        statuses = [client.get(url).status_code for _ in range(31)]

        assert statuses[:30] == [200] * 30
        assert statuses[30] == 429
    finally:
        store.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_web_server.py -v`
Expected: FAIL — `TypeError: create_app() takes 1 positional argument but 2 were given`

- [ ] **Step 4: Write the implementation**

Replace the full contents of `automation/web/server.py`:

```python
"""FastAPI app that hosts pre-rendered analysis reports.

Bound to ``127.0.0.1`` only — Cloudflare Tunnel is the sole public ingress
(see ``automation/linux/cloudflared.service``). Reports are rendered once by
:func:`automation.reports.render_to_html` and served here as static files;
this module does no per-request markdown work.

Report tokens are verified, not looked up (see :mod:`automation.tokens`) —
there is no bearer secret in the database to leak. Requests are rate
limited per client (keyed off Cloudflare's ``CF-Connecting-IP`` header,
falling back to the raw socket address) to slow down scraping/abuse.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from automation.config import ServiceConfig
from automation.runlog import get_logger
from automation.store import Store
from automation.tokens import verify_report_token

log = get_logger(__name__)

REPORT_CACHE_CONTROL = "private, max-age=3600"
REPORT_RATE_LIMIT = "30/minute"


def _client_key(request: Request) -> str:
    """Prefer Cloudflare's real-visitor-IP header; Cloudflare Tunnel proxies
    every request through a local loopback connection, so the raw socket
    address (``get_remote_address``) would otherwise be the same for every
    visitor and rate-limit them all as one shared bucket."""
    return request.headers.get("cf-connecting-ip") or get_remote_address(request)


def create_app(store: Store, config: ServiceConfig) -> FastAPI:
    """Build the FastAPI app, injecting ``store``/``config`` for report
    lookups and token verification."""
    limiter = Limiter(key_func=_client_key)
    app = FastAPI(title="TradingAgents Reports")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/report/{report_id}")
    @limiter.limit(REPORT_RATE_LIMIT)
    def get_report(request: Request, report_id: str, token: str = "") -> FileResponse:
        report = store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")

        signed_user_id = verify_report_token(config.reports_signing_key, token, report_id)
        if signed_user_id is None or signed_user_id != report.user_id:
            raise HTTPException(status_code=403, detail="Invalid, expired, or missing token")

        html_path = Path(report.html_path)
        if not html_path.exists():
            raise HTTPException(status_code=410, detail="Report file no longer available")

        return FileResponse(
            html_path,
            media_type="text/html",
            headers={"Cache-Control": REPORT_CACHE_CONTROL},
        )

    return app


if __name__ == "__main__":
    import uvicorn

    cfg = ServiceConfig.from_env()
    store = Store(cfg.db_path)
    store.init_db()
    uvicorn.run(create_app(store, cfg), host=cfg.web_host, port=cfg.web_port)
```

- [ ] **Step 5: Update `service.py`'s `create_app` call**

In `automation/service.py`, change:

```python
    uvicorn_config = uvicorn.Config(
        create_app(store),
        host=config.web_host,
        port=config.web_port,
        log_level="info",
    )
```

to:

```python
    uvicorn_config = uvicorn.Config(
        create_app(store, config),
        host=config.web_host,
        port=config.web_port,
        log_level="info",
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_web_server.py -v`
Expected: 7 passed

- [ ] **Step 7: Verify the whole repo still imports**

Run: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/python -c "import automation.service, automation.web.server"`
Expected: no output, exit code 0 (`bot.py` itself never calls `create_app` — only `web/server.py`'s own `__main__` block and `service.py` do, and both are updated in this task, so nothing in the repo still references the old `create_app(store)` signature after this step).

- [ ] **Step 8: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/web/server.py automation/service.py automation/requirements.txt tests/test_web_server.py
git commit -m "feat(automation): verify report links via signed tokens, rate-limit the endpoint"
```

---

### Task 7: `service.py` + `bot.py.__main__` — wire the bot-side rate limiter

**Files:**
- Modify: `automation/service.py`
- Modify: `automation/bot.py:1-37` (imports/constants) and `:360-365` (the `__main__` block)

**Interfaces:**
- Consumes: `limits.storage.MemoryStorage`, `limits.strategies.FixedWindowRateLimiter` (from the `limits` dependency added in Task 6).
- Produces: a `rate_limiter` object (an instance of `FixedWindowRateLimiter`) constructed once per process and passed into `run_bot(...)` as a new fourth positional parameter — `run_bot`'s own signature changes in Task 8, this task only prepares the two composition roots that call it.

This task exists separately from Task 8 so the two places that *construct* a process (`service.py`'s `main()` and `bot.py`'s `if __name__ == "__main__":` block) change together, before `run_bot`'s signature changes in the next task — avoiding a moment where one composition root passes a 4th argument `run_bot` doesn't yet accept.

- [ ] **Step 1: Update `automation/service.py`**

Add the import (with the other `automation.*`/third-party imports):

```python
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
```

In `main()`, construct the limiter right after `job_queue = JobQueue(...)`:

```python
    job_queue = JobQueue(store, config.report_cache_ttl_seconds)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
```

And pass it into the `run_bot` call:

```python
    log.info("Bot starting (preset=%s, daily_cap=%s)", config.preset, config.daily_cap)
    run_bot(config, store, job_queue, rate_limiter, stop_event=stop_event)
```

- [ ] **Step 2: Update `automation/bot.py`'s `__main__` block**

Add the same import near the top of `automation/bot.py` (with the other imports):

```python
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
```

Change the `if __name__ == "__main__":` block at the bottom of `automation/bot.py`:

```python
if __name__ == "__main__":
    cfg = ServiceConfig.from_env()
    store = Store(cfg.db_path)
    store.init_db()
    jobs = JobQueue(store, cfg.report_cache_ttl_seconds)
    rate_limiter = FixedWindowRateLimiter(MemoryStorage())
    run_bot(cfg, store, jobs, rate_limiter)
```

- [ ] **Step 3: Verify imports still resolve**

Run: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/python -c "import automation.service, automation.bot"`
Expected: no output, exit code 0. (Note: at this point `run_bot`'s signature still has `stop_event` as its 4th positional parameter, so the `run_bot(cfg, store, jobs, rate_limiter)` call written above would, if actually executed right now, silently bind `rate_limiter` to `stop_event` rather than raise an error. That's fine — this step only checks that the module *imports*, never executes `__main__`. Do not run `python -m automation.bot` or `python -m automation.service` until after Task 8 makes `run_bot`'s real signature match this call.)

- [ ] **Step 4: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/service.py automation/bot.py
git commit -m "feat(automation): construct the bot-side rate limiter in both composition roots"
```

---

### Task 8: `bot.py` — per-user burst throttle on ticker submissions

**Files:**
- Modify: `automation/bot.py`
- Test: `tests/test_bot.py` (new file)

**Interfaces:**
- Consumes: `rate_limiter: FixedWindowRateLimiter` (constructed in Task 7), `limits.RateLimitItemPerSecond`.
- Produces: `run_bot(config, store, job_queue, rate_limiter, stop_event=None)`, `_handle_update(update, config, store, job_queue, rate_limiter)`, `_handle_ticker_message(text, user_id, chat_id, config, store, job_queue, rate_limiter)`, `_handle_watchlist(text, user_id, chat_id, config, store, job_queue, rate_limiter)`, `_enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue, rate_limiter)` — all gain a trailing `rate_limiter` parameter.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bot.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -v`
Expected: FAIL — `TypeError: _handle_ticker_message() takes 6 positional arguments but 7 were given`

- [ ] **Step 3: Write the implementation**

In `automation/bot.py`, add the import:

```python
from limits import RateLimitItemPerSecond
```

Add a module-level constant (stateless config, not mutable state — same category as `_SYMBOL_RE`/`_COMMANDS`) near the other constants at the top:

```python
ANALYSIS_RATE_LIMIT = RateLimitItemPerSecond(1, 10)  # 1 submission per 10s per user
```

Update `run_bot`'s signature and body:

```python
def run_bot(
    config: ServiceConfig,
    store: Store,
    job_queue: JobQueue,
    rate_limiter,
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
                _handle_update(update, config, store, job_queue, rate_limiter)
            except Exception:
                log.exception("Failed to handle update %s", update.get("update_id"))
```

Update `_handle_update`:

```python
def _handle_update(
    update: dict, config: ServiceConfig, store: Store, job_queue: JobQueue, rate_limiter
) -> None:
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
        _handle_watchlist(text, user_id, chat_id, config, store, job_queue, rate_limiter)
    else:
        _handle_ticker_message(text, user_id, chat_id, config, store, job_queue, rate_limiter)
```

Update `_handle_ticker_message`:

```python
def _handle_ticker_message(
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
        _reply(chat_id, "I couldn't find a ticker in that message.\n\n" + _help_text(config), config)
        return

    _enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue, rate_limiter)
```

Update `_enqueue_symbols` to check the throttle before doing anything else:

```python
def _enqueue_symbols(
    symbols: list[str],
    user_id: int,
    chat_id: str,
    config: ServiceConfig,
    store: Store,
    job_queue: JobQueue,
    rate_limiter,
) -> None:
    if not rate_limiter.hit(ANALYSIS_RATE_LIMIT, str(user_id)):
        _reply(chat_id, "⏳ Please wait a few seconds between requests.", config)
        return

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
```

Update `_handle_watchlist`'s signature and its `run` branch's call to `_enqueue_symbols`:

```python
def _handle_watchlist(
    text: str,
    user_id: int,
    chat_id: str,
    config: ServiceConfig,
    store: Store,
    job_queue: JobQueue,
    rate_limiter,
) -> None:
```

(leave the body identical except the one call site inside the `elif sub == "run":` branch)

```python
    elif sub == "run":
        symbols = store.watchlist_list(user_id)
        if not symbols:
            _reply(chat_id, "Your watchlist is empty. Add tickers first: /watchlist add NVDA", config)
            return
        _enqueue_symbols(symbols, user_id, chat_id, config, store, job_queue, rate_limiter)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -v`
Expected: 2 passed

- [ ] **Step 5: Update `service.py`/`bot.py.__main__` call sites are already correct**

Task 7 already updated both `run_bot(...)` call sites to pass `rate_limiter` positionally in the right place — confirm by running:

Run: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/python -c "import automation.service, automation.bot"`
Expected: no output, exit code 0

- [ ] **Step 6: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/bot.py tests/test_bot.py
git commit -m "feat(automation): throttle per-user ticker submissions to 1 per 10s"
```

---

### Task 9: `bot.py` — admin `/invite` command + DB-backed `/start`

**Files:**
- Modify: `automation/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `Store.create_invite`/`consume_invite` (Task 4).
- Produces: `_handle_invite(text, user_id, chat_id, config, store)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -k invite -v`
Expected: FAIL — `AttributeError: module 'automation.bot' has no attribute '_handle_invite'`

- [ ] **Step 3: Write the implementation**

In `automation/bot.py`, add constants near the other `*_DEFAULT_*`/`*_MAX_*` constants:

```python
INVITE_DEFAULT_MAX_USES = 1
INVITE_DEFAULT_TTL_HOURS = 72
```

Add `("invite", "Admin only: generate a new invite code")` to `_COMMANDS`:

```python
_COMMANDS: list[tuple[str, str]] = [
    ("start", "Unlock access with an invite code"),
    ("help", "Show usage instructions"),
    ("cancel", "Cancel your most recently queued analysis"),
    ("history", "Show your recent analyses"),
    ("watchlist", "Manage your personal ticker watchlist"),
    ("invite", "Admin only: generate a new invite code"),
    ("status", "Admin only: queue/usage/log snapshot"),
]
```

Update `_handle_start` to try the DB-backed invite first, falling back to the bootstrap env code:

```python
def _handle_start(
    text: str, user_id: int, user_name: str, chat_id: str, config: ServiceConfig, store: Store
) -> None:
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""

    if not code:
        _reply(chat_id, _help_text(config), config)
        return

    if store.consume_invite(code) or hmac.compare_digest(code, config.invite_code):
        store.unlock(user_id, user_name)
        _reply(
            chat_id,
            "✅ You're in! Send a ticker (e.g. NVDA) to run an analysis.\n\n" + _help_text(config),
            config,
        )
    else:
        _reply(chat_id, "❌ That invite code isn't valid. Ask the owner for an invite code.", config)
```

Add the dispatch branch in `_handle_update` (right after the `/cancel` branch):

```python
    elif text == "/cancel":
        _handle_cancel(user_id, chat_id, job_queue, config)
    elif text == "/invite" or text.startswith("/invite "):
        _handle_invite(text, user_id, chat_id, config, store)
    elif text == "/history" or text.startswith("/history "):
```

Add the new handler function (near `_handle_status`, since both are admin-only):

```python
def _handle_invite(text: str, user_id: int, chat_id: str, config: ServiceConfig, store: Store) -> None:
    if config.admin_user_id is None or user_id != config.admin_user_id:
        _reply(chat_id, "This command is restricted to the service admin.", config)
        return

    parts = text.split()
    try:
        max_uses = int(parts[1]) if len(parts) > 1 else INVITE_DEFAULT_MAX_USES
        ttl_hours = int(parts[2]) if len(parts) > 2 else INVITE_DEFAULT_TTL_HOURS
    except ValueError:
        _reply(chat_id, "Usage: /invite [max_uses] [ttl_hours]", config)
        return
    if max_uses < 1 or ttl_hours < 1:
        _reply(chat_id, "max_uses and ttl_hours must both be at least 1.", config)
        return

    code, expires_at = store.create_invite(max_uses, ttl_hours)
    _reply(
        chat_id,
        f"🎫 New invite code: <code>{html.escape(code)}</code>\n"
        f"Max uses: {max_uses} · Expires: {html.escape(expires_at)}",
        config,
    )
```

Add `import html` to the top-of-file imports (alphabetical, with the other stdlib imports):

```python
import datetime as _dt
import hmac
import html
import json
import re
import threading
import time
from typing import Optional
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -v`
Expected: all passed (the two from Task 8 plus the five new ones)

- [ ] **Step 5: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/bot.py tests/test_bot.py
git commit -m "feat(automation): add admin /invite command, DB-backed /start invites"
```

---

### Task 10: `bot.py` — HTML-rendered messages, signed links, visible expiry

**Files:**
- Modify: `automation/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `automation.tokens.sign_report_token`/`report_token_expiry_date` (Task 1).
- Produces: `_format_result(result, report_id, user_id, config) -> str` (signature changes — drops the `user_token` param, takes `user_id` instead). `_reply` and all direct `telegram_api.send_message(...)` calls in `bot.py` now pass `parse_mode="HTML"`. `_build_job` drops its `store` parameter (no longer needed). `_handle_history` no longer calls `store.get_token`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -k "format_result or history_reply" -v`
Expected: FAIL — `TypeError: _format_result() takes 3 positional arguments but 4 were given` (current signature is `(result, report_id, user_token, config)`)

- [ ] **Step 3: Write the implementation**

In `automation/bot.py`, add the import:

```python
from automation.tokens import report_token_expiry_date, sign_report_token
```

Replace `_format_result`:

```python
def _format_result(
    result: RunResult, report_id: Optional[str], user_id: int, config: ServiceConfig
) -> str:
    if not result.ok:
        return f"⚠️ <b>{html.escape(result.ticker)}</b> failed: {html.escape(str(result.error))}"

    lines = [
        f"📈 <b>{html.escape(result.ticker)}</b> — "
        f"{html.escape(result.rating or 'N/A')} ({html.escape(result.date)})"
    ]
    if result.rationale:
        lines.append(html.escape(result.rationale))
    if report_id:
        token = sign_report_token(config.reports_signing_key, user_id, report_id)
        link = f"{config.public_base_url}/report/{report_id}?token={token}"
        lines.append(f'📄 <a href="{html.escape(link)}">Full report</a>')
        lines.append(f"<i>Link expires {report_token_expiry_date()} (7 days)</i>")
    lines.append(f"({result.duration_seconds:.0f}s, preset {html.escape(result.preset)})")
    return "\n".join(lines)
```

Update `_build_job` (drop the unused `store` param, call `telegram_api.send_message` with `parse_mode="HTML"`, pass `user_id` to `_format_result`):

```python
def _build_job(
    symbol: str, user_id: int, chat_id: str, spec: TickerSpec, date: str, config: ServiceConfig
) -> Job:
    def on_start() -> None:
        telegram_api.send_message(
            f"⏳ Running {html.escape(symbol)}…", chat_id, token=config.bot_token, parse_mode="HTML"
        )

    def on_complete(result: RunResult, report_id: Optional[str]) -> None:
        text = _format_result(result, report_id, user_id, config)
        telegram_api.send_message(text, chat_id, token=config.bot_token, parse_mode="HTML")

    return Job(user_id=user_id, chat_id=chat_id, spec=spec, date=date, on_start=on_start, on_complete=on_complete)
```

Update `_enqueue_symbols`'s call site (drop the `store` argument from `_build_job`, and HTML-escape the queued-position reply since `symbol` is attacker-influenced-ish free text matched against `_SYMBOL_RE` but still worth being consistent):

```python
        spec = TickerSpec(symbol=symbol, preset=config.preset, asset_type="stock")
        job = _build_job(symbol, user_id, chat_id, spec, date, config)
        position = job_queue.submit(job)
        _reply(
            chat_id,
            f"📥 <b>{html.escape(symbol)}</b> queued (position {position}). I'll message you when it's done.",
            config,
        )
```

Replace `_handle_history`:

```python
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

    header = f"📚 Your last {len(reports_list)} analyses:"
    entries = []
    for r in reports_list:
        token = sign_report_token(config.reports_signing_key, user_id, r.report_id)
        link = f"{config.public_base_url}/report/{r.report_id}?token={token}"
        entries.append(
            f"<b>{html.escape(r.ticker)}</b> — {html.escape(r.date)}\n"
            f'📄 <a href="{html.escape(link)}">Full report</a>\n'
            f"<i>Link expires {report_token_expiry_date()} (7 days)</i>"
        )
    _reply(chat_id, header + "\n\n" + "\n\n".join(entries), config)
```

Update `_reply` to send HTML by default:

```python
def _reply(chat_id: str, text: str, config: ServiceConfig) -> None:
    telegram_api.send_message(text, chat_id, token=config.bot_token, parse_mode="HTML")
```

Update the `_handle_invite` reply from Task 9 is already HTML-safe (it already used `html.escape` and `<code>` tags) — no change needed there.

Update `_help_text`'s admin-command filter to also skip `/invite` (it's already filtering `/status`):

```python
def _help_text(config: ServiceConfig) -> str:
    command_lines = "\n".join(
        f"/{name} - {desc}" for name, desc in _COMMANDS if name not in ("status", "invite")
    )
    return (
        "Send me 1-3 stock tickers (e.g. NVDA or NVDA AAPL) and I'll run an "
        "analysis and reply with a summary and a link to the full report.\n\n"
        f"You get {config.daily_cap} analyses per day.\n\n"
        "Commands:\n"
        f"{command_lines}\n\n"
        "Watchlist usage: /watchlist [list|add SYM...|remove SYM...|run]"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_bot.py -v`
Expected: all passed

- [ ] **Step 5: Run the full automation-related test suite**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q -k "store or job_queue or web_server or telegram or bot or config or tokens"`
Expected: all passed, 0 failed

- [ ] **Step 6: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add automation/bot.py tests/test_bot.py
git commit -m "feat(automation): render bot replies as HTML with escaped text and visible link expiry"
```

---

### Task 11: `CLAUDE.md` — update docs to match

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing (documentation only).

- [ ] **Step 1: Update the file bullet list**

In `CLAUDE.md`, under **On-demand bot + hosted reports (new)**, replace the `store.py` and `telegram_api.py` bullets and add a `tokens.py` bullet (insert it right after the `config.py` bullet):

```markdown
- `config.py` — `ServiceConfig.from_env()` / `ConfigError`. Single source of
  truth for all env vars below; everything else takes an injected config.
- `tokens.py` — `sign_report_token`/`verify_report_token`: stateless,
  HMAC-signed (`itsdangerous`) report-link tokens keyed on
  `REPORTS_SIGNING_KEY`. No bearer secret is stored in the database;
  tokens expire 7 days after signing.
- `store.py` — `Store` (SQLite at `automation/logs/service.db`, `chmod
  600`'d on creation): allowlist (`User`), expiring/capped-use invite
  codes (`invites` table), daily usage caps, rendered `Report` records,
  personal watchlists, the run-result cache.
- `telegram_api.py` — `send_message` (HTML `parse_mode`) / `get_updates` /
  `set_my_commands` / `_split_message` (4096-char chunking).
  `notify_telegram.send_message` now delegates here.
```

Replace the `bot.py` bullet:

```markdown
- `bot.py` — `run_bot(...)`: long-polls Telegram, handles `/start <code>`
  (DB-backed or bootstrap-env-var invite unlock), `/help`, `/cancel`,
  `/history`, `/watchlist`, ticker messages (enqueue with daily-cap +
  per-user burst-rate check), owner-only `/status` and `/invite`. Replies
  are sent as HTML with escaped dynamic text and signed report links.
```

Replace the `web/server.py` bullet:

```markdown
- `web/server.py` — `create_app(store, config)`: FastAPI app serving
  `GET /report/{report_id}?token=...` (signed-token verification via
  `automation.tokens`, rate-limited 30/min/client via `slowapi`) and
  `GET /healthz`. Bind `127.0.0.1` only.
```

- [ ] **Step 2: Update the flow diagram**

Replace the **Flow: ticker → report link** section:

```markdown
## Flow: ticker → report link

\`\`\`
Telegram "/start <code>"  → store.consume_invite() or bootstrap env code
                           → store.unlock() → permanent allowlist
Telegram "NVDA"            → daily cap + per-user burst-rate check
                           → JobQueue.enqueue()
JobQueue worker             → run_one_ticker() → reports.render_to_html()
                            → store.add_report() → append_decision()
Reply to user (HTML)         "<b>TICKER</b> — rating\n<escaped rationale>
                               📄 <a href=...>Full report</a>
                               Link expires <date> (7 days)"
\`\`\`
```

(Use literal triple backticks in the actual file, not escaped — the escaping above is only because this plan step is itself inside a fenced code block.)

- [ ] **Step 3: Update the env vars list**

In **Key env vars**, add a line after the `TELEGRAM_INVITE_CODE` line:

```markdown
- `TELEGRAM_INVITE_CODE` — bootstrap-only shared secret for `/start
  <code>`; ordinary invites are issued via the admin's `/invite` command
  and stored in the DB with expiry/use limits.
- `REPORTS_SIGNING_KEY` — secret key signing report-link tokens
  (`automation/tokens.py`); rotating it invalidates all outstanding links.
```

- [ ] **Step 4: Update the security model section**

Replace the **Security model** section entirely:

```markdown
## Security model

- **Access**: invite-only. The admin issues per-invitee codes via
  `/invite [max_uses] [ttl_hours]` (DB-backed, expiring, capped-use —
  `invites` table). `TELEGRAM_INVITE_CODE` in `.env` remains as a
  one-time bootstrap code (compared with `hmac.compare_digest`) so the
  admin can unlock themselves before issuing any `/invite` codes. Either
  path calls `store.unlock()`, a permanent per-user allowlist entry.
  Non-allowlisted users can never enqueue work.
- **Report links**: `report_id = secrets.token_urlsafe(8)` identifies the
  rendered file; the `?token=...` query param is a stateless, signed
  token (`automation/tokens.py`, via `itsdangerous`) over `(user_id,
  report_id)`, verified by recomputing the signature with
  `REPORTS_SIGNING_KEY` — no bearer secret is stored in the database.
  Tokens expire after 7 days; every bot reply that includes a report
  link re-signs a fresh token and states the expiry inline, so a user
  can always get a live link via `/history` even after an old one
  expires.
- **Rate limiting**: layered — `BOT_DAILY_CAP` analyses/user/day (SQLite
  `usage` table), a 1-request-per-10-seconds-per-user burst throttle on
  ticker submissions (`limits`, in-memory), the single-worker queue
  capping total load, and a 30-requests/minute-per-client limit on the
  public `/report/{id}` endpoint (`slowapi`, keyed off Cloudflare's
  `CF-Connecting-IP` header).
- **Report content**: LLM-generated markdown is rendered once and
  sanitized with `bleach` before being served — no stored XSS.
- **Bot messages**: sent with Telegram `parse_mode="HTML"`; all dynamic
  text (tickers, dates, LLM rationale, invite codes) is `html.escape`'d
  before interpolation, so malformed or adversarial LLM output can't
  break rendering or inject markup.
- **Database file**: `service.db` is `chmod 600`'d on creation —
  least-privilege, matching `.env`'s own protection.
- **Network**: FastAPI/uvicorn bound to `127.0.0.1`; Cloudflare Tunnel is
  the only public ingress; Ollama stays on localhost. No secrets in code
  — all via `.env` / systemd `EnvironmentFile`.
```

- [ ] **Step 5: Commit**

```bash
cd /home/nachiappan-hari/TradingAgents
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for signed report tokens, invites, rate limiting"
```

---

### Task 12: Full verification pass

**Files:** none modified — verification only.

- [ ] **Step 1: Run the full repo test suite**

Run: `cd /home/nachiappan-hari/TradingAgents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`
Expected: all passed, 0 failed (this also re-confirms nothing outside `automation/` regressed)

- [ ] **Step 2: Run ruff over the changed files**

Run: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/ruff check automation/tokens.py automation/store.py automation/config.py automation/bot.py automation/web/server.py automation/service.py tests/test_tokens.py tests/test_bot.py tests/test_store.py tests/test_config.py tests/test_web_server.py`
Expected: no findings (CI runs `ruff` strict over the full repo, so this must be clean before pushing)

- [ ] **Step 3: Confirm clean-install import still works**

Run: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/python -c "import automation.service, automation.bot, automation.web.server, automation.tokens, automation.config, automation.store; print('import OK')"`
Expected: `import OK`

- [ ] **Step 4: Generate a real signing key for local manual testing**

Run: `.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"`
Add the output to your repo-root `.env` as `REPORTS_SIGNING_KEY=<value>` (alongside the existing `TELEGRAM_BOT_TOKEN`, `TELEGRAM_INVITE_CODE`, etc. — see `automation/env.example`).

- [ ] **Step 5: Manual smoke test against real Telegram (test credentials)**

Start the service: `cd /home/nachiappan-hari/TradingAgents && .venv/bin/python -m automation.service`

In Telegram, from a test account:
1. Send `/start <your TELEGRAM_INVITE_CODE>` — confirm "✅ You're in!" renders with bold/no literal asterisks if any formatting is present, and the message isn't visibly broken.
2. Send a ticker, e.g. `NVDA` — confirm the "📥 queued" reply renders the ticker in bold.
3. From the admin account (matching `BOT_ADMIN_USER_ID`), send `/invite 1 24` — confirm a code comes back, then `/start <that code>` from a second test account successfully unlocks it.
4. After an analysis completes, confirm the reply shows the ticker in bold, the rationale text renders as plain readable text (no stray HTML), a tappable "Full report" link, and an "expires <date> (7 days)" line — then open the link in a browser and confirm it loads.
5. Send two tickers back-to-back within 10 seconds — confirm the second gets "⏳ Please wait a few seconds between requests." instead of being queued.
6. Send `/history` — confirm it lists past analyses with working, freshly-signed links.

Stop the service with Ctrl-C once confirmed.

- [ ] **Step 6: Final commit (if any manual-testing fixups were needed)**

```bash
cd /home/nachiappan-hari/TradingAgents
git add -A
git status  # confirm only intended files are staged before committing any fixups
```
