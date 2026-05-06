#!/usr/bin/env bash
set -euo pipefail

APP_ID="claude-usage-widget"
INSTALL_DIR="$HOME/.local/share/$APP_ID"
BIN_LINK="$HOME/.local/bin/claude-usage-widget"

echo "╔══════════════════════════════════════════╗"
echo "║   Claude AI Usage Widget — Installer     ║"
echo "╚══════════════════════════════════════════╝"

# ── Python — detect pyenv vs system ─────────────────────────────────────────

echo ""
echo "▸ Detecting Python environment…"

PYTHON=""

if command -v pyenv &>/dev/null; then
    # Resolve pyenv's active python binary
    PYTHON=$(pyenv which python3 2>/dev/null || pyenv which python 2>/dev/null || true)
    if [ -z "$PYTHON" ]; then
        echo "  ✗ pyenv found but no Python version is active."
        echo "    Run: pyenv install 3.12 && pyenv global 3.12"
        exit 1
    fi
    echo "  ✓ pyenv found — using $PYTHON"
else
    echo "  ✗ pyenv not found."
    echo ""
    echo "  pyenv is recommended for managing Python environments."
    echo "  Install it with:  curl https://pyenv.run | bash"
    echo "  Then reload your shell and run:  pyenv install 3.12 && pyenv global 3.12"
    echo ""
    read -rp "  Continue with system Python instead? [Y/n] " yn
    case "${yn,,}" in
        n|no) echo "  Aborted. Install pyenv then re-run."; exit 1 ;;
        *) PYTHON=$(command -v python3) ;;
    esac
    echo "  Using system Python: $PYTHON"
fi

# ── Dependencies ─────────────────────────────────────────────────────────────

echo ""
echo "▸ Checking dependencies…"

# System GI type libraries — always required via apt regardless of Python env.
# These are runtime type data, not Python packages; pip cannot provide them.
SYSTEM_MISSING=()
for pkg in gir1.2-appindicator3-0.1 gir1.2-notify-0.7; do
    dpkg -s "$pkg" &>/dev/null || SYSTEM_MISSING+=("$pkg")
done

if [ ${#SYSTEM_MISSING[@]} -gt 0 ]; then
    echo "  ✗ Missing system GI libraries: ${SYSTEM_MISSING[*]}"
    echo "    (These are needed even with pyenv — they are not pip-installable.)"
    read -rp "  Install via apt now? [Y/n] " yn
    case "${yn,,}" in
        n|no) echo "  Aborted."; exit 1 ;;
        *)
            sudo apt update -qq
            sudo apt install -y "${SYSTEM_MISSING[@]}"
            ;;
    esac
fi

# Python packages — always use a dedicated venv with --copies.
# --copies physically copies the Python binary into the venv, so the widget
# keeps working even if pyenv switches versions or removes the source version.
VENV_DIR="$INSTALL_DIR/venv"
echo "  ▸ Creating isolated venv at $VENV_DIR …"
mkdir -p "$INSTALL_DIR"
"$PYTHON" -m venv --copies "$VENV_DIR"
VENV_PYTHON="$VENV_DIR/bin/python3"

if command -v pyenv &>/dev/null; then
    # Build deps needed to compile PyGObject from source.
    # PyGObject ≥3.51 requires girepository-2.0 (libgirepository-2.0-dev),
    # which is only available on Ubuntu 24.04+. On 22.04 we must use the
    # 1.0 API and pin PyGObject to the last compatible release (<3.51).
    BUILD_PKGS=(libcairo2-dev pkg-config python3-dev)
    if apt-cache show libgirepository-2.0-dev &>/dev/null 2>&1; then
        BUILD_PKGS+=(libgirepository-2.0-dev)
        PYGOBJECT_VERSION=""          # latest works fine
    else
        BUILD_PKGS+=(libgirepository1.0-dev)
        PYGOBJECT_VERSION="<3.51"     # last version supporting girepository-1.0
    fi

    MISSING_BUILD=()
    for pkg in "${BUILD_PKGS[@]}"; do
        dpkg -s "$pkg" &>/dev/null || MISSING_BUILD+=("$pkg")
    done
    if [ ${#MISSING_BUILD[@]} -gt 0 ]; then
        echo "  ▸ Installing build deps: ${MISSING_BUILD[*]}"
        sudo apt install -y "${MISSING_BUILD[@]}"
    fi

    echo "  ▸ Installing PyGObject${PYGOBJECT_VERSION:+ (pinned to ${PYGOBJECT_VERSION})} + pycairo into venv…"
    "$VENV_PYTHON" -m pip install --quiet --upgrade pip
    "$VENV_PYTHON" -m pip install --quiet "PyGObject${PYGOBJECT_VERSION}" pycairo
    echo "  ✓ pip packages installed into venv"
else
    # System python — python3-gi is managed by apt and lives outside venv.
    # Add a .pth file to the venv so it can see system site-packages for gi.
    if ! "$PYTHON" -c "import gi" 2>/dev/null; then
        echo "  ✗ python3-gi not found"
        read -rp "  Install via apt? [Y/n] " yn
        case "${yn,,}" in
            n|no) echo "  Aborted."; exit 1 ;;
            *) sudo apt install -y python3-gi ;;
        esac
    fi
    # Allow venv to see system gi package (apt-installed)
    SITE_PKG=$("$VENV_PYTHON" -c "import site; print(site.getsitepackages()[0])")
    SYS_SITE=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])")
    echo "$SYS_SITE" > "$SITE_PKG/system-gi.pth"
