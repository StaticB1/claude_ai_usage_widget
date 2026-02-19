#!/usr/bin/env bash
set -euo pipefail

echo "╔══════════════════════════════════════════╗"
echo "║   Claude AI Usage Widget — Upgrader      ║"
echo "╚══════════════════════════════════════════╝"

# ── Check we're in the repo ──────────────────────────────────────────────────

if [ ! -f "claude_usage_widget.py" ] || [ ! -f "install.sh" ]; then
    echo "✗ Run this script from inside the cloned repository directory."
    exit 1
fi

# ── Pull latest ──────────────────────────────────────────────────────────────

echo ""
echo "▸ Fetching latest version from GitHub…"
git pull

# ── Stop running widget ──────────────────────────────────────────────────────

echo ""
echo "▸ Stopping widget if running…"
if pkill -f claude_usage_widget.py 2>/dev/null; then
    echo "  ✓ Stopped"
    sleep 1
else
    echo "  ✓ Widget was not running"
fi

# ── Reinstall ────────────────────────────────────────────────────────────────

echo ""
echo "▸ Installing updated files…"
bash install.sh

# ── Restart widget ───────────────────────────────────────────────────────────

echo ""
echo "▸ Restarting widget…"
bash "$HOME/.local/bin/claude-widget-start"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ Upgrade complete!                    ║"
echo "╚══════════════════════════════════════════╝"
