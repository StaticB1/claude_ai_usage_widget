#!/usr/bin/env python3
"""
Claude AI Usage Widget — Linux System Tray (Multi-Account)
Shows claude.ai subscription usage (5h / 7d) in the taskbar for multiple accounts.
Click to see detailed breakdown + reset timers.

Reads credentials from configurable Claude Code directories.
Config: ~/.config/claude-usage-widget/config.json

Author: Statotech Systems (original), extended for multi-account
Version: 2.0.0
License: MIT
"""

__version__ = "2.0.0"
__author__ = "Statotech Systems"

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Notify', '0.7')

from gi.repository import Gtk, AppIndicator3, GLib, Notify
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

from shared import (
    COLOR_GRAY, DEFAULT_THRESHOLDS, get_color_for_pct, hex_to_rgb,
    parse_utilization, format_reset_time, format_reset_clock, compute_burn_rate,
)
from usage_popup import UsageDetailWindow
from config_window import ConfigWindow

# ── Config ──────────────────────────────────────────────────────────────────

APP_ID = "claude-usage-widget"
APP_NAME = "Claude Usage"
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"

CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
NOTIFICATION_STATE_FILE = CONFIG_DIR / "notification_state.json"

DEFAULT_ACCOUNTS = [
    {"label": "Claude", "credentials_dir": "~/.claude"},
]
DEFAULT_POLL_INTERVAL = 300  # 5 minutes


# ── Icon generation ─────────────────────────────────────────────────────────

def write_icon(pct: float, error: bool = False) -> str:
    """Generate PNG icon with Cairo and return path."""
    color = COLOR_GRAY if error else get_color_for_pct(pct)
    r, g, b = hex_to_rgb(color)

    size = 32
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    ctx.set_source_rgba(r, g, b, 0.25)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.fill()

    ctx.set_source_rgb(r, g, b)
    ctx.set_line_width(2)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.stroke()

    ctx.set_source_rgb(r, g, b)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(22)
    text = "C"
    x_bearing, y_bearing, width, height, *_ = ctx.text_extents(text)
    ctx.move_to(size/2 - width/2 - x_bearing, size/2 - height/2 - y_bearing)
    ctx.show_text(text)

    icon_dir = Path("/tmp") / APP_ID
    icon_dir.mkdir(exist_ok=True)
    # Reason: AppIndicator3 compares the path string and skips updates if
    # unchanged, so we encode the color into the filename to force a reload.
    icon_path = icon_dir / f"icon_{color.lstrip('#')}.png"
    surface.write_to_png(str(icon_path))
    return str(icon_path)


def write_loading_icon(frame: int) -> str:
    """Generate a spinner icon for the given frame (0–7), rotating a 3/4 arc."""
    PI2 = 2 * 3.14159
    size = 32

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    # Dim background circle
    ctx.set_source_rgba(0.5, 0.5, 0.5, 0.2)
    ctx.arc(size/2, size/2, 13, 0, PI2)
    ctx.fill()

    # Rotating 3/4 arc — start angle advances by 45° per frame
    angle_start = (frame / 8) * PI2 - (PI2 / 4)  # offset so 0 starts at top
    angle_end = angle_start + PI2 * 0.75
    ctx.set_source_rgba(0.55, 0.55, 0.55, 0.9)
    ctx.set_line_width(2.5)
    ctx.arc(size/2, size/2, 13, angle_start, angle_end)
    ctx.stroke()

    # Gray "C" to keep it recognisable while loading
    ctx.set_source_rgba(0.5, 0.5, 0.5, 0.7)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(22)
    text = "C"
    x_bearing, y_bearing, width, height, *_ = ctx.text_extents(text)
    ctx.move_to(size/2 - width/2 - x_bearing, size/2 - height/2 - y_bearing)
    ctx.show_text(text)

    icon_dir = Path("/tmp") / APP_ID
    icon_dir.mkdir(exist_ok=True)
    icon_path = icon_dir / f"icon_loading_{frame}.png"
    surface.write_to_png(str(icon_path))
    return str(icon_path)


