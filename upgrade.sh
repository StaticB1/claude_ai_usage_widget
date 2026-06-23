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

# ── Sweep legacy v1 widget (the rebrand changed every path) ──────────────────
# v1 was the single-file `claude-usage-widget`; v2 is `claude-token-tracker`
# with new dirs/binaries. Installing v2 doesn't touch v1, so a stale autostart
# entry would keep launching the dead widget alongside the new one.

echo ""
echo "▸ Removing legacy claude-usage-widget files (if present)…"
LEGACY_ID="claude-usage-widget"
legacy_found=false
for f in \
    "$HOME/.local/share/$LEGACY_ID" \
    "$HOME/.local/bin/$LEGACY_ID" \
    "$HOME/.local/bin/claude-widget-start" \
    "$HOME/.local/bin/claude-widget-stop" \
    "$HOME/.config/autostart/$LEGACY_ID.desktop" \
    "$HOME/.local/share/applications/$LEGACY_ID.desktop"; do
    if [ -e "$f" ]; then
        rm -rf "$f"
        legacy_found=true
    fi
done
if $legacy_found; then
    echo "  ✓ Cleared legacy widget files"
    # Leave ~/.config/claude-usage-widget alone — it may hold a pasted token.
    if [ -d "$HOME/.config/$LEGACY_ID" ]; then
        echo "  · Kept ~/.config/$LEGACY_ID (old config — delete manually to wipe)"
    fi
else
    echo "  ✓ No legacy files found"
fi

# ── Reinstall ────────────────────────────────────────────────────────────────

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
