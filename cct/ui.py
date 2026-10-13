"""Design system and drawn components for the GTK dashboard.

The look follows a warm editorial system: a cream canvas with near-black ink in
light mode, a warm charcoal ladder in dark mode, one coral accent for the
interface, a serif for page titles and a sans for everything you read or
compare. Usage itself is coloured by state, not by brand: teal while there is
room, amber past the warning threshold, red past the critical one. Those three
were checked for colour-blind separation and contrast against both surfaces
(dataviz validator, 13 Oct 2026), which is why they are not the coral.

Everything drawn here (rings, meters, the daily chart, the navigation icons,
the tray icon) uses Cairo, so it looks the same on every GTK theme.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import random
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402
import cairo  # noqa: E402

# ── Palettes ────────────────────────────────────────────────────────────────
# Keys kept from the old palette ('bg', 'surface', 'overlay', 'text', ...) so
# every existing reference keeps working; the new ones describe the surface
# ladder and the usage states.

LIGHT = {
    'bg':        '#FAF9F5',   # canvas
    'shell':     '#F1ECE3',   # outer bezel of a panel
    'surface':   '#FFFEFB',   # inner core of a panel
    'sunken':    '#F5F0E8',   # wells, table stripes, inputs
    'overlay':   '#EBE6DF',   # hover fills
    'hairline':  '#E3DCD1',
    'text':      '#141413',
    'subtext':   '#3D3D3A',
    'muted':     '#6C6A64',
    'faint':     '#8E8B82',
    'accent':    '#CC785C',   # coral: interface accent
    'accent-strong': '#A9583E',
    'accent-soft': 'rgba(204, 120, 92, 0.12)',
    'on-accent': '#FFFFFF',
    'ok':        '#178F76',   # usage states, validated as a set
    'warn':      '#B8860B',
    'danger':    '#C4403F',
    'chart':     '#CC785C',
    'grid':      'rgba(20, 20, 19, 0.07)',
    'shadow':    'rgba(80, 60, 30, 0.08)',
    'highlight': 'rgba(255, 255, 255, 0.85)',
    'grain':     (0.25, 0.18, 0.10, 0.035),
    # legacy names still used by older code paths
    'green':     '#178F76', 'yellow': '#B8860B', 'red': '#C4403F',
    'blue':      '#3D6FB4', 'orange': '#CC785C',
}

DARK = {
    'bg':        '#181715',
    'shell':     '#1F1E1B',
    'surface':   '#252320',
    'sunken':    '#1C1B19',
    'overlay':   '#2E2C28',
    'hairline':  '#34322D',
    'text':      '#FAF9F5',
    'subtext':   '#D6D2C9',
    'muted':     '#A09D96',
    'faint':     '#7C7972',
    'accent':    '#D98B70',
    'accent-strong': '#E8A58C',
    'accent-soft': 'rgba(217, 139, 112, 0.14)',
    'on-accent': '#1A1310',
    'ok':        '#26A085',
    'warn':      '#BB8A10',
    'danger':    '#D0466A',
    'chart':     '#D98B70',
    'grid':      'rgba(250, 249, 245, 0.07)',
    'shadow':    'rgba(0, 0, 0, 0.35)',
    'highlight': 'rgba(255, 255, 255, 0.05)',
    'grain':     (1.0, 0.95, 0.85, 0.022),
    'green':     '#26A085', 'yellow': '#BB8A10', 'red': '#D0466A',
    'blue':      '#7FA3D6', 'orange': '#D98B70',
}

# The live palette. Updated in place on a theme switch, so anything that reads
# it at draw time follows the theme without being rebuilt.
P: dict = dict(LIGHT)

# Usage thresholds in percent; the dashboard keeps them in step with Settings.
THRESHOLDS = {'warn': 60.0, 'critical': 85.0}

SANS = 'Geist'
MONO = 'Geist Mono'
SERIF = 'Instrument Serif'


def use_palette(dark: bool) -> None:
    P.clear()
    P.update(DARK if dark else LIGHT)


def is_dark() -> bool:
    return P.get('bg') == DARK['bg']


def level(pct: Optional[float]) -> str:
    """'ok', 'warn' or 'danger' for a usage percentage; 'none' when unknown."""
    if pct is None:
        return 'none'
    if pct >= THRESHOLDS['critical']:
        return 'danger'
    if pct >= THRESHOLDS['warn']:
        return 'warn'
    return 'ok'


def level_color(pct: Optional[float]) -> str:
    lv = level(pct)
    return P['faint'] if lv == 'none' else P[lv]


def level_word(pct: Optional[float]) -> str:
    """The state in words, so colour never carries it alone."""
    return {'ok': 'room left', 'warn': 'getting close',
            'danger': 'near the limit', 'none': 'no reading'}[level(pct)]


def rgba(color: str, alpha: Optional[float] = None) -> Tuple[float, float, float, float]:
    """A CSS colour ('#rrggbb' or 'rgba(r, g, b, a)') as Cairo floats."""
    c = color.strip()
    if c.startswith('#'):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        a = 1.0
    else:
        inner = c[c.index('(') + 1:c.index(')')].split(',')
        r, g, b = (float(x) / 255 for x in inner[:3])
        a = float(inner[3]) if len(inner) > 3 else 1.0
    return (r, g, b, a if alpha is None else alpha)


# ── Fonts ───────────────────────────────────────────────────────────────────

def register_fonts(asset_dirs: Sequence[Path]) -> List[str]:
    """Make the bundled fonts (OFL: Geist, Geist Mono, Instrument Serif)
    available to this process through fontconfig. Call before any widget is
    created. Returns the files registered; an empty list just means the
    system fonts are used."""
    lib = ctypes.util.find_library('fontconfig')
    if not lib:
        return []
    try:
        fc = ctypes.CDLL(lib)
        fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        fc.FcConfigAppFontAddFile.restype = ctypes.c_int
    except (OSError, AttributeError):
        return []
    done = []
    for d in asset_dirs:
        fonts = Path(d) / 'fonts'
        if not fonts.is_dir():
            continue
        for f in sorted(fonts.glob('*.ttf')):
            if fc.FcConfigAppFontAddFile(None, str(f).encode()):
                done.append(f.name)
        if done:
            break
    return done


def font(family: str = SANS, px: float = 13, weight: int = 400) -> Pango.FontDescription:
    d = Pango.FontDescription()
    d.set_family(family)
    d.set_absolute_size(px * Pango.SCALE)
    # Pango only takes its named weights; snap to the nearest one.
    named = (100, 200, 300, 350, 380, 400, 500, 600, 700, 800, 900)
    d.set_weight(Pango.Weight(min(named, key=lambda w: abs(w - weight))))
    return d


def draw_text(cr, text: str, x: float, y: float, desc: Pango.FontDescription,
              color: str, align: str = 'left', valign: str = 'top',
              tabular: bool = False) -> Tuple[float, float]:
    """Draw ``text`` with Pango; returns its (width, height) in px."""
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(desc)
    if tabular:
        attrs = Pango.AttrList()
        try:
            attrs.insert(Pango.attr_font_features_new('tnum 1'))
            layout.set_attributes(attrs)
        except (AttributeError, TypeError):
            pass
    layout.set_text(text, -1)
    w, h = (v / Pango.SCALE for v in layout.get_size())
    if align == 'center':
        x -= w / 2
    elif align == 'right':
        x -= w
    if valign == 'middle':
        y -= h / 2
    elif valign == 'bottom':
        y -= h
    cr.set_source_rgba(*rgba(color))
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return w, h


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# ── Motion ──────────────────────────────────────────────────────────────────

def ease(t: float) -> float:
    """A heavy ease-out, close to cubic-bezier(0.32, 0.72, 0, 1)."""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 4


class Tween:
    """Animates a number toward a target on the widget's frame clock."""

    DURATION_US = 650_000

    def __init__(self, widget: Gtk.Widget, value: float = 0.0):
        self.widget = widget
        self.value = value
        self._from = value
        self._to = value
        self._start = None
        self._tick = None

    def to(self, target: float, animate: bool = True) -> None:
        if abs(target - self._to) < 1e-6 and self._tick is None:
            self.value = target
            return
        self._from, self._to = self.value, target
        if not animate or not self.widget.get_mapped():
            self.value = target
            self.widget.queue_draw()
            return
        self._start = None
        if self._tick is None:
            self._tick = self.widget.add_tick_callback(self._step)

    def _step(self, widget, clock):
        now = clock.get_frame_time()
        if self._start is None:
            self._start = now
        t = (now - self._start) / self.DURATION_US
        self.value = self._from + (self._to - self._from) * ease(t)
        widget.queue_draw()
        if t >= 1:
            self.value = self._to
            self._tick = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE


