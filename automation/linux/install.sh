#!/usr/bin/env bash
# Installs the TradingAgents on-demand bot + report service as systemd
# --user units, plus a Cloudflare Tunnel unit, and enables both so they
# survive logout and start on boot.
#
# Usage:
#   bash automation/linux/install.sh [tunnel-name]
#
# Prerequisites:
#   - `ollama serve` is already running as its own service (not managed here)
#   - a virtualenv exists at <repo>/.venv with this project installed:
#       python -m venv .venv && .venv/bin/pip install -e .
#   - <repo>/.env exists and is filled in (copy from automation/env.example)
#   - cloudflared is installed if you want the hosted-report tunnel
#
# Manage afterwards with:
#   systemctl --user status tradingagents-bot cloudflared
#   journalctl --user -u tradingagents-bot -f

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="${TRADINGAGENTS_VENV:-$REPO_ROOT/.venv}"
TUNNEL_NAME="${1:-tradingagents-reports}"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "ERROR: no python found at $VENV_DIR/bin/python" >&2
    echo "Create it with: python -m venv \"$VENV_DIR\" && \"$VENV_DIR/bin/pip\" install -e \"$REPO_ROOT\"" >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "ERROR: $REPO_ROOT/.env not found. Copy automation/env.example to .env and fill it in first." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"

sed \
    -e "s#__REPO_DIR__#$REPO_ROOT#g" \
    -e "s#__VENV_DIR__#$VENV_DIR#g" \
    "$REPO_ROOT/automation/linux/tradingagents-bot.service" > "$UNIT_DIR/tradingagents-bot.service"

if command -v cloudflared >/dev/null 2>&1; then
    CLOUDFLARED_BIN="$(command -v cloudflared)"
    sed \
        -e "s#__TUNNEL_NAME__#$TUNNEL_NAME#g" \
        -e "s#__CLOUDFLARED_BIN__#$CLOUDFLARED_BIN#g" \
        "$REPO_ROOT/automation/linux/cloudflared.service" > "$UNIT_DIR/cloudflared.service"
else
    echo "WARNING: cloudflared not found on PATH; skipping cloudflared.service." >&2
    echo "Install cloudflared, then re-run this script, or run a no-DNS tunnel manually:" >&2
    echo "  cloudflared tunnel --url http://127.0.0.1:8787" >&2
fi

systemctl --user daemon-reload
systemctl --user enable --now tradingagents-bot.service

if [ -f "$UNIT_DIR/cloudflared.service" ]; then
    systemctl --user enable --now cloudflared.service
fi

# Let user services keep running after logout and start at boot.
loginctl enable-linger "$(whoami)"

echo ""
echo "Installed. Check status with:"
echo "  systemctl --user status tradingagents-bot"
echo "  journalctl --user -u tradingagents-bot -f"
