#!/usr/bin/env bash
# Runs a Cloudflare *quick* tunnel (no account/domain needed) for the
# TradingAgents report server, captures the random *.trycloudflare.com URL,
# writes it into the repo-root .env as REPORTS_PUBLIC_BASE_URL, and restarts
# the bot so report links use the current URL. Stays in the foreground so it
# can be the systemd service main process.
#
# Used by the cloudflared-quick.service systemd --user unit. The named-tunnel
# alternative (stable hostname, needs a Cloudflare domain) is the shipped
# cloudflared.service instead.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
PORT="${REPORTS_WEB_PORT:-8787}"
LOG="$(mktemp)"

cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate >"$LOG" 2>&1 &
CF_PID=$!
trap 'kill "$CF_PID" 2>/dev/null || true' EXIT

URL=""
for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 2
done

if [ -z "$URL" ]; then
    echo "quick_tunnel: failed to obtain a trycloudflare URL after 60s" >&2
    cat "$LOG" >&2
    exit 1
fi

echo "quick_tunnel: REPORTS_PUBLIC_BASE_URL=$URL"
if grep -q '^REPORTS_PUBLIC_BASE_URL=' "$ENV_FILE"; then
    sed -i "s#^REPORTS_PUBLIC_BASE_URL=.*#REPORTS_PUBLIC_BASE_URL=${URL}#" "$ENV_FILE"
else
    printf '\nREPORTS_PUBLIC_BASE_URL=%s\n' "$URL" >>"$ENV_FILE"
fi

# Pick up the new URL in the bot (EnvironmentFile is re-read on restart).
systemctl --user restart tradingagents-bot.service || true

# Surface cloudflared output to journald and keep it in the foreground.
tail -f "$LOG" &
wait "$CF_PID"
