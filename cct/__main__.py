import sys

# Claude Code runs the status line on every refresh, so `ctt statusline`
# skips the rest of the CLI and its imports.
if sys.argv[1:2] == ['statusline'] and sys.argv[2:3] not in (['-h'], ['--help']):
    from .statusline import run
    command = sys.argv[2:]
    sys.exit(run(command[1:] if command[:1] == ['--'] else command))

from .cli import main  # noqa: E402

sys.exit(main())
