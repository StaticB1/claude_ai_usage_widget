#!/usr/bin/env bash
set -euo pipefail

# Repo info — used when the script is piped from curl and needs to fetch
# the source tarball itself.
REPO_OWNER="StaticB1"
REPO_NAME="claude_ai_usage_widget"
REPO_BRANCH="main"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}"
TARBALL_URL="${REPO_URL}/archive/refs/heads/${REPO_BRANCH}.tar.gz"

# Resolve the script's directory, but be tolerant when piped (no real path).
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR=""
fi

INSTALL_DIR="$HOME/.local/share/claude-token-tracker"
BIN_DIR="$HOME/.local/bin"
GUI_BIN="$BIN_DIR/claude-token-tracker"
CLI_BIN="$BIN_DIR/ctt"
AUTOSTART_DIR="$HOME/.config/autostart"
APPS_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"
DESKTOP_ID="claude-token-tracker"

AUTOSTART=true
UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --no-autostart) AUTOSTART=false ;;
        --uninstall)    UNINSTALL=true  ;;
        --help|-h)
            echo "Usage: bash install.sh [--no-autostart] [--uninstall]"
            echo "  --no-autostart   Skip adding to login startup"
            echo "  --uninstall      Remove all installed files"
            echo
            echo "Curl install (no clone needed):"
            echo "  curl -fsSL ${REPO_URL}/raw/${REPO_BRANCH}/install.sh | bash"
            exit 0 ;;
    esac
done

# ── Colours ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
    BOLD='\033[1m';     RESET='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; RESET=''
fi

ok()      { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}! $*${RESET}"; }
err()     { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }
section() { echo -e "\n${BOLD}── $* ──${RESET}"; }

CONFIG_DIR="$HOME/.config/claude-token-tracker"

if $UNINSTALL; then
    section "Uninstalling Claude Usage Widget & Token Tracker"
    rm -rf  "$INSTALL_DIR"
    rm -f   "$GUI_BIN" "$CLI_BIN"
    rm -f   "$AUTOSTART_DIR/$DESKTOP_ID.desktop"
    rm -f   "$APPS_DIR/$DESKTOP_ID.desktop"
    for size in 16 24 32 48 64 128 256 512; do
        rm -f "$ICON_BASE/${size}x${size}/apps/$DESKTOP_ID.png"
    done
    command -v gtk-update-icon-cache &>/dev/null \
        && gtk-update-icon-cache -q "$ICON_BASE" 2>/dev/null || true
    if [ -d "$CONFIG_DIR" ]; then
        if [ -t 0 ]; then
            read -r -p "Also delete $CONFIG_DIR (history, budgets, accounts)? [y/N]: " ans
            case "$ans" in
                y|Y|yes|YES)
                    rm -rf "$CONFIG_DIR"
                    ok "Removed $CONFIG_DIR"
                    ;;
                *)
                    ok "Kept $CONFIG_DIR (delete manually if desired)"
                    ;;
            esac
        else
            ok "Kept $CONFIG_DIR (run interactively, or 'rm -rf $CONFIG_DIR' to wipe)"
        fi
    fi
    ok "Uninstalled."
    exit 0
fi

echo -e "${BOLD}Claude Usage Widget & Token Tracker — installer${RESET}"

# When piped from curl, or run from a directory missing the source tree,
# fetch the tarball and re-anchor SCRIPT_DIR onto the extracted copy.
needs_bootstrap=false
if [ -z "$SCRIPT_DIR" ] || [ ! -d "$SCRIPT_DIR/cct" ]; then
    needs_bootstrap=true
fi

if $needs_bootstrap; then
    section "Fetching source from $REPO_URL"
    command -v tar &>/dev/null || err "tar not found. Install tar."
    if command -v curl &>/dev/null; then
        FETCH=(curl -fsSL --retry 3 -o)
    elif command -v wget &>/dev/null; then
        FETCH=(wget -q -O)
    else
        err "Neither curl nor wget found. Install one to bootstrap from GitHub."
    fi
    TMPDIR_BOOT="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_BOOT"' EXIT
    "${FETCH[@]}" "$TMPDIR_BOOT/src.tar.gz" "$TARBALL_URL" \
        || err "Download failed: $TARBALL_URL"
    tar -xzf "$TMPDIR_BOOT/src.tar.gz" -C "$TMPDIR_BOOT"
    extracted="$(find "$TMPDIR_BOOT" -maxdepth 2 -type d -name 'cct' \
                 -printf '%h\n' | head -1)"
    [ -n "$extracted" ] || err "Source tarball did not contain a cct/ package."
    SCRIPT_DIR="$extracted"
    ok "Source extracted to $SCRIPT_DIR"
