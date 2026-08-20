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

# 4: rows are upserted rather than insert-or-ignored, so a parser or
#    rate-card correction repairs history on the next scan instead of being
#    masked by the rows already stored. Migrating to it drops the scan
#    signatures (forcing one full re-read) and reprices everything.
SCHEMA_VERSION = 4

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS file_scan (
    account TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    PRIMARY KEY (account, path)
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
CREATE INDEX IF NOT EXISTS idx_messages_account_ts
    ON messages(account, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);
"""

VALID_PERIODS = ('day', 'week', 'month', '5h', '7d')

# Every aggregate query sums the same five token columns; spelling that out
# once keeps the SQL statements below honest with each other.
_TOKEN_SUMS = """
       COALESCE(SUM(input_tokens), 0) AS input_tokens,
       COALESCE(SUM(cache_creation_5m + cache_creation_1h), 0)
           AS cache_creation,
       COALESCE(SUM(cache_read), 0) AS cache_read,
       COALESCE(SUM(output_tokens), 0) AS output_tokens,
       COALESCE(SUM(input_tokens + cache_creation_5m + cache_creation_1h
                    + cache_read + output_tokens), 0) AS total_tokens,
       COALESCE(SUM(cost_usd), 0) AS cost_usd,
       COUNT(*) AS messages
"""


class Store:
    """SQLite-backed message store.

    Connections are per-thread and kept open. Reopening the database on every
    call meant re-running ``PRAGMA journal_mode=WAL`` (a write) hundreds of
    times per dashboard refresh; the GUI polls this from two threads, so the
    handle is thread-local rather than shared.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init()

    # ── Connection handling ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        with self._lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── Schema ──────────────────────────────────────────────────────────────

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
            try:
                prev = int(row['value']) if row else 0
            except (TypeError, ValueError):
                prev = 0
            if prev and prev < SCHEMA_VERSION:
                # Rows written by an earlier version carry under-counted tool
                # attribution and costs from a stale rate card. Forget the
                # per-file scan signatures so the next scan re-reads every log
                # and upserts corrected values over them.
                c.execute("DELETE FROM file_scan")
            c.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._migrated_from = prev

    def needs_reprice(self) -> bool:
        """True when this process opened a database written by an older
        version, whose stored costs were computed from a stale rate card."""
        prev = getattr(self, '_migrated_from', 0)
        return bool(prev) and prev < SCHEMA_VERSION

    # ── Ingest ──────────────────────────────────────────────────────────────

    def upsert_turns(self, turns: Iterable[Turn], rate_card: RateCard,
                     account: str = DEFAULT_ACCOUNT_LABEL) -> int:
        """Insert or refresh rows for ``turns``. Returns the number written.

        This updates on conflict rather than ignoring, so re-reading a log
        after a parser or pricing fix corrects the stored row instead of
        leaving the first (wrong) version in place forever.
        """
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
            c.executemany(
                """INSERT INTO messages(
                    msg_key, timestamp, account, project, session_id, model,
                    input_tokens, cache_creation_5m, cache_creation_1h,
                    cache_read, output_tokens, cost_usd, is_sidechain,
                    tool_uses_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(msg_key) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    account=excluded.account,
                    project=excluded.project,
                    session_id=excluded.session_id,
                    model=excluded.model,
                    input_tokens=excluded.input_tokens,
                    cache_creation_5m=excluded.cache_creation_5m,
                    cache_creation_1h=excluded.cache_creation_1h,
                    cache_read=excluded.cache_read,
                    output_tokens=excluded.output_tokens,
                    cost_usd=excluded.cost_usd,
                    is_sidechain=excluded.is_sidechain,
                    tool_uses_json=excluded.tool_uses_json""",
                rows,
            )
            return len(rows)

    # ── Incremental-scan bookkeeping ─────────────────────────────────────────

    def scanned_file_map(self, account: str) -> Dict[str, tuple]:
        """Return ``{path: (size, mtime)}`` for files already imported for
        ``account``. Used to skip re-parsing unchanged session logs."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT path, size, mtime FROM file_scan WHERE account=?",
                (account,),
            ).fetchall()
        return {r['path']: (r['size'], r['mtime']) for r in rows}

    def mark_files_scanned(self, account: str,
                           items: Iterable[tuple]) -> None:
        """Record ``(path, size, mtime)`` signatures for freshly-parsed files
        so the next scan can skip them while they're unchanged."""
        rows = [(account, str(p), int(sz), float(mt)) for p, sz, mt in items]
        if not rows:
            return
        with self._conn() as c:
            c.executemany(
                """INSERT INTO file_scan(account, path, size, mtime)
                   VALUES(?,?,?,?)
                   ON CONFLICT(account, path)
                   DO UPDATE SET size=excluded.size, mtime=excluded.mtime""",
                rows,
            )

    def forget_scans(self, account: Optional[str] = None) -> None:
        """Drop scan signatures so the next scan re-reads the logs."""
        with self._conn() as c:
            if account:
                c.execute("DELETE FROM file_scan WHERE account=?", (account,))
            else:
                c.execute("DELETE FROM file_scan")

    def reprice_all(self, rate_card: RateCard) -> int:
        """Recompute every stored cost against ``rate_card``.

        Grouped by model so one UPDATE covers every row of a model rather than
        one statement per row.
        """
        with self._conn() as c:
            models = [r[0] for r in c.execute(
                "SELECT DISTINCT model FROM messages").fetchall()]
            total = 0
            for model in models:
                p_in, p_5m, p_1h, p_cr, p_out = rate_card.for_model(model)
                where = "model IS ?" if model is None else "model = ?"
                cur = c.execute(
                    f"""UPDATE messages SET cost_usd =
                        (input_tokens * ? + cache_creation_5m * ?
                         + cache_creation_1h * ? + cache_read * ?
                         + output_tokens * ?) / 1000000.0
                        WHERE {where}""",
                    (p_in, p_5m, p_1h, p_cr, p_out, model),
                )
                total += cur.rowcount or 0
            return total

    # ── Queries ─────────────────────────────────────────────────────────────

    @staticmethod
    def _filters(project=None, model=None, account=None,
                 since=None, until=None,
                 include_sidechain=True) -> tuple:
        sql = ""
        args: List = []
        if since is not None:
            sql += " AND timestamp >= ?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
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
        return sql, args

    def query(self, since: Optional[datetime] = None,
              until: Optional[datetime] = None,
              project: Optional[str] = None,
              model: Optional[str] = None,
              account: Optional[str] = None,
              include_sidechain: bool = True) -> List[sqlite3.Row]:
        """Raw rows. Prefer ``totals`` for summing — pulling 100k+ rows into
        Python to add up five columns is what used to stall the dashboard."""
        where, args = self._filters(project, model, account, since, until,
                                    include_sidechain)
        sql = "SELECT * FROM messages WHERE 1=1" + where + \
            " ORDER BY timestamp ASC"
        with self._conn() as c:
            return list(c.execute(sql, args).fetchall())

    def totals(self, since: Optional[datetime] = None,
               until: Optional[datetime] = None,
               project: Optional[str] = None,
               model: Optional[str] = None,
               account: Optional[str] = None,
               include_sidechain: bool = True) -> Dict:
        """One row of summed tokens, cost, message and project counts."""
        where, args = self._filters(project, model, account, since, until,
                                    include_sidechain)
        sql = (f"SELECT {_TOKEN_SUMS}, "
               "COUNT(DISTINCT project) AS projects, "
               "COUNT(DISTINCT session_id) AS sessions, "
               "MAX(timestamp) AS last_used "
               "FROM messages WHERE 1=1" + where)
        with self._conn() as c:
            row = c.execute(sql, args).fetchone()
        return dict(row) if row else {}

    def project_summary(self, since: Optional[datetime] = None,
                        until: Optional[datetime] = None,
                        account: Optional[str] = None) -> List[Dict]:
        where, args = self._filters(account=account, since=since, until=until)
        sql = (f"SELECT project, COUNT(DISTINCT session_id) AS sessions, "
               f"MAX(timestamp) AS last_used, {_TOKEN_SUMS} "
               "FROM messages WHERE 1=1" + where +
               " GROUP BY project ORDER BY total_tokens DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def model_summary(self, since: Optional[datetime] = None,
                      until: Optional[datetime] = None,
                      account: Optional[str] = None) -> List[Dict]:
        where, args = self._filters(account=account, since=since, until=until)
        sql = (f"SELECT COALESCE(model, '(unknown)') AS model, {_TOKEN_SUMS} "
               "FROM messages WHERE 1=1" + where +
               " GROUP BY model ORDER BY cost_usd DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def account_summary(self, since: Optional[datetime] = None,
                        until: Optional[datetime] = None) -> List[Dict]:
        where, args = self._filters(since=since, until=until)
        sql = (f"SELECT account, COUNT(DISTINCT project) AS projects, "
               f"MAX(timestamp) AS last_used, {_TOKEN_SUMS} "
               "FROM messages WHERE 1=1" + where +
               " GROUP BY account ORDER BY cost_usd DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def session_summary(self, since: Optional[datetime] = None,
                        until: Optional[datetime] = None,
                        account: Optional[str] = None) -> List[Dict]:
        """Per-session totals, one row per (project, session).

        The Projects view used to read every message for the period and group
        them by session in Python — 167k rows on the interface thread for the
        all-time view, on every refresh. There are a few thousand sessions,
        and SQLite groups them in a fraction of the time.
        """
        where, args = self._filters(account=account, since=since, until=until)
        sql = ("SELECT project, "
               "COALESCE(session_id, '(no-session)') AS session_id, "
               "MIN(timestamp) AS first_used, MAX(timestamp) AS last_used, "
               f"{_TOKEN_SUMS} "
               "FROM messages WHERE 1=1" + where +
               " GROUP BY project, session_id ORDER BY last_used DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def tool_summary(self, since: Optional[datetime] = None,
                     until: Optional[datetime] = None,
                     account: Optional[str] = None) -> List[Dict]:
        """Per-tool attribution. Reads only the columns it needs and skips
        rows with no tool JSON in SQL, instead of pulling every column of
        every row and discarding most of them in Python."""
        where, args = self._filters(account=account, since=since, until=until)
        sql = ("SELECT tool_uses_json, input_tokens, output_tokens, "
               "cache_creation_5m, cache_creation_1h, cache_read, cost_usd "
               "FROM messages WHERE tool_uses_json IS NOT NULL" + where)
        counts: Dict[str, Dict[str, float]] = {}
        with self._conn() as c:
            for r in c.execute(sql, args):
                try:
                    tools = _json.loads(r['tool_uses_json'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(tools, dict):
                    continue
                for name, n in tools.items():
                    bucket = counts.setdefault(str(name), {
                        'calls': 0, 'messages': 0,
                        'input_tokens': 0, 'output_tokens': 0,
                        'cache_tokens': 0, 'cost_usd': 0.0,
                    })
                    bucket['calls'] += n or 0
                    bucket['messages'] += 1
                    bucket['input_tokens'] += r['input_tokens'] or 0
                    bucket['output_tokens'] += r['output_tokens'] or 0
                    bucket['cache_tokens'] += ((r['cache_creation_5m'] or 0)
                                               + (r['cache_creation_1h'] or 0)
                                               + (r['cache_read'] or 0))
                    bucket['cost_usd'] += r['cost_usd'] or 0
        result = [{'name': k, **v} for k, v in counts.items()]
        result.sort(key=lambda x: x['cost_usd'], reverse=True)
        return result

    def daily_series(self, days: int = 30,
                     account: Optional[str] = None) -> List[Dict]:
        """Per-day totals, bucketed by the user's **local** calendar day.

        Grouping on the stored UTC string put usage in the wrong day for
        anyone not on UTC. Rolling up hour buckets (a few hundred rows) in
        Python instead lets the real timezone — including its history of
        offset changes — decide which day each hour belongs to.
        """
        from .periods import local_day_start, local_now
        start_local = local_day_start(local_now()) - timedelta(days=days - 1)
        where, args = self._filters(
            account=account, since=start_local.astimezone(timezone.utc))
        sql = ("SELECT substr(timestamp, 1, 13) AS hour," + _TOKEN_SUMS +
               "FROM messages WHERE 1=1" + where + " GROUP BY hour")
        buckets: Dict[str, Dict] = {}
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        for r in rows:
            try:
                utc_hour = datetime.strptime(
                    r['hour'], '%Y-%m-%dT%H').replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            day = utc_hour.astimezone().strftime('%Y-%m-%d')
            b = buckets.setdefault(day, {
                'day': day, 'input_tokens': 0, 'cache_creation': 0,
                'cache_read': 0, 'output_tokens': 0, 'total_tokens': 0,
                'cost_usd': 0.0, 'messages': 0,
            })
            for k in ('input_tokens', 'cache_creation', 'cache_read',
                      'output_tokens', 'total_tokens', 'messages'):
                b[k] += r[k] or 0
            b['cost_usd'] += r['cost_usd'] or 0
        return [buckets[d] for d in sorted(buckets)]

    def total_cost(self, since: Optional[datetime] = None,
                   until: Optional[datetime] = None,
                   project: Optional[str] = None,
                   model: Optional[str] = None,
                   account: Optional[str] = None) -> float:
        where, args = self._filters(project, model, account, since, until)
        sql = ("SELECT COALESCE(SUM(cost_usd), 0) FROM messages WHERE 1=1"
               + where)
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
