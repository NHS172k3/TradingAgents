# TradingAgents

Multi-agent LLM financial trading framework (`tradingagents/`, `cli/` — upstream,
keep unmodified so `git pull` stays clean) plus a separate `automation/` layer
that adds scheduled runs, notifications, a dashboard, and an on-demand
Telegram bot with hosted reports. Everything project-specific lives under
`automation/`.

## automation/ at a glance

**Scheduled (Windows, existing)**
- `run_watchlist.py` — runs `automation/watchlist.yaml` tickers via
  `runner.run_one_ticker` (which calls `upstream.build_graph`/`propagate`/
  `save_reports`), appends to `automation/logs/decisions.jsonl`, sends a
  Telegram alert and (on failure) notifications.
- `weekly_email.py` — owner-only Gmail digest of the week's decisions +
  resolved outcomes. **Unchanged by the bot work below.**
- `dashboard.py` — Streamlit app (Overview, Performance, History, Reports,
  Watchlist, Workflow pages).
- `calendar_check.py` — NYSE trading-day helpers.
- `settings.py` — `TickerSpec`, `Watchlist`, `PRESETS` (`standard`,
  `cost_saver`).

**On-demand bot + hosted reports (new)**
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
- `reports.py` — `render_to_html(report_dir)`: converts
  `complete_report.md` → sanitized (`bleach`) static `report.html`, once.
- `job_queue.py` — `JobQueue`/`Job`: single background worker (Ollama is
  serial); runs `run_one_ticker`, renders the report, records it in the
  store and in `decisions.jsonl`, posts the result back to Telegram.
- `bot.py` — `run_bot(...)`: long-polls Telegram, handles `/start <code>`
  (DB-backed or bootstrap-env-var invite unlock), `/help`, `/cancel`,
  `/history`, `/watchlist`, ticker messages (enqueue with daily-cap +
  per-user burst-rate check), owner-only `/status` and `/invite`. Replies
  are sent as HTML with escaped dynamic text and signed report links.
- `web/server.py` — `create_app(store, config)`: FastAPI app serving
  `GET /report/{report_id}?token=...` (signed-token verification via
  `automation.tokens`, rate-limited 30/min/client via `slowapi`) and
  `GET /healthz`. Bind `127.0.0.1` only.
- `service.py` — composition root: builds `ServiceConfig`, opens the
  `Store`, starts uvicorn in a background thread, starts the queue worker,
  runs the bot loop in the foreground. Entry point: `python -m
  automation.service`.
- `linux/` — `tradingagents-bot.service`, `cloudflared.service`,
  `install.sh` (systemd **user** units for running the above unattended).

## Flow: ticker → report link

```
Telegram "/start <code>"  → store.consume_invite() or bootstrap env code
                           → store.unlock() → permanent allowlist
Telegram "NVDA"            → daily cap + per-user burst-rate check
                           → JobQueue.enqueue()
JobQueue worker             → run_one_ticker() → reports.render_to_html()
                            → store.add_report() → append_decision()
Reply to user (HTML)         "<b>TICKER</b> — rating\n<escaped rationale>
                               📄 <a href=...>Full report</a>
                               Link expires <date> (7 days)"
```

## Running it

- **Windows (scheduled daily/weekly)**:
  `powershell -ExecutionPolicy Bypass -File automation\windows\register_tasks.ps1`
- **Linux (shared on-demand service)**: `ollama serve` running, `.venv` with
  `pip install -e .`, repo-root `.env` from `automation/env.example`, then
  `bash automation/linux/install.sh [tunnel-name]`. See
  `automation/README.md` → "Run as a shared service (Linux)" for the full
  walkthrough (systemd units, Cloudflare Tunnel, invite codes, `/status`), and
  `automation/FUTURE.md` for the service handoff and next-step checklist.

## Key env vars (see `automation/env.example`)

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — existing alerts.
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` — weekly digest.
- `TELEGRAM_INVITE_CODE` — bootstrap-only shared secret for `/start
  <code>`; ordinary invites are issued via the admin's `/invite` command
  and stored in the DB with expiry/use limits.
- `REPORTS_SIGNING_KEY` — secret key signing report-link tokens
  (`automation/tokens.py`); rotating it invalidates all outstanding links.
- `REPORTS_PUBLIC_BASE_URL` — public HTTPS base for report links (Cloudflare
  Tunnel hostname).
- `BOT_DAILY_CAP` (default 5), `BOT_PRESET` (default `cost_saver`).
- `REPORTS_WEB_HOST` (default `127.0.0.1`), `REPORTS_WEB_PORT` (default 8787).
- `BOT_DB_PATH` (default `automation/logs/service.db`).
- `BOT_ADMIN_USER_ID` — Telegram user ID for owner-only `/status`.
- `TRADINGAGENTS_LLM_PROVIDER=ollama` (+ deep/quick think model vars) for
  local models; omit to use OpenAI.

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
