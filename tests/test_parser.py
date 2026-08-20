import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cct.parser import parse_jsonl, parse_timestamp


def _write_jsonl(path: Path, lines):
    with open(path, 'w', encoding='utf-8') as f:
        for entry in lines:
            f.write(json.dumps(entry) + '\n')


def test_parse_assistant_turn(tmp_path):
    p = tmp_path / 'session.jsonl'
    _write_jsonl(p, [
        {'type': 'system', 'cwd': str(tmp_path / 'my-proj')},
        {
            'type': 'assistant',
            'timestamp': '2026-04-28T10:00:00Z',
            'sessionId': 's-1',
            'requestId': 'r-1',
            'uuid': 'u-1',
            'isSidechain': False,
            'message': {
                'id': 'msg_001',
                'model': 'claude-opus-4-7',
                'usage': {
                    'input_tokens': 100,
                    'output_tokens': 200,
                    'cache_creation_input_tokens': 50,
                    'cache_creation': {
                        'ephemeral_5m_input_tokens': 30,
                        'ephemeral_1h_input_tokens': 20,
                    },
                    'cache_read_input_tokens': 10,
                },
                'content': [
                    {'type': 'text', 'text': 'hi'},
                    {'type': 'tool_use', 'name': 'Bash'},
                    {'type': 'tool_use', 'name': 'Read'},
                ],
            },
        },
    ])
    label, turns = parse_jsonl(p)
    assert label == 'my-proj'
    assert len(turns) == 1
    t = turns[0]
    assert t.input_tokens == 100
    assert t.output_tokens == 200
    assert t.cache_creation_5m == 30
    assert t.cache_creation_1h == 20
    assert t.cache_read == 10
    assert t.model == 'claude-opus-4-7'
    assert t.tool_uses == {'Bash': 1, 'Read': 1}
    assert t.session_id == 's-1'
    assert t.is_sidechain is False


def test_dedup_on_message_id(tmp_path):
    p = tmp_path / 's.jsonl'
    msg = {
        'type': 'assistant',
        'timestamp': '2026-04-28T10:00:00Z',
        'sessionId': 's-1',
        'message': {
            'id': 'msg_dup',
            'model': 'claude-sonnet-4-6',
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        },
    }
    _write_jsonl(p, [msg, msg, msg])
    _, turns = parse_jsonl(p)
    assert len(turns) == 1


def test_skips_zero_usage(tmp_path):
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        {'type': 'assistant', 'timestamp': '2026-04-28T10:00:00Z',
         'message': {'id': 'm1', 'usage': {}}}
    ])
    _, turns = parse_jsonl(p)
    assert turns == []


def test_cache_creation_split_fallback_to_5m(tmp_path):
    """Older logs lack the cache_creation breakdown — assume 5m."""
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        {'type': 'assistant', 'timestamp': '2026-04-28T10:00:00Z',
         'message': {'id': 'm1', 'model': 'claude-sonnet-4-6',
                     'usage': {'input_tokens': 0, 'output_tokens': 1,
                               'cache_creation_input_tokens': 100}}}
    ])
    _, turns = parse_jsonl(p)
    assert turns[0].cache_creation_5m == 100
    assert turns[0].cache_creation_1h == 0


def test_parse_timestamp_formats():
    assert parse_timestamp('2026-04-28T10:00:00Z') is not None
    assert parse_timestamp('2026-04-28T10:00:00+00:00') is not None
    assert parse_timestamp(1714296000000) is not None  # ms since epoch
    assert parse_timestamp(None) is None
    assert parse_timestamp('') is None
    assert parse_timestamp('not a date') is None


def test_non_dict_and_wrong_type_lines_skipped_not_crashing(tmp_path):
    """A valid-JSON but non-object line (null / number / list / string), or an
    assistant entry whose message/usage fields are the wrong type, must be
    skipped without aborting the whole file (regression: these used to raise an
    uncaught AttributeError and drop every turn in the session)."""
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        None,                                    # JSON null
        123,                                     # bare number
        [1, 2, 3],                               # array
        "just a string",                         # string
        {'type': 'assistant', 'message': 'not-a-dict'},
        {'type': 'assistant',
         'message': {'id': 'm0', 'usage': 'not-a-dict'}},
        {'type': 'assistant', 'cwd': 12345,      # non-string cwd
         'timestamp': '2026-04-28T10:00:00Z',
         'message': {'id': 'm-good', 'model': 'claude-sonnet-4-6',
                     'usage': {'input_tokens': 5, 'output_tokens': 7}}},
    ])
    label, turns = parse_jsonl(p)
    # Only the last, well-formed assistant turn survives.
    assert len(turns) == 1
    assert turns[0].input_tokens == 5
    assert turns[0].output_tokens == 7


