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

from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Pango  # noqa: E402

from .blocks import BLOCK_HOURS, compute_blocks, forecast_active
from .budgets import evaluate_budgets, period_window
from .cli import scan_into_store
from .cloud import (AuthError, CloudApiError, RateLimitError,
                    extract_model_limits, fetch_cloud_usage, format_reset_time,
                    load_subscription_info, load_token, normalize_utilization,
                    save_token, subscription_summary)
from .config import (APP_ID, APP_NAME, APP_VERSION, LOCAL_SCAN_INTERVAL,
                     NOTIFICATION_THRESHOLDS, PERIOD_LABELS,
                     DEFAULT_ACCOUNT_LABEL, Account,
                     load_accounts, load_settings, save_accounts,
                     save_settings, Settings)
from .notifications import NotificationManager, WindowSnapshot
from .format import (fmt, fmt_cost, fmt_duration, fmt_full,
                     fmt_reset_absolute, fmt_reset_countdown,
                     period_range_text, rel_time)
from .pricing import load_rate_card
from .single_instance import AlreadyRunning, acquire, signal_running_instance
from .store import Store


# ─── Theme ─────────────────────────────────────────────────────────────────
# Light theme keyed off the brand logo: warm orange + ink-black on off-white.
COLORS_LIGHT = {
    'bg':      '#FAFAF7',  # paper / app background
    'surface': '#FFFFFF',  # cards, sidebar, header
    'overlay': '#ECECE8',  # hover, dividers, subtle fills
    'text':    '#15151A',  # ink — primary text
    'subtext': '#4A4A52',  # secondary text
    'muted':   '#8A8A93',  # tertiary / labels
    'accent':  '#F5821F',  # brand orange
    'blue':    '#2563EB',
    'green':   '#16A34A',
    'yellow':  '#CA8A04',
    'red':     '#DC2626',
    'orange':  '#EA580C',
}
COLORS_DARK = {
    'bg':      '#1E1E22',  # dark app background
    'surface': '#26262B',  # cards, sidebar, header
    'overlay': '#333339',  # hover, dividers, subtle fills
    'text':    '#EDEDEF',  # primary text
    'subtext': '#B8B8C0',  # secondary text
    'muted':   '#84848E',  # tertiary / labels
    'accent':  '#F5821F',  # brand orange
    'blue':    '#60A5FA',
    'green':   '#4ADE80',
    'yellow':  '#FACC15',
    'red':     '#F87171',
    'orange':  '#FB923C',
}
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
# Mutable palette dict: updated in place on theme switch so inline
# color references pick up the new values on the next redraw.
COLORS = dict(_resolve_palette(_load_settings_for_theme().theme))
USAGE_COLORS = {
    'low':      '#16A34A',
    'medium':   '#CA8A04',
    'high':     '#EA580C',
    'critical': '#DC2626',
    'unknown':  '#8A8A93',
}


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
    if pct < 0.5:
        return USAGE_COLORS['low']
    if pct < 0.75:
        return USAGE_COLORS['medium']
    if pct < 0.9:
        return USAGE_COLORS['high']
    return USAGE_COLORS['critical']


