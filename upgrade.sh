#!/usr/bin/env bash
set -euo pipefail

echo "╔══════════════════════════════════════════╗"
echo "║   Claude Usage Widget & Token Tracker    ║"
echo "║   Upgrader                               ║"
echo "╚══════════════════════════════════════════╝"

# ── Check we're in the repo ──────────────────────────────────────────────────
# v2 markers: the installer and the cct/ package. (The old single-file widget
# script may still be present at the repo root, so don't key off that.)

if [ ! -f "install.sh" ] || [ ! -d "cct" ]; then
    echo "✗ Run this script from inside the cloned repository directory."
    echo "  (Expected install.sh and the cct/ package here.)"
    exit 1
fi

# ── Pull latest ──────────────────────────────────────────────────────────────

echo ""
echo "▸ Fetching latest version from GitHub…"
git pull

# ── Stop any running widget (old single-file OR new tracker) ─────────────────

echo ""
echo "▸ Stopping any running widget…"
stopped=false
for proc in claude_usage_widget.py claude_token_tracker.py; do
    if pkill -f "$proc" 2>/dev/null; then
        stopped=true
    fi
done
if $stopped; then
    echo "  ✓ Stopped"
    sleep 1
else
    echo "  ✓ Nothing was running"
fi

# ── Reinstall ────────────────────────────────────────────────────────────────
# install.sh also sweeps any leftover legacy claude-usage-widget files (old
# dir, binaries, and autostart entry), so the dead widget can't keep launching
# alongside the new app.

echo ""
echo "▸ Installing updated files…"
bash install.sh

# ── Restart widget ───────────────────────────────────────────────────────────

echo ""
echo "▸ Restarting widget…"
GUI_BIN="$HOME/.local/bin/claude-token-tracker"
if [ -x "$GUI_BIN" ]; then
    # Detach so the GUI keeps running after this script exits, and never let a
    # launch failure (e.g. a headless / CLI-only box) abort a successful upgrade.
    nohup "$GUI_BIN" >/dev/null 2>&1 < /dev/null &
    disown 2>/dev/null || true
    echo "  ✓ Started claude-token-tracker"
else
    echo "  ! GUI launcher not found at $GUI_BIN."
    echo "    If this is a CLI-only (pip) install, use the 'ctt' command instead."
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ Upgrade complete!                    ║"
echo "╚══════════════════════════════════════════╝"
