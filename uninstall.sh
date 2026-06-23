#!/usr/bin/env bash
set -euo pipefail

# Single uninstall entry point for this repo.
#
# The repo now ships the Claude Token Tracker, whose canonical uninstaller is
# `install.sh --uninstall`. This script delegates to it, then sweeps any
# leftovers from the original `claude-usage-widget` so people upgrading from
# the old single-file widget don't leave a stale copy behind.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "▸ Uninstalling…"

# 1) Remove the token tracker via the maintained uninstaller.
if [ -f "$SCRIPT_DIR/install.sh" ]; then
    bash "$SCRIPT_DIR/install.sh" --uninstall
else
    echo "! install.sh not found next to uninstall.sh — skipping tracker removal" >&2
fi

# 2) Sweep legacy claude-usage-widget remnants (pre-upgrade installs).
LEGACY_ID="claude-usage-widget"
pkill -f "claude_usage_widget.py" 2>/dev/null || true
rm -rf "$HOME/.local/share/$LEGACY_ID"
rm -f  "$HOME/.local/bin/$LEGACY_ID" \
       "$HOME/.local/bin/claude-widget-start" \
       "$HOME/.local/bin/claude-widget-stop"
rm -f  "$HOME/.config/autostart/$LEGACY_ID.desktop"
rm -f  "$HOME/.local/share/applications/$LEGACY_ID.desktop"
echo "  ✓ Cleared any legacy $LEGACY_ID files"

# Leave the legacy widget config (~/.config/claude-usage-widget) untouched —
# it may hold a pasted token. Remove it manually if you want a clean wipe.
if [ -d "$HOME/.config/$LEGACY_ID" ]; then
    echo "  · Kept ~/.config/$LEGACY_ID (delete manually to wipe widget config)"
fi

echo "  ✓ Uninstalled"
