# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-06-23

Rebrand + rewrite: **claude_ai_usage_widget → Claude Usage Widget & Token Tracker.**
The live tray widget is now one half of a two-part tool; the other half is a
persistent local **Token Tracker** built on top of it.

### Added
- **Token Tracker (local analytics)** — a `cct` package with a SQLite history
  store at `~/.config/claude-token-tracker/history.db` that survives
  `~/.claude/projects` cleanup, with **per-project / per-model / per-tool**
  attribution, cost estimates from an overridable rate card, and a self-computed
  local 5-hour block + burn-rate forecast (no token required).
- **`ctt` CLI** (cross-platform, pure stdlib): `scan`, `summary`, `models`,
  `tools`, `accounts`, `block`, `cloud`, `prompt`, `export`, `reprice`,
  `budget`, and `gui`.
- **Budgets** — daily/weekly/monthly USD or token caps (global, per-project, or
  per-model), plus plan-utilization % budgets that ride the live 5h/7d windows.
- **GTK dashboard** — Catppuccin-themed Dashboard / Projects / Breakdowns /
  Budgets / Settings views; backs off live polling while hidden.
- **Multi-account** — track several Claude logins, each with its own tray
  readout, hide-from-tray, and disable-polling switch.

### Changed
- **New install layout** — app dir `~/.local/share/claude-token-tracker`,
  binaries `claude-token-tracker` (GUI) + `ctt` (CLI), config under
  `~/.config/claude-token-tracker/`. (The old widget used
  `claude-usage-widget` / `claude-widget-start`.)
- **Faster refresh** via fully incremental scanning — each pass skips session
  logs whose size/mtime are unchanged, so only the active session is re-parsed.
- Installer now provisions GTK3/AppIndicator/libnotify and is pyenv-aware.

### Migration
- v2 installs alongside v1 rather than replacing it (all paths changed). Run
  `bash upgrade.sh` from a clone — it stops the old widget, sweeps its leftover
  files, installs v2, and relaunches. See **Upgrading from the old widget** in
  the README. Your OAuth token (`~/.claude/.credentials.json`) and log history
  (`~/.claude/projects`) carry over automatically.

---

## [1.0.4] - 2026-03-26

### Fixed
- **429 rate limit** — the widget now preserves and keeps displaying the last
  cached usage data while rate limited, instead of blanking out, so the tray
  stays useful during the 10-minute back-off

---

## [1.0.3] - 2026-02-19

### Fixed
- **429 rate limit handling** — widget shows ERR and backs off 10 minutes before retrying instead of hammering the API every 2 minutes while rate limited

---

## [1.0.2] - 2026-02-19

### Fixed
- **ERR on startup / after idle** — widget now re-reads `~/.claude/.credentials.json` on every poll cycle so a token refreshed by Claude Code overnight is picked up automatically, instead of staying stuck on the expired token loaded at startup

---

## [1.0.1] - 2026-02-19

### Fixed
- **Weekly reset timer** now shows days correctly (e.g. `4d 23h` instead of `119h 0m`)
- **Poll thread** wrapped in exception handler so a transient error no longer silently kills background refresh
- **Extra usage** (pay-as-you-go credits) was present in the API response but never displayed — it now appears in both the tray menu and the "Show Details" window

### Added
- **Extra Usage section** in the detail popup: shows monthly credit utilization with a colour-coded percentage and `used / limit` credits breakdown
- **Extra credits menu item**: displayed in the tray menu when extra usage is enabled on the account

---

## [1.0.0] - 2026-02-15

### Added
- Initial release: Claude AI Usage Widget for Linux
- System tray indicator showing 5-hour utilisation percentage
- Colour-coded "C" icon (green → yellow → orange → red)
- Click menu with 5h and 7d utilisation + reset timers
- "Show Details" popup with progress bars, reset timers, and subscription plan
- Threshold-based desktop notifications: startup, 75%, 90%, 100%
- Auto-detection of OAuth token from `~/.claude/.credentials.json`
- Autostart on login via `.desktop` entry
- `install.sh` / `uninstall.sh` helper scripts
- `validate.sh` pre-release quality-check script
- MIT licence — open source by Statotech Systems
