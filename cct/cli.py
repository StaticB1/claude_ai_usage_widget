from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .blocks import SESSION_LOOKBACK, active_session, forecast
from .budgets import evaluate_budgets
from .cloud import (AuthError, CloudApiError, RateLimitError,
                    fetch_cloud_usage, load_token, normalize_utilization)
from .config import (APP_NAME, APP_VERSION, Account,
                     find_account, load_accounts, BEDROCK_ACCOUNT_LABEL)
from . import hop as claudehop
from .format import fmt, fmt_cost, fmt_duration
from .parser import iter_project_dirs, parse_jsonl
from .periods import UnknownPeriod, cutoff, range_text
from .pricing import RateCard, load_rate_card
from .store import HOP_PREFIX, Store


def _safe_cloud_data(account: Optional[Account] = None) -> Optional[dict]:
    """Best-effort cloud usage fetch — silent on any failure (token missing,
    rate-limited, network down). Used by budget evaluation so utilization
    budgets can show a value when a token is configured."""
    creds = account.credentials_file if account else None
    token = load_token(creds)
    if not token:
        return None
    try:
        return fetch_cloud_usage(token)
    except (RateLimitError, AuthError, CloudApiError):
        return None


def _resolve_account(label: Optional[str]) -> Optional[Account]:
    if not label:
        return None
    if label.startswith(HOP_PREFIX):
        hs = claudehop.load_state()
        names = [p.name for p in hs.profiles] if hs else []
        if label[len(HOP_PREFIX):] not in names:
            raise SystemExit(
                f"Unknown claudehop account '{label[len(HOP_PREFIX):]}'. Known: "
                f"{', '.join(names) or 'none (claudehop is not in use)'}")
        return None  # no credentials of its own here; see _hop_reading
    accounts = load_accounts()
    acc = find_account(label, accounts)
    if not acc:
        raise SystemExit(
            f"Unknown account '{label}'. Known: "
            f"{', '.join(a.label for a in accounts)}"
        )
    return acc


def _cutoff(period: str) -> Optional[datetime]:
    """Period start, or None for all time. See ``cct.periods``."""
    try:
        return cutoff(period)
    except UnknownPeriod as e:
        raise SystemExit(str(e))


def _hop_reading(label: Optional[str]) -> Optional['claudehop.Reading']:
    """For a ``hop:<name>`` filter, that account's newest plan reading: live
    when it is the live login, else the last one claudehop or the widget took."""
    if not label or not label.startswith(HOP_PREFIX):
        return None
    name = label[len(HOP_PREFIX):]
    if claudehop.active_name(load_token()) == name:
        data = _safe_cloud_data()
        if data:
            return claudehop.reading_from_cloud(data, datetime.now(timezone.utc))
    return claudehop.newest(
        claudehop.reading_from_seen(Store().hop_usage().get(name)),
        claudehop.load_usage_cache().get(name))


def _resets_at_5h(account: Optional[Account]) -> Optional[str]:
    """The account's live 5-hour reset moment, or None if unreachable.

    The reset time the account is actually billed against is a server-side
    fact, so the CLI asks for it before reporting a session. Silent on any
    failure — the caller falls back to inferring the window from local logs.
    """
    data = _safe_cloud_data(account)
    if not data:
        return None
    five = data.get('five_hour')
    return five.get('resets_at') if isinstance(five, dict) else None


def _session_rows(store: Store, account: Optional[str]):
    return store.query(
        since=datetime.now(timezone.utc) - SESSION_LOOKBACK,
        account=account,
    )


