#!/usr/bin/env python3
"""
Claude AI Usage Widget — Linux System Tray
Shows claude.ai subscription usage (5h / 7d) in the taskbar.
Click to see detailed breakdown + reset timers.

Supports two auth methods:
  1. Auto-detect from Claude Code credentials (~/.claude/.credentials.json)
  2. Manual OAuth token via config file (~/.config/claude-usage-widget/config.json)

Author: Statotech Systems
Version: 1.0.0
License: MIT
"""

__version__ = "1.0.0"
__author__ = "Statotech Systems"

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Notify', '0.7')

from gi.repository import Gtk, AppIndicator3, GLib, Notify, Gdk, Pango
import cairo
import json
import os
import sys
import urllib.request
import urllib.error
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

APP_ID = "claude-usage-widget"
APP_NAME = "Claude Usage"
ICON_NAME = "network-transmit-receive"  # fallback icon
REFRESH_INTERVAL_SEC = 120  # 2 minutes
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"

CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"

# ── Colors for the dynamic SVG icon ─────────────────────────────────────────

COLOR_GREEN = "#22c55e"
COLOR_YELLOW = "#eab308"
COLOR_ORANGE = "#f97316"
COLOR_RED = "#ef4444"
COLOR_GRAY = "#6b7280"


def get_color_for_pct(pct: float) -> str:
    if pct < 0.5:
        return COLOR_GREEN
    elif pct < 0.75:
        return COLOR_YELLOW
    elif pct < 0.9:
        return COLOR_ORANGE
    else:
        return COLOR_RED


def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex color to RGB tuple (0-1 range)."""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def write_icon(pct: float, error: bool = False) -> str:
    """Generate PNG icon with Cairo and return path."""
    if error:
        color = COLOR_GRAY
    else:
        color = get_color_for_pct(pct)

    r, g, b = hex_to_rgb(color)

    # Create PNG icon with Cairo
    size = 32
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    # Clear background (transparent)
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    # Draw filled circle (background)
    ctx.set_source_rgba(r, g, b, 0.25)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.fill()

    # Draw circle border
    ctx.set_source_rgb(r, g, b)
    ctx.set_line_width(2)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.stroke()

    # Draw "C" text
    ctx.set_source_rgb(r, g, b)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(22)

    text = "C"
    x_bearing, y_bearing, width, height, x_advance, y_advance = ctx.text_extents(text)
    ctx.move_to(size/2 - width/2 - x_bearing, size/2 - height/2 - y_bearing)
    ctx.show_text(text)

    # Save to file
    icon_dir = Path("/tmp") / APP_ID
    icon_dir.mkdir(exist_ok=True)
    icon_path = icon_dir / "icon.png"
    surface.write_to_png(str(icon_path))

    return str(icon_path)


# ── Token loading ───────────────────────────────────────────────────────────

def load_token() -> str | None:
    """Try loading OAuth token from Claude Code creds, then config file."""
    # 1. Claude Code credentials (Linux)
    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text())
            token = data.get("claudeAiOauth", {}).get("accessToken")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. Widget config file
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            token = data.get("oauth_token")
            if token:
                return token
        except (json.JSONDecodeError, KeyError):
            pass

    return None


def load_subscription_info() -> dict | None:
    """Load subscription information from Claude Code credentials."""
    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text())
            oauth = data.get("claudeAiOauth", {})
            if oauth:
                return {
                    "subscription_type": oauth.get("subscriptionType", "").title(),
                    "rate_limit_tier": oauth.get("rateLimitTier", ""),
                }
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def save_token(token: str):
    """Save token to widget config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            pass
    config["oauth_token"] = token
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    os.chmod(CONFIG_FILE, 0o600)


# ── API call ────────────────────────────────────────────────────────────────

