"""A read-only view of claudehop's state, so the tracker can say whose usage it shows.

claudehop (https://github.com/psychofict/claudehop) keeps one saved login per
account in ``~/.claude/accounts/<name>.json`` plus a few small files this
module reads:

- ``active``: the account whose login is live. claudehop replaces the file on
  every hop, so its modification time is when the last hop happened.
- ``provider`` and ``<name>.provider``: a provider such as Bedrock that new
  sessions run through instead of the login.
- ``.usage-cache``: the last 5-hour and weekly reading of every account, kept
  by ``hop usage`` and ``hop next`` (claudehop 1.6.0 and later). No tokens.
- ``next``: the logins ``hop next`` may choose from.
- ``.run/<name>/``: the config home of a ``hop run`` session, a terminal on
  its own account (claudehop 1.7.0 and later). ``.hop-pids`` lists the open
  sessions; Claude Code's own ``sessions/<pid>.json`` names their session ids.

Nothing here writes to claudehop's files, and no token is kept: a token is read
once to tell which saved account is live, then dropped. ``read_all`` is the one
call that does more, and it does it by running claudehop itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .cloud import weekly_window


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


def claude_home() -> Path:
    """The Claude Code config dir claudehop manages (its live login and logs)."""
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude'))


def accounts_dir() -> Path:
    env = os.environ.get('CLAUDE_ACCOUNTS_DIR')
    if env:
        return Path(env)
    return claude_home() / 'accounts'


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


def active_changed_at() -> Optional[datetime]:
    """When the ``active`` pointer was last written, i.e. the last hop."""
    try:
        return datetime.fromtimestamp((accounts_dir() / 'active').stat().st_mtime,
                                      timezone.utc)
    except OSError:
        return None


# ── Readings ────────────────────────────────────────────────────────────────

@dataclass
class Reading:
    """One account's plan usage as last read, wherever it came from."""
    pct5: Optional[float]
    resets_5h: Optional[str]
    pct7: Optional[float]
    resets_7d: Optional[str]
    week_scope: Optional[str]          # the model the weekly figure covers; None = all models
    at: Optional[datetime]             # when it was read
    source: str                        # 'live', 'widget', 'claudehop' or 'probe'
    throttled_until: Optional[datetime] = None

    @property
    def has_figures(self) -> bool:
        return self.at is not None and (self.pct5 is not None or self.pct7 is not None)


def _parse_iso(raw) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _epoch(raw) -> Optional[datetime]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _num(raw) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def reading_from_cloud(data: Optional[dict], at: datetime,
                       source: str = 'live') -> Optional[Reading]:
    """A reading from a usage API reply (``five_hour``, ``seven_day``, ``limits``)."""
    if not isinstance(data, dict):
        return None
    five = data.get('five_hour') if isinstance(data.get('five_hour'), dict) else {}
    week = weekly_window(data) or {}
    return Reading(_num(five.get('utilization')), five.get('resets_at'),
                   _num(week.get('utilization')), week.get('resets_at'),
                   week.get('scope'), at, source)


def reading_from_cache(entry: dict) -> Optional[Reading]:
    """A reading from one entry of claudehop's ``.usage-cache``.

    claudehop stores windows as ``{"pct", "resetsAt"}`` and its ``limits`` rows
    as ``{"kind", "group", "pct", "resetsAt", "scope"}``. They are put back in
    the API's shape so the weekly figure is chosen exactly as for a live reply.
    """
    if not isinstance(entry, dict):
        return None
    windows = entry.get('windows') if isinstance(entry.get('windows'), dict) else None

    def win(w):
        return ({'utilization': w.get('pct'), 'resets_at': w.get('resetsAt')}
                if isinstance(w, dict) else None)

    throttled = _epoch(entry.get('throttledUntil'))
    if windows is None:
        if throttled is None:
            return None
        return Reading(None, None, None, None, None, None, 'claudehop', throttled)
    shaped = {
        'five_hour': win(windows.get('five_hour')),
        'seven_day': win(windows.get('seven_day')),
        'limits': [{'kind': r.get('kind'), 'group': r.get('group'),
                    'percent': r.get('pct'), 'resets_at': r.get('resetsAt'),
                    'scope': ({'model': {'display_name': r['scope']}}
                              if r.get('scope') else None)}
                   for r in entry.get('limits') or [] if isinstance(r, dict)],
    }
    source = 'probe' if entry.get('source') == 'probe' else 'claudehop'
    r = reading_from_cloud(shaped, _epoch(entry.get('fetchedAt')), source)
    if r is not None:
        r.throttled_until = throttled
    return r