def scan_into_store(store: Store, rate_card: RateCard,
                    claude_dir: Optional[Path] = None,
                    accounts: Optional[List[Account]] = None,
                    only: Optional[str] = None,
                    ) -> dict:
    """Import new turns. Returns ``{account_label: count}``.

    If ``claude_dir`` is set, scans that single directory under the synthetic
    label ``default`` (back-compat with --claude-dir). Otherwise iterates
    every configured account; ``only`` (label) restricts to one account.

    When claudehop is in use, turns from the config dir it manages are also
    filed under the claudehop account that made them (see ``track_hop``).
    """
    def _scan_dir(projects_root: Path, label: str, hop_of=None) -> int:
        # Skip files whose (size, mtime) are unchanged since the last scan, so
        # a repeated scan only re-parses the active session log instead of the
        # whole history. Claude Code session JSONL is append-only, so a change
        # in size or mtime is a reliable "has new turns" signal.
        prev = store.scanned_file_map(label)
        n = 0
        updates = []
        for proj in iter_project_dirs(projects_root):
            for jsonl in proj.rglob('*.jsonl'):
                try:
                    st = jsonl.stat()
                except OSError:
                    continue
                key = str(jsonl)
                sig = (st.st_size, st.st_mtime)
                if prev.get(key) == sig:
                    continue
                _, turns = parse_jsonl(jsonl)
                n += store.upsert_turns(turns, rate_card, account=label,
                                        hop_of=hop_of)
                updates.append((key, st.st_size, st.st_mtime))
        store.mark_files_scanned(label, updates)
        return n

    if claude_dir is not None:
        return {'default': _scan_dir(claude_dir, 'default')}

    if accounts is None:
        accounts = load_accounts()
    hop_label = track_hop(store, accounts)
    hop_of = store.hop_resolver() if hop_label else None
    if only and only.startswith(HOP_PREFIX):
        only = hop_label or only
    counts: dict = {}
    for acc in accounts:
        if only and acc.label != only:
            continue
        counts[acc.label] = _scan_dir(
            acc.projects_dir, acc.label,
            hop_of if acc.label == hop_label else None)
    return counts


def track_hop(store: Store, accounts: List[Account]) -> Optional[str]:
    """Note which claudehop account is live and which conversations are open in
    ``hop run`` sessions. Returns the label of the configured account whose
    logs claudehop manages, or None when claudehop is not in use."""
    try:
        home = claudehop.claude_home().resolve()
        acc = next((a for a in accounts
                    if Path(a.claude_dir).resolve() == home), None)
        if acc is None:
            return None
        hs = claudehop.load_state(load_token(acc.credentials_file))
        if hs is None or not hs.profiles:
            return None
        changed = [t for t in (
            store.track_hop(hs.active, claudehop.active_changed_at()),
            store.note_run_sessions(claudehop.run_sessions())) if t]
        if not store.meta_get('hop_backfilled'):
            # Once: rows an earlier version stored since tracking began.
            first = store.hop_tracking_since()
            if first:
                changed.append(first)
                store.meta_set('hop_backfilled', '1')
        if changed:
            store.reattribute(acc.label, min(changed))
        return acc.label
    except Exception as e:  # attribution is extra; never let it stop a scan
        print(f"tracker: claudehop tracking skipped: {e}", file=sys.stderr)
        return None


# ── Subcommand handlers ────────────────────────────────────────────────────

def cmd_scan(args):
    store = Store()
    rc = load_rate_card()
    claude_dir = Path(args.claude_dir) if args.claude_dir else None
    counts = scan_into_store(store, rc, claude_dir,
                             only=getattr(args, 'account', None))
    print(json.dumps({
        'imported': counts,
        'imported_total': sum(counts.values()),
        'total_messages': store.message_count(),
    }, indent=2))


