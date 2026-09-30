"""A read-only view of claudehop's state, so the tracker can say whose usage it shows.

claudehop keeps one saved login per account in ``~/.claude/accounts/<name>.json``
plus two small pointers: ``active`` (the account whose login is live) and
``provider`` (a provider such as Bedrock that new sessions run through instead
of the login, described by ``<name>.provider``). This module only reads them.
It never writes, and it keeps no token: a token is read once to tell which
saved account is live, then dropped.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class HopProfile:
    name: str
    email: Optional[str]
    plan: Optional[str]


@dataclass
class HopState:
    active: Optional[str]              # the account whose login is live
    provider: Optional[str]            # e.g. 'bedrock' while new sessions bypass the login
    provider_command: Optional[str]
    profiles: List[HopProfile] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)

    def profile(self, name: Optional[str]) -> Optional[HopProfile]:
        return next((p for p in self.profiles if p.name == name), None)


def accounts_dir() -> Path:
    env = os.environ.get('CLAUDE_ACCOUNTS_DIR')
    if env:
        return Path(env)
    base = os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude')
    return Path(base) / 'accounts'


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _first_line(path: Path) -> Optional[str]:
    try:
        with open(path) as f:
            line = f.readline().strip()
    except OSError:
        return None
    return line or None


def load_state(live_token: Optional[str] = None) -> Optional[HopState]:
    """What claudehop currently says, or None when it is not in use.

    ``live_token`` is the access token in the live credential store. When it
    matches a saved account exactly that account is the active one; otherwise
    the ``active`` pointer decides, as claudehop itself does.
    """
    d = accounts_dir()
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return None
    profiles: List[HopProfile] = []
    tokens: Dict[str, Optional[str]] = {}
    providers: List[str] = []
    for f in entries:
        n = f.name
        if n.startswith('.'):
            continue
        if n.endswith('.json'):
            doc = _read_json(f)
            name = n[:-len('.json')]
            profiles.append(HopProfile(name, doc.get('email'), doc.get('plan')))
            tokens[name] = (doc.get('claudeAiOauth') or {}).get('accessToken')
        elif n.endswith('.provider'):
            providers.append(n[:-len('.provider')])
    if not profiles and not providers:
        return None

    active: Optional[str] = None
    if live_token:
        active = next((name for name, tok in tokens.items()
                       if tok and tok == live_token), None)
    if active is None:
        ptr = _first_line(d / 'active')
        if ptr in tokens:
            active = ptr

    provider = _first_line(d / 'provider')
    command = _first_line(d / f'{provider}.provider') if provider else None
    if not command:
        provider = None            # a pointer to a provider that is gone counts as off
    return HopState(active, provider, command, profiles, providers)


def active_name(live_token: Optional[str] = None) -> Optional[str]:
    st = load_state(live_token)
    return st.active if st else None


# ── What to say about each account ──────────────────────────────────────────

def _ago(iso: str, now: datetime) -> str:
    try:
        when = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return "some time"
    secs = max(0, (now - when).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _window_text(label: str, pct: Optional[int], resets: Optional[str],
                 now: datetime) -> str:
    """One window's reading. A window that has reset since the reading was
    taken is reported as reset, not as the stale percentage."""
    if pct is None:
        return f"{label} ?"
    if resets:
        try:
            when = datetime.fromisoformat(resets.replace('Z', '+00:00'))
            if when <= now:
                return f"{label} reset"
        except ValueError:
            pass
    return f"{label} {pct}%"


def usage_text(name: str, state: HopState, live_pct5: Optional[int],
               live_pct7: Optional[int], live_ok: bool,
               seen: Optional[dict],
               now: Optional[datetime] = None) -> Tuple[str, str]:
    """``(usage, freshness)`` for one saved account.

    The active account is read live from the widget's own polling. An account
    you have hopped away from cannot be polled without renewing its token, so
    it shows the last reading taken while it was active, with its age.
    """
    now = now or datetime.now(timezone.utc)
    if name == state.active and live_ok and live_pct5 is not None:
        return (f"5-hour {live_pct5}% · 7-day {live_pct7 if live_pct7 is not None else '?'}%",
                "live")
    if seen:
        usage = (f"{_window_text('5-hour', seen.get('pct5'), seen.get('resets_5h'), now)} · "
                 f"{_window_text('7-day', seen.get('pct7'), seen.get('resets_7d'), now)}")
        return usage, f"last seen {_ago(seen.get('seen_at') or '', now)}"
    return "no reading yet", ""
