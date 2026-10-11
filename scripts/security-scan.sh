#!/bin/bash
#====== AIxYad/scripts/security-scan.sh ======
# Runs security scans on the codebase
# Usage: ./security-scan.sh [--full] [--deps-only]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
FULL_SCAN=false
DEPS_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --full)
            FULL_SCAN=true
            shift
            ;;
        --deps-only)
            DEPS_ONLY=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--full] [--deps-only]"
            echo "  --full       Run all available security scans"
            echo "  --deps-only  Only scan dependencies"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

cd "$PROJECT_ROOT"

echo "========================================"
echo "Security Scan"
echo "========================================"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

ISSUES_FOUND=0
WARNINGS=0

# 1. Check for secrets in code
if [ "$DEPS_ONLY" != "true" ]; then
    echo "## Scanning for Secrets"

    # Common secret patterns
    SECRETS_PATTERNS=(
        "password\s*=\s*['\"][^'\"]+['\"]"
        "api[_-]?key\s*=\s*['\"][^'\"]+['\"]"
        "secret\s*=\s*['\"][^'\"]+['\"]"
        "token\s*=\s*['\"][^'\"]+['\"]"
        "AWS_ACCESS_KEY"
        "PRIVATE[_-]KEY"
    )

    for pattern in "${SECRETS_PATTERNS[@]}"; do
        MATCHES=$(grep -rniE "$pattern" --include="*.js" --include="*.ts" --include="*.py" \
            --include="*.json" --include="*.yaml" --include="*.yml" --include="*.env*" \
            . 2>/dev/null | grep -v node_modules | grep -v ".git" | head -5 || true)
        if [ -n "$MATCHES" ]; then
            echo "  ⚠ Potential secret found (pattern: $pattern):"
            echo "$MATCHES" | sed 's/^/    /'
            WARNINGS=$((WARNINGS + 1))
        fi
    done

    if [ "$WARNINGS" -eq 0 ]; then
        echo "  ✓ No obvious secrets found"
    fi
    echo ""
fi

# 2. Dependency vulnerabilities
echo "## Dependency Vulnerabilities"

# Node.js
if [ -f "package.json" ]; then
    echo "  Checking npm packages..."
    if npm audit --json 2>/dev/null | grep -q '"vulnerabilities"'; then
        VULN_COUNT=$(npm audit --json 2>/dev/null | grep -o '"total":[0-9]*' | head -1 | cut -d: -f2 || echo 0)
        if [ "$VULN_COUNT" -gt 0 ]; then
            echo "  ⚠ Found $VULN_COUNT vulnerabilities"
            npm audit 2>/dev/null | head -30 || true
            ISSUES_FOUND=$((ISSUES_FOUND + VULN_COUNT))
        else
            echo "  ✓ No npm vulnerabilities"
        fi
    else
        npm audit 2>/dev/null || echo "  ⚠ npm audit failed"
    fi
fi

# Python
if [ -f "requirements.txt" ]; then
    echo "  Checking Python packages..."
    if command -v pip-audit &> /dev/null; then
        pip-audit -r requirements.txt 2>/dev/null || ISSUES_FOUND=$((ISSUES_FOUND + 1))
    elif command -v safety &> /dev/null; then
        safety check -r requirements.txt 2>/dev/null || ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo "  ⚠ No Python security scanner installed (pip-audit or safety)"
    fi
fi

# Rust
if [ -f "Cargo.toml" ]; then
    echo "  Checking Cargo packages..."
    if command -v cargo-audit &> /dev/null; then
        cargo audit 2>/dev/null || ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo "  ⚠ cargo-audit not installed"
    fi
fi
echo ""

# 3. Code security patterns (if full scan)
if [ "$FULL_SCAN" == "true" ] && [ "$DEPS_ONLY" != "true" ]; then
    echo "## Code Security Patterns"

    # SQL Injection patterns
    echo "  Checking for SQL injection risks..."
    SQL_RISKS=$(grep -rn "execute.*\$\|query.*\+" --include="*.js" --include="*.ts" --include="*.py" \
        . 2>/dev/null | grep -v node_modules | grep -v ".git" | head -5 || true)
    if [ -n "$SQL_RISKS" ]; then
        echo "  ⚠ Potential SQL injection:"
        echo "$SQL_RISKS" | sed 's/^/    /'
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  ✓ No obvious SQL injection patterns"
    fi

    # XSS patterns
    echo "  Checking for XSS risks..."
    XSS_RISKS=$(grep -rn "innerHTML\|dangerouslySetInnerHTML\|v-html" --include="*.js" --include="*.ts" --include="*.tsx" --include="*.vue" \
        . 2>/dev/null | grep -v node_modules | grep -v ".git" | head -5 || true)
    if [ -n "$XSS_RISKS" ]; then
        echo "  ⚠ Potential XSS vectors:"
        echo "$XSS_RISKS" | sed 's/^/    /'
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  ✓ No obvious XSS patterns"
    fi

    # Eval patterns
    echo "  Checking for dangerous eval..."
    EVAL_RISKS=$(grep -rn "eval(\|new Function(" --include="*.js" --include="*.ts" \
        . 2>/dev/null | grep -v node_modules | grep -v ".git" | head -5 || true)
    if [ -n "$EVAL_RISKS" ]; then
        echo "  ⚠ Dangerous eval usage:"
        echo "$EVAL_RISKS" | sed 's/^/    /'
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  ✓ No eval usage found"
    fi
    echo ""
fi

# Summary
echo "========================================"
echo "Security Scan Summary"
echo "========================================"
echo "  Vulnerabilities: $ISSUES_FOUND"
echo "  Warnings:        $WARNINGS"
echo ""

if [ "$ISSUES_FOUND" -gt 0 ]; then
    echo "Status: ✗ SECURITY ISSUES FOUND"
    echo "Run: ./scripts/inject-task.sh -t \"Fix security vulnerabilities\" -p HIGH -a SecurityAuditor"
    exit 1
elif [ "$WARNINGS" -gt 0 ]; then
    echo "Status: ⚠ WARNINGS (review recommended)"
    exit 0
else
    echo "Status: ✓ PASSED"
    exit 0
fi
