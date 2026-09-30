import sqlite3
from datetime import datetime, timezone

from cct.parser import Turn
from cct.pricing import load_rate_card
from cct.store import Store


def _turn(msg_id, model='claude-sonnet-4-6'):
    return Turn(
        timestamp=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        project='p', msg_id=msg_id, request_id=None, uuid=None,
        session_id='s', model=model, input_tokens=10, cache_creation_5m=0,
        cache_creation_1h=0, cache_read=0, output_tokens=5,
        is_sidechain=False)


def test_bedrock_turns_are_told_apart_by_message_id():
    assert _turn('msg_bdrk_01abc').is_bedrock
    assert not _turn('msg_01abc').is_bedrock
    assert not _turn(None).is_bedrock


def test_bedrock_turns_are_filed_under_their_own_account(tmp_path):
    store = Store(tmp_path / 'h.db')
    store.upsert_turns([_turn('msg_bdrk_1'), _turn('msg_01')],
                       load_rate_card(), account='default')
    assert store.has_account_rows('bedrock')
    assert store.totals(account='bedrock')['messages'] == 1
    assert store.totals(account='default')['messages'] == 1


def test_no_bedrock_rows_means_no_bedrock_account(tmp_path):
    store = Store(tmp_path / 'h.db')
    store.upsert_turns([_turn('msg_01')], load_rate_card(), account='default')
    assert not store.has_account_rows('bedrock')


def test_rows_imported_before_the_label_existed_are_relabelled(tmp_path):
    path = tmp_path / 'h.db'
    Store(path).close()
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO messages(msg_key, timestamp, account, project, session_id,"
        " model, input_tokens, cache_creation_5m, cache_creation_1h, cache_read,"
        " output_tokens, cost_usd, is_sidechain) VALUES"
        " ('msg_bdrk_old', '2026-09-30T12:00:00+00:00', 'default', 'p', 's',"
        " 'claude-sonnet-4-6', 1, 0, 0, 0, 1, 0.0, 0)")
    con.commit()
    con.close()
    store = Store(path)          # opening the store runs the relabel
    assert store.totals(account='bedrock')['messages'] == 1
    assert store.totals(account='default')['messages'] == 0


def test_hop_usage_is_one_row_per_account(tmp_path):
    store = Store(tmp_path / 'h.db')
    store.save_hop_usage('work', 34, 61, 'r5', 'r7')
    store.save_hop_usage('work', 50, 62, 'r5b', 'r7b')
    seen = store.hop_usage()
    assert list(seen) == ['work']
    assert (seen['work']['pct5'], seen['work']['resets_5h']) == (50, 'r5b')
