# Setting up the automation layer on a new machine (Windows)

Complete from-scratch setup. Assumes nothing but Windows 10/11 and an
NVIDIA GPU (8 GB+ VRAM) if you want local models.

## 1. Clone

```powershell
git clone -b automation https://github.com/NHS172k3/TradingAgents.git
cd TradingAgents
```

The `automation` branch = upstream `main` + the `automation/` overlay.
To pull upstream updates later:

```powershell
git remote add upstream https://github.com/TauricResearch/TradingAgents.git
git fetch upstream
git merge upstream/main
```

## 2. Python + dependencies

Install Python 3.11–3.13 (python.org or `winget install Python.Python.3.13`),
then from the repo root:

```powershell
pip install -e .                              # upstream package + its deps
pip install -r automation/requirements.txt   # automation extras (streamlit etc.)
```

Tell the Task Scheduler wrappers which Python to use (they don't trust PATH):

```powershell
[Environment]::SetEnvironmentVariable("TRADINGAGENTS_PYTHON", (Get-Command python).Source, "User")
```

(Alternative: create a `.venv` in the repo root — the wrappers find that too.)

## 3. Local models (Ollama)

```powershell
winget install Ollama.Ollama
ollama pull qwen3:8b
[Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "16384", "User")
```

`qwen3:8b` (Q4, ~5.2 GB) fits an 8 GB GPU with the 16k context. The context
var matters: Ollama's 4096 default silently truncates the long analyst
prompts. Restart Ollama after setting it.

Skipping local models? Omit this step and the `TRADINGAGENTS_*` lines below —
runs then use OpenAI via `OPENAI_API_KEY`.

## 4. Secrets — `.env` in the repo root

Create `.env` (never commit it; it's gitignored) with:

```
# Telegram: create a bot via @BotFather (/newbot) for the token; send your
# bot a message, then find chat_id at
# https://api.telegram.org/bot<TOKEN>/getUpdates
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Gmail digest: needs 2FA enabled; create an app password at
# https://myaccount.google.com/apppasswords
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=...

# Only needed for cloud runs (or as fallback if you disable local models)
OPENAI_API_KEY=...

# Local models via Ollama (comment out these 3 lines to use OpenAI instead)
TRADINGAGENTS_LLM_PROVIDER=ollama
TRADINGAGENTS_DEEP_THINK_LLM=qwen3:8b
TRADINGAGENTS_QUICK_THINK_LLM=qwen3:8b
```

The weekly digest goes to `GMAIL_ADDRESS` unless `email.to` is set in
`automation/watchlist.yaml`.

## 5. Verify before scheduling

```powershell
python -m automation.run_watchlist --dry-run    # config + trading-day logic
python -m automation.notify_telegram            # test Telegram message
python -m automation.weekly_email --dry-run     # prints digest, sends nothing
python -m automation.run_watchlist --ticker NVDA   # one real end-to-end run
```

Expect roughly 5–15 min/ticker on an 8 GB GPU (vs ~1–3 min on cloud models).

## 6. Schedule it

```powershell
powershell -ExecutionPolicy Bypass -File automation\windows\register_tasks.ps1
```

Registers **TradingAgents Daily** (Mon–Fri 17:30) and **TradingAgents Weekly
Email** (Sun 18:00), both run-when-missed. The 17:30 default is tuned for
UTC+8 (Singapore): it runs after the most recent US close and ~4 h before the
next US open. In a different timezone, pick any time between US close and the
next open and edit the triggers in `register_tasks.ps1`.

## 7. Dashboard

```powershell
streamlit run automation/dashboard.py
```

The **Workflow** page shows live setup status (secrets present, Ollama
reachable, scheduled tasks registered) — if everything there is ✅, the
machine is fully set up. Day-to-day usage is documented on that page.