fi

section "Checking Python"
command -v python3 &>/dev/null || err "python3 not found. Install Python 3.8+."
PY_OK=$(python3 -c "import sys; print(sys.version_info >= (3,8))")
PY_VER=$(python3 -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}')")
[[ "$PY_OK" == "True" ]] || err "Python 3.8+ required (found $PY_VER)."
ok "Python $PY_VER"

# pyenv-aware install: when pyenv is detected, drop a dedicated venv next to
# the install. The launcher invokes that venv's python directly so a later
# `pyenv global` flip won't break the widget. --system-site-packages keeps
# PyGObject available (it's installed system-wide via apt/dnf, not pip).
USE_VENV=false
VENV_DIR="$INSTALL_DIR/.venv"
PYTHON_FOR_LAUNCHER="python3"
if command -v pyenv &>/dev/null; then
    ok "pyenv detected — creating isolated venv (survives pyenv version switches)"
    USE_VENV=true
fi

section "Installing system dependencies (GTK3 + AppIndicator)"

need_gi=false; need_gtk=false
python3 -c "import gi" 2>/dev/null || need_gi=true
python3 -c "
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
" 2>/dev/null || need_gtk=true

has_indicator() {
    python3 -c "
import gi
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3
except Exception:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
" 2>/dev/null
}

if ! $need_gi && ! $need_gtk; then
    ok "GTK3 Python bindings already present"
else
    if command -v apt-get &>/dev/null; then
        pkgs=()
        $need_gi  && pkgs+=("python3-gi")
        $need_gtk && pkgs+=("gir1.2-gtk-3.0")
        echo "apt: installing ${pkgs[*]}"
        sudo apt-get install -y "${pkgs[@]}"
    elif command -v dnf &>/dev/null; then
        pkgs=()
        $need_gi  && pkgs+=("python3-gobject")
        $need_gtk && pkgs+=("gtk3")
        sudo dnf install -y "${pkgs[@]}"
    elif command -v pacman &>/dev/null; then
        pkgs=()
        $need_gi  && pkgs+=("python-gobject")
        $need_gtk && pkgs+=("gtk3")
        sudo pacman -S --noconfirm "${pkgs[@]}"
    elif command -v zypper &>/dev/null; then
        pkgs=()
        $need_gi  && pkgs+=("python3-gobject")
        $need_gtk && pkgs+=("gtk3")
        sudo zypper install -y "${pkgs[@]}"
    else
        warn "Unknown package manager. Install python3-gi + GTK3 bindings manually."
    fi
fi

python3 -c "
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
" 2>/dev/null || err "GTK3 bindings still not available."
ok "GTK3 bindings OK"

# libnotify GI bindings — desktop alerts. Optional: the app runs without them
# (notifications just go quiet). The typelib package is separate from python3-gi.
if ! python3 -c "
import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify
" 2>/dev/null; then
    echo "  Installing libnotify bindings (desktop notifications)..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y gir1.2-notify-0.7 2>/dev/null \
            || warn "libnotify bindings unavailable — desktop alerts off (app still works)"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y libnotify 2>/dev/null \
            || warn "libnotify bindings unavailable — desktop alerts off"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm libnotify 2>/dev/null \
            || warn "libnotify bindings unavailable — desktop alerts off"
    elif command -v zypper &>/dev/null; then
        sudo zypper install -y typelib-1_0-Notify-0_7 2>/dev/null \
            || warn "libnotify bindings unavailable — desktop alerts off"
    fi
fi
python3 -c "
import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify
" 2>/dev/null && ok "libnotify bindings OK" || true

if ! has_indicator; then
    echo "  Installing AppIndicator (system tray)..."
    if command -v apt-get &>/dev/null; then
        # Debian 13 / recent Ubuntu renamed the old gir1.2-appindicator3-0.1
        # to the Ayatana package; try it first, fall back for older distros.
        sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1 2>/dev/null \
            || sudo apt-get install -y gir1.2-appindicator3-0.1 2>/dev/null \
            || warn "AppIndicator unavailable — tray icon won't appear (app still works)"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y libayatana-appindicator-gtk3 2>/dev/null \
            || sudo dnf install -y libappindicator-gtk3 2>/dev/null \
            || warn "AppIndicator unavailable — tray icon won't appear"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm libayatana-appindicator 2>/dev/null \
            || warn "AppIndicator unavailable — tray icon won't appear"
    elif command -v zypper &>/dev/null; then
        sudo zypper install -y libayatana-appindicator3-1 2>/dev/null \
            || warn "AppIndicator unavailable — tray icon won't appear"
    fi
