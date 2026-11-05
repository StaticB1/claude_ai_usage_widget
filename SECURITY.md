# Security

## What this tool reads and sends

The token tracker half works from files on your own disk. It reads Claude Code's
session logs under `~/.claude/projects` and never changes them.

The live usage widget also reads the OAuth access token that Claude Code keeps
in `~/.claude/.credentials.json`. That token is sent to one place only,
`GET https://api.anthropic.com/api/oauth/usage`, with a `User-Agent` of
`claude-token-tracker/<version>`, so the request says which tool made it and
does not pose as Claude Code. The token is never written to the tracker's
database, config or logs, and the tracker never writes Claude Code's
credentials. Turn the polling off per account in Settings, or use the CLI with
`--no-cloud`; see "About the live usage feature" in the README for what using
the token from a third-party tool means for your account.

With [claudehop](https://github.com/psychofict/claudehop) installed, the widget
also reads `~/.claude/accounts`. It compares the saved logins' access tokens
with the live one in memory to tell which account is live, and keeps none of
them. It reads claudehop's usage cache, `active` and `next` files and its
`hop run` folders, and never writes to any of them. The "Read all accounts"
button runs `hop usage --json`, which makes claudehop's own requests.

Everything the tracker writes stays in its own folder,
`~/.config/claude-token-tracker` (the history database, settings, notification
state and lock file). It sends no telemetry.

## Reporting a vulnerability

Email contact@ebenworks.co with what you found and how to reproduce it. Please
don't file a public issue for anything that could expose a token. Expect a
first reply within a week.