def _assistant(msg_id, out, tools=(), ts='2026-08-20T10:00:00Z',
               inp=2, cc=13028, cr=0):
    return {
        'type': 'assistant',
        'timestamp': ts,
        'sessionId': 's-1',
        'message': {
            'id': msg_id,
            'model': 'claude-opus-5',
            'usage': {
                'input_tokens': inp,
                'output_tokens': out,
                'cache_creation_input_tokens': cc,
                'cache_read_input_tokens': cr,
            },
            'content': ([{'type': 'text', 'text': 'x'}]
                        + [{'type': 'tool_use', 'name': t} for t in tools]),
        },
    }


def test_repeated_entries_keep_the_completed_output_count(tmp_path):
    """Measured in real logs: the earlier entries of one response carry a
    placeholder output count and only the last carries the true one. Keeping
    the first lost 5.44M output tokens across 5,163 messages."""
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        {'type': 'system', 'cwd': str(tmp_path / 'proj')},
        _assistant('msg_1', 4),
        _assistant('msg_1', 4),
        _assistant('msg_1', 568),
    ])
    _, turns = parse_jsonl(p)
    assert len(turns) == 1
    assert turns[0].output_tokens == 568


def test_repeated_entries_are_not_summed(tmp_path):
    """They restate one response's usage; adding them up would triple it."""
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        {'type': 'system', 'cwd': str(tmp_path / 'proj')},
        _assistant('msg_1', 100),
        _assistant('msg_1', 100),
        _assistant('msg_1', 100),
    ])
    _, turns = parse_jsonl(p)
    assert turns[0].output_tokens == 100
    assert turns[0].input_tokens == 2
    assert turns[0].cache_creation_5m == 13028


def test_repeated_entries_merge_their_tool_calls(tmp_path):
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        {'type': 'system', 'cwd': str(tmp_path / 'proj')},
        _assistant('msg_1', 4, tools=()),
        _assistant('msg_1', 4, tools=('Bash',)),
        _assistant('msg_1', 4, tools=('Bash', 'Read')),
        _assistant('msg_1', 900, tools=('Edit',)),
    ])
    _, turns = parse_jsonl(p)
    assert len(turns) == 1
    assert turns[0].tool_uses == {'Bash': 2, 'Read': 1, 'Edit': 1}
    assert turns[0].output_tokens == 900


def test_a_missing_timestamp_never_becomes_now(tmp_path):
    """"Now" would drop a historical turn into the live 5-hour window."""
    p = tmp_path / 's.jsonl'
    entry = _assistant('msg_2', 50)
    del entry['timestamp']
    _write_jsonl(p, [
        {'type': 'system', 'cwd': str(tmp_path / 'proj')},
        _assistant('msg_1', 50, ts='2026-01-01T00:00:00Z'),
        entry,
    ])
    _, turns = parse_jsonl(p)
    assert len(turns) == 2
    assert turns[1].timestamp == turns[0].timestamp


def test_a_first_entry_with_no_timestamp_falls_back_to_the_file(tmp_path):
    p = tmp_path / 's.jsonl'
    entry = _assistant('msg_1', 50)
    del entry['timestamp']
    _write_jsonl(p, [{'type': 'system', 'cwd': str(tmp_path / 'proj')}, entry])
    _, turns = parse_jsonl(p)
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    assert turns[0].timestamp == mtime


def test_project_backfills_when_cwd_appears_late(tmp_path):
    """cwd can land after the first assistant turn, which used to keep
    'Unknown' while the rest of the same session got the real name."""
    p = tmp_path / 's.jsonl'
    _write_jsonl(p, [
        _assistant('msg_1', 50),
        {'type': 'system', 'cwd': str(tmp_path / 'late-proj')},
        _assistant('msg_2', 60),
    ])
    project, turns = parse_jsonl(p)
    assert project == 'late-proj'
    assert [t.project for t in turns] == ['late-proj', 'late-proj']
