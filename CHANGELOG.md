# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [2.1.0] - 2026-08-20

Audit release. The 5-hour session window now matches what `claude /usage`
reports, tool attribution recovers the 59% of calls it was discarding, and
costs are priced from the published rate card instead of a stale one.

### Fixed
- **5-hour session window disagreed with the account's real window.** The
  window was inferred from the first message in the local Claude Code logs.
  It cannot be: usage from claude.ai, the desktop and mobile apps and other
  machines all counts toward the same window, and its start is a server-side
  fact. Measured against the live API, the local guess ended at 05:01:36Z
  while the account's window ended at 05:30:00Z — a 28-minute error in the
  countdown, the burn rate and the ETA. The active window is now anchored on
  the API's `resets_at`, with local rows summed inside it. Local inference
  survives as a fallback for when there is no token or the network is down,
  and the dashboard and tray label it as an estimate.
- **The window moved depending on who asked.** The dashboard, the tray,
  `ctt block` and `ctt prompt` each read a different span of history (24h,
  24h, 24h, 6h) and inferred boundaries from whatever row their query clipped
  first, so the same data produced different sessions in each. All four now
  share one definition.
- **Tool counts were missing 59% of calls.** Claude Code splits one API
  response across several log entries that repeat the same `message.id`, with
  the text on the first and each `tool_use` on a later one. The parser skipped
  every duplicate id wholesale. Measured on a real history: 37,351 calls
  counted out of 91,101 present, Bash alone 18,129 out of 51,295. Duplicate
  entries now merge their tool calls into the recorded turn.
- **"Today" was cut at UTC midnight while labelled with the local date.**
  Measured at 08:00 in Seoul: the dashboard showed 0 messages and 0 tokens
  for a local day that already held 4 messages and 3.3M tokens. Calendar
  periods — today, and the day/week/month budget windows — now follow the
  local calendar. A monthly cap rolls over at local midnight on the 1st.
- **Costs were priced from a stale rate card.** `claude-opus-5` and
  `claude-sonnet-5` had no entry at all and fell through to a family guess;
  Opus 4.5 through 4.8 were priced at Opus 4.1's $15/$75 rather than $5/$25;
  Haiku 4.5 carried retired Haiku 3.5's price. Dated snapshot ids
  (`claude-haiku-4-5-20251001`) never matched their own entry. Cache rates are
  now derived from the documented multipliers (1.25x for a 5-minute write, 2x
  for an hour, 0.1x for a read) instead of four hand-typed numbers per model,
  and a cost from a guessed rate is reported as approximate.
- **Utilization above 100% displayed as exactly 100%.** Overage on a plan
  limit now reads as the figure it is; progress bars cap at full without
  capping the number beside them.
- **Burn rate and time-to-limit measured against different clocks** —
  wall-clock for one, time-to-last-message for the other — so the ETA
  contradicted the rate it was derived from.
- **Daily charts bucketed by UTC date**, splitting a local evening's work
  across two bars.
- **Output tokens were under-counted on 5,163 messages.** The repeated log
  entries of one response carry identical input, cache-creation and cache-read
  counts, but the earlier ones often carry a placeholder output count (4) and
  only the last carries the real one (568). Keeping the first entry lost 5.44M
  output tokens, 7.6% of all output on disk and the dearest token class there
  is. Each token count is now the highest seen for the message.
- **A log entry with no timestamp was recorded as happening now**, dropping a
  historical turn into the live 5-hour window. It now takes the previous
  entry's time, or the file's own modified time.
- **The first turn of a session could keep the project name "Unknown"** when
  the working directory appeared on a later line than the first response.
- **The tray's 5-hour figures mixed accounts** — every account's messages were
  summed inside the primary account's window.
- **Dashboard bars disagreed with the numbers beside them.** Top Projects
  ranked and drew by tokens while printing cost, Top Models did the reverse,
  so a shorter bar could sit next to a bigger number in both cards. Each card
  now ranks, draws and prints the same measure, and says which.
- **`ctt models`, `ctt tools` and `ctt accounts` printed no period**, so their
  default 30-day or 7-day figures read as lifetime totals.
- **`ctt tools` cut MCP tool names to 22 characters**, and MCP names share a
  long prefix, so several different tools rendered as the same row.
- `validate.sh` compiled, permission-checked and read the version from a
  single-file widget that no longer ships, so it reported version 1.0.4 for
  a 2.0.0 release. It now compiles the package, runs the tests, starts the
  CLI and checks the version against `pyproject.toml` and the changelog.

### Added
- **An edited rate card takes effect while the app runs.** `rate_card.json`
  was read once when the poll thread started, so a corrected price sat unused
  until the next launch. A change is now picked up, the logs are re-read and
  stored costs are rewritten.

### Changed
- **Upserts replace insert-or-ignore.** A rescan used to leave existing rows
  untouched, so a parser or pricing fix could never repair recorded history.
  Rows are now updated in place, and a database written by an older version
  is rescanned and repriced once on first launch.
- **The dashboard aggregates in SQL.** It was loading and summing every row
  on the interface thread on each 10-second refresh — 0.44s for 166,744 rows,
  now 10ms. The Projects view did the same and then grouped those rows by
  session in Python; sessions are grouped in SQL now. Connections are
  per-thread and persistent rather than opened, WAL-mode set and closed on
  every call, and repricing runs one statement per model rather than one per
  row.
- **One countdown helper, not two.** The same reset time rendered as
  "unknown"/"any moment" in one part of the window and "—"/"now" in another.
  The three places that read a utilization percentage also each carried their
  own copy of the parsing and its ceiling.

### Removed
- The v1 single-file widget, superseded by the `cct` package at 2.0.0 and
  neither installed nor imported since.
- Unreachable code: a tray-icon generator that wrote an SVG to a predictable
  path in the system temp directory and was never called, a rate-card writer
  nothing wrote with, and four unused helpers.

### Added
- **Per-model plan limits** — the tray **Limits** section and the dashboard
  plan-limits card now show a row/bar for each model-scoped weekly cap the
  usage API reports (e.g. a separate **Opus** weekly limit on Max plans), each
  with its own percentage and reset countdown; a `●` marks the limit currently
  binding your usage. Read from the API's `limits[]` array (kind
  `weekly_scoped`); previously only the account-wide 5h / 7d windows were shown.
- **Appearance setting** — Settings now has a System / Light / Dark theme
  switch. System follows the desktop's `org.gnome.desktop.interface`
  color-scheme live (no restart); switching updates the whole app,
  including per-widget colors (usage bars, summary cards) that a plain
  CSS-provider swap doesn't reach.
- **Tray menu, redone** — the 5h/7d limits and the current 5-hour block now
  render as small colored progress indicators instead of plain text, and a
  new **Usage panel…** entry (also reachable via middle-click on the tray
  icon) opens a small themed standalone window with real progress bars and
  cloud connection status — useful since the AppIndicator dropdown itself
  can't be restyled by the app.
- **Scrollable dashboard views** — Dashboard/Projects/Breakdowns/Budgets/
  Settings now scroll vertically, so the window can be resized well below
  its previous 900×600 floor without clipping content.
- **Pricing** — added `claude-fable-5`, `claude-mythos-5`, and
  `claude-opus-4-8` to the built-in rate card.

### Fixed
- `claude-mythos-5` no longer silently priced as `claude-fable-5` in the
  substring-match fallback.
- `theme` values from a hand-edited config are now validated against
  `{system, light, dark}` instead of accepted as-is.

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
