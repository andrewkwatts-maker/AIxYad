#!/bin/bash
#====== Automatica/scripts/lint-code.sh ======
# Runs linters and formatters based on project type
# Usage: ./lint-code.sh [--fix] [--check-only] [path]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
FIX_MODE=false
CHECK_ONLY=false
TARGET_PATH="."

while [[ $# -gt 0 ]]; do
    case $1 in
        --fix)
            FIX_MODE=true
            shift
            ;;
        --check-only)
            CHECK_ONLY=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--fix] [--check-only] [path]"
            echo "  --fix         Auto-fix issues where possible"
            echo "  --check-only  Only check, exit 1 if issues found"
            echo "  path          Specific path to lint (default: .)"
            exit 0
            ;;
        *)
            TARGET_PATH="$1"
            shift
            ;;
    esac
done

cd "$PROJECT_ROOT"

echo "========================================"
echo "Code Linting"
echo "========================================"
echo ""

LINT_PASSED=true

# Detect and run appropriate linters

# Node.js / JavaScript / TypeScript
if [ -f "package.json" ]; then
    echo "## JavaScript/TypeScript"

    # Check for ESLint
    if grep -q '"eslint"' package.json 2>/dev/null || [ -f ".eslintrc.js" ] || [ -f ".eslintrc.json" ]; then
        echo "Running ESLint..."
        if [ "$FIX_MODE" == "true" ]; then
            npx eslint "$TARGET_PATH" --fix 2>/dev/null || LINT_PASSED=false
        else
            npx eslint "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
        fi
    fi

    # Check for Prettier
    if grep -q '"prettier"' package.json 2>/dev/null || [ -f ".prettierrc" ]; then
        echo "Running Prettier..."
        if [ "$FIX_MODE" == "true" ]; then
            npx prettier --write "$TARGET_PATH" 2>/dev/null || true
        else
            npx prettier --check "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
        fi
    fi

    # TypeScript check
    if [ -f "tsconfig.json" ]; then
        echo "Running TypeScript check..."
        npx tsc --noEmit 2>/dev/null || LINT_PASSED=false
    fi
    echo ""
fi

# Python
if [ -f "pyproject.toml" ] || [ -f "setup.py" ] || [ -f "requirements.txt" ]; then
    echo "## Python"

    # Ruff (fast linter)
    if command -v ruff &> /dev/null; then
        echo "Running Ruff..."
        if [ "$FIX_MODE" == "true" ]; then
            ruff check "$TARGET_PATH" --fix 2>/dev/null || LINT_PASSED=false
        else
            ruff check "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
        fi
    # Fallback to flake8
    elif command -v flake8 &> /dev/null; then
        echo "Running Flake8..."
        flake8 "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
    fi

    # Black formatter
    if command -v black &> /dev/null; then
        echo "Running Black..."
        if [ "$FIX_MODE" == "true" ]; then
            black "$TARGET_PATH" 2>/dev/null || true
        else
            black --check "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
        fi
    fi

    # Type checking with mypy
    if command -v mypy &> /dev/null && [ -f "pyproject.toml" ]; then
        echo "Running MyPy..."
        mypy "$TARGET_PATH" 2>/dev/null || LINT_PASSED=false
    fi
    echo ""
fi

# Rust
if [ -f "Cargo.toml" ]; then
    echo "## Rust"
    echo "Running cargo fmt check..."
    if [ "$FIX_MODE" == "true" ]; then
        cargo fmt 2>/dev/null || true
    else
        cargo fmt --check 2>/dev/null || LINT_PASSED=false
    fi

    echo "Running cargo clippy..."
    cargo clippy 2>/dev/null || LINT_PASSED=false
    echo ""
fi

# Go
if [ -f "go.mod" ]; then
    echo "## Go"
    if [ "$FIX_MODE" == "true" ]; then
        echo "Running gofmt..."
        gofmt -w "$TARGET_PATH" 2>/dev/null || true
    else
        echo "Checking gofmt..."
        test -z "$(gofmt -l "$TARGET_PATH" 2>/dev/null)" || LINT_PASSED=false
    fi

    echo "Running go vet..."
    go vet ./... 2>/dev/null || LINT_PASSED=false
    echo ""
fi

# Shell scripts
echo "## Shell Scripts"
if command -v shellcheck &> /dev/null; then
    echo "Running ShellCheck..."
    find "$TARGET_PATH" -name "*.sh" -type f ! -path './node_modules/*' -exec shellcheck {} \; 2>/dev/null || LINT_PASSED=false
else
    echo "ShellCheck not installed (skipping)"
fi
echo ""

# Summary
echo "========================================"
if [ "$LINT_PASSED" == "true" ]; then
    echo "Result: ✓ All checks passed"
    exit 0
else
    echo "Result: ✗ Some checks failed"
    if [ "$CHECK_ONLY" == "true" ]; then
        exit 1
    fi
    exit 0
fi
