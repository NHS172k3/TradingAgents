# TradingAgents automation layer

Daily scheduled watchlist analysis + Telegram alerts + weekly Gmail digest +
local Streamlit dashboard. Lives entirely in this folder; nothing in
`tradingagents/` or `cli/` is modified, so `git pull` from upstream keeps
working.

> **New machine?** Follow [SETUP.md](SETUP.md) for the complete from-scratch
> setup (clone, Python, Ollama, secrets, scheduling, verification).
> **Maintaining the bot?** See [FUTURE.md](FUTURE.md) for the service handoff
> checklist and next improvements.

## One-time setup

1. **Keep git clean** — add these lines to `.git/info/exclude` (NOT
   `.gitignore`; this file stays local):

   ```
   automation/
   .env
   ```

2. **Install this layer's extra dependencies** (on top of the upstream
   install) into the project environment:

   ```
   pip install -r automation/requirements.txt
   ```

3. **Secrets** — append the variables from `automation/env.example` to the
   repo-root `.env` (instructions for getting each value are in that file):
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD`.

4. **Edit the watchlist** — `automation/watchlist.yaml` (tickers, preset,
   notification toggles). Or use the dashboard's Watchlist page.

5. **Schedule it (Windows)** — from PowerShell:

   ```
   powershell -ExecutionPolicy Bypass -File automation\windows\register_tasks.ps1
   ```

   Registers "TradingAgents Daily" (Mon–Fri 17:30) and "TradingAgents Weekly
   Email" (Sun 18:00), both with run-when-missed. Adjust trigger times to
   your timezone — daily should fire after US market close (4pm ET).

## Commands

```
python -m automation.run_watchlist --dry-run        # validate config, no LLM calls
python -m automation.run_watchlist                  # full watchlist run
python -m automation.run_watchlist --ticker NVDA --preset cost_saver   # one ticker
python -m automation.notify_telegram                # send a test Telegram message
python -m automation.weekly_email --dry-run         # print the digest
python -m automation.calendar_check 2026-07-03      # trading-day check
streamlit run automation/dashboard.py               # dashboard
```

The dashboard's **Workflow** page documents the day-to-day workflow and shows
live setup status (secrets present, scheduled tasks registered).

## Where data lives

- `automation/logs/decisions.jsonl` — one line per ticker per run (this
  layer's own record).
- `automation/logs/run_YYYY-MM-DD.log` / `task_*.log` — run logs.
- `~/.tradingagents/logs/{TICKER}/{DATE}/reports/` — markdown reports
  (written by the runner, same layout as the upstream CLI).
- `~/.tradingagents/memory/trading_memory.md` — upstream decision/outcome
  log; pending entries resolve automatically the next time the same ticker
  runs, which is what feeds the dashboard's Performance page.

## Presets

Presets control analyst count and debate depth; **provider and model come
from `.env`**, never from presets.

- `standard` — all 4 analysts, full debate (~12 LLM calls/ticker).
- `cost_saver` — market+news analysts only, shorter debates (~8 calls/ticker;
  faster, which matters most on local models).

## Local models (Ollama)

The automation runs on local models via Ollama. Setup:

1. Install Ollama (`winget install Ollama.Ollama`) and pull the model:
   `ollama pull qwen3:8b` (fits an 8 GB GPU with the 16k context below).
2. Set the user env var `OLLAMA_CONTEXT_LENGTH=16384` — the default 4096
   silently truncates this graph's long analyst prompts.
3. In the repo-root `.env`:

   ```
   TRADINGAGENTS_LLM_PROVIDER=ollama
   TRADINGAGENTS_DEEP_THINK_LLM=qwen3:8b
   TRADINGAGENTS_QUICK_THINK_LLM=qwen3:8b
   ```

No API key is needed for ollama; the endpoint defaults to
`http://localhost:11434/v1`. Runs cost $0 but take longer (~5-15 min/ticker
on an 8 GB GPU). **Revert to OpenAI** by commenting out those three lines.

## Run as a shared service (Linux)

In addition to the scheduled Windows tasks above, `automation/service.py` runs
an **on-demand Telegram bot + hosted-report server** so other people can
request analyses by typing a ticker into Telegram and get back a summary plus
a link to the full report. This is designed to run unattended on a Linux
laptop with Ollama.

1. **Ollama** — install and run `ollama serve` as its own service (not managed
   by this layer); pull a model as in [Local models (Ollama)](#local-models-ollama)
   above.

2. **Python environment** — create a virtualenv and install the project:

   ```
   python -m venv .venv
   .venv/bin/pip install -e .
   ```

3. **Secrets** — copy `automation/env.example` into the repo-root `.env` and
   fill in the bot section: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_INVITE_CODE`,
   `REPORTS_PUBLIC_BASE_URL`, `BOT_DAILY_CAP`, `BOT_PRESET`,
   `REPORTS_WEB_HOST`/`REPORTS_WEB_PORT`, optional `BOT_DB_PATH`, and
   `BOT_ADMIN_USER_ID`.

4. **Cloudflare Tunnel** (optional but recommended) — exposes the local report
   server (bound to `127.0.0.1:8787`) on a public HTTPS URL without opening any
   inbound ports:

   ```
   cloudflared tunnel login
   cloudflared tunnel create tradingagents-reports
   cloudflared tunnel route dns tradingagents-reports <your-hostname>
   ```

   Set `REPORTS_PUBLIC_BASE_URL` to `https://<your-hostname>`. To try it
   without DNS setup, skip the install step for `cloudflared.service` and run
   `cloudflared tunnel --url http://127.0.0.1:8787` for a temporary
   `*.trycloudflare.com` URL instead.

5. **Install and start the service**:

   ```
   bash automation/linux/install.sh
   ```

   This installs `automation/linux/tradingagents-bot.service` and (if
   `cloudflared` is on `PATH`) `automation/linux/cloudflared.service` as
   systemd **user** units, enables them, and runs `loginctl enable-linger` so
   they keep running after logout and start on boot.

6. **Invite people** — share your `TELEGRAM_INVITE_CODE` with anyone you want
   to grant access. They message the bot `/start <code>` once to join the
   permanent allowlist, then send a ticker (e.g. `NVDA`) to queue an analysis.
   Each user is limited to `BOT_DAILY_CAP` analyses/day; one analysis runs at a
   time.

7. **Monitor** — as the admin (`BOT_ADMIN_USER_ID`), send `/status` to the bot
   for queue depth, today's usage, and recent log activity, or tail full logs
   with:

   ```
   journalctl --user -u tradingagents-bot -f
   journalctl --user -u cloudflared -f
   ```

**Security notes**: the report web server binds to `127.0.0.1` only — Cloudflare
Tunnel (or no tunnel at all) is the sole route in. Report links embed a
per-user access token; invite codes and tokens are compared with
`hmac.compare_digest`. Report HTML is sanitized with `bleach` before being
served. As the group grows, consider adding
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
in front of the tunnel for a login layer, and rotate `TELEGRAM_INVITE_CODE`
periodically.
