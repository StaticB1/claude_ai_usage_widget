"""GTK3 dashboard. Linux-only by design — the CLI handles cross-platform."""
from __future__ import annotations
import html
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import gi
gi.require_version('Gtk', '3.0')

HAS_INDICATOR = False
AppIndicator = None
for _mod in ('AyatanaAppIndicator3', 'AppIndicator3'):
    try:
        gi.require_version(_mod, '0.1')
        AppIndicator = __import__(
            'gi.repository', fromlist=[_mod]).__dict__[_mod]
        HAS_INDICATOR = True
        break
    except (ValueError, ImportError, KeyError):
        continue

try:
    gi.require_version('Notify', '0.7')
    from gi.repository import Notify
    HAS_NOTIFY = True
except (ValueError, ImportError):
    HAS_NOTIFY = False

from gi.repository import Gtk, GLib, GObject, Gdk, GdkPixbuf, Pango  # noqa: E402

from .blocks import (BLOCK_DURATION, SESSION_LOOKBACK, active_session,
                     forecast, parse_resets_at)
from .budgets import evaluate_budgets
from .cli import scan_into_store
from .cloud import (AuthError, CloudApiError, RateLimitError,
                    extract_model_limits, fetch_cloud_usage,
                    load_subscription_info, load_token, normalize_utilization,
                    save_token, subscription_summary, utilization_pct,
                    weekly_window)
from .config import (APP_ID, APP_NAME, APP_VERSION, LOCAL_SCAN_INTERVAL,
                     PERIOD_LABELS, RATE_CARD_FILE, DEFAULT_ACCOUNT_LABEL,
                     BEDROCK_ACCOUNT_LABEL,
                     Account, load_accounts, load_settings, save_accounts,
                     save_settings, Settings)
from .notifications import NotificationManager, WindowSnapshot
from . import hop as claudehop
from . import ui
from .format import (fmt, fmt_cost, fmt_duration, fmt_full,
                     fmt_reset_absolute, fmt_reset_countdown, rel_time)
from .periods import cutoff_or_none, range_text
from .pricing import load_rate_card
from .single_instance import AlreadyRunning, acquire, signal_running_instance
from .store import HOP_PREFIX, Store


# ─── Theme ─────────────────────────────────────────────────────────────────
# The palettes and drawn components live in ui.py. COLORS is the same dict
# object as ui.P, so a theme switch updates both at once.
COLORS_LIGHT = ui.LIGHT
COLORS_DARK = ui.DARK
CACHE_DIR = Path.home() / '.cache' / APP_ID


def _gnome_interface_settings():
    """Return a Gio.Settings for org.gnome.desktop.interface, or None if
    the schema (or the color-scheme key) isn't installed — e.g. non-GNOME
    desktops or a GNOME older than 42. Gio.Settings.new() on a missing
    schema is a *fatal* GLib error (aborts the process), not a Python
    exception, so we must check via SettingsSchemaSource first rather
    than relying on try/except."""
    try:
        from gi.repository import Gio
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return None
        schema = source.lookup('org.gnome.desktop.interface', True)
        if schema is None or not schema.has_key('color-scheme'):
            return None
        return Gio.Settings.new('org.gnome.desktop.interface')
    except Exception:
        return None


def _resolve_palette(pref: str) -> dict:
    """Map a theme preference ('system'|'light'|'dark') to a palette."""
    if pref == 'dark':
        return COLORS_DARK
    if pref == 'light':
        return COLORS_LIGHT
    settings = _gnome_interface_settings()  # 'system': follow the desktop
    if settings is None:
        return COLORS_LIGHT
    scheme = settings.get_string('color-scheme')
    return COLORS_DARK if 'dark' in scheme else COLORS_LIGHT


from .config import load_settings as _load_settings_for_theme
COLORS = ui.P
COLORS.clear()
COLORS.update(_resolve_palette(_load_settings_for_theme().theme))


class _UsageColors(dict):
    """The old severity names, mapped onto the validated usage states."""
    _MAP = {'low': 'ok', 'medium': 'warn', 'high': 'warn',
            'critical': 'danger', 'unknown': 'faint'}

    def __getitem__(self, key):
        return COLORS[self._MAP[key]]


USAGE_COLORS = _UsageColors()


def _asset_dirs() -> List[Path]:
    root = Path(__file__).resolve().parent.parent
    return [root / 'assets', Path.home() / '.local' / 'share' / APP_ID / 'assets']


# Bundled fonts (OFL) must be known to fontconfig before any widget exists.
ui.register_fonts(_asset_dirs())


def _icon_path(size: int = 256) -> Optional[Path]:
    """Locate the bundled app icon.

    Looks first next to the installed package (install.sh layout), then in
    the repo `assets/` dir (when running from a checkout), then in
    /usr/share/icons (system install). Returns None if nothing matches —
    callers fall back to a stock icon."""
    candidates = []
    pkg_root = Path(__file__).resolve().parent.parent
    candidates.append(pkg_root / 'assets' / f'icon-{size}.png')
    candidates.append(pkg_root / 'assets' / 'icon.png')
    candidates.append(Path.home() / '.local' / 'share' / 'icons' / 'hicolor'
                      / f'{size}x{size}' / 'apps'
                      / f'{APP_ID}.png')
    candidates.append(Path('/usr/share/icons/hicolor') / f'{size}x{size}'
                      / 'apps' / f'{APP_ID}.png')
    for p in candidates:
        if p.exists():
            return p
    return None


def get_usage_color(pct: float) -> str:
    """Colour for a 0..1 utilization: teal, amber past the warning threshold,
    red past the critical one."""
    return ui.level_color(pct * 100.0)


def _ease_css() -> str:
    return "cubic-bezier(0.32, 0.72, 0, 1)"


def _make_css() -> str:
    C = COLORS
    grain = ui.grain_png(CACHE_DIR)
    grain_css = (f'background-image: url("file://{grain}"); '
                 'background-repeat: repeat;') if grain else ''
    ez = _ease_css()
    t = (f"transition: background-color 220ms {ez}, color 220ms {ez}, "
         f"box-shadow 260ms {ez}, border-color 220ms {ez};")
    return f"""
/* ── Base ───────────────────────────────────────────────────────────── */
window, .content-area {{
    background-color: {C['bg']}; color: {C['text']};
    font-family: "{ui.SANS}", "Cantarell", sans-serif; font-size: 13px;
}}
window.background {{ {grain_css} }}
label {{ color: {C['text']}; }}
.subtitle {{ font-size: 12.5px; color: {C['muted']}; }}
.faint {{ color: {C['faint']}; }}
.mono {{ font-family: "{ui.MONO}", monospace; }}
.tnum {{ font-feature-settings: "tnum"; }}

/* ── Sidebar ────────────────────────────────────────────────────────── */
.sidebar {{
    background-color: {C['bg']};
    border-right: 1px solid {C['hairline']};
    padding: 22px 14px 18px;
}}
.brand {{ font-family: "{ui.SERIF}", serif; font-size: 25px; color: {C['text']}; }}
.brand-sub {{ font-size: 11.5px; color: {C['faint']}; }}
.nav-btn {{
    background: transparent; background-image: none; border: none;
    border-radius: 12px; color: {C['muted']}; padding: 9px 12px;
    font-size: 13.5px; font-weight: 500; box-shadow: none; {t}
}}
.nav-btn label {{ color: {C['muted']}; }}
.nav-btn:hover {{ background-color: {C['overlay']}; }}
.nav-btn:hover label, .nav-btn:hover .nav-icon {{ color: {C['text']}; }}
.nav-icon {{ color: {C['faint']}; }}
.nav-btn.active {{
    background-color: {C['surface']};
    box-shadow: inset 0 0 0 1px {C['hairline']}, 0 1px 2px {C['shadow']},
                0 8px 18px -12px {C['shadow']};
}}
.nav-btn.active label {{ color: {C['text']}; font-weight: 600; }}
.nav-btn.active .nav-icon {{ color: {C['accent']}; }}
button.side-gauge-btn {{
    padding: 0; border: none; background: transparent; box-shadow: none;
    border-radius: 18px;
}}
.side-gauge {{
    background-color: {C['shell']}; border-radius: 18px; padding: 10px 12px;
    border: 1px solid {C['hairline']}; {t}
}}
button.side-gauge-btn:hover .side-gauge {{ background-color: {C['overlay']}; }}
.side-gauge-title {{ font-size: 12px; font-weight: 600; color: {C['text']}; }}
.side-gauge-sub {{ font-size: 11.5px; color: {C['muted']}; }}

/* ── Header ─────────────────────────────────────────────────────────── */
.header-bar {{
    background-color: transparent; border: none;
    padding: 22px 32px 10px 32px;
}}
.page-title {{
    font-family: "{ui.SERIF}", serif; font-size: 36px; font-weight: 400;
    color: {C['text']}; letter-spacing: -0.01em;
}}
.page-sub {{ font-size: 12.5px; color: {C['muted']}; }}
.pill {{
    background-color: {C['surface']}; border: 1px solid {C['hairline']};
    border-radius: 999px; padding: 6px 12px 6px 10px; font-size: 12.5px;
}}

/* ── Panels: a cream shell holding a bright core ───────────────────── */
.shell {{
    background-color: {C['shell']}; border-radius: 24px; padding: 5px;
    border: 1px solid {C['hairline']};
}}
.core {{
    background-color: {C['surface']}; border-radius: 19px;
    padding: 20px 22px 20px 22px;
    box-shadow: inset 0 1px 0 {C['highlight']}, 0 1px 2px {C['shadow']},
                0 18px 36px -26px {C['shadow']};
}}
/* Older views use .card; it gets the same two-layer look as a ring. */
.card {{
    background-color: {C['surface']}; border-radius: 19px; padding: 20px 22px;
    border: 1px solid {C['hairline']};
    box-shadow: 0 0 0 5px {C['shell']}, 0 0 0 6px {C['hairline']},
                0 18px 36px -26px {C['shadow']};
    margin: 6px;
}}
.card-title, .panel-title {{
    font-family: "{ui.SERIF}", serif; font-size: 22px; font-weight: 400;
    color: {C['text']};
}}
.eyebrow {{ font-size: 12px; font-weight: 500; color: {C['muted']}; }}
.eyebrow-strong {{ font-size: 12.5px; font-weight: 600; color: {C['text']}; }}
.tag {{
    border-radius: 999px; padding: 2px 9px; font-size: 11px; font-weight: 600;
    background-color: {C['accent-soft']}; color: {C['accent-strong']};
}}
.tag-ok {{ background-color: alpha({C['ok']}, 0.12); color: {C['ok']}; }}
.tag-warn {{ background-color: alpha({C['warn']}, 0.14); color: {C['warn']}; }}
.tag-danger {{ background-color: alpha({C['danger']}, 0.12); color: {C['danger']}; }}
.tag-muted {{ background-color: {C['sunken']}; color: {C['muted']}; }}
.figure {{ font-size: 44px; font-weight: 600; letter-spacing: -0.02em; color: {C['text']}; }}
.figure-unit {{ font-size: 15px; font-weight: 500; color: {C['muted']}; }}
.stat-value {{ font-size: 25px; font-weight: 600; letter-spacing: -0.01em; color: {C['text']}; }}
.stat-value-sm {{ font-size: 17px; font-weight: 600; color: {C['text']}; }}
.stat-label {{ font-size: 12px; font-weight: 500; color: {C['muted']}; }}
.stat-sub {{ font-size: 11.5px; color: {C['faint']}; }}
.ring-label {{ font-size: 13px; font-weight: 600; color: {C['text']}; }}
.ring-sub {{ font-size: 12px; color: {C['muted']}; }}
.divider-v {{ background-color: {C['hairline']}; min-width: 1px; }}
.divider-h {{ background-color: {C['hairline']}; min-height: 1px; }}
.callout {{
    background-color: {C['accent-soft']}; border-radius: 14px; padding: 11px 14px;
}}
.callout label {{ color: {C['text']}; }}
.notice {{ border-radius: 14px; padding: 11px 14px; background-color: {C['sunken']}; }}
.notice-warn {{ background-color: alpha({C['warn']}, 0.10); }}
.notice-danger {{ background-color: alpha({C['danger']}, 0.09); }}

/* ── Account tiles ──────────────────────────────────────────────────── */
.tile {{
    background-color: {C['sunken']}; border-radius: 16px; padding: 14px 15px;
    border: 1px solid transparent; {t}
}}
.tile:hover {{ background-color: {C['overlay']}; }}
.tile.live {{
    background-color: {C['surface']}; border-color: alpha({C['accent']}, 0.55);
    box-shadow: 0 0 0 3px {C['accent-soft']};
}}
.tile-name {{ font-size: 13.5px; font-weight: 600; color: {C['text']}; }}
.tile-sub {{ font-size: 11.5px; color: {C['muted']}; }}
.tile-num {{ font-size: 12px; font-weight: 600; color: {C['subtext']}; font-feature-settings: "tnum"; }}
.dot-live {{ color: {C['ok']}; }}
.dot-idle {{ color: {C['faint']}; }}

/* ── Bar lists ──────────────────────────────────────────────────────── */
.bar-name {{ font-size: 13px; color: {C['subtext']}; }}
.bar-value {{ font-size: 12.5px; font-weight: 600; color: {C['text']}; font-feature-settings: "tnum"; }}

/* ── Buttons ────────────────────────────────────────────────────────── */
button {{
    background-image: none; background-color: {C['surface']};
    border: 1px solid {C['hairline']}; border-radius: 999px;
    color: {C['text']}; padding: 7px 15px; font-size: 13px; font-weight: 500;
    box-shadow: 0 1px 1px {C['shadow']}; text-shadow: none; -gtk-icon-shadow: none; {t}
}}
button:hover {{ background-color: {C['overlay']}; }}
button:active, button:checked {{ background-color: {C['sunken']}; box-shadow: none; }}
button:disabled {{ color: {C['faint']}; background-color: {C['sunken']}; box-shadow: none; }}
*:focus {{ outline-color: alpha({C['accent']}, 0.55); outline-style: solid; outline-width: 2px; outline-offset: 1px; -gtk-outline-radius: 12px; }}
button.btn-primary {{
    background-color: {C['accent']}; border-color: {C['accent']}; color: {C['on-accent']};
    font-weight: 600; padding: 8px 18px;
    box-shadow: 0 1px 2px alpha({C['accent']}, 0.30), inset 0 1px 0 rgba(255,255,255,0.18);
}}
button.btn-primary label {{ color: {C['on-accent']}; }}
button.btn-primary:hover {{ background-color: {C['accent-strong']}; border-color: {C['accent-strong']}; }}
button.btn-secondary {{ background-color: {C['surface']}; }}
button.btn-ghost {{ background-color: transparent; border-color: transparent; box-shadow: none; }}
button.btn-ghost:hover {{ background-color: {C['overlay']}; }}
button.btn-icon {{ padding: 7px; min-width: 20px; min-height: 20px; }}
button.btn-link {{
    background: transparent; border: none; box-shadow: none; padding: 2px 4px;
    color: {C['accent-strong']}; font-weight: 600;
}}
button.btn-link label {{ color: {C['accent-strong']}; }}
button.btn-link:hover {{ background-color: {C['accent-soft']}; }}

/* ── Inputs ─────────────────────────────────────────────────────────── */
entry, spinbutton, .search-entry {{
    background-image: none; background-color: {C['sunken']}; color: {C['text']};
    border: 1px solid {C['hairline']}; border-radius: 12px; padding: 8px 12px;
    caret-color: {C['accent']}; box-shadow: none; {t}
}}
entry:focus, .search-entry:focus, spinbutton:focus {{
    border-color: {C['accent']}; background-color: {C['surface']};
    box-shadow: 0 0 0 3px {C['accent-soft']};
}}
entry selection {{ background-color: alpha({C['accent']}, 0.28); color: {C['text']}; }}
spinbutton entry {{ border: none; box-shadow: none; background: transparent; padding: 0 4px; }}
spinbutton button {{ background: transparent; border: none; box-shadow: none; color: {C['muted']}; border-radius: 10px; }}
spinbutton button:hover {{ background-color: {C['overlay']}; color: {C['text']}; }}
combobox button {{ padding: 6px 12px; }}
combobox arrow {{ color: {C['muted']}; }}
checkbutton, checkbutton label, radiobutton label {{ color: {C['text']}; }}
checkbutton check, radiobutton radio, treeview check {{
    background-image: none; background-color: {C['surface']};
    border: 1px solid {C['hairline']}; border-radius: 5px;
}}
checkbutton check:checked, radiobutton radio:checked, treeview check:checked {{
    background-color: {C['accent']}; border-color: {C['accent']}; color: #FFFFFF;
}}
switch {{
    background-image: none; background-color: {C['overlay']};
    border: 1px solid {C['hairline']}; border-radius: 999px; {t}
}}
switch:checked {{ background-color: {C['accent']}; border-color: {C['accent']}; }}
switch slider {{
    background-image: none; background-color: {'#D6D2C9' if ui.is_dark() else '#FFFFFF'};
    border-radius: 999px; border: 1px solid {C['hairline']};
    box-shadow: 0 1px 2px {C['shadow']};
}}

/* ── Segmented control (Breakdowns) ─────────────────────────────────── */
stackswitcher {{ background-color: {C['sunken']}; border-radius: 999px; padding: 3px; }}
stackswitcher button {{
    background: transparent; border: none; box-shadow: none; border-radius: 999px;
    color: {C['muted']}; padding: 5px 16px; font-weight: 500;
}}
stackswitcher button:checked {{
    background-color: {C['surface']}; color: {C['text']}; font-weight: 600;
    box-shadow: 0 1px 2px {C['shadow']}, inset 0 0 0 1px {C['hairline']};
}}

/* ── Segmented control (dashboard chart) ────────────────────────────── */
.segmented {{ background-color: {C['sunken']}; border-radius: 999px; padding: 3px; }}
.segmented button {{
    background: transparent; border: none; box-shadow: none; border-radius: 999px;
    color: {C['muted']}; padding: 4px 14px; font-weight: 500;
}}
.segmented button:checked {{
    background-color: {C['surface']}; color: {C['text']};
    box-shadow: 0 1px 2px {C['shadow']}, inset 0 0 0 1px {C['hairline']};
}}
.segmented button label {{ color: inherit; }}

/* ── Tables ─────────────────────────────────────────────────────────── */
treeview {{
    background-color: {C['surface']}; color: {C['text']};
    font-family: "{ui.SANS}", sans-serif; font-size: 13px;
    font-feature-settings: "tnum";
}}
treeview header button {{
    background-color: {C['surface']}; color: {C['muted']};
    font-weight: 500; font-size: 12px; border: none; border-radius: 0;
    border-bottom: 1px solid {C['hairline']}; padding: 10px 8px; box-shadow: none;
}}
treeview header button:hover {{ background-color: {C['sunken']}; color: {C['text']}; }}
treeview.view:nth-child(even) {{ background-color: {C['sunken']}; }}
treeview.view:hover {{ background-color: alpha({C['accent']}, 0.06); }}
treeview.view:selected {{ background-color: alpha({C['accent']}, 0.16); color: {C['text']}; }}
treeview expander {{ color: {C['faint']}; -gtk-icon-source: -gtk-icontheme("pan-end-symbolic"); }}
treeview expander:checked {{ color: {C['accent']}; -gtk-icon-source: -gtk-icontheme("pan-down-symbolic"); }}
treeview entry {{ background-color: {C['surface']}; border: 1px solid {C['accent']}; }}
.core-table {{ padding: 6px 4px 4px 4px; }}
.table-wrap {{ border-radius: 14px; border: 1px solid {C['hairline']}; }}

scrollbar {{ background-color: transparent; border: none; }}
scrollbar slider {{
    background-color: alpha({C['text']}, 0.16); border-radius: 999px;
    min-width: 6px; min-height: 6px; border: 3px solid transparent;
}}
scrollbar slider:hover {{ background-color: alpha({C['text']}, 0.32); }}

/* ── Menus, popovers, tooltips ──────────────────────────────────────── */
menu, .menu, popover, popover.background {{
    background-image: none; background-color: {C['surface']}; color: {C['text']};
    border: 1px solid {C['hairline']}; border-radius: 14px; padding: 6px;
    box-shadow: 0 12px 32px -12px {C['shadow']};
}}
menuitem {{ padding: 7px 12px; border-radius: 8px; color: {C['text']}; }}
menuitem:hover {{ background-color: {C['accent-soft']}; color: {C['accent-strong']}; }}
menuitem:disabled {{ color: {C['muted']}; }}
separator {{ background-color: {C['hairline']}; min-height: 1px; min-width: 1px; margin: 4px 0; }}
tooltip, tooltip.background {{
    background-color: {C['text']}; color: {C['bg']}; border-radius: 10px;
    padding: 7px 10px; text-shadow: none; box-shadow: none;
}}
tooltip label {{ color: {C['bg']}; font-size: 12.5px; }}
dialog, messagedialog, filechooser {{ background-color: {C['bg']}; color: {C['text']}; }}
headerbar, dialog .titlebar {{
    background-image: none; background-color: {C['surface']}; color: {C['text']};
    border-bottom: 1px solid {C['hairline']}; box-shadow: none; text-shadow: none;
}}

/* ── Status bar ─────────────────────────────────────────────────────── */
.status-bar {{
    background-color: {C['bg']}; border-top: 1px solid {C['hairline']};
    padding: 8px 32px;
}}
.status-bar label {{ font-size: 11.5px; color: {C['faint']}; }}

/* ── Tray popup ─────────────────────────────────────────────────────── */
window.usage-popup {{ background-color: transparent; }}
window.usage-popup.opaque {{ background-color: {C['shell']}; }}
.popup-shell {{
    background-color: {C['shell']}; border-radius: 26px; padding: 5px;
    border: 1px solid {C['hairline']};
}}
.popup-core {{
    background-color: {C['surface']}; border-radius: 21px; padding: 18px 20px 16px;
    box-shadow: inset 0 1px 0 {C['highlight']};
}}
.popup-title {{ font-family: "{ui.SERIF}", serif; font-size: 24px; color: {C['text']}; }}
"""


