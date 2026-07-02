from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

APP_ID = 'claude-token-tracker'
APP_NAME = 'Claude Usage Widget & Token Tracker'
APP_VERSION = '2.0.0'

CLAUDE_DIR = Path.home() / '.claude' / 'projects'
CREDENTIALS_FILE = Path.home() / '.claude' / '.credentials.json'

CONFIG_DIR = Path.home() / '.config' / APP_ID
CONFIG_FILE = CONFIG_DIR / 'config.json'
DB_FILE = CONFIG_DIR / 'history.db'
RATE_CARD_FILE = CONFIG_DIR / 'rate_card.json'
NOTIFICATION_STATE_FILE = CONFIG_DIR / 'notification_state.json'

USAGE_API_URL = 'https://api.anthropic.com/api/oauth/usage'

LOCAL_SCAN_INTERVAL = 10           # seconds — local log re-scan / tray block

NOTIFICATION_THRESHOLDS = [75, 90, 100]

DEFAULT_POLL_INTERVAL_SECONDS = 120  # match the original widget's cadence
DEFAULT_THRESHOLDS = {'warn': 60, 'critical': 85}
DEFAULT_BURN_RATE = {'enabled': False, 'multiplier': 1.5}
BURN_RATE_GRACE_HOURS = 8

DEFAULT_ACCOUNT_LABEL = 'default'

PERIOD_LABELS = {
    'all':   'All Time',
    'today': 'Today',
    '5h':    'Last 5 Hours',
    '7d':    'Last 7 Days',
    '30d':   'Last 30 Days',
}


@dataclass
class Account:
    label: str
    claude_dir: Path
    disable_polling: bool = False
    hide_from_tray: bool = False

    @property
    def projects_dir(self) -> Path:
        return self.claude_dir / 'projects'

    @property
    def credentials_file(self) -> Path:
        return self.claude_dir / '.credentials.json'

    def to_dict(self) -> dict:
        return {
            'label': self.label,
            'claude_dir': str(self.claude_dir),
            'disable_polling': self.disable_polling,
            'hide_from_tray': self.hide_from_tray,
        }


def write_private_file(path, text: str) -> None:
    """Write ``text`` to ``path`` so the file is never world/group-readable.

    We create a temp file with 0600 *before* writing any content, then
    atomically rename it over the target. This avoids the window where a
    token-bearing file briefly exists with the process umask (often 0644),
    and it doesn't depend on a later ``chmod`` succeeding.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # Enforce 0600 even if a stale tmp file pre-existed with looser perms.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _expand(p) -> Path:
    return Path(os.path.expanduser(str(p))).resolve() if p else Path.home() / '.claude'


def _default_account() -> Account:
    return Account(
        label=DEFAULT_ACCOUNT_LABEL,
        claude_dir=Path.home() / '.claude',
    )


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict) -> None:
    write_private_file(CONFIG_FILE, json.dumps(cfg, indent=2))


def load_accounts() -> List[Account]:
    cfg = load_config()
    raw = cfg.get('accounts')
    if not raw:
        return [_default_account()]
    out: List[Account] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = entry.get('label') or DEFAULT_ACCOUNT_LABEL
        claude_dir = _expand(entry.get('claude_dir'))
        out.append(Account(
            label=label,
            claude_dir=claude_dir,
            disable_polling=bool(entry.get('disable_polling', False)),
            hide_from_tray=bool(entry.get('hide_from_tray', False)),
        ))
    return out or [_default_account()]


def save_accounts(accounts: List[Account]) -> None:
    cfg = load_config()
    cfg['accounts'] = [a.to_dict() for a in accounts]
    save_config(cfg)


def find_account(label: str,
                 accounts: Optional[List[Account]] = None) -> Optional[Account]:
    accounts = accounts if accounts is not None else load_accounts()
    for a in accounts:
        if a.label == label:
            return a
    return None


@dataclass
class Settings:
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    burn_rate: dict = field(default_factory=lambda: dict(DEFAULT_BURN_RATE))
    theme: str = 'system'  # 'system' | 'light' | 'dark'


def load_settings() -> Settings:
    cfg = load_config()
    th = cfg.get('thresholds') or {}
    br = cfg.get('burn_rate') or {}
    return Settings(
        poll_interval_seconds=int(
            cfg.get('poll_interval_seconds') or DEFAULT_POLL_INTERVAL_SECONDS),
        thresholds={
            'warn': int(th.get('warn', DEFAULT_THRESHOLDS['warn'])),
            'critical': int(th.get('critical', DEFAULT_THRESHOLDS['critical'])),
        },
        burn_rate={
            'enabled': bool(br.get('enabled', DEFAULT_BURN_RATE['enabled'])),
            'multiplier': float(
                br.get('multiplier', DEFAULT_BURN_RATE['multiplier'])),
        },
        theme=(t if (t := str(cfg.get('theme') or 'system'))
              in ('system', 'light', 'dark') else 'system'),
    )


def save_settings(s: Settings) -> None:
    cfg = load_config()
    cfg['poll_interval_seconds'] = int(s.poll_interval_seconds)
    cfg['thresholds'] = dict(s.thresholds)
    cfg['burn_rate'] = dict(s.burn_rate)
    cfg['theme'] = s.theme
    save_config(cfg)