def cmd_summary(args):
    store = Store()
    account = getattr(args, 'account', None)
    if args.scan:
        scan_into_store(store, load_rate_card(), only=account)
    cutoff = _cutoff(args.period)
    proj = store.project_summary(since=cutoff, account=account)
    total_cost = sum((p['cost_usd'] or 0) for p in proj)
    total_tokens = sum((p['total_tokens'] or 0) for p in proj)
    total_msgs = sum((p['messages'] or 0) for p in proj)
    out = {
        'period': args.period,
        'account': account,
        'projects': proj,
        'totals': {
            'projects': len(proj),
            'messages': total_msgs,
            'tokens': total_tokens,
            'cost_usd': total_cost,
        },
    }
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"{APP_NAME} {APP_VERSION}")
        scope = f" · account={account}" if account else ""
        print(f"Period: {args.period}{scope} · {len(proj)} projects · "
              f"{total_msgs} messages · {fmt(total_tokens)} tokens · "
              f"{fmt_cost(total_cost)}")
        print()
        print(f"{'Project':<32} {'Msgs':>6} {'Tokens':>10} {'Cost':>10}")
        print('-' * 62)
        for p in proj[: args.limit]:
            print(f"{(p['project'] or '')[:32]:<32} "
                  f"{p['messages'] or 0:>6} "
                  f"{fmt(p['total_tokens'] or 0):>10} "
                  f"{fmt_cost(p['cost_usd'] or 0):>10}")


def _period_line(period: str, account: Optional[str]) -> str:
    """One line naming the window the figures below cover."""
    scope = f" · account {account}" if account else ""
    return f"Period: {period} ({range_text(period) or 'all time'}){scope}"


def cmd_models(args):
    store = Store()
    cutoff = _cutoff(args.period)
    account = getattr(args, 'account', None)
    out = store.model_summary(since=cutoff, account=account)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(_period_line(args.period, account))
        print(f"{'Model':<32} {'Msgs':>6} {'Tokens':>10} {'Cost':>10}")
        print('-' * 62)
        for m in out:
            print(f"{(m['model'] or '')[:32]:<32} "
                  f"{m['messages']:>6} "
                  f"{fmt(m['total_tokens'] or 0):>10} "
                  f"{fmt_cost(m['cost_usd'] or 0):>10}")


def _elide(name: str, width: int) -> str:
    """Shorten from the middle, keeping both ends.

    MCP tool names share a long prefix (``mcp__chrome-devtools__…``), so
    cutting the tail rendered several different tools as the same string.
    """
    if len(name) <= width:
        return name
    keep = width - 1
    head = (keep + 1) // 2
    return name[:head] + '…' + name[len(name) - (keep - head):]


def cmd_tools(args):
    store = Store()
    cutoff = _cutoff(args.period)
    account = getattr(args, 'account', None)
    out = store.tool_summary(since=cutoff, account=account)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(_period_line(args.period, account))
        print(f"{'Tool':<34} {'Calls':>8} {'Msgs':>6} {'Cost':>10}")
        print("Cost is the whole turn's cost, charged to each tool that turn "
              "called, so the")
        print("column neither adds up to your total spend nor divides it "
              "per call.")
        print('-' * 62)
        for t in out:
            print(f"{_elide(t['name'], 34):<34} "
                  f"{int(t['calls']):>8} "
                  f"{int(t['messages']):>6} "
                  f"{fmt_cost(t['cost_usd']):>10}")


