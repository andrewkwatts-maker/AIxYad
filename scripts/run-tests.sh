#!/bin/bash
#====== AIxYad/scripts/run-tests.sh ======
# Runs project tests based on settings.json configuration
# Usage: ./run-tests.sh [--coverage] [--threshold N]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"
LOG_FILE="$PROJECT_ROOT/logs/tests.log"

# Defaults
RUN_COVERAGE=false
THRESHOLD=80

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --coverage)
            RUN_COVERAGE=true
            shift
            ;;
        --threshold)
            THRESHOLD="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--coverage] [--threshold N]"
            echo "  --coverage   Run with coverage analysis"
            echo "  --threshold  Coverage threshold percentage (default: 80)"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Read test configuration from settings
if [ -f "$SETTINGS_FILE" ]; then
    TEST_CMD=$(grep -o '"test_command"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    COV_CMD=$(grep -o '"coverage_command"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    COV_THRESHOLD=$(grep -o '"coverage_threshold"[[:space:]]*:[[:space:]]*[0-9]*' "$SETTINGS_FILE" | grep -o '[0-9]*$')
    THRESHOLD="${COV_THRESHOLD:-$THRESHOLD}"
fi

# Auto-detect test framework if not configured
if [ -z "$TEST_CMD" ]; then
    if [ -f "package.json" ]; then
        if grep -q '"test"' package.json; then
            TEST_CMD="npm test"
        fi
    elif [ -f "pytest.ini" ] || [ -f "pyproject.toml" ] || [ -d "tests" ]; then
        TEST_CMD="pytest"
    elif [ -f "Cargo.toml" ]; then
        TEST_CMD="cargo test"
    elif [ -f "go.mod" ]; then
        TEST_CMD="go test ./..."
    elif [ -f "pom.xml" ]; then
        TEST_CMD="mvn test"
    elif [ -f "build.gradle" ]; then
        TEST_CMD="./gradlew test"
    fi
fi

if [ -z "$TEST_CMD" ]; then
    echo "No test command configured and could not auto-detect."
    echo "Configure 'test_command' in settings.json"
    exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running tests: $TEST_CMD" >> "$LOG_FILE"

echo "========================================"
echo "Running Tests"
echo "========================================"
echo "Command: $TEST_CMD"
echo ""

# Run tests
if $TEST_CMD; then
    echo ""
    echo "✓ Tests passed"
    TEST_RESULT=0
else
    echo ""
    echo "✗ Tests failed"
    TEST_RESULT=1
fi

# Run coverage if requested
if [ "$RUN_COVERAGE" == "true" ]; then
    echo ""
    echo "========================================"
    echo "Running Coverage Analysis"
    echo "========================================"

    if [ -z "$COV_CMD" ]; then
        # Auto-detect coverage command
        if [ -f "package.json" ]; then
            COV_CMD="npm run coverage 2>/dev/null || npm test -- --coverage"
        elif command -v pytest &> /dev/null; then
            COV_CMD="pytest --cov=. --cov-report=term-missing"
        elif [ -f "Cargo.toml" ]; then
            COV_CMD="cargo tarpaulin"
        elif [ -f "go.mod" ]; then
            COV_CMD="go test -coverprofile=coverage.out ./... && go tool cover -func=coverage.out"
        fi
    fi

    if [ -n "$COV_CMD" ]; then
        echo "Command: $COV_CMD"
        eval "$COV_CMD" || true
        echo ""
        echo "Coverage threshold: ${THRESHOLD}%"
    else
        echo "No coverage command configured"
    fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Test result: $([ $TEST_RESULT -eq 0 ] && echo 'PASSED' || echo 'FAILED')" >> "$LOG_FILE"

exit $TEST_RESULT
