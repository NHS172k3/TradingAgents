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
- `store.py` — `Store` (SQLite at `automation/logs/service.db`):
  allowlist/tokens (`User`), daily usage caps, rendered `Report` records.
- `telegram_api.py` — `send_message` / `get_updates` / `_split_message`
  (4096-char chunking). `notify_telegram.send_message` now delegates here.
- `reports.py` — `render_to_html(report_dir)`: converts
  `complete_report.md` → sanitized (`bleach`) static `report.html`, once.
- `job_queue.py` — `JobQueue`/`Job`: single background worker (Ollama is
  serial); runs `run_one_ticker`, renders the report, records it in the
  store and in `decisions.jsonl`, posts the result back to Telegram.
- `bot.py` — `run_bot(...)`: long-polls Telegram, handles `/start <code>`
  (invite-code unlock), `/help`, ticker messages (enqueue with cap check),
  and owner-only `/status`.
- `web/server.py` — `create_app(store)`: FastAPI app serving
  `GET /report/{report_id}?token=...` (token-gated, `FileResponse` of the
  pre-rendered HTML) and `GET /healthz`. Bind `127.0.0.1` only.
- `service.py` — composition root: builds `ServiceConfig`, opens the
  `Store`, starts uvicorn in a background thread, starts the queue worker,
  runs the bot loop in the foreground. Entry point: `python -m
  automation.service`.
- `linux/` — `tradingagents-bot.service`, `cloudflared.service`,
  `install.sh` (systemd **user** units for running the above unattended).

## Flow: ticker → report link

```
Telegram "/start <code>"  → store.unlock() → permanent allowlist + access token
Telegram "NVDA"            → cap check → JobQueue.enqueue()
JobQueue worker             → run_one_ticker() → reports.render_to_html()
                            → store.add_report() → append_decision()
Reply to user                "<ticker> — <rating>\n<rationale excerpt>
                               Full report: {REPORTS_PUBLIC_BASE_URL}/report/{id}?token={token}"
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
- `TELEGRAM_INVITE_CODE` — shared secret for `/start <code>`.
- `REPORTS_PUBLIC_BASE_URL` — public HTTPS base for report links (Cloudflare
  Tunnel hostname).
- `BOT_DAILY_CAP` (default 5), `BOT_PRESET` (default `cost_saver`).
- `REPORTS_WEB_HOST` (default `127.0.0.1`), `REPORTS_WEB_PORT` (default 8787).
- `BOT_DB_PATH` (default `automation/logs/service.db`).
- `BOT_ADMIN_USER_ID` — Telegram user ID for owner-only `/status`.
- `TRADINGAGENTS_LLM_PROVIDER=ollama` (+ deep/quick think model vars) for
  local models; omit to use OpenAI.

## Security model

- **Access**: invite code (compared with `hmac.compare_digest`) → permanent
  per-user allowlist entry + `secrets.token_urlsafe(16)` access token.
  Non-allowlisted users can never enqueue work.
- **Rate limiting**: `BOT_DAILY_CAP` analyses/user/day (SQLite `usage`
  table, reset by date), single-worker queue caps total load.
- **Report links**: `report_id = secrets.token_urlsafe(8)`; viewing requires
  `?token=<user_token>` (403 on mismatch, 404 unknown id, 410 missing file).
- **Report content**: LLM-generated markdown is rendered once and sanitized
  with `bleach` before being served — no stored XSS.
- **Network**: FastAPI/uvicorn bound to `127.0.0.1`; Cloudflare Tunnel is the
  only public ingress; Ollama stays on localhost. No secrets in code — all
  via `.env` / systemd `EnvironmentFile`.