def reading_from_seen(row: Optional[dict]) -> Optional[Reading]:
    """A reading the widget took itself while the account was live (``hop_usage``)."""
    if not row:
        return None
    return Reading(_num(row.get('pct5')), row.get('resets_5h'),
                   _num(row.get('pct7')), row.get('resets_7d'),
                   row.get('week_scope'), _parse_iso(row.get('seen_at')), 'widget')


def load_usage_cache() -> Dict[str, Reading]:
    """claudehop's last reading of every account, keyed by account name."""
    doc = _read_json(accounts_dir() / '.usage-cache')
    out: Dict[str, Reading] = {}
    for name, entry in doc.items():
        r = reading_from_cache(entry)
        if r is not None:
            out[str(name)] = r
    return out


def newest(*readings: Optional[Reading]) -> Optional[Reading]:
    """The most recent reading that has figures, carrying any server throttle
    claudehop has noted for the account. A window the newest reading lacks
    (readings from before the Team-seat fix have no weekly figure) is taken
    from the next newest one that has it."""
    have = sorted((r for r in readings if r is not None and r.has_figures),
                  key=lambda r: r.at, reverse=True)
    throttle = max((r.throttled_until for r in readings
                    if r is not None and r.throttled_until), default=None)
    if not have:
        return next((r for r in readings if r is not None), None)
    best = replace(have[0], throttled_until=throttle or have[0].throttled_until)
    if best.pct5 is None:
        donor = next((r for r in have[1:] if r.pct5 is not None), None)
        if donor:
            best = replace(best, pct5=donor.pct5, resets_5h=donor.resets_5h)
    if best.pct7 is None:
        donor = next((r for r in have[1:] if r.pct7 is not None), None)
        if donor:
            best = replace(best, pct7=donor.pct7, resets_7d=donor.resets_7d,
                           week_scope=donor.week_scope)
    return best


def all_readings(names: List[str], seen_rows: Dict[str, dict],
                 live_name: Optional[str] = None,
                 live: Optional[Reading] = None) -> Dict[str, Optional[Reading]]:
    """The best reading for each account: the live one for the live login when
    there is one, otherwise the newer of the widget's own (``seen_rows``, from
    ``Store.hop_usage``) and claudehop's cache."""
    cache = load_usage_cache()
    out: Dict[str, Optional[Reading]] = {}
    for n in names:
        if n == live_name and live is not None and live.has_figures:
            out[n] = live
        else:
            out[n] = newest(reading_from_seen(seen_rows.get(n)), cache.get(n))
    return out


# ── What to say about each account ──────────────────────────────────────────

def countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def ago(when: Optional[datetime], now: datetime) -> str:
    if when is None:
        return "some time ago"
    secs = max(0, (now - when).total_seconds())
    if secs < 60:
        return "just now"
    return f"{countdown(secs)} ago"


def window_now(pct: Optional[float], resets: Optional[str],
               now: datetime) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """``(pct now, seconds to reset, pct before a reset)`` for a stored window.

    A window whose reset time has passed is empty again whatever it read when
    it was fetched; the third value keeps that old figure.
    """
    if pct is None:
        return None, None, None
    when = _parse_iso(resets)
    if when is None:
        return pct, None, None
    left = (when - now).total_seconds()
    if left <= 0:
        return 0.0, None, pct
    return pct, left, None


def _window_text(label: str, pct: Optional[float], resets: Optional[str],
                 now: datetime) -> str:
    cur, _left, was = window_now(pct, resets, now)
    if cur is None:
        return f"{label} ?"
    if was is not None:
        return f"{label} reset, was {was:.0f}%" if was >= 0.5 else f"{label} 0%"
    return f"{label} {cur:.0f}%"


def usage_text(reading: Optional[Reading],
               now: Optional[datetime] = None) -> Tuple[str, str]:
    """``(usage, freshness)`` for one account's reading."""
    now = now or datetime.now(timezone.utc)
    throttle = ""
    if reading is not None and reading.throttled_until and reading.throttled_until > now:
        throttle = ("server throttling reads for "
                    f"{countdown((reading.throttled_until - now).total_seconds())}")
    if reading is None or not reading.has_figures:
        return "no reading yet", throttle
    week = '7-day' + (f" {reading.week_scope}" if reading.week_scope else '')
    usage = (f"{_window_text('5-hour', reading.pct5, reading.resets_5h, now)} · "
             f"{_window_text(week, reading.pct7, reading.resets_7d, now)}")
    if reading.source == 'live':
        fresh = 'live'
    else:
        fresh = f"read {ago(reading.at, now)}"
        if reading.source == 'probe':
            fresh += " through claude"
    return usage, "; ".join(x for x in (fresh, throttle) if x)


