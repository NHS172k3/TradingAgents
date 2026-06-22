# automation/ SWE practices audit — findings

Scope: the full `automation/` folder (bot service stack: `bot.py`,
`store.py`, `job_queue.py`, `telegram_api.py`, `web/server.py`,
`service.py`, `config.py`; plus the older scheduled-run stack:
`run_watchlist.py`, `weekly_email.py`, `dashboard.py`, `calendar_check.py`,
`settings.py`, `runner.py`, `upstream.py`, `notify_telegram.py`,
`runlog.py`; plus `linux/` deployment units and `requirements.txt`).

This is a findings report, not a design — nothing here is implemented.
Each finding names the gap, why it matters, and the industry-standard
fix, so you can pick which become specs/plans next.

## What's already good (don't change)

Worth stating explicitly so the findings below aren't read as "this
codebase is sloppy" — it isn't:

- **Dependency injection throughout**: `store`/`config`/`job_queue` are
  passed as params, never module globals. Easy to test, easy to fake.
- **Single upstream facade** (`upstream.py`): isolates all coupling to
  `tradingagents`/`cli` behind one file, with lazy imports so `--dry-run`
  and the dashboard never pay for loading the LLM stack.
- **Fail-soft network/IO boundaries**: Telegram calls, report rendering,
  job callbacks all catch and log rather than crashing a shared loop.
- **CI already does the right things**: `pytest` across 4 Python versions,
  `ruff` strict over the full repo, a clean-install smoke test. This audit
  doesn't need to introduce tooling, just close coverage/practice gaps
  within what's already enforced.
- **`hmac.compare_digest`** used correctly for both the invite code and
  report-token comparisons (timing-safe) — this is the right primitive,
  just applied to secrets that have their own gaps (below).

## Findings

### P0 — security-relevant, worth fixing soon

**1. Bearer tokens stored in plaintext in SQLite**
`store.py`'s `users.access_token` and the report `token` flow are stored
and compared as raw strings. Anyone who reads `service.db` (backup,
misconfigured permissions, a future bug) gets live, usable credentials
for every user, forever.
*Industry standard*: store only a hash of the token (SHA-256 is the norm
for high-entropy random tokens — this is exactly how GitHub/Stripe/AWS
store API keys: hash at rest, look up by hash, never persist the raw
value after issuing it once). bcrypt/argon2 are for low-entropy
human-chosen secrets (passwords) and are unnecessary overhead here.

**2. No file permissions on the SQLite DB**
`Store.__init__` calls `sqlite3.connect(path)` with no `os.chmod` — the
file inherits the process umask (commonly `0644`, world-readable on a
shared box).
*Industry standard*: `os.chmod(db_path, 0o600)` right after creation —
least-privilege for any file holding credentials at rest, same principle
as `chmod 600 ~/.ssh/id_rsa`.

**3. No rate limiting on the public web endpoint**
`web/server.py`'s `/report/{id}` has zero throttling — only the bot's
per-user daily cap limits *new analyses*, but viewing/re-viewing existing
reports (or guessing tokens, even though they're high-entropy enough to
be impractical to brute force) is unlimited.
*Industry standard*: either (a) a rate-limiting middleware library for
FastAPI (`slowapi`, built on the same algorithm as Cloudflare/AWS API
Gateway: token bucket per client key) or (b) — since Cloudflare Tunnel is
already the ingress — Cloudflare's own free dashboard rate-limiting rules,
which block abusive traffic before it reaches your box at all. (b) is
less code and arguably more "industry standard" for anything sitting
behind Cloudflare already.

**4. `bot.py` has zero direct unit tests**
It's the most logic-dense, security-relevant module (invite-code
unlocking, allowlist checks, command dispatch) and the only one in the
service stack untested. `store.py`/`job_queue.py`/`telegram_api.py`/
`web/server.py` all have tests; `bot.py` doesn't.
*Industry standard*: dependency-injected fakes (a fake `Store`/`JobQueue`/
`telegram_api.send_message`) + table-driven tests per command — the
existing code structure (everything takes `store`/`config`/`job_queue` as
params) already makes this straightforward, no refactor needed first.

### P1 — robustness / correctness gaps