def cmd_accounts(args):
    """List accounts with their last-used and totals."""
    accounts = load_accounts()
    store = Store()
    cutoff = _cutoff(args.period)
    summary = {row['account']: row for row in store.account_summary(cutoff)}
    out = []
    for a in accounts:
        s = summary.get(a.label) or {}
        out.append({
            'label': a.label,
            'claude_dir': str(a.claude_dir),
            'disable_polling': a.disable_polling,
            'hide_from_tray': a.hide_from_tray,
            'messages': s.get('messages', 0),
            'projects': s.get('projects', 0),
            'tokens': s.get('total_tokens', 0),
            'cost_usd': s.get('cost_usd', 0.0),
            'last_used': s.get('last_used'),
        })
    # Bedrock turns are stored under their own label but it is not a configured
    # account, so show it here as a row of its own once there is any usage.
    if (BEDROCK_ACCOUNT_LABEL in summary
            and BEDROCK_ACCOUNT_LABEL not in {a.label for a in accounts}):
        s = summary[BEDROCK_ACCOUNT_LABEL]
        out.append({
            'label': BEDROCK_ACCOUNT_LABEL,
            'claude_dir': 'Amazon Bedrock (list-price estimate)',
            'disable_polling': True,
            'hide_from_tray': True,
            'messages': s.get('messages', 0),
            'projects': s.get('projects', 0),
            'tokens': s.get('total_tokens', 0),
            'cost_usd': s.get('cost_usd', 0.0),
            'last_used': s.get('last_used'),
        })
    hop = _hop_report(store, cutoff)
    if args.json:
        if hop:
            out.extend(hop['rows'])
        print(json.dumps(out, indent=2, default=str))
        return
    print(_period_line(args.period, None))
    print(f"{'Account':<16} {'Claude dir':<32} {'Msgs':>6} "
          f"{'Tokens':>10} {'Cost':>10}")
    print('-' * 80)
    for a in out:
        print(f"{a['label'][:16]:<16} {a['claude_dir'][:32]:<32} "
              f"{a['messages']:>6} {fmt(a['tokens'] or 0):>10} "
              f"{fmt_cost(a['cost_usd'] or 0):>10}")
    if not hop:
        return
    hs = hop['state']
    print()
    print(f"claudehop: live login is {hs.active or 'unknown'}"
          + (f"; new sessions run through {hs.provider}" if hs.provider else ""))
    since = hop['tracking_since']
    print("Tokens are those made with each account "
          + (f"since tracking began on {since.astimezone():%Y-%m-%d %H:%M}."
             if since else "(tracking starts with the next scan)."))
    w = max(len(r['name']) for r in hop['rows'])
    print(f"   {'Account':<{w}} {'Msgs':>6} {'Tokens':>8}  Plan usage")
    for r in hop['rows']:
        mark = '*' if r['live'] else ' '
        extra = [x for x in (r['freshness'],
                             f"open in {r['run_terminals']} hop run terminal"
                             + ('s' if r['run_terminals'] != 1 else '')
                             if r['run_terminals'] else '') if x]
        print(f" {mark} {r['name']:<{w}} {r['messages']:>6} "
              f"{fmt(r['tokens'] or 0):>8}  {r['usage']}"
              + (f" ({'; '.join(extra)})" if extra else ''))
    if hop['unattributed']:
        print(f"   {'(unattributed)':<{w}} {hop['unattributed']['messages']:>6} "
              f"{fmt(hop['unattributed']['total_tokens'] or 0):>8}  "
              "before tracking began, or while nothing was watching")
    pick = hop['next']
    if pick is not None:
        who = 'hop next would pick' if pick.listed else 'most 5-hour room'
        print(f"Next: {pick.name} ({who}; {pick.why})" if pick.name
              else f"Next: {pick.why}")


def _hop_report(store: Store, cutoff: Optional[datetime]) -> Optional[dict]:
    """Everything ``accounts`` says about claudehop accounts, or None."""
    hs = claudehop.load_state(load_token())
    if hs is None or not hs.profiles:
        return None
    now = datetime.now(timezone.utc)
    names = [p.name for p in hs.profiles]
    readings = claudehop.all_readings(names, store.hop_usage())
    runs = claudehop.run_terminals()
    totals = {r['hop_account']: r for r in store.hop_account_summary(cutoff)}
    rows = []
    for p in sorted(hs.profiles, key=lambda q: (q.name != hs.active, q.name)):
        r = readings.get(p.name)
        usage, fresh = claudehop.usage_text(r, now)
        t = totals.get(p.name) or {}
        rows.append({
            'label': f'{HOP_PREFIX}{p.name}',
            'name': p.name,
            'claude_dir': 'claudehop account',
            'email': p.email,
            'plan': p.plan,
            'live': p.name == hs.active and not hs.provider,
            'disable_polling': True,
            'hide_from_tray': True,
            'messages': t.get('messages', 0),
            'projects': t.get('projects', 0),
            'tokens': t.get('total_tokens', 0),
            'cost_usd': t.get('cost_usd', 0.0),
            'last_used': t.get('last_used'),
            'usage': usage,
            'freshness': fresh,
            'reading': ({'pct5': r.pct5, 'resets_5h': r.resets_5h,
                         'pct7': r.pct7, 'resets_7d': r.resets_7d,
                         'week_scope': r.week_scope, 'source': r.source,
                         'read_at': r.at.isoformat() if r.at else None}
                        if r is not None and r.has_figures else None),
            'run_terminals': runs.get(p.name, 0),
        })
    return {
        'state': hs,
        'rows': rows,
        'unattributed': totals.get(None),
        'tracking_since': store.hop_tracking_since(),
        'next': claudehop.most_room(readings, names, hs.active, now),
    }


