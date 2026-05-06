"""
Shared utilities for Claude Usage Widget.
Colors, utilization parsing, and time formatting.
"""

from datetime import datetime, timezone

# ── Colors ──────────────────────────────────────────────────────────────────

COLOR_GREEN = "#22c55e"
COLOR_YELLOW = "#eab308"
COLOR_RED = "#ef4444"
COLOR_GRAY = "#6b7280"

DEFAULT_THRESHOLDS = {"warn": 60, "critical": 85}


def get_color_for_pct(pct: float, thresholds: dict | None = None) -> str:
    """Return color based on percentage (0-100 scale)."""
    warn = (thresholds or DEFAULT_THRESHOLDS).get("warn", 60)
    critical = (thresholds or DEFAULT_THRESHOLDS).get("critical", 85)
    if pct < warn:
        return COLOR_GREEN
    elif pct < critical:
        return COLOR_YELLOW
    else:
        return COLOR_RED


def hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def parse_utilization(raw: float) -> tuple[int, float]:
    """Return (percentage_int, decimal_0_to_1) from API utilization value."""
    if raw >= 1:  # Already a percentage (API returns 0-100 scale; 1.0 means 1%)
        return int(raw), raw / 100
    return int(raw * 100), raw


def compute_burn_rate(seven: dict) -> float | None:
    """Return the 7d burn rate multiplier, or None if the window is too new."""
    resets_at_str = seven.get("resets_at")
    if not resets_at_str:
        return None
    pct7, _ = parse_utilization(seven.get("utilization", 0))
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        window_secs = 7 * 24 * 3600
        elapsed_secs = window_secs - (resets_at - now).total_seconds()
        if elapsed_secs < 0.05 * window_secs:  # ignore first ~8h
            return None
        return (pct7 / 100) / (elapsed_secs / window_secs)
    except Exception:
        return None


def format_reset_clock(iso_str: str | None) -> str:
    """Format reset time as a clock time, e.g. '9:00P'. Used when auto-poll is off."""
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return reset_dt.strftime("%-I:%M") + ("A" if reset_dt.hour < 12 else "P")
    except Exception:
        return "unknown"


def format_reset_clock_7d(iso_str: str | None) -> str:
    """Format reset time as day + clock, e.g. 'Th 7:00P'. Used for 7d window when auto-poll is off."""
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        day = reset_dt.strftime("%a")[:2]  # "Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"
        time_str = reset_dt.strftime("%-I:%M") + ("A" if reset_dt.hour < 12 else "P")
        return f"{day} {time_str}"
    except Exception:
        return "unknown"


def format_reset_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        total_sec = int((reset_dt - datetime.now(timezone.utc)).total_seconds())
        if total_sec <= 0:
            return "any moment"
        days, remainder = divmod(total_sec, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return iso_str
