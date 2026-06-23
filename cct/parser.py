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
    def tool_count(self) -> int:
        return sum(self.tool_uses.values())

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


def parse_jsonl(path: Path) -> Tuple[Optional[str], List[Turn]]:
    """Parse one Claude Code session JSONL. Returns (project_label, turns).

    Claude Code may emit duplicate copies of the same assistant message — we
    dedup on `message.id`, falling back to requestId / uuid for older logs.
    """
    project_name: Optional[str] = None
    turns: List[Turn] = []
    seen: set = set()
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

                if project_name is None:
                    cwd = entry.get('cwd')
                    if cwd:
                        project_name = os.path.basename(cwd)

                if entry.get('type') != 'assistant':
                    continue

                msg = entry.get('message') or {}
                usage = msg.get('usage') or {}
                inp = usage.get('input_tokens', 0) or 0
                cc_total = usage.get('cache_creation_input_tokens', 0) or 0
                cr = usage.get('cache_read_input_tokens', 0) or 0
                out = usage.get('output_tokens', 0) or 0
                model = msg.get('model')

                cc_split = usage.get('cache_creation') or {}
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
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)

                ts = parse_timestamp(entry.get('timestamp')) \
                    or datetime.now(timezone.utc)

                turns.append(Turn(
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
                    tool_uses=_extract_tool_uses(msg),
                ))
    except (OSError, PermissionError):
        pass
    return project_name, turns


def iter_project_dirs(root: Optional[Path] = None) -> Iterator[Path]:
    base = root or CLAUDE_DIR
    if not base.exists():
        return
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name.startswith('-'):
            yield d


def fallback_label(proj_dir: Path) -> str:
    name = proj_dir.name[1:] if proj_dir.name.startswith('-') else proj_dir.name
    if '-' in name:
        name = name.split('-', 1)[1]
    return name or 'Unknown'