def cmd_block(args):
    """The current 5-hour session window, anchored on the live reset time."""
    store = Store()
    account = getattr(args, 'account', None)
    resets_at = None
    hr = None
    if not args.no_cloud:
        acc = _resolve_account(account)
        hr = _hop_reading(account)
        resets_at = hr.resets_5h if hr is not None else _resets_at_5h(acc)
    block = active_session(_session_rows(store, account), resets_at)
    if block is None:
        print(json.dumps({'active': False, 'anchored': bool(resets_at)},
                         indent=2))
        return
    fc = forecast(block)
    payload = {
        'active': True,
        # False means the window was inferred from local logs alone and can be
        # minutes out; True means it came from the account's own reset time.
        'anchored': block.anchored,
        'start': block.start.isoformat(),
        'end': block.end.isoformat(),
        'last_message': block.last_message.isoformat(),
        'messages': block.messages,
        'tokens': block.total_tokens,
        'input_tokens': block.input_tokens,
        'cache_creation': block.cache_creation,
        'cache_read': block.cache_read,
        'output_tokens': block.output_tokens,
        'cost_usd': block.cost_usd,
        'remaining_seconds': int(block.remaining().total_seconds()),
        'burn_per_min_tokens': fc.burn_rate_per_min_tokens,
        'burn_per_min_cost_usd': fc.burn_rate_per_min_cost,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        source = ('estimated from logs' if not block.anchored
                  else 'reset time from its last reading' if hr is not None
                  else 'live reset time')
        print(f"Session: {block.start.astimezone():%Y-%m-%d %H:%M}"
              f" \u2192 {block.end.astimezone():%H:%M}  ({source})")
        print(f"  Messages: {block.messages}  "
              f"Tokens: {fmt(block.total_tokens)}"
              f"  Cost: {fmt_cost(block.cost_usd)}")
        print(f"  Remaining: "
              f"{fmt_duration(block.remaining().total_seconds())}")
        print(f"  Burn: {fmt(int(fc.burn_rate_per_min_tokens))} tok/min, "
              f"${fc.burn_rate_per_min_cost:.4f}/min")


def cmd_cloud(args):
    acc = _resolve_account(getattr(args, 'account', None))
    creds = acc.credentials_file if acc else None
    token = load_token(creds)
    if not token:
        print(json.dumps({'error': 'no_token'}, indent=2))
        sys.exit(2)
    try:
        data = fetch_cloud_usage(token)
    except RateLimitError as e:
        print(json.dumps({'error': 'rate_limited',
                          'retry_after': e.retry_after}, indent=2))
        sys.exit(3)
    except AuthError as e:
        print(json.dumps({'error': 'auth', 'code': e.code}, indent=2))
        sys.exit(4)
    except CloudApiError as e:
        print(json.dumps({'error': 'network', 'detail': str(e)}, indent=2))
        sys.exit(5)
    print(json.dumps(data, indent=2, default=str))


def cmd_prompt(args):
    """One-line status for shell prompts / status bars / IDE strips."""
    store = Store()
    account = getattr(args, 'account', None)
    data = None
    if not args.no_cloud:
        acc = _resolve_account(account)
        hr = _hop_reading(account)
        if hr is not None and hr.pct5 is not None:
            data = {'five_hour': {'utilization': hr.pct5,
                                  'resets_at': hr.resets_5h}}
        elif hr is None:
            data = _safe_cloud_data(acc)
    five = data.get('five_hour') if isinstance(data, dict) else None
    five = five if isinstance(five, dict) else {}

    pieces = []
    block = active_session(_session_rows(store, account),
                           five.get('resets_at'))
    if block is not None:
        pieces.append(fmt_duration(block.remaining().total_seconds()))
        if block.cost_usd > 0:
            pieces.append(fmt_cost(block.cost_usd))
    if five:
        pct = int(normalize_utilization(five.get('utilization', 0)) * 100)
        pieces.append(f"5h:{pct}%")
    print(' '.join(pieces) if pieces else '\u2014')


def cmd_export(args):
    store = Store()
    cutoff = _cutoff(args.period)
    account = getattr(args, 'account', None)
    rows = store.query(since=cutoff,
                       project=args.project, model=args.model,
                       account=account)
    if args.format == 'json':
        sys.stdout.write(
            json.dumps([dict(r) for r in rows], indent=2, default=str)
        )
    else:
        import csv
        cols = ['timestamp', 'account', 'hop_account', 'project',
                'session_id', 'model',
                'input_tokens', 'cache_creation_5m', 'cache_creation_1h',
                'cache_read', 'output_tokens', 'cost_usd', 'is_sidechain']
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])