# ── hop next: which login has the most room ─────────────────────────────────

NEXT_FULL_PCT = 95.0   # claudehop's own line: a 5-hour window this full is not worth hopping into


def next_list() -> List[str]:
    """The logins ``hop next`` may choose from, in the order given."""
    try:
        with open(accounts_dir() / 'next') as f:
            return [ln.strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith('#')]
    except OSError:
        return []


@dataclass
class Pick:
    name: Optional[str]     # None when no login has room
    why: str
    listed: bool            # chosen from claudehop's `next` list


def most_room(readings: Dict[str, Optional[Reading]], names: List[str],
              active: Optional[str],
              now: Optional[datetime] = None) -> Optional[Pick]:
    """The login with the most 5-hour room, judged as ``hop next`` judges it.

    Candidates are the ``next`` list when there is one, else ``names``, never
    the live login. A login is full when its 5-hour window is at 95% or an
    all-model weekly limit is used up; a weekly meter for one model does not
    stop the others. Within ten points, the window that resets soonest wins,
    since what it has left is lost at the reset. None with fewer than one
    candidate.
    """
    now = now or datetime.now(timezone.utc)
    listed = [n for n in next_list() if n in names]
    pool = [n for n in (listed or names) if n != active]
    if not pool:
        return None
    room, full, unknown = [], [], []
    for n in pool:
        r = readings.get(n)
        if r is None or not r.has_figures or r.pct5 is None:
            unknown.append(n)
            continue
        pct, left, _ = window_now(r.pct5, r.resets_5h, now)
        wk, wleft, _ = window_now(r.pct7, r.resets_7d, now)
        if r.week_scope is None and wk is not None and wk >= 100:
            full.append((wleft, n))
        elif pct >= NEXT_FULL_PCT:
            full.append((left, n))
        else:
            room.append((int(pct // 10), left if left else float('inf'), n, pct, left, r))
    if room:
        room.sort(key=lambda x: (x[0], x[1]))
        _, _, n, pct, left, r = room[0]
        state = f"resets in {countdown(left)}" if left else "window not started"
        return Pick(n, f"5-hour {pct:.0f}%, {state}; read {ago(r.at, now)}",
                    bool(listed))
    timed = sorted((left, n) for left, n in full if left)
    why = "no login has room"
    if timed:
        why += f"; {timed[0][1]} frees up first, in {countdown(timed[0][0])}"
    if unknown:
        why += f"; no reading for {', '.join(unknown)}"
    return Pick(None, why, bool(listed))


# ── hop run sessions ────────────────────────────────────────────────────────

def _proc_start(pid: int) -> str:
    """The start time /proc records for a pid; it tells a reused pid from ours."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _alive(pid: int, started: str = "") -> bool:
    if started:
        return _proc_start(pid) == started
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _run_homes() -> List[Path]:
    try:
        return sorted(p for p in (accounts_dir() / '.run').iterdir() if p.is_dir())
    except OSError:
        return []


def run_terminals() -> Dict[str, int]:
    """How many ``hop run`` sessions each account has open right now."""
    out: Dict[str, int] = {}
    for home in _run_homes():
        try:
            entries = list((home / '.hop-pids').iterdir())
        except OSError:
            continue
        n = 0
        for e in entries:
            if not e.name.isdigit():
                continue
            try:
                started = e.read_text().strip()
            except OSError:
                continue
            if _alive(int(e.name), started):
                n += 1
        if n:
            out[home.name] = n
    return out


@dataclass
class RunSession:
    session_id: str
    account: str
    since: datetime         # when its claude process started


def run_sessions() -> List[RunSession]:
    """The conversations open in ``hop run`` sessions, and whose they are.

    A ``hop run`` session points Claude Code at ``.run/<name>/``, and Claude
    Code writes ``sessions/<pid>.json`` there with the conversation's id, so a
    turn in that conversation was made with that account's login, not the live
    one. Files of processes that have ended are skipped.
    """
    out: List[RunSession] = []
    for home in _run_homes():
        try:
            files = sorted((home / 'sessions').glob('*.json'))
        except OSError:
            continue
        for f in files:
            doc = _read_json(f)
            sid, pid = doc.get('sessionId'), doc.get('pid')
            if not isinstance(sid, str) or not sid or not isinstance(pid, int):
                continue
            if not _alive(pid, str(doc.get('procStart') or '')):
                continue
            since = _epoch((doc.get('startedAt') or 0) / 1000)
            if since is None:
                continue
            out.append(RunSession(sid, home.name, since))
    return out


# ── Asking claudehop for a fresh reading of every account ──────────────────

READ_ALL_WAIT_S = 120


def find_claudehop() -> Optional[str]:
    """The claudehop command: on PATH (as ``claudehop`` or ``hop``), or where
    its installer and ``pip install --user`` put it."""
    for name in ('claudehop', 'hop'):
        found = shutil.which(name)
        if found:
            return found
    for p in (claude_home() / 'bin' / 'claudehop',
              Path.home() / '.local' / 'bin' / 'claudehop'):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def lock_busy() -> bool:
    """True while another claudehop command holds claudehop's lock.

    Every claudehop command that changes anything, ``hop usage`` included,
    takes ``accounts/.lock`` first, and the bare ``hop`` picker holds it until
    you choose an account. A read started then waits behind it.
    """
    try:
        import fcntl
    except ImportError:            # not on Windows; claudehop's lock is fcntl too
        return False
    try:
        fd = os.open(accounts_dir() / '.lock', os.O_RDONLY)
    except OSError:
        return False               # no lock file: nothing holds it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@dataclass
class ReadAll:
    status: str                    # 'done', 'busy', 'slow' or 'error'
    message: str = ''
    notes: Dict[str, str] = field(default_factory=dict)   # account -> why its reading is old


BUSY = ("Another claudehop command holds its lock (an open hop picker does "
        "until you choose).")
_running: Optional[subprocess.Popen] = None


def read_all(wait: float = READ_ALL_WAIT_S) -> ReadAll:
    """Run ``hop usage --json`` once and report what came of it.

    claudehop asks the usage endpoint for every saved account, renews an idle
    account's aged-out token first under its own locks, waits out a server
    throttle, and writes ``.usage-cache``, which ``load_usage_cache`` reads. It
    spends a little of the endpoint's per-token budget, so it runs only when
    the person asks.

    claudehop is never killed: stopping it between a token renewal and the
    write that saves it would lose that account's login. A read still going
    after ``wait`` seconds, or queued behind another claudehop command, is left
    to finish on its own, and its figures appear in the cache when it does.
    """
    global _running
    if _running is not None and _running.poll() is None:
        return ReadAll('slow', "The last read is still running; its figures "
                               "appear when it finishes.")
    exe = find_claudehop()
    if not exe:
        return ReadAll('error', "claudehop is not installed or not on PATH.")
    if lock_busy():
        return ReadAll('busy', BUSY + " Press again once it is closed.")
    try:
        proc = subprocess.Popen([exe, 'usage', '--json'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
    except OSError as e:
        return ReadAll('error', f"Could not run claudehop: {e}")
    _running = proc
    out: List[str] = []
    err: List[str] = []
    queued = threading.Event()

    def pump(stream, sink, watch):
        for line in stream:
            sink.append(line)
            if watch and 'waiting for another' in line:
                queued.set()
    pumps = [threading.Thread(target=pump, args=(proc.stdout, out, False), daemon=True),
             threading.Thread(target=pump, args=(proc.stderr, err, True), daemon=True)]
    for t in pumps:
        t.start()
    threading.Thread(target=proc.wait, daemon=True).start()   # reaped whenever it ends

    deadline = time.monotonic() + wait
    while proc.poll() is None:
        if queued.is_set():
            return ReadAll('busy', BUSY + " This read runs as soon as it is closed.")
        if time.monotonic() > deadline:
            return ReadAll('slow', "claudehop is still reading; the figures "
                                   "appear when it finishes.")
        time.sleep(0.2)
    for t in pumps:
        t.join(timeout=2)
    try:
        doc = json.loads(''.join(out))
    except ValueError:
        said = (''.join(err).strip().splitlines() or [''])[-1]
        return ReadAll('error', f"claudehop: {said[:160]}" if said
                       else f"claudehop exited {proc.returncode}.")
    notes: Dict[str, str] = {}
    for a in (doc.get('accounts') or []) if isinstance(doc, dict) else []:
        if isinstance(a, dict) and a.get('name'):
            notes[str(a['name'])] = str(a.get('note') or '')
    return ReadAll('done', notes=notes)
