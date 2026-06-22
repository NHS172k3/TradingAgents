"""SQLite-backed persistence for the on-demand bot service (the "cheap DB").

Wraps a single sqlite3 connection. All access is parameterized and guarded
by a lock (the connection is shared across the bot, queue worker, and web
server threads). Returns immutable row dataclasses. No module-level
connection — callers inject a :class:`Store` instance built once in the
composition root.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TOKEN_BYTES = 16
REPORT_ID_BYTES = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL UNIQUE,
    telegram_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    html_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS run_cache (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    preset TEXT NOT NULL,
    rating TEXT,
    rationale TEXT,
    duration_seconds REAL,
    html_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date, preset)
);
"""


@dataclass(frozen=True)
class User:
    user_id: int
    access_token: str
    telegram_name: str
    created_at: str


@dataclass(frozen=True)
class Report:
    report_id: str
    user_id: int
    ticker: str
    date: str
    html_path: str
    created_at: str


@dataclass(frozen=True)
class CachedRun:
    ticker: str
    date: str
    preset: str
    rating: Optional[str]
    rationale: str
    duration_seconds: float
    html_path: str
    created_at: str


class Store:
    """SQLite-backed store for the allowlist, daily usage, and reports."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()

    def init_db(self) -> None:
        """Create tables if they do not already exist."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- allowlist ----------------------------------------------------

    def is_allowed(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row is not None

    def unlock(self, user_id: int, telegram_name: str) -> str:
        """Add user_id to the allowlist (idempotent) and return their token."""
        with self._lock:
            row = self._conn.execute(
                "SELECT access_token FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(TOKEN_BYTES)
            created_at = _dt.datetime.now().isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO users (user_id, access_token, telegram_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, token, telegram_name, created_at),
            )
            self._conn.commit()
            return token

    def get_token(self, user_id: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT access_token FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    # -- daily usage cap ------------------------------------------------

    def check_and_increment_usage(
        self, user_id: int, cap: int, *, today: Optional[str] = None
    ) -> bool:
        """Atomically check and increment today's usage counter.

        Returns True (and increments) if the user is under `cap` for today;
        returns False without incrementing once `cap` is reached. Days are
        keyed by `today` (default: today's date), so the cap resets daily.
        """
        day = today or _dt.date.today().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM usage WHERE user_id = ? AND day = ?",
                (user_id, day),
            ).fetchone()
            count = row[0] if row else 0
            if count >= cap:
                return False
            if row:
                self._conn.execute(
                    "UPDATE usage SET count = count + 1 WHERE user_id = ? AND day = ?",
                    (user_id, day),
                )
            else:
                self._conn.execute(
                    "INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1)",
                    (user_id, day),
                )
            self._conn.commit()
        return True

    def usage_today(self, *, today: Optional[str] = None) -> list[tuple[int, int]]:
        """Return ``(user_id, count)`` pairs for everyone with usage today."""
        day = today or _dt.date.today().isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, count FROM usage WHERE day = ? ORDER BY user_id",
                (day,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    # -- reports ----------------------------------------------------------

    def add_report(self, user_id: int, ticker: str, date: str, html_path: str) -> str:
        report_id = secrets.token_urlsafe(REPORT_ID_BYTES)
        created_at = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO reports "
                "(report_id, user_id, ticker, date, html_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, user_id, ticker, date, html_path, created_at),
            )
            self._conn.commit()
        return report_id

    def get_report(self, report_id: str) -> Optional[Report]:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_id, user_id, ticker, date, html_path, created_at "
                "FROM reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return Report(*row) if row else None

    def list_reports_for_user(self, user_id: int, limit: int = 5) -> list[Report]:
        """Return a user's own reports, most recent first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT report_id, user_id, ticker, date, html_path, created_at "
                "FROM reports WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [Report(*row) for row in rows]

    # -- personal watchlist -------------------------------------------------

    def watchlist_add(self, user_id: int, symbol: str, *, max_size: int = 10) -> bool:
        """Add a symbol to a user's watchlist.

        Returns False (no-op) if the symbol is already present or the
        watchlist is already at ``max_size``; True if it was added.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM watchlist WHERE user_id = ? AND symbol = ?",
                (user_id, symbol),
            ).fetchone()
            if row:
                return False
            count = self._conn.execute(
                "SELECT COUNT(*) FROM watchlist WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if count >= max_size:
                return False
            added_at = _dt.datetime.now().isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO watchlist (user_id, symbol, added_at) VALUES (?, ?, ?)",
                (user_id, symbol, added_at),
            )
            self._conn.commit()
            return True

    def watchlist_remove(self, user_id: int, symbol: str) -> bool:
        """Remove a symbol from a user's watchlist. Returns whether it existed."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?",
                (user_id, symbol),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def watchlist_list(self, user_id: int) -> list[str]:
        """Return a user's watchlist symbols, oldest-added first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM watchlist WHERE user_id = ? ORDER BY added_at, rowid",
                (user_id,),
            ).fetchall()
        return [row[0] for row in rows]

    # -- run cache ------------------------------------------------------

    def get_cached_run(
        self, ticker: str, date: str, preset: str, max_age_seconds: int
    ) -> Optional[CachedRun]:
        """Return a cached run for (ticker, date, preset) if still fresh.

        ``max_age_seconds <= 0`` disables the cache (always returns None).
        """
        if max_age_seconds <= 0:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT ticker, date, preset, rating, rationale, duration_seconds, "
                "html_path, created_at FROM run_cache "
                "WHERE ticker = ? AND date = ? AND preset = ?",
                (ticker, date, preset),
            ).fetchone()
        if not row:
            return None
        cached = CachedRun(*row)
        age = (_dt.datetime.now() - _dt.datetime.fromisoformat(cached.created_at)).total_seconds()
        if age > max_age_seconds:
            return None
        return cached

    def upsert_cached_run(
        self,
        ticker: str,
        date: str,
        preset: str,
        rating: Optional[str],
        rationale: str,
        duration_seconds: float,
        html_path: str,
    ) -> None:
        created_at = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_cache "
                "(ticker, date, preset, rating, rationale, duration_seconds, html_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, date, preset, rating, rationale, duration_seconds, html_path, created_at),
            )
            self._conn.commit()