def _make_css() -> str:
    return f"""
window {{
    background-color: {COLORS['bg']}; color: {COLORS['text']};
    font-family: "Inter", "SF Pro Text", "Segoe UI", system-ui, sans-serif;
}}
.sidebar {{
    background-color: {COLORS['surface']};
    border-right: 1px solid {COLORS['overlay']};
    padding: 18px 10px;
}}
.sidebar-section {{
    font-size: 10px; font-weight: 700; letter-spacing: 0.10em;
    color: {COLORS['muted']}; padding: 14px 14px 4px;
}}
.sidebar-btn {{
    background: transparent; border: none; border-radius: 8px;
    color: {COLORS['subtext']}; padding: 10px 14px;
    font-size: 13px; font-weight: 500; box-shadow: none;
}}
.sidebar-btn:hover {{
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}
.sidebar-btn.active {{
    background-color: rgba(245, 130, 31, 0.10);
    color: {COLORS['accent']};
    font-weight: 600;
}}
.content-area {{ background-color: {COLORS['bg']}; }}
.header-bar {{
    background-color: {COLORS['surface']};
    border-bottom: 1px solid {COLORS['overlay']};
    padding: 14px 28px;
}}
.page-title {{
    font-size: 22px; font-weight: 700; color: {COLORS['text']};
}}
.subtitle {{ font-size: 12px; color: {COLORS['muted']}; }}

.card {{
    background-color: {COLORS['surface']};
    border-radius: 14px;
    border: 1px solid {COLORS['overlay']};
    padding: 22px;
    box-shadow: 0 1px 2px rgba(15, 15, 25, 0.04);
}}
.card-title {{
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    color: {COLORS['muted']}; margin-bottom: 10px;
}}
.card-value {{
    font-size: 30px; font-weight: 700; color: {COLORS['text']};
}}
.card-subtitle {{
    font-size: 12px; color: {COLORS['subtext']}; margin-top: 6px;
}}
.accent-dash {{ border-radius: 2px; }}

.search-entry {{
    background-color: {COLORS['surface']};
    border: 1px solid #D8D8D2;
    border-radius: 10px; color: {COLORS['text']};
    padding: 9px 14px; font-size: 13px;
}}
.search-entry:focus {{
    border-color: {COLORS['accent']};
    box-shadow: 0 0 0 3px rgba(245, 130, 31, 0.18);
}}

/* Form controls — force readable colors against the light surface so they
 * don't pick up the user's system dark theme defaults inside our cards. */
spinbutton, spinbutton entry,
.card entry {{
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid #D8D8D2;
    border-radius: 8px;
    padding: 6px 8px;
}}
spinbutton button {{
    background-color: {COLORS['surface']};
    color: {COLORS['subtext']};
    border: 1px solid #D8D8D2;
}}
spinbutton button:hover {{ background-color: {COLORS['overlay']}; }}
.card label {{ color: {COLORS['text']}; }}
.card .subtitle {{ color: {COLORS['muted']}; }}
checkbutton, checkbutton label {{ color: {COLORS['text']}; }}
checkbutton check {{
    background-color: {COLORS['surface']};
    border: 1px solid #C8C8C2;
}}
checkbutton check:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
    color: {COLORS['surface']};
}}
/* Cell-rendered toggles inside a TreeView (CellRendererToggle) — these
 * are drawn by the cell renderer, not real GtkCheckButton widgets, so
 * they need their own selectors. Without these the cells inherit the
 * user's system dark theme and show as black squares against our light
 * surface. */
treeview.view check, treeview check {{
    background-color: {COLORS['surface']};
    border: 1px solid #C8C8C2;
    border-radius: 3px;
    min-width: 14px; min-height: 14px;
    color: {COLORS['text']};
}}
treeview.view check:checked, treeview check:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
    color: {COLORS['surface']};
}}
treeview.view check:hover, treeview check:hover {{
    border-color: {COLORS['accent']};
}}
/* TreeStore disclosure (▶/▼) arrows for expandable rows — without these
 * the system theme draws them invisible against our light surface. */
treeview.view expander, treeview expander {{
    color: {COLORS['muted']};
    -gtk-icon-source: -gtk-icontheme("pan-end-symbolic");
}}
treeview.view expander:hover, treeview expander:hover {{
    color: {COLORS['accent']};
}}
treeview.view expander:checked, treeview expander:checked {{
    -gtk-icon-source: -gtk-icontheme("pan-down-symbolic");
    color: {COLORS['accent']};
}}
/* Editable cell entries (CellRendererText with editable=True) — the
 * popup edit field needs explicit colors to stay readable. */
treeview entry, .view entry {{
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['accent']};
}}
/* StackSwitcher buttons (Breakdowns Models/Tools toggle) — match our
 * sidebar-button feel rather than the system default. */
stackswitcher button {{
    background: transparent;
    color: {COLORS['subtext']};
    border: none;
    border-radius: 8px;
    padding: 6px 14px;
    font-weight: 500;
}}
stackswitcher button:hover {{
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}
stackswitcher button:checked {{
    background-color: rgba(245, 130, 31, 0.10);
    color: {COLORS['accent']};
    font-weight: 600;
}}
switch {{
    background-color: {COLORS['overlay']};
    border-radius: 14px;
    border: 1px solid #D8D8D2;
}}
switch:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
switch slider {{
    background-color: {COLORS['surface']};
    border-radius: 12px;
    border: 1px solid #D8D8D2;
}}

treeview {{
    background-color: {COLORS['surface']}; color: {COLORS['text']};
    font-family: "JetBrains Mono","Fira Code",monospace;
    font-size: 13px;
}}
treeview header button {{
    background-color: {COLORS['bg']};
    color: {COLORS['muted']};
    font-weight: 700; font-size: 10px; letter-spacing: 0.06em;
    border-bottom: 1px solid {COLORS['overlay']};
    padding: 12px 10px;
}}
treeview header button:hover {{
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}
treeview row {{ padding: 4px 0; }}
treeview row:nth-child(even) {{ background-color: {COLORS['bg']}; }}
treeview row:hover {{ background-color: rgba(245, 130, 31, 0.06); }}
treeview row:selected {{
    background-color: rgba(245, 130, 31, 0.18);
    color: {COLORS['text']};
}}

scrollbar {{ background-color: transparent; }}
scrollbar slider {{
    background-color: #D8D8D2; border-radius: 6px;
    min-width: 8px; min-height: 8px;
    border: 2px solid transparent;
}}
scrollbar slider:hover {{ background-color: {COLORS['muted']}; }}

/* Base button — overrides system theme gradients/dark fills.
   Sidebar / combobox / treeview headers have their own selectors and
   keep precedence via class specificity. */
button {{
    background-image: none;
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 10px;
    color: {COLORS['text']};
    padding: 8px 14px;
    font-size: 13px;
    box-shadow: none;
    text-shadow: none;
    -gtk-icon-shadow: none;
}}
button:hover {{
    background-image: none;
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}
button:active, button:checked {{
    background-image: none;
    background-color: #DEDED6;
}}
button:disabled {{
    background-image: none;
    background-color: {COLORS['surface']};
    color: {COLORS['muted']};
    opacity: 0.6;
}}

button.btn-primary {{
    background-image: none;
    background-color: {COLORS['accent']};
    border: 1px solid {COLORS['accent']};
    border-radius: 10px; color: #FFFFFF;
    padding: 9px 18px; font-weight: 600; font-size: 13px;
    box-shadow: 0 1px 2px rgba(245, 130, 31, 0.20);
    text-shadow: none;
}}
button.btn-primary:hover {{
    background-image: none;
    background-color: #E36F0D;
    border-color: #E36F0D;
    color: #FFFFFF;
}}
button.btn-primary:active {{
    background-image: none;
    background-color: #C95F08;
    border-color: #C95F08;
}}
button.btn-secondary {{
    background-image: none;
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 10px; color: {COLORS['text']};
    padding: 9px 16px; font-size: 13px;
    text-shadow: none;
}}
button.btn-secondary:hover {{
    background-image: none;
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}

.status-bar {{
    background-color: {COLORS['surface']};
    border-top: 1px solid {COLORS['overlay']};
    padding: 8px 28px;
}}

combobox button {{
    background-image: none;
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 10px; color: {COLORS['text']};
    padding: 6px 12px;
    text-shadow: none;
}}
combobox button:hover {{
    background-image: none;
    background-color: {COLORS['overlay']};
}}
combobox arrow {{ color: {COLORS['muted']}; -gtk-icon-shadow: none; }}

/* Dropdown popup itself — combobox menu, right-click menus, etc. */
menu, .menu, popover, popover.background, popover contents {{
    background-image: none;
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 10px;
    padding: 6px;
    box-shadow: 0 4px 14px rgba(15, 15, 25, 0.10);
}}
menuitem, .menuitem {{
    background-image: none;
    background-color: transparent;
    color: {COLORS['text']};
    padding: 7px 12px;
    border-radius: 6px;
    text-shadow: none;
}}
menuitem:hover, .menuitem:hover,
menuitem:focus, .menuitem:focus {{
    background-image: none;
    background-color: rgba(245, 130, 31, 0.10);
    color: {COLORS['accent']};
}}
menuitem:disabled {{
    color: {COLORS['muted']};
    opacity: 0.85;
}}
window.usage-popup {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 14px;
}}
.usage-popup-title {{
    font-size: 15px; font-weight: 700; color: {COLORS['text']};
}}
.usage-popup-label {{ font-size: 13px; font-weight: 600; color: {COLORS['text']}; }}
.usage-popup-sub {{ font-size: 12px; color: {COLORS['muted']}; }}
.usage-popup progressbar trough {{
    min-height: 8px; border-radius: 4px;
    background-color: {COLORS['overlay']}; border: none;
}}
.usage-popup progressbar progress {{
    min-height: 8px; border-radius: 4px; border: none;
}}
.pb-low progress {{ background-color: {USAGE_COLORS['low']}; background-image: none; }}
.pb-medium progress {{ background-color: {USAGE_COLORS['medium']}; background-image: none; }}
.pb-high progress {{ background-color: {USAGE_COLORS['high']}; background-image: none; }}
.pb-critical progress {{ background-color: {USAGE_COLORS['critical']}; background-image: none; }}
.pb-unknown progress {{ background-color: {USAGE_COLORS['unknown']}; background-image: none; }}
menuitem separator, separator {{
    background-color: {COLORS['overlay']};
    min-height: 1px; min-width: 1px;
    margin: 4px 0;
}}

/* Editable text entries (token field, budget name, etc.) */
entry {{
    background-image: none;
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid #D8D8D2;
    border-radius: 10px;
    padding: 8px 12px;
    caret-color: {COLORS['accent']};
}}
entry:focus {{
    border-color: {COLORS['accent']};
    box-shadow: 0 0 0 3px rgba(245, 130, 31, 0.18);
}}
entry selection {{
    background-color: rgba(245, 130, 31, 0.25);
    color: {COLORS['text']};
}}

/* Spin buttons (used inside combobox / numeric inputs) */
spinbutton {{
    background-image: none;
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['overlay']};
    border-radius: 10px;
}}
spinbutton entry {{ border: none; box-shadow: none; }}
spinbutton button {{
    background-image: none;
    background-color: transparent;
    border: none;
    color: {COLORS['muted']};
}}
spinbutton button:hover {{
    background-color: {COLORS['overlay']};
    color: {COLORS['text']};
}}

/* Dialog windows (file chooser, message dialogs, etc.) */
dialog, messagedialog, filechooser {{
    background-color: {COLORS['bg']};
    color: {COLORS['text']};
}}
dialog .titlebar, headerbar {{
    background-image: none;
    background-color: {COLORS['surface']};
    color: {COLORS['text']};
    border-bottom: 1px solid {COLORS['overlay']};
    box-shadow: none;
    text-shadow: none;
}}

/* Tooltips */
tooltip {{
    background-color: {COLORS['text']};
    color: {COLORS['surface']};
    border-radius: 6px;
    padding: 6px 10px;
    text-shadow: none;
}}
tooltip label {{ color: {COLORS['surface']}; }}

/* Checkboxes / radios — keep them brand-colored when active */
checkbutton check, radiobutton radio {{
    background-image: none;
    background-color: {COLORS['surface']};
    border: 1px solid #C8C8C2;
}}
checkbutton check:checked, radiobutton radio:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
    color: #FFFFFF;
}}

/* Frames / labels generally pick up the window background */
label {{ color: {COLORS['text']}; }}
"""


# ─── Widgets ───────────────────────────────────────────────────────────────

