<p align="center">
  <img src="assets/cover.png" alt="Claude Token Tracker" width="400">
</p>

# Claude Token Tracker

> The upgraded successor to **claude_ai_usage_widget**. The original was a
> lightweight GTK widget for watching your Claude.ai 5-hour usage; this is a
> full local analytics tool built on the same idea.

Persistent local analytics for Claude Code: per-project / per-model / per-tool
spend, 5-hour block forecasts with ETA-to-limit, budgets, and a Linux GTK
dashboard. The CLI works on Linux, macOS, and Windows; the GUI is Linux-only.

Where it differs from `ccusage` and other CLIs: it keeps a SQLite history
that survives `~/.claude/projects` cleanup, attributes spend to **tools** and
**models**, and lets you set USD/token budgets that fire desktop notifications.

## What's new vs. the original widget

- **SQLite history** that outlives `~/.claude/projects` cleanup (the widget
  only read live state).
- **Per-project / per-model / per-tool** attribution instead of a single
  global gauge.
- **Budgets** with desktop notifications, **5-hour block forecasts** with
  ETA-to-limit, **multi-account** support, and a **cross-platform CLI** (`ctt`).
- The familiar tray + 5h/7d plan-utilization view is still here, now inside a
  full Catppuccin-themed dashboard.

## Features

- **SQLite history** — `~/.config/claude-token-tracker/history.db`. Stays
  intact even if you wipe `~/.claude/projects`.
- **Per-project / per-model / per-tool breakdown** — see which projects,
  which model, and which tool calls (`Bash`, `Read`, `Edit`, `Agent`, …)
  drive your spend.
- **5-hour rolling block + forecast** — mirrors Anthropic's `claude /usage`
  semantics. Shows ETA to your 5h cap when cloud usage is available.
- **Budgets** — daily/weekly/monthly USD or token caps, scoped globally,
  per-project, or per-model. Desktop notifications when crossed.
- **Cross-platform CLI (`ctt`)** — JSON output for piping into shell prompts,
  status bars, CI checks.
- **Editable rate card** — drop a JSON file at
  `~/.config/claude-token-tracker/rate_card.json` to override pricing without
  editing code.
- **Tray + dashboard (Linux)** — Catppuccin-themed GTK UI, auto-backs-off
  cloud polling when the window is hidden.

## Install

### One-line install (any Linux distro)

```bash
curl -fsSL https://github.com/StaticB1/claude_ai_usage_widget/raw/main/install.sh | bash
```

The script downloads the source, installs GTK3 bindings via your distro's
package manager (apt / dnf / pacman / zypper), drops the app into
`~/.local/share/claude-token-tracker`, registers `claude-token-tracker` (GUI)
and `ctt` (CLI) in `~/.local/bin`, installs hicolor icons, and adds an
autostart entry. Pass `--no-autostart` to skip the autostart step:

```bash
curl -fsSL https://github.com/StaticB1/claude_ai_usage_widget/raw/main/install.sh | bash -s -- --no-autostart
```

### From a clone

```bash
git clone https://github.com/StaticB1/claude_ai_usage_widget.git
cd claude_ai_usage_widget
bash install.sh                    # add --no-autostart to skip login startup
```

### macOS / Windows / headless servers (CLI only)

The CLI (`ctt`) is pure-Python stdlib and works without GTK:

```bash
pip install --user git+https://github.com/StaticB1/claude_ai_usage_widget.git
ctt --help
```

## Uninstall

```bash
# From a clone — one command (also clears the legacy widget):
bash uninstall.sh

# No clone? The installer doubles as its own uninstaller:
bash <(curl -fsSL https://github.com/StaticB1/claude_ai_usage_widget/raw/main/install.sh) --uninstall

# CLI-only (pip) install:
pip uninstall claude-token-tracker

# Remove your stored history (optional):
rm -rf ~/.config/claude-token-tracker
```

