# TradingAgents automation layer

Daily scheduled watchlist analysis + Telegram alerts + weekly Gmail digest +
local Streamlit dashboard. Lives entirely in this folder; nothing in
`tradingagents/` or `cli/` is modified, so `git pull` from upstream keeps
working.

> **New machine?** Follow [SETUP.md](SETUP.md) for the complete from-scratch
> setup (clone, Python, Ollama, secrets, scheduling, verification).

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
