#!/bin/bash
#====== Automatica/scripts/get-context.sh ======
# Extracts relevant context for a task (related files, dependencies)
# Usage: ./get-context.sh "search term" [--files-only] [--deps]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
SEARCH_TERM=""
FILES_ONLY=false
SHOW_DEPS=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --files-only)
            FILES_ONLY=true
            shift
            ;;
        --deps)
            SHOW_DEPS=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 \"search term\" [--files-only] [--deps]"
            echo "  --files-only    Only list matching files"
            echo "  --deps          Include dependency info"
            exit 0
            ;;
        *)
            if [ -z "$SEARCH_TERM" ]; then
                SEARCH_TERM="$1"
            fi
            shift
            ;;
    esac
done

if [ -z "$SEARCH_TERM" ]; then
    echo "Usage: $0 \"search term\" [--files-only] [--deps]"
    exit 1
fi

cd "$PROJECT_ROOT"

echo "========================================"
echo "Context Search: $SEARCH_TERM"
echo "========================================"
echo ""

# Find files matching the term
echo "## Matching Files"
MATCHING_FILES=$(grep -rl "$SEARCH_TERM" --include="*.js" --include="*.ts" --include="*.tsx" --include="*.jsx" \
    --include="*.py" --include="*.rs" --include="*.go" --include="*.java" --include="*.cs" --include="*.cpp" --include="*.c" \
    --include="*.md" --include="*.json" \
    . 2>/dev/null | grep -v node_modules | grep -v ".git" | head -20 || echo "")

if [ -n "$MATCHING_FILES" ]; then
    echo "$MATCHING_FILES" | while read -r file; do
        if [ "$FILES_ONLY" == "true" ]; then
            echo "$file"
        else
            COUNT=$(grep -c "$SEARCH_TERM" "$file" 2>/dev/null || echo 0)
            echo "  $file ($COUNT matches)"
        fi
    done
else
    echo "  No files found matching '$SEARCH_TERM'"
fi
echo ""

if [ "$FILES_ONLY" == "true" ]; then
    exit 0
fi

# Find file names matching the term
echo "## Files Named Similarly"
find . -type f -iname "*${SEARCH_TERM}*" ! -path './node_modules/*' ! -path './.git/*' 2>/dev/null | head -10 || echo "  None found"
echo ""

# Show imports/requires in matching files
if [ -n "$MATCHING_FILES" ]; then
    echo "## Dependencies in Matching Files"
    echo "$MATCHING_FILES" | head -5 | while read -r file; do
        echo "  $file:"
        # JavaScript/TypeScript imports
        grep -E "^import|^const.*require|^from " "$file" 2>/dev/null | head -5 | sed 's/^/    /'
        # Python imports
        grep -E "^import |^from .* import" "$file" 2>/dev/null | head -5 | sed 's/^/    /'
    done
    echo ""
fi

# Show exports from matching files
if [ -n "$MATCHING_FILES" ]; then
    echo "## Exports from Matching Files"
    echo "$MATCHING_FILES" | head -5 | while read -r file; do
        EXPORTS=$(grep -E "^export|module\.exports" "$file" 2>/dev/null | head -3)
        if [ -n "$EXPORTS" ]; then
            echo "  $file:"
            echo "$EXPORTS" | sed 's/^/    /'
        fi
    done
    echo ""
fi

# Show package dependencies if requested
if [ "$SHOW_DEPS" == "true" ]; then
    echo "## Project Dependencies"
    if [ -f "package.json" ]; then
        echo "  Node.js dependencies:"
        grep -A 50 '"dependencies"' package.json 2>/dev/null | grep -E '^\s+"[^"]+":' | head -10 | sed 's/^/    /'
    fi
    if [ -f "requirements.txt" ]; then
        echo "  Python dependencies:"
        head -10 requirements.txt | sed 's/^/    /'
    fi
    if [ -f "Cargo.toml" ]; then
        echo "  Rust dependencies:"
        grep -A 20 '\[dependencies\]' Cargo.toml 2>/dev/null | grep -v '^\[' | head -10 | sed 's/^/    /'
    fi
fi