class SummaryCard(Gtk.Box):
    def __init__(self, title: str, value: str = "--", subtitle: str = "",
                 accent_color: str = COLORS['accent'],
                 color_key: Optional[str] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class('card')
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        # color_key (e.g. 'blue') lets retint() re-derive the accent from
        # the live COLORS dict after a theme switch; accent_color alone
        # would stay pinned to the palette active at construction time.
        self._color_key = color_key
        self._accent_color = accent_color

        # 28×3px colored accent dash above the title.
        dash = Gtk.Box()
        dash.set_size_request(28, 3)
        dash.set_halign(Gtk.Align.START)
        dash.get_style_context().add_class('accent-dash')
        dash_provider = Gtk.CssProvider()
        dash_provider.load_from_data(
            f'box {{ background-color: {accent_color}; }}'.encode())
        dash.get_style_context().add_provider(
            dash_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        dash.set_margin_bottom(12)
        self.pack_start(dash, False, False, 0)
        self._dash = dash
        self._dash_provider = dash_provider

        self.title_lbl = Gtk.Label(label=title)
        self.title_lbl.get_style_context().add_class('card-title')
        self.title_lbl.set_halign(Gtk.Align.START)
        self.pack_start(self.title_lbl, False, False, 0)

        self.value_lbl = Gtk.Label()
        self.value_lbl.get_style_context().add_class('card-value')
        self.value_lbl.set_halign(Gtk.Align.START)
        self.value_lbl.set_markup(
            f'<span foreground="{accent_color}">{html.escape(value)}</span>'
        )
        self.pack_start(self.value_lbl, False, False, 0)

        self.sub_lbl = Gtk.Label(label=subtitle)
        self.sub_lbl.get_style_context().add_class('card-subtitle')
        self.sub_lbl.set_halign(Gtk.Align.START)
        self.sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(self.sub_lbl, False, False, 0)

    def update(self, value: str, subtitle: Optional[str] = None,
               color: Optional[str] = None, title: Optional[str] = None):
        c = color or self._accent_color
        self.value_lbl.set_markup(
            f'<span foreground="{c}">{html.escape(value)}</span>'
        )
        if subtitle is not None:
            self.sub_lbl.set_text(subtitle)
        if title is not None:
            self.title_lbl.set_text(title)

    def retint(self):
        """Re-apply the accent color from the live COLORS dict — call
        after a theme switch so the dash and value label don't stay
        pinned to the palette active when the card was constructed."""
        if self._color_key is None:
            return
        self._accent_color = COLORS[self._color_key]
        self._dash_provider.load_from_data(
            f'box {{ background-color: {self._accent_color}; }}'.encode())
        current = self.value_lbl.get_text()
        self.value_lbl.set_markup(
            f'<span foreground="{self._accent_color}">'
            f'{html.escape(current)}</span>'
        )


class UsageBar(Gtk.Box):
    def __init__(self, label: str, percentage: float):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(8)
        self.set_margin_bottom(8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.label_lbl = Gtk.Label(label=label)
        self.label_lbl.set_halign(Gtk.Align.START)
        header.pack_start(self.label_lbl, True, True, 0)
        self.pct_lbl = Gtk.Label(label=f"{int(percentage * 100)}%")
        self.pct_lbl.set_halign(Gtk.Align.END)
        header.pack_end(self.pct_lbl, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.progress = Gtk.LevelBar()
        self.progress.set_min_value(0)
        self.progress.set_max_value(1.0)
        self.progress.set_value(percentage)
        self.progress.set_size_request(-1, 8)

        self._trough_provider = Gtk.CssProvider()
        self.progress.get_style_context().add_provider(
            self._trough_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._percentage = percentage
        self._apply_bar_css()
        self.pack_start(self.progress, False, False, 0)

    def _apply_bar_css(self):
        bar_color = get_usage_color(self._percentage)
        css = f"""
            levelbar trough {{
                background-color: {COLORS['overlay']};
                border-radius: 4px; min-height: 8px;
            }}
            levelbar trough block.filled {{
                background-color: {bar_color};
                border-radius: 4px; min-height: 8px;
            }}
        """
        self._trough_provider.load_from_data(css.encode())

    def update(self, percentage: float, label: Optional[str] = None,
               reset_time: Optional[str] = None):
        self._percentage = percentage
        self.progress.set_value(percentage)
        self.pct_lbl.set_text(f"{int(percentage * 100)}%")
        if label:
            text = label
            if reset_time and reset_time != 'unknown':
                text += f"  (resets in {reset_time})"
            self.label_lbl.set_text(text)
        self._apply_bar_css()  # keep trough color in sync with the live theme

    def retint(self):
        """Re-apply trough colors from the live COLORS dict after a
        theme switch, without touching the current percentage/label."""
        self._apply_bar_css()


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
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._stack.set_transition_duration(150)
        def _scrolled(widget):
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.add(widget)
            return sw

        self._stack.add_named(_scrolled(self._build_dashboard_view()), 'dashboard')
        self._stack.add_named(_scrolled(self._build_projects_view()), 'projects')
        self._stack.add_named(_scrolled(self._build_breakdowns_view()), 'breakdowns')
        self._stack.add_named(_scrolled(self._build_budgets_view()), 'budgets')
        self._stack.add_named(_scrolled(self._build_settings_view()), 'settings')
        content.pack_start(self._stack, True, True, 0)

        content.pack_start(self._build_status_bar(), False, False, 0)

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.get_style_context().add_class('sidebar')
        sidebar.set_size_request(220, -1)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_margin_bottom(24)
        title_box.set_margin_start(8)
        sidebar_icon = None
        ip = _icon_path(48)
        if ip is not None:
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ip), 28, 28)
                sidebar_icon = Gtk.Image.new_from_pixbuf(pix)
            except Exception:
                sidebar_icon = None
        if sidebar_icon is None:
            sidebar_icon = Gtk.Image.new_from_icon_name(
                'utilities-terminal-symbolic', Gtk.IconSize.DND)
        title_box.pack_start(sidebar_icon, False, False, 0)
        t = Gtk.Label(label="Token Tracker")
        t.get_style_context().add_class('page-title')
        t.set_halign(Gtk.Align.START)
        t.set_ellipsize(Pango.EllipsizeMode.END)
        title_box.pack_start(t, True, True, 0)
        sidebar.pack_start(title_box, False, False, 0)

        nav_header = Gtk.Label(label="NAVIGATION")
        nav_header.get_style_context().add_class('sidebar-section')
        nav_header.set_halign(Gtk.Align.START)
        sidebar.pack_start(nav_header, False, False, 0)

        self._nav_buttons: Dict[str, Gtk.Button] = {}
        for vid, label in [
            ('dashboard',  'Dashboard'),
            ('projects',   'Projects'),
            ('breakdowns', 'Breakdowns'),
            ('budgets',    'Budgets'),
            ('settings',   'Settings'),
        ]:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class('sidebar-btn')
            btn.set_relief(Gtk.ReliefStyle.NONE)
            child = btn.get_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0.0)
            btn.connect('clicked',
                        lambda _, v=vid: self._switch_view(v))
            sidebar.pack_start(btn, False, False, 0)
            self._nav_buttons[vid] = btn
        return sidebar

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.get_style_context().add_class('header-bar')
        self._header_title = Gtk.Label(label="Dashboard")
        self._header_title.get_style_context().add_class('page-title')
        header.pack_start(self._header_title, False, False, 0)
        header.pack_start(Gtk.Box(), True, True, 0)

        # Account filter — only meaningful when more than one account.
        # Always pack it so changes via Settings can show/hide it without
        # rebuilding the header.
        self._account_combo = Gtk.ComboBoxText()
        self._populate_account_combo()
        self._account_combo.connect('changed', self._on_account_filter_changed)
        self._account_combo.set_no_show_all(True)
        header.pack_end(self._account_combo, False, False, 0)
        self._account_combo.set_visible(len(self.app._accounts) > 1)

        refresh_btn = Gtk.Button()
        refresh_btn.set_image(Gtk.Image.new_from_icon_name(
            'view-refresh-symbolic', Gtk.IconSize.BUTTON))
        refresh_btn.set_tooltip_text("Refresh (Ctrl+R)")
        refresh_btn.get_style_context().add_class('btn-secondary')
        refresh_btn.connect('clicked', lambda _: self.refresh_data())
        header.pack_end(refresh_btn, False, False, 0)
        self._header_box = header  # so we can re-show the combo on reload
        return header

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
        self._account_combo.set_visible(len(self.app._accounts) > 1)

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

        if view_id == 'dashboard':    self._update_dashboard()
        elif view_id == 'projects':   self._update_projects_view()
        elif view_id == 'breakdowns': self._update_breakdowns_view()
        elif view_id == 'budgets':    self._update_budgets_view()

    # ── Period helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _cutoff(period: str) -> Optional[datetime]:
        now = datetime.now(timezone.utc)
        if period == 'all':   return None
        if period == 'today': return datetime(now.year, now.month, now.day,
                                              tzinfo=timezone.utc)
        if period == '5h':    return now - timedelta(hours=5)
        if period == '7d':    return now - timedelta(days=7)
        if period == '30d':   return now - timedelta(days=30)
        return None

    # ── Dashboard ──────────────────────────────────────────────────────────
    def _build_dashboard_view(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        c.set_margin_top(24); c.set_margin_bottom(24)
        c.set_margin_start(24); c.set_margin_end(24)
        scrolled.add(c)

        # Period selector
        scope = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        h = Gtk.Label(label="Period")
        # Styled via the .subtitle CSS class (not inline markup) so it
        # picks up the live palette automatically when the global CSS
        # provider is swapped on a theme switch — no manual retint needed.
        h.get_style_context().add_class('subtitle')
        scope.pack_start(h, False, False, 0)
        self._dashboard_period_combo = Gtk.ComboBoxText()
        for k in ('all', 'today', '7d', '30d'):
            self._dashboard_period_combo.append(k, PERIOD_LABELS[k])
        self._dashboard_period_combo.set_active_id(self._dashboard_period)
        self._dashboard_period_combo.connect(
            'changed', lambda combo: (
                setattr(self, '_dashboard_period',
                        combo.get_active_id() or 'all'),
                self._update_dashboard()))
        scope.pack_start(self._dashboard_period_combo, False, False, 0)
        self._dashboard_scope_label = Gtk.Label()
        self._dashboard_scope_label.set_halign(Gtk.Align.START)
        self._dashboard_scope_label.set_hexpand(True)
        self._dashboard_scope_label.set_ellipsize(Pango.EllipsizeMode.END)
        scope.pack_start(self._dashboard_scope_label, True, True, 0)
        c.pack_start(scope, False, False, 0)

        # ── Plan limits (was its own tab) ───────────────────────────────────
        self._cloud_status_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._cloud_status_card.get_style_context().add_class('card')
        self._cloud_status_card.set_margin_top(16)
        c.pack_start(self._cloud_status_card, False, False, 0)

        self._cloud_usage_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._cloud_usage_box.set_margin_top(12)
        self._cloud_usage_box.set_homogeneous(True)
        self._usage_5h = UsageBar("5-Hour Window", 0.0)
        self._cloud_usage_box.pack_start(self._usage_5h, True, True, 0)
        self._usage_7d = UsageBar("7-Day Window", 0.0)
        self._cloud_usage_box.pack_start(self._usage_7d, True, True, 0)
        # Per-model weekly caps get their own bars, created on demand as the
        # API reports them (keyed by display name).
        self._usage_model_bars: Dict[str, UsageBar] = {}
        c.pack_start(self._cloud_usage_box, False, False, 0)

        # Summary cards
        cards = Gtk.FlowBox()
        cards.set_selection_mode(Gtk.SelectionMode.NONE)
        cards.set_column_spacing(16); cards.set_row_spacing(16)
        cards.set_margin_top(16); cards.set_homogeneous(True)
        cards.set_max_children_per_line(5); cards.set_min_children_per_line(2)
        self._summary_cards = {
            'total':  SummaryCard("Total Tokens", "0", "", COLORS['accent'],
                                  color_key='accent'),
            'input':  SummaryCard("Input Tokens", "0", "", COLORS['blue'],
                                  color_key='blue'),
            'cache':  SummaryCard("Cache Tokens", "0", "", COLORS['orange'],
                                  color_key='orange'),
            'output': SummaryCard("Output Tokens", "0", "", COLORS['green'],
                                  color_key='green'),
            'cost':   SummaryCard("API Equivalent", "$0.00", "",
                                  COLORS['yellow'], color_key='yellow'),
        }
        self._summary_cards['cost'].set_tooltip_text(
            "What these tokens would cost on the pay-as-you-go API.\n"
            "If you're on Pro/Max/Team, you pay a flat fee — this is the\n"
            "API-equivalent value, not your actual bill."
        )
        for card in self._summary_cards.values():
            cards.add(card)
        c.pack_start(cards, False, False, 0)

        # Active block / forecast card
        block_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        block_box.get_style_context().add_class('card')
        block_box.set_margin_top(16)
        bt = Gtk.Label(label="Current 5h Block")
        bt.get_style_context().add_class('card-title')
        bt.set_halign(Gtk.Align.START)
        block_box.pack_start(bt, False, False, 0)
        self._block_window_lbl = Gtk.Label()
        self._block_window_lbl.set_halign(Gtk.Align.START)
        self._block_window_lbl.get_style_context().add_class('subtitle')
        block_box.pack_start(self._block_window_lbl, False, False, 0)

        block_grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        block_grid.set_margin_top(8)
        self._block_remaining = self._mini_stat("Remaining", "—",
                                                COLORS['accent'])
        self._block_tokens = self._mini_stat("Tokens", "0", COLORS['blue'])
        self._block_cost = self._mini_stat("Cost", "$0.00", COLORS['yellow'])
        self._block_burn = self._mini_stat("Burn", "—/min", COLORS['orange'])
        self._block_eta = self._mini_stat("ETA to limit", "—", COLORS['red'])
        for w in (self._block_remaining, self._block_tokens, self._block_cost,
                  self._block_burn, self._block_eta):
            block_grid.pack_start(w, False, False, 0)
        block_box.pack_start(block_grid, False, False, 0)
        c.pack_start(block_box, False, False, 0)

        # Bottom: top projects + top models
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        bottom.set_margin_top(24)
        c.pack_start(bottom, True, True, 0)

        # Projects
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pbox.get_style_context().add_class('card')
        pbox.set_hexpand(True)
        pt = Gtk.Label(label="Top Projects")
        pt.get_style_context().add_class('card-title')
        pt.set_halign(Gtk.Align.START)
        pbox.pack_start(pt, False, False, 0)
        self._dashboard_projects_subtitle = Gtk.Label()
        self._dashboard_projects_subtitle.get_style_context().add_class('subtitle')
        self._dashboard_projects_subtitle.set_halign(Gtk.Align.START)
        pbox.pack_start(self._dashboard_projects_subtitle, False, False, 0)
        self._dashboard_projects_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pbox.pack_start(self._dashboard_projects_content, True, True, 0)
        bottom.pack_start(pbox, True, True, 0)

        # Models
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mbox.get_style_context().add_class('card')
        mbox.set_hexpand(True)
        mt = Gtk.Label(label="Top Models")
        mt.get_style_context().add_class('card-title')
        mt.set_halign(Gtk.Align.START)
        mbox.pack_start(mt, False, False, 0)
        self._dashboard_models_subtitle = Gtk.Label()
        self._dashboard_models_subtitle.get_style_context().add_class('subtitle')
        self._dashboard_models_subtitle.set_halign(Gtk.Align.START)
        mbox.pack_start(self._dashboard_models_subtitle, False, False, 0)
        self._dashboard_models_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mbox.pack_start(self._dashboard_models_content, True, True, 0)
        bottom.pack_start(mbox, True, True, 0)

        return scrolled

    def _mini_stat(self, title: str, value: str, color: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(label=title)
        t.get_style_context().add_class('subtitle')
        t.set_halign(Gtk.Align.START)
        box.pack_start(t, False, False, 0)
        v = Gtk.Label()
        v.set_markup(
            f'<span foreground="{color}" font_weight="bold" '
            f'font_size="14pt">{html.escape(value)}</span>'
        )
        v.set_halign(Gtk.Align.START)
        box.pack_start(v, False, False, 0)
        box._value_lbl = v  # type: ignore[attr-defined]
        box._color = color  # type: ignore[attr-defined]
        return box

    @staticmethod
    def _set_mini(box: Gtk.Box, value: str, color: Optional[str] = None):
        c = color or box._color  # type: ignore[attr-defined]
        box._value_lbl.set_markup(  # type: ignore[attr-defined]
            f'<span foreground="{c}" font_weight="bold" '
            f'font_size="14pt">{html.escape(value)}</span>'
        )

    def _update_dashboard(self):
        store = self.app.store
        period = self._dashboard_period
        cutoff = self._cutoff(period)
        acc = self._account_filter
        rows = store.query(since=cutoff, account=acc)
        self._update_cloud_section()

        total = len(rows)
        total_tokens = sum(r['input_tokens'] + r['cache_creation_5m']
                           + r['cache_creation_1h'] + r['cache_read']
                           + r['output_tokens'] for r in rows)
        total_input = sum(r['input_tokens'] for r in rows)
        total_cc = sum(r['cache_creation_5m'] + r['cache_creation_1h']
                       for r in rows)
        total_cr = sum(r['cache_read'] for r in rows)
        total_output = sum(r['output_tokens'] for r in rows)
        total_cost = sum(r['cost_usd'] or 0 for r in rows)
        total_cache = total_cc + total_cr
        projects = len({r['project'] for r in rows})

        label = PERIOD_LABELS.get(period, 'All Time')
        rng = period_range_text(period)
        self._dashboard_scope_label.set_markup(
            f'<span foreground="{COLORS["subtext"]}">'
            f'Showing <b>{html.escape(label)}</b>'
            + (f' · {html.escape(rng)}' if rng else '')
            + f' · {projects} project{"s" if projects != 1 else ""}'
            + f' · {total} message{"s" if total != 1 else ""}'
            '</span>'
        )

        suffix = f"· {label}"
        self._summary_cards['total'].update(
            fmt(total_tokens),
            f"{fmt_full(total_tokens)} across {projects} projects",
            title=f"Total Tokens {suffix}",
        )
        self._summary_cards['input'].update(
            fmt(total_input), "Fresh prompt tokens (not cached)",
            title=f"Input Tokens {suffix}",
        )
        self._summary_cards['cache'].update(
            fmt(total_cache),
            f"create: {fmt(total_cc)} · read: {fmt(total_cr)}",
            title=f"Cache Tokens {suffix}",
        )
        self._summary_cards['output'].update(
            fmt(total_output), "Generated text",
            title=f"Output Tokens {suffix}",
        )
        plan, flat = subscription_summary(self.app.subscription_info)
        sub = (f"API equiv. — you're on {plan} (flat fee)"
               if flat else "API per-token pricing from logs")
        self._summary_cards['cost'].update(
            fmt_cost(total_cost), sub,
            title=f"API Equivalent {suffix}",
        )

        # Block / forecast
        block_rows = store.query(
            since=datetime.now(timezone.utc) - timedelta(hours=24),
            account=acc)
        blocks = compute_blocks(block_rows)
        cloud_5h = None
        cv = self.app.account_state(acc)
        if cv.usage_data:
            cloud_5h = normalize_utilization(
                (cv.usage_data.get('five_hour') or {}).get('utilization', 0)
            )
        fc = forecast_active(blocks, cloud_5h_pct=cloud_5h)
        if fc.block and fc.block.is_active():
            b = fc.block
            self._block_window_lbl.set_text(
                f"{b.start.astimezone():%H:%M} → {b.end.astimezone():%H:%M}  "
                f"· {b.messages} message{'s' if b.messages != 1 else ''}"
            )
            self._set_mini(self._block_remaining,
                           fmt_duration(b.remaining().total_seconds()))
            self._set_mini(self._block_tokens, fmt(b.total_tokens))
            self._set_mini(self._block_cost, fmt_cost(b.cost_usd))
            self._set_mini(self._block_burn,
                           f"{fmt(int(fc.burn_rate_per_min_tokens))}/min")
            if fc.eta_to_limit is not None:
                secs = int(fc.eta_to_limit.total_seconds())
                if secs <= 0:
                    self._set_mini(self._block_eta, "at limit",
                                   USAGE_COLORS['critical'])
                else:
                    self._set_mini(self._block_eta,
                                   fmt_duration(secs),
                                   get_usage_color(min((cloud_5h or 0)
                                                       + 0.01, 0.95)))
            else:
                self._set_mini(self._block_eta, "no cloud data",
                               COLORS['muted'])
        else:
            self._block_window_lbl.set_text("No activity in the last 5 hours")
            for w in (self._block_remaining, self._block_tokens,
                      self._block_cost, self._block_burn, self._block_eta):
                self._set_mini(w, "—", COLORS['muted'])

        # Top projects (period)
        for child in self._dashboard_projects_content.get_children():
            self._dashboard_projects_content.remove(child)
        self._dashboard_projects_subtitle.set_text(rng or label)
        proj = store.project_summary(since=cutoff, account=acc)
        if not proj:
            l = Gtk.Label(label="No activity")
            l.get_style_context().add_class('subtitle')
            self._dashboard_projects_content.pack_start(l, True, True, 0)
        else:
            top = proj[:5]
            max_t = top[0]['total_tokens'] or 1
            for p in top:
                self._dashboard_projects_content.pack_start(
                    self._build_bar_row(p['project'],
                                        p['total_tokens'] or 0,
                                        max_t,
                                        fmt_cost(p['cost_usd'] or 0)),
                    False, False, 0,
                )
        self._dashboard_projects_content.show_all()

        # Top models (period)
        for child in self._dashboard_models_content.get_children():
            self._dashboard_models_content.remove(child)
        self._dashboard_models_subtitle.set_text(rng or label)
        models = store.model_summary(since=cutoff, account=acc)
        if not models:
            l = Gtk.Label(label="No activity")
            l.get_style_context().add_class('subtitle')
            self._dashboard_models_content.pack_start(l, True, True, 0)
        else:
            top = models[:5]
            max_c = max((m['cost_usd'] or 0) for m in top) or 1
            for m in top:
                self._dashboard_models_content.pack_start(
                    self._build_bar_row(m['model'] or '(unknown)',
                                        m['cost_usd'] or 0,
                                        max_c,
                                        fmt(m['total_tokens'] or 0)),
                    False, False, 0,
                )
        self._dashboard_models_content.show_all()

    @staticmethod
    def _build_bar_row(name: str, value: float, max_value: float,
                       right_text: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        n = Gtk.Label(label=name)
        n.set_halign(Gtk.Align.START)
        n.set_ellipsize(Pango.EllipsizeMode.END)
        n.set_size_request(150, -1)
        row.pack_start(n, False, False, 0)
        bar = Gtk.LevelBar()
        bar.set_min_value(0); bar.set_max_value(1.0)
        bar.set_value(min(value / max_value, 1.0) if max_value else 0.0)
        bar.set_size_request(80, 6)
        row.pack_start(bar, True, True, 0)
        r = Gtk.Label(label=right_text)
        r.set_halign(Gtk.Align.END)
        row.pack_end(r, False, False, 0)
        return row

    # ── Projects view ──────────────────────────────────────────────────────
    def _build_projects_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        c.set_margin_top(16); c.set_margin_bottom(16)
        c.set_margin_start(16); c.set_margin_end(16)

        fb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        fb.set_margin_bottom(16)
        self._projects_search = Gtk.SearchEntry()
        self._projects_search.set_placeholder_text("Search projects...")
        self._projects_search.get_style_context().add_class('search-entry')
        self._projects_search.connect('search-changed',
                                      lambda _: self._update_projects_view())
        fb.pack_start(self._projects_search, True, True, 0)
        pl = Gtk.Label(label="Period:")
        pl.get_style_context().add_class('subtitle')
        self._projects_period_combo = Gtk.ComboBoxText()
        for k in ('all', 'today', '7d', '30d'):
            self._projects_period_combo.append(k, PERIOD_LABELS[k])
        self._projects_period_combo.set_active_id(self._projects_period)
        self._projects_period_combo.connect(
            'changed', lambda combo: (
                setattr(self, '_projects_period',
                        combo.get_active_id() or 'all'),
                self._update_projects_view()))
        fb.pack_end(self._projects_period_combo, False, False, 0)
        fb.pack_end(pl, False, False, 0)
        c.pack_start(fb, False, False, 0)

        self._projects_totals = Gtk.Label()
        self._projects_totals.set_halign(Gtk.Align.START)
        self._projects_totals.set_ellipsize(Pango.EllipsizeMode.END)
        self._projects_totals.set_margin_bottom(12)
        c.pack_start(self._projects_totals, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        c.pack_start(scrolled, True, True, 0)

        # TreeStore (parent = project, children = sessions). Display columns
        # 0-9; trailing columns are numeric counterparts used for sorting.
        self._projects_store = Gtk.TreeStore(
            str, str, str, str, str, str, str, str, str, str,   # 0-9 display
            float, int, int, int, int, int, int, int, int, float,  # 10-19 sort
        )
        DISPLAY = 10
        treeview = Gtk.TreeView(model=self._projects_store)
        treeview.set_headers_visible(True)
        for title, idx, width, expand in [
            ('Project / Session', 0, 220, True),
            ('Last Used', 1, 110, False),
            ('Sessions', 2, 80, False),
            ('Messages', 3, 80, False),
            ('Input', 4, 90, False),
            ('Cache Create', 5, 100, False),
            ('Cache Read', 6, 100, False),
            ('Output', 7, 90, False),
            ('Total', 8, 100, False),
            ('Cost', 9, 90, False),
        ]:
            r = Gtk.CellRendererText()
            r.set_property('xpad', 8); r.set_property('ypad', 6)
            if expand:
                r.set_property('ellipsize', Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_expand(expand); col.set_resizable(True)
            col.set_min_width(width)
            col.set_sort_column_id(idx + DISPLAY)
            treeview.append_column(col)
        scrolled.add(treeview)

        bb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bb.set_margin_top(16)
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

        rng = period_range_text(period) or PERIOD_LABELS.get(period, '')
        total_tokens = sum((r['total_tokens'] or 0) for r in rows)
        total_msgs = sum((r['messages'] or 0) for r in rows)
        total_cost = sum((r['cost_usd'] or 0) for r in rows)
        self._projects_totals.set_markup(
            f'<span foreground="{COLORS["subtext"]}">'
            f'<b>{len(rows)}</b> projects · '
            f'<b>{fmt(total_tokens)}</b> tokens · '
            f'{total_msgs} msg · '
            f'<b>{fmt_cost(total_cost)}</b> · {html.escape(rng)}'
            f'</span>'
        )

        # Pull session rows once, group by project, then attach as children.
        all_msgs = self.app.store.query(since=cutoff, account=acc)
        sessions_by_project: Dict[str, Dict[str, list]] = {}
        for m in all_msgs:
            proj = m['project'] or 'Unknown'
            sid = m['session_id'] or '(no-session)'
            sessions_by_project.setdefault(proj, {}).setdefault(sid, []).append(m)

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
                str(r['sessions'] or 0),
                str(r['messages'] or 0),
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
            sess_groups = sessions_by_project.get(name) or {}
            sess_summary = []
            for sid, msgs in sess_groups.items():
                msgs.sort(key=lambda x: x['timestamp'])
                first = datetime.fromisoformat(msgs[0]['timestamp'])
                last = datetime.fromisoformat(msgs[-1]['timestamp'])
                inp = sum(m['input_tokens'] or 0 for m in msgs)
                cc = sum((m['cache_creation_5m'] or 0)
                         + (m['cache_creation_1h'] or 0) for m in msgs)
                cr = sum(m['cache_read'] or 0 for m in msgs)
                out = sum(m['output_tokens'] or 0 for m in msgs)
                tot = inp + cc + cr + out
                cost = sum(m['cost_usd'] or 0 for m in msgs)
                sess_summary.append({
                    'sid': sid, 'first': first, 'last': last,
                    'msgs': len(msgs), 'inp': inp, 'cc': cc, 'cr': cr,
                    'out': out, 'tot': tot, 'cost': cost,
                })
            sess_summary.sort(key=lambda s: s['last'], reverse=True)
            for s in sess_summary[:50]:
                self._projects_store.append(parent, [
                    f"  {s['first'].astimezone():%Y-%m-%d %H:%M} · "
                    f"{s['sid'][:8]}",
                    rel_time(s['last']),
                    "—",
                    str(s['msgs']),
                    fmt(s['inp']),
                    fmt(s['cc']),
                    fmt(s['cr']),
                    fmt(s['out']),
                    fmt(s['tot']),
                    fmt_cost(s['cost']),
                    s['last'].timestamp(),
                    0,
                    s['msgs'],
                    s['inp'],
                    s['cc'],
                    s['cr'],
                    s['out'],
                    s['tot'],
                    int(s['cost'] * 1000),
                    s['cost'],
                ])

    # ── Breakdowns view (was: separate Models / Tools / Sessions tabs) ─────
    def _build_breakdowns_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        c.set_margin_top(16); c.set_margin_bottom(16)
        c.set_margin_start(16); c.set_margin_end(16)

        # Top bar: kind switcher + period selector
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.set_margin_bottom(16)

        self._breakdowns_stack = Gtk.Stack()
        self._breakdowns_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE)
        self._breakdowns_stack.set_transition_duration(120)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self._breakdowns_stack)
        top.pack_start(switcher, False, False, 0)

        top.pack_start(Gtk.Box(), True, True, 0)

        pl = Gtk.Label(label="Period:")
        pl.get_style_context().add_class('subtitle')
        self._breakdowns_period_combo = Gtk.ComboBoxText()
        for k in ('all', 'today', '7d', '30d'):
            self._breakdowns_period_combo.append(k, PERIOD_LABELS[k])
        self._breakdowns_period_combo.set_active_id(self._breakdowns_period)
        self._breakdowns_period_combo.connect(
            'changed', lambda cb: (
                setattr(self, '_breakdowns_period',
                        cb.get_active_id() or 'all'),
                self._update_breakdowns_view()))
        top.pack_end(self._breakdowns_period_combo, False, False, 0)
        top.pack_end(pl, False, False, 0)
        c.pack_start(top, False, False, 0)

        self._breakdowns_totals = Gtk.Label()
        self._breakdowns_totals.set_halign(Gtk.Align.START)
        self._breakdowns_totals.set_margin_bottom(12)
        c.pack_start(self._breakdowns_totals, False, False, 0)

        # Models page
        models_scrolled = Gtk.ScrolledWindow()
        models_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                   Gtk.PolicyType.AUTOMATIC)
        self._models_store = Gtk.ListStore(
            str, str, str, str, str, str, str, str)
        models_tv = Gtk.TreeView(model=self._models_store)
        models_tv.set_headers_visible(True)
        for title, idx, w in [('Model', 0, 240),
                              ('Messages', 1, 90),
                              ('Input', 2, 90),
                              ('Cache Create', 3, 110),
                              ('Cache Read', 4, 100),
                              ('Output', 5, 90),
                              ('Total', 6, 100),
                              ('Cost', 7, 100)]:
            r = Gtk.CellRendererText()
            r.set_property('xpad', 8)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_resizable(True); col.set_min_width(w)
            models_tv.append_column(col)
        models_scrolled.add(models_tv)
        self._breakdowns_stack.add_titled(models_scrolled, 'models', 'Models')

        # Tools page
        tools_scrolled = Gtk.ScrolledWindow()
        tools_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
        self._tools_store = Gtk.ListStore(str, str, str, str, str, str)
        tools_tv = Gtk.TreeView(model=self._tools_store)
        tools_tv.set_headers_visible(True)
        for title, idx, w in [('Tool', 0, 200),
                              ('Calls', 1, 80),
                              ('Messages', 2, 100),
                              ('Input tok', 3, 110),
                              ('Output tok', 4, 110),
                              ('Cost (turns)', 5, 120)]:
            r = Gtk.CellRendererText()
            r.set_property('xpad', 8)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.set_resizable(True); col.set_min_width(w)
            tools_tv.append_column(col)
        tools_scrolled.add(tools_tv)
        self._breakdowns_stack.add_titled(tools_scrolled, 'tools', 'Tools')

        c.pack_start(self._breakdowns_stack, True, True, 0)
        self._breakdowns_stack.set_visible_child_name(self._breakdowns_kind)
        self._breakdowns_stack.connect(
            'notify::visible-child-name',
            lambda *_: self._on_breakdowns_kind_changed())
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
                str(r['messages'] or 0),
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
                str(int(r['calls'])),
                str(int(r['messages'])),
                fmt(int(r['input_tokens'])),
                fmt(int(r['output_tokens'])),
                fmt_cost(r['cost_usd']),
            ])

        self._update_breakdowns_totals()

    def _update_breakdowns_totals(self):
        kind = self._breakdowns_kind
        rng = period_range_text(self._breakdowns_period) or PERIOD_LABELS.get(
            self._breakdowns_period, '')
        if kind == 'models':
            n = len(self._models_store)
            self._breakdowns_totals.set_markup(
                f'<span foreground="{COLORS["subtext"]}">'
                f'<b>{n}</b> models · {html.escape(rng)}</span>')
        else:
            n = len(self._tools_store)
            self._breakdowns_totals.set_markup(
                f'<span foreground="{COLORS["subtext"]}">'
                f'<b>{n}</b> tools · {html.escape(rng)} '
                f'· cost is the parent turn\'s cost, not per-call</span>')

    # ── Budgets view ───────────────────────────────────────────────────────
    def _build_budgets_view(self) -> Gtk.Widget:
        c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        c.set_margin_top(24); c.set_margin_bottom(24)
        c.set_margin_start(24); c.set_margin_end(24)

        intro = Gtk.Label()
        intro.set_halign(Gtk.Align.START)
        intro.set_line_wrap(True)
        intro.get_style_context().add_class('subtitle')
        intro.set_text(
            "Budgets warn you when you cross a limit. Choose USD or tokens "
            "per day/week/month, or — on Max/Pro/Team plans — a % of plan "
            "utilization over the rolling 5h or 7d window pulled from "
            "claude.ai. Plan-% budgets need a working OAuth token."
        )
        c.pack_start(intro, False, False, 0)

        self._budgets_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                     spacing=8)
        c.pack_start(self._budgets_list, False, False, 0)

        # Add form
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.get_style_context().add_class('card')
        form.set_margin_top(12)
        ft = Gtk.Label(label="Add a budget")
        ft.get_style_context().add_class('card-title')
        ft.set_halign(Gtk.Align.START)
        form.pack_start(ft, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(8); grid.set_row_spacing(6)
        self._budget_name = Gtk.Entry()
        self._budget_name.set_placeholder_text("Name (e.g. 'monthly cap')")
        self._budget_scope = Gtk.Entry()
        self._budget_scope.set_text("global")
        self._budget_scope.set_placeholder_text(
            "global | project:NAME | model:MODEL_ID")
        self._budget_period = Gtk.ComboBoxText()
        for pid, plabel in (('day', 'day'), ('week', 'week'),
                            ('month', 'month'),
                            ('5h', '5h (plan window)'),
                            ('7d', '7d (plan window)')):
            self._budget_period.append(pid, plabel)
        self._budget_period.set_active_id('month')
        self._budget_usd = Gtk.Entry()
        self._budget_usd.set_placeholder_text("USD limit (or leave blank)")
        self._budget_tokens = Gtk.Entry()
        self._budget_tokens.set_placeholder_text("Token limit (or leave blank)")
        self._budget_plan_pct = Gtk.Entry()
        self._budget_plan_pct.set_placeholder_text(
            "Plan utilization % (Max/Pro/Team — uses 5h or 7d)")
        self._budget_notify_pct = Gtk.SpinButton.new_with_range(50, 100, 5)
        self._budget_notify_pct.set_value(80)

        def lbl(text):
            l = Gtk.Label(label=text); l.set_halign(Gtk.Align.START); return l
        grid.attach(lbl("Name:"), 0, 0, 1, 1)
        grid.attach(self._budget_name, 1, 0, 2, 1)
        grid.attach(lbl("Scope:"), 0, 1, 1, 1)
        grid.attach(self._budget_scope, 1, 1, 2, 1)
        grid.attach(lbl("Period:"), 0, 2, 1, 1)
        grid.attach(self._budget_period, 1, 2, 1, 1)
        grid.attach(lbl("USD:"), 0, 3, 1, 1)
        grid.attach(self._budget_usd, 1, 3, 1, 1)
        grid.attach(lbl("Tokens:"), 0, 4, 1, 1)
        grid.attach(self._budget_tokens, 1, 4, 1, 1)
        grid.attach(lbl("Plan %:"), 0, 5, 1, 1)
        grid.attach(self._budget_plan_pct, 1, 5, 1, 1)
        grid.attach(lbl("Notify at %:"), 0, 6, 1, 1)
        grid.attach(self._budget_notify_pct, 1, 6, 1, 1)
        form.pack_start(grid, False, False, 0)

        add_btn = Gtk.Button(label="Add Budget")
        add_btn.get_style_context().add_class('btn-primary')
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect('clicked', self._on_budget_add)
        form.pack_start(add_btn, False, False, 0)

        c.pack_start(form, False, False, 0)
        return c

    def _update_budgets_view(self):
        for child in self._budgets_list.get_children():
            self._budgets_list.remove(child)
        states = evaluate_budgets(self.app.store,
                                  usage_data=self.app.usage_data)
        if not states:
            l = Gtk.Label(label="No budgets configured.")
            l.get_style_context().add_class('subtitle')
            l.set_halign(Gtk.Align.START)
            self._budgets_list.pack_start(l, False, False, 0)
            self._budgets_list.show_all()
            return
        for s in states:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.get_style_context().add_class('card')
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            t = Gtk.Label()
            kind = ' · plan-%' if s.is_pct_based else ''
            t.set_markup(
                f'<b>{html.escape(s.name)}</b>  '
                f'<span foreground="{COLORS["subtext"]}">'
                f'· {html.escape(s.scope)} · {s.period}{kind}</span>'
            )
            t.set_halign(Gtk.Align.START)
            left.pack_start(t, False, False, 0)

            if s.is_pct_based:
                limit = f"{s.limit_pct:.0f}% of plan"
                if s.data_available:
                    spent = f"{s.spent_pct:.0f}% used"
                    pct_text = f"{s.pct * 100:.0f}%"
                    color = get_usage_color(s.pct)
                else:
                    spent = "no cloud data — connect token in Cloud tab"
                    pct_text = "—"
                    color = COLORS["subtext"]
            else:
                spent = (fmt_cost(s.spent_usd) if s.limit_usd
                         else f"{s.spent_tokens:,} tok")
                limit = (fmt_cost(s.limit_usd) if s.limit_usd
                         else f"{s.limit_tokens:,} tok"
                         if s.limit_tokens else '—')
                pct_text = f"{s.pct * 100:.0f}%"
                color = get_usage_color(s.pct)
            d = Gtk.Label()
            d.set_markup(
                f'<span foreground="{color}" font_weight="bold">'
                f'{pct_text}</span>  '
                f'<span foreground="{COLORS["subtext"]}">'
                f'{html.escape(spent)} of {html.escape(limit)} '
                f'(period {html.escape(s.period_key)})</span>'
            )
            d.set_halign(Gtk.Align.START)
            left.pack_start(d, False, False, 0)
            row.pack_start(left, True, True, 0)

            del_btn = Gtk.Button(label="Remove")
            del_btn.get_style_context().add_class('btn-secondary')
            del_btn.connect('clicked',
                            lambda _, bid=s.id: self._on_budget_remove(bid))
            row.pack_end(del_btn, False, False, 0)
            self._budgets_list.pack_start(row, False, False, 0)
        self._budgets_list.show_all()

    def _on_budget_add(self, _btn):
        name = self._budget_name.get_text().strip()
        scope = self._budget_scope.get_text().strip() or 'global'
        period = self._budget_period.get_active_id() or 'month'
        usd_text = self._budget_usd.get_text().strip()
        tok_text = self._budget_tokens.get_text().strip()
        plan_pct_text = self._budget_plan_pct.get_text().strip()
        notify_pct = int(self._budget_notify_pct.get_value())
        if not name:
            self._status_label.set_text("Budget needs a name")
            return
        try:
            usd = float(usd_text) if usd_text else None
            tokens = int(tok_text) if tok_text else None
            plan_pct = float(plan_pct_text) if plan_pct_text else None
        except ValueError:
            self._status_label.set_text("USD/Tokens/Plan-% must be numbers")
            return
        if not (usd or tokens or plan_pct):
            self._status_label.set_text(
                "Budget needs a USD, token, or plan-% limit")
            return
        try:
            self.app.store.add_budget(name, scope, period, usd, tokens,
                                      notify_pct, plan_pct)
        except ValueError as e:
            self._status_label.set_text(str(e))
            return
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

    # ── Plan-limits section (lives inside the dashboard view) ──────────────
    def _update_cloud_section(self):
        for child in self._cloud_status_card.get_children():
            self._cloud_status_card.remove(child)

        cv, state = self._cloud_view()
        have = bool(cv.usage_data)
        # When showing 'All accounts' with more than one configured, the bars
        # below reflect only the primary account — say so.
        primary_only = (self._account_filter is None
                        and len(self.app._accounts) > 1)

        if state == 'no_token':
            self._cloud_card_msg("No OAuth token configured",
                                 "Run `claude` to log in, or paste a token "
                                 "in Settings.")
            btn = Gtk.Button(label="Set Token")
            btn.get_style_context().add_class('btn-primary')
            btn.set_halign(Gtk.Align.START)
            btn.connect('clicked', lambda _: self.app._on_set_token())
            self._cloud_status_card.pack_start(btn, False, False, 0)
        elif state == 'rate_limited':
            retry = cv.cloud_retry_at
            extra = ""
            if retry:
                secs = max(0, int((retry - datetime.now(timezone.utc))
                                  .total_seconds()))
                extra = f" Next retry in {fmt_duration(secs)}."
            self._cloud_card_msg("Rate limited",
                                 (cv.cloud_error or "") + extra,
                                 color=COLORS['yellow'])
            self._cloud_refresh_btn("Retry now")
        elif state == 'auth_error':
            self._cloud_card_msg("Token rejected",
                                 cv.cloud_error or
                                 "OAuth token expired or invalid.",
                                 color=COLORS['red'])
            self._cloud_refresh_btn("Retry")
        elif state == 'network_error':
            self._cloud_card_msg("Couldn't reach Claude.ai",
                                 cv.cloud_error or "Network error.",
                                 color=COLORS['red'])
            self._cloud_refresh_btn("Retry")
        elif state == 'loading' and not have:
            self._cloud_card_msg("Loading cloud usage…",
                                 "Contacting api.anthropic.com…")
        else:
            sub = cv.subscription_info or {}
            line = sub.get('type') or 'Claude.ai'
            if sub.get('tier'):
                line += f" · {sub['tier']}"
            detail = "Live 5-hour and 7-day windows."
            if primary_only:
                detail += f" Showing the primary account ({cv.account.label})."
            self._cloud_card_msg(f"Connected — {line}", detail,
                                 color=COLORS['green'])
            self._cloud_refresh_btn("Refresh")
        self._cloud_status_card.show_all()

        if have:
            five = cv.usage_data.get('five_hour', {}) or {}
            seven = cv.usage_data.get('seven_day', {}) or {}
            u5 = normalize_utilization(five.get('utilization', 0))
            u7 = normalize_utilization(seven.get('utilization', 0))
            self._usage_5h.update(u5, "5-Hour Window",
                                  format_reset_time(five.get('resets_at')))
            self._usage_7d.update(u7, "7-Day Window",
                                  format_reset_time(seven.get('resets_at')))
            # Per-model weekly caps: one bar each, created on first sighting
            # and dropped when the API stops reporting the model.
            model_limits = extract_model_limits(cv.usage_data)
            present = set()
            for m in model_limits:
                name = m['model']
                present.add(name)
                label = f"{name} (weekly)" + (" ●" if m['is_active'] else "")
                bar = self._usage_model_bars.get(name)
                if bar is None:
                    bar = UsageBar(label, m['fraction'])
                    self._usage_model_bars[name] = bar
                    self._cloud_usage_box.pack_start(bar, True, True, 0)
                bar.update(m['fraction'], label,
                           format_reset_time(m['resets_at']))
            for name in list(self._usage_model_bars):
                if name not in present:
                    bar = self._usage_model_bars.pop(name)
                    self._cloud_usage_box.remove(bar)
                    bar.destroy()
            self._cloud_usage_box.show_all()
        else:
            self._cloud_usage_box.hide()

    def _cloud_card_msg(self, title_text: str, sub_text: str,
                        color: Optional[str] = None):
        t = Gtk.Label()
        t.get_style_context().add_class('card-title')
        t.set_halign(Gtk.Align.START)
        if color:
            t.set_markup(f'<span foreground="{color}" font_weight="bold">'
                         f'{html.escape(title_text)}</span>')
        else:
            t.set_text(title_text)
        self._cloud_status_card.pack_start(t, False, False, 0)
        s = Gtk.Label(label=sub_text)
        s.get_style_context().add_class('subtitle')
        s.set_halign(Gtk.Align.START)
        s.set_line_wrap(True); s.set_xalign(0)
        self._cloud_status_card.pack_start(s, False, False, 0)

    def _cloud_refresh_btn(self, label: str):
        btn = Gtk.Button(label=label)
        btn.get_style_context().add_class('btn-secondary')
        btn.set_halign(Gtk.Align.START)
        btn.connect('clicked', lambda _: self.app._refresh_cloud())
        self._cloud_status_card.pack_start(btn, False, False, 0)

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
        # Retint widgets that bake colors into a per-widget CssProvider or
        # markup at construction time — the global provider swap above
        # doesn't reach those; without this they'd stay stale until the
        # next full data refresh (or forever, if not on the dashboard view).
        self._usage_5h.retint()
        self._usage_7d.retint()
        for bar in self._usage_model_bars.values():
            bar.retint()
        for card in self._summary_cards.values():
            card.retint()
        self.refresh_data()  # redraw everything else

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
        t = Gtk.Label(label="Cloud Integration")
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
        sb = Gtk.Button(label="Save Token")
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
        rt = Gtk.Label(label="Rate Card")
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

def _create_tray_icon(percentage: float, error: bool = False,
                      label: Optional[str] = None) -> str:
    if error:
        color = USAGE_COLORS['unknown']
        pct_text = label or "!"
    else:
        color = get_usage_color(percentage)
        pct_text = label if label is not None else str(int(percentage * 100))
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <rect width="24" height="24" rx="4" fill="{COLORS['surface']}" stroke="{COLORS['overlay']}" stroke-width="1"/>
  <circle cx="12" cy="12" r="8" fill="none" stroke="{COLORS['overlay']}" stroke-width="2"/>
  <circle cx="12" cy="12" r="8" fill="none" stroke="{color}" stroke-width="2"
    stroke-dasharray="{percentage * 50:.1f} 50" transform="rotate(-90 12 12)"/>
  <text x="12" y="15" text-anchor="middle" font-family="sans-serif" font-size="7"
    font-weight="bold" fill="{COLORS['text']}">{pct_text}</text>
</svg>'''
    icon_path = Path('/tmp') / APP_ID / 'tray_icon.svg'
    icon_path.parent.mkdir(exist_ok=True)
    icon_path.write_text(svg)
    return str(icon_path)


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

        self._notif_mgr = NotificationManager()
        self._settings = load_settings()
        self._startup_notified = False
        if HAS_NOTIFY:
            Notify.init(APP_NAME)

        self._indicator = None
        self._tray_items: Dict[str, Gtk.MenuItem] = {}
        self._tray_account_items: Dict[str, Gtk.MenuItem] = {}
        if HAS_INDICATOR:
            self._setup_tray()

        # Initial scan to ensure store is up to date even before window opens
        scan_into_store(self.store, load_rate_card(), accounts=self._accounts)

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

    @staticmethod
    def _bar(pct: float, width: int = 10) -> str:
        """Unicode block-segment progress bar (renders identically across
        every desktop because it's plain text)."""
        pct = max(0.0, min(1.0, pct))
        filled = int(round(pct * width))
        return '▰' * filled + '▱' * (width - filled)

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
        """Small themed panel with real progress bars (opened from the
        tray menu or a middle-click on the indicator icon)."""
        if getattr(self, '_usage_popup', None):
            self._close_usage_popup()
            return
        st = self._primary_state()

        # Gtk.WindowType.POPUP (override-redirect) + an explicit pointer
        # grab, rather than a TOPLEVEL window relying on focus-out: WM/
        # compositor focus-out timing is unreliable (Wayland can fire it
        # right at map time), which previously required a grace-period
        # hack that just moved the bug around (missed dismiss clicks,
        # broken re-toggle from the tray menu). A grab we own ourselves
        # dismisses deterministically on any click outside the window.
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_resizable(False)
        win.set_default_size(380, -1)
        win.get_style_context().add_class('usage-popup')

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(outer, f'set_margin_{side}')(20)
        win.add(outer)

        title = Gtk.Label(label='Claude Usage')
        title.get_style_context().add_class('usage-popup-title')
        outer.pack_start(title, False, False, 0)

        def _pb_class(frac: Optional[float]) -> str:
            if frac is None:
                return 'pb-unknown'
            if frac < 0.5:
                return 'pb-low'
            if frac < 0.75:
                return 'pb-medium'
            if frac < 0.9:
                return 'pb-high'
            return 'pb-critical'

        def section(name: str, pct: Optional[int], reset_iso, window: str):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            lab = Gtk.Label(label=name)
            lab.get_style_context().add_class('usage-popup-label')
            lab.set_halign(Gtk.Align.START)
            row.pack_start(lab, True, True, 0)
            reset = Gtk.Label(label='Resets ' + fmt_reset_absolute(
                reset_iso, with_weekday=(window == '7d')))
            reset.get_style_context().add_class('usage-popup-sub')
            reset.set_halign(Gtk.Align.END)
            row.pack_end(reset, False, False, 0)
            box.pack_start(row, False, False, 0)

            frac = None if pct is None else max(0.0, min(1.0, pct / 100.0))
            pb = Gtk.ProgressBar()
            pb.set_fraction(frac or 0.0)
            pb.get_style_context().add_class(_pb_class(frac))
            box.pack_start(pb, False, False, 0)

            used = Gtk.Label(
                label='?' if pct is None else f'{pct}% used')
            used.get_style_context().add_class('usage-popup-sub')
            box.pack_start(used, False, False, 0)
            return box

        outer.pack_start(section('Session (5 hour)', st.last_pct5,
                                 st.last_resets_5h, '5h'), False, False, 0)
        outer.pack_start(section('Weekly (7 day)', st.last_pct7,
                                 st.last_resets_7d, '7d'), False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # Only the account's actual error states count as "bad" — 'idle'
        # (no fetch yet) and 'loading' (fetch in flight) are normal,
        # transient states, not failures, and shouldn't render as red.
        error_states = {'auth_error', 'network_error', 'rate_limited',
                        'no_token'}
        state = st.cloud_state if st.token else 'no_token'
        is_error = state in error_states
        ok = state == 'ok'
        dot = Gtk.Label()
        color = USAGE_COLORS['critical' if is_error else 'low']
        dot.set_markup(f'<span foreground="{color}">●</span>')
        status_row.pack_start(dot, False, False, 0)
        if ok:
            status_txt = 'Cloud connection OK'
        elif is_error:
            status_txt = state.replace('_', ' ')
        else:
            status_txt = 'Checking…' if state == 'loading' else 'Waiting for first update'
        stl = Gtk.Label(label=status_txt)
        stl.get_style_context().add_class('usage-popup-label')
        status_row.pack_start(stl, False, False, 0)
        outer.pack_start(status_row, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        upd = Gtk.Label(
            label='Last updated: ' + datetime.now().strftime('%H:%M'))
        upd.get_style_context().add_class('usage-popup-sub')
        upd.set_halign(Gtk.Align.START)
        bottom.pack_start(upd, True, True, 0)
        rb = Gtk.Button(label='Refresh')
        rb.get_style_context().add_class('btn-secondary')

        def _do_refresh(_b):
            self._close_usage_popup()
            self._refresh_cloud()
        rb.connect('clicked', _do_refresh)
        bottom.pack_end(rb, False, False, 0)
        outer.pack_start(bottom, False, False, 0)

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
        win.present()
        self._usage_popup = win

        seat = Gdk.Display.get_default().get_default_seat()
        seat.grab(win.get_window(), Gdk.SeatCapabilities.ALL, True,
                  None, None, None, None)

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
        for k in ('5h', '7d'):
            it = self._limit_menu_item()
            menu.append(it)
            self._tray_items[k] = it
        # Per-model weekly caps (e.g. a separate Opus limit on Max plans).
        # Rows are dynamic: the menu is rebuilt when the model set changes
        # (see _rebuild_tray), so build from the primary account's cache.
        self._tray_model_names = [m['model'] for m
                                  in self._primary_state().last_model_limits]
        for name in self._tray_model_names:
            it = self._limit_menu_item()
            menu.append(it)
            self._tray_items[f'model:{name}'] = it

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
        model_names = [m['model'] for m
                       in self._primary_state().last_model_limits]
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

        # ── LIMITS section (primary account drives this) ──────────────────
        primary = self._primary_state()
        self._update_tray_limits(primary)

        # ── ACCOUNTS section ──────────────────────────────────────────────
        for acc in self._accounts:
            it = self._tray_account_items.get(acc.label)
            if it is None:
                continue
            st = self._account_states[acc.label]
            it.set_label(self._tray_account_line(st))

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

        if st.cloud_state in ('auth_error', 'network_error', 'rate_limited',
                              'no_token'):
            hint = st.cloud_state.replace('_', ' ')
            _set('5h', f"5h  {self._emoji_bar(None)}  {hint}")
            _set('7d', f"7d  {self._emoji_bar(None)}  {hint}")
            for m in st.last_model_limits:
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
        _set('7d', f"7d  {b7}  {p7} · ↻ {r7}")
        # Per-model weekly caps. ● flags the one currently binding your usage.
        for m in st.last_model_limits:
            bar = self._emoji_bar(m['fraction'])
            rr = self._fmt_reset(m['resets_at'], st.account, window='7d')
            marker = ' ●' if m['is_active'] else ''
            _set(f"model:{m['model']}",
                 f"   {m['model']}  {bar}  {m['pct']}%{marker} · ↻ {rr}")

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
            rows = self.store.query(
                since=datetime.now(timezone.utc) - timedelta(hours=24))
            blocks = compute_blocks(rows)
            if blocks and blocks[-1].is_active():
                b = blocks[-1]
                rem_s = b.remaining().total_seconds()
                remaining = fmt_duration(rem_s)
                elapsed_frac = 1.0 - rem_s / (BLOCK_HOURS * 3600.0)
                bar = self._emoji_bar(elapsed_frac)
                self._tray_items['block'].set_label(
                    f"⏳  {bar}  {remaining} left")
                self._tray_items['block_tokens'].set_label(
                    f"🪙  {fmt(b.total_tokens)} tokens")
                self._tray_items['block_cost'].set_label(
                    f"💸  {fmt_cost(b.cost_usd)} spent")
            else:
                self._tray_items['block'].set_label(
                    f"⏳  {self._emoji_bar(None)}  idle")
                self._tray_items['block_tokens'].set_label('🪙  —')
                self._tray_items['block_cost'].set_label('💸  —')
        except Exception as e:
            print(f"[{APP_ID}] tray block update: {e}", file=sys.stderr)
        return False

    def _local_loop(self):
        rc = load_rate_card()
        while self._running:
            try:
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
                try:
                    data = fetch_cloud_usage(state.token)
                    GLib.idle_add(self._on_cloud_data, data, acc.label)
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
                       account_label: str = DEFAULT_ACCOUNT_LABEL):
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
            seven = data.get('seven_day') or {}
            u5 = normalize_utilization(five.get('utilization', 0))
            u7 = normalize_utilization(seven.get('utilization', 0))
            pct5 = int(u5 * 100); pct7 = int(u7 * 100)
            r5 = five.get('resets_at')
            r7 = seven.get('resets_at')

            # If the resets_at shifted while we had no fresh data, the
            # window has rolled over — but the API has now returned new
            # numbers so clear any "?" awaiting flag.
            st.last_pct5, st.last_pct7 = pct5, pct7
            st.last_resets_5h, st.last_resets_7d = r5, r7
            st.last_model_limits = extract_model_limits(data)

            if account_label == self._primary_account().label:
                if HAS_NOTIFY:
                    self._notify_thresholds(pct5, pct7, max(u5, u7),
                                            usage_data=data,
                                            account=account_label)
                    self._check_budgets()
            elif HAS_NOTIFY:
                # Threshold notifications fire for every account, but only
                # the primary triggers budget evaluation (budgets are
                # global today; per-account scope lands as a follow-up).
                self._notify_thresholds(pct5, pct7, max(u5, u7),
                                        usage_data=data,
                                        account=account_label)
        self._sync_primary()
        self._rebuild_tray()
        self._update_tray_block()
        if self.window:
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