fi

# Final check inside the venv
"$VENV_PYTHON" -c "
import gi
gi.require_version('Gtk','3.0')
gi.require_version('AppIndicator3','0.1')
gi.require_version('Notify','0.7')
from gi.repository import Gtk, AppIndicator3, Notify
" || { echo "  ✗ GI import failed inside venv — check errors above"; exit 1; }

echo "  ✓ All dependencies satisfied"

# ── Install files ───────────────────────────────────────────────────────────

echo ""
echo "▸ Installing to $INSTALL_DIR …"

cp claude_usage_widget.py shared.py usage_popup.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/claude_usage_widget.py"

mkdir -p "$(dirname "$BIN_LINK")"
ln -sf "$INSTALL_DIR/claude_usage_widget.py" "$BIN_LINK"

# Wrapper scripts use the venv Python — fully independent of pyenv version changes
cat > "$HOME/.local/bin/claude-widget-start" <<EOFSTART
#!/bin/bash
# Start Claude Usage Widget — uses dedicated venv (pyenv-version-independent)
env -i \\
  HOME="\$HOME" \\
  DISPLAY="\$DISPLAY" \\
  DBUS_SESSION_BUS_ADDRESS="\$DBUS_SESSION_BUS_ADDRESS" \\
  XDG_RUNTIME_DIR="\$XDG_RUNTIME_DIR" \\
  PATH="/usr/local/bin:/usr/bin:/bin" \\
  $VENV_PYTHON $INSTALL_DIR/claude_usage_widget.py > /tmp/claude-widget.log 2>&1 &

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
Exec=env -u LD_LIBRARY_PATH PATH="/usr/local/bin:/usr/bin:/bin" $VENV_PYTHON $INSTALL_DIR/claude_usage_widget.py
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

# ── Account setup ────────────────────────────────────────────────────────────

echo ""
echo "▸ Setting up accounts…"
echo "  Each account reads OAuth credentials from a Claude Code config directory."
echo "  The default is ~/.claude (standard single-account Claude Code install)."
echo ""

CONFIG_OUT="$HOME/.config/$APP_ID/config.json"
mkdir -p "$(dirname "$CONFIG_OUT")"

# Ask how many accounts
while true; do
    read -rp "  How many accounts do you want to monitor? [1]: " NUM_ACCOUNTS
    NUM_ACCOUNTS="${NUM_ACCOUNTS:-1}"
    if [[ "$NUM_ACCOUNTS" =~ ^[1-9][0-9]*$ ]]; then
        break
    fi
    echo "  Please enter a positive number."
done

ACCOUNTS_JSON="["
for i in $(seq 1 "$NUM_ACCOUNTS"); do
    echo ""
    echo "  — Account $i of $NUM_ACCOUNTS —"

    # Label
    DEFAULT_LABEL="Claude"
    [ "$NUM_ACCOUNTS" -gt 1 ] && DEFAULT_LABEL="Account$i"
    read -rp "  Label [$DEFAULT_LABEL]: " LABEL
    LABEL="${LABEL:-$DEFAULT_LABEL}"

    # Credentials dir
    DEFAULT_DIR="~/.claude"
    read -rp "  Claude config dir [$DEFAULT_DIR]: " CRED_DIR
    CRED_DIR="${CRED_DIR:-$DEFAULT_DIR}"

    # Expand ~ and check for credentials file
    CRED_PATH="${CRED_DIR/#\~/$HOME}/.credentials.json"
    if [ -f "$CRED_PATH" ]; then
        echo "  ✓ Found credentials at $CRED_PATH"
    else
        echo "  ⚠ No credentials at $CRED_PATH"
        echo "    Make sure Claude Code is installed and you've run 'claude login'."
    fi

    [ "$i" -gt 1 ] && ACCOUNTS_JSON+=","
    ACCOUNTS_JSON+="{\"label\":\"$LABEL\",\"credentials_dir\":\"$CRED_DIR\"}"
done
ACCOUNTS_JSON+="]"

# Write config
cat > "$CONFIG_OUT" <<EOF
{
  "accounts": $ACCOUNTS_JSON,
  "poll_interval_seconds": 300,
  "thresholds": { "warn": 60, "critical": 85 }
}
EOF
chmod 600 "$CONFIG_OUT"
echo ""
echo "  ✓ Config written to $CONFIG_OUT"
echo "  Edit it any time to change accounts, poll interval, or thresholds."

# ── Done ────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ Installation complete!               ║"
echo "║                                          ║"
echo "║   Start:  claude-widget-start            ║"
echo "║   Stop:   claude-widget-stop             ║"
echo "║   (or reboot — it autostarts)            ║"
echo "╚══════════════════════════════════════════╝"
