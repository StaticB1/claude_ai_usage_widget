"""Single-instance lock backed by fcntl.flock.

The lock file lives at CONFIG_DIR/app.lock. If acquisition fails another
instance is already running — we read its PID from the file, send it
SIGUSR1 (so the running app can raise its window), and the caller should
exit cleanly. The kernel releases flock when the process dies, so a
crash leaves no stale lock to clean up.
"""
from __future__ import annotations
import fcntl
import os
import signal
from pathlib import Path
from typing import Optional, TextIO

from .config import CONFIG_DIR

LOCK_FILE = CONFIG_DIR / 'app.lock'


class AlreadyRunning(Exception):
    def __init__(self, pid: Optional[int]):
        super().__init__(f"Another instance is running (pid={pid})")
        self.pid = pid


def acquire() -> TextIO:
    """Acquire the singleton lock. Caller must keep the returned handle
    open for the life of the process. Raises AlreadyRunning if another
    instance holds it."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, 'a+')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        try:
            pid = int((fh.read() or '0').strip())
        except ValueError:
            pid = None
        fh.close()
        raise AlreadyRunning(pid)
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def signal_running_instance(pid: Optional[int]) -> bool:
    """Tell the running instance to raise its window. Returns True if the
    signal was delivered."""
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False
