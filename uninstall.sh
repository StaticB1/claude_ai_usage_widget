#!/usr/bin/env bash
set -euo pipefail

APP_ID="claude-usage-widget"
INSTALL_DIR="$HOME/.local/share/$APP_ID"

echo "╔══════════════════════════════════════════╗"
echo "║   Claude AI Usage Widget — Uninstaller   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Stop running instance
echo "▸ Stopping widget if running…"
pkill -f "claude_usage_widget.py" 2>/dev/null && echo "  ✓ Stopped" || echo "  (not running)"

# App files + venv
echo ""
echo "▸ Removing installed files…"
rm -rf "$INSTALL_DIR"
echo "  ✓ $INSTALL_DIR"

# Bin scripts + symlink
for f in \
    "$HOME/.local/bin/claude-usage-widget" \
    "$HOME/.local/bin/claude-widget-start" \
    "$HOME/.local/bin/claude-widget-stop"
do
    rm -f "$f" && echo "  ✓ $f"
done

# Desktop entries
rm -f "$HOME/.config/autostart/$APP_ID.desktop"   && echo "  ✓ autostart entry"
rm -f "$HOME/.local/share/applications/$APP_ID.desktop" && echo "  ✓ app launcher entry"

# Temp files
rm -f /tmp/claude-widget.log
rm -rf "/tmp/$APP_ID"
echo "  ✓ temp files"

# Config — ask, since the user may want to keep their account setup
echo ""
read -rp "▸ Remove config + account setup (~/.config/$APP_ID)? [y/N] " yn
case "${yn,,}" in
    y|yes)
        rm -rf "$HOME/.config/$APP_ID"
        echo "  ✓ Config removed"
        ;;
    *)
        echo "  Config preserved at ~/.config/$APP_ID"
        ;;
esac

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ Uninstall complete                   ║"
echo "╚══════════════════════════════════════════╝"
