#!/bin/bash
#====== Automatica/scripts/validate-task.sh ======
# Validates if the current task meets its Definition of Done
# Usage: ./validate-task.sh [--strict]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CURRENT_TASK_FILE="$PROJECT_ROOT/CURRENT_TASK.md"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"

# Parse arguments
STRICT_MODE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --strict)
            STRICT_MODE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--strict]"
            echo "  --strict    Fail on any unchecked criterion"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Check if current task exists
if [ ! -f "$CURRENT_TASK_FILE" ]; then
    echo "No current task found (CURRENT_TASK.md missing)"
    exit 1
fi

TASK_CONTENT=$(cat "$CURRENT_TASK_FILE")

echo "========================================"
echo "Task Validation"
echo "========================================"
echo ""

# Extract task title
TASK_TITLE=$(echo "$TASK_CONTENT" | sed -n '/^\[/d; /^---/d; /^$/d; p' | head -1)
echo "Task: $TASK_TITLE"
echo ""

# Count acceptance criteria
TOTAL_CRITERIA=$(echo "$TASK_CONTENT" | grep -c "^\- \[ \]" || echo 0)
CHECKED_CRITERIA=$(echo "$TASK_CONTENT" | grep -c "^\- \[x\]" || echo 0)
UNCHECKED_CRITERIA=$((TOTAL_CRITERIA))  # Original unchecked

echo "Acceptance Criteria:"
echo "  Total:     $TOTAL_CRITERIA"
echo "  Checked:   $CHECKED_CRITERIA"
echo "  Unchecked: $UNCHECKED_CRITERIA"
echo ""

# Check various quality gates
VALIDATION_PASSED=true
WARNINGS=0
ERRORS=0

echo "Quality Gates:"
echo ""

# 1. Check if tests pass
echo -n "  Tests: "
if "$SCRIPT_DIR/run-tests.sh" > /dev/null 2>&1; then
    echo "✓ Passing"
else
    echo "✗ Failing or not configured"
    if [ "$STRICT_MODE" == "true" ]; then
        ERRORS=$((ERRORS + 1))
        VALIDATION_PASSED=false
    else
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# 2. Check for uncommitted changes
echo -n "  Git Status: "
if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
    echo "✓ Clean"
else
    echo "⚠ Uncommitted changes"
    WARNINGS=$((WARNINGS + 1))
fi

# 3. Check for backup files remaining
echo -n "  Temp Files: "
TEMP_DIR="$PROJECT_ROOT/temp"
if [ -d "$TEMP_DIR" ] && [ "$(find "$TEMP_DIR" -type f 2>/dev/null | wc -l)" -gt 0 ]; then
    TEMP_COUNT=$(find "$TEMP_DIR" -type f 2>/dev/null | wc -l)
    echo "⚠ $TEMP_COUNT backup file(s) remaining"
    WARNINGS=$((WARNINGS + 1))
else
    echo "✓ Clean"
fi

# 4. Check linting (if configured)
echo -n "  Linting: "
if [ -f "$PROJECT_ROOT/package.json" ] && grep -q '"lint"' "$PROJECT_ROOT/package.json" 2>/dev/null; then
    if npm run lint --silent 2>/dev/null; then
        echo "✓ Passing"
    else
        echo "✗ Failing"
        if [ "$STRICT_MODE" == "true" ]; then
            ERRORS=$((ERRORS + 1))
            VALIDATION_PASSED=false
        else
            WARNINGS=$((WARNINGS + 1))
        fi
    fi
else
    echo "- Not configured"
fi

# 5. Check acceptance criteria completion
echo -n "  Acceptance Criteria: "
if [ "$UNCHECKED_CRITERIA" -eq 0 ] && [ "$TOTAL_CRITERIA" -gt 0 ]; then
    echo "✓ All complete"
elif [ "$TOTAL_CRITERIA" -eq 0 ]; then
    echo "⚠ None defined"
    WARNINGS=$((WARNINGS + 1))
else
    echo "✗ $UNCHECKED_CRITERIA unchecked"
    if [ "$STRICT_MODE" == "true" ]; then
        ERRORS=$((ERRORS + 1))
        VALIDATION_PASSED=false
    fi
fi

echo ""
echo "========================================"
echo "Summary"
echo "========================================"
echo "  Warnings: $WARNINGS"
echo "  Errors:   $ERRORS"
echo ""

if [ "$VALIDATION_PASSED" == "true" ] && [ "$ERRORS" -eq 0 ]; then
    echo "Status: ✓ READY FOR COMPLETION"
    echo ""
    echo "Run: ./scripts/end-task.sh --push"
    exit 0
else
    echo "Status: ✗ NOT READY"
    echo ""
    echo "Fix the errors above before completing the task."
    exit 1
fi
