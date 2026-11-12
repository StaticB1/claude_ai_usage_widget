"""Which logins Claude.ai's usage endpoint is refusing, kept across restarts.

The endpoint behind Claude Code's /usage answers clients other than Claude
Code from a small allowance per login, then refuses them with HTTP 429 and a
Retry-After of 25 minutes to an hour (measured by claudehop on 6 and 13 Oct
2026). Claude Code asks it too, so the login that is live, or was lately, is
the one most often refused. A request sent before the Retry-After has passed
only brings another 429, so the widget keeps each refusal until it lapses,
under the claudehop account's name when it knows it, and writes it to disk so
a restart does not ask again at once.

Every request the widget sends is also logged, one line each, so how much the
endpoint allows can be measured from real use instead of guessed. No token is
written to either file.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union

from .config import CONFIG_DIR, write_private_file

THROTTLE_FILE = CONFIG_DIR / 'throttle.json'
REQUEST_LOG = CONFIG_DIR / 'usage-requests.log'
# Past this size the log keeps its newest half: at one request every two
# minutes that is still more than two days of lines.
REQUEST_LOG_MAX_BYTES = 256_000


def key_for(label: str, hop_name: Optional[str] = None) -> str:
    """The name a refusal is kept under: the claudehop account when the
    reading is of one (the refusal follows the login, not the widget's
    account entry), else the widget's account label."""
    return f"hop:{hop_name}" if hop_name else f"account:{label}"


def backoff_holds(cloud_state: str, retry_at: Optional[datetime], now: datetime,
                  token_before: Optional[str], token_now: Optional[str]) -> bool:
    """Whether an account's last refusal still stops the poll from asking.

    Only while it lasts, and only for the token it was earned with: after a
    claudehop hop the token is another account's, whose own refusal (if any)
    is found under its own ``key_for`` key. A token Claude Code merely
    renewed is another token too; the same account's refusal is then still
    found under that key.
    """
    return (cloud_state == 'rate_limited' and retry_at is not None
            and retry_at > now and token_now == token_before)


def _parse(raw) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


class Throttles:
    """Refusals by key, read from and saved to ``path``. Used from the poll
    thread only."""

    def __init__(self, path: Union[str, Path] = THROTTLE_FILE):
        self.path = Path(path)
        self._until: Dict[str, datetime] = {}
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            doc = {}
        for key, raw in (doc.items() if isinstance(doc, dict) else []):
            when = _parse(raw)
            if when is not None:
                self._until[str(key)] = when

    def until(self, key: str, now: Optional[datetime] = None) -> Optional[datetime]:
        """When the endpoint will answer this key again; None when it will now."""
        now = now or datetime.now(timezone.utc)
        when = self._until.get(key)
        return when if when is not None and when > now else None

    def set(self, key: str, until: datetime) -> None:
        if self._until.get(key) != until:
            self._until[key] = until
            self._save()

    def clear(self, key: str) -> None:
        if self._until.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        now = datetime.now(timezone.utc)
        self._until = {k: v for k, v in self._until.items() if v > now}
        try:
            write_private_file(self.path, json.dumps(
                {k: v.isoformat() for k, v in sorted(self._until.items())}, indent=1))
        except OSError:
            pass                    # a file we cannot write only costs one 429 after a restart


def log_request(key: str, outcome: Union[int, str], retry_after: Optional[int] = None,
                path: Union[str, Path] = REQUEST_LOG,
                now: Optional[datetime] = None) -> None:
    """Append one request's outcome: time, key, HTTP status (or 'error') and
    the Retry-After a 429 named. Past ``REQUEST_LOG_MAX_BYTES`` the log keeps
    its newest half, cut at a line end."""
    path = Path(path)
    when = (now or datetime.now(timezone.utc)).astimezone().isoformat(timespec='seconds')
    line = f"{when} {key} {outcome}"
    if retry_after is not None:
        line += f" retry-after={int(retry_after)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(line + "\n")
        if path.stat().st_size > REQUEST_LOG_MAX_BYTES:
            tail = path.read_text()[-(REQUEST_LOG_MAX_BYTES // 2):]
            write_private_file(path, tail[tail.find("\n") + 1:])
    except OSError:
        pass
