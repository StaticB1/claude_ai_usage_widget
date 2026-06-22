from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .blocks import compute_blocks, forecast_active
from .budgets import evaluate_budgets
from .cloud import (AuthError, CloudApiError, RateLimitError,
                    fetch_cloud_usage, load_token, normalize_utilization)
from .config import (APP_NAME, APP_VERSION, CLAUDE_DIR, Account,
                     find_account, load_accounts)
from .format import fmt, fmt_cost, fmt_duration
from .parser import iter_project_dirs, parse_jsonl
from .pricing import RateCard, load_rate_card
from .store import Store


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
    accounts = load_accounts()
    acc = find_account(label, accounts)
    if not acc:
        raise SystemExit(
            f"Unknown account '{label}'. Known: "
            f"{', '.join(a.label for a in accounts)}"
        )
    return acc


def _cutoff(period: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if period == 'all':
        return None
    if period == 'today':
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if period == '5h':
        return now - timedelta(hours=5)
    if period == '7d':
        return now - timedelta(days=7)
    if period == '30d':
        return now - timedelta(days=30)
    if period.endswith('d') and period[:-1].isdigit():
        return now - timedelta(days=int(period[:-1]))
    if period.endswith('h') and period[:-1].isdigit():
        return now - timedelta(hours=int(period[:-1]))
    raise SystemExit(f"Unknown period: {period}")


def scan_into_store(store: Store, rate_card: RateCard,
                    claude_dir: Optional[Path] = None,
                    accounts: Optional[List[Account]] = None,
                    only: Optional[str] = None,
                    ) -> dict:
    """Import new turns. Returns ``{account_label: count}``.

    If ``claude_dir`` is set, scans that single directory under the synthetic
    label ``default`` (back-compat with --claude-dir). Otherwise iterates
    every configured account; ``only`` (label) restricts to one account.
    """
    if claude_dir is not None:
        n = 0
        for proj in iter_project_dirs(claude_dir):
            for jsonl in proj.rglob('*.jsonl'):
                _, turns = parse_jsonl(jsonl)
                n += store.upsert_turns(turns, rate_card)
        return {'default': n}

    if accounts is None:
        accounts = load_accounts()
    counts: dict = {}
    for acc in accounts:
        if only and acc.label != only:
            continue
        n = 0
        for proj in iter_project_dirs(acc.projects_dir):
            for jsonl in proj.rglob('*.jsonl'):
                _, turns = parse_jsonl(jsonl)
                n += store.upsert_turns(turns, rate_card, account=acc.label)
        counts[acc.label] = n
    return counts


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


def cmd_models(args):
    store = Store()
    cutoff = _cutoff(args.period)
    account = getattr(args, 'account', None)
    out = store.model_summary(since=cutoff, account=account)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"{'Model':<32} {'Msgs':>6} {'Tokens':>10} {'Cost':>10}")
        print('-' * 62)
        for m in out:
            print(f"{(m['model'] or '')[:32]:<32} "
                  f"{m['messages']:>6} "
                  f"{fmt(m['total_tokens'] or 0):>10} "
                  f"{fmt_cost(m['cost_usd'] or 0):>10}")


def cmd_tools(args):
    store = Store()
    cutoff = _cutoff(args.period)
    account = getattr(args, 'account', None)
    out = store.tool_summary(since=cutoff, account=account)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"{'Tool':<22} {'Calls':>8} {'Msgs':>6} {'Cost':>10}")
        print('-' * 50)
        for t in out:
            print(f"{t['name'][:22]:<22} "
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
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    print(f"{'Account':<16} {'Claude dir':<32} {'Msgs':>6} "
          f"{'Tokens':>10} {'Cost':>10}")
    print('-' * 80)
    for a in out:
        print(f"{a['label'][:16]:<16} {a['claude_dir'][:32]:<32} "
              f"{a['messages']:>6} {fmt(a['tokens'] or 0):>10} "
              f"{fmt_cost(a['cost_usd'] or 0):>10}")


def cmd_block(args):
    store = Store()
    account = getattr(args, 'account', None)
    rows = store.query(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
        account=account,
    )
    blocks = compute_blocks(rows)
    if not blocks:
        print(json.dumps({'active': False}, indent=2))
        return
    fc = forecast_active(blocks)
    block = blocks[-1]
    payload = {
        'active': block.is_active(),
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
        rem = block.remaining()
        print(f"Block: {block.start.astimezone():%Y-%m-%d %H:%M}"
              f" → {block.end.astimezone():%H:%M}")
        print(f"  Messages: {block.messages}  Tokens: {fmt(block.total_tokens)}"
              f"  Cost: {fmt_cost(block.cost_usd)}")
        print(f"  Remaining: {fmt_duration(rem.total_seconds())}")
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
    rows = store.query(
        since=datetime.now(timezone.utc) - timedelta(hours=6),
        account=account,
    )
    blocks = compute_blocks(rows)
    pieces = []
    if blocks and blocks[-1].is_active():
        b = blocks[-1]
        rem = b.remaining()
        pieces.append(fmt_duration(rem.total_seconds()))
        if b.cost_usd > 0:
            pieces.append(fmt_cost(b.cost_usd))
    if not args.no_cloud:
        acc = _resolve_account(account)
        creds = acc.credentials_file if acc else None
        token = load_token(creds)
        if token:
            try:
                data = fetch_cloud_usage(token)
                u5 = int(normalize_utilization(
                    (data.get('five_hour') or {}).get('utilization', 0)) * 100)
                pieces.append(f"5h:{u5}%")
            except (RateLimitError, AuthError, CloudApiError):
                pass
    print(' '.join(pieces) if pieces else '—')


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
        cols = ['timestamp', 'account', 'project', 'session_id', 'model',
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
    sp.add_argument('--account', help='filter to one account')
    sp.add_argument('--no-scan', dest='scan', action='store_false', default=True)
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser('models', help='Per-model breakdown')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--account', help='filter to one account')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser('tools', help='Per-tool attribution (Bash, Read, Edit, Agent, ...)')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--account', help='filter to one account')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser('accounts', help='List configured accounts and their totals')
    sp.add_argument('--period', default='30d')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser('block', help='Current 5-hour rolling block + ETA')
    sp.add_argument('--account', help='filter to one account')
    sp.add_argument('--json', action='store_true')
    sp.set_defaults(func=cmd_block)

    sp = sub.add_parser('cloud', help='Live cloud usage from claude.ai (JSON)')
    sp.add_argument('--account', help="account whose creds to use")
    sp.set_defaults(func=cmd_cloud)

    sp = sub.add_parser('prompt', help='One-line status for shell/status bars')
    sp.add_argument('--no-cloud', action='store_true',
                    help='skip live cloud API call')
    sp.add_argument('--account', help='filter to one account')
    sp.set_defaults(func=cmd_prompt)

    sp = sub.add_parser('export', help='Dump rows to JSON or CSV')
    sp.add_argument('--period', default='all')
    sp.add_argument('--project')
    sp.add_argument('--model')
    sp.add_argument('--account')
    sp.add_argument('--format', choices=['json', 'csv'], default='json')
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser('reprice',
                        help='Recompute cost_usd for stored rows (after rate-card edit)')
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