# ─── Widgets ───────────────────────────────────────────────────────────────

# ─── Main Window ───────────────────────────────────────────────────────────

class TrackerWindow(Gtk.Window):
    VIEWS = ['dashboard', 'projects', 'breakdowns', 'budgets', 'settings']

    def __init__(self, app: 'App'):
        super().__init__(title=APP_NAME)
        self.app = app
        self.set_default_size(1200, 620)
        self.set_size_request(900, 380)

        ip = _icon_path(128)
        if ip is not None:
            try:
                self.set_icon_from_file(str(ip))
            except Exception:
                pass

        provider = Gtk.CssProvider()
        provider.load_from_data(_make_css().encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._css_provider = provider
        # live-update when the desktop scheme flips (theme='system');
        # None on desktops without the GNOME interface schema.
        self._iface_settings = _gnome_interface_settings()
        if self._iface_settings is not None:
            self._iface_settings.connect(
                'changed::color-scheme', self._on_system_scheme_changed)

        self._current_view = 'dashboard'
        self._dashboard_period = '7d'
        self._projects_period = 'all'
        self._breakdowns_period = '7d'
        self._breakdowns_kind = 'models'   # or 'tools'
        self._account_filter: Optional[str] = None  # None = all accounts

        self._build_ui()
        self._install_shortcuts()
        self._nav_buttons['dashboard'].get_style_context().add_class('active')
        GLib.idle_add(self.refresh_data)
        self.show_all()

    # ── Layout scaffolding ─────────────────────────────────────────────────
    PAGE_SUBTITLES = {
        'dashboard':  'Your plan windows, this session, and what the tokens went on.',
        'projects':   'Tokens and cost per project, and per session inside it.',
        'breakdowns': 'Where the tokens went, by model and by tool.',
        'budgets':    'Limits that warn you before you reach them.',
        'settings':   'Accounts, alerts, pricing and appearance.',
    }

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(main_box)

        sidebar = self._build_sidebar()
        main_box.pack_start(sidebar, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.get_style_context().add_class('content-area')
        content.set_hexpand(True)
        main_box.pack_start(content, True, True, 0)

        content.pack_start(self._build_header(), False, False, 0)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(220)

        def _scrolled(widget):
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.add(widget)
            return sw

        self._stack.add_named(_scrolled(self._build_dashboard_view()), 'dashboard')
        self._stack.add_named(self._build_projects_view(), 'projects')
        self._stack.add_named(self._build_breakdowns_view(), 'breakdowns')
        self._stack.add_named(_scrolled(self._build_budgets_view()), 'budgets')
        self._stack.add_named(_scrolled(self._build_settings_view()), 'settings')
        content.pack_start(self._stack, True, True, 0)

        content.pack_start(self._build_status_bar(), False, False, 0)

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        sidebar.get_style_context().add_class('sidebar')
        sidebar.set_size_request(204, -1)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        brand.set_margin_start(6)
        brand.set_margin_bottom(28)
        mark = None
        ip = _icon_path(64)
        if ip is not None:
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ip), 34, 34)
                mark = Gtk.Image.new_from_pixbuf(pix)
            except Exception:
                mark = None
        if mark is not None:
            brand.pack_start(mark, False, False, 0)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(label="Token Tracker", xalign=0)
        t.get_style_context().add_class('brand')
        names.pack_start(t, False, False, 0)
        st = Gtk.Label(label="Claude usage, kept", xalign=0)
        st.get_style_context().add_class('brand-sub')
        names.pack_start(st, False, False, 0)
        brand.pack_start(names, False, False, 0)
        sidebar.pack_start(brand, False, False, 0)

        self._nav_buttons: Dict[str, Gtk.Button] = {}
        for vid, label in [
            ('dashboard',  'Dashboard'),
            ('projects',   'Projects'),
            ('breakdowns', 'Breakdowns'),
            ('budgets',    'Budgets'),
            ('settings',   'Settings'),
        ]:
            btn = Gtk.Button()
            btn.get_style_context().add_class('nav-btn')
            btn.set_relief(Gtk.ReliefStyle.NONE)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
            icon = ui.NavIcon(vid)
            icon.get_style_context().add_class('nav-icon')
            row.pack_start(icon, False, False, 0)
            row.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
            btn.add(row)
            btn.connect('clicked', lambda _, v=vid: self._switch_view(v))
            sidebar.pack_start(btn, False, False, 0)
            self._nav_buttons[vid] = btn

        sidebar.pack_start(Gtk.Box(), True, True, 0)

        # A small live gauge that stays in view on every page.
        gauge_btn = Gtk.Button()
        gauge_btn.get_style_context().add_class('side-gauge-btn')
        gauge_btn.set_relief(Gtk.ReliefStyle.NONE)
        gauge_btn.set_tooltip_text("Open the usage panel")
        gauge_btn.connect('clicked', lambda _b: self.app._show_usage_popup())
        g = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        g.get_style_context().add_class('side-gauge')
        self._side_ring = ui.Ring(diameter=46, stroke=4.5)
        g.pack_start(self._side_ring, False, False, 0)
        gt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        gt.set_valign(Gtk.Align.CENTER)
        self._side_title = Gtk.Label(label="5-hour window", xalign=0)
        self._side_title.get_style_context().add_class('side-gauge-title')
        self._side_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._side_title.set_max_width_chars(18)
        self._side_sub = Gtk.Label(label="waiting for a reading", xalign=0)
        self._side_sub.get_style_context().add_class('side-gauge-sub')
        self._side_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self._side_sub.set_max_width_chars(20)
        gt.pack_start(self._side_title, False, False, 0)
        gt.pack_start(self._side_sub, False, False, 0)
        g.pack_start(gt, True, True, 0)
        gauge_btn.add(g)
        sidebar.pack_start(gauge_btn, False, False, 0)

        v = Gtk.Label(label=f"v{APP_VERSION}", xalign=0)
        v.get_style_context().add_class('brand-sub')
        v.set_margin_start(8)
        v.set_margin_top(10)
        sidebar.pack_start(v, False, False, 0)
        return sidebar

    def _update_side_gauge(self):
        st = self.app._primary_state()
        bad = st.cloud_state in ('auth_error', 'network_error', 'rate_limited', 'no_token')
        try:
            hs = claudehop.load_state(st.token)
        except Exception:
            hs = None
        who = hs.active if hs and hs.active else "5-hour window"
        self._side_title.set_text(who)
        if bad:
            self._side_ring.set_value(st.last_pct5, dimmed=True)
            self._side_sub.set_text(st.cloud_state.replace('_', ' '))
        elif st.last_pct5 is None:
            self._side_ring.set_value(None)
            self._side_sub.set_text("waiting for a reading")
        else:
            self._side_ring.set_value(st.last_pct5)
            left = fmt_reset_countdown(st.last_resets_5h)
            self._side_sub.set_text(f"5h · resets {left}" if left not in ('—', '')
                                    else "5-hour window")

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.get_style_context().add_class('header-bar')
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._header_title = Gtk.Label(label="Dashboard", xalign=0)
        self._header_title.get_style_context().add_class('page-title')
        titles.pack_start(self._header_title, False, False, 0)
        self._header_sub = Gtk.Label(label=self.PAGE_SUBTITLES['dashboard'], xalign=0)
        self._header_sub.get_style_context().add_class('page-sub')
        self._header_sub.set_ellipsize(Pango.EllipsizeMode.END)
        titles.pack_start(self._header_sub, False, False, 0)
        header.pack_start(titles, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        right.set_valign(Gtk.Align.CENTER)
        # Which claudehop account this is (hidden when claudehop is not in use).
        self._hop_badge_box = Gtk.Box()
        self._hop_badge_box.get_style_context().add_class('pill')
        self._hop_badge_box.set_no_show_all(True)
        self._hop_badge = Gtk.Label()
        self._hop_badge.show()
        self._hop_badge_box.add(self._hop_badge)
        right.pack_start(self._hop_badge_box, False, False, 0)

        # Account filter — only meaningful when more than one account.
        self._account_combo = Gtk.ComboBoxText()
        self._populate_account_combo()
        self._account_combo.connect('changed', self._on_account_filter_changed)
        self._account_combo.set_no_show_all(True)
        right.pack_start(self._account_combo, False, False, 0)
        self._account_combo.set_visible(self._combo_wanted())

        refresh_btn = Gtk.Button()
        refresh_btn.add(ui.NavIcon('refresh', 16))
        refresh_btn.set_tooltip_text("Rescan the logs (Ctrl+R)")
        refresh_btn.get_style_context().add_class('btn-icon')
        refresh_btn.connect('clicked', lambda _: self.refresh_data())
        right.pack_start(refresh_btn, False, False, 0)
        header.pack_end(right, False, False, 0)
        self._update_hop_badge()
        self._header_box = header  # so we can re-show the combo on reload
        return header

    def _update_hop_badge(self):
        """Header badge: which claudehop account the widget is showing, or the
        provider new sessions run through. Hidden when claudehop is not in use."""
        try:
            hs = claudehop.load_state(self.app._primary_state().token)
        except Exception:
            hs = None
        if hs is None or not (hs.active or hs.provider):
            self._hop_badge_box.set_visible(False)
            return
        acct = hs.profile(hs.active)
        if hs.provider:
            head = hs.provider.capitalize()
            tail = f" · {hs.active} login idle" if hs.active else ""
            tip = (f"New sessions run through {hs.provider_command}. "
                   f"The {hs.active or 'saved'} login is not in use.")
        else:
            head = hs.active
            tail = f" · {acct.plan.capitalize()}" if acct and acct.plan else ""
            tip = (acct.email if acct and acct.email else hs.active)
        self._hop_badge.set_markup(
            f'<span foreground="{COLORS["ok"]}">●</span>  '
            f'<b>{html.escape(head)}</b>'
            f'<span foreground="{COLORS["muted"]}">{html.escape(tail)}</span>')
        self._hop_badge_box.set_tooltip_text(tip)
        self._hop_badge_box.set_visible(True)

    def _populate_account_combo(self):
        """(Re)build the account-filter combobox items from current accounts.

        Mutating the model fires ``changed``, which would otherwise re-enter
        the filter callback on every ``append``; block the handler around
        the rebuild and emit one final change at the end."""
        try:
            self._account_combo.handler_block_by_func(
                self._on_account_filter_changed)
        except TypeError:
            pass  # not yet connected
        self._account_combo.remove_all()
        self._account_combo.append('__all__', 'All accounts')
        for a in self.app._accounts:
            self._account_combo.append(a.label, a.label)
        # Bedrock is not a configured account, so it is offered only once the
        # store holds Bedrock turns.
        self._has_bedrock = self.app.store.has_account_rows(BEDROCK_ACCOUNT_LABEL)
        if self._has_bedrock:
            self._account_combo.append(BEDROCK_ACCOUNT_LABEL, 'Bedrock')
        # claudehop accounts, once turns have been filed under them.
        self._hop_combo_names = self.app.store.hop_accounts_with_rows()
        for n in self._hop_combo_names:
            self._account_combo.append(f'{HOP_PREFIX}{n}', f'{n} · claudehop')
        active = self._account_filter or '__all__'
        if not self._account_combo.set_active_id(active):
            self._account_combo.set_active_id('__all__')
            self._account_filter = None
        try:
            self._account_combo.handler_unblock_by_func(
                self._on_account_filter_changed)
        except TypeError:
            pass
        # Show/hide based on current account count.
        self._account_combo.set_visible(self._combo_wanted())

    def _combo_wanted(self) -> bool:
        return (len(self.app._accounts) > 1 or self._has_bedrock
                or bool(self._hop_combo_names))

    def _combo_stale(self) -> bool:
        """True when Bedrock or a claudehop account gained its first turns."""
        store = self.app.store
        return (store.has_account_rows(BEDROCK_ACCOUNT_LABEL)
                != getattr(self, '_has_bedrock', False)
                or store.hop_accounts_with_rows()
                != getattr(self, '_hop_combo_names', []))

    def _on_account_filter_changed(self, combo: Gtk.ComboBoxText):
        sel = combo.get_active_id()
        self._account_filter = None if (not sel or sel == '__all__') else sel
        # Re-render whatever view is currently up.
        self.refresh_data()

    def _build_status_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.get_style_context().add_class('status-bar')
        self._status_label = Gtk.Label(label="Ready")
        self._status_label.get_style_context().add_class('subtitle')
        bar.pack_start(self._status_label, False, False, 0)
        bar.pack_start(Gtk.Box(), True, True, 0)
        self._last_update_label = Gtk.Label(label="")
        self._last_update_label.get_style_context().add_class('subtitle')
        bar.pack_end(self._last_update_label, False, False, 0)
        return bar

    def _switch_view(self, view_id: str):
        self._current_view = view_id
        self._stack.set_visible_child_name(view_id)
        for vid, btn in self._nav_buttons.items():
            ctx = btn.get_style_context()
            (ctx.add_class if vid == view_id else ctx.remove_class)('active')
        self._header_title.set_text({
            'dashboard':  'Dashboard',
            'projects':   'Projects',
            'breakdowns': 'Breakdowns',
            'budgets':    'Budgets',
            'settings':   'Settings',
        }.get(view_id, 'Token Tracker'))
        self._header_sub.set_text(self.PAGE_SUBTITLES.get(view_id, ''))

        if view_id == 'dashboard':    self._update_dashboard()
        elif view_id == 'projects':   self._update_projects_view()
        elif view_id == 'breakdowns': self._update_breakdowns_view()
        elif view_id == 'budgets':    self._update_budgets_view()

    # ── Period helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _cutoff(period: str) -> Optional[datetime]:
        """Period start in UTC, or None for all time.

        Delegates to the shared period rules so the dashboard and the CLI
        agree — they used to carry separate copies, and 'Today' in particular
        cut at UTC midnight while the label showed the local date.
        """
        return cutoff_or_none(period)

    # ── Dashboard ──────────────────────────────────────────────────────────
    @staticmethod
    def _bezel(child: Gtk.Widget) -> Gtk.Widget:
        """A panel: a cream shell with a hairline holding a brighter core."""
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        shell.get_style_context().add_class('shell')
        core = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        core.get_style_context().add_class('core')
        core.pack_start(child, True, True, 0)
        shell.pack_start(core, True, True, 0)
        return shell

    @staticmethod
    def _label(text: str = '', cls: Optional[str] = None, xalign: float = 0,
               wrap: bool = False, ellipsize: bool = False) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=xalign)
        if cls:
            for c in cls.split():
                lbl.get_style_context().add_class(c)
        if wrap:
            lbl.set_line_wrap(True)
            lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if ellipsize:
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
        return lbl

    def _panel_head(self, title: str, sub: Optional[str] = None) -> tuple:
        """(row, title label, sub label, right-hand box) for a panel header."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t = self._label(title, 'panel-title')
        texts.pack_start(t, False, False, 0)
        s = self._label(sub or '', 'subtitle', wrap=True)
        texts.pack_start(s, False, False, 0)
        row.pack_start(texts, True, True, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right.set_valign(Gtk.Align.START)
        row.pack_end(right, False, False, 0)
        return row, t, s, right

    def _build_dashboard_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        c.set_margin_top(10); c.set_margin_bottom(30)
        c.set_margin_start(32); c.set_margin_end(32)

        # ── Row 1: plan rings + the current session ────────────────────────
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        top.pack_start(self._build_plan_panel(), True, True, 0)
        session = self._build_session_panel()
        session.set_size_request(300, -1)
        top.pack_start(session, False, False, 0)
        c.pack_start(top, False, False, 0)

        # ── Row 2: totals for the chosen period ────────────────────────────
        c.pack_start(self._build_totals_panel(), False, False, 0)

        # ── Row 3: daily usage chart ───────────────────────────────────────
        c.pack_start(self._build_chart_panel(), False, False, 0)

        # ── Row 4: claudehop accounts ──────────────────────────────────────
        # Shown by _update_hop_card only while claudehop is in use.
        self._hop_card = self._build_accounts_panel()
        c.pack_start(self._hop_card, False, False, 0)

        # ── Row 5: top projects and models ─────────────────────────────────
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        bottom.set_homogeneous(True)
        for key, title in (('projects', 'Top projects'), ('models', 'Top models')):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            head, _t, sub, _r = self._panel_head(title)
            box.pack_start(head, False, False, 0)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.pack_start(content, False, False, 0)
            setattr(self, f'_dashboard_{key}_subtitle', sub)
            setattr(self, f'_dashboard_{key}_content', content)
            bottom.pack_start(self._bezel(box), True, True, 0)
        c.pack_start(bottom, False, False, 0)
        return c

    # Plan panel ------------------------------------------------------------
    def _build_plan_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._plan_eyebrow = self._label('Plan limits', 'eyebrow')
        self._plan_title = self._label('Claude', 'panel-title', ellipsize=True)
        texts.pack_start(self._plan_eyebrow, False, False, 0)
        texts.pack_start(self._plan_title, False, False, 0)
        head.pack_start(texts, True, True, 0)
        self._plan_tag = self._label('', 'tag')
        self._plan_tag.set_valign(Gtk.Align.START)
        head.pack_end(self._plan_tag, False, False, 0)
        box.pack_start(head, False, False, 0)

        # Equal columns: the two rings, plus a third for any one-model weekly
        # caps when the plan has them (hidden columns take no share).
        rings = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        rings.set_homogeneous(True)
        rings.set_margin_top(4)
        self._plan_rings = {}
        for key, name in (('5h', '5-hour window'), ('week', '7-day window')):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            ring = ui.Ring(diameter=152, caption='used')
            ring.set_halign(Gtk.Align.CENTER)
            col.pack_start(ring, False, False, 0)
            lab = self._label(name, 'ring-label', xalign=0.5)
            lab.set_margin_top(6)
            sub = self._label('', 'ring-sub', xalign=0.5)
            col.pack_start(lab, False, False, 0)
            col.pack_start(sub, False, False, 0)
            rings.pack_start(col, False, False, 0)
            self._plan_rings[key] = (ring, lab, sub)
        # Other weekly meters (a separate cap for one model) beside the rings.
        self._plan_models = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._plan_models.set_valign(Gtk.Align.CENTER)
        self._plan_models.set_no_show_all(True)
        rings.pack_start(self._plan_models, True, True, 0)
        box.pack_start(rings, False, False, 0)

        self._plan_notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._plan_notice.get_style_context().add_class('notice')
        self._plan_notice_lbl = self._label('', wrap=True)
        self._plan_notice.pack_start(self._plan_notice_lbl, True, True, 0)
        self._plan_notice_btn = Gtk.Button()
        self._plan_notice_btn.set_valign(Gtk.Align.CENTER)
        self._plan_notice_btn.connect('clicked', self._on_plan_notice_action)
        self._plan_notice.pack_end(self._plan_notice_btn, False, False, 0)
        self._plan_notice.set_no_show_all(True)
        self._plan_notice_lbl.show()
        box.pack_start(self._plan_notice, False, False, 0)

        self._plan_foot = self._label('', 'stat-sub')
        box.pack_start(self._plan_foot, False, False, 0)
        self._plan_action = None
        return self._bezel(box)

    def _on_plan_notice_action(self, _btn):
        if self._plan_action == 'token':
            self.app._on_set_token()
        elif self._plan_action == 'read':
            self.app.read_all_hop()
        else:
            self.app._refresh_cloud()

    def _set_notice(self, text: Optional[str], kind: str = '',
                    action: Optional[str] = None, label: str = ''):
        ctx = self._plan_notice.get_style_context()
        for k in ('notice-warn', 'notice-danger'):
            ctx.remove_class(k)
        if not text:
            self._plan_notice.hide()
            return
        if kind:
            ctx.add_class(f'notice-{kind}')
        self._plan_notice_lbl.set_text(text)
        self._plan_action = action
        if label:
            self._plan_notice_btn.set_label(label)
            self._plan_notice_btn.get_style_context().add_class('btn-secondary')
            self._plan_notice_btn.show()
        else:
            self._plan_notice_btn.hide()
        self._plan_notice.show()

    def _set_tag(self, text: str, kind: str = ''):
        ctx = self._plan_tag.get_style_context()
        for k in ('tag-ok', 'tag-warn', 'tag-danger', 'tag-muted'):
            ctx.remove_class(k)
        if kind:
            ctx.add_class(f'tag-{kind}')
        self._plan_tag.set_text(text)
        self._plan_tag.set_visible(bool(text))

    @staticmethod
    def _reset_text(iso: Optional[str], weekly: bool = False) -> str:
        if not iso:
            return "Window not started"
        left = fmt_reset_countdown(iso)
        if left in ('—', '', 'now'):
            return "Resetting now"
        try:
            at = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone()
        except ValueError:
            return f"Resets in {left}"
        when = at.strftime('%a %H:%M' if weekly else '%H:%M')
        return f"Resets in {left} · {when}"

    def _set_rings(self, five: Optional[float], r5: Optional[str],
                   week: Optional[float], r7: Optional[str], scope: Optional[str],
                   dimmed: bool = False, models: Optional[list] = None,
                   was5: Optional[float] = None, was7: Optional[float] = None):
        ring, lab, sub = self._plan_rings['5h']
        ring.set_value(five, dimmed=dimmed)
        sub.set_text(f"Reset since, was {was5:.0f}%" if was5
                     else (self._reset_text(r5) if five is not None else "No reading"))
        ring, lab, sub = self._plan_rings['week']
        ring.set_value(week, dimmed=dimmed)
        lab.set_text("7-day window" + (f" · {scope}" if scope else ""))
        if week is None:
            sub.set_text("No weekly limit reported")
        else:
            sub.set_text(f"Reset since, was {was7:.0f}%" if was7
                         else self._reset_text(r7, weekly=True))
        for child in self._plan_models.get_children():
            self._plan_models.remove(child)
        models = models or []
        for m in models[:4]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            top.pack_start(self._label(f"{m['model']} weekly", 'eyebrow-strong'), True, True, 0)
            top.pack_end(self._label(f"{m['pct']}%", 'bar-value'), False, False, 0)
            row.pack_start(top, False, False, 0)
            meter = ui.Meter(height=6)
            meter.set_value(m['pct'])
            row.pack_start(meter, False, False, 0)
            row.pack_start(self._label(self._reset_text(m.get('resets_at'), True),
                                       'stat-sub'), False, False, 0)
            row.set_tooltip_text("The model you are using now" if m.get('is_active') else None)
            self._plan_models.pack_start(row, False, False, 0)
            row.show_all()
        self._plan_models.set_visible(bool(models))

    # Session panel ---------------------------------------------------------
    def _build_session_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self._label('Current session', 'eyebrow'), False, False, 0)
        fig = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._sess_left = self._label('—', 'figure')
        fig.pack_start(self._sess_left, False, False, 0)
        self._sess_unit = self._label('left', 'figure-unit')
        self._sess_unit.set_valign(Gtk.Align.END)
        self._sess_unit.set_margin_bottom(9)
        fig.pack_start(self._sess_unit, False, False, 0)
        box.pack_start(fig, False, False, 0)
        self._block_window_lbl = self._label('No session open', 'subtitle', ellipsize=True)
        box.pack_start(self._block_window_lbl, False, False, 0)
        self._sess_meter = ui.Meter(height=8, color='accent')
        self._sess_meter.set_margin_top(4)
        box.pack_start(self._sess_meter, False, False, 0)
        grid = Gtk.Grid()
        grid.set_column_spacing(18); grid.set_row_spacing(14)
        grid.set_column_homogeneous(True)
        grid.set_margin_top(12)
        self._sess_stats = {}
        for i, (key, label) in enumerate((('tokens', 'Tokens'), ('cost', 'API equivalent'),
                                          ('burn', 'Burn rate'), ('eta', 'Limit at this pace'))):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            cell.pack_start(self._label(label, 'stat-label'), False, False, 0)
            v = self._label('—', 'stat-value-sm')
            cell.pack_start(v, False, False, 0)
            grid.attach(cell, i % 2, i // 2, 1, 1)
            self._sess_stats[key] = v
        box.pack_start(grid, False, False, 0)
        return self._bezel(box)

    # Totals panel ----------------------------------------------------------
    def _build_totals_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        head, _t, self._dashboard_scope_label, right = self._panel_head('Totals')
        self._dashboard_period_combo = Gtk.ComboBoxText()
        for k in ('all', 'today', '7d', '30d'):
            self._dashboard_period_combo.append(k, PERIOD_LABELS[k])
        self._dashboard_period_combo.set_active_id(self._dashboard_period)
        self._dashboard_period_combo.connect(
            'changed', lambda combo: (
                setattr(self, '_dashboard_period', combo.get_active_id() or 'all'),
                self._update_dashboard()))
        right.pack_start(self._dashboard_period_combo, False, False, 0)
        box.pack_start(head, False, False, 0)

        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._summary_cards = {}
        cells = (('total', 'Total tokens'), ('input', 'Fresh input'),
                 ('cache', 'Cache'), ('output', 'Output'), ('cost', 'API equivalent'))
        for i, (key, label) in enumerate(cells):
            if i:
                sep = Gtk.Box()
                sep.get_style_context().add_class('divider-v')
                sep.set_margin_start(20); sep.set_margin_end(20)
                strip.pack_start(sep, False, False, 0)
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.pack_start(self._label(label, 'stat-label'), False, False, 0)
            val = self._label('0', 'stat-value')
            cell.pack_start(val, False, False, 0)
            sub = self._label('', 'stat-sub', ellipsize=True)
            cell.pack_start(sub, False, False, 0)
            strip.pack_start(cell, True, True, 0)
            self._summary_cards[key] = (val, sub)
        self._summary_cards_cost_cell = cell
        cell.set_tooltip_text(
            "What these tokens would cost on the pay-as-you-go API. On a Pro, "
            "Max or Team plan you pay a flat fee; this is the API-equivalent "
            "value, not your bill.")
        box.pack_start(strip, False, False, 0)
        return self._bezel(box)

    # Chart panel -----------------------------------------------------------
    def _build_chart_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        head, self._chart_title, self._chart_sub, right = self._panel_head(
            'Daily usage', 'Last 30 days. Point at a day for its figures.')
        seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        seg.get_style_context().add_class('segmented')
        self._chart_key = 'total_tokens'
        self._chart_btns = {}
        group = None
        for key, label in (('total_tokens', 'Tokens'), ('cost_usd', 'Cost')):
            b = Gtk.RadioButton.new_with_label_from_widget(group, label)
            b.set_mode(False)
            group = group or b
            b.connect('toggled', self._on_chart_key, key)
            seg.pack_start(b, False, False, 0)
            self._chart_btns[key] = b
        right.pack_start(seg, False, False, 0)
        box.pack_start(head, False, False, 0)

        def tip(row: dict) -> str:
            day = row.get('day') or ''
            try:
                day = datetime.fromisoformat(day).strftime('%A %-d %B')
            except ValueError:
                pass
            return (f"<b>{html.escape(day)}</b>\n"
                    f"{fmt(int(row.get('total_tokens') or 0))} tokens · "
                    f"{fmt_cost(row.get('cost_usd') or 0)}\n"
                    f"{int(row.get('messages') or 0):,} messages")
        self._chart = ui.DailyBars(height=200, fmt_value=fmt, tooltip=tip)
        box.pack_start(self._chart, False, False, 0)
        return self._bezel(box)

    def _on_chart_key(self, btn, key):
        if btn.get_active():
            self._chart_key = key
            self._chart.fmt_value = fmt_cost if key == 'cost_usd' else (lambda v: fmt(int(v)))
            self._update_chart()

    def _update_chart(self):
        acc = self._account_filter
        rows = {r['day']: r for r in self.app.store.daily_series(days=30, account=acc)}
        today = datetime.now().date()
        series = []
        for i in range(29, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            series.append(rows.get(d) or {'day': d, 'total_tokens': 0,
                                          'cost_usd': 0.0, 'messages': 0})
        self._chart.set_rows(series, self._chart_key)
        total = sum(float(r.get(self._chart_key) or 0) for r in series)
        busiest = max(series, key=lambda r: float(r.get(self._chart_key) or 0))
        if total > 0:
            val = (fmt_cost(total) if self._chart_key == 'cost_usd'
                   else f"{fmt(int(total))} tokens")
            self._chart_sub.set_text(
                f"Last 30 days: {val}. Busiest day {ui._short_day(busiest['day'])}. "
                "Point at a day for its figures.")
        else:
            self._chart_sub.set_text("Last 30 days. Nothing recorded yet.")

    # Accounts panel (claudehop) ---------------------------------------------
    def _build_accounts_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        head, _t, self._hop_sub, right = self._panel_head('Accounts')
        self._hop_read_btn = Gtk.Button(label='Read all accounts')
        self._hop_read_btn.get_style_context().add_class('btn-secondary')
        self._hop_read_btn.set_tooltip_text(
            "Runs `hop usage`: claudehop reads every saved account's 5-hour and "
            "weekly usage, renewing an idle account's token first. Each read "
            "spends a little of the usage endpoint's allowance, so it only "
            "runs when you press this.")
        self._hop_read_btn.connect('clicked', lambda _b: self.app.read_all_hop())
        right.pack_start(self._hop_read_btn, False, False, 0)
        box.pack_start(head, False, False, 0)

        self._hop_next = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._hop_next.get_style_context().add_class('callout')
        arrow = ui.NavIcon('arrow', 16)
        arrow.get_style_context().add_class('nav-icon')
        self._hop_next.pack_start(arrow, False, False, 0)
        self._hop_next_lbl = self._label('', wrap=True)
        self._hop_next.pack_start(self._hop_next_lbl, True, True, 0)
        self._hop_next.set_no_show_all(True)
        arrow.show(); self._hop_next_lbl.show()
        box.pack_start(self._hop_next, False, False, 0)

        self._hop_tiles = Gtk.FlowBox()
        self._hop_tiles.set_selection_mode(Gtk.SelectionMode.NONE)
        self._hop_tiles.set_homogeneous(True)
        self._hop_tiles.set_min_children_per_line(2)
        self._hop_tiles.set_max_children_per_line(4)
        self._hop_tiles.set_column_spacing(10)
        self._hop_tiles.set_row_spacing(10)
        box.pack_start(self._hop_tiles, False, False, 0)
        self._hop_tile_widgets: Dict[str, dict] = {}
        self._hop_tile_names: tuple = ()

        self._hop_status = self._label('', 'stat-sub', wrap=True)
        box.pack_start(self._hop_status, False, False, 0)
        return self._bezel(box)

    def _make_hop_tile(self, name: str) -> dict:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        tile.get_style_context().add_class('tile')
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        dot = self._label('●', 'dot-idle')
        top.pack_start(dot, False, False, 0)
        nm = self._label(name, 'tile-name', ellipsize=True)
        top.pack_start(nm, True, True, 0)
        tag = self._label('', 'tag')
        top.pack_end(tag, False, False, 0)
        tile.pack_start(top, False, False, 0)
        who = self._label('', 'tile-sub', ellipsize=True)
        tile.pack_start(who, False, False, 0)
        meters = {}
        for key, short in (('5h', '5h'), ('week', '7d')):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lab = self._label(short, 'tile-sub')
            lab.set_size_request(18, -1)
            row.pack_start(lab, False, False, 0)
            m = ui.Meter(height=5)
            row.pack_start(m, True, True, 0)
            num = self._label('—', 'tile-num', xalign=1)
            num.set_size_request(40, -1)
            row.pack_end(num, False, False, 0)
            tile.pack_start(row, False, False, 0)
            meters[key] = (m, num, lab)
        fresh = self._label('', 'tile-sub', ellipsize=True)
        tile.pack_start(fresh, False, False, 0)
        child = Gtk.FlowBoxChild()
        child.add(tile)
        child.show_all()
        return {'child': child, 'tile': tile, 'dot': dot, 'tag': tag, 'who': who,
                'meters': meters, 'fresh': fresh}

    def _fill_hop_tile(self, w: dict, *, live: bool, email: str, plan: str,
                       reading, usage: str, fresh: str, runs: int, note: str,
                       now: datetime):
        ctx = w['tile'].get_style_context()
        (ctx.add_class if live else ctx.remove_class)('live')
        dctx = w['dot'].get_style_context()
        dctx.remove_class('dot-live'); dctx.remove_class('dot-idle')
        dctx.add_class('dot-live' if live else 'dot-idle')
        tctx = w['tag'].get_style_context()
        for k in ('tag-ok', 'tag-muted'):
            tctx.remove_class(k)
        if live:
            w['tag'].set_text('live'); tctx.add_class('tag-ok')
        elif runs:
            w['tag'].set_text(f"{runs} hop run"); tctx.add_class('tag-muted')
        else:
            w['tag'].set_text('')
        w['tag'].set_visible(bool(w['tag'].get_text()))
        w['who'].set_text(" · ".join(x for x in (email, plan.capitalize() if plan else '') if x))
        p5 = p7 = None
        scope = None
        if reading is not None and reading.has_figures:
            p5, _l5, _w5 = claudehop.window_now(reading.pct5, reading.resets_5h, now)
            p7, _l7, _w7 = claudehop.window_now(reading.pct7, reading.resets_7d, now)
            scope = reading.week_scope
        for key, val in (('5h', p5), ('week', p7)):
            m, num, lab = w['meters'][key]
            m.set_value(val)
            num.set_text('—' if val is None else f"{val:.0f}%")
        w['meters']['week'][2].set_text('7d')
        w['meters']['week'][2].set_tooltip_text(f"Weekly meter for {scope}" if scope else "7-day window")
        line = fresh + (f" · {note}" if note else "")
        w['fresh'].set_text(line)
        w['tile'].set_tooltip_text(f"{usage}\n{line}".strip())

    def _update_hop_card(self):
        """The Accounts panel: one tile per claudehop account with its newest
        5-hour and weekly reading, any `hop run` terminals on it, and the login
        with the most room. Hidden when claudehop is not in use."""
        live = self.app._primary_state()
        try:
            hs = claudehop.load_state(live.token)
        except Exception:
            hs = None
        if hs is None or not (hs.profiles or hs.providers):
            self._hop_card.set_visible(False)
            return
        now = datetime.now(timezone.utc)
        names = [p.name for p in hs.profiles]
        readings = self.app.hop_readings(hs)
        runs = claudehop.run_terminals()
        read_at, result = self.app._hop_read_result or (None, None)
        fresh_notes = read_at is not None and now - read_at < timedelta(minutes=30)
        notes = result.notes if result is not None else {}

        since = self.app.store.hop_tracking_since()
        sub = ("The live login updates every poll; the others show their newest "
               "reading, from claudehop or from the last time they were live.")
        if since is not None:
            sub += f" Tokens are split by account since {since.astimezone():%-d %b %H:%M}."
        self._hop_sub.set_text(sub)

        order = sorted(hs.profiles, key=lambda q: (q.name != hs.active, q.name))
        keys = tuple(p.name for p in order) + tuple(f'provider:{n}' for n in hs.providers)
        if keys != self._hop_tile_names:
            for child in self._hop_tiles.get_children():
                self._hop_tiles.remove(child)
            self._hop_tile_widgets = {}
            for k in keys:
                label = k.split(':', 1)[1].capitalize() if k.startswith('provider:') else k
                w = self._make_hop_tile(label)
                self._hop_tile_widgets[k] = w
                self._hop_tiles.add(w['child'])
            self._hop_tile_names = keys

        for p in order:
            w = self._hop_tile_widgets[p.name]
            r = readings.get(p.name)
            usage, fresh = claudehop.usage_text(r, now)
            on = p.name == hs.active and not hs.provider
            note = notes.get(p.name) if fresh_notes else ''
            if fresh == 'live':
                fresh = "polled by this widget"
            self._fill_hop_tile(w, live=on, email=p.email or '', plan=p.plan or '',
                                reading=r, usage=usage, fresh=fresh,
                                runs=runs.get(p.name, 0), note=note or '', now=now)
            if p.name == hs.active and hs.provider:
                w['fresh'].set_text("login idle while a provider is on")
        for prov in hs.providers:
            w = self._hop_tile_widgets[f'provider:{prov}']
            on = prov == hs.provider
            detail = ''
            if prov == BEDROCK_ACCOUNT_LABEL:
                t = self.app.store.totals(since=now - timedelta(days=7),
                                          account=BEDROCK_ACCOUNT_LABEL)
                n = t.get('messages', 0) or 0
                detail = (f"7 days: {n:,} messages, {fmt_cost(t.get('cost_usd', 0) or 0)}"
                          if n else "no usage in 7 days")
            self._fill_hop_tile(w, live=on, email='Provider', plan='',
                                reading=None, usage='Billed per token',
                                fresh=detail or ('on' if on else 'off'), runs=0,
                                note='', now=now)
            for m, num, lab in w['meters'].values():
                m.get_parent().hide()

        pick = claudehop.most_room(readings, names, hs.active, now)
        if pick is not None:
            head = "Next, as hop next would pick" if pick.listed else "Most 5-hour room"
            body = (f"<b>{html.escape(pick.name)}</b>  "
                    f"<span foreground=\"{COLORS['muted']}\">{html.escape(pick.why)}</span>"
                    if pick.name else html.escape(pick.why))
            self._hop_next_lbl.set_markup(f"{head}: {body}")
            self._hop_next.show()
        else:
            self._hop_next.hide()

        busy = self.app._hop_reading_all
        self._hop_read_btn.set_label('Reading…' if busy else 'Read all accounts')
        self._hop_read_btn.set_sensitive(not busy)
        status = ''
        if result is not None and fresh_notes:
            when = f"{read_at.astimezone():%H:%M}"
            if result.status == 'done':
                ok = sum(1 for n in names if not notes.get(n))
                status = f"Read all accounts at {when}: {ok} of {len(names)} answered."
            else:
                status = f"{when}: {result.message}"
        self._hop_status.set_text(status)
        self._hop_status.set_visible(bool(status))
        self._hop_card.show_all()
        self._hop_next.set_visible(pick is not None)
        self._hop_status.set_visible(bool(status))
        for k, w in self._hop_tile_widgets.items():
            w['tag'].set_visible(bool(w['tag'].get_text()))
            if k.startswith('provider:'):
                for m, num, lab in w['meters'].values():
                    m.get_parent().hide()

    # The update ------------------------------------------------------------
    def _update_dashboard(self):
        store = self.app.store
        period = self._dashboard_period
        cutoff = self._cutoff(period)
        acc = self._account_filter
        self._update_cloud_section()
        self._update_side_gauge()

        # Summed in SQL. This method runs on the interface thread every few
        # seconds; reading 160k rows into Python to add up five columns froze
        # the window for about half a second on every tick.
        agg = store.totals(since=cutoff, account=acc)
        total = int(agg.get('messages') or 0)
        total_tokens = int(agg.get('total_tokens') or 0)
        total_input = int(agg.get('input_tokens') or 0)
        total_cc = int(agg.get('cache_creation') or 0)
        total_cr = int(agg.get('cache_read') or 0)
        total_output = int(agg.get('output_tokens') or 0)
        total_cost = float(agg.get('cost_usd') or 0)
        projects = int(agg.get('projects') or 0)

        label = PERIOD_LABELS.get(period, 'All Time')
        rng = range_text(period)
        self._dashboard_scope_label.set_text(
            (f"{rng} · " if rng else "")
            + f"{projects} project{'s' if projects != 1 else ''} · "
            f"{total:,} message{'s' if total != 1 else ''}")

        def put(key, value, sub):
            v, s = self._summary_cards[key]
            v.set_text(value)
            s.set_text(sub)
        put('total', fmt(total_tokens), f"{fmt_full(total_tokens)} tokens")
        put('input', fmt(total_input), "not served from cache")
        put('cache', fmt(total_cc + total_cr),
            f"{fmt(total_cc)} written · {fmt(total_cr)} read")
        put('output', fmt(total_output), "generated")
        plan, flat = subscription_summary(self.app.subscription_info)
        put('cost', fmt_cost(total_cost),
            f"you pay the {plan} flat fee" if flat else "at API prices")

        # ── Current session window ──────────────────────────────────────────
        # Anchored on the account's own reset time whenever the cloud has
        # given us one, because the window covers usage from every surface —
        # the browser, the desktop and mobile apps, other machines — and none
        # of that reaches these logs.
        cv = self.app.account_state(acc)
        cloud_5h = None
        resets_5h = cv.last_resets_5h
        hop_name = self._hop_filter()
        hop_r = self._hop_idle_reading(hop_name) if hop_name else None
        if hop_r is not None:
            p5, _, _ = claudehop.window_now(hop_r.pct5, hop_r.resets_5h,
                                            datetime.now(timezone.utc))
            cloud_5h = p5 / 100.0 if p5 is not None else None
            resets_5h = hop_r.resets_5h
        elif acc == BEDROCK_ACCOUNT_LABEL:
            resets_5h = None        # Bedrock has no plan window to anchor on
        elif cv.usage_data:
            cloud_5h = normalize_utilization(
                (cv.usage_data.get('five_hour') or {}).get('utilization', 0))
        session_rows = store.query(
            since=datetime.now(timezone.utc) - SESSION_LOOKBACK, account=acc)
        block = active_session(session_rows, resets_5h)
        fc = forecast(block, cloud_5h_pct=cloud_5h)
        if block is not None:
            source = "" if block.anchored else " · estimated from local logs"
            self._block_window_lbl.set_text(
                f"{block.start.astimezone():%H:%M} → {block.end.astimezone():%H:%M}"
                f" · {block.messages:,} message{'s' if block.messages != 1 else ''}"
                f"{source}")
            self._sess_left.set_text(fmt_duration(block.remaining().total_seconds()))
            self._sess_unit.set_text('left')
            self._sess_meter.set_value(
                100.0 * block.elapsed().total_seconds() / BLOCK_DURATION.total_seconds())
            self._sess_stats['tokens'].set_text(fmt(block.total_tokens))
            self._sess_stats['cost'].set_text(fmt_cost(block.cost_usd))
            self._sess_stats['burn'].set_text(f"{fmt(int(fc.burn_rate_per_min_tokens))}/min")
            eta = self._sess_stats['eta']
            if fc.eta_to_limit is None:
                eta.set_text("no plan data")
            else:
                secs = int(fc.eta_to_limit.total_seconds())
                eta.set_text("at the limit" if secs <= 0 else f"in {fmt_duration(secs)}")
        else:
            self._sess_left.set_text("Idle")
            self._sess_unit.set_text('')
            self._block_window_lbl.set_text("No session open. The next message starts one.")
            self._sess_meter.set_value(0)
            for v in self._sess_stats.values():
                v.set_text("—")

        self._update_chart()

        # Top projects / models (period)
        def bars(container, sub_lbl, rows, name_key, value_key, fmt_fn, unit):
            for child in container.get_children():
                container.remove(child)
            sub_lbl.set_text(f"{rng or label} · by {unit}")
            if not rows:
                container.pack_start(self._label("Nothing recorded in this period.",
                                                 'subtitle'), False, False, 0)
                container.show_all()
                return
            top_rows = rows[:6]
            vmax = max(float(r[value_key] or 0) for r in top_rows) or 1
            for r in top_rows:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                line.pack_start(self._label(r[name_key] or '(unknown)', 'bar-name',
                                            ellipsize=True), True, True, 0)
                line.pack_end(self._label(fmt_fn(r[value_key] or 0), 'bar-value'),
                              False, False, 0)
                row.pack_start(line, False, False, 0)
                m = ui.Meter(height=5, color='chart')
                m.set_value(100.0 * float(r[value_key] or 0) / vmax)
                row.pack_start(m, False, False, 0)
                container.pack_start(row, False, False, 0)
            container.show_all()

        bars(self._dashboard_projects_content, self._dashboard_projects_subtitle,
             store.project_summary(since=cutoff, account=acc), 'project',
             'total_tokens', lambda v: fmt(int(v)), 'tokens')
        bars(self._dashboard_models_content, self._dashboard_models_subtitle,
             store.model_summary(since=cutoff, account=acc), 'model',
             'cost_usd', fmt_cost, 'API-equivalent cost')

    # ── Tables ─────────────────────────────────────────────────────────────
    @staticmethod
    def _add_columns(tree: Gtk.TreeView, cols, sort_offset: Optional[int] = None):
        """Columns as (title, index, min width, kind) where kind is 'text',
        'grow' (takes the spare width) or 'num' (right-aligned figures)."""
        for title, idx, width, kind in cols:
            r = Gtk.CellRendererText()
            r.set_property('xpad', 8); r.set_property('ypad', 7)
            if kind == 'num':
                r.set_property('xalign', 1.0)
            if kind == 'grow':
                # Ask for a modest width and take the spare room, so a long
                # name ellipsizes instead of pushing the figures off the edge.
                r.set_property('ellipsize', Pango.EllipsizeMode.END)
                r.set_property('width-chars', 16)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_expand(kind == 'grow'); col.set_resizable(True)
            col.set_min_width(width)
            if kind == 'num':
                col.set_alignment(1.0)
            if sort_offset is not None:
                col.set_sort_column_id(idx + sort_offset)
            tree.append_column(col)

    def _table_panel(self, tree: Gtk.TreeView) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(tree)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        shell.get_style_context().add_class('shell')
        core = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        core.get_style_context().add_class('core')
        core.get_style_context().add_class('core-table')
        core.pack_start(scrolled, True, True, 0)
        shell.pack_start(core, True, True, 0)
        return shell

    def _period_combo(self, current: str, on_change) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for k in ('all', 'today', '7d', '30d'):
            combo.append(k, PERIOD_LABELS[k])
        combo.set_active_id(current)
        combo.connect('changed', lambda cb: on_change(cb.get_active_id() or 'all'))
        return combo

    # ── Projects view ──────────────────────────────────────────────────────
    def _build_projects_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        c.set_margin_top(10); c.set_margin_bottom(22)
        c.set_margin_start(32); c.set_margin_end(32)

        fb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._projects_search = Gtk.SearchEntry()
        self._projects_search.set_placeholder_text("Search projects")
        self._projects_search.get_style_context().add_class('search-entry')
        self._projects_search.connect('search-changed',
                                      lambda _: self._update_projects_view())
        fb.pack_start(self._projects_search, True, True, 0)

        def _set(period):
            self._projects_period = period
            self._update_projects_view()
        self._projects_period_combo = self._period_combo(self._projects_period, _set)
        fb.pack_end(self._projects_period_combo, False, False, 0)
        c.pack_start(fb, False, False, 0)

        self._projects_totals = self._label('', 'subtitle', ellipsize=True)
        self._projects_totals.set_margin_start(4)
        c.pack_start(self._projects_totals, False, False, 0)

        # TreeStore (parent = project, children = sessions). Display columns
        # 0-9; trailing columns are numeric counterparts used for sorting.
        self._projects_store = Gtk.TreeStore(
            str, str, str, str, str, str, str, str, str, str,   # 0-9 display
            # Token totals pass 2**31 (14 billion for one project), so the counts
            # are 64-bit; a 32-bit column raised OverflowError and blanked the tab.
            float, *([GObject.TYPE_INT64] * 8), float,  # 10-19 sort
        )
        treeview = Gtk.TreeView(model=self._projects_store)
        treeview.set_headers_visible(True)
        self._add_columns(treeview, [
            ('Project / session', 0, 170, 'grow'),
            ('Last used', 1, 92, 'text'),
            ('Sessions', 2, 74, 'num'),
            ('Messages', 3, 84, 'num'),
            ('Input', 4, 74, 'num'),
            ('Cache write', 5, 92, 'num'),
            ('Cache read', 6, 90, 'num'),
            ('Output', 7, 78, 'num'),
            ('Total', 8, 82, 'num'),
            ('Cost', 9, 82, 'num'),
        ], sort_offset=10)
        c.pack_start(self._table_panel(treeview), True, True, 0)

        bb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hint = self._label("Expand a project to see its 50 most recent sessions.",
                           'stat-sub')
        bb.pack_start(hint, False, False, 0)
        export_btn = Gtk.Button(label="Export CSV")
        export_btn.get_style_context().add_class('btn-secondary')
        export_btn.connect('clicked', self._export_csv)
        bb.pack_end(export_btn, False, False, 0)
        c.pack_start(bb, False, False, 0)
        return c

    def _update_projects_view(self):
        period = self._projects_period
        cutoff = self._cutoff(period)
        acc = self._account_filter
        rows = self.app.store.project_summary(since=cutoff, account=acc)
        search = self._projects_search.get_text().lower()

        rng = range_text(period) or PERIOD_LABELS.get(period, '')
        total_tokens = sum((r['total_tokens'] or 0) for r in rows)
        total_msgs = sum((r['messages'] or 0) for r in rows)
        total_cost = sum((r['cost_usd'] or 0) for r in rows)
        self._projects_totals.set_markup(
            f'<b>{len(rows)}</b> projects · <b>{fmt(total_tokens)}</b> tokens · '
            f'{total_msgs:,} messages · <b>{fmt_cost(total_cost)}</b> at API prices'
            f' · {html.escape(rng)}')

        # Session children come pre-aggregated from SQL, already newest
        # first. Grouping the raw messages here meant reading every row for
        # the period on the interface thread each refresh.
        sessions_by_project: Dict[str, list] = {}
        for srow in self.app.store.session_summary(since=cutoff, account=acc):
            sessions_by_project.setdefault(
                srow['project'] or 'Unknown', []).append(srow)

        self._projects_store.clear()
        for r in rows:
            name = r['project']
            if search and search not in (name or '').lower():
                continue
            last_used_dt = (datetime.fromisoformat(r['last_used'])
                            if r['last_used'] else None)
            parent = self._projects_store.append(None, [
                name,
                rel_time(last_used_dt),
                f"{r['sessions'] or 0:,}",
                f"{r['messages'] or 0:,}",
                fmt(r['input_tokens'] or 0),
                fmt(r['cache_creation'] or 0),
                fmt(r['cache_read'] or 0),
                fmt(r['output_tokens'] or 0),
                fmt(r['total_tokens'] or 0),
                fmt_cost(r['cost_usd'] or 0),
                last_used_dt.timestamp() if last_used_dt else 0,
                r['sessions'] or 0,
                r['messages'] or 0,
                r['input_tokens'] or 0,
                r['cache_creation'] or 0,
                r['cache_read'] or 0,
                r['output_tokens'] or 0,
                r['total_tokens'] or 0,
                int((r['cost_usd'] or 0) * 1000),
                r['cost_usd'] or 0,
            ])
            # Attach sessions as expandable children — capped to the 50 most
            # recent so a project with hundreds of sessions doesn't bloat the
            # tree.
            for srow in (sessions_by_project.get(name) or [])[:50]:
                first = datetime.fromisoformat(srow['first_used'])
                last = datetime.fromisoformat(srow['last_used'])
                cost = srow['cost_usd'] or 0
                self._projects_store.append(parent, [
                    f"{first.astimezone():%d %b %H:%M} · {srow['session_id'][:8]}",
                    rel_time(last),
                    "—",
                    f"{srow['messages'] or 0:,}",
                    fmt(srow['input_tokens'] or 0),
                    fmt(srow['cache_creation'] or 0),
                    fmt(srow['cache_read'] or 0),
                    fmt(srow['output_tokens'] or 0),
                    fmt(srow['total_tokens'] or 0),
                    fmt_cost(cost),
                    last.timestamp(),
                    0,
                    srow['messages'] or 0,
                    srow['input_tokens'] or 0,
                    srow['cache_creation'] or 0,
                    srow['cache_read'] or 0,
                    srow['output_tokens'] or 0,
                    srow['total_tokens'] or 0,
                    int(cost * 1000),
                    cost,
                ])

    # ── Breakdowns view ────────────────────────────────────────────────────
    def _build_breakdowns_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        c.set_margin_top(10); c.set_margin_bottom(22)
        c.set_margin_start(32); c.set_margin_end(32)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._breakdowns_stack = Gtk.Stack()
        self._breakdowns_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._breakdowns_stack.set_transition_duration(180)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self._breakdowns_stack)
        top.pack_start(switcher, False, False, 0)
        self._breakdowns_totals = self._label('', 'subtitle', ellipsize=True)
        top.pack_start(self._breakdowns_totals, True, True, 0)

        def _set(period):
            self._breakdowns_period = period
            self._update_breakdowns_view()
        self._breakdowns_period_combo = self._period_combo(self._breakdowns_period, _set)
        top.pack_end(self._breakdowns_period_combo, False, False, 0)
        c.pack_start(top, False, False, 0)

        # Share of the period, as bars: the table below has the exact figures.
        share = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        head, self._share_title, self._share_sub, _r = self._panel_head('Share')
        share.pack_start(head, False, False, 0)
        self._share_rows = Gtk.Grid()
        self._share_rows.set_column_spacing(28); self._share_rows.set_row_spacing(12)
        self._share_rows.set_column_homogeneous(True)
        share.pack_start(self._share_rows, False, False, 0)
        c.pack_start(self._bezel(share), False, False, 0)

        self._models_store = Gtk.ListStore(str, str, str, str, str, str, str, str)
        models_tv = Gtk.TreeView(model=self._models_store)
        self._add_columns(models_tv, [
            ('Model', 0, 220, 'grow'), ('Messages', 1, 90, 'num'),
            ('Input', 2, 84, 'num'), ('Cache write', 3, 100, 'num'),
            ('Cache read', 4, 96, 'num'), ('Output', 5, 84, 'num'),
            ('Total', 6, 90, 'num'), ('Cost', 7, 90, 'num')])
        self._breakdowns_stack.add_titled(self._table_panel(models_tv), 'models', 'Models')

        self._tools_store = Gtk.ListStore(str, str, str, str, str, str)
        tools_tv = Gtk.TreeView(model=self._tools_store)
        self._add_columns(tools_tv, [
            ('Tool', 0, 220, 'grow'), ('Calls', 1, 80, 'num'),
            ('Messages', 2, 90, 'num'), ('Input', 3, 96, 'num'),
            ('Output', 4, 96, 'num'), ('Cost of those turns', 5, 140, 'num')])
        self._breakdowns_stack.add_titled(self._table_panel(tools_tv), 'tools', 'Tools')

        c.pack_start(self._breakdowns_stack, True, True, 0)
        self._breakdowns_stack.set_visible_child_name(self._breakdowns_kind)
        self._breakdowns_stack.connect(
            'notify::visible-child-name',
            lambda *_: self._on_breakdowns_kind_changed())
        self._breakdown_rows = {'models': [], 'tools': []}
        return c

    def _on_breakdowns_kind_changed(self):
        kind = self._breakdowns_stack.get_visible_child_name() or 'models'
        self._breakdowns_kind = kind
        self._update_breakdowns_totals()

    def _update_breakdowns_view(self):
        cutoff = self._cutoff(self._breakdowns_period)
        acc = self._account_filter
        store = self.app.store

        models = store.model_summary(since=cutoff, account=acc)
        self._models_store.clear()
        for r in models:
            self._models_store.append([
                r['model'] or '(unknown)',
                f"{r['messages'] or 0:,}",
                fmt(r['input_tokens'] or 0),
                fmt(r['cache_creation'] or 0),
                fmt(r['cache_read'] or 0),
                fmt(r['output_tokens'] or 0),
                fmt(r['total_tokens'] or 0),
                fmt_cost(r['cost_usd'] or 0),
            ])

        tools = store.tool_summary(since=cutoff, account=acc)
        self._tools_store.clear()
        for r in tools:
            self._tools_store.append([
                r['name'],
                f"{int(r['calls']):,}",
                f"{int(r['messages']):,}",
                fmt(int(r['input_tokens'])),
                fmt(int(r['output_tokens'])),
                fmt_cost(r['cost_usd']),
            ])
        self._breakdown_rows = {'models': models, 'tools': tools}
        self._update_breakdowns_totals()

    def _update_breakdowns_totals(self):
        kind = self._breakdowns_kind
        rng = range_text(self._breakdowns_period) or PERIOD_LABELS.get(
            self._breakdowns_period, '')
        rows = self._breakdown_rows.get(kind) or []
        if kind == 'models':
            self._breakdowns_totals.set_text(f"{len(rows)} models · {rng}")
            self._share_title.set_text("Share of cost")
            self._share_sub.set_text("API-equivalent cost by model, largest first.")
            items = [(r['model'] or '(unknown)', float(r['cost_usd'] or 0),
                      fmt_cost(r['cost_usd'] or 0)) for r in rows]
        else:
            self._breakdowns_totals.set_text(
                f"{len(rows)} tools · {rng} · a turn's cost counts once for "
                "each tool it called")
            self._share_title.set_text("Share of calls")
            self._share_sub.set_text("Tool calls, largest first.")
            items = [(r['name'], float(r['calls'] or 0), f"{int(r['calls']):,} calls")
                     for r in rows]
        for child in self._share_rows.get_children():
            self._share_rows.remove(child)
        total = sum(v for _n, v, _t in items) or 1.0
        if not items:
            self._share_rows.attach(self._label("Nothing recorded in this period.",
                                                'subtitle'), 0, 0, 1, 1)
        for i, (name, value, text) in enumerate(items[:8]):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.pack_start(self._label(name, 'bar-name', ellipsize=True), True, True, 0)
            line.pack_end(self._label(f"{100 * value / total:.1f}%", 'bar-value'),
                          False, False, 0)
            cell.pack_start(line, False, False, 0)
            m = ui.Meter(height=5, color='chart')
            m.set_value(100 * value / total)
            cell.pack_start(m, False, False, 0)
            cell.set_tooltip_text(text)
            self._share_rows.attach(cell, i % 2, i // 2, 1, 1)
        self._share_rows.show_all()

    # ── Budgets view ───────────────────────────────────────────────────────
    def _build_budgets_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        c.set_margin_top(10); c.set_margin_bottom(28)
        c.set_margin_start(32); c.set_margin_end(32)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        head, _t, _s, _r = self._panel_head(
            'Your budgets',
            "A budget warns you when you cross a limit: dollars or tokens per "
            "day, week or month, or a share of your plan's 5-hour or 7-day "
            "window. Plan budgets need a working OAuth token.")
        left.pack_start(head, False, False, 0)
        self._budgets_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.pack_start(self._budgets_list, False, False, 0)
        lp = self._bezel(left)
        lp.set_valign(Gtk.Align.START)
        c.pack_start(lp, True, True, 0)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        fh, _t, _s, _r = self._panel_head('Add a budget')
        form.pack_start(fh, False, False, 0)

        self._budget_name = Gtk.Entry()
        self._budget_name.set_placeholder_text("Monthly cap")
        self._budget_scope = Gtk.Entry()
        self._budget_scope.set_text("global")
        self._budget_scope.set_placeholder_text("global, project:NAME or model:ID")
        self._budget_period = Gtk.ComboBoxText()
        for pid, plabel in (('day', 'Day'), ('week', 'Week'), ('month', 'Month'),
                            ('5h', '5-hour plan window'), ('7d', '7-day plan window')):
            self._budget_period.append(pid, plabel)
        self._budget_period.set_active_id('month')
        self._budget_usd = Gtk.Entry()
        self._budget_usd.set_placeholder_text("e.g. 150")
        self._budget_tokens = Gtk.Entry()
        self._budget_tokens.set_placeholder_text("e.g. 500000000")
        self._budget_plan_pct = Gtk.Entry()
        self._budget_plan_pct.set_placeholder_text("e.g. 80, with a plan window")
        self._budget_notify_pct = Gtk.SpinButton.new_with_range(50, 100, 5)
        self._budget_notify_pct.set_value(80)

        def field(label, widget, hint=None):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            box.pack_start(self._label(label, 'stat-label'), False, False, 0)
            box.pack_start(widget, False, False, 0)
            if hint:
                box.pack_start(self._label(hint, 'stat-sub', wrap=True), False, False, 0)
            form.pack_start(box, False, False, 0)
        field("Name", self._budget_name)
        field("Scope", self._budget_scope)
        field("Period", self._budget_period)
        limits = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        limits.set_homogeneous(True)
        for label, w in (("Dollars", self._budget_usd), ("Tokens", self._budget_tokens)):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            b.pack_start(self._label(label, 'stat-label'), False, False, 0)
            b.pack_start(w, False, False, 0)
            limits.pack_start(b, True, True, 0)
        form.pack_start(limits, False, False, 0)
        field("Plan share (%)", self._budget_plan_pct,
              "Only with a 5-hour or 7-day period, and scope global.")
        field("Warn me at (%) of the limit", self._budget_notify_pct)

        add_btn = Gtk.Button(label="Add budget")
        add_btn.get_style_context().add_class('btn-primary')
        add_btn.set_halign(Gtk.Align.START)
        add_btn.set_margin_top(4)
        add_btn.connect('clicked', self._on_budget_add)
        form.pack_start(add_btn, False, False, 0)
        self._budget_error = self._label('', 'stat-sub', wrap=True)
        form.pack_start(self._budget_error, False, False, 0)
        panel = self._bezel(form)
        panel.set_size_request(360, -1)
        panel.set_valign(Gtk.Align.START)
        c.pack_start(panel, False, False, 0)
        return c

    def _update_budgets_view(self):
        for child in self._budgets_list.get_children():
            self._budgets_list.remove(child)
        states = evaluate_budgets(self.app.store, usage_data=self.app.usage_data)
        if not states:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            empty.get_style_context().add_class('notice')
            empty.pack_start(self._label("No budgets yet", 'eyebrow-strong'), False, False, 0)
            empty.pack_start(self._label(
                "Add one on the right. A monthly dollar cap is a good start; a "
                "plan budget at 80% of the 5-hour window warns you before Claude "
                "slows you down.", 'subtitle', wrap=True), False, False, 0)
            self._budgets_list.pack_start(empty, False, False, 0)
            self._budgets_list.show_all()
            return
        for s in states:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            row.get_style_context().add_class('tile')
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            kind = ' · plan share' if s.is_pct_based else ''
            name = self._label('', ellipsize=True)
            name.set_markup(f"<b>{html.escape(s.name)}</b>  "
                            f"<span foreground=\"{COLORS['muted']}\">"
                            f"{html.escape(s.scope)} · {s.period}{kind}</span>")
            top.pack_start(name, True, True, 0)
            del_btn = Gtk.Button(label="Remove")
            del_btn.get_style_context().add_class('btn-link')
            del_btn.connect('clicked', lambda _, bid=s.id: self._on_budget_remove(bid))
            top.pack_end(del_btn, False, False, 0)
            row.pack_start(top, False, False, 0)

            if s.is_pct_based:
                limit = f"{s.limit_pct:.0f}% of the plan window"
                if s.data_available:
                    spent = f"{s.spent_pct:.0f}% used"
                    pct = s.pct * 100
                else:
                    spent, pct = "no plan data yet", None
            else:
                spent = (fmt_cost(s.spent_usd) if s.limit_usd
                         else f"{s.spent_tokens:,} tokens")
                limit = (fmt_cost(s.limit_usd) if s.limit_usd
                         else f"{s.limit_tokens:,} tokens" if s.limit_tokens else '—')
                pct = s.pct * 100
            m = ui.Meter(height=6)
            m.set_value(pct)
            row.pack_start(m, False, False, 0)
            row.pack_start(self._label(
                f"{'—' if pct is None else f'{pct:.0f}%'} · {spent} of {limit} · "
                f"warns at {s.notify_at_pct}% · period {s.period_key}",
                'tile-sub', ellipsize=True), False, False, 0)
            self._budgets_list.pack_start(row, False, False, 0)
        self._budgets_list.show_all()

    def _on_budget_add(self, _btn):
        def fail(msg):
            self._budget_error.set_markup(
                f"<span foreground=\"{COLORS['danger']}\">{html.escape(msg)}</span>")
        name = self._budget_name.get_text().strip()
        scope = self._budget_scope.get_text().strip() or 'global'
        period = self._budget_period.get_active_id() or 'month'
        usd_text = self._budget_usd.get_text().strip()
        tok_text = self._budget_tokens.get_text().strip()
        plan_pct_text = self._budget_plan_pct.get_text().strip()
        notify_pct = int(self._budget_notify_pct.get_value())
        if not name:
            return fail("Give the budget a name.")
        try:
            usd = float(usd_text) if usd_text else None
            tokens = int(tok_text) if tok_text else None
            plan_pct = float(plan_pct_text) if plan_pct_text else None
        except ValueError:
            return fail("Dollars, tokens and plan share must be numbers.")
        if not (usd or tokens or plan_pct):
            return fail("Set a dollar, token or plan-share limit.")
        try:
            self.app.store.add_budget(name, scope, period, usd, tokens,
                                      notify_pct, plan_pct)
        except ValueError as e:
            return fail(str(e))
        self._budget_error.set_text("")
        self._budget_name.set_text(""); self._budget_usd.set_text("")
        self._budget_tokens.set_text(""); self._budget_plan_pct.set_text("")
        self._update_budgets_view()
        self._status_label.set_text(f"Added budget: {name}")

    def _on_budget_remove(self, bid: int):
        self.app.store.delete_budget(bid)
        self._update_budgets_view()

    def _cloud_view(self):
        """(AccountState, normalized_cloud_state) for the account the dashboard
        is filtered to. Cloud usage is per-account and can't be aggregated, so
        for 'All accounts' we fall back to the primary account's view."""
        st = self.app.account_state(self._account_filter)
        state = st.cloud_state if st.token else 'no_token'
        return st, state

    def _hop_filter(self) -> Optional[str]:
        """The claudehop account the dashboard is filtered to, if any."""
        f = self._account_filter
        return f[len(HOP_PREFIX):] if f and f.startswith(HOP_PREFIX) else None

    def _hop_idle_reading(self, name: str):
        """The last reading of a claudehop account that is not the live login,
        or None when it is live (the primary account's own polling covers it)."""
        live = self.app._primary_state()
        try:
            hs = claudehop.load_state(live.token)
        except Exception:
            hs = None
        if hs is not None and hs.active == name:
            return None
        return claudehop.all_readings([name], self.app.store.hop_usage()).get(name) \
            or claudehop.Reading(None, None, None, None, None, None, 'widget')

    def _update_hop_cloud(self, name: str) -> bool:
        """Plan rings for a claudehop account that is not the live login, from
        its last reading. False when it is live, so the caller shows the live
        figures instead."""
        r = self._hop_idle_reading(name)
        if r is None:
            return False
        now = datetime.now(timezone.utc)
        self._plan_eyebrow.set_text("Plan limits · last reading")
        self._plan_title.set_text(name)
        self._plan_foot.set_text('')
        if r.has_figures:
            p5, _l5, was5 = claudehop.window_now(r.pct5, r.resets_5h, now)
            p7, _l7, was7 = claudehop.window_now(r.pct7, r.resets_7d, now)
            self._set_rings(p5, r.resets_5h if was5 is None else None,
                            p7, r.resets_7d if was7 is None else None,
                            r.week_scope, was5=was5, was7=was7)
            who = {'probe': "claudehop, through claude", 'claudehop': "claudehop",
                   'widget': "this widget while it was live"}.get(r.source, r.source)
            self._set_tag(f"Read {claudehop.ago(r.at, now)}", 'muted')
            self._set_notice(
                f"Only the live login is polled. This reading was taken by {who}. "
                f"Hop to {name}, or read every account now.",
                action='read', label='Read all accounts')
        else:
            self._set_rings(None, None, None, None, None)
            self._set_tag("No reading", 'muted')
            self._set_notice(
                "Only the live login is polled, and nothing has read this "
                "account yet.", action='read', label='Read all accounts')
        return True

    # ── Plan-limits section (lives inside the dashboard view) ──────────────
    def _update_cloud_section(self):
        self._update_hop_card()
        hop_name = self._hop_filter()
        if hop_name and self._update_hop_cloud(hop_name):
            return
        if self._account_filter == BEDROCK_ACCOUNT_LABEL:
            self._plan_eyebrow.set_text("Amazon Bedrock")
            self._plan_title.set_text("Billed per token")
            self._set_tag("No plan windows", 'muted')
            self._set_rings(None, None, None, None, None)
            self._set_notice(
                "Bedrock has no 5-hour or weekly windows. Costs here are an "
                "estimate at Anthropic list price; AWS Cost Explorer has the "
                "real charge.")
            self._plan_foot.set_text('')
            return

        cv, state = self._cloud_view()
        primary = self.app._primary_state()
        plan_name, _flat = subscription_summary(cv.subscription_info)
        try:
            hs = claudehop.load_state(primary.token)
        except Exception:
            hs = None
        if hop_name or (cv is primary and hs and hs.active and not hs.provider):
            title = hop_name or hs.active
        elif len(self.app._accounts) > 1:
            title = cv.account.label
        else:
            title = plan_name
        eyebrow = f"Plan limits · {plan_name}"
        if self._account_filter is None and len(self.app._accounts) > 1:
            eyebrow += " · primary account"
        self._plan_eyebrow.set_text(eyebrow)
        self._plan_title.set_text(title)

        data = cv.usage_data if cv.usage_data else None
        if data:
            five = data.get('five_hour') or {}
            week = weekly_window(data) or {}
            p5 = utilization_pct(five.get('utilization'))
            r5 = five.get('resets_at')
            p7 = utilization_pct(week.get('utilization')) if week else None
            r7, scope = week.get('resets_at'), week.get('scope')
            models = [m for m in extract_model_limits(data) if m['model'] != scope]
        else:
            p5, r5 = cv.last_pct5, cv.last_resets_5h
            p7, r7, scope = cv.last_pct7, cv.last_resets_7d, cv.last_week_scope
            models = [m for m in cv.last_model_limits if m['model'] != scope]
        dimmed = state in ('auth_error', 'network_error', 'rate_limited', 'no_token')
        self._set_rings(p5, r5, p7, r7, scope, dimmed=dimmed, models=models)

        if state == 'no_token':
            self._set_tag("Not connected", 'muted')
            self._set_notice("No OAuth token. Run `claude` to log in, or paste a "
                             "token.", action='token', label='Set token')
            self._plan_foot.set_text('')
        elif state == 'rate_limited':
            extra = ""
            if cv.cloud_retry_at:
                secs = max(0, int((cv.cloud_retry_at - datetime.now(timezone.utc))
                                  .total_seconds()))
                extra = f" Next try in {fmt_duration(secs)}."
            self._set_tag("Rate limited", 'warn')
            self._set_notice((cv.cloud_error or "Claude.ai is rate-limiting this "
                              "token.") + extra + " The rings show the last reading.",
                             'warn', action='retry', label='Try now')
            self._plan_foot.set_text('')
        elif state == 'auth_error':
            self._set_tag("Token rejected", 'danger')
            self._set_notice(cv.cloud_error or "The OAuth token expired or is "
                             "invalid. Run `claude` to refresh it.", 'danger',
                             action='retry', label='Retry')
            self._plan_foot.set_text('')
        elif state == 'network_error':
            self._set_tag("Offline", 'danger')
            self._set_notice(f"Couldn't reach Claude.ai: {cv.cloud_error or 'network error'}.",
                             'danger', action='retry', label='Retry')
            self._plan_foot.set_text('')
        elif state == 'loading' and not data and p5 is None:
            self._set_tag("Connecting", 'muted')
            self._set_notice(None)
            self._plan_foot.set_text("Asking api.anthropic.com for the current windows…")
        else:
            self._set_tag("Live", 'ok')
            self._set_notice(None)
            when = (cv.last_read_at.astimezone().strftime('%H:%M')
                    if cv.last_read_at else '—')
            every = max(1, self.app._settings.poll_interval_seconds // 60)
            self._plan_foot.set_text(
                f"Updated {when} · checked every {every} min · "
                f"{ui.level_word(p5).capitalize()} in this window")

    # ── Settings view ──────────────────────────────────────────────────────
    def _apply_theme(self, pref: str):
        COLORS.clear()
        COLORS.update(_resolve_palette(pref))
        new_provider = Gtk.CssProvider()
        new_provider.load_from_data(_make_css().encode())
        screen = self.get_screen()
        Gtk.StyleContext.remove_provider_for_screen(
            screen, self._css_provider)
        Gtk.StyleContext.add_provider_for_screen(
            screen, new_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = new_provider
        # Drawn widgets read the palette when they draw; markup-coloured
        # labels are rebuilt by the refresh.
        self.queue_draw()
        self.refresh_data()
        self.app._rebuild_tray()

    def _on_system_scheme_changed(self, *_args):
        from .config import load_settings
        if load_settings().theme == 'system':
            self._apply_theme('system')

    def _on_theme_changed(self, combo):
        from .config import load_settings, save_settings
        st = load_settings()
        st.theme = combo.get_active_id() or 'system'
        save_settings(st)
        self._apply_theme(st.theme)

    def _build_settings_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        c.set_margin_top(24); c.set_margin_bottom(24)
        c.set_margin_start(24); c.set_margin_end(24)

        cloud = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cloud.get_style_context().add_class('card')
        t = Gtk.Label(label="Cloud connection")
        t.get_style_context().add_class('card-title')
        t.set_halign(Gtk.Align.START)
        cloud.pack_start(t, False, False, 0)
        d = Gtk.Label(label="Token is read from ~/.claude/.credentials.json. "
                            "Paste one manually if you don't have the `claude` "
                            "CLI on this machine.")
        d.get_style_context().add_class('subtitle')
        d.set_halign(Gtk.Align.START); d.set_line_wrap(True)
        cloud.pack_start(d, False, False, 8)
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tb.set_margin_top(12)
        self._settings_token = Gtk.Entry()
        self._settings_token.set_placeholder_text("sk-ant-oat01-...")
        self._settings_token.set_visibility(False)
        self._settings_token.set_hexpand(True)
        if self.app.token:
            self._settings_token.set_text(self.app.token[:20] + "...")
        tb.pack_start(self._settings_token, True, True, 0)
        self._token_saved_label = Gtk.Label()
        self._token_saved_label.set_no_show_all(True)
        tb.pack_end(self._token_saved_label, False, False, 0)
        sb = Gtk.Button(label="Save token")
        sb.get_style_context().add_class('btn-primary')
        sb.connect('clicked', self._on_save_token)
        tb.pack_end(sb, False, False, 0)
        cloud.pack_start(tb, False, False, 0)
        c.pack_start(cloud, False, False, 0)

        # Appearance section
        ap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ap.get_style_context().add_class('card')
        ap.set_margin_top(16)
        at = Gtk.Label(label="Appearance")
        at.get_style_context().add_class('card-title')
        at.set_halign(Gtk.Align.START)
        ap.pack_start(at, False, False, 0)
        arow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        arow.set_margin_top(8)
        al = Gtk.Label(label="Theme")
        al.get_style_context().add_class('subtitle')
        arow.pack_start(al, False, False, 0)
        self._theme_combo = Gtk.ComboBoxText()
        self._theme_combo.append('system', 'System')
        self._theme_combo.append('light', 'Light')
        self._theme_combo.append('dark', 'Dark')
        from .config import load_settings as _ls
        self._theme_combo.set_active_id(_ls().theme)
        self._theme_combo.connect('changed', self._on_theme_changed)
        arow.pack_start(self._theme_combo, False, False, 0)
        ap.pack_start(arow, False, False, 0)
        c.pack_start(ap, False, False, 0)

        # Rate card section
        rc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        rc_box.get_style_context().add_class('card')
        rc_box.set_margin_top(16)
        rt = Gtk.Label(label="Rate card")
        rt.get_style_context().add_class('card-title')
        rt.set_halign(Gtk.Align.START)
        rc_box.pack_start(rt, False, False, 0)
        from .config import RATE_CARD_FILE
        rd = Gtk.Label()
        rd.set_halign(Gtk.Align.START); rd.set_line_wrap(True); rd.set_xalign(0)
        rd.get_style_context().add_class('subtitle')
        rd.set_markup(
            "Override pricing without editing code. Drop a JSON file at:\n"
            f"<tt>{html.escape(str(RATE_CARD_FILE))}</tt>\n"
            "Schema: <tt>{\"models\": {\"claude-opus-4-7\": "
            "[input, write_5m, write_1h, read, output]}}</tt> "
            "(USD per million tokens)."
        )
        rc_box.pack_start(rd, False, False, 0)
        reprice_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reprice_row.set_margin_top(8)
        reprice_btn = Gtk.Button(label="Reload rate card & reprice history")
        reprice_btn.get_style_context().add_class('btn-secondary')
        reprice_btn.connect('clicked', self._on_reprice)
        reprice_row.pack_start(reprice_btn, False, False, 0)
        self._reprice_saved_label = Gtk.Label()
        self._reprice_saved_label.set_no_show_all(True)
        reprice_row.pack_start(self._reprice_saved_label, False, False, 0)
        rc_box.pack_start(reprice_row, False, False, 0)
        c.pack_start(rc_box, False, False, 0)

        # Accounts card
        accounts_card = self._build_accounts_card()
        accounts_card.set_margin_top(16)
        c.pack_start(accounts_card, False, False, 0)

        # Notifications card
        notif_card = self._build_notifications_card()
        notif_card.set_margin_top(16)
        c.pack_start(notif_card, False, False, 0)

        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        about.get_style_context().add_class('card')
        about.set_margin_top(16)
        at = Gtk.Label(label="About")
        at.get_style_context().add_class('card-title')
        at.set_halign(Gtk.Align.START)
        about.pack_start(at, False, False, 0)
        v = Gtk.Label(label=f"Version {APP_VERSION} · GUI + CLI (`ctt`)")
        v.get_style_context().add_class('subtitle')
        v.set_halign(Gtk.Align.START)
        about.pack_start(v, False, False, 8)
        d = Gtk.Label(label=(
            "Unofficial — not affiliated with or endorsed by Anthropic. "
            "“Claude” and “Anthropic” are trademarks of "
            "Anthropic, PBC."))
        d.get_style_context().add_class('subtitle')
        d.set_halign(Gtk.Align.START)
        d.set_line_wrap(True)
        d.set_xalign(0)
        about.pack_start(d, False, False, 0)
        c.pack_start(about, False, False, 0)
        return c

    def _build_accounts_card(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class('card')

        t = Gtk.Label(label="Accounts")
        t.get_style_context().add_class('card-title')
        t.set_halign(Gtk.Align.START)
        box.pack_start(t, False, False, 0)

        d = Gtk.Label(label=(
            "Add one row per Claude account you want to track. Each "
            "config dir must contain a .credentials.json (run "
            "`claude login` against it). “No poll” disables background "
            "API calls for that account; “Hide from tray” keeps it in "
            "the dashboard but skips it in the menu-bar label."))
        d.get_style_context().add_class('subtitle')
        d.set_halign(Gtk.Align.START); d.set_line_wrap(True); d.set_xalign(0)
        box.pack_start(d, False, False, 8)

        # Account rows: real Entry / CheckButton widgets so we don't depend
        # on cell-renderer theming, which is unreliable across distros.
        self._accounts_rows: List[dict] = []
        self._accounts_rows_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._accounts_rows_box.set_margin_top(8)
        box.pack_start(self._accounts_rows_box, False, False, 0)

        for a in self.app._accounts:
            self._add_account_row(a.label, str(a.claude_dir),
                                  a.disable_polling, a.hide_from_tray)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_margin_top(12)
        add_btn = Gtk.Button(label="+ Add account")
        add_btn.get_style_context().add_class('btn-secondary')
        add_btn.connect('clicked', lambda _: self._add_account_row(
            f"account{len(self._accounts_rows) + 1}",
            str(Path.home() / '.claude'), False, False))
        button_row.pack_start(add_btn, False, False, 0)

        # Inline save-confirmation label that appears next to the button and
        # auto-fades. The status bar at the bottom is too small / too easy to
        # miss, and gets overwritten by the next scan-loop tick.
        self._accounts_saved_label = Gtk.Label()
        self._accounts_saved_label.set_no_show_all(True)
        button_row.pack_end(self._accounts_saved_label, False, False, 0)

        save_btn = Gtk.Button(label="Save accounts")
        save_btn.get_style_context().add_class('btn-primary')
        save_btn.connect('clicked', self._on_save_accounts)
        button_row.pack_end(save_btn, False, False, 0)
        box.pack_start(button_row, False, False, 0)
        return box

    def _add_account_row(self, label: str, claude_dir: str,
                         no_poll: bool, hide_tray: bool):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        label_entry = Gtk.Entry()
        label_entry.set_text(label)
        label_entry.set_placeholder_text("Label")
        label_entry.set_width_chars(10)
        row.pack_start(label_entry, False, False, 0)

        dir_entry = Gtk.Entry()
        dir_entry.set_text(claude_dir)
        dir_entry.set_placeholder_text("~/.claude")
        dir_entry.set_hexpand(True)
        row.pack_start(dir_entry, True, True, 0)

        # Switches instead of CheckButtons — CheckButton's check glyph
        # rendered as a dark unclickable square under the user's GTK theme.
        # Switch is the same widget that already works on the Notifications
        # card for the burn-rate toggle.
        no_poll_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        no_poll_lbl = Gtk.Label(label="No poll")
        no_poll_lbl.get_style_context().add_class('subtitle')
        no_poll_box.pack_start(no_poll_lbl, False, False, 0)
        no_poll_switch = Gtk.Switch()
        no_poll_switch.set_active(no_poll)
        no_poll_switch.set_tooltip_text(
            "Skip background API polling for this account.")
        no_poll_box.pack_start(no_poll_switch, False, False, 0)
        row.pack_start(no_poll_box, False, False, 0)

        hide_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hide_lbl = Gtk.Label(label="Hide tray")
        hide_lbl.get_style_context().add_class('subtitle')
        hide_box.pack_start(hide_lbl, False, False, 0)
        hide_switch = Gtk.Switch()
        hide_switch.set_active(hide_tray)
        hide_switch.set_tooltip_text(
            "Hide this account from the menu-bar label "
            "(still shown in the dashboard).")
        hide_box.pack_start(hide_switch, False, False, 0)
        row.pack_start(hide_box, False, False, 0)

        remove_btn = Gtk.Button(label="×")
        remove_btn.get_style_context().add_class('btn-secondary')
        remove_btn.set_tooltip_text("Remove this account")
        record = {
            'label_entry': label_entry,
            'dir_entry': dir_entry,
            'no_poll': no_poll_switch,
            'hide_tray': hide_switch,
            'row': row,
        }
        remove_btn.connect('clicked',
                           lambda _b, rec=record: self._remove_account_row(rec))
        row.pack_start(remove_btn, False, False, 0)

        self._accounts_rows.append(record)
        self._accounts_rows_box.pack_start(row, False, False, 0)
        row.show_all()

    def _remove_account_row(self, record: dict):
        if len(self._accounts_rows) <= 1:
            self._status_label.set_text("Need at least one account.")
            return
        self._accounts_rows.remove(record)
        self._accounts_rows_box.remove(record['row'])

    def _on_save_accounts(self, _btn):
        accounts: List[Account] = []
        for rec in self._accounts_rows:
            label = rec['label_entry'].get_text().strip()
            cdir = rec['dir_entry'].get_text().strip()
            if not label or not cdir:
                continue
            accounts.append(Account(
                label=label,
                claude_dir=Path(cdir).expanduser(),
                disable_polling=rec['no_poll'].get_active(),
                hide_from_tray=rec['hide_tray'].get_active(),
            ))
        if not accounts:
            self._flash_saved(self._accounts_saved_label,
                              "Need at least one account.",
                              ok=False)
            return
        save_accounts(accounts)
        self.app.reload_accounts(accounts)
        self._flash_saved(
            self._accounts_saved_label,
            f"✓ Saved {len(accounts)} account{'s' if len(accounts) != 1 else ''}",
        )
        self._status_label.set_text(
            f"Saved {len(accounts)} account(s) — tray updating…")

    def _flash_saved(self, label: Gtk.Label, text: str, ok: bool = True,
                     duration_ms: int = 3000):
        """Show ``text`` next to a Save button, then fade after ``duration_ms``.

        Replaces any previous timeout so spamming Save resets the timer
        instead of leaving an old message."""
        color = COLORS['green'] if ok else COLORS['red']
        label.set_markup(
            f'<span foreground="{color}" font_weight="bold">'
            f'{html.escape(text)}</span>')
        label.show()
        # Cancel any prior fade-out so multiple saves don't stack timers.
        prev = getattr(label, '_flash_source_id', None)
        if prev:
            try:
                GLib.source_remove(prev)
            except Exception:
                pass

        def clear():
            label.set_text("")
            label.hide()
            label._flash_source_id = None  # type: ignore[attr-defined]
            return False

        label._flash_source_id = GLib.timeout_add(  # type: ignore[attr-defined]
            duration_ms, clear)

    def _build_notifications_card(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class('card')

        t = Gtk.Label(label="Notifications")
        t.get_style_context().add_class('card-title')
        t.set_halign(Gtk.Align.START)
        box.pack_start(t, False, False, 0)

        d = Gtk.Label(label=(
            "Threshold alerts fire once per window per level (warn → "
            "critical → max). State persists across restarts and "
            "re-arms when Anthropic resets the window. Burn-rate "
            "alerts (7d only) fire when your pace exceeds the "
            "multiplier — higher = quieter."))
        d.get_style_context().add_class('subtitle')
        d.set_halign(Gtk.Align.START); d.set_line_wrap(True); d.set_xalign(0)
        box.pack_start(d, False, False, 8)

        s = self.app._settings
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.set_margin_top(8)

        def label(text: str) -> Gtk.Label:
            l = Gtk.Label(label=text)
            l.set_halign(Gtk.Align.START)
            return l

        self._poll_spin = Gtk.SpinButton.new_with_range(60, 3600, 30)
        self._poll_spin.set_value(s.poll_interval_seconds)
        grid.attach(label("Poll interval (seconds)"), 0, 0, 1, 1)
        grid.attach(self._poll_spin, 1, 0, 1, 1)

        self._warn_spin = Gtk.SpinButton.new_with_range(1, 99, 1)
        self._warn_spin.set_value(s.thresholds.get('warn', 60))
        grid.attach(label("Warn threshold (%)"), 0, 1, 1, 1)
        grid.attach(self._warn_spin, 1, 1, 1, 1)

        self._crit_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        self._crit_spin.set_value(s.thresholds.get('critical', 85))
        grid.attach(label("Critical threshold (%)"), 0, 2, 1, 1)
        grid.attach(self._crit_spin, 1, 2, 1, 1)

        self._burn_switch = Gtk.Switch()
        self._burn_switch.set_active(bool(s.burn_rate.get('enabled', False)))
        self._burn_switch.set_halign(Gtk.Align.START)
        grid.attach(label("Burn-rate alerts (7d)"), 0, 3, 1, 1)
        grid.attach(self._burn_switch, 1, 3, 1, 1)

        self._mult_spin = Gtk.SpinButton.new_with_range(1.0, 10.0, 0.1)
        self._mult_spin.set_digits(1)
        self._mult_spin.set_value(float(s.burn_rate.get('multiplier', 1.5)))
        grid.attach(label("Burn-rate multiplier"), 0, 4, 1, 1)
        grid.attach(self._mult_spin, 1, 4, 1, 1)

        box.pack_start(grid, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(14)
        self._notif_saved_label = Gtk.Label()
        self._notif_saved_label.set_no_show_all(True)
        row.pack_end(self._notif_saved_label, False, False, 0)
        save_btn = Gtk.Button(label="Save notifications")
        save_btn.get_style_context().add_class('btn-primary')
        save_btn.connect('clicked', self._on_save_notifications)
        row.pack_end(save_btn, False, False, 0)
        box.pack_start(row, False, False, 0)

        return box

    def _on_save_notifications(self, _btn):
        new_settings = Settings(
            poll_interval_seconds=int(self._poll_spin.get_value()),
            thresholds={
                'warn': int(self._warn_spin.get_value()),
                'critical': int(self._crit_spin.get_value()),
            },
            burn_rate={
                'enabled': bool(self._burn_switch.get_active()),
                'multiplier': float(self._mult_spin.get_value()),
            },
            theme=load_settings().theme,
        )
        self.app.apply_settings(new_settings)
        self._flash_saved(self._notif_saved_label,
                          "✓ Notification settings saved")
        self._status_label.set_text("Notification settings saved.")

    def _on_save_token(self, _btn):
        tok = self._settings_token.get_text().strip()
        # The field is prefilled with a *masked* placeholder (token[:20] + "…").
        # If the user clicks Save without retyping, don't overwrite the real
        # token with the truncated placeholder — just keep what we have.
        masked = (self.app.token[:20] + "...") if self.app.token else None
        if masked and tok == masked:
            self._flash_saved(self._token_saved_label, "✓ Token unchanged")
            return
        if tok and len(tok) > 10 and not tok.endswith("..."):
            self.app.token = tok
            save_token(tok)
            self._settings_token.set_text(tok[:20] + "...")
            self._flash_saved(self._token_saved_label,
                              "✓ Token saved")
            self._status_label.set_text("Token saved. Refreshing cloud usage…")
            self.app.cloud_state = 'loading'
            if self._current_view == 'dashboard':
                self._update_dashboard()
            self.app._refresh_cloud()
        else:
            self._flash_saved(self._token_saved_label,
                              "Token looks too short", ok=False)

    def _on_reprice(self, _btn):
        rc = load_rate_card()
        n = self.app.store.reprice_all(rc)
        self._flash_saved(self._reprice_saved_label,
                          f"✓ Repriced {n:,} messages")
        self._status_label.set_text(f"Repriced {n} messages")
        self.refresh_data()

    # ── Refresh + shortcuts + export ───────────────────────────────────────
    def refresh_view(self, data_changed: bool = True):
        """Re-render the current view from the store WITHOUT re-scanning.

        Driven by the local loop every few seconds so the dashboard's live
        values — 5h block countdown, burn rate, ETA, limits — stay fresh even
        when no new turns were imported, removing the need to hit Refresh. The
        dashboard updates its cards/labels in place (cheap, no flicker); the
        heavier table views only rebuild when ``data_changed`` so scroll
        position and selection survive idle ticks."""
        self._last_update_label.set_text(
            f"Updated {datetime.now().strftime('%H:%M:%S')}")
        if data_changed and self._combo_stale():
            self._populate_account_combo()
        v = self._current_view
        if v == 'dashboard':
            self._update_dashboard()
        elif data_changed:
            if v == 'projects':     self._update_projects_view()
            elif v == 'breakdowns': self._update_breakdowns_view()
            elif v == 'budgets':    self._update_budgets_view()
        return False

    def refresh_data(self):
        self._status_label.set_text("Scanning…")
        rc = load_rate_card()
        counts = scan_into_store(self.app.store, rc,
                                 accounts=self.app._accounts)
        new = sum(counts.values())
        if self._combo_stale():
            self._populate_account_combo()
        self._update_hop_badge()
        self._last_update_label.set_text(
            f"Updated {datetime.now().strftime('%H:%M:%S')}"
            + (f" · {new} new" if new else ""))
        self._status_label.set_text(
            f"{self.app.store.message_count():,} messages indexed")
        if self._current_view == 'dashboard':    self._update_dashboard()
        elif self._current_view == 'projects':   self._update_projects_view()
        elif self._current_view == 'breakdowns': self._update_breakdowns_view()
        elif self._current_view == 'budgets':    self._update_budgets_view()
        return False

    def _install_shortcuts(self):
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)

        def bind(keystr, cb):
            key, mod = Gtk.accelerator_parse(keystr)
            if key:
                accel.connect(key, mod, Gtk.AccelFlags.VISIBLE,
                              lambda *_: (cb(), True)[1])
        bind('<Control>r', lambda: self.refresh_data())
        bind('<Control>1', lambda: self._switch_view('dashboard'))
        bind('<Control>2', lambda: self._switch_view('projects'))
        bind('<Control>3', lambda: self._switch_view('breakdowns'))
        bind('<Control>4', lambda: self._switch_view('budgets'))
        bind('<Control>5', lambda: self._switch_view('settings'))
        bind('<Control>q', lambda: self.app._quit())

    def _export_csv(self, _btn):
        import csv as _csv
        dialog = Gtk.FileChooserDialog(
            title='Export Token Data', parent=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        dialog.add_button(Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(
            f"claude_tokens_{datetime.now():%Y%m%d_%H%M%S}.csv")
        ff = Gtk.FileFilter(); ff.set_name('CSV files'); ff.add_pattern('*.csv')
        dialog.add_filter(ff)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename(); dialog.destroy()
            cutoff = self._cutoff(self._projects_period)
            rows = self.app.store.project_summary(since=cutoff)
            with open(path, 'w', newline='', encoding='utf-8') as f:
                w = _csv.writer(f)
                w.writerow(['Project', 'Last Used', 'Sessions', 'Messages',
                            'Input', 'Cache Create', 'Cache Read', 'Output',
                            'Total', 'Cost USD'])
                for r in rows:
                    w.writerow([
                        r['project'], r['last_used'] or '',
                        r['sessions'] or 0, r['messages'] or 0,
                        r['input_tokens'] or 0, r['cache_creation'] or 0,
                        r['cache_read'] or 0, r['output_tokens'] or 0,
                        r['total_tokens'] or 0, f"{r['cost_usd'] or 0:.4f}",
                    ])
            self._status_label.set_text(f"Exported to {path}")
        else:
            dialog.destroy()


# ─── Tray + App ────────────────────────────────────────────────────────────

def _rate_card_stamp() -> tuple:
    """(size, mtime) of the user's rate-card override, or () if absent."""
    try:
        st = RATE_CARD_FILE.stat()
    except OSError:
        return ()
    return (st.st_size, st.st_mtime)


@dataclass
class AccountState:
    account: Account
    token: Optional[str] = None
    usage_data: Optional[dict] = None
    subscription_info: Optional[dict] = None
    cloud_state: str = 'idle'         # ok / loading / no_token /
                                      # rate_limited / auth_error / network_error
    cloud_error: str = ""
    cloud_retry_at: Optional[datetime] = None
    last_pct5: Optional[int] = None   # last successful 5h utilization
    last_pct7: Optional[int] = None   # last successful 7d utilization
    last_resets_5h: Optional[str] = None
    last_resets_7d: Optional[str] = None
    # The model a Team seat's weekly figure covers (None: all models), and
    # when the last good reading came in.
    last_week_scope: Optional[str] = None
    last_read_at: Optional[datetime] = None
    awaiting_new_window: bool = False  # window rolled over, no fresh data yet
    # Per-model weekly caps (kind=weekly_scoped), last-known; preserved
    # between fetches like last_pctX so a blip doesn't blank the rows.
    last_model_limits: List[dict] = field(default_factory=list)


class App:
    def __init__(self):
        self.store = Store()
        self.window: Optional[TrackerWindow] = None
        self._running = True

        self._accounts: List[Account] = load_accounts()
        self._account_states: Dict[str, AccountState] = {}
        for acc in self._accounts:
            self._account_states[acc.label] = AccountState(
                account=acc,
                token=load_token(acc.credentials_file),
                subscription_info=load_subscription_info(acc.credentials_file),
            )
        self._sync_primary()

        self._api_wake = threading.Event()
        self._window_visible = True
        # "Read all accounts": whether we are waiting on `hop usage`, and its
        # last result as (when, hop.ReadAll).
        self._hop_reading_all = False
        self._hop_read_result: Optional[tuple] = None

        self._notif_mgr = NotificationManager()
        self._settings = load_settings()
        ui.THRESHOLDS.update(self._settings.thresholds)  # gauge colours follow Settings
        self._startup_notified = False
        if HAS_NOTIFY:
            Notify.init(APP_NAME)

        self._indicator = None
        self._tray_items: Dict[str, Gtk.MenuItem] = {}
        self._tray_account_items: Dict[str, Gtk.MenuItem] = {}
        if HAS_INDICATOR:
            self._setup_tray()

        # Initial scan, so the store is current before the window opens.
        rate_card = load_rate_card()
        migrated = self.store.needs_reprice()
        scan_into_store(self.store, rate_card, accounts=self._accounts)
        if migrated:
            # This database was written by an earlier version, whose costs came
            # from a stale rate card and whose tool attribution was
            # incomplete. The scan above re-read and corrected every log still
            # on disk; reprice covers rows whose log has since been deleted.
            n = self.store.reprice_all(rate_card)
            print(f"[{APP_ID}] upgraded store: repriced {n:,} messages",
                  file=sys.stderr)

        threading.Thread(target=self._local_loop, daemon=True).start()
        threading.Thread(target=self._api_loop, daemon=True).start()

    # ── Multi-account helpers ───────────────────────────────────────────────

    def _primary_account(self) -> Account:
        for a in self._accounts:
            if not a.hide_from_tray:
                return a
        return self._accounts[0]

    def _primary_state(self) -> AccountState:
        return self._account_states[self._primary_account().label]

    def account_state(self, label: Optional[str]) -> AccountState:
        """AccountState for ``label``; falls back to the primary account when
        ``label`` is None ('All accounts') or no longer configured."""
        if label and label in self._account_states:
            return self._account_states[label]
        return self._primary_state()

    def _visible_accounts(self) -> List[Account]:
        vis = [a for a in self._accounts if not a.hide_from_tray]
        return vis or self._accounts

    def _polling_accounts(self) -> List[Account]:
        return [a for a in self._accounts if not a.disable_polling]

    # ── claudehop ───────────────────────────────────────────────────────────

    def hop_readings(self, hs) -> dict:
        """The newest reading of every claudehop account; the live login's
        comes from this widget's own polling while that is healthy."""
        primary = self._primary_state()
        live = None
        if (primary.cloud_state not in ('auth_error', 'network_error',
                                        'rate_limited', 'no_token')
                and primary.last_read_at is not None):
            live = claudehop.Reading(
                primary.last_pct5, primary.last_resets_5h, primary.last_pct7,
                primary.last_resets_7d, primary.last_week_scope,
                primary.last_read_at, 'live')
        return claudehop.all_readings([p.name for p in hs.profiles],
                                      self.store.hop_usage(), hs.active, live)

    def read_all_hop(self):
        """Run `hop usage` once in the background (the Read all button)."""
        if self._hop_reading_all:
            return
        self._hop_reading_all = True
        self._after_hop_change()

        def work():
            try:
                result = claudehop.read_all()
            except Exception as e:  # never leave the button stuck on Reading…
                result = claudehop.ReadAll('error', f"claudehop: {e}")
            GLib.idle_add(self._on_hop_read_all, result)
        threading.Thread(target=work, daemon=True).start()

    def _on_hop_read_all(self, result):
        self._hop_reading_all = False
        self._hop_read_result = (datetime.now(timezone.utc), result)
        self._after_hop_change()
        return False

    def _after_hop_change(self):
        self._rebuild_tray()
        if self.window and self.window._current_view == 'dashboard':
            self.window._update_dashboard()

    def _sync_primary(self):
        """Mirror the primary account's view onto ``self.<scalar>`` so the
        existing dashboard tabs (cloud / dashboard / budgets) keep reading
        a single ``self.usage_data`` etc. without each one needing to know
        about multi-account state."""
        st = self._primary_state()
        self.token = st.token
        self.usage_data = st.usage_data
        self.subscription_info = st.subscription_info
        self.cloud_state = st.cloud_state if st.token else 'no_token'
        self.cloud_error = st.cloud_error
        self.cloud_retry_at = st.cloud_retry_at

    def _set_tray_menu(self):
        """(Re)build the tray menu and rewire the middle-click target —
        set_secondary_activate_target must be reapplied after every
        set_menu(), since it points at a specific menu-item instance that
        a rebuild discards."""
        self._indicator.set_menu(self._build_tray_menu())
        try:  # middle-click on the tray icon opens the usage panel
            self._indicator.set_secondary_activate_target(
                self._usage_menu_item)
        except Exception:
            pass

    def _setup_tray(self):
        # Use the brand icon as the indicator base; live state goes into
        # the indicator's label string. AppIndicator's icon must be either
        # an absolute file path or an icon-theme name. We try the theme
        # name first (post-install), then fall back to a bundled file.
        brand = _icon_path(48)
        if brand is not None:
            icon_arg = str(brand)
        else:
            icon_arg = APP_ID  # resolves through hicolor after install
        self._indicator = AppIndicator.Indicator.new(
            APP_ID, icon_arg,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title(APP_NAME)
        self._indicator.set_label("--", "100%")
        self._set_tray_menu()

    def _close_usage_popup(self):
        """Tear down the popup and release its input grab, if any. Safe to
        call multiple times or when no popup is open."""
        win = getattr(self, '_usage_popup', None)
        if win is None:
            return
        self._usage_popup = None
        seat = Gdk.Display.get_default().get_default_seat()
        seat.ungrab()
        win.destroy()

    def _show_usage_popup(self, *_a):
        """The usage panel: the two plan rings, the current session and the
        login with the most room, in a small floating panel (opened from the
        tray menu, a middle-click on the indicator, or the sidebar gauge)."""
        if getattr(self, '_usage_popup', None):
            self._close_usage_popup()
            return
        st = self._primary_state()
        now = datetime.now(timezone.utc)

        # Gtk.WindowType.POPUP (override-redirect) + an explicit pointer
        # grab, rather than a TOPLEVEL window relying on focus-out: WM/
        # compositor focus-out timing is unreliable (Wayland can fire it
        # right at map time), which previously required a grace-period
        # hack that just moved the bug around (missed dismiss clicks,
        # broken re-toggle from the tray menu). A grab we own ourselves
        # dismisses deterministically on any click outside the window.
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_resizable(False)
        win.get_style_context().add_class('usage-popup')
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            win.set_visual(visual)          # rounded corners over the desktop
            win.set_app_paintable(True)
        else:
            win.get_style_context().add_class('opaque')

        def lbl(text='', cls=None, xalign=0.0, ellipsize=False):
            l = Gtk.Label(label=text, xalign=xalign)
            for c in (cls or '').split():
                l.get_style_context().add_class(c)
            if ellipsize:
                l.set_ellipsize(Pango.EllipsizeMode.END)
            return l

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        shell.get_style_context().add_class('popup-shell')
        core = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        core.get_style_context().add_class('popup-core')
        core.set_size_request(340, -1)
        shell.pack_start(core, True, True, 0)
        win.add(shell)

        try:
            hs = claudehop.load_state(st.token)
        except Exception:
            hs = None
        plan, _flat = subscription_summary(st.subscription_info)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        texts.pack_start(lbl(f"Claude usage · {plan}", 'eyebrow'), False, False, 0)
        texts.pack_start(lbl(hs.active if hs and hs.active else "Plan limits",
                             'popup-title', ellipsize=True), False, False, 0)
        head.pack_start(texts, True, True, 0)
        state = st.cloud_state if st.token else 'no_token'
        tag_text, tag_kind = {
            'ok': ('Live', 'ok'), 'rate_limited': ('Rate limited', 'warn'),
            'auth_error': ('Token rejected', 'danger'),
            'network_error': ('Offline', 'danger'),
            'no_token': ('Not connected', 'muted'),
        }.get(state, ('Checking', 'muted'))
        tag = lbl(tag_text, f'tag tag-{tag_kind}')
        tag.set_valign(Gtk.Align.START)
        head.pack_end(tag, False, False, 0)
        core.pack_start(head, False, False, 0)

        bad = state in ('auth_error', 'network_error', 'rate_limited', 'no_token')
        rings = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        rings.set_homogeneous(True)
        for pct, iso, name, weekly in (
                (st.last_pct5, st.last_resets_5h, '5-hour window', False),
                (st.last_pct7, st.last_resets_7d,
                 '7-day' + (f' · {st.last_week_scope}' if st.last_week_scope else ''),
                 True)):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            ring = ui.Ring(diameter=112, caption='used')
            ring.set_halign(Gtk.Align.CENTER)
            ring.set_value(pct, dimmed=bad)
            col.pack_start(ring, False, False, 0)
            name_l = lbl(name, 'ring-label', 0.5)
            name_l.set_margin_top(4)
            col.pack_start(name_l, False, False, 0)
            if pct is None:
                sub = "no reading" if not weekly else "none reported"
            else:
                left = fmt_reset_countdown(iso)
                sub = f"resets in {left}" if iso and left not in ('—', '') else "not started"
            col.pack_start(lbl(sub, 'ring-sub', 0.5), False, False, 0)
            rings.pack_start(col, True, True, 0)
        core.pack_start(rings, False, False, 0)

        # The current session, from the local logs, anchored on the reset time.
        try:
            acc = self._primary_account().label
            rows = self.store.query(since=now - SESSION_LOOKBACK, account=acc)
            block = active_session(rows, st.last_resets_5h)
        except Exception:
            block = None
        sess = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        sess.get_style_context().add_class('notice')
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.pack_start(lbl('Current session', 'eyebrow-strong'), True, True, 0)
        if block is not None:
            top.pack_end(lbl(f"{fmt_duration(block.remaining().total_seconds())} left",
                             'bar-value'), False, False, 0)
            sess.pack_start(top, False, False, 0)
            m = ui.Meter(height=6, color='accent')
            m.set_value(100.0 * block.elapsed().total_seconds()
                        / BLOCK_DURATION.total_seconds())
            sess.pack_start(m, False, False, 0)
            sess.pack_start(lbl(f"{fmt(block.total_tokens)} tokens · "
                                f"{fmt_cost(block.cost_usd)} at API prices · "
                                f"{block.messages:,} messages", 'stat-sub'),
                            False, False, 0)
        else:
            top.pack_end(lbl('idle', 'stat-sub'), False, False, 0)
            sess.pack_start(top, False, False, 0)
        core.pack_start(sess, False, False, 0)

        if hs is not None and len(hs.profiles) > 1:
            try:
                pick = claudehop.most_room(self.hop_readings(hs),
                                           [p.name for p in hs.profiles],
                                           hs.active, now)
            except Exception:
                pick = None
            if pick is not None and pick.name:
                nxt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
                nxt.get_style_context().add_class('callout')
                arrow = ui.NavIcon('arrow', 15)
                arrow.get_style_context().add_class('nav-icon')
                nxt.pack_start(arrow, False, False, 0)
                l = lbl('', ellipsize=True)
                l.set_markup(f"Most room: <b>{html.escape(pick.name)}</b>  "
                             f"<span foreground=\"{COLORS['muted']}\">"
                             f"{html.escape(pick.why.split(';')[0])}</span>")
                nxt.pack_start(l, True, True, 0)
                core.pack_start(nxt, False, False, 0)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        when = st.last_read_at.astimezone().strftime('%H:%M') if st.last_read_at else '—'
        foot.pack_start(lbl(f"Updated {when}" if not bad else state.replace('_', ' '),
                            'stat-sub'), True, True, 0)
        rb = Gtk.Button()
        rb.add(ui.NavIcon('refresh', 15))
        rb.get_style_context().add_class('btn-icon')
        rb.set_tooltip_text("Ask for fresh figures now")

        def _do_refresh(_b):
            self._close_usage_popup()
            self._refresh_cloud()
        rb.connect('clicked', _do_refresh)
        foot.pack_end(rb, False, False, 0)
        ob = Gtk.Button(label='Open dashboard')
        ob.get_style_context().add_class('btn-primary')

        def _do_open(_b):
            self._close_usage_popup()
            self._show_view('dashboard')
        ob.connect('clicked', _do_open)
        foot.pack_end(ob, False, False, 0)
        core.pack_start(foot, False, False, 0)

        def _on_button_press(_w, event):
            # Grabbed pointer delivers every button-press to us; a click
            # outside the popup's own allocation means "dismiss".
            alloc = win.get_allocation()
            if not (0 <= event.x < alloc.width and 0 <= event.y < alloc.height):
                self._close_usage_popup()
            return False

        win.connect('button-press-event', _on_button_press)
        win.connect('key-press-event', lambda w, e:
                    self._close_usage_popup() if e.keyval == 65307 else None)
        win.show_all()
        self._place_popup(win)
        win.present()
        self._usage_popup = win

        seat = Gdk.Display.get_default().get_default_seat()
        seat.grab(win.get_window(), Gdk.SeatCapabilities.ALL, True,
                  None, None, None, None)

    @staticmethod
    def _place_popup(win: Gtk.Window):
        """Open under the pointer (the tray icon, when opened from the tray),
        kept inside the monitor's work area."""
        try:
            display = Gdk.Display.get_default()
            pointer = display.get_default_seat().get_pointer()
            _scr, px, py = pointer.get_position()
            mon = display.get_monitor_at_point(px, py)
            area = mon.get_workarea()
            w, h = win.get_size()
            x = min(max(area.x + 8, px - w // 2), area.x + area.width - w - 8)
            y = py + 14
            if y + h > area.y + area.height - 8:
                y = max(area.y + 8, py - h - 14)
            win.move(x, max(area.y + 8, y))
        except Exception:
            pass

    @staticmethod
    def _emoji_bar(frac: Optional[float], width: int = 8) -> str:
        """Colored emoji progress bar for menu items: colour tracks load."""
        if frac is None:
            return '⬜' * width
        f = max(0.0, min(1.0, frac))
        filled = round(f * width)
        if filled == 0 and f > 0:
            filled = 1
        if f < 0.5:
            seg = '🟩'
        elif f < 0.75:
            seg = '🟨'
        elif f < 0.9:
            seg = '🟧'
        else:
            seg = '🟥'
        return seg * filled + '⬜' * (width - filled)

    def _limit_menu_item(self) -> Gtk.MenuItem:
        it = Gtk.MenuItem(label='--')
        it.set_sensitive(False)
        return it

    def _build_tray_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        self._tray_items.clear()
        self._tray_account_items.clear()

        # ── LIMITS (primary account) ──────────────────────────────────────
        lim_hdr = Gtk.MenuItem(label='Limits')
        lim_hdr.set_sensitive(False); menu.append(lim_hdr)
        self._tray_limits_hdr = lim_hdr
        for k in ('5h', '7d'):
            it = self._limit_menu_item()
            menu.append(it)
            self._tray_items[k] = it
        # Per-model weekly caps (e.g. a separate Opus limit on Max plans).
        # Rows are dynamic: the menu is rebuilt when the model set changes
        # (see _rebuild_tray), so build from the primary account's cache.
        self._tray_model_names = self._tray_models(self._primary_state())
        for name in self._tray_model_names:
            it = self._limit_menu_item()
            menu.append(it)
            self._tray_items[f'model:{name}'] = it

        # ── HOP accounts (from claudehop) ─────────────────────────────────
        self._tray_hop_items = {}
        self._tray_hop_names = ()
        try:
            hs = claudehop.load_state(self._primary_state().token)
            self._tray_hop_names = self._hop_names(hs)
            if hs and (len(hs.profiles) > 1 or hs.providers):
                hop_hdr = Gtk.MenuItem(label='Hop accounts')
                hop_hdr.set_sensitive(False); menu.append(hop_hdr)
                for p in hs.profiles:
                    it = Gtk.MenuItem(label=f"   {p.name}: —")
                    it.set_sensitive(False); menu.append(it)
                    self._tray_hop_items[p.name] = it
                for prov in hs.providers:
                    it = Gtk.MenuItem(label=f"   {prov.capitalize()}: —")
                    it.set_sensitive(False); menu.append(it)
                    self._tray_hop_items[f'provider:{prov}'] = it
                if len(hs.profiles) > 1:
                    it = Gtk.MenuItem(label="   ➜ Next: —")
                    it.set_sensitive(False); menu.append(it)
                    self._tray_hop_items['next'] = it
                    it = Gtk.MenuItem(label="   Read all hop accounts")
                    it.connect('activate', lambda _i: self.read_all_hop())
                    menu.append(it)
                    self._tray_hop_items['read_all'] = it
        except Exception as e:
            print(f"tracker: hop section skipped: {e}", file=sys.stderr)

        # ── ACCOUNTS section (only when more than one configured) ─────────
        if len(self._accounts) > 1:
            acc_hdr = Gtk.MenuItem(label='Accounts')
            acc_hdr.set_sensitive(False); menu.append(acc_hdr)
            for acc in self._accounts:
                it = Gtk.MenuItem(label=f"   {acc.label}: —")
                it.set_sensitive(False); menu.append(it)
                self._tray_account_items[acc.label] = it

        # ── BLOCK section ─────────────────────────────────────────────────
        blk_hdr = Gtk.MenuItem(label='Current 5h block')
        blk_hdr.set_sensitive(False); menu.append(blk_hdr)
        for k, txt in (('block',        '⏳  —'),
                       ('block_tokens', '🪙  —'),
                       ('block_cost',   '💸  —')):
            it = Gtk.MenuItem(label=txt)
            it.set_sensitive(False); menu.append(it)
            self._tray_items[k] = it

        menu.append(Gtk.SeparatorMenuItem())

        # ── Quick actions ─────────────────────────────────────────────────
        usage_item = Gtk.MenuItem(label='Usage panel…')
        usage_item.connect('activate', self._show_usage_popup)
        menu.append(usage_item)
        self._usage_menu_item = usage_item

        open_item = Gtk.MenuItem(label='Open Dashboard')
        open_item.connect('activate',
                          lambda _: self._show_view('dashboard'))
        menu.append(open_item)

        details_item = Gtk.MenuItem(label='Show Details')
        details_sub = Gtk.Menu()
        for vid, label in (('projects',   'Projects'),
                           ('breakdowns', 'Breakdowns'),
                           ('budgets',    'Budgets'),
                           ('settings',   'Settings')):
            sub = Gtk.MenuItem(label=label)
            sub.connect('activate',
                        lambda _, v=vid: self._show_view(v))
            details_sub.append(sub)
        details_item.set_submenu(details_sub)
        menu.append(details_item)

        menu.append(Gtk.SeparatorMenuItem())
        for label, cb in [('Refresh now', self._refresh),
                          ('Settings…',
                           lambda: self._show_view('settings'))]:
            it = Gtk.MenuItem(label=label)
            it.connect('activate', lambda _, c=cb: c())
            menu.append(it)
        menu.append(Gtk.SeparatorMenuItem())
        q = Gtk.MenuItem(label='Quit')
        q.connect('activate', lambda _: self._quit())
        menu.append(q)
        menu.show_all()
        return menu

    def _rebuild_tray(self):
        """Refresh the tray label and per-account menu lines from
        ``self._account_states``. Tolerant of intermediate failure states:
        ``!`` for auth/network errors, ``?`` while we're waiting on fresh
        data after a window rollover, and the last-known % preserved
        between resets so a missed fetch doesn't blank the indicator."""
        if not self._indicator:
            return
        # The per-model rows are dynamic (models come and go per plan). When
        # the set changes, rebuild the menu so rows are added/removed; label
        # updates below then fill them in. Rare event — not every fetch.
        model_names = self._tray_models(self._primary_state())
        if model_names != getattr(self, '_tray_model_names', []):
            self._set_tray_menu()
        # ── Tray label: aggregated multi-account 5h % ─────────────────────
        pieces: List[str] = []
        for acc in self._visible_accounts():
            st = self._account_states[acc.label]
            tag = self._tray_tag(st)
            pieces.append(f"{acc.label}:{tag}")
        if not pieces:
            self._indicator.set_label("--", "100%")
        else:
            # A single account → keep the older bare-% style.
            label = (pieces[0].split(':', 1)[1]
                     if len(pieces) == 1 else ' '.join(pieces))
            self._indicator.set_label(label, "100%")

        # ── Tray icon: a ring for the primary account's 5-hour window ─────
        primary = self._primary_state()
        self._update_tray_icon(primary)

        # ── LIMITS section (primary account drives this) ──────────────────
        self._update_tray_limits(primary)
        try:
            self._update_tray_hop(primary)
        except Exception as e:
            print(f"tracker: hop tray update failed: {e}", file=sys.stderr)

        # ── ACCOUNTS section ──────────────────────────────────────────────
        for acc in self._accounts:
            it = self._tray_account_items.get(acc.label)
            if it is None:
                continue
            st = self._account_states[acc.label]
            it.set_label(self._tray_account_line(st))

    def _update_tray_icon(self, st: AccountState):
        """Draw the indicator icon as a ring filled to the 5-hour use, in the
        state colour; a broken ring when the connection has failed."""
        if st.cloud_state in ('auth_error', 'network_error', 'rate_limited',
                              'no_token'):
            state, pct = 'error', None
        elif st.awaiting_new_window or st.last_pct5 is None:
            state, pct = 'none', None
        else:
            pct = float(st.last_pct5)
            state = ui.level(pct)
        path = ui.tray_icon_png(CACHE_DIR, pct, state)
        if path is None or str(path) == getattr(self, '_tray_icon_path', None):
            return
        self._tray_icon_path = str(path)
        desc = (f"5-hour window {int(pct)}% used" if pct is not None
                else "No current reading")
        try:
            self._indicator.set_icon_full(str(path), desc)
        except Exception as e:
            print(f"[{APP_ID}] tray icon: {e}", file=sys.stderr)

    @staticmethod
    def _tray_tag(st: AccountState) -> str:
        """The tray-label fragment for one account: ``67%`` / ``?`` / ``!``.

        ``!`` for any non-loading failure state.
        ``?`` for the brief gap between a window rollover and the API
        returning fresh data.
        Otherwise the most recently known 5h percentage — preserved between
        resets so a transient blip doesn't blank the label."""
        if st.cloud_state in ('auth_error', 'network_error', 'rate_limited',
                              'no_token'):
            return '!'
        if st.awaiting_new_window or st.last_pct5 is None:
            return '?'
        return f"{st.last_pct5}%"

    def _update_tray_limits(self, st: AccountState):
        """5h/7d lines render either from current usage_data or the
        last-known cached values (preserving info between fetches)."""
        def _set(key: str, text: str):
            it = self._tray_items.get(key)
            if it is not None:
                it.set_label(text)

        wk = f"7d {st.last_week_scope}" if st.last_week_scope else "7d"
        models = [m for m in st.last_model_limits
                  if m['model'] != st.last_week_scope]
        if st.cloud_state in ('auth_error', 'network_error', 'rate_limited',
                              'no_token'):
            hint = st.cloud_state.replace('_', ' ')
            _set('5h', f"5h  {self._emoji_bar(None)}  {hint}")
            _set('7d', f"{wk}  {self._emoji_bar(None)}  {hint}")
            for m in models:
                _set(f"model:{m['model']}",
                     f"   {m['model']}  {self._emoji_bar(None)}  {hint}")
            return
        u5 = (st.last_pct5 or 0) / 100.0
        u7 = (st.last_pct7 or 0) / 100.0
        r5 = self._fmt_reset(st.last_resets_5h, st.account, window='5h')
        r7 = self._fmt_reset(st.last_resets_7d, st.account, window='7d')
        p5 = '?' if st.last_pct5 is None else f"{st.last_pct5}%"
        p7 = '?' if st.last_pct7 is None else f"{st.last_pct7}%"
        b5 = self._emoji_bar(u5 if st.last_pct5 is not None else None)
        b7 = self._emoji_bar(u7 if st.last_pct7 is not None else None)
        _set('5h', f"5h  {b5}  {p5} · ↻ {r5}")
        _set('7d', f"{wk}  {b7}  {p7} · ↻ {r7}")
        # Per-model weekly caps. ● flags the one currently binding your usage.
        for m in models:
            bar = self._emoji_bar(m['fraction'])
            rr = self._fmt_reset(m['resets_at'], st.account, window='7d')
            marker = ' ●' if m['is_active'] else ''
            _set(f"model:{m['model']}",
                 f"   {m['model']}  {bar}  {m['pct']}%{marker} · ↻ {rr}")

    @staticmethod
    def _tray_models(st: 'AccountState') -> List[str]:
        """Per-model rows for the tray, less the one shown as the weekly line."""
        return [m['model'] for m in st.last_model_limits
                if m['model'] != st.last_week_scope]

    @staticmethod
    def _hop_names(hs) -> tuple:
        if not hs:
            return ()
        return (tuple(p.name for p in hs.profiles)
                + tuple(f'provider:{n}' for n in hs.providers))

    def _update_tray_hop(self, primary: 'AccountState'):
        """Name the account in the tray and keep the hop lines current."""
        try:
            hs = claudehop.load_state(primary.token)
        except Exception:
            return
        if self._hop_names(hs) != getattr(self, '_tray_hop_names', ()):
            self._set_tray_menu()          # accounts added or removed: rebuild
        hdr = getattr(self, '_tray_limits_hdr', None)
        if hdr is not None:
            if hs and hs.active:
                hdr.set_label(f"Limits · {hs.active}"
                              + (" (login idle)" if hs.provider else ""))
            else:
                hdr.set_label('Limits')
        if hs is None:
            return
        now = datetime.now(timezone.utc)
        readings = self.hop_readings(hs)
        runs = claudehop.run_terminals()
        items = getattr(self, '_tray_hop_items', {})
        for p in hs.profiles:
            it = items.get(p.name)
            if it is None:
                continue
            usage, tag = claudehop.usage_text(readings.get(p.name), now)
            on = p.name == hs.active and not hs.provider
            short = usage.replace('5-hour', '5h').replace('7-day', '7d')
            n_run = runs.get(p.name, 0)
            it.set_label(f"   {'●' if on else '○'} {p.name}: {short}"
                         + (f" · {tag}" if tag else "")
                         + (f" · {n_run} hop run" if n_run else ""))
        for prov in hs.providers:
            it = items.get(f'provider:{prov}')
            if it is not None:
                on = prov == hs.provider
                it.set_label(f"   {'●' if on else '○'} {prov.capitalize()}: "
                             f"{'on' if on else 'off'}")
        it = items.get('next')
        if it is not None:
            pick = claudehop.most_room(readings, [p.name for p in hs.profiles],
                                       hs.active, now)
            if pick is None:
                it.set_label("   ➜ Next: —")
            elif pick.name:
                it.set_label(f"   ➜ Next: {pick.name} · "
                             + pick.why.split(';')[0].replace('5-hour', '5h'))
            else:
                it.set_label(f"   ➜ {pick.why.split(';')[0].capitalize()}")
        it = items.get('read_all')
        if it is not None:
            it.set_label("   Reading hop accounts…" if self._hop_reading_all
                         else "   Read all hop accounts")
            it.set_sensitive(not self._hop_reading_all)

    def _tray_account_line(self, st: AccountState) -> str:
        if st.cloud_state in ('auth_error', 'network_error', 'rate_limited',
                              'no_token'):
            return f"   {st.account.label}: !"
        if st.last_pct7 is None:
            return f"   {st.account.label}: ?"
        r5 = self._fmt_reset(st.last_resets_5h, st.account, window='5h')
        return (f"   {st.account.label}: {st.last_pct7}% 7d "
                f"· ↺ {r5}")

    @staticmethod
    def _fmt_reset(iso: Optional[str], account: Account,
                   window: str) -> str:
        """Countdown by default, absolute when polling is disabled (so a
        long-disabled account still shows a useful timestamp)."""
        if account.disable_polling:
            return fmt_reset_absolute(iso, with_weekday=(window == '7d'))
        return fmt_reset_countdown(iso)

    def _show_view(self, view_id: str):
        self._show_window()
        if self.window is not None:
            self.window._switch_view(view_id)

    def _update_tray_block(self):
        """Refresh the tray's Block / Tokens / Cost lines from local store.

        Independent of cloud state so the tray stays informative even when
        the cloud token is missing or rate-limited."""
        if not self._indicator:
            return False
        try:
            # Filtered to the account whose reset time anchors the window —
            # otherwise a second account's messages were counted inside the
            # primary account's session.
            acc = self._primary_account().label
            rows = self.store.query(
                since=datetime.now(timezone.utc) - SESSION_LOOKBACK,
                account=acc)
            block = active_session(rows,
                                   self._account_states[acc].last_resets_5h)
            if block is not None:
                elapsed_frac = (block.elapsed().total_seconds()
                                / BLOCK_DURATION.total_seconds())
                mark = '' if block.anchored else ' ~'
                self._tray_items['block'].set_label(
                    f"⏳  {self._emoji_bar(elapsed_frac)}  "
                    f"{fmt_duration(block.remaining().total_seconds())}"
                    f" left{mark}")
                self._tray_items['block_tokens'].set_label(
                    f"🪙  {fmt(block.total_tokens)} tokens")
                self._tray_items['block_cost'].set_label(
                    f"💸  {fmt_cost(block.cost_usd)} spent")
            else:
                self._tray_items['block'].set_label(
                    f"⏳  {self._emoji_bar(None)}  idle")
                self._tray_items['block_tokens'].set_label('🪙  —')
                self._tray_items['block_cost'].set_label('💸  —')
        except Exception as e:
            print(f"[{APP_ID}] tray block update: {e}", file=sys.stderr)
        return False

    def _reprice_after_card_change(self, rc):
        """Rewrite stored costs from an edited rate card, then redraw."""
        try:
            n = self.store.reprice_all(rc)
            print(f"[{APP_ID}] rate card changed: repriced {n:,} messages",
                  file=sys.stderr)
        except Exception as e:
            print(f"[{APP_ID}] reprice: {e}", file=sys.stderr)
        if self.window and self._window_visible:
            self.window.refresh_view(True)
        return False

    def _local_loop(self):
        rc = load_rate_card()
        rc_stamp = _rate_card_stamp()
        while self._running:
            try:
                # Pick up an edited rate_card.json without a restart. The card
                # was read once at thread start, so a corrected price sat
                # unused until the app was relaunched.
                stamp = _rate_card_stamp()
                if stamp != rc_stamp:
                    rc_stamp = stamp
                    rc = load_rate_card()
                    self.store.forget_scans()
                    GLib.idle_add(self._reprice_after_card_change, rc)
                counts = scan_into_store(self.store, rc,
                                         accounts=self._accounts)
                imported = sum(counts.values())
                # Re-render every tick (not only when new turns landed) so the
                # dashboard's live values — 5h block countdown, burn rate, ETA,
                # limits — stay current without a manual Refresh. Only while the
                # window is visible; the tray updates separately below.
                # `imported > 0` tells the view whether the heavier table views
                # need a rebuild (they keep scroll/selection otherwise).
                if self.window and self._window_visible:
                    GLib.idle_add(self.window.refresh_view, imported > 0)
                GLib.idle_add(self._update_tray_block)
                # Re-check budgets on the main loop — _check_budgets fires
                # libnotify, which (like all GLib/D-Bus state) must not be
                # touched from this worker thread.
                GLib.idle_add(self._check_budgets)
            except Exception as e:
                print(f"[{APP_ID}] local loop: {e}", file=sys.stderr)
            time.sleep(LOCAL_SCAN_INTERVAL)

    def _api_loop(self):
        while self._running:
            # Consume any pending wake *before* polling, so a wake request
            # raised while we poll/sleep below is preserved and triggers the
            # next pass instead of being cleared away (lost-wakeup race).
            self._api_wake.clear()
            poll = self._polling_accounts()
            primary_label = self._primary_account().label

            if not poll:
                # Everything has polling disabled — still touch the primary
                # so the tray reflects token state.
                state = self._primary_state()
                if not state.token:
                    GLib.idle_add(self._set_cloud, 'no_token', "", None,
                                  primary_label)

            now = datetime.now(timezone.utc)
            # Soonest per-account rate-limit retry, tracked synchronously here
            # (state.cloud_retry_at is set asynchronously via idle_add, so it
            # can't be relied on within this same iteration).
            retries: List[datetime] = []
            for acc in poll:
                # reload_accounts() can atomically swap self._account_states on
                # the main thread while we iterate this snapshot of `poll`; a
                # just-removed account won't be in the new dict. Use .get()+skip
                # so a mid-poll removal can't KeyError out of this worker thread
                # and leave cloud polling dead until restart.
                state = self._account_states.get(acc.label)
                if state is None:
                    continue
                # Honor this account's own rate-limit backoff without holding
                # up healthy accounts (a 429 on one token used to delay the
                # whole loop, and thus every other account, by ≥120s).
                retry_at = state.cloud_retry_at
                if (state.cloud_state == 'rate_limited' and retry_at
                        and retry_at > now):
                    retries.append(retry_at)
                    continue
                fresh_token = load_token(acc.credentials_file)
                if fresh_token:
                    state.token = fresh_token
                    fs = load_subscription_info(acc.credentials_file)
                    if fs:
                        state.subscription_info = fs
                if not state.token:
                    GLib.idle_add(self._set_cloud, 'no_token', "", None,
                                  acc.label)
                    continue
                GLib.idle_add(self._set_cloud, 'loading', "", None, acc.label)
                # Who claudehop says is live, read before and after the fetch so a
                # hop in the middle files the reading under neither account.
                who = (claudehop.active_name(state.token)
                       if acc.label == primary_label else None)
                try:
                    data = fetch_cloud_usage(state.token)
                    if who and claudehop.active_name(
                            load_token(acc.credentials_file)) != who:
                        who = None
                    GLib.idle_add(self._on_cloud_data, data, acc.label, who)
                except RateLimitError as e:
                    wait = max(e.retry_after or 0, 120)
                    retry_at = datetime.now(timezone.utc) + timedelta(
                        seconds=wait)
                    retries.append(retry_at)
                    GLib.idle_add(self._set_cloud, 'rate_limited',
                                  "Claude.ai is rate-limiting this token.",
                                  retry_at, acc.label)
                except AuthError as e:
                    GLib.idle_add(self._set_cloud, 'auth_error',
                                  f"OAuth rejected (HTTP {e.code}). "
                                  "Re-run `claude` to refresh.", None,
                                  acc.label)
                except CloudApiError as e:
                    GLib.idle_add(self._set_cloud, 'network_error',
                                  str(e), None, acc.label)
                except Exception as e:
                    GLib.idle_add(self._set_cloud, 'network_error',
                                  str(e), None, acc.label)

            # The tray gauge is always visible, so poll at the configured
            # interval whether or not the dashboard window is open. (This used
            # to balloon to 10 min when the window was merely hidden, leaving
            # the widget stale and forcing a manual Refresh; the original
            # widget polled steadily regardless.)
            interval = self._settings.poll_interval_seconds
            sleep_for = interval
            # Wake up no later than the soonest per-account retry, so a
            # rate-limited account is retried promptly without forcing the
            # whole loop to back off.
            if retries:
                soonest = int((min(retries)
                               - datetime.now(timezone.utc)).total_seconds())
                sleep_for = max(1, min(sleep_for, soonest))
            self._api_wake.wait(timeout=sleep_for)

    def _on_cloud_data(self, data: Optional[dict],
                       account_label: str = DEFAULT_ACCOUNT_LABEL,
                       hop_name: Optional[str] = None):
        st = self._account_states.get(account_label)
        if st is None:
            return False
        st.usage_data = data
        st.cloud_state = 'ok' if data else st.cloud_state
        st.cloud_error = ""
        st.cloud_retry_at = None
        st.awaiting_new_window = False
        if data:
            five = data.get('five_hour') or {}
            # Team seats have no seven_day window; their weekly figure is the
            # fullest weekly meter, named after the model it covers.
            seven = weekly_window(data)
            u5 = normalize_utilization(five.get('utilization', 0))
            u7 = normalize_utilization((seven or {}).get('utilization', 0))
            pct5 = int(u5 * 100)
            pct7 = int(u7 * 100) if seven else None
            r5 = five.get('resets_at')
            r7 = (seven or {}).get('resets_at')

            # If the resets_at shifted while we had no fresh data, the
            # window has rolled over — but the API has now returned new
            # numbers so clear any "?" awaiting flag.
            st.last_pct5, st.last_pct7 = pct5, pct7
            st.last_resets_5h, st.last_resets_7d = r5, r7
            st.last_week_scope = (seven or {}).get('scope')
            st.last_read_at = datetime.now(timezone.utc)
            st.last_model_limits = extract_model_limits(data)
            if hop_name and account_label == self._primary_account().label:
                try:
                    self.store.save_hop_usage(hop_name, pct5, pct7, r5, r7,
                                              st.last_week_scope)
                except Exception as e:
                    print(f"tracker: could not save hop usage: {e}", file=sys.stderr)

            if account_label == self._primary_account().label:
                if HAS_NOTIFY:
                    self._notify_thresholds(pct5, pct7 or 0, max(u5, u7),
                                            usage_data=data,
                                            account=account_label)
                    self._check_budgets()
            elif HAS_NOTIFY:
                # Threshold notifications fire for every account, but only
                # the primary triggers budget evaluation (budgets are
                # global today; per-account scope lands as a follow-up).
                self._notify_thresholds(pct5, pct7 or 0, max(u5, u7),
                                        usage_data=data,
                                        account=account_label)
        self._sync_primary()
        self._rebuild_tray()
        self._update_tray_block()
        if self.window:
            self.window._update_hop_badge()
            v = self.window._current_view
            if v == 'dashboard':
                self.window._update_dashboard()
            elif v == 'budgets':
                self.window._update_budgets_view()
        return False

    def _set_cloud(self, state: str, msg: str,
                   retry_at: Optional[datetime],
                   account_label: str = DEFAULT_ACCOUNT_LABEL):
        st = self._account_states.get(account_label)
        if st is None:
            return False
        st.cloud_state = state
        st.cloud_error = msg
        st.cloud_retry_at = retry_at
        if state not in ('ok', 'loading'):
            # Auth / rate / network failure: drop the cached payload so
            # the dashboard's cloud view stops showing stale percentages
            # (the tray still preserves last-known via st.last_pctX).
            st.usage_data = None
            # If the window we last had numbers for has since reset, that
            # last-known percentage belongs to a closed window. Flag it so
            # the tray shows "?" rather than a figure that reads as current.
            reset = parse_resets_at(st.last_resets_5h)
            if reset is not None and reset <= datetime.now(timezone.utc):
                st.awaiting_new_window = True
        self._sync_primary()
        self._rebuild_tray()
        if self.window and self.window._current_view == 'dashboard':
            self.window._update_dashboard()
        return False

    def _notify_thresholds(self, pct5: int, pct7: int, dominant: float,
                           usage_data: Optional[dict] = None,
                           account: str = DEFAULT_ACCOUNT_LABEL):
        if not self._startup_notified:
            self._startup_notified = True
            n = Notify.Notification.new(
                f"{APP_NAME} started",
                f"Cloud: 5h={pct5}%, 7d={pct7}%",
                "dialog-information")
            n.show()
            # Fall through and still evaluate thresholds: the persisted
            # NotificationManager state already de-dupes alerts that fired
            # before this launch, so this account doesn't silently lose its
            # first-tick evaluation just because it showed the banner.
        snap = WindowSnapshot.from_cloud_data(usage_data or {
            'five_hour': {'utilization': pct5},
            'seven_day': {'utilization': pct7},
        })
        notes = self._notif_mgr.evaluate(
            account, snap,
            thresholds=self._settings.thresholds,
            burn_rate=self._settings.burn_rate,
        )
        for note in notes:
            icon = 'dialog-information'
            if note.urgency == 'critical':
                icon = 'dialog-error'
            elif note.level in ('warn', 'early'):
                icon = 'dialog-warning'
            n = Notify.Notification.new(note.title, note.body, icon)
            if note.urgency == 'critical':
                n.set_urgency(Notify.Urgency.CRITICAL)
            else:
                n.set_urgency(Notify.Urgency.NORMAL)
            n.show()

    def _check_budgets(self):
        if not HAS_NOTIFY:
            return
        for s in evaluate_budgets(self.store, usage_data=self.usage_data):
            if s.should_notify():
                pct = int(s.pct * 100)
                if s.is_pct_based:
                    limit = f"{s.limit_pct:.0f}% of plan"
                    spent = f"{s.spent_pct:.0f}% used"
                else:
                    limit = (fmt_cost(s.limit_usd) if s.limit_usd
                             else f"{s.limit_tokens:,} tok")
                    spent = (fmt_cost(s.spent_usd) if s.limit_usd
                             else f"{s.spent_tokens:,} tok")
                n = Notify.Notification.new(
                    f"Budget '{s.name}' at {pct}%",
                    f"{spent} of {limit} ({s.scope}, {s.period})",
                    "dialog-warning" if pct < 100 else "dialog-error")
                if pct >= 100:
                    n.set_urgency(Notify.Urgency.CRITICAL)
                n.show()
                self.store.update_budget_notification(
                    s.id, pct, s.period_key)

    # ── Window management ──────────────────────────────────────────────────
    def _on_set_token(self):
        dialog = Gtk.Dialog(title="Set OAuth Token", transient_for=self.window)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_default_size(400, 120)
        ca = dialog.get_content_area()
        ca.set_margin_top(12); ca.set_margin_bottom(12)
        ca.set_margin_start(12); ca.set_margin_end(12)
        l = Gtk.Label(label="Enter your Claude OAuth token:")
        l.set_halign(Gtk.Align.START)
        ca.pack_start(l, False, False, 0)
        e = Gtk.Entry()
        e.set_placeholder_text("sk-ant-oat01-...")
        e.set_visibility(False)
        ca.pack_start(e, False, False, 8)
        h = Gtk.Label(label="Find it in ~/.claude/.credentials.json")
        h.get_style_context().add_class('subtitle')
        h.set_halign(Gtk.Align.START)
        ca.pack_start(h, False, False, 0)
        w = Gtk.Label(label=(
            "⚠ This is your Claude subscription token. Anthropic's terms "
            "restrict it to Claude Code and claude.ai — using it in a "
            "third-party tool may violate those terms and risk your account. "
            "Use at your own risk."))
        w.get_style_context().add_class('subtitle')
        w.set_halign(Gtk.Align.START)
        w.set_line_wrap(True)
        w.set_xalign(0)
        w.set_max_width_chars(48)
        ca.pack_start(w, False, False, 8)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            tok = e.get_text().strip()
            if tok:
                primary = self._primary_state()
                primary.token = tok
                primary.cloud_state = 'loading'
                save_token(tok)
                self._sync_primary()
                if self.window:
                    self.window._status_label.set_text(
                        "Token saved. Refreshing…")
                self._refresh_cloud()
        dialog.destroy()

    def _refresh_cloud(self):
        self._api_wake.set()

    def apply_settings(self, settings: Settings):
        """Apply new notification/poll settings live and persist them.

        Called by the dashboard's Settings tab when the user clicks Save."""
        save_settings(settings)
        self._settings = settings
        ui.THRESHOLDS.update(settings.thresholds)
        self._refresh_cloud()

    def reload_accounts(self, accounts: List[Account]):
        """Rebuild per-account state and tray menu after a config change.

        Existing accounts retain their last-known % and tokens so the tray
        doesn't flash ``?`` while we wait for the next fetch. New accounts
        start blank; removed accounts have their notification state reset
        so they don't leave stale records."""
        old_states = self._account_states
        new_states: Dict[str, AccountState] = {}
        for acc in accounts:
            old = old_states.get(acc.label)
            if old is not None:
                old.account = acc
                new_states[acc.label] = old
            else:
                new_states[acc.label] = AccountState(
                    account=acc,
                    token=load_token(acc.credentials_file),
                    subscription_info=load_subscription_info(
                        acc.credentials_file),
                )
        for label in set(old_states) - {a.label for a in accounts}:
            self._notif_mgr.reset_account(label)
        self._accounts = accounts
        self._account_states = new_states
        self._sync_primary()
        # The accounts section is structural — rebuild the whole tray menu.
        if self._indicator:
            self._set_tray_menu()
        self._rebuild_tray()
        # Repopulate the dashboard's header account combo if the window is up.
        if self.window is not None:
            self.window._populate_account_combo()

    def _show_window(self):
        if self.window is None:
            self.window = TrackerWindow(self)
            self.window.connect('delete-event', self._on_window_delete)
            self.window.connect('show', lambda *_: self._set_visible(True))
            self.window.connect('hide', lambda *_: self._set_visible(False))
        self.window.present()
        self._set_visible(True)
        self.window.refresh_data()

    def _set_visible(self, v: bool):
        if v != self._window_visible:
            self._window_visible = v
            if v:
                self._api_wake.set()  # re-poll promptly when shown

    def _on_window_delete(self, *_):
        if HAS_INDICATOR:
            self.window.hide()
            self._set_visible(False)
            return True
        self._quit()
        return False

    def _refresh(self):
        if self.window:
            self.window.refresh_data()
        if self.token:
            self._refresh_cloud()

    def _quit(self):
        self._running = False
        self._api_wake.set()
        if HAS_NOTIFY:
            Notify.uninit()
        Gtk.main_quit()

    def run(self):
        # SIGUSR1 = "second instance asked us to raise the window"
        try:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                                 self._on_raise_signal)
        except (AttributeError, ValueError):
            pass  # platform without unix signals — fine
        self._show_window()
        Gtk.main()

    def _on_raise_signal(self):
        if self.window:
            self.window.deiconify()
            self.window.present()
            self._set_visible(True)
        else:
            self._show_window()
        return GLib.SOURCE_CONTINUE


def run():
    try:
        lock = acquire()
    except AlreadyRunning as e:
        signal_running_instance(e.pid)
        print(f"Claude Token Tracker is already running (pid={e.pid}). "
              "Bringing existing window forward.", file=sys.stderr)
        sys.exit(0)
    try:
        App().run()
    finally:
        lock.close()