`uninstall.sh` delegates to `install.sh --uninstall` (the canonical path) and
then sweeps any leftovers from the original `claude-usage-widget`, so upgrading
from the old widget leaves nothing behind. The uninstaller removes the app
dir, both binaries, the `.desktop` entries, the autostart entry, and the
hicolor icons. It deliberately keeps `~/.config/claude-token-tracker/`
(history.db, budgets, rate-card override) so you don't lose months of data on
a reinstall — delete that directory manually if you want a true clean wipe.

## CLI

```
ctt scan                       # import new logs from ~/.claude into the store
ctt summary --period 30d       # per-project totals (table or --json)
ctt models  --period 7d        # per-model breakdown (table or --json)
ctt tools   --period 7d        # tool-call attribution (Bash, Edit, …)
ctt block                      # active 5h block + burn rate + ETA
ctt cloud                      # raw cloud usage JSON from claude.ai
ctt prompt                     # one-line status: shell prompts / status bars
ctt export --format csv > x.csv
ctt budget add --name "month cap" --usd 100 --period month
ctt budget list
ctt budget remove 1
ctt reprice                    # reprice stored history after rate-card edit
ctt gui                        # launch GTK dashboard (Linux)
```

### Examples

Show in your shell prompt:
```bash
PS1='$(ctt prompt --no-cloud) \$ '
# Renders e.g. '4h 17m $1.24 \$' while Claude Code is active.
```

CI guardrail — fail a build if today's Opus spend > $20:
```bash
COST=$(ctt summary --period today --json | jq '.totals.cost_usd')
[ "$(echo "$COST > 20" | bc)" -eq 1 ] && { echo "Daily Claude budget blown: $$COST"; exit 1; }
```

## Configuration

| Path | What it is |
|---|---|
| `~/.claude/projects/` | Read — Claude Code's session JSONL (source of truth). |
| `~/.claude/.credentials.json` | Read — OAuth token managed by `claude` CLI. |
| `~/.config/claude-token-tracker/history.db` | SQLite — long-term token/cost history. |
| `~/.config/claude-token-tracker/config.json` | Optional fallback OAuth token (when `claude` CLI isn't installed). |
| `~/.config/claude-token-tracker/rate_card.json` | Optional pricing override. |

### Rate-card override

```json
{
  "models": {
    "claude-opus-4-7":    [15.0, 18.75, 30.0, 1.50, 75.0],
    "claude-sonnet-4-7":  [3.0,  3.75,   6.0, 0.30, 15.0]
  }
}
```

Tuple is `[input, cache_write_5m, cache_write_1h, cache_read, output]` in
USD per million tokens. Run `ctt reprice` after editing.

## Architecture

```
cct/
├── parser.py    JSONL → Turn (with tool_use extraction, sidechain flag)
├── pricing.py   Rate card + override loader
├── store.py     SQLite store with budgets + summaries
├── blocks.py    Anthropic-style 5h rolling blocks + burn-rate forecast
├── budgets.py   Period evaluation (day/week/month, scoped)
├── cloud.py     OAuth + claude.ai usage API
├── cli.py       `ctt` argparse commands
└── gui.py       GTK3 dashboard (Linux)
```

## Tests

```bash
pip install pytest
pytest
```

## Contributing & Contact

Contributions are welcome!

- **Bug reports / feature requests** — [Open an issue](https://github.com/StaticB1/claude_ai_usage_widget/issues)
- **Discussions / collaboration** — [GitHub Discussions](https://github.com/StaticB1/claude_ai_usage_widget/discussions)
- **Email** — contact@statotec.com

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

## Acknowledgments

Claude Token Tracker grew out of
[**claude_ai_usage_widget**](https://github.com/StaticB1/claude_ai_usage_widget)
by **StaticB1** — the original GTK widget for live Claude.ai 5-hour / 7-day
usage. Its tray gauge, escalation notifications, and plan-utilization view are
the foundation this project builds on. Thanks to StaticB1 for the original
work and for taking this upgrade upstream.

## License

MIT.

## Authors

- Original `claude_ai_usage_widget` — **StaticB1**
- Token-tracker upgrade — **Statotech Systems**, in partnership with **Ebenworks**