# ── Ring gauge ──────────────────────────────────────────────────────────────

class Ring(Gtk.DrawingArea):
    """A usage ring: the arc is the share of the window used, coloured by state,
    over a track that is a lighter step of the same colour. The number in the
    middle is the figure itself, so the ring never relies on colour alone."""

    def __init__(self, diameter: int = 168, stroke: Optional[float] = None,
                 caption: str = ''):
        super().__init__()
        self.d = diameter
        self.stroke = stroke or max(6.0, diameter * 0.075)
        self.caption = caption
        self.pct: Optional[float] = None
        self.dimmed = False
        self._tween = Tween(self, 0.0)
        self.set_size_request(diameter, diameter)
        self.connect('draw', self._draw)

    def set_value(self, pct: Optional[float], caption: Optional[str] = None,
                  dimmed: bool = False) -> None:
        self.pct = pct
        self.dimmed = dimmed
        if caption is not None:
            self.caption = caption
        self._tween.to(min(max(pct or 0.0, 0.0), 100.0))
        self.queue_draw()

    def _draw(self, _w, cr):
        alloc = self.get_allocation()
        size = min(alloc.width, alloc.height)
        cx, cy = alloc.width / 2, alloc.height / 2
        r = size / 2 - self.stroke / 2 - 1
        color = level_color(self.pct) if not self.dimmed else P['faint']
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(self.stroke)
        cr.set_source_rgba(*rgba(color, 0.16 if not is_dark() else 0.22))
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        frac = self._tween.value / 100.0
        if self.pct is not None and frac > 0.002:
            start = -math.pi / 2
            cr.set_source_rgba(*rgba(color))
            cr.arc(cx, cy, r, start, start + 2 * math.pi * min(frac, 0.9999))
            cr.stroke()
        big = size * 0.25
        if self.pct is None:
            draw_text(cr, '—', cx, cy, font(SANS, big, 500), P['faint'],
                      'center', 'middle')
            return False
        shown = f"{round(self._tween.value)}"
        num = font(SANS, big, 600)
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(num)
        layout.set_text(shown, -1)
        nw, nh = (v / Pango.SCALE for v in layout.get_size())
        pct_desc = font(SANS, big * 0.42, 500)
        pl = PangoCairo.create_layout(cr)
        pl.set_font_description(pct_desc)
        pl.set_text('%', -1)
        pw, ph = (v / Pango.SCALE for v in pl.get_size())
        total = nw + pw + 1
        top = cy - nh / 2 - (size * 0.05 if self.caption else 0)
        cr.set_source_rgba(*rgba(P['text'] if not self.dimmed else P['muted']))
        cr.move_to(cx - total / 2, top)
        PangoCairo.show_layout(cr, layout)
        cr.set_source_rgba(*rgba(P['muted']))
        cr.move_to(cx - total / 2 + nw + 1, top + nh * 0.18)
        PangoCairo.show_layout(cr, pl)
        if self.caption:
            draw_text(cr, self.caption, cx, top + nh - big * 0.08,
                      font(SANS, max(10.0, size * 0.068), 500), P['muted'],
                      'center')
        return False


