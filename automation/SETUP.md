# Setting up the automation layer on a new machine (Windows)

Complete from-scratch setup. Assumes nothing but Windows 10/11 and an
NVIDIA GPU (8 GB+ VRAM) if you want local models.

> **On Linux?** The PowerShell commands below have direct equivalents; the
> Linux-specific differences (venv package, Ollama as a root systemd service,
> setting `OLLAMA_CONTEXT_LENGTH` via a systemd drop-in) are called out in
> **[Linux notes](#linux-notes)** at the bottom. For the unattended shared
> bot service, see `automation/README.md` → "Run as a shared service (Linux)".

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

## Linux notes

The steps above are written for Windows. On Linux (verified on Ubuntu 24.04,
Python 3.12, NVIDIA RTX 2000 Ada / 8 GB) the differences are:

**Step 2 — Python environment.** Debian/Ubuntu's system Python ships without
the `venv` bootstrap, so `python3 -m venv .venv` fails with an
`ensurepip is not available` error. Two options:

```bash
# Option A — install the venv package (needs sudo), then the standard flow:
sudo apt-get install -y python3.12-venv
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install -r automation/requirements.txt

# Option B — use uv (no sudo; the repo already ships a uv.lock):
uv venv .venv --python 3.12
VIRTUAL_ENV=.venv uv pip install -e .
VIRTUAL_ENV=.venv uv pip install -r automation/requirements.txt
```

Prefix later commands with the venv, e.g.
`.venv/bin/python -m automation.run_watchlist --dry-run`.

**Step 3 — Ollama + context length.** Install from
https://ollama.com/download/linux (`curl -fsSL https://ollama.com/install.sh | sh`),
which registers Ollama as a **root** systemd service (`ollama.service`) and
keeps it running. Pull the model with `ollama pull qwen3:8b`.

Because the server runs as root, `OLLAMA_CONTEXT_LENGTH` cannot be set as a
plain user env var — it must go into the service environment via a systemd
drop-in (needs sudo):

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=16384"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Verify it took effect — load the model and check the `CONTEXT` column:

```bash
ollama run qwen3:8b "hi" >/dev/null && ollama ps   # CONTEXT should read 16384
```

**VRAM fit (8 GB).** At the default 4k context qwen3:8b loads fully on GPU
(~5.6 GB). At 16k context the KV cache grows and the total approaches the 8 GB
ceiling; with a desktop/Xorg also using VRAM, Ollama may offload a few layers
to CPU (`ollama ps` shows e.g. `90% GPU / 10% CPU`). That still works, just
slower — budget toward the upper end of the 5–15 min/ticker range. If it
offloads heavily, close GPU-using desktop apps or drop
`OLLAMA_CONTEXT_LENGTH` to `12288`.

**Step 5 — verify.** Same commands, run through the venv:

```bash
.venv/bin/python -m automation.run_watchlist --dry-run
.venv/bin/python -m automation.run_watchlist --ticker NVDA --preset cost_saver
```

**Step 6 — scheduling.** The Windows Task Scheduler script does not apply.
Use cron or systemd timers, or run the on-demand bot service documented in
`automation/README.md` → "Run as a shared service (Linux)".
