#!/bin/bash
#====== Automatica/scripts/analyze-codebase.sh ======
# Analyzes codebase structure, size, and composition
# Usage: ./analyze-codebase.sh [--json] [--full]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
JSON_OUTPUT=false
FULL_REPORT=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --full)
            FULL_REPORT=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--json] [--full]"
            echo "  --json    Output in JSON format"
            echo "  --full    Include detailed file listing"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

cd "$PROJECT_ROOT"

# Count files by extension
count_by_ext() {
    find . -type f -name "*.$1" ! -path './node_modules/*' ! -path './.git/*' ! -path './vendor/*' ! -path './temp/*' 2>/dev/null | wc -l
}

# Count lines of code
count_loc() {
    find . -type f -name "*.$1" ! -path './node_modules/*' ! -path './.git/*' ! -path './vendor/*' ! -path './temp/*' -exec cat {} \; 2>/dev/null | wc -l
}

# Detect primary language
detect_language() {
    local max_count=0
    local primary=""

    for ext in js ts jsx tsx py rs go java cs cpp c rb php; do
        count=$(count_by_ext "$ext")
        if [ "$count" -gt "$max_count" ]; then
            max_count=$count
            primary=$ext
        fi
    done

    case $primary in
        js|jsx) echo "JavaScript" ;;
        ts|tsx) echo "TypeScript" ;;
        py) echo "Python" ;;
        rs) echo "Rust" ;;
        go) echo "Go" ;;
        java) echo "Java" ;;
        cs) echo "C#" ;;
        cpp|c) echo "C/C++" ;;
        rb) echo "Ruby" ;;
        php) echo "PHP" ;;
        *) echo "Unknown" ;;
    esac
}

# Gather stats
TOTAL_FILES=$(find . -type f ! -path './node_modules/*' ! -path './.git/*' ! -path './vendor/*' ! -path './temp/*' 2>/dev/null | wc -l)
TOTAL_DIRS=$(find . -type d ! -path './node_modules/*' ! -path './.git/*' ! -path './vendor/*' ! -path './temp/*' 2>/dev/null | wc -l)

# Language-specific counts
JS_FILES=$(count_by_ext "js")
TS_FILES=$(count_by_ext "ts")
PY_FILES=$(count_by_ext "py")
MD_FILES=$(count_by_ext "md")
JSON_FILES=$(count_by_ext "json")
SH_FILES=$(count_by_ext "sh")
BAT_FILES=$(count_by_ext "bat")

# LOC counts
JS_LOC=$(count_loc "js")
TS_LOC=$(count_loc "ts")
PY_LOC=$(count_loc "py")

PRIMARY_LANG=$(detect_language)

# Check for common project files
HAS_PACKAGE_JSON=$( [ -f "package.json" ] && echo "true" || echo "false" )
HAS_TSCONFIG=$( [ -f "tsconfig.json" ] && echo "true" || echo "false" )
HAS_CARGO=$( [ -f "Cargo.toml" ] && echo "true" || echo "false" )
HAS_GOMOD=$( [ -f "go.mod" ] && echo "true" || echo "false" )
HAS_PYPROJECT=$( [ -f "pyproject.toml" ] || [ -f "setup.py" ] && echo "true" || echo "false" )
HAS_GIT=$( [ -d ".git" ] && echo "true" || echo "false" )

if [ "$JSON_OUTPUT" == "true" ]; then
    cat <<EOF
{
  "project_root": "$PROJECT_ROOT",
  "primary_language": "$PRIMARY_LANG",
  "total_files": $TOTAL_FILES,
  "total_directories": $TOTAL_DIRS,
  "files_by_type": {
    "javascript": $JS_FILES,
    "typescript": $TS_FILES,
    "python": $PY_FILES,
    "markdown": $MD_FILES,
    "json": $JSON_FILES,
    "shell": $SH_FILES,
    "batch": $BAT_FILES
  },
  "lines_of_code": {
    "javascript": $JS_LOC,
    "typescript": $TS_LOC,
    "python": $PY_LOC
  },
  "project_files": {
    "package_json": $HAS_PACKAGE_JSON,
    "tsconfig": $HAS_TSCONFIG,
    "cargo_toml": $HAS_CARGO,
    "go_mod": $HAS_GOMOD,
    "python_config": $HAS_PYPROJECT,
    "git": $HAS_GIT
  }
}
EOF
else
    echo "========================================"
    echo "Codebase Analysis: $(basename "$PROJECT_ROOT")"
    echo "========================================"
    echo ""
    echo "Primary Language: $PRIMARY_LANG"
    echo ""
    echo "Overview:"
    echo "  Total Files:       $TOTAL_FILES"
    echo "  Total Directories: $TOTAL_DIRS"
    echo ""
    echo "Files by Type:"
    [ "$JS_FILES" -gt 0 ] && echo "  JavaScript:  $JS_FILES files ($JS_LOC lines)"
    [ "$TS_FILES" -gt 0 ] && echo "  TypeScript:  $TS_FILES files ($TS_LOC lines)"
    [ "$PY_FILES" -gt 0 ] && echo "  Python:      $PY_FILES files ($PY_LOC lines)"
    [ "$MD_FILES" -gt 0 ] && echo "  Markdown:    $MD_FILES files"
    [ "$JSON_FILES" -gt 0 ] && echo "  JSON:        $JSON_FILES files"
    [ "$SH_FILES" -gt 0 ] && echo "  Shell:       $SH_FILES files"
    [ "$BAT_FILES" -gt 0 ] && echo "  Batch:       $BAT_FILES files"
    echo ""
    echo "Project Configuration:"
    [ "$HAS_PACKAGE_JSON" == "true" ] && echo "  ✓ package.json (Node.js)"
    [ "$HAS_TSCONFIG" == "true" ] && echo "  ✓ tsconfig.json (TypeScript)"
    [ "$HAS_CARGO" == "true" ] && echo "  ✓ Cargo.toml (Rust)"
    [ "$HAS_GOMOD" == "true" ] && echo "  ✓ go.mod (Go)"
    [ "$HAS_PYPROJECT" == "true" ] && echo "  ✓ Python config"
    [ "$HAS_GIT" == "true" ] && echo "  ✓ Git repository"
    echo ""

    if [ "$FULL_REPORT" == "true" ]; then
        echo "Directory Structure:"
        find . -type d ! -path './node_modules/*' ! -path './.git/*' ! -path './vendor/*' -maxdepth 3 2>/dev/null | head -30
        echo ""
    fi
fi