def cmd_budget(args):
    store = Store()
    if args.action == 'list':
        # Only fetch cloud data if any pct-based budget exists
        usage_data = None
        if any(b.get('limit_pct') for b in store.list_budgets()):
            usage_data = _safe_cloud_data()
        states = evaluate_budgets(store, usage_data=usage_data)
        if args.json:
            print(json.dumps([s.__dict__ for s in states],
                             indent=2, default=str))
        else:
            if not states:
                print("No budgets configured. Try: ctt budget add --help")
                return
            for s in states:
                if s.is_pct_based:
                    if not s.data_available:
                        spent = "no cloud data"
                    else:
                        spent = f"{s.spent_pct:.0f}% plan use"
                    limit = f"{s.limit_pct:.0f}% of plan"
                else:
                    limit = (fmt_cost(s.limit_usd) if s.limit_usd
                             else f"{s.limit_tokens:,} tok"
                             if s.limit_tokens else '—')
                    spent = (fmt_cost(s.spent_usd) if s.limit_usd
                             else f"{s.spent_tokens:,} tok")
                print(f"[{s.id}] {s.name}: {spent} / {limit} "
                      f"({s.pct * 100:.0f}%) — {s.scope} ({s.period})")
    elif args.action == 'add':
        if not (args.usd or args.tokens or args.pct):
            raise SystemExit("Specify --usd, --tokens, or --pct")
        scope = args.scope or 'global'
        if args.pct is not None:
            if args.usd or args.tokens:
                raise SystemExit(
                    "--pct cannot be combined with --usd or --tokens")
            if args.pct <= 0:
                raise SystemExit("--pct must be greater than 0")
            if scope != 'global':
                raise SystemExit(
                    "--pct budgets must use --scope global "
                    "(plan utilization is account-wide, not per project/model)")
            if args.period not in ('5h', '7d'):
                raise SystemExit(
                    "--pct budgets require --period 5h or 7d "
                    "(Anthropic's rolling plan windows)")
        bid = store.add_budget(
            name=args.name,
            scope=scope,
            period=args.period,
            limit_usd=args.usd, limit_tokens=args.tokens,
            limit_pct=args.pct,
            notify_at_pct=args.notify_pct,
        )
        print(json.dumps({'id': bid}, indent=2))
    elif args.action == 'remove':
        store.delete_budget(args.id)
        print(json.dumps({'removed': args.id}, indent=2))


def cmd_reprice(args):
    store = Store()
    if getattr(args, 'rescan', False):
        store.forget_scans()
        scan_into_store(store, load_rate_card())
    n = store.reprice_all(load_rate_card())
    print(json.dumps({'repriced': n}, indent=2))


