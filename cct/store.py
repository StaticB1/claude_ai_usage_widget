from __future__ import annotations
import json as _json
import sqlite3
import threading
from bisect import bisect_right
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional

from .config import BEDROCK_ACCOUNT_LABEL, DB_FILE, DEFAULT_ACCOUNT_LABEL
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

# An account filter of the form ``hop:<name>`` selects the turns made with that
# claudehop account (the ``hop_account`` column) instead of a configured
# account's logs.
HOP_PREFIX = 'hop:'

# claudehop attribution. The hop timeline is only trusted while something was
# watching it: a silence longer than this between two checks may hide hops, so
# that stretch is filed as unknown.
HOP_WATCH_GAP = timedelta(minutes=3)
# A `hop run` session's file disappears when its process ends, between two
# checks; turns logged this soon after the last sighting still belong to it.
HOP_RUN_GRACE = timedelta(seconds=60)


class _TurnAt:
    """The two things ``hop_resolver`` reads from a turn, for a stored row."""
    __slots__ = ('timestamp', 'session_id')

    def __init__(self, timestamp: datetime, session_id: Optional[str]):
        self.timestamp = timestamp
        self.session_id = session_id


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _from_iso(raw) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

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
            c.execute("""CREATE TABLE IF NOT EXISTS hop_usage (
                name TEXT PRIMARY KEY, pct5 INTEGER, pct7 INTEGER,
                resets_5h TEXT, resets_7d TEXT, seen_at TEXT NOT NULL)""")
            hcols = {r['name'] for r in c.execute(
                "PRAGMA table_info(hop_usage)").fetchall()}
            if 'week_scope' not in hcols:
                c.execute("ALTER TABLE hop_usage ADD COLUMN week_scope TEXT")
                # Readings taken before weekly_window existed stored 0 for a
                # Team seat, whose reply has no seven_day window and so no
                # weekly reset time. That 0 was never measured.
                c.execute("UPDATE hop_usage SET pct7=NULL WHERE resets_7d IS NULL")
            # When each claudehop account became the live login (account NULL:
            # a stretch nobody was watching), and which conversations ran in
            # `hop run` sessions on an account of their own.
            c.execute("""CREATE TABLE IF NOT EXISTS hop_timeline (
                at TEXT PRIMARY KEY, account TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS hop_run_sessions (
                session_id TEXT NOT NULL, account TEXT NOT NULL,
                since TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY (session_id, since))""")
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
            if 'hop_account' not in mcols:
                # NULL for every row already stored: nothing recorded which
                # account was live before this version, so they stay unattributed.
                c.execute("ALTER TABLE messages ADD COLUMN hop_account TEXT")
            c.executescript(SCHEMA_INDEXES)
            c.execute("CREATE INDEX IF NOT EXISTS idx_messages_hop_ts "
                      "ON messages(hop_account, timestamp)")
            # Bedrock turns live under their own account (see Turn.is_bedrock).
            # Idempotent, so it also repairs rows imported before this existed.
            c.execute("UPDATE messages SET account=? "
                      "WHERE substr(msg_key, 1, 9)='msg_bdrk_' AND account<>?",
                      (BEDROCK_ACCOUNT_LABEL, BEDROCK_ACCOUNT_LABEL))
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
                     account: str = DEFAULT_ACCOUNT_LABEL,
                     hop_of: Optional[Callable[[Turn], Optional[str]]] = None
                     ) -> int:
        """Insert or refresh rows for ``turns``. Returns the number written.

        This updates on conflict rather than ignoring, so re-reading a log
        after a parser or pricing fix corrects the stored row instead of
        leaving the first (wrong) version in place forever. ``hop_of`` names
        the claudehop account each turn was made with (see ``hop_resolver``).
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
                BEDROCK_ACCOUNT_LABEL if t.is_bedrock else account,
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
                hop_of(t) if hop_of is not None and not t.is_bedrock else None,
            ))
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT INTO messages(
                    msg_key, timestamp, account, project, session_id, model,
                    input_tokens, cache_creation_5m, cache_creation_1h,
                    cache_read, output_tokens, cost_usd, is_sidechain,
                    tool_uses_json, hop_account
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    tool_uses_json=excluded.tool_uses_json,
                    hop_account=excluded.hop_account""",
                rows,
            )
            return len(rows)

    # ── Incremental-scan bookkeeping ─────────────────────────────────────────

    def save_hop_usage(self, name: str, pct5: Optional[int], pct7: Optional[int],
                       resets_5h: Optional[str], resets_7d: Optional[str],
                       week_scope: Optional[str] = None) -> None:
        """Remember the plan usage last seen while claudehop account ``name`` was
        live, so it can still be shown after you hop to another account.
        ``week_scope`` names the model a Team seat's weekly figure covers."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO hop_usage(name, pct5, pct7, resets_5h, resets_7d, "
                "week_scope, seen_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO "
                "UPDATE SET pct5=excluded.pct5, pct7=excluded.pct7, "
                "resets_5h=excluded.resets_5h, resets_7d=excluded.resets_7d, "
                "week_scope=excluded.week_scope, seen_at=excluded.seen_at",
                (name, pct5, pct7, resets_5h, resets_7d, week_scope,
                 datetime.now(timezone.utc).isoformat()))

    def hop_usage(self) -> Dict[str, dict]:
        with self._conn() as c:
            return {r['name']: dict(r)
                    for r in c.execute("SELECT * FROM hop_usage").fetchall()}

    # ── Which claudehop account made each turn ──────────────────────────────

    def track_hop(self, active: Optional[str], changed_at: Optional[datetime],
                  now: Optional[datetime] = None) -> Optional[datetime]:
        """Record which claudehop account is live, for attributing turns.

        Call this on every scan, before importing. ``changed_at`` is when
        claudehop last wrote its ``active`` pointer, which it does on every hop,
        so a hop is dated to the second even when it is noticed later. Hops
        made while nothing was checking (a silence over ``HOP_WATCH_GAP``) can't
        be ordered, so that stretch is recorded as unknown. A first check
        dates the live account from its hop: it has been live since then.

        Returns the earliest time the timeline changed from, so turns already
        stored since then can be filed again (``reattribute``), or None.
        """
        now = now or datetime.now(timezone.utc)
        changed: List[datetime] = []
        with self._conn() as c:
            rows = [(_from_iso(r['at']), r['account']) for r in
                    c.execute("SELECT at, account FROM hop_timeline").fetchall()]
            rows = sorted((r for r in rows if r[0] is not None),
                          key=lambda r: r[0])
            hb_row = c.execute("SELECT value FROM meta WHERE key='hop_watch_last'"
                               ).fetchone()
            hb = _from_iso(hb_row['value']) if hb_row else None

            def put(at: datetime, acc: Optional[str]):
                c.execute("INSERT INTO hop_timeline(at, account) VALUES(?,?) "
                          "ON CONFLICT(at) DO UPDATE SET account=excluded.account",
                          (_iso(at), acc))
                changed.append(at)

            if not rows:
                if active:
                    start = changed_at if changed_at and changed_at <= now else now
                    put(start, active)
            else:
                last_at, last_acc = rows[-1]
                gap = hb is None or now - hb > HOP_WATCH_GAP
                hopped = changed_at is not None and (hb is None or changed_at > hb)
                if active != last_acc or (gap and hopped):
                    floor = last_at
                    if gap and hb is not None and hb > last_at:
                        put(hb, None)
                        floor = hb
                    at = changed_at if hopped and changed_at <= now else now
                    if at <= floor:
                        at = now
                    put(at, active)
            c.execute("INSERT INTO meta(key, value) VALUES('hop_watch_last', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_iso(now),))
        return min(changed) if changed else None

    def note_run_sessions(self, sessions: Iterable, now: Optional[datetime] = None
                          ) -> Optional[datetime]:
        """Remember the conversations open in ``hop run`` sessions (objects with
        ``session_id``, ``account`` and ``since``), and that they are still open.
        Returns the earliest start of a session not seen before, or None."""
        now = now or datetime.now(timezone.utc)
        rows = [(s.session_id, s.account, _iso(s.since), _iso(now))
                for s in sessions]
        if not rows:
            return None
        with self._conn() as c:
            known = {(r['session_id'], r['since']) for r in c.execute(
                "SELECT session_id, since FROM hop_run_sessions").fetchall()}
            new = [_from_iso(r[2]) for r in rows if (r[0], r[2]) not in known]
            c.executemany(
                "INSERT INTO hop_run_sessions(session_id, account, since, last_seen) "
                "VALUES(?,?,?,?) ON CONFLICT(session_id, since) DO UPDATE SET "
                "account=excluded.account, last_seen=excluded.last_seen", rows)
        new = [t for t in new if t]
        return min(new) if new else None

    def reattribute(self, account: str, since: datetime) -> int:
        """File the stored turns of ``account`` from ``since`` on again, under the
        claudehop account each was made with. Needed when the timeline learns
        something about turns already stored: a hop noticed a few seconds late,
        a `hop run` session seen for the first time, or rows an earlier version
        stored without attribution. Returns the number of rows changed."""
        resolve = self.hop_resolver()
        start = _iso(since - timedelta(seconds=1))
        with self._conn() as c:
            rows = c.execute(
                "SELECT msg_key, timestamp, session_id, hop_account FROM messages "
                "WHERE account=? AND timestamp >= ?", (account, start)).fetchall()
            updates = []
            for r in rows:
                ts = _from_iso(r['timestamp'])
                if ts is None:
                    continue
                who = resolve(_TurnAt(ts, r['session_id']))
                if who != r['hop_account']:
                    updates.append((who, r['msg_key']))
            c.executemany("UPDATE messages SET hop_account=? WHERE msg_key=?",
                          updates)
        return len(updates)

    def meta_get(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row['value'] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    def hop_resolver(self) -> Callable[[Turn], Optional[str]]:
        """A function naming the claudehop account a turn was made with.

        A conversation open in a ``hop run`` session used that session's account
        between its start and its last sighting; anything else used whichever
        account was live at the turn's time. None before tracking began and in
        stretches nobody was watching.
        """
        with self._conn() as c:
            tl = [(_from_iso(r['at']), r['account']) for r in
                  c.execute("SELECT at, account FROM hop_timeline").fetchall()]
            runs: Dict[str, List[tuple]] = {}
            for r in c.execute("SELECT * FROM hop_run_sessions").fetchall():
                since, seen = _from_iso(r['since']), _from_iso(r['last_seen'])
                if since and seen:
                    runs.setdefault(r['session_id'], []).append(
                        (since, seen + HOP_RUN_GRACE, r['account']))
        tl = sorted((r for r in tl if r[0] is not None), key=lambda r: r[0])
        times = [t for t, _ in tl]

        def resolve(turn: Turn) -> Optional[str]:
            t = turn.timestamp
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            for since, until, acc in runs.get(turn.session_id or '', ()):
                if since <= t <= until:
                    return acc
            i = bisect_right(times, t) - 1
            return tl[i][1] if i >= 0 else None
        return resolve

    def hop_tracking_since(self) -> Optional[datetime]:
        """When attribution to claudehop accounts began, or None if it never has."""
        with self._conn() as c:
            rows = c.execute("SELECT at FROM hop_timeline").fetchall()
        times = [t for t in (_from_iso(r['at']) for r in rows) if t]
        return min(times) if times else None

    def hop_accounts_with_rows(self) -> List[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT hop_account FROM messages "
                "WHERE hop_account IS NOT NULL ORDER BY hop_account").fetchall()]

    def hop_account_summary(self, since: Optional[datetime] = None,
                            until: Optional[datetime] = None) -> List[Dict]:
        """Totals per claudehop account; ``hop_account`` None is unattributed."""
        where, args = self._filters(since=since, until=until)
        sql = (f"SELECT hop_account, COUNT(DISTINCT project) AS projects, "
               f"MAX(timestamp) AS last_used, {_TOKEN_SUMS} "
               "FROM messages WHERE account <> ?" + where +
               " GROUP BY hop_account ORDER BY total_tokens DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                sql, [BEDROCK_ACCOUNT_LABEL, *args]).fetchall()]

    def has_account_rows(self, account: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM messages WHERE account=? LIMIT 1",
                             (account,)).fetchone() is not None

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
        if account and account.startswith(HOP_PREFIX):
            sql += " AND hop_account = ?"
            args.append(account[len(HOP_PREFIX):])
        elif account:
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
        where, args = self._filters(account=account)
        sql = "SELECT COUNT(*) FROM messages WHERE 1=1" + where
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
