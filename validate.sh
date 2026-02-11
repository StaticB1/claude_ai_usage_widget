#!/bin/bash
# Pre-release validation script
echo "🔍 Claude Usage Widget - Pre-Release Validation"
echo "================================================"
echo ""

ERRORS=0

# 1. Check Python syntax
echo "▸ Checking Python syntax..."
if python3 -m py_compile claude_usage_widget.py 2>/dev/null; then
    echo "  ✓ Python syntax valid"
else
    echo "  ✗ Python syntax errors found"
    ((ERRORS++))
fi

# 2. Check shell script syntax
echo "▸ Checking shell scripts..."
if bash -n install.sh 2>/dev/null && bash -n uninstall.sh 2>/dev/null; then
    echo "  ✓ Shell scripts valid"
else
    echo "  ✗ Shell script syntax errors"
    ((ERRORS++))
fi

# 3. Check for sensitive data (real tokens, not placeholders)
echo "▸ Checking for real tokens in repository..."
# Look for tokens that aren't followed by "..." (placeholders)
if grep -rE "sk-ant-oat[0-9a-zA-Z_-]{50,}" . --exclude-dir=.git --exclude-dir=.claude --exclude="*.pyc" >/dev/null 2>&1; then
    echo "  ✗ WARNING: Real token found in repository!"
    ((ERRORS++))
else
    echo "  ✓ No real tokens in repository (placeholders OK)"
fi

# 4. Check file permissions
echo "▸ Checking file permissions..."
if [[ $(stat -c %a claude_usage_widget.py) == "644" ]]; then
    echo "  ✓ Main script has correct permissions"
else
    echo "  ⚠ Main script permissions: $(stat -c %a claude_usage_widget.py)"
fi

# 5. Check required files
echo "▸ Checking required files..."
REQUIRED_FILES=(
    "LICENSE"
    "README.md"
    "claude_usage_widget.py"
    "install.sh"
    "uninstall.sh"
    ".gitignore"
    "screenshot.png"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        echo "  ✓ $file exists"
    else
        echo "  ✗ $file missing"
        ((ERRORS++))
    fi
done

# 6. Check for TODO/FIXME comments
echo "▸ Checking for unresolved TODOs..."
if grep -rn "TODO\|FIXME\|XXX" --include="*.py" --include="*.sh" . 2>/dev/null; then
    echo "  ⚠ Found TODO/FIXME comments (review before release)"
else
    echo "  ✓ No TODO/FIXME found"
fi

# 7. Check README placeholders
echo "▸ Checking README placeholders..."
if grep -q "YOUR_USERNAME\|<this-repo>\|TODO" README.md; then
    echo "  ✗ README contains placeholders"
    ((ERRORS++))
else
    echo "  ✓ README placeholders resolved"
fi

# 8. Check version consistency
echo "▸ Checking version consistency..."
VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' claude_usage_widget.py)
if git tag | grep -q "v$VERSION"; then
    echo "  ✓ Git tag v$VERSION exists"
else
    echo "  ⚠ Git tag v$VERSION not found"
fi

echo ""
echo "================================================"
if [[ $ERRORS -eq 0 ]]; then
    echo "✅ All checks passed! Ready for release."
    exit 0
else
    echo "❌ Found $ERRORS error(s). Please fix before releasing."
    exit 1
fi
