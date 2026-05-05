#!/bin/bash
#====== Automatica/scripts/check-dependencies.sh ======
# Checks project dependencies for issues (outdated, vulnerable, unused)
# Usage: ./check-dependencies.sh [--security] [--outdated] [--update]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
CHECK_SECURITY=false
CHECK_OUTDATED=false
DO_UPDATE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --security)
            CHECK_SECURITY=true
            shift
            ;;
        --outdated)
            CHECK_OUTDATED=true
            shift
            ;;
        --update)
            DO_UPDATE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--security] [--outdated] [--update]"
            echo "  --security   Check for security vulnerabilities"
            echo "  --outdated   Check for outdated packages"
            echo "  --update     Update dependencies (interactive)"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Default to checking everything
if [ "$CHECK_SECURITY" == "false" ] && [ "$CHECK_OUTDATED" == "false" ]; then
    CHECK_SECURITY=true
    CHECK_OUTDATED=true
fi

cd "$PROJECT_ROOT"

echo "========================================"
echo "Dependency Check"
echo "========================================"
echo ""

# Node.js
if [ -f "package.json" ]; then
    echo "## Node.js Dependencies"

    # Count dependencies
    DEPS=$(grep -c '"[^"]*":' package.json 2>/dev/null || echo 0)
    echo "  Total packages in package.json: ~$DEPS"
    echo ""

    if [ "$CHECK_OUTDATED" == "true" ]; then
        echo "  ### Outdated Packages"
        npm outdated 2>/dev/null || echo "  All packages up to date or npm outdated failed"
        echo ""
    fi

    if [ "$CHECK_SECURITY" == "true" ]; then
        echo "  ### Security Audit"
        npm audit 2>/dev/null || echo "  Audit completed (check output above)"
        echo ""
    fi

    if [ "$DO_UPDATE" == "true" ]; then
        echo "  ### Updating Dependencies"
        npm update 2>/dev/null || echo "  Update failed"
        echo ""
    fi
fi

# Python
if [ -f "requirements.txt" ] || [ -f "pyproject.toml" ]; then
    echo "## Python Dependencies"

    if [ -f "requirements.txt" ]; then
        DEPS=$(wc -l < requirements.txt 2>/dev/null || echo 0)
        echo "  Total packages in requirements.txt: $DEPS"
        echo ""
    fi

    if [ "$CHECK_OUTDATED" == "true" ]; then
        echo "  ### Outdated Packages"
        if command -v pip &> /dev/null; then
            pip list --outdated 2>/dev/null | head -20 || echo "  Could not check outdated"
        fi
        echo ""
    fi

    if [ "$CHECK_SECURITY" == "true" ]; then
        echo "  ### Security Check"
        if command -v pip-audit &> /dev/null; then
            pip-audit 2>/dev/null || echo "  Audit completed"
        elif command -v safety &> /dev/null; then
            safety check 2>/dev/null || echo "  Check completed"
        else
            echo "  No security scanner installed (pip-audit or safety)"
        fi
        echo ""
    fi

    if [ "$DO_UPDATE" == "true" ] && [ -f "requirements.txt" ]; then
        echo "  ### Updating Dependencies"
        pip install --upgrade -r requirements.txt 2>/dev/null || echo "  Update failed"
        echo ""
    fi
fi

# Rust
if [ -f "Cargo.toml" ]; then
    echo "## Rust Dependencies"

    if [ "$CHECK_OUTDATED" == "true" ]; then
        echo "  ### Outdated Packages"
        if command -v cargo-outdated &> /dev/null; then
            cargo outdated 2>/dev/null || echo "  Could not check outdated"
        else
            echo "  cargo-outdated not installed"
        fi
        echo ""
    fi

    if [ "$CHECK_SECURITY" == "true" ]; then
        echo "  ### Security Audit"
        if command -v cargo-audit &> /dev/null; then
            cargo audit 2>/dev/null || echo "  Audit completed"
        else
            echo "  cargo-audit not installed"
        fi
        echo ""
    fi

    if [ "$DO_UPDATE" == "true" ]; then
        echo "  ### Updating Dependencies"
        cargo update 2>/dev/null || echo "  Update failed"
        echo ""
    fi
fi

# Go
if [ -f "go.mod" ]; then
    echo "## Go Dependencies"

    if [ "$CHECK_OUTDATED" == "true" ]; then
        echo "  ### Checking for updates"
        go list -u -m all 2>/dev/null | head -20 || echo "  Could not check"
        echo ""
    fi

    if [ "$DO_UPDATE" == "true" ]; then
        echo "  ### Updating Dependencies"
        go get -u ./... 2>/dev/null || echo "  Update failed"
        go mod tidy 2>/dev/null || true
        echo ""
    fi
fi

echo "========================================"
echo "Dependency check complete"
echo "========================================"
