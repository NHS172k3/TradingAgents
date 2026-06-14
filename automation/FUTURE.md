# Future Guide: On-demand Bot Service

This guide is the handoff for the Telegram bot + hosted report service.
Use it after the initial setup in `automation/README.md`.

## Current shape

- `automation/service.py` is the process entry point.
- `automation/config.py` owns env parsing and validation.
- `automation/store.py` keeps the allowlist, daily usage counters, and report
  records in SQLite.
- `automation/bot.py` long-polls Telegram, unlocks users with `/start <code>`,
  queues ticker requests, and exposes admin `/status`.
- `automation/job_queue.py` runs one ticker at a time, renders the report, and
  replies with a token-gated link.
- `automation/web/server.py` serves rendered reports from
  `/report/{report_id}?token=...`.
- `automation/linux/install.sh` installs systemd user units for the bot and,
  when available, Cloudflare Tunnel.

## Operating checklist

1. Keep `automation/.venv`, `automation/logs`, and `__pycache__` out of scans
   and commits. They are local/generated.
2. Set service env vars in the repo-root `.env`, using
   `automation/env.example` as the source of truth.
3. For Linux service runs, use a repo-root `.venv` and install with:

   ```bash
   python -m venv .venv
   .venv/bin/pip install -e .
   bash automation/linux/install.sh
   ```

4. Keep `REPORTS_WEB_HOST=127.0.0.1`; expose reports through Cloudflare Tunnel
   or another authenticated reverse tunnel.
5. Rotate `TELEGRAM_INVITE_CODE` after broad sharing, and keep
   `BOT_DAILY_CAP` low while using local Ollama models.

## Change checklist

Run these before trusting a change:

```bash
python -m pytest tests/test_config.py tests/test_store.py tests/test_telegram_api.py tests/test_reports.py tests/test_web_server.py
python -m automation.run_watchlist --dry-run
python -m automation.service
```

For `automation.service`, use test Telegram credentials and stop it after:

- `/healthz` returns `{"ok": true}` on the configured local port.
- `/start <code>` unlocks a test user.
- A ticker request is queued.
- Admin `/status` reports queue depth and today's usage.

Do not run a real ticker in verification unless Ollama/OpenAI credentials and
market-data access are ready; one run can take several minutes on local models.

## Next improvements

- Persist the Telegram update offset in SQLite if duplicate processing after a
  long outage becomes a problem.
- Add a cancel command for queued jobs before the worker starts them.
- Add per-user report history (`/reports`) using existing report rows.
- Put Cloudflare Access or another login layer in front of public report URLs
  if report links are shared outside a small trusted group.
- Add a periodic cleanup job for old rendered reports and old SQLite rows.
- Add integration tests with a fake Telegram API before changing bot dispatch.
