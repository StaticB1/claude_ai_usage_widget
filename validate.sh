#!/usr/bin/env bash
# Pre-release validation.
#
# Everything here checks the cct/ package. It used to check a single-file v1
# widget that no longer ships, so it read the version from a file the
# installer never copied and reported a stale number for every release.
set -uo pipefail

echo "Claude Usage Widget — pre-release validation"
echo "==========================================="
echo ""

ERRORS=0
fail() { echo "  x $1"; ERRORS=$((ERRORS + 1)); }
ok()   { echo "  - $1"; }
warn() { echo "  ! $1"; }

PYTHON="${PYTHON:-python3}"

echo "> Compiling the package..."
if "$PYTHON" -m compileall -q cct >/dev/null 2>&1; then
    ok "cct/ compiles"
else
    fail "syntax errors in cct/"
    "$PYTHON" -m compileall -q cct
fi

echo "> Checking shell scripts..."
sh_bad=0
for f in install.sh uninstall.sh upgrade.sh validate.sh; do
    bash -n "$f" 2>/dev/null || { fail "$f has syntax errors"; sh_bad=1; }
done
[ "$sh_bad" -eq 0 ] && ok "shell scripts parse"

echo "> Running the test suite..."
if "$PYTHON" -m pytest -q >/tmp/ctt-validate-pytest.log 2>&1; then
    ok "$(tail -1 /tmp/ctt-validate-pytest.log)"
else
    fail "tests failed"
    tail -20 /tmp/ctt-validate-pytest.log
fi

echo "> Checking the CLI runs..."
if "$PYTHON" -m cct --version >/dev/null 2>&1; then
    ok "ctt --version works"
else
    fail "the CLI does not start"
fi

echo "> Checking for real tokens in the repository..."
if grep -rEq "sk-ant-oat[0-9a-zA-Z_-]{50,}" . \
        --exclude-dir=.git --exclude-dir=.claude --exclude-dir=__pycache__ \
        --exclude="*.pyc" 2>/dev/null; then
    fail "a real OAuth token is committed"
else
    ok "no real tokens (placeholders are fine)"
fi

echo "> Checking required files..."
for file in LICENSE README.md CHANGELOG.md pyproject.toml install.sh \
            uninstall.sh upgrade.sh .gitignore screenshot.png \
            claude_token_tracker.py cct/__init__.py cct/gui.py cct/cli.py; do
    [ -f "$file" ] && ok "$file" || fail "$file missing"
done

echo "> Checking for unresolved TODOs..."
# --exclude this file: it greps for those words, so it always matched itself.
todos=$(grep -rn "TODO\|FIXME\|XXX" --include="*.py" --include="*.sh" \
        --exclude-dir=__pycache__ --exclude=validate.sh . 2>/dev/null || true)
if [ -n "$todos" ]; then
    warn "TODO/FIXME present:"
    echo "$todos" | sed 's/^/      /'
else
    ok "no TODO/FIXME"
fi

echo "> Checking README placeholders..."
if grep -q "YOUR_USERNAME\|<this-repo>\|TODO" README.md; then
    fail "README still contains placeholders"
else
    ok "README placeholders resolved"
fi

echo "> Checking version consistency..."
PKG_VERSION=$(grep -oP "^APP_VERSION\s*=\s*'\K[^']+" cct/config.py)
TOML_VERSION=$(grep -oP '^version\s*=\s*"\K[^"]+' pyproject.toml)
if [ -z "$PKG_VERSION" ]; then
    fail "could not read APP_VERSION from cct/config.py"
elif [ "$PKG_VERSION" != "$TOML_VERSION" ]; then
    fail "cct/config.py says $PKG_VERSION, pyproject.toml says $TOML_VERSION"
else
    ok "version $PKG_VERSION matches in both places"
    grep -q "\[$PKG_VERSION\]\|## $PKG_VERSION" CHANGELOG.md \
        && ok "CHANGELOG has an entry for $PKG_VERSION" \
        || fail "CHANGELOG has no entry for $PKG_VERSION"
    git tag | grep -qx "v$PKG_VERSION" \
        && ok "git tag v$PKG_VERSION exists" \
        || warn "git tag v$PKG_VERSION not created yet"
fi

echo ""
echo "==========================================="
if [ "$ERRORS" -eq 0 ]; then
    echo "All checks passed."
    exit 0
fi
echo "$ERRORS check(s) failed."
exit 1
