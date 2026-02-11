#!/usr/bin/env bash
set -euo pipefail

APP_ID="claude-usage-widget"
INSTALL_DIR="$HOME/.local/share/$APP_ID"
BIN_LINK="$HOME/.local/bin/claude-usage-widget"

echo "╔══════════════════════════════════════════╗"
echo "║   Claude AI Usage Widget — Installer     ║"
echo "╚══════════════════════════════════════════╝"

# ── Dependencies ────────────────────────────────────────────────────────────

echo ""
echo "▸ Checking dependencies…"

MISSING=()

# Python 3
if ! command -v python3 &>/dev/null; then
    MISSING+=("python3")
fi

# GIR packages
python3 -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('AppIndicator3','0.1'); gi.require_version('Notify','0.7')" 2>/dev/null || {
    MISSING+=("gir1.2-appindicator3-0.1" "gir1.2-notify-0.7")
}

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  ✗ Missing packages: ${MISSING[*]}"
    echo ""
    echo "  Install them with:"
    echo "    sudo apt install python3 gir1.2-appindicator3-0.1 gir1.2-notify-0.7 python3-gi"
    echo ""
    read -rp "  Install now? [Y/n] " yn
    case "${yn,,}" in
        n|no) echo "  Aborted."; exit 1 ;;
        *)
            sudo apt update
            sudo apt install -y python3 python3-gi gir1.2-appindicator3-0.1 gir1.2-notify-0.7
            ;;
    esac
fi

echo "  ✓ All dependencies satisfied"

# ── Install files ───────────────────────────────────────────────────────────

echo ""
echo "▸ Installing to $INSTALL_DIR …"

mkdir -p "$INSTALL_DIR"
cp claude_usage_widget.py "$INSTALL_DIR/claude_usage_widget.py"
chmod +x "$INSTALL_DIR/claude_usage_widget.py"

mkdir -p "$(dirname "$BIN_LINK")"
ln -sf "$INSTALL_DIR/claude_usage_widget.py" "$BIN_LINK"

# Create wrapper scripts for easy start/stop
cat > "$HOME/.local/bin/claude-widget-start" <<'EOFSTART'
#!/bin/bash
# Start Claude Usage Widget with clean environment
env -i \
  HOME="$HOME" \
  DISPLAY="$DISPLAY" \
  DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
  PATH="/usr/local/bin:/usr/bin:/bin" \
  /usr/bin/python3 ~/.local/share/claude-usage-widget/claude_usage_widget.py > /tmp/claude-widget.log 2>&1 &

sleep 1
if ps aux | grep -q '[c]laude_usage_widget'; then
    echo "✓ Claude widget started"
else
    echo "✗ Failed to start. Check /tmp/claude-widget.log"
    exit 1
fi
EOFSTART

cat > "$HOME/.local/bin/claude-widget-stop" <<'EOFSTOP'
#!/bin/bash
# Stop Claude Usage Widget
if pkill -f claude_usage_widget.py 2>/dev/null; then
    echo "✓ Claude widget stopped"
else
    echo "✗ Widget not running"
    exit 1
fi
EOFSTOP

chmod +x "$HOME/.local/bin/claude-widget-start"
chmod +x "$HOME/.local/bin/claude-widget-stop"

echo "  ✓ Installed"

# ── Desktop autostart entry ─────────────────────────────────────────────────

echo ""
echo "▸ Creating autostart entry…"

AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"

cat > "$AUTOSTART_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Widget
Comment=Shows Claude AI usage in system tray
Exec=env -u LD_LIBRARY_PATH PATH="/usr/local/bin:/usr/bin:/bin" /usr/bin/python3 $INSTALL_DIR/claude_usage_widget.py
Icon=network-transmit-receive
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF

echo "  ✓ Autostart enabled"

# ── Desktop application entry ───────────────────────────────────────────────

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Widget
Comment=Shows Claude AI usage in system tray
Exec=$BIN_LINK
Icon=network-transmit-receive
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

echo "  ✓ Application entry created"

# ── Check for existing token ────────────────────────────────────────────────

echo ""
CRED_FILE="$HOME/.claude/.credentials.json"
if [ -f "$CRED_FILE" ]; then
    echo "  ✓ Found Claude Code credentials — token will be auto-detected"
else
    echo "  ⚠ No Claude Code credentials found at $CRED_FILE"
    echo "    You'll be prompted to enter your OAuth token on first run."
    echo ""
    echo "    To get your token:"
    echo "    Option A: Install Claude Code → 'claude login' → token saved automatically"
    echo "    Option B: Browser DevTools → Network tab → filter 'api.anthropic.com'"
    echo "              → copy the Authorization: Bearer sk-ant-oat01-... header value"
fi

# ── Done ────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ Installation complete!               ║"
echo "║                                          ║"
echo "║   Start:  claude-widget-start            ║"
echo "║   Stop:   claude-widget-stop             ║"
echo "║   (or reboot — it autostarts)            ║"
echo "╚══════════════════════════════════════════╝"
