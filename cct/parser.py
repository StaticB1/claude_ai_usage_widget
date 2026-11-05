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
    def is_bedrock(self) -> bool:
        """Claude Code on Amazon Bedrock logs ordinary model names such as
        ``claude-sonnet-4-6``; the only tell is the response id, which Bedrock
        prefixes ``msg_bdrk_`` where Anthropic uses ``msg_01``."""
        return (self.msg_id or '').startswith('msg_bdrk_')

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

    The whole file, including a last line with no newline; ``scan_jsonl`` is
    the resumable form the tracker's scans use."""
    project, turns, _off, _st = scan_jsonl(path, final=True)
    return project, turns


# How many recent responses a resumable scan remembers between reads. Claude
# Code writes a response's repeat entries one after another, so a read that
# stops between two of them needs only the last few to merge the rest.
RECENT_TURNS = 16

# A line can only hold a response's usage when it has both of these. Testing
# the raw bytes first means the large lines (tool output, file contents,
# compacted history) are never decoded, which is most of a session log's
# bytes: on a 1.8 GB log, 244 MB of lines pass.
_NEEDLES = (b'"usage"', b'"assistant"')


def _turn_to_state(t: Turn) -> dict:
    return {'ts': t.timestamp.isoformat(), 'project': t.project, 'msg_id': t.msg_id,
            'request_id': t.request_id, 'uuid': t.uuid, 'session_id': t.session_id,
            'model': t.model, 'in': t.input_tokens, 'c5': t.cache_creation_5m,
            'c1': t.cache_creation_1h, 'cr': t.cache_read, 'out': t.output_tokens,
            'side': t.is_sidechain, 'tools': dict(t.tool_uses)}


def _turn_from_state(d: dict) -> Optional[Turn]:
    ts = parse_timestamp(d.get('ts'))
    if ts is None:
        return None
    return Turn(timestamp=ts, project=d.get('project') or 'Unknown',
                msg_id=d.get('msg_id'), request_id=d.get('request_id'),
                uuid=d.get('uuid'), session_id=d.get('session_id'),
                model=d.get('model'), input_tokens=int(d.get('in') or 0),
                cache_creation_5m=int(d.get('c5') or 0),
                cache_creation_1h=int(d.get('c1') or 0),
                cache_read=int(d.get('cr') or 0), output_tokens=int(d.get('out') or 0),
                is_sidechain=bool(d.get('side')),
                tool_uses={str(k): int(v) for k, v in (d.get('tools') or {}).items()})


def _decode(line: bytes):
    try:
        return json.loads(line)
    except UnicodeDecodeError:
        try:
            return json.loads(line.decode('utf-8', errors='ignore'))
        except ValueError:
            return None
    except ValueError:
        return None


def scan_jsonl(path: Path, offset: int = 0, state: Optional[dict] = None,
               max_bytes: Optional[int] = None, final: bool = False
               ) -> Tuple[Optional[str], List[Turn], int, dict]:
    """Read one Claude Code session JSONL from byte ``offset``.

    Returns ``(project_label, turns, new_offset, new_state)``. Pass the offset
    and state back to read only what was appended since. A last line without a
    newline is one Claude Code is still writing: it is left for the next read
    unless it already decodes, or ``final`` says the file is complete.
    ``max_bytes`` stops after about that much, so a huge log is read in parts.

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

    So a repeat entry merges its tool counts into the turn already recorded
    and raises each token count to the highest seen for that id. A response
    whose entries straddle two reads is merged through the state, and the
    merged turn is returned again so the stored row is complete.
    """
    st = state or {}
    project_name: Optional[str] = st.get('project')
    turns: List[Turn] = []
    emitted: set = set()
    seen: Dict[str, Turn] = {}
    for d in st.get('recent') or []:
        t = _turn_from_state(d) if isinstance(d, dict) else None
        if t is not None:
            seen[t.msg_id or t.request_id or t.uuid or ''] = t
    seen.pop('', None)
    last_ts: Optional[datetime] = parse_timestamp(st.get('last_ts'))
    pos = offset
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            while True:
                if max_bytes and pos - offset >= max_bytes:
                    break
                raw = f.readline()
                if not raw:
                    break
                complete = raw.endswith(b'\n')
                start = pos
                pos += len(raw)
                line = raw.strip()
                if not line:
                    continue
                wanted = all(n in line for n in _NEEDLES)
                if project_name is None and b'"cwd"' in line:
                    wanted = True
                if not wanted:
                    if not complete and not final:
                        pos = start            # may still be growing; read it again
                        break
                    continue
                entry = _decode(line)
                if entry is None and not complete and not final:
                    pos = start
                    break
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
                        if id(prior) not in emitted:   # first seen in an earlier read
                            turns.append(prior)
                            emitted.add(id(prior))
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
                emitted.add(id(turn))
                if key is not None:
                    seen.pop(key, None)
                    seen[key] = turn
    except (OSError, PermissionError):
        pass
    if project_name:
        # `cwd` can appear after the first assistant turn, which then kept
        # 'Unknown' while the rest of the same session got the real name.
        for t in turns:
            if t.project == 'Unknown':
                t.project = project_name
    recent = list(seen.values())[-RECENT_TURNS:]
    new_state = {'project': project_name,
                 'last_ts': last_ts.isoformat() if last_ts else None,
                 'recent': [_turn_to_state(t) for t in recent]}
    return project_name, turns, pos, new_state


def iter_project_dirs(root: Optional[Path] = None) -> Iterator[Path]:
    base = root or CLAUDE_DIR
    if not base.exists():
        return
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name.startswith('-'):
            yield d
