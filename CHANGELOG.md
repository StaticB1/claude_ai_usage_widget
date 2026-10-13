# Changelog

All notable changes to this project will be documented in this file.

## [2.3.0] - 2026-10-13

A redesign of the dashboard, the tray icon and the usage panel.

### Changed
- **A new look.** A warm cream canvas with near-black ink, a warm charcoal dark
  theme, one coral accent for the interface, a serif for page titles and a sans
  for everything you read or compare. Panels are a light core inside a cream
  shell. The app ships Geist, Geist Mono and Instrument Serif (SIL Open Font
  License) and registers them at start-up, so it no longer depends on the fonts
  a distribution happens to have.
- **Usage is coloured by state, not by brand.** Teal while there is room, amber
  past the warning threshold and red past the critical one, following the
  thresholds in Settings. The three colours were checked for colour-blind
  separation and contrast against both themes, and each always sits next to
  its number.
- **The dashboard is rebuilt.** Rings for the 5-hour and 7-day windows, with a
  third column for any separate weekly cap on one model; the current session
  with time left, burn rate and when the limit falls at this pace; one strip of
  totals; a 30-day chart of tokens or cost per day with the day's figures on
  hover; a tile per claudehop account with 5-hour and weekly meters; and the top
  projects and models as bars. Connection problems (rate limited, token
  rejected, offline, no token) show in the plan panel with a button that fixes
  or retries them.
- **The tray icon is a gauge.** It is drawn as a ring that fills with the
  5-hour window in the state colour, breaks when the connection fails, and
  shows a dot before the first reading.
- **The usage panel is rebuilt** with both rings, the current session, the
  account with the most room, and buttons to refresh or open the dashboard. It
  opens under the pointer and stays on screen.
- **Projects, Breakdowns and Budgets** put their tables in panels with
  right-aligned figures. Breakdowns shows each model's share of the cost (or
  each tool's share of the calls) above the table. Budgets lists each budget
  with a meter and keeps the form beside it, with errors shown in the form.
- A sidebar gauge shows the live account's 5-hour window on every page and
  opens the usage panel.
- Keyboard focus is drawn as an outline only when the keyboard moved it.

- **The account selector lists every claudehop account**, the live one first,
  whether or not tokens have been filed under it yet; then Bedrock, then the
  configured accounts when there is more than one. A single configured account
  is no longer listed beside "All accounts", since it held the same turns.
  Clicking an account tile filters the dashboard to that account, and clicking
  it again shows them all. An account with nothing filed under it says so and
  when the split by account began.

### Fixed
- **The window froze for half a second to a second at a time.** The dashboard
  queried the store on the interface thread every ten seconds (about 0.5 s on a
  large history), and opening Projects or Breakdowns took 0.6 to 0.8 s, long
  enough for the page fade to stall half-drawn over the previous page. Those
  queries now run in the background and are reused until new turns arrive (or
  a minute passes, for the session), so a refresh or a page switch takes a few
  milliseconds on the interface thread. The search box filters the last
  result instead of querying again.
- **Changing the account filter, the theme or pressing refresh rescanned every
  log on the interface thread.** The filter and theme now only redraw, and the
  refresh button scans in the background.
- The tray's session line and the budget check also read the store on the
  interface thread every ten seconds; both now work in the background.
- **A Team seat on a Max rate tier was labelled "Max 5x plan".** The seat type
  is now checked first, so it reads "Team plan".
- A cost of nothing reads `$0`, not `$0.000`.

## [2.2.0] - 2026-10-13

Brings the widget up to date with claudehop 1.6 to 1.9, and fixes the weekly
figure for Team and Enterprise seats.

### Fixed
- **Team and Enterprise seats showed a weekly figure of 0%.** Their usage reply
  has no `seven_day` window; the weekly caps come as rows in its `limits` list,
  often one per model. Reading only `seven_day`, the 7-day bar, the tray line,
  the weekly alerts and plan-percentage budgets all read 0% for these seats
  whatever had been used (on one account, 0% against a real 9%). The weekly
  figure is now the all-model window when there is one, else an all-model
  weekly row, else the fullest weekly meter, labelled with the model it covers
  (`7-Day Window · Fable`). That meter is not shown a second time as a model
  bar. Stored readings from earlier versions that had no weekly reset time had
  recorded that 0, and are cleared to unknown on start.
- Limits scoped to a surface rather than a model are named too.
- **The Projects tab raised OverflowError for a project over 2.1 billion tokens.**
  The numeric sort columns were 32-bit; they are now 64-bit.

### Added
- **Every claudehop account's usage, not only the live one's.** claudehop 1.6.0
  and later keep the last reading of each account in `accounts/.usage-cache`.
  The Accounts card, the tray and `ctt accounts` now show each account's newest
  reading, from the widget's own polling or from that cache, with its age, where
  it came from, and any server throttle claudehop has noted. A window whose
  reset time has passed reads `reset, was 84%` instead of its old figure.
- **Read all accounts.** A button on the Accounts card, and an item in the tray,
  runs `hop usage` once so claudehop reads every account. It never runs on its
  own, because each read spends a little of the usage endpoint's allowance. If
  another claudehop command holds claudehop's lock (an open `hop` picker holds
  it until you choose), the card says so instead of waiting. claudehop is never
  killed: a read that takes longer than two minutes is left to finish and its
  figures appear when it does.
- **The login with the most room.** The card, the tray and `ctt accounts` name
  the account `hop next` would choose: from claudehop's `next` list when there
  is one, never a login whose 5-hour window is at 95% or whose all-model weekly
  limit is used up, and the soonest reset first among close calls.
- **Tokens per claudehop account.** Each turn is now filed under the claudehop
  account that made it, in a new `hop_account` column. A conversation open in a
  `hop run` session belongs to that session's account (Claude Code names it in
  the session's own `sessions/` folder); anything else belongs to the account
  that was live at the time of the turn, dated from claudehop's `active`
  pointer, which it rewrites on every hop. The dashboard's account selector
  offers each claudehop account once it has turns, `--account hop:NAME` works on
  the command-line reports, and `ctt export` includes the column. History from
  before this version, and any stretch when neither the dashboard nor `ctt` ran
  to see a hop, stays unattributed rather than guessed.
- **`hop run` terminals.** Accounts open in a `hop run` session (claudehop 1.7.0
  and later) show how many on the card and in the tray.
- Filtering the dashboard to a claudehop account that is not live shows its last
  reading in the plan-limits section, and anchors the 5-hour block on its reset
  time.
- **Bedrock usage is tracked as its own account.** Claude Code on Amazon Bedrock
  logs the same model names as an Anthropic account, so the only tell is the
  response id, which Bedrock prefixes `msg_bdrk_`. Those turns are filed under the
  account `bedrock` at ingest and existing rows are relabelled on start, so they
  never count toward a Claude plan window. The dashboard offers "Bedrock" in the
  account selector once such turns exist, and `--account bedrock` works on the
  command-line reports. Cost is Anthropic list price, an estimate.
- **The widget says which claudehop account it is showing.** A new read-only module
  (`cct/hop.py`) reads claudehop's saved accounts. The dashboard header names the
  live account (or the provider new sessions run through), a new Accounts card lists
  every account with its last 5-hour and 7-day reading, the tray names the account
  and lists them, and `cct accounts` prints the same view. An account you have hopped
  away from is not polled, because that would mean renewing its token; it shows the
  last reading taken while it was active, with its age.

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
