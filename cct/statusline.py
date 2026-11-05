"""Claude Code's status line as the source of the plan windows, with no token.

Claude Code runs its status-line command with a JSON description of the
session on stdin. For Pro, Max and Ultra subscribers that JSON carries
``rate_limits``: the five-hour and weekly windows, each with
``used_percentage`` (0 to 100) and ``resets_at`` (Unix seconds), from the
session's first reply on. ``ctt statusline`` saves that part, one file per
Claude config home, then runs the status line you already had with the same
input, so nothing changes on screen. The widget and the CLI read these files
instead of asking Claude.ai with your login token.

Team and Enterprise seats get no ``rate_limits`` in the status line, so for
them this source stays empty and claudehop's readings (or polling, if turned
on) fill the gap.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import CONFIG_DIR

READINGS_DIR = CONFIG_DIR / 'statusline'
PASS_THROUGH_TIMEOUT_S = 10


def default_claude_dir() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude'))


def _resolved(path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser())


def _file_for(config_dir: str) -> Path:
    key = hashlib.sha1(_resolved(config_dir).encode()).hexdigest()[:16]
    return READINGS_DIR / f'{key}.json'


def hop_name_for(config_dir: Path) -> Optional[str]:
    """The claudehop account a session in ``config_dir`` runs as, if any.

    A ``hop run`` session's home is ``<accounts>/.run/<name>``, which names the
    account. Any other home is the live login, whose name claudehop keeps in
    ``<home>/accounts/active``."""
    p = Path(_resolved(config_dir))
    if p.parent.name == '.run':
        return p.name or None
    acc = Path(os.environ.get('CLAUDE_ACCOUNTS_DIR') or str(p / 'accounts'))
    try:
        with open(acc / 'active') as f:
            return f.readline().strip() or None
    except OSError:
        return None


def _window(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    pct = raw.get('used_percentage')
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    out = {'used_percentage': float(pct)}
    reset = raw.get('resets_at')
    if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
        out['resets_at'] = float(reset)
    return out


def save(payload: dict, config_dir: Path, now: Optional[datetime] = None) -> bool:
    """Keep the plan windows from one status-line payload. False when it has none."""
    rl = payload.get('rate_limits') if isinstance(payload, dict) else None
    if not isinstance(rl, dict):
        return False
    five, week = _window(rl.get('five_hour')), _window(rl.get('seven_day'))
    if five is None and week is None:
        return False
    now = now or datetime.now(timezone.utc)
    doc = {
        'config_dir': _resolved(config_dir),
        'hop_name': hop_name_for(config_dir),
        'at': now.isoformat(),
        'five_hour': five,
        'seven_day': week,
    }
    path = _file_for(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Several terminals may write the same home's file at once, so each
    # writes its own temporary file and renames it into place.
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    return True


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def as_cloud(entry: dict) -> dict:
    """A saved reading in the usage API's shape (``utilization`` in percent)."""
    def win(w):
        if not isinstance(w, dict) or w.get('used_percentage') is None:
            return None
        return {'utilization': w['used_percentage'], 'resets_at': _iso(w.get('resets_at'))}
    return {'five_hour': win(entry.get('five_hour')),
            'seven_day': win(entry.get('seven_day')), 'limits': []}


def read_at(entry: dict) -> Optional[datetime]:
    raw = entry.get('at')
    try:
        when = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def load_all() -> List[dict]:
    """Every saved reading, newest first."""
    out = []
    try:
        names = os.listdir(READINGS_DIR)
    except OSError:
        return []
    for n in names:
        if not n.endswith('.json'):
            continue
        try:
            with open(READINGS_DIR / n) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and read_at(doc) is not None:
            out.append(doc)
    out.sort(key=read_at, reverse=True)
    return out


def for_dir(claude_dir, entries: Optional[List[dict]] = None) -> Optional[dict]:
    """The newest reading taken in ``claude_dir``, or None."""
    want = _resolved(claude_dir)
    for e in entries if entries is not None else load_all():
        if e.get('config_dir') == want:
            return e
    return None


def by_hop_name(entries: Optional[List[dict]] = None) -> Dict[str, dict]:
    """The newest reading of each claudehop account."""
    out: Dict[str, dict] = {}
    for e in entries if entries is not None else load_all():
        name = e.get('hop_name')
        if name and name not in out:
            out[name] = e
    return out


def _default_line(payload: dict) -> str:
    rl = payload.get('rate_limits') if isinstance(payload, dict) else None
    rl = rl if isinstance(rl, dict) else {}
    model = ((payload.get('model') or {}).get('display_name')
             if isinstance(payload, dict) and isinstance(payload.get('model'), dict) else None)
    parts = [model] if model else []
    for key, label in (('five_hour', '5h'), ('seven_day', '7d')):
        w = _window(rl.get(key))
        if w is not None:
            parts.append(f"{label} {w['used_percentage']:.0f}%")
    return ' · '.join(parts)


def run(command: List[str]) -> int:
    """``ctt statusline [-- COMMAND ...]``: save the plan windows from stdin,
    then print COMMAND's status line (run with the same input), or a short
    one of our own. Never fails the status line over a save that went wrong."""
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode('utf-8', 'replace') or '{}')
    except ValueError:
        payload = {}
    try:
        save(payload, default_claude_dir())
    except Exception as e:  # the status line matters more than the reading
        print(f"ctt statusline: could not save the reading: {e}", file=sys.stderr)
    if command:
        try:
            done = subprocess.run(' '.join(command), shell=True, input=raw,
                                  stdout=subprocess.PIPE,
                                  timeout=PASS_THROUGH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return 1
        sys.stdout.buffer.write(done.stdout)
        sys.stdout.flush()
        return done.returncode
    print(_default_line(payload))
    return 0