**5. Invite code is a single static, non-expiring shared secret**
`TELEGRAM_INVITE_CODE` in `.env` never expires and has unlimited uses
until manually rotated (FUTURE.md already flags rotation as a manual
chore). One leak grants indefinite access.
*Industry standard*: time-limited, capped-use invite tokens generated
per-invitee and stored in the DB (the same pattern as GitHub org invites
or magic sign-up links) — `invite(code, expires_at, max_uses, uses)` —
rather than one long-lived value in an env var.

**6. No per-minute/burst throttling, only a per-day cap**
`store.check_and_increment_usage` enforces `BOT_DAILY_CAP`/day but a user
(or a buggy client) can submit all 5 in one second, monopolizing the
single-worker queue.
*Industry standard*: a token-bucket or sliding-window limiter per
`user_id` at message-ingestion time, layered under the daily cap — same
algorithm class as #3, applied to the Telegram side instead of the web
side.

**7. No schema migration story**
`store.py`'s schema is `CREATE TABLE IF NOT EXISTS` only — fine for
additive changes (which is all we've done so far), but there's no path
for a column rename/drop/backfill without hand-written one-off scripts.
*Industry standard*: the lightweight pattern small SQLite apps use — a
`schema_version` table + an ordered list of migration functions applied
on startup (the same idea as `golang-migrate`/Sqitch, just hand-rolled).
Pulling in SQLAlchemy+Alembic would be disproportionate for one SQLite
file at this scale.

**8. Missing index for the new per-user query**
`list_reports_for_user` (added this session) filters `reports` by
`user_id` with no index beyond the `report_id` primary key — a full table
scan that gets slower as reports accumulate.
*Industry standard*: `CREATE INDEX idx_reports_user_id ON
reports(user_id, created_at)` — standard practice: index any column used
in a `WHERE`/`ORDER BY` on a table expected to grow past a few thousand
rows.

**9. No foreign key constraints**
`reports.user_id`, `usage.user_id`, `watchlist.user_id` aren't declared
as `FOREIGN KEY` references to `users.user_id` (SQLite supports this with
`PRAGMA foreign_keys=ON`).
*Industry standard*: declare the FKs and enable the pragma per connection
— mostly a documentation/integrity safety net here (cascade deletes
aren't needed at this scale), but it's the standard way to make table
relationships explicit and catch orphaned-row bugs early.

### P2 — scale-ahead / nice-to-have

**10. No retention/cleanup job**
`reports`, `run_cache` rows and the underlying rendered HTML files (and
report directories under `upstream.save_reports`) accumulate forever.
Already flagged in `FUTURE.md`.
*Industry standard*: a scheduled job (cron or a startup check in
`service.py`) that deletes rows/files past a retention window (e.g. 90
days) — standard log/data retention practice.

**11. Single shared SQLite connection + `threading.Lock`**
Worth calling out as a **deliberate, correct** choice at this scale, not
a finding — `store.py`'s docstring already says so. If usage ever grows
enough to need real concurrent writers, the standard next step is
Postgres + a pooled client (`psycopg`/SQLAlchemy), not WAL-mode tuning on
SQLite. Not warranted yet; listed only so it's not "fixed" prematurely.

**12. `bot.py` is growing (365 lines, 6 commands now)**
Still single-purpose-per-function and readable, not yet a problem.
*Industry standard*: if more commands get added, split into
`bot_commands.py` (the `_handle_*` functions) + `bot.py` (dispatch loop +
`run_bot`) — standard "router vs. handlers" separation once a dispatch
file outgrows being readable in one pass.

## Suggested order if you want to act on these

1. **#1 (hash tokens) + #2 (file perms)** — smallest, highest-leverage
   security fix, no behavior change for users.
   This naturally becomes the "auth" half of the auth+rate-limiting spec
   you flagged earlier.
2. **#3 + #6 (rate limiting, web + bot side)** — the other half of that
   spec.
3. **#7 + #8 + #9 (storage/schema)** — the DB redesign spec.
4. **#4 (bot.py tests)** — can happen in parallel with any of the above;
   doesn't block anything.
5. **#5, #10, #12** — lower urgency, fold in opportunistically.

Items #11 is explicitly *not* a to-do — included so it doesn't get
"fixed" without reason later.