fi
has_indicator && ok "AppIndicator OK" || true

# ── Sweep the legacy v1 widget ────────────────────────────────────────────────
# The original single-file widget (claude_ai_usage_widget) installed under the
# `claude-usage-widget` name — different dir, different binaries, different
# autostart entry — so a v2 install never overwrites it, and its autostart
# entry would keep launching the dead widget alongside the new app. Clearing it
# here means BOTH `curl | bash` upgraders (who have no clone to run upgrade.sh
# from) and clone users get a clean migration. The old config dir is preserved
# in case it holds a manually-pasted OAuth token.
section "Checking for the old widget"
LEGACY_ID="claude-usage-widget"
legacy_hits=()
for f in \
    "$HOME/.local/share/$LEGACY_ID" \
    "$HOME/.local/bin/$LEGACY_ID" \
    "$HOME/.local/bin/claude-widget-start" \
    "$HOME/.local/bin/claude-widget-stop" \
    "$HOME/.config/autostart/$LEGACY_ID.desktop" \
    "$HOME/.local/share/applications/$LEGACY_ID.desktop"; do
    [ -e "$f" ] && legacy_hits+=("$f")
done
if [ "${#legacy_hits[@]}" -gt 0 ]; then
    pkill -f claude_usage_widget.py 2>/dev/null || true
    for f in "${legacy_hits[@]}"; do rm -rf "$f"; done
    ok "Removed old claude-usage-widget"
    [ -d "$HOME/.config/$LEGACY_ID" ] \
        && echo "  · Kept ~/.config/$LEGACY_ID (old config — delete manually to wipe)"
else
    ok "No old widget found"
fi

section "Installing app"
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$AUTOSTART_DIR" "$APPS_DIR"

if $USE_VENV; then
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        python3 -m venv --system-site-packages "$VENV_DIR"
    fi
    PYTHON_FOR_LAUNCHER="$VENV_DIR/bin/python"
    ok "Venv ready: $VENV_DIR"
fi

# Copy package + entry shim. We don't pip-install because GTK is system-only.
rm -rf "$INSTALL_DIR/cct" "$INSTALL_DIR/assets" "$INSTALL_DIR/claude_token_tracker.py"
cp -r  "$SCRIPT_DIR/cct" "$INSTALL_DIR/cct"
[ -d "$SCRIPT_DIR/assets" ] && cp -r "$SCRIPT_DIR/assets" "$INSTALL_DIR/assets"
cp     "$SCRIPT_DIR/claude_token_tracker.py" "$INSTALL_DIR/claude_token_tracker.py"
chmod +x "$INSTALL_DIR/claude_token_tracker.py"
ok "Copied to $INSTALL_DIR"

# Install hicolor icons so the desktop file's Icon= name resolves system-wide
section "Installing app icon"
mkdir -p "$ICON_BASE"
# gtk-update-icon-cache is a no-op without an index.theme. Most user-level
# hicolor dirs don't ship one — copy it from the system theme so launchers
# (Unity, GNOME, KDE) actually resolve our Icon= name.
if [ ! -f "$ICON_BASE/index.theme" ] && [ -f /usr/share/icons/hicolor/index.theme ]; then
    cp /usr/share/icons/hicolor/index.theme "$ICON_BASE/index.theme"
fi
for size in 16 24 32 48 64 128 256 512; do
    src="$SCRIPT_DIR/assets/icon-${size}.png"
    if [ -f "$src" ]; then
        dest_dir="$ICON_BASE/${size}x${size}/apps"
        mkdir -p "$dest_dir"
        cp "$src" "$dest_dir/$DESKTOP_ID.png"
    fi
done
if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -f -q "$ICON_BASE" 2>/dev/null || true
fi
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi
ok "Icons installed to $ICON_BASE"

# GUI launcher (note: $PYTHON_FOR_LAUNCHER is interpolated at install time —
# either "python3" (system Python) or the venv's python (pyenv-isolated)).
cat > "$GUI_BIN" << EOF
#!/usr/bin/env bash
exec "$PYTHON_FOR_LAUNCHER" "$INSTALL_DIR/claude_token_tracker.py" "\$@"
EOF
chmod +x "$GUI_BIN"
ok "GUI launcher: $GUI_BIN"

