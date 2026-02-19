# Changelog

All notable changes to this project will be documented in this file.

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