# ── Meter ───────────────────────────────────────────────────────────────────

class Meter(Gtk.DrawingArea):
    """A thin horizontal usage bar with a rounded end, coloured by state.
    ``color`` overrides the state colour for bars that are not usage (a
    share of tokens, a session's elapsed time)."""

    def __init__(self, height: int = 6, color: Optional[str] = None,
                 width: int = -1):
        super().__init__()
        self.h = height
        self.color = color
        self.pct: Optional[float] = None
        self._tween = Tween(self, 0.0)
        self.set_size_request(width, height)
        self.set_valign(Gtk.Align.CENTER)
        self.connect('draw', self._draw)

    def set_value(self, pct: Optional[float], color: Optional[str] = None) -> None:
        self.pct = pct
        if color is not None:
            self.color = color
        self._tween.to(min(max(pct or 0.0, 0.0), 100.0))

    def _draw(self, _w, cr):
        a = self.get_allocation()
        h = min(self.h, a.height)
        y = (a.height - h) / 2
        base = (P[self.color] if self.color in P else self.color) if self.color \
            else level_color(self.pct)
        cr.set_source_rgba(*rgba(base, 0.15 if not is_dark() else 0.22))
        rounded_rect(cr, 0, y, a.width, h, h / 2)
        cr.fill()
        frac = self._tween.value / 100.0
        if self.pct is not None and frac > 0:
            w = max(h, a.width * min(frac, 1.0))
            cr.set_source_rgba(*rgba(base))
            rounded_rect(cr, 0, y, w, h, h / 2)
            cr.fill()
        return False


