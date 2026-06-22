from __future__ import annotations
import json as _json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .config import DB_FILE, DEFAULT_ACCOUNT_LABEL
from .parser import Turn
from .pricing import RateCard

SCHEMA_VERSION = 2

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    msg_key TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT 'default',
    project TEXT NOT NULL,
    session_id TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    tool_uses_json TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    period TEXT NOT NULL,
    limit_usd REAL,
    limit_tokens INTEGER,
    limit_pct REAL,
    notify_at_pct INTEGER NOT NULL DEFAULT 80,
    last_notified_pct INTEGER NOT NULL DEFAULT 0,
    last_notified_period TEXT
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);
"""

VALID_PERIODS = ('day', 'week', 'month', '5h', '7d')


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA_TABLES)
            bcols = {r['name'] for r in c.execute(
                "PRAGMA table_info(budgets)").fetchall()}
            if 'limit_pct' not in bcols:
                c.execute("ALTER TABLE budgets ADD COLUMN limit_pct REAL")
            mcols = {r['name'] for r in c.execute(
                "PRAGMA table_info(messages)").fetchall()}
            if 'account' not in mcols:
                c.execute(
                    "ALTER TABLE messages ADD COLUMN account TEXT "
                    "NOT NULL DEFAULT 'default'"
                )
            c.executescript(SCHEMA_INDEXES)
            row = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                c.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=15.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ── Ingest ──────────────────────────────────────────────────────────────

    def upsert_turns(self, turns: Iterable[Turn], rate_card: RateCard,
                     account: str = DEFAULT_ACCOUNT_LABEL) -> int:
        rows = []
        for t in turns:
            cost = rate_card.cost(
                t.model, t.input_tokens, t.cache_creation_5m,
                t.cache_creation_1h, t.cache_read, t.output_tokens,
            )
            rows.append((
                t.dedup_key,
                t.timestamp.astimezone(timezone.utc).isoformat(),
                account,
                t.project,
                t.session_id,
                t.model,
                t.input_tokens,
                t.cache_creation_5m,
                t.cache_creation_1h,
                t.cache_read,
                t.output_tokens,
                cost,
                int(t.is_sidechain),
                _json.dumps(t.tool_uses) if t.tool_uses else None,
            ))
        if not rows:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                """INSERT OR IGNORE INTO messages(
                    msg_key, timestamp, account, project, session_id, model,
                    input_tokens, cache_creation_5m, cache_creation_1h,
                    cache_read, output_tokens, cost_usd, is_sidechain,
                    tool_uses_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            return cur.rowcount or 0

    def reprice_all(self, rate_card: RateCard) -> int:
        with self._conn() as c:
            cur = c.execute(
                """SELECT msg_key, model, input_tokens, cache_creation_5m,
                          cache_creation_1h, cache_read, output_tokens
                   FROM messages"""
            )
            updates = []
            for row in cur.fetchall():
                cost = rate_card.cost(
                    row['model'],
                    row['input_tokens'],
                    row['cache_creation_5m'],
                    row['cache_creation_1h'],
                    row['cache_read'],
                    row['output_tokens'],
                )
                updates.append((cost, row['msg_key']))
            if updates:
                c.executemany(
                    "UPDATE messages SET cost_usd = ? WHERE msg_key = ?",
                    updates,
                )
            return len(updates)

    # ── Queries ─────────────────────────────────────────────────────────────

    def query(self, since: Optional[datetime] = None,
              until: Optional[datetime] = None,
              project: Optional[str] = None,
              model: Optional[str] = None,
              account: Optional[str] = None,
              include_sidechain: bool = True) -> List[sqlite3.Row]:
        sql = "SELECT * FROM messages WHERE 1=1"
        args: List = []
        if since:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if until:
            sql += " AND timestamp < ?"
            args.append(until.astimezone(timezone.utc).isoformat())
        if project:
            sql += " AND project = ?"
            args.append(project)
        if model:
            sql += " AND model = ?"
            args.append(model)
        if account:
            sql += " AND account = ?"
            args.append(account)
        if not include_sidechain:
            sql += " AND is_sidechain = 0"
        sql += " ORDER BY timestamp ASC"
        with self._conn() as c:
            return list(c.execute(sql, args).fetchall())

    def project_summary(self, since: Optional[datetime] = None,
                        account: Optional[str] = None) -> List[Dict]:
        sql = """
        SELECT project,
               COUNT(*) AS messages,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(input_tokens) AS input_tokens,
               SUM(cache_creation_5m + cache_creation_1h) AS cache_creation,
               SUM(cache_read) AS cache_read,
               SUM(output_tokens) AS output_tokens,
               SUM(input_tokens + cache_creation_5m + cache_creation_1h
                   + cache_read + output_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd,
               MAX(timestamp) AS last_used
        FROM messages
        WHERE 1=1
        """
        args: List = []
        if since:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if account:
            sql += " AND account = ?"
            args.append(account)
        sql += " GROUP BY project ORDER BY total_tokens DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def model_summary(self, since: Optional[datetime] = None,
                      account: Optional[str] = None) -> List[Dict]:
        sql = """
        SELECT COALESCE(model, '(unknown)') AS model,
               COUNT(*) AS messages,
               SUM(input_tokens) AS input_tokens,
               SUM(cache_creation_5m + cache_creation_1h) AS cache_creation,
               SUM(cache_read) AS cache_read,
               SUM(output_tokens) AS output_tokens,
               SUM(input_tokens + cache_creation_5m + cache_creation_1h
                   + cache_read + output_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd
        FROM messages
        WHERE 1=1
        """
        args: List = []
        if since:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if account:
            sql += " AND account = ?"
            args.append(account)
        sql += " GROUP BY model ORDER BY cost_usd DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def account_summary(self, since: Optional[datetime] = None) -> List[Dict]:
        sql = """
        SELECT account,
               COUNT(*) AS messages,
               COUNT(DISTINCT project) AS projects,
               SUM(input_tokens + cache_creation_5m + cache_creation_1h
                   + cache_read + output_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd,
               MAX(timestamp) AS last_used
        FROM messages
        WHERE 1=1
        """
        args: List = []
        if since:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        sql += " GROUP BY account ORDER BY cost_usd DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def tool_summary(self, since: Optional[datetime] = None,
                     account: Optional[str] = None) -> List[Dict]:
        rows = self.query(since=since, account=account)
        counts: Dict[str, Dict[str, float]] = {}
        for r in rows:
            raw = r['tool_uses_json']
            if not raw:
                continue
            try:
                tools = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            for name, n in tools.items():
                bucket = counts.setdefault(name, {
                    'calls': 0, 'messages': 0,
                    'input_tokens': 0, 'output_tokens': 0,
                    'cache_tokens': 0, 'cost_usd': 0.0,
                })
                bucket['calls'] += n
                bucket['messages'] += 1
                bucket['input_tokens'] += r['input_tokens']
                bucket['output_tokens'] += r['output_tokens']
                bucket['cache_tokens'] += (r['cache_creation_5m']
                                           + r['cache_creation_1h']
                                           + r['cache_read'])
                bucket['cost_usd'] += r['cost_usd']
        result = [{'name': k, **v} for k, v in counts.items()]
        result.sort(key=lambda x: x['cost_usd'], reverse=True)
        return result

    def daily_series(self, days: int = 30,
                     account: Optional[str] = None) -> List[Dict]:
        # Snap the cutoff to the start of the earliest day (UTC) so the
        # leftmost day bucket is a full day, not a partial one truncated at
        # the current wall-clock time-of-day.
        earliest = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        cutoff = datetime(earliest.year, earliest.month, earliest.day,
                          tzinfo=timezone.utc).isoformat()
        sql = """
        SELECT substr(timestamp, 1, 10) AS day,
               SUM(input_tokens) AS input_tokens,
               SUM(cache_creation_5m + cache_creation_1h) AS cache_creation,
               SUM(cache_read) AS cache_read,
               SUM(output_tokens) AS output_tokens,
               SUM(input_tokens + cache_creation_5m + cache_creation_1h
                   + cache_read + output_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd,
               COUNT(*) AS messages
        FROM messages
        WHERE timestamp >= ?
        """
        args: List = [cutoff]
        if account:
            sql += " AND account = ?"
            args.append(account)
        sql += " GROUP BY day ORDER BY day"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def total_cost(self, since: Optional[datetime] = None,
                   project: Optional[str] = None,
                   model: Optional[str] = None,
                   account: Optional[str] = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM messages WHERE 1=1"
        args: List = []
        if since:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if project:
            sql += " AND project = ?"
            args.append(project)
        if model:
            sql += " AND model = ?"
            args.append(model)
        if account:
            sql += " AND account = ?"
            args.append(account)
        with self._conn() as c:
            return float(c.execute(sql, args).fetchone()[0])

    def message_count(self, account: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM messages"
        args: List = []
        if account:
            sql += " WHERE account = ?"
            args.append(account)
        with self._conn() as c:
            return int(c.execute(sql, args).fetchone()[0])

    # ── Budgets ─────────────────────────────────────────────────────────────

    def list_budgets(self) -> List[Dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM budgets ORDER BY id"
            ).fetchall()]

    def add_budget(self, name: str, scope: str, period: str,
                   limit_usd: Optional[float],
                   limit_tokens: Optional[int],
                   notify_at_pct: int = 80,
                   limit_pct: Optional[float] = None) -> int:
        if not (limit_usd or limit_tokens or limit_pct):
            raise ValueError("Budget needs a USD, token, or plan-% limit")
        if period not in VALID_PERIODS:
            raise ValueError(f"Bad period: {period}")
        if limit_pct is not None:
            if period not in ('5h', '7d'):
                raise ValueError(
                    "Plan-utilization budgets require period '5h' or '7d'")
            if scope != 'global':
                raise ValueError(
                    "Plan-utilization budgets must be scope 'global'")
            if not (0 < limit_pct <= 100):
                raise ValueError("limit_pct must be between 0 and 100")
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO budgets(name, scope, period, limit_usd,
                                       limit_tokens, limit_pct, notify_at_pct)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, scope, period, limit_usd, limit_tokens, limit_pct,
                 notify_at_pct),
            )
            return cur.lastrowid

    def delete_budget(self, budget_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))

    def update_budget_notification(self, budget_id: int,
                                   pct: int, period_key: str) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE budgets
                   SET last_notified_pct = ?, last_notified_period = ?
                   WHERE id = ?""",
                (pct, period_key, budget_id),
            )
