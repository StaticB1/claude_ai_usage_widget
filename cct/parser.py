from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .config import CLAUDE_DIR


@dataclass
class Turn:
    timestamp: datetime
    project: str
    msg_id: Optional[str]
    request_id: Optional[str]
    uuid: Optional[str]
    session_id: Optional[str]
    model: Optional[str]
    input_tokens: int
    cache_creation_5m: int
    cache_creation_1h: int
    cache_read: int
    output_tokens: int
    is_sidechain: bool
    tool_uses: Dict[str, int] = field(default_factory=dict)

    @property
    def cache_creation(self) -> int:
        return self.cache_creation_5m + self.cache_creation_1h

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.cache_creation
                + self.cache_read + self.output_tokens)

    @property
    def dedup_key(self) -> str:
        return self.msg_id or self.request_id or self.uuid or (
            f"{self.project}|{self.session_id or ''}|"
            f"{self.timestamp.isoformat()}|{self.output_tokens}"
        )


def parse_timestamp(raw) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _extract_tool_uses(msg: dict) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    content = msg.get('content')
    if not isinstance(content, list):
        return counts
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'tool_use':
            name = block.get('name') or 'unknown'
            counts[name] = counts.get(name, 0) + 1
    return counts


def _file_mtime(path: Path) -> datetime:
    """Last-modified time of ``path``, or the epoch if it can't be read."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_jsonl(path: Path) -> Tuple[Optional[str], List[Turn]]:
    """Parse one Claude Code session JSONL. Returns (project_label, turns).

    Claude Code writes one assistant API response across several log entries
    that repeat the same `message.id` — the text block on one line, each
    tool_use block on the next. Two things were lost by skipping the repeats
    outright, both measured over this machine's logs:

    * The tool_use blocks the repeats carried. 59% of all tool calls (53,750
      of 91,101) never reached the store, so every tool was under-reported.
    * The real ``output_tokens``. Input, cache-creation and cache-read counts
      are identical on every entry of a message, but the earlier entries
      often carry a placeholder output count (4, or 8) and only the last
      entry has the true one (568). 5,163 messages were affected, costing
      5.44M output tokens — 7.6% of all output on disk, and output is the
      dearest token class there is.

    So a repeat entry now merges its tool counts into the turn already
    recorded and raises each token count to the highest seen for that id.
    """
    project_name: Optional[str] = None
    turns: List[Turn] = []
    seen: Dict[str, Turn] = {}
    last_ts: Optional[datetime] = None
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    # A valid-JSON but non-object line (null, a bare number, a
                    # list) would crash the entry.get(...) calls below with
                    # AttributeError and abort the whole file. Skip it like any
                    # other malformed line.
                    continue

                if project_name is None:
                    cwd = entry.get('cwd')
                    if isinstance(cwd, str) and cwd:
                        project_name = os.path.basename(cwd)

                if entry.get('type') != 'assistant':
                    continue

                msg = entry.get('message')
                if not isinstance(msg, dict):
                    msg = {}
                usage = msg.get('usage')
                if not isinstance(usage, dict):
                    usage = {}
                inp = usage.get('input_tokens', 0) or 0
                cc_total = usage.get('cache_creation_input_tokens', 0) or 0
                cr = usage.get('cache_read_input_tokens', 0) or 0
                out = usage.get('output_tokens', 0) or 0
                model = msg.get('model')

                cc_split = usage.get('cache_creation')
                if not isinstance(cc_split, dict):
                    cc_split = {}
                cc_1h = cc_split.get('ephemeral_1h_input_tokens', 0) or 0
                cc_5m = cc_split.get('ephemeral_5m_input_tokens', 0) or 0
                # `cache_creation_input_tokens` is authoritative. If the 5m/1h
                # breakdown is absent or only partially accounts for the total,
                # attribute the unitemized remainder to 5m rather than silently
                # dropping those tokens.
                remainder = cc_total - (cc_1h + cc_5m)
                if remainder > 0:
                    cc_5m += remainder
                cc_stored = cc_5m + cc_1h

                if not (inp or cc_stored or cr or out):
                    continue

                msg_id = msg.get('id')
                req_id = entry.get('requestId')
                uuid_ = entry.get('uuid')
                key = msg_id or req_id or uuid_
                tool_uses = _extract_tool_uses(msg)
                if key is not None:
                    prior = seen.get(key)
                    if prior is not None:
                        for name, n in tool_uses.items():
                            prior.tool_uses[name] = \
                                prior.tool_uses.get(name, 0) + n
                        # Never sum: the entries restate one response's usage
                        # rather than adding to it. Take the highest, which is
                        # the completed count.
                        prior.input_tokens = max(prior.input_tokens, inp)
                        prior.cache_creation_5m = max(
                            prior.cache_creation_5m, cc_5m)
                        prior.cache_creation_1h = max(
                            prior.cache_creation_1h, cc_1h)
                        prior.cache_read = max(prior.cache_read, cr)
                        prior.output_tokens = max(prior.output_tokens, out)
                        if prior.model is None and model:
                            prior.model = model
                        continue

                # A missing timestamp used to become "now", which drops a
                # historical turn into the current 5-hour window and inflates
                # it. The log is append-ordered, so the previous entry's time
                # is a sound bound; failing that, the file's own mtime.
                ts = parse_timestamp(entry.get('timestamp'))
                if ts is None:
                    ts = last_ts or _file_mtime(path)
                else:
                    last_ts = ts

                turn = Turn(
                    timestamp=ts,
                    project=project_name or 'Unknown',
                    msg_id=msg_id,
                    request_id=req_id,
                    uuid=uuid_,
                    session_id=entry.get('sessionId'),
                    model=model,
                    input_tokens=inp,
                    cache_creation_5m=cc_5m,
                    cache_creation_1h=cc_1h,
                    cache_read=cr,
                    output_tokens=out,
                    is_sidechain=bool(entry.get('isSidechain')),
                    tool_uses=tool_uses,
                )
                turns.append(turn)
                if key is not None:
                    seen[key] = turn
    except (OSError, PermissionError):
        pass
    if project_name:
        # `cwd` can appear after the first assistant turn, which then kept
        # 'Unknown' while the rest of the same session got the real name.
        for t in turns:
            if t.project == 'Unknown':
                t.project = project_name
    return project_name, turns


def iter_project_dirs(root: Optional[Path] = None) -> Iterator[Path]:
    base = root or CLAUDE_DIR
    if not base.exists():
        return
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name.startswith('-'):
            yield d
