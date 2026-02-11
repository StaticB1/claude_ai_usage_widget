#!/usr/bin/env bash
set -euo pipefail

APP_ID="claude-usage-widget"
INSTALL_DIR="$HOME/.local/share/$APP_ID"
BIN_LINK="$HOME/.local/bin/claude-usage-widget"

echo "▸ Removing Claude Usage Widget…"

# Kill running instance
pkill -f "claude_usage_widget.py" 2>/dev/null || true

rm -rf "$INSTALL_DIR"
rm -f "$BIN_LINK"
rm -f "$HOME/.local/bin/claude-widget-start"
rm -f "$HOME/.local/bin/claude-widget-stop"
rm -f "$HOME/.config/autostart/$APP_ID.desktop"
rm -f "$HOME/.local/share/applications/$APP_ID.desktop"

# Optionally remove config (preserves token)
read -rp "  Remove config (~/.config/$APP_ID)? [y/N] " yn
case "${yn,,}" in
    y|yes) rm -rf "$HOME/.config/$APP_ID"; echo "  ✓ Config removed" ;;
    *) echo "  ✓ Config preserved" ;;
esac

echo "  ✓ Uninstalled"