def cmd_gui(args):
    try:
        from .gui import run as run_gui
    except ImportError as e:
        print(f"GUI requires GTK3 + PyGObject: {e}", file=sys.stderr)
        print("Install on Ubuntu/Debian: "
              "sudo apt install python3-gi gir1.2-gtk-3.0", file=sys.stderr)
        sys.exit(1)
    run_gui()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='ctt',
        description=f'{APP_NAME} {APP_VERSION} — Claude Code usage analytics',
    )
    p.add_argument('--version', action='version',
                   version=f'{APP_NAME} {APP_VERSION}')
    sub = p.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('scan', help='Import new turns from ~/.claude into the store')
    sp.add_argument('--claude-dir', help='override ~/.claude path (single-account)')
    sp.add_argument('--account', help='restrict scan to one configured account')
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser('summary', help='Per-project token & cost summary')
    sp.add_argument('--period', default='30d',
                    help='today | 5h | 7d | 30d | NNd | NNh | all')
    sp.add_argument('--limit', type=int, default=20)
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.add_argument('--no-scan', dest='scan', action='store_false', default=True)
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser('models', help='Per-model breakdown')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser('tools', help='Per-tool attribution (Bash, Read, Edit, Agent, ...)')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser('accounts', help='List accounts, claudehop accounts '
                                         'included, and their totals')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser(
        'block', help="Current 5-hour session window + burn rate")
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.add_argument('--no-cloud', action='store_true',
                    help='skip the live reset-time lookup and infer the '
                         'window from local logs (less accurate)')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_block)

    sp = sub.add_parser('cloud', help='Live cloud usage from claude.ai (JSON)')
    sp.add_argument('--account', help="account whose creds to use")
    sp.set_defaults(func=cmd_cloud)

    sp = sub.add_parser('prompt', help='One-line status for shell/status bars')
    sp.add_argument('--no-cloud', action='store_true',
                    help='skip live cloud API call')
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.set_defaults(func=cmd_prompt)

    sp = sub.add_parser('export', help='Dump rows to JSON or CSV')
    sp.add_argument('--period', default='all')
    sp.add_argument('--project')
    sp.add_argument('--model')
    sp.add_argument('--account', help='filter to one account '
                                        '(hop:NAME for a claudehop account)')
    sp.add_argument('--format', choices=['json', 'csv'], default='json')
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser(
        'reprice',
        help='Recompute stored costs after a rate-card edit')
    sp.add_argument('--rescan', action='store_true',
                    help='also re-read every session log first, repairing '
                         'rows written by an older version')
    sp.set_defaults(func=cmd_reprice)

    bp = sub.add_parser('budget', help='Manage spend/usage budgets')
    bsub = bp.add_subparsers(dest='action', required=True)
    bsl = bsub.add_parser('list')
    bsl.add_argument('--json', action='store_true')
    bsa = bsub.add_parser('add')
    bsa.add_argument('--name', required=True)
    bsa.add_argument('--scope', default='global',
                     help='global | project:NAME | model:MODEL_ID '
                          '(must be global for --pct)')
    bsa.add_argument('--period',
                     choices=['day', 'week', 'month', '5h', '7d'],
                     default='month',
                     help="'5h' / '7d' track Anthropic's rolling plan "
                          "windows (required for --pct)")
    bsa.add_argument('--usd', type=float, help='limit in USD')
    bsa.add_argument('--tokens', type=int, help='limit in total tokens')
    bsa.add_argument('--pct', type=float,
                     help='limit as %% of Claude plan utilization '
                          '(Max/Pro/Team — needs OAuth token; '
                          'use with --period 5h or 7d)')
    bsa.add_argument('--notify-pct', type=int, default=80)
    bsr = bsub.add_parser('remove')
    bsr.add_argument('id', type=int)
    bp.set_defaults(func=cmd_budget)

    sp = sub.add_parser('gui', help='Launch the GTK dashboard (Linux only)')
    sp.set_defaults(func=cmd_gui)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0