cat > "$CLI_BIN" << EOF
#!/usr/bin/env bash
PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH-}" \\
    exec "$PYTHON_FOR_LAUNCHER" -m cct "\$@"
EOF
chmod +x "$CLI_BIN"
ok "CLI launcher:  $CLI_BIN  (try: ctt summary)"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$rc" ]]; then
            echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$rc"
            warn "Added ~/.local/bin to PATH in $rc — restart shell or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
            break
        fi
    done
fi

cat > "$APPS_DIR/$DESKTOP_ID.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Widget & Token Tracker
Exec=$GUI_BIN
Icon=claude-token-tracker
Comment=Track Claude Code token usage per project
Categories=Utility;Development;
Terminal=false
StartupNotify=false
EOF
ok "App menu entry created"

if [ -t 0 ]; then
    section "Setting up accounts"
    mkdir -p "$CONFIG_DIR"
    CFG="$CONFIG_DIR/config.json"
    read -r -p "  How many Claude accounts do you want to monitor? [1]: " NACC
    NACC="${NACC:-1}"
    if ! [[ "$NACC" =~ ^[0-9]+$ ]] || [ "$NACC" -lt 1 ]; then
        warn "Invalid count — defaulting to 1"
        NACC=1
    fi

    ACC_JSON=""
    for i in $(seq 1 "$NACC"); do
        echo
        echo "  — Account $i of $NACC —"
        DEFAULT_LABEL="Account$i"
        [ "$i" = "1" ] && [ "$NACC" = "1" ] && DEFAULT_LABEL="default"
        read -r -p "    Label [$DEFAULT_LABEL]: " LABEL
        LABEL="${LABEL:-$DEFAULT_LABEL}"
        read -r -p "    Claude config dir [$HOME/.claude]: " CDIR
        CDIR="${CDIR:-$HOME/.claude}"
        # Expand ~/ in case the user typed it literally.
        CDIR="${CDIR/#\~/$HOME}"
        if [ -f "$CDIR/.credentials.json" ]; then
            ok "    Found credentials at $CDIR/.credentials.json"
        else
            warn "    No .credentials.json in $CDIR — run 'claude login' there later."
        fi
        # Build the JSON line by line — escape backslashes/quotes in label.
        ESC_LABEL="${LABEL//\\/\\\\}"; ESC_LABEL="${ESC_LABEL//\"/\\\"}"
        ESC_CDIR="${CDIR//\\/\\\\}"; ESC_CDIR="${ESC_CDIR//\"/\\\"}"
        SEP=","
        [ -z "$ACC_JSON" ] && SEP=""
        ACC_JSON+="${SEP}{\"label\":\"$ESC_LABEL\",\"claude_dir\":\"$ESC_CDIR\",\"disable_polling\":false,\"hide_from_tray\":false}"
    done

    # Preserve any existing config keys (e.g. legacy oauth_token, settings)
    # by merging in Python rather than blasting the file.
    python3 - "$CFG" "$ACC_JSON" << 'PYEOF'
import json, os, sys
path, accounts_json = sys.argv[1], sys.argv[2]
existing = {}
if os.path.exists(path):
    try:
        existing = json.loads(open(path).read())
    except Exception:
        existing = {}
existing['accounts'] = json.loads('[' + accounts_json + ']')
with open(path, 'w') as f:
    json.dump(existing, f, indent=2)
os.chmod(path, 0o600)
PYEOF
    ok "Account config written to $CFG"
else
    # Non-interactive (curl | bash): leave config alone — the app falls back
    # to a single 'default' account at ~/.claude on first run.
    :
fi

if $AUTOSTART; then
    cat > "$AUTOSTART_DIR/$DESKTOP_ID.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Widget & Token Tracker
Exec=$GUI_BIN
Icon=claude-token-tracker
Comment=Track Claude Code token usage per project
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
    ok "Autostart on login enabled"
else
    rm -f "$AUTOSTART_DIR/$DESKTOP_ID.desktop"
    ok "Skipped autostart (--no-autostart)"
fi

echo ""
echo -e "${BOLD}${GREEN}Installation complete!${RESET}"
echo ""
echo "  GUI:        claude-token-tracker"
echo "  CLI:        ctt summary  |  ctt block  |  ctt --help"
echo "  App menu:   search 'Claude Usage Widget & Token Tracker'"
$AUTOSTART && echo "  Startup:    auto-starts on next login"
echo ""
echo "  Uninstall:  bash $SCRIPT_DIR/install.sh --uninstall"