# ── Daily usage chart ───────────────────────────────────────────────────────

class DailyBars(Gtk.DrawingArea):
    """One bar per day, one series (so no legend: the card title names it).
    Bars sit on the baseline with rounded tops and a 2px gap; a hovered bar
    stays full strength while the rest step back, and the tooltip gives the
    exact figures. Gridlines are recessive; the max is labelled once."""

    def __init__(self, height: int = 196,
                 fmt_value: Callable[[float], str] = str,
                 tooltip: Optional[Callable[[dict], str]] = None):
        super().__init__()
        self.rows: List[dict] = []
        self.key = 'total_tokens'
        self.fmt_value = fmt_value
        self.tooltip_fn = tooltip
        self.hover: Optional[int] = None
        self._tween = Tween(self, 0.0)
        self.set_size_request(-1, height)
        self.set_has_tooltip(True)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('draw', self._draw)
        self.connect('motion-notify-event', self._motion)
        self.connect('leave-notify-event', self._leave)
        self.connect('query-tooltip', self._query_tooltip)
        self._geom: List[Tuple[float, float]] = []

    def set_rows(self, rows: List[dict], key: str = 'total_tokens') -> None:
        changed = (key != self.key
                   or [r.get(key) for r in rows] != [r.get(key) for r in self.rows])
        self.rows, self.key = rows, key
        if changed:
            self._tween.value = 0.0
            self._tween.to(1.0)
        self.queue_draw()

    def _plot(self):
        a = self.get_allocation()
        left, right, top, bottom = 4, 4, 22, 26
        return left, top, a.width - left - right, a.height - top - bottom

    def _draw(self, _w, cr):
        x0, y0, pw, ph = self._plot()
        vals = [float(r.get(self.key) or 0) for r in self.rows]
        vmax = max(vals) if vals else 0
        small = font(SANS, 11, 450)
        # recessive gridlines at a third and two thirds, plus the baseline
        cr.set_line_width(1)
        for i in (1, 2, 3):
            yy = round(y0 + ph - ph * i / 3) + 0.5
            cr.set_source_rgba(*rgba(P['grid']))
            cr.move_to(x0, yy)
            cr.line_to(x0 + pw, yy)
            cr.stroke()
        cr.set_source_rgba(*rgba(P['hairline']))
        cr.move_to(x0, y0 + ph + 0.5)
        cr.line_to(x0 + pw, y0 + ph + 0.5)
        cr.stroke()
        if not vals or vmax <= 0:
            draw_text(cr, 'No usage in this period yet', x0 + pw / 2, y0 + ph / 2,
                      font(SANS, 13, 500), P['faint'], 'center', 'middle')
            self._geom = []
            return False
        draw_text(cr, self.fmt_value(vmax), x0, 2, small, P['faint'])
        n = len(vals)
        gap = 2.0 if n > 40 else 3.0
        bw = max(2.0, (pw - gap * (n - 1)) / n)
        grow = self._tween.value
        self._geom = []
        for i, v in enumerate(vals):
            bx = x0 + i * (bw + gap)
            bh = 0 if vmax <= 0 else ph * (v / vmax) * grow
            self._geom.append((bx, bx + bw))
            if bh <= 0:
                continue
            bh = max(bh, 2.0)
            strong = self.hover is None or self.hover == i
            cr.set_source_rgba(*rgba(P['chart'], 1.0 if strong else 0.38))
            r = min(4.0, bw / 2, bh)
            top_y = y0 + ph - bh
            cr.new_sub_path()
            cr.move_to(bx, y0 + ph)
            cr.line_to(bx, top_y + r)
            cr.arc(bx + r, top_y + r, r, math.pi, 3 * math.pi / 2)
            cr.arc(bx + bw - r, top_y + r, r, 3 * math.pi / 2, 2 * math.pi)
            cr.line_to(bx + bw, y0 + ph)
            cr.close_path()
            cr.fill()
        # date ticks: first, middle, last
        for i in sorted({0, n // 2, n - 1}):
            d = self.rows[i].get('day')
            label = _short_day(d)
            bx0, bx1 = self._geom[i]
            align = 'left' if i == 0 else ('right' if i == n - 1 else 'center')
            x = bx0 if align == 'left' else (bx1 if align == 'right' else (bx0 + bx1) / 2)
            draw_text(cr, label, x, y0 + ph + 8, small, P['faint'], align)
        return False

    def _index_at(self, x: float) -> Optional[int]:
        for i, (a, b) in enumerate(self._geom):
            if a - 1.5 <= x <= b + 1.5:
                return i
        return None

    def _motion(self, _w, ev):
        i = self._index_at(ev.x)
        if i != self.hover:
            self.hover = i
            self.queue_draw()
            self.trigger_tooltip_query()
        return False

    def _leave(self, *_a):
        if self.hover is not None:
            self.hover = None
            self.queue_draw()
        return False

    def _query_tooltip(self, _w, x, _y, _kb, tip):
        i = self._index_at(x)
        if i is None or not self.tooltip_fn:
            return False
        tip.set_markup(self.tooltip_fn(self.rows[i]))
        return True


def _short_day(day: Optional[str]) -> str:
    try:
        d = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return str(day or '')
    return f"{d.strftime('%b')} {d.day}"


# ── Navigation icons ────────────────────────────────────────────────────────

class NavIcon(Gtk.DrawingArea):
    """An 18px line icon drawn with a 1.5px stroke in the label's colour."""

    def __init__(self, kind: str, size: int = 18):
        super().__init__()
        self.kind = kind
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.connect('draw', self._draw)

    def _draw(self, _w, cr):
        a = self.get_allocation()
        s = min(a.width, a.height)
        ctx = self.get_style_context()
        c = ctx.get_color(ctx.get_state())
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
        cr.set_line_width(1.5)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        u = s / 18.0
        cr.translate((a.width - s) / 2, (a.height - s) / 2)
        cr.scale(u, u)
        k = self.kind
        if k == 'dashboard':
            for (x, y, w, h) in ((2.75, 2.75, 5.5, 7), (9.75, 2.75, 5.5, 4),
                                 (9.75, 8.25, 5.5, 7), (2.75, 11.25, 5.5, 4)):
                rounded_rect(cr, x, y, w, h, 1.4)
                cr.stroke()
        elif k == 'projects':
            cr.move_to(2.75, 5.5)
            cr.line_to(2.75, 14)
            cr.curve_to(2.75, 14.7, 3.3, 15.25, 4, 15.25)
            cr.line_to(14, 15.25)
            cr.curve_to(14.7, 15.25, 15.25, 14.7, 15.25, 14)
            cr.line_to(15.25, 7.25)
            cr.curve_to(15.25, 6.55, 14.7, 6, 14, 6)
            cr.line_to(9, 6)
            cr.line_to(7.4, 3.6)
            cr.line_to(4, 3.6)
            cr.curve_to(3.3, 3.6, 2.75, 4.2, 2.75, 5.5)
            cr.stroke()
        elif k == 'breakdowns':
            for x, h in ((4, 6), (9, 11), (14, 8.5)):
                cr.move_to(x, 15.25)
                cr.line_to(x, 15.25 - h)
                cr.stroke()
        elif k == 'budgets':
            cr.arc(9, 9, 6.25, 0, 2 * math.pi)
            cr.stroke()
            cr.arc(9, 9, 2.5, 0, 2 * math.pi)
            cr.stroke()
        elif k == 'settings':
            for y, kx in ((5, 11.5), (9, 6), (13, 10)):
                cr.move_to(2.75, y)
                cr.line_to(15.25, y)
                cr.stroke()
                cr.arc(kx, y, 1.7, 0, 2 * math.pi)
                cr.save()
                bg = rgba(P['shell'])
                cr.set_source_rgba(*bg)
                cr.fill_preserve()
                cr.restore()
                cr.stroke()
        elif k == 'refresh':
            cr.arc(9, 9, 5.75, -0.35 * math.pi, 1.45 * math.pi)
            cr.stroke()
            cr.move_to(12.6, 2.6)
            cr.line_to(12.9, 5.0)
            cr.line_to(10.5, 5.3)
            cr.stroke()
        elif k == 'arrow':
            cr.move_to(4, 9)
            cr.line_to(14, 9)
            cr.move_to(10, 5)
            cr.line_to(14, 9)
            cr.line_to(10, 13)
            cr.stroke()
        return False


# ── Texture ─────────────────────────────────────────────────────────────────

def grain_png(cache_dir: Path) -> Optional[Path]:
    """A small tileable grain texture for the canvas, written once per theme."""
    r, g, b, a = P['grain']
    name = f"grain-{'dark' if is_dark() else 'light'}-v1.png"
    path = Path(cache_dir) / name
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 160
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surf)
        rnd = random.Random(7)
        for _ in range(size * size // 3):
            x, y = rnd.randrange(size), rnd.randrange(size)
            cr.set_source_rgba(r, g, b, a * rnd.uniform(0.4, 1.0))
            cr.rectangle(x, y, 1, 1)
            cr.fill()
        surf.write_to_png(str(path))
        return path
    except (OSError, cairo.Error):
        return None


# ── Tray icon ───────────────────────────────────────────────────────────────

def tray_icon_png(cache_dir: Path, pct: Optional[float], state: str = 'ok',
                  size: int = 44) -> Optional[Path]:
    """Render the tray icon: a ring showing the 5-hour window's use.

    The top bar is dark on most desktops, so the track is a translucent white
    and the arc takes the state colour (``state``: 'ok', 'warn', 'danger',
    'error' or 'none'). An error draws a hollow ring with a gap, so a broken
    connection never looks like an empty window.
    The file name changes with the figure because AppIndicator only reloads
    an icon whose path changed.
    """
    colors = {'ok': DARK['ok'], 'warn': '#E0A526', 'danger': '#F06A7E',
              'error': '#A09D96', 'none': '#A09D96'}
    key = 'na' if pct is None else str(int(round(min(max(pct, 0), 100))))
    path = Path(cache_dir) / f"tray-{state}-{key}.png"
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surf)
        c = size / 2
        w = size * 0.16
        r = c - w / 2 - size * 0.04
        cr.set_line_width(w)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(1, 1, 1, 0.28)
        if state == 'error':
            cr.arc(c, c, r, -math.pi / 2 + 0.6, -math.pi / 2 + 2 * math.pi - 0.6)
        else:
            cr.arc(c, c, r, 0, 2 * math.pi)
        cr.stroke()
        if pct is not None and state != 'error':
            frac = min(max(pct, 0.0), 100.0) / 100.0
            if frac > 0:
                cr.set_source_rgba(*rgba(colors.get(state, colors['ok'])))
                start = -math.pi / 2
                cr.arc(c, c, r, start, start + 2 * math.pi * max(frac, 0.03))
                cr.stroke()
        else:
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.arc(c, c, size * 0.07, 0, 2 * math.pi)
            cr.fill()
        surf.write_to_png(str(path))
        return path
    except (OSError, cairo.Error):
        return None
