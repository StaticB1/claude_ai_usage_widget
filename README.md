# ⚡ Claude AI Usage Widget — Linux Taskbar (Multi-Account Fork)

> **Fork of [StaticB1/claude_ai_usage_widget](https://github.com/StaticB1/claude_ai_usage_widget)**

A lightweight system tray widget that shows your Claude AI subscription usage (5h and 7d rate limit windows) directly in your Linux taskbar. Supports multiple accounts and is fully configurable via a built-in UI — no config file editing required.

| Tray menu | Details popup |
|---|---|
| ![Tray menu](screenshots/screenshot_tray.png) | ![Details popup](screenshots/screenshot_popup.png) |

## Features

- **Multiple accounts** — tray label shows `Work:67% Personal:12%` for all accounts at a glance; individual accounts can be hidden from the tray while still appearing in the dropdown and popup
- **Redesigned popup** — two-column table layout showing 5h and 7d side-by-side with inline reset times (`72% — 2h 15m`)
- **Configure window** — edit accounts, thresholds, burn rate alerts, and poll interval live from the tray menu (no config file editing needed)
- **Burn rate alerts** — warns when your 7d usage pace suggests you'll exceed your weekly allocation (e.g. 50% used with only 25% of the week elapsed)
- **Configurable notifications** — set your own warn/critical thresholds (defaults: 60% / 85%)
- **Per-account polling control** — disable background auto-refresh per account (e.g. keep personal accounts polling, skip a work account). The widget always fetches all accounts once on launch and "Refresh Now" always fetches everything. When polling is disabled for an account, reset times switch from a countdown (`2h 15m`) to the actual reset time (`9:00P` for 5h, `Th 7:00P` for 7d)
- **Configurable poll interval** — change how often the widget checks (default: 5 min)
- **Config-driven accounts** — `~/.config/claude-usage-widget/config.json` lists each account's label and Claude Code config dir
- **Graceful failures** — if one account errors it shows `Work:!`; if a period just rolled over and the API hasn't returned fresh data yet it shows `Work:?`; in between resets the last known value is preserved instead of flashing an error
- **Interactive install** — `install.sh` asks how many accounts you want and where their credentials are
- **pyenv support** — installer detects pyenv and creates an isolated venv so the widget survives Python version switches

---

## Quick Start

```bash
git clone https://github.com/gqcorneby/claude_ai_usage_widget.git
cd claude_ai_usage_widget
./install.sh
claude-widget-start
```

The installer will ask how many accounts to monitor and where each one's Claude Code config directory is.

## Requirements

- Linux with GTK3 (GNOME, KDE, XFCE, etc.)
- Python 3.10+
- `gir1.2-appindicator3-0.1`, `gir1.2-notify-0.7` (installer handles these)

## Install

```bash
./install.sh
```

The installer will:
1. Detect pyenv or fall back to system Python
2. Install system GI libraries via apt
3. Create an isolated venv (survives pyenv version changes)
4. Ask you to configure your accounts interactively
5. Set up autostart on login

```
▸ Setting up accounts…
  How many accounts do you want to monitor? [1]: 2

  — Account 1 of 2 —
  Label [Account1]: Work
  Claude config dir [~/.claude]: ~/.claude/work
  ✓ Found credentials

  — Account 2 of 2 —
  Label [Account2]: Personal
  Claude config dir [~/.claude]: ~/.claude
  ✓ Found credentials
```

## Usage

```bash
claude-widget-start   # Start
claude-widget-stop    # Stop
```

The tray label updates every 5 minutes by default. Click for the full breakdown popup; right-click for the menu.

## What's Displayed Where

| Location | Shows |
|---|---|
| **Tray label** | 5h usage % per account — `Work:67% Personal:12%`; `?` if the period just reset and fresh data hasn't arrived yet; `!` on auth/network error |
| **Dropdown menu** | 7d usage %, burn rate, 5h reset time — `Work: 45% ↑1.8× ↺ 1h 20m` (or `↺ 9:00P` when polling is disabled for that account) |
| **Details popup (5h column)** | Progress bar, 5h % and reset time (`2h 15m` or `9:00P`) |
| **Details popup (7d column)** | Progress bar, 7d % and reset time (`3d 4h` or `Th 7:00P`), burn rate pace (`↑1.8×` / `↓0.3×`) |

The tray gives you the quick hourly glance; the dropdown shows weekly pace at a glance; the popup has the full picture.

### Notifications

![Alert notification](screenshots/screenshot_alert.png)

| Type | Triggers | Urgency |
|---|---|---|
| Usage threshold — warn | 5h or 7d hits warn % (default 60%) | Normal |
| Usage threshold — critical | 5h or 7d hits critical % (default 85%) | Critical |
| Burn rate — early | 7d pace exceeds multiplier before warn % | Normal |
| Burn rate — warn | 7d pace exceeds multiplier at warn % | Normal |
| Burn rate — critical | 7d pace exceeds multiplier at critical % | Critical |

#### Window reset behaviour

Threshold and burn rate notifications each track the API's `resets_at` timestamp for their respective windows. When that timestamp shifts (i.e. the window rolled over), the escalation level resets to zero and the full warn → critical → 100% sequence can fire again.

- **5h threshold**: resets when the 5h `resets_at` shifts by more than 1 hour
- **7d threshold**: resets when the 7d `resets_at` shifts by more than 6 hours
- **Burn rate**: resets when the 7d `resets_at` shifts by more than 6 hours; additionally skips the first ~8 hours of each new window to avoid false alarms on low-elapsed-time data

State is persisted to `~/.config/claude-usage-widget/notification_state.json`, so restarting the widget mid-window will not re-fire notifications that already fired for the current window.

## Configuration

The easiest way is via the tray menu → **Configure...**:

| Accounts tab | Notifications tab |
|---|---|
| ![Config accounts](screenshots/screenshot_config_accounts.png) | ![Config notifications](screenshots/screenshot_config_notifications.png) |

- **Accounts tab** — add, edit, or remove accounts (label + credentials directory); check **Hide tray** to exclude an account from the tray label; check **No poll** to disable background auto-refresh for that account
- **Notifications tab** — set the poll interval, warn/critical thresholds, and burn rate alert

Changes take effect immediately without restarting the widget.

The config is stored at `~/.config/claude-usage-widget/config.json` and can also be edited directly:

```json
{
  "accounts": [
    { "label": "Work",     "credentials_dir": "~/.claude/work", "disable_polling": true },
    { "label": "Personal", "credentials_dir": "~/.claude", "hide_from_tray": true }
  ],
  "poll_interval_seconds": 300,
  "thresholds": { "warn": 60, "critical": 85 },
  "burn_rate": { "enabled": false, "multiplier": 1.5 }
}
```

Each `credentials_dir` must contain a `.credentials.json` file from Claude Code (`claude login`).

### Burn Rate Alert

When enabled, fires a notification if your 7-day usage rate suggests you'll exceed your weekly limit. The multiplier controls sensitivity — `1.5` means: warn if you're on pace to use 150% of your allocation.

Notifications escalate up to 3 times per window, mirroring the usage thresholds:

| When | Condition |
|---|---|
| Early in the week (below warn %) | First alert — catches it before it's serious |
| At warn % (default 60%) | Second alert if burn rate is still high |
| At critical % (default 85%) | Final alert — critical urgency |

Each level fires at most once. The first ~8 hours of a new window are ignored to avoid false alarms, and all levels reset when the window rolls over.

## How It Works

Uses the same internal API endpoint as Claude Code's `/usage`:

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <oauth-token>
anthropic-beta: oauth-2025-04-20
```

Credentials are read directly from Claude Code's credential files — no separate login required.

## Uninstall

```bash
./uninstall.sh
```

Removes all installed files, scripts, desktop entries, and temp files. Prompts before removing your account config.

## Troubleshooting

| Problem | Fix |
|---|---|
| Account shows `!` | Check `/tmp/claude-widget.log`. Usually an expired token — re-run `claude login` |
| Account shows `?` | The usage window just rolled over and the API hasn't returned data for the new period yet — it will clear on the next successful poll |
| No tray icon on GNOME 43+ | Install `gnome-shell-extension-appindicator` and enable it |
| `AppIndicator3` import fails | `sudo apt install gir1.2-appindicator3-0.1` |
| Widget broke after pyenv switch | Re-run `./install.sh` — creates a new venv from current pyenv version |
| `command not found` after install | Run `hash -r` or open a new terminal |

```bash
cat /tmp/claude-widget.log   # Check logs
```

## License

MIT — see original repo for full history and credits.

## Credits

Original widget by **[Statotech Systems](https://github.com/StaticB1)**.
Multi-account fork by [gqcorneby](https://github.com/gqcorneby).
