"""Per-window, per-account notification escalation.

Mirrors the behaviour of `claude_ai_usage_widget`: each window (5h / 7d)
escalates through tiers (warn → critical → max) at most once per window. State
is persisted to ``~/.config/claude-token-tracker/notification_state.json`` so a
restart mid-window does not re-fire alerts that already fired. When a window
rolls over (its ``resets_at`` shifts), the escalation chain re-arms.

Burn-rate alerts (7d only) follow the same escalation but skip the first
``BURN_RATE_GRACE_HOURS`` of a new window, where elapsed time is too small to
draw reliable conclusions.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from .config import (BURN_RATE_GRACE_HOURS, DEFAULT_BURN_RATE,
                     DEFAULT_THRESHOLDS, NOTIFICATION_STATE_FILE,
                     write_private_file)

# Threshold escalation levels (5h or 7d usage %).
LEVEL_NONE = 0
LEVEL_WARN = 1
LEVEL_CRITICAL = 2
LEVEL_MAX = 3

# Burn-rate escalation levels (7d only).
BURN_NONE = 0
BURN_EARLY = 1     # fires when pct_used < warn but pace exceeds multiplier
BURN_WARN = 2      # fires once pct_used reaches warn
BURN_CRITICAL = 3  # fires once pct_used reaches critical

# Drift tolerated before treating a `resets_at` change as a new window.
ROLLOVER_TOLERANCE_5H_SECONDS = 3600       # 1 hour
ROLLOVER_TOLERANCE_7D_SECONDS = 6 * 3600   # 6 hours

WINDOW_TOTAL_SECONDS = {
    '5h': 5 * 3600,
    '7d': 7 * 24 * 3600,
}


@dataclass
class WindowSnapshot:
    """Normalized view of one cloud-API usage payload for one account."""
    five_hour_pct: Optional[float] = None       # 0..100 or None when unknown
    five_hour_resets_at: Optional[str] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets_at: Optional[str] = None

    @classmethod
    def from_cloud_data(cls, data: Optional[dict]) -> 'WindowSnapshot':
        if not data:
            return cls()
        fh = data.get('five_hour') or {}
        sd = data.get('seven_day') or {}
        return cls(
            five_hour_pct=_pct(fh.get('utilization')),
            five_hour_resets_at=fh.get('resets_at'),
            seven_day_pct=_pct(sd.get('utilization')),
            seven_day_resets_at=sd.get('resets_at'),
        )


@dataclass
class Notification:
    account: str
    kind: str          # 'threshold' or 'burn_rate'
    window: str        # '5h' or '7d'
    level: str         # 'warn' / 'critical' / 'max' / 'early'
    urgency: str       # 'normal' or 'critical'
    title: str
    body: str


def _pct(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # Utilization from the usage API is already a percentage (0-100); use it
    # directly. The old `if v <= 1: v *= 100` heuristic mis-scaled genuine
    # sub-1% usage into 100% (issue #4).
    return max(0.0, min(v, 999.0))


def _parse_iso(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_rollover(prev: Optional[str], curr: Optional[str],
                 tolerance_seconds: int) -> bool:
    """A window has rolled over when its ``resets_at`` shifts by more than
    ``tolerance_seconds``. Small drift (clock skew, server jitter) does not
    count."""
    if not curr:
        return False
    if not prev:
        return True
    p = _parse_iso(prev)
    c = _parse_iso(curr)
    if p is None or c is None:
        return prev != curr
    return abs((c - p).total_seconds()) > tolerance_seconds


def _threshold_level(pct: float, warn: float, critical: float) -> int:
    if pct >= 100:
        return LEVEL_MAX
    if pct >= critical:
        return LEVEL_CRITICAL
    if pct >= warn:
        return LEVEL_WARN
    return LEVEL_NONE


def _level_name(level: int) -> str:
    return {LEVEL_WARN: 'warn', LEVEL_CRITICAL: 'critical',
            LEVEL_MAX: 'max'}.get(level, 'none')


def _burn_level_name(level: int) -> str:
    return {BURN_EARLY: 'early', BURN_WARN: 'warn',
            BURN_CRITICAL: 'critical'}.get(level, 'none')


def _urgency_for_level(level: int) -> str:
    return 'critical' if level >= LEVEL_CRITICAL else 'normal'


def _elapsed_seconds(window: str, resets_at: Optional[str],
                     now: datetime) -> Optional[float]:
    total = WINDOW_TOTAL_SECONDS.get(window)
    end = _parse_iso(resets_at)
    if total is None or end is None:
        return None
    start = end.timestamp() - total
    return max(now.timestamp() - start, 0.0)


@dataclass
class _AccountState:
    five_hour: Dict[str, object] = field(default_factory=dict)
    seven_day: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'5h': dict(self.five_hour), '7d': dict(self.seven_day)}

    @classmethod
    def from_dict(cls, d: dict) -> '_AccountState':
        fh = d.get('5h')
        sd = d.get('7d')
        return cls(
            five_hour=dict(fh) if isinstance(fh, dict) else {},
            seven_day=dict(sd) if isinstance(sd, dict) else {},
        )


class NotificationManager:
    def __init__(self, state_file: Optional[Union[str, Path]] = None):
        self.state_file = Path(state_file) if state_file else NOTIFICATION_STATE_FILE
        self._state: Dict[str, _AccountState] = self._load()

    def _load(self) -> Dict[str, _AccountState]:
        if not self.state_file.exists():
            return {}
        try:
            raw = json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        # A structurally-valid-but-wrong state file (JSON null/list/string, a
        # non-dict 'accounts', or non-dict per-account entries) must not crash
        # NotificationManager construction — that would wedge notifications for
        # the whole tray process. Fall back to empty state instead.
        if not isinstance(raw, dict):
            return {}
        accounts = raw.get('accounts')
        if not isinstance(accounts, dict):
            return {}
        return {label: _AccountState.from_dict(d)
                for label, d in accounts.items()
                if isinstance(d, dict)}

    def _save(self) -> None:
        payload = {
            'accounts': {label: st.to_dict()
                         for label, st in self._state.items()},
        }
        write_private_file(self.state_file, json.dumps(payload, indent=2))

    def reset_account(self, account: str) -> None:
        self._state.pop(account, None)
        self._save()

    def reset_all(self) -> None:
        self._state.clear()
        self._save()

    def evaluate(self, account: str, snapshot: WindowSnapshot,
                 thresholds: Optional[dict] = None,
                 burn_rate: Optional[dict] = None,
                 now: Optional[datetime] = None,
                 ) -> List[Notification]:
        """Return the notifications that should fire for this snapshot.

        Updates and persists internal state — caller need not save.
        """
        thresholds = thresholds or DEFAULT_THRESHOLDS
        burn_rate = burn_rate or DEFAULT_BURN_RATE
        warn = float(thresholds.get('warn', DEFAULT_THRESHOLDS['warn']))
        critical = float(thresholds.get('critical',
                                        DEFAULT_THRESHOLDS['critical']))
        now = now or datetime.now(timezone.utc)

        st = self._state.setdefault(account, _AccountState())
        notes: List[Notification] = []

        notes.extend(self._eval_window(
            account, st, '5h',
            snapshot.five_hour_pct, snapshot.five_hour_resets_at,
            warn, critical,
        ))
        notes.extend(self._eval_window(
            account, st, '7d',
            snapshot.seven_day_pct, snapshot.seven_day_resets_at,
            warn, critical,
            burn_rate=burn_rate, now=now,
        ))

        self._save()
        return notes

    def _eval_window(self, account: str, st: _AccountState, window: str,
                     pct: Optional[float], resets_at: Optional[str],
                     warn: float, critical: float,
                     burn_rate: Optional[dict] = None,
                     now: Optional[datetime] = None,
                     ) -> List[Notification]:
        bucket = st.five_hour if window == '5h' else st.seven_day
        tolerance = (ROLLOVER_TOLERANCE_5H_SECONDS if window == '5h'
                     else ROLLOVER_TOLERANCE_7D_SECONDS)

        if _is_rollover(bucket.get('resets_at'), resets_at, tolerance):
            bucket['threshold_level'] = LEVEL_NONE
            bucket['burn_rate_level'] = BURN_NONE
        if resets_at:
            bucket['resets_at'] = resets_at

        notes: List[Notification] = []
        if pct is None:
            return notes

        prev_level = int(bucket.get('threshold_level') or LEVEL_NONE)
        new_level = _threshold_level(pct, warn, critical)
        if new_level > prev_level:
            for lvl in range(prev_level + 1, new_level + 1):
                notes.append(self._threshold_note(
                    account, window, lvl, pct))
            bucket['threshold_level'] = new_level

        if window == '7d' and burn_rate and burn_rate.get('enabled'):
            note = self._maybe_burn_rate_note(
                account, bucket, pct, resets_at, warn, critical,
                multiplier=float(burn_rate.get(
                    'multiplier', DEFAULT_BURN_RATE['multiplier'])),
                now=now or datetime.now(timezone.utc),
            )
            if note is not None:
                notes.append(note)

        return notes

    @staticmethod
    def _threshold_note(account: str, window: str, level: int,
                        pct: float) -> Notification:
        name = _level_name(level)
        urgency = _urgency_for_level(level)
        if level == LEVEL_MAX:
            title = f"Claude {window} limit reached"
            body = f"[{account}] {window} usage at {pct:.0f}%"
        else:
            title = f"Claude {window} usage {name}"
            body = f"[{account}] {window} usage at {pct:.0f}%"
        return Notification(
            account=account, kind='threshold', window=window,
            level=name, urgency=urgency, title=title, body=body,
        )

    @staticmethod
    def _maybe_burn_rate_note(account: str, bucket: dict,
                              pct: float, resets_at: Optional[str],
                              warn: float, critical: float,
                              multiplier: float,
                              now: datetime) -> Optional[Notification]:
        elapsed = _elapsed_seconds('7d', resets_at, now)
        if elapsed is None:
            return None
        if elapsed < BURN_RATE_GRACE_HOURS * 3600:
            return None
        elapsed_pct = (elapsed / WINDOW_TOTAL_SECONDS['7d']) * 100.0
        if elapsed_pct <= 0:
            return None
        ratio = pct / elapsed_pct
        if ratio < multiplier:
            return None

        if pct >= critical:
            target = BURN_CRITICAL
        elif pct >= warn:
            target = BURN_WARN
        else:
            target = BURN_EARLY

        prev = int(bucket.get('burn_rate_level') or BURN_NONE)
        if target <= prev:
            return None
        bucket['burn_rate_level'] = target

        urgency = 'critical' if target == BURN_CRITICAL else 'normal'
        body = (f"[{account}] 7d at {pct:.0f}% with only "
                f"{elapsed_pct:.0f}% of week elapsed "
                f"(pace ×{ratio:.1f})")
        return Notification(
            account=account, kind='burn_rate', window='7d',
            level=_burn_level_name(target),
            urgency=urgency,
            title="Claude burn rate elevated",
            body=body,
        )