def fetch_usage(token: str) -> dict | None:
    """Fetch usage data from the Claude API."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "claude-usage-widget/1.0",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    }

    req = urllib.request.Request(USAGE_API_URL, headers=headers, method="GET")

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[claude-usage] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[claude-usage] Error: {e}", file=sys.stderr)
        return None


# ── Time formatting ─────────────────────────────────────────────────────────

def format_reset_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = reset_dt - now
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "any moment"
        hours, remainder = divmod(total_sec, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return iso_str


# ── Detail popup window ─────────────────────────────────────────────────────

class UsageDetailWindow(Gtk.Window):
    """Popup window showing detailed usage info."""

    def __init__(self, usage_data: dict | None, last_updated: str, token_status: str, user_info: dict | None = None):
        super().__init__(title="Claude AI Usage")
        self.set_default_size(380, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.MOUSE)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self.set_decorated(True)

        # Lose focus → close
        self.connect("focus-out-event", lambda *_: self.destroy())

        # Apply CSS
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background-color: #1a1a2e; }
            .title-label { color: #e0e0ff; font-size: 16px; font-weight: bold; }
            .section-label { color: #a0a0c0; font-size: 11px; font-weight: bold; letter-spacing: 2px; }
            .metric-value { color: #ffffff; font-size: 28px; font-weight: bold; }
            .metric-sub { color: #8888aa; font-size: 11px; }
            .reset-label { color: #6b7280; font-size: 11px; }
            .status-ok { color: #22c55e; font-size: 11px; }
            .status-warn { color: #eab308; font-size: 11px; }
            .status-err { color: #ef4444; font-size: 11px; }
            .bar-bg { background-color: #2a2a4a; border-radius: 4px; }
            .separator { background-color: #2a2a4a; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label="⚡ Claude Usage")
        title.get_style_context().add_class("title-label")
        header.pack_start(title, False, False, 0)

        status_label = Gtk.Label(label=f"● {token_status}")
        sc = "status-ok" if token_status == "Connected" else "status-err"
        status_label.get_style_context().add_class(sc)
        status_label.set_halign(Gtk.Align.END)
        header.pack_end(status_label, False, False, 0)
        vbox.pack_start(header, False, False, 0)

        # Subscription info (if available)
        if user_info:
            sub_type = user_info.get("subscription_type")
            if sub_type:
                sub_label = Gtk.Label(label=f"Plan: {sub_type}")
                sub_label.get_style_context().add_class("metric-sub")
                sub_label.set_halign(Gtk.Align.START)
                vbox.pack_start(sub_label, False, False, 0)

        if usage_data:
            for key, label_text in [("five_hour", "5-HOUR WINDOW"), ("seven_day", "7-DAY WINDOW")]:
                bucket = usage_data.get(key)
                if not bucket:
                    continue

                sep = Gtk.Separator()
                sep.get_style_context().add_class("separator")
                vbox.pack_start(sep, False, False, 4)

                section_label = Gtk.Label(label=label_text)
                section_label.get_style_context().add_class("section-label")
                section_label.set_halign(Gtk.Align.START)
                vbox.pack_start(section_label, False, False, 0)

                utilization = bucket.get("utilization", 0)
                # Handle both decimal (0-1) and percentage (0-100) formats
                if utilization > 1:  # Already a percentage
                    pct = int(utilization)
                    utilization_decimal = utilization / 100
                else:  # Decimal format
                    pct = int(utilization * 100)
                    utilization_decimal = utilization

                # Big number
                val = Gtk.Label(label=f"{pct}%")
                val.get_style_context().add_class("metric-value")
                color = get_color_for_pct(utilization_decimal)
                val.set_markup(f'<span foreground="{color}" font_weight="bold" font="28">{pct}%</span>')
                val.set_halign(Gtk.Align.START)
                vbox.pack_start(val, False, False, 0)

                # Progress bar (GTK level bar)
                bar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                bar_box.get_style_context().add_class("bar-bg")

                bar = Gtk.LevelBar()
                bar.set_min_value(0)
                bar.set_max_value(1.0)
                bar.set_value(utilization_decimal)
                bar.set_size_request(-1, 8)

                # Remove default offset classes and add custom
                bar.remove_offset_value("low")
                bar.remove_offset_value("high")
                bar.remove_offset_value("full")
                bar_css = Gtk.CssProvider()
                bar_css.load_from_data(f"""
                    levelbar trough {{
                        background-color: #2a2a4a;
                        border-radius: 4px;
                        min-height: 8px;
                    }}
                    levelbar trough block.filled {{
                        background-color: {color};
                        border-radius: 4px;
                        min-height: 8px;
                    }}
                """.encode())
                bar.get_style_context().add_provider(bar_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

                bar_box.pack_start(bar, True, True, 0)
                vbox.pack_start(bar_box, False, False, 0)

                # Reset time
                resets = bucket.get("resets_at")
                reset_str = format_reset_time(resets)
                reset_lbl = Gtk.Label(label=f"Resets in {reset_str}")
                reset_lbl.get_style_context().add_class("reset-label")
                reset_lbl.set_halign(Gtk.Align.START)
                vbox.pack_start(reset_lbl, False, False, 0)
        else:
            err_label = Gtk.Label(label="Unable to fetch usage data.\nCheck token and connectivity.")
            err_label.get_style_context().add_class("status-err")
            vbox.pack_start(err_label, False, False, 8)

        # Footer
        sep = Gtk.Separator()
        sep.get_style_context().add_class("separator")
        vbox.pack_start(sep, False, False, 4)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        updated = Gtk.Label(label=f"Updated: {last_updated}")
        updated.get_style_context().add_class("metric-sub")
        footer.pack_start(updated, False, False, 0)

        refresh_btn = Gtk.Button(label="↻ Refresh")
        refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
        refresh_css = Gtk.CssProvider()
        refresh_css.load_from_data(b"""
            button { color: #8888aa; background: transparent; border: none; padding: 2px 8px; }
            button:hover { color: #e0e0ff; }
        """)
        refresh_btn.get_style_context().add_provider(refresh_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        refresh_btn.connect("clicked", lambda _: (self.destroy(), app.force_refresh()))
        footer.pack_end(refresh_btn, False, False, 0)

        vbox.pack_start(footer, False, False, 0)

        # Version info
        version_label = Gtk.Label(label=f"v{__version__}")
        version_label.get_style_context().add_class("reset-label")
        version_label.set_halign(Gtk.Align.CENTER)
        version_label.set_margin_top(4)
        vbox.pack_start(version_label, False, False, 0)

        self.add(vbox)
        self.show_all()


# ── Token entry dialog ──────────────────────────────────────────────────────

class TokenDialog(Gtk.Dialog):
    def __init__(self, parent=None):
        super().__init__(title="Claude OAuth Token", transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                         Gtk.STOCK_OK, Gtk.ResponseType.OK)
        self.set_default_size(450, -1)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label()
        label.set_markup(
            "Enter your Claude OAuth token.\n"
            "<small>Get it from <b>~/.claude/.credentials.json</b> (Claude Code)\n"
            "or browser DevTools → Network → api.anthropic.com headers.</small>"
        )
        label.set_line_wrap(True)
        box.pack_start(label, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("sk-ant-oat01-...")
        self.entry.set_visibility(False)
        box.pack_start(self.entry, False, False, 0)

        self.show_all()

    def get_token(self) -> str:
        return self.entry.get_text().strip()


# ── Main App ────────────────────────────────────────────────────────────────

class ClaudeUsageApp:
    def __init__(self):
        self.usage_data: dict | None = None
        self.subscription_info: dict | None = None
        self.last_updated: str = "never"
        self.token: str | None = None
        self.running = True
        self.last_notification_threshold: int = 0  # Track last notified threshold (0, 75, 90, 100)
        self.startup_notification_sent: bool = False

        Notify.init(APP_NAME)

        # Create indicator
        icon_path = write_icon(0, error=True)
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)
        self.indicator.set_label("--", "")

        # Build menu
        self.menu = Gtk.Menu()

        self.item_5h = Gtk.MenuItem(label="5h: --%")
        self.item_5h.set_sensitive(False)
        self.menu.append(self.item_5h)

        self.item_7d = Gtk.MenuItem(label="7d: --%")
        self.item_7d.set_sensitive(False)
        self.menu.append(self.item_7d)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_details = Gtk.MenuItem(label="Show Details…")
        item_details.connect("activate", self.on_show_details)
        self.menu.append(item_details)

        item_refresh = Gtk.MenuItem(label="Refresh Now")
        item_refresh.connect("activate", lambda _: self.force_refresh())
        self.menu.append(item_refresh)

        item_token = Gtk.MenuItem(label="Set Token…")
        item_token.connect("activate", self.on_set_token)
        self.menu.append(item_token)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self.on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        # Load token and subscription info
        self.token = load_token()
        self.subscription_info = load_subscription_info()
        if not self.token:
            GLib.timeout_add_seconds(2, self._prompt_token_once)

        # Start background polling
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _prompt_token_once(self):
        """Show token dialog on first run if no token found."""
        if not self.token:
            self.on_set_token(None)
        return False  # don't repeat

    def _poll_loop(self):
        """Background thread: fetch usage periodically."""
        while self.running:
            if self.token:
                data = fetch_usage(self.token)
                GLib.idle_add(self._update_ui, data)
            time.sleep(REFRESH_INTERVAL_SEC)

    def force_refresh(self):
        """Immediate refresh triggered by user."""
        def _do():
            if self.token:
                data = fetch_usage(self.token)
                GLib.idle_add(self._update_ui, data)
        threading.Thread(target=_do, daemon=True).start()

    def _update_ui(self, data: dict | None):
        """Update indicator label + icon from fetched data (runs on GTK thread)."""
        self.usage_data = data
        self.last_updated = datetime.now().strftime("%H:%M:%S")

        if data:
            five = data.get("five_hour", {}) or {}
            seven = data.get("seven_day", {}) or {}
            u5 = five.get("utilization", 0)
            u7 = seven.get("utilization", 0)

            # Handle both decimal (0-1) and percentage (0-100) formats
            if u5 > 1:  # Already a percentage
                pct5 = int(u5)
                u5_decimal = u5 / 100
            else:  # Decimal format, convert to percentage
                pct5 = int(u5 * 100)
                u5_decimal = u5

            if u7 > 1:  # Already a percentage
                pct7 = int(u7)
                u7_decimal = u7 / 100
            else:  # Decimal format, convert to percentage
                pct7 = int(u7 * 100)
                u7_decimal = u7

            dominant = max(u5_decimal, u7_decimal)

            self.indicator.set_label(f"{pct5}%", "")
            icon_path = write_icon(dominant)
            self.indicator.set_icon_full(icon_path, f"{pct5}%")

            self.item_5h.set_label(f"5h: {pct5}%  (resets {format_reset_time(five.get('resets_at'))})")
            self.item_7d.set_label(f"7d: {pct7}%  (resets {format_reset_time(seven.get('resets_at'))})")

            # Send notifications at specific thresholds only
            self._check_and_notify_threshold(pct5, pct7, dominant)
        else:
            self.indicator.set_label("ERR", "")
            icon_path = write_icon(0, error=True)
            self.indicator.set_icon_full(icon_path, "Error")
            self.item_5h.set_label("5h: error")
            self.item_7d.set_label("7d: error")

        return False  # GLib.idle_add one-shot

    def _check_and_notify_threshold(self, pct5: int, pct7: int, dominant: float):
        """Send notifications only at specific thresholds: startup, 75%, 90%, 100%"""
        # Startup notification (first successful data fetch)
        if not self.startup_notification_sent:
            self.startup_notification_sent = True
            n = Notify.Notification.new(
                "✓ Claude Usage Widget Started",
                f"Current usage: 5h: {pct5}%  |  7d: {pct7}%",
                "dialog-information",
            )
            n.show()
            return

        # Determine current threshold
        pct_val = int(dominant * 100)
        current_threshold = 0

        if pct_val >= 100:
            current_threshold = 100
        elif pct_val >= 90:
            current_threshold = 90
        elif pct_val >= 75:
            current_threshold = 75

        # Only notify if crossing a new threshold
        if current_threshold > self.last_notification_threshold:
            if current_threshold == 75:
                n = Notify.Notification.new(
                    "⚠️ Claude Usage: 75%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nApproaching rate limits.",
                    "dialog-warning",
                )
                n.set_urgency(Notify.Urgency.NORMAL)
            elif current_threshold == 90:
                n = Notify.Notification.new(
                    "⚠️ Claude Usage: 90%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nClose to rate limits!",
                    "dialog-warning",
                )
                n.set_urgency(Notify.Urgency.CRITICAL)
            elif current_threshold == 100:
                n = Notify.Notification.new(
                    "🛑 Claude Usage: 100%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nRate limit reached!",
                    "dialog-error",
                )
                n.set_urgency(Notify.Urgency.CRITICAL)
            else:
                return  # No notification for this threshold

            n.show()
            self.last_notification_threshold = current_threshold

    def on_show_details(self, _widget):
        token_status = "Connected" if self.usage_data else ("No token" if not self.token else "Error")
        UsageDetailWindow(self.usage_data, self.last_updated, token_status, self.subscription_info)

    def on_set_token(self, _widget):
        dialog = TokenDialog()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            token = dialog.get_token()
            if token:
                self.token = token
                save_token(token)
                self.force_refresh()
        dialog.destroy()

    def on_quit(self, _widget):
        self.running = False
        Notify.uninit()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    app = ClaudeUsageApp()
    app.run()