# ── Config loading ──────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load widget config, creating defaults if missing."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[claude-usage] Bad config, using defaults: {e}", file=sys.stderr)

    config = {
        "accounts": DEFAULT_ACCOUNTS,
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL,
        "thresholds": DEFAULT_THRESHOLDS,
        "burn_rate": {"enabled": False, "multiplier": 1.5},
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    os.chmod(CONFIG_FILE, 0o600)
    return config


def load_notification_state() -> dict:
    """Load persisted notification state (keyed by account label)."""
    if NOTIFICATION_STATE_FILE.exists():
        try:
            return json.loads(NOTIFICATION_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_notification_state(state_map: dict):
    """Persist notification state so restarts don't re-fire."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        NOTIFICATION_STATE_FILE.write_text(json.dumps(state_map, indent=2))
    except OSError as e:
        print(f"[claude-usage] Could not save notification state: {e}", file=sys.stderr)


# ── Per-account token loading ───────────────────────────────────────────────

def load_token(credentials_dir: str) -> str | None:
    """Load OAuth access token from a Claude Code credentials directory."""
    cred_path = Path(credentials_dir).expanduser() / ".credentials.json"
    if not cred_path.exists():
        return None
    try:
        data = json.loads(cred_path.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def load_subscription_info(credentials_dir: str) -> dict | None:
    """Load subscription info from a Claude Code credentials directory."""
    cred_path = Path(credentials_dir).expanduser() / ".credentials.json"
    if not cred_path.exists():
        return None
    try:
        data = json.loads(cred_path.read_text())
        oauth = data.get("claudeAiOauth", {})
        if oauth:
            return {
                "subscription_type": oauth.get("subscriptionType", "").title(),
                "rate_limit_tier": oauth.get("rateLimitTier", ""),
            }
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return None


# ── API call ────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    pass


def fetch_usage(token: str) -> dict | None:
    """Fetch usage data from the Claude API."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"claude-usage-widget/{__version__}",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    }
    req = urllib.request.Request(USAGE_API_URL, headers=headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError()
        print(f"[claude-usage] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[claude-usage] Error: {e}", file=sys.stderr)
        return None


def _is_resets_at_future(resets_at_str: str | None) -> bool:
    """Return True if the given resets_at timestamp is still in the future."""
    if not resets_at_str:
        return False
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < resets_at
    except Exception:
        return False


def _is_usage_stale(usage: dict) -> bool:
    """Return True if any usage window's resets_at has passed, meaning the period rolled over."""
    now = datetime.now(timezone.utc)
    for key in ("five_hour", "seven_day"):
        window = usage.get(key) or {}
        resets_at_str = window.get("resets_at")
        if resets_at_str:
            try:
                resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
                if now >= resets_at:
                    return True
            except Exception:
                pass
    return False


# ── Main App ────────────────────────────────────────────────────────────────

class ClaudeUsageApp:
    def __init__(self):
        self.config = load_config()
        self.accounts = self.config.get("accounts", DEFAULT_ACCOUNTS)
        self.poll_interval = self.config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
        self.thresholds = self.config.get("thresholds", DEFAULT_THRESHOLDS)
        self.burn_rate_cfg = self.config.get("burn_rate", {"enabled": False, "multiplier": 1.5})

        # Per-account state — seed notification fields from disk so restarts don't re-fire
        persisted = load_notification_state()
        self.account_states: dict[str, dict] = {}
        for acct in self.accounts:
            state = self._blank_state(acct["credentials_dir"])
            saved = persisted.get(acct["label"], {})
            state["last_notification_threshold"] = saved.get("last_notification_threshold", 0)
            state["last_threshold_five_resets_at"] = saved.get("last_threshold_five_resets_at")
            state["last_threshold_seven_resets_at"] = saved.get("last_threshold_seven_resets_at")
            state["last_burn_rate_resets_at"] = saved.get("last_burn_rate_resets_at")
            state["last_burn_rate_threshold"] = saved.get("last_burn_rate_threshold", 0)
            state["last_seven_resets_at"] = saved.get("last_seven_resets_at")
            self.account_states[acct["label"]] = state

        self.last_updated = "never"
        self.running = True
        self.startup_notification_sent = False
        self._loading = False
        self._loading_frame = 0

        Notify.init(APP_NAME)

        # Create indicator
        icon_path = write_icon(0, error=True)
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, icon_path, AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)
        self.indicator.set_label("--", "")

        self._build_menu()

        # Start background polling
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _build_menu(self):
        self.menu = Gtk.Menu()
        self.menu_items = {}

        for acct in self.accounts:
            lbl = acct["label"]
            item = Gtk.MenuItem(label=f"{lbl}: --%")
            item.set_sensitive(False)
            self.menu.append(item)
            self.menu_items[lbl] = item

        self.menu.append(Gtk.SeparatorMenuItem())

        item_details = Gtk.MenuItem(label="Show Details...")
        item_details.connect("activate", self.on_show_details)
        self.menu.append(item_details)

        item_refresh = Gtk.MenuItem(label="Refresh Now")
        item_refresh.connect("activate", lambda _: self.force_refresh())
        self.menu.append(item_refresh)

        item_configure = Gtk.MenuItem(label="Configure...")
        item_configure.connect("activate", self.on_configure)
        self.menu.append(item_configure)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self.on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def _poll_loop(self):
        """Background thread: fetch usage for all accounts periodically."""
        # Reason: initial fetch always runs for all accounts regardless of disable_polling
        results = {label: self._fetch_account(label, state)
                   for label, state in self.account_states.items()}
        GLib.idle_add(self._update_ui, results)
        while self.running:
            time.sleep(self.poll_interval)
            if any(not a.get("disable_polling", False) for a in self.accounts):
                self._do_poll()

    def _do_poll(self):
        """Background periodic poll — respects per-account disable_polling."""
        acct_cfg = {a["label"]: a for a in self.accounts}
        results = {}
        for label, state in self.account_states.items():
            if not acct_cfg.get(label, {}).get("disable_polling", False):
                results[label] = self._fetch_account(label, state)
        GLib.idle_add(self._update_ui, results)

    def _fetch_account(self, label: str, state: dict) -> dict:
        """Fetch usage for a single account. Returns update dict."""
        cred_dir = state["credentials_dir"]
        try:
            token = load_token(cred_dir)
            if not token:
                return {"error": "No token", "usage_data": None, "token": None}
            data = fetch_usage(token)
            sub_info = load_subscription_info(cred_dir)
            if data is None:
                return {"error": "API error", "usage_data": None, "token": token,
                        "subscription_info": sub_info}
            return {"error": None, "usage_data": data, "token": token,
                    "subscription_info": sub_info}
        except RateLimitError:
            print(f"[claude-usage] {label}: rate limited", file=sys.stderr)
            return {"error": "Rate limited", "usage_data": None, "token": state.get("token")}
        except Exception as e:
            print(f"[claude-usage] {label}: {e}", file=sys.stderr)
            return {"error": str(e), "usage_data": None, "token": state.get("token")}

    def force_refresh(self):
        GLib.idle_add(self._start_loading_animation)
        def _do():
            results = {}
            for label, state in self.account_states.items():
                results[label] = self._fetch_account(label, state)
            GLib.idle_add(self._update_ui, results)
        threading.Thread(target=_do, daemon=True).start()

    def _start_loading_animation(self):
        """Begin spinner animation (must run on GTK thread via idle_add)."""
        self._loading = True
        self._loading_frame = 0
        GLib.timeout_add(120, self._tick_loading_icon)
        return False  # one-shot

    def _tick_loading_icon(self):
        """Advance spinner one frame; stops automatically when _loading is cleared."""
        if not self._loading:
            return False
        self._loading_frame = (self._loading_frame + 1) % 8
        icon_path = write_loading_icon(self._loading_frame)
        self.indicator.set_icon_full(icon_path, "Refreshing...")
        return True  # keep going

    def _update_ui(self, results: dict):
        """Update indicator label + icon from fetched data (GTK thread)."""
        self._loading = False  # stop spinner; _tick_loading_icon will not reschedule
        self.last_updated = datetime.now().strftime("%H:%M:%S")

        persist_needed = False
        for label, result in results.items():
            if result.get("usage_data"):
                result["pending_reset"] = False
                seven = result["usage_data"].get("seven_day") or {}
                new_resets_at = seven.get("resets_at")
                if new_resets_at and new_resets_at != self.account_states[label].get("last_seven_resets_at"):
                    result["last_seven_resets_at"] = new_resets_at
                    persist_needed = True
            else:
                cached = self.account_states[label].get("usage_data")
                if cached and _is_usage_stale(cached):
                    # Window(s) have reset — cached data is misleading, clear it and wait for fresh data
                    result = {k: v for k, v in result.items() if k != "usage_data"}
                    result["pending_reset"] = True
                elif cached:
                    # Still within the window, preserve last known values
                    result = {k: v for k, v in result.items() if k != "usage_data"}
                # else: no cached data at all — leave as-is (will show ! or ? if 7d still active)
            self.account_states[label].update(result)

        if persist_needed:
            self._persist_notification_state()

        # Build tray label: "G:67% N:12%" — accounts with hide_from_tray are skipped
        label_parts = []
        max_pct = 0
        any_ok = False

        for acct in self.accounts:
            lbl = acct["label"]
            hide = acct.get("hide_from_tray", False)
            state = self.account_states[lbl]
            usage = state.get("usage_data")

            if usage:
                five = usage.get("five_hour", {}) or {}
                seven = usage.get("seven_day", {}) or {}
                pct5, _ = parse_utilization(five.get("utilization", 0))
                pct7, _ = parse_utilization(seven.get("utilization", 0))

                if not hide:
                    any_ok = True
                    label_parts.append(f"{lbl}:{pct5}%")
                    max_pct = max(max_pct, pct5, pct7)

                r5 = format_reset_clock(five.get("resets_at")) if acct.get("disable_polling", False) else format_reset_time(five.get("resets_at"))
                burn_rate = compute_burn_rate(seven)
                if burn_rate is not None:
                    arrow = "\u2191" if burn_rate >= 1.0 else "\u2193"
                    br_str = f"  {arrow}{burn_rate:.1f}\u00d7  \u21ba {r5}"
                else:
                    br_str = f"  \u21ba {r5}"
                self.menu_items[lbl].set_label(f"{lbl}: {pct7}%{br_str}")
            else:
                seven_resets_at = state.get("last_seven_resets_at")
                seven_active = _is_resets_at_future(seven_resets_at)
                if not hide:
                    if state.get("pending_reset") or seven_active:
                        label_parts.append(f"{lbl}:?")
                    else:
                        label_parts.append(f"{lbl}:!")
                if state.get("pending_reset"):
                    self.menu_items[lbl].set_label(f"{lbl}: waiting for new period data...")
                elif seven_active:
                    reset_str = format_reset_time(seven_resets_at)
                    self.menu_items[lbl].set_label(f"{lbl}: ?  \u21ba {reset_str}")
                else:
                    self.menu_items[lbl].set_label(f"{lbl}: {state.get('error', 'error')}")

        tray_text = " ".join(label_parts)
        self.indicator.set_label(tray_text, "")

        icon_path = write_icon(max_pct) if any_ok else write_icon(0, error=True)
        self.indicator.set_icon_full(icon_path, tray_text)

        # Startup notification
        if not self.startup_notification_sent and any_ok:
            self.startup_notification_sent = True
            n = Notify.Notification.new("Claude Usage Widget Started", tray_text, "dialog-information")
            n.show()

        # Per-account threshold notifications
        for acct in self.accounts:
            lbl = acct["label"]
            state = self.account_states[lbl]
            usage = state.get("usage_data")
            if not usage:
                continue
            self._check_threshold(lbl, usage, state)
            self._check_burn_rate(lbl, usage, state)

        return False  # GLib.idle_add one-shot

    @staticmethod
    def _resets_at_changed(stored_str: str | None, new_str: str | None, tolerance_hours: float) -> bool:
        """Return True if resets_at shifted enough to indicate a new window."""
        if not new_str:
            return False
        if stored_str is None:
            return True
        try:
            stored_dt = datetime.fromisoformat(stored_str.replace("Z", "+00:00"))
            new_dt = datetime.fromisoformat(new_str.replace("Z", "+00:00"))
            return abs((new_dt - stored_dt).total_seconds()) > tolerance_hours * 3600
        except Exception:
            return stored_str != new_str

    def _check_threshold(self, label: str, usage: dict, state: dict):
        """Send notification when an account crosses a threshold.

        Resets the escalation level whenever either the 5h or 7d window rolls
        over, so notifications fire again at the start of each new window.
        """
        if not self.startup_notification_sent:
            return

        five = usage.get("five_hour", {}) or {}
        seven = usage.get("seven_day", {}) or {}
        pct5, _ = parse_utilization(five.get("utilization", 0))
        pct7, _ = parse_utilization(seven.get("utilization", 0))
        pct = max(pct5, pct7)

        five_resets_at = five.get("resets_at")
        seven_resets_at = seven.get("resets_at")

        # Detect window rollovers — 5h uses 1h tolerance, 7d uses 6h tolerance
        five_new = self._resets_at_changed(state.get("last_threshold_five_resets_at"), five_resets_at, 1)
        seven_new = self._resets_at_changed(state.get("last_threshold_seven_resets_at"), seven_resets_at, 6)
        if five_new or seven_new:
            state["last_notification_threshold"] = 0
            if five_resets_at:
                state["last_threshold_five_resets_at"] = five_resets_at
            if seven_resets_at:
                state["last_threshold_seven_resets_at"] = seven_resets_at

        prev = state.get("last_notification_threshold", 0)
        warn = self.thresholds.get("warn", 60)
        crit = self.thresholds.get("critical", 85)

        current = 0
        if pct >= 100:
            current = 100
        elif pct >= crit:
            current = crit
        elif pct >= warn:
            current = warn

        if current <= prev:
            return

        urgency = Notify.Urgency.CRITICAL if current >= crit else Notify.Urgency.NORMAL
        icon = "dialog-warning" if current >= crit else "dialog-information"
        n = Notify.Notification.new(f"{label}: Usage at {pct}%", f"Account {label} reached {pct}%", icon)
        n.set_urgency(urgency)
        n.show()
        state["last_notification_threshold"] = current
        self._persist_notification_state()

    @staticmethod
    def _blank_state(credentials_dir: str) -> dict:
        return {
            "credentials_dir": credentials_dir,
            "token": None, "usage_data": None, "pending_reset": False,
            "subscription_info": None, "error": None,
            "last_notification_threshold": 0,       # highest level already notified this window
            "last_threshold_five_resets_at": None,  # 5h window resets_at seen last poll
            "last_threshold_seven_resets_at": None, # 7d window resets_at seen last poll
            "last_burn_rate_resets_at": None,       # resets_at value of the current window
            "last_burn_rate_threshold": 0,          # highest usage % level already notified (0/warn/critical)
            "last_seven_resets_at": None,           # last known 7d resets_at, persisted across restarts
        }

    def _persist_notification_state(self):
        """Persist notification state to disk so restarts don't re-fire.

        Both threshold and burn-rate state are now safe to persist because both
        track their respective window resets_at timestamps — a new window is
        detected on the next poll and the level resets to 0 automatically.
        """
        state_map = {
            lbl: {
                "last_notification_threshold": s.get("last_notification_threshold", 0),
                "last_threshold_five_resets_at": s.get("last_threshold_five_resets_at"),
                "last_threshold_seven_resets_at": s.get("last_threshold_seven_resets_at"),
                "last_burn_rate_resets_at": s.get("last_burn_rate_resets_at"),
                "last_burn_rate_threshold": s.get("last_burn_rate_threshold", 0),
                "last_seven_resets_at": s.get("last_seven_resets_at"),
            }
            for lbl, s in self.account_states.items()
        }
        save_notification_state(state_map)

    def on_configure(self, _widget):
        ConfigWindow(self.accounts, self.thresholds, self.burn_rate_cfg,
                     self.poll_interval, self._on_config_saved)

    def _on_config_saved(self, new_config: dict):
        """Rebuild live state from saved config and refresh."""
        self.accounts = new_config["accounts"]
        self.thresholds = new_config["thresholds"]
        self.burn_rate_cfg = new_config["burn_rate"]
        self.poll_interval = new_config["poll_interval_seconds"]
        persisted = load_notification_state()
        self.account_states = {}
        for acct in self.accounts:
            state = self._blank_state(acct["credentials_dir"])
            saved = persisted.get(acct["label"], {})
            state["last_notification_threshold"] = saved.get("last_notification_threshold", 0)
            state["last_threshold_five_resets_at"] = saved.get("last_threshold_five_resets_at")
            state["last_threshold_seven_resets_at"] = saved.get("last_threshold_seven_resets_at")
            state["last_burn_rate_resets_at"] = saved.get("last_burn_rate_resets_at")
            state["last_burn_rate_threshold"] = saved.get("last_burn_rate_threshold", 0)
            state["last_seven_resets_at"] = saved.get("last_seven_resets_at")
            self.account_states[acct["label"]] = state
        self._build_menu()
        self.force_refresh()

    def _check_burn_rate(self, label: str, usage: dict, state: dict):
        """Notify when 7d burn rate exceeds the multiplier.

        Escalation levels mirror the usage thresholds:
          0  → fires once early in the window (before warn threshold)
          warn % → fires again if burn rate still high at warn level
          critical % → fires again if burn rate still high at critical level
        Each level fires at most once per window cycle.
        """
        if not self.burn_rate_cfg.get("enabled"):
            return
        if not self.startup_notification_sent:
            return

        seven = usage.get("seven_day") or {}
        resets_at_str = seven.get("resets_at")
        if not resets_at_str:
            return

        pct7, _ = parse_utilization(seven.get("utilization", 0))
        if pct7 <= 0:
            return

        try:
            burn_rate = compute_burn_rate(seven)
            if burn_rate is None:
                return

            multiplier = self.burn_rate_cfg.get("multiplier", 1.5)
            if burn_rate < multiplier:
                return

            # Reset escalation tracker when a genuinely new window starts (6h tolerance).
            if self._resets_at_changed(state.get("last_burn_rate_resets_at"), resets_at_str, 6):
                state["last_burn_rate_resets_at"] = resets_at_str
                state["last_burn_rate_threshold"] = 0

            # Determine which escalation level we're at (mirrors usage thresholds)
            warn = self.thresholds.get("warn", 60)
            crit = self.thresholds.get("critical", 85)
            if pct7 >= crit:
                current_level = crit
            elif pct7 >= warn:
                current_level = warn
            else:
                current_level = 1  # early warning — below warn threshold

            prev_level = state.get("last_burn_rate_threshold", 0)
            if current_level <= prev_level:
                return

            projected = int(burn_rate * 100)
            urgency = Notify.Urgency.CRITICAL if pct7 >= crit else Notify.Urgency.NORMAL
            icon = "dialog-warning" if pct7 >= crit else "dialog-information"
            n = Notify.Notification.new(
                f"{label}: High burn rate",
                f"7d usage at {pct7}% — on pace for {projected}%",
                icon,
            )
            n.set_urgency(urgency)
            n.show()
            state["last_burn_rate_threshold"] = current_level
            # Persist so a widget restart won't re-fire for the same window/level
            self._persist_notification_state()
        except Exception as e:
            print(f"[claude-usage] burn rate check error: {e}", file=sys.stderr)

    def on_show_details(self, _widget):
        accts_data = []
        for acct in self.accounts:
            lbl = acct["label"]
            state = self.account_states[lbl]
            accts_data.append({
                "label": lbl,
                "usage_data": state.get("usage_data"),
                "error": state.get("error"),
                "subscription_info": state.get("subscription_info"),
                "disable_polling": acct.get("disable_polling", False),
            })
        UsageDetailWindow(accts_data, self.last_updated, self.thresholds,
                          self.burn_rate_cfg, __version__, self.force_refresh)

    def on_quit(self, _widget):
        self.running = False
        Notify.uninit()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    app = ClaudeUsageApp()
    app.run()
