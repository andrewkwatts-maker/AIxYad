#!/bin/bash
#====== AIxYad/scripts/diff-file.sh ======
# Diffs a file against its backup in temp directory
# Usage: ./diff-file.sh <file_path> [--brief] [--cleanup]
# Options:
#   --brief    Only show if files differ
#   --cleanup  Delete backup after diff

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"

# Get temp directory from settings or use default
if [ -f "$SETTINGS_FILE" ]; then
    TEMP_DIR=$(grep -o '"temp_backup"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
fi
TEMP_DIR="${TEMP_DIR:-./temp}"

# Convert relative to absolute
if [[ "$TEMP_DIR" == ./* ]]; then
    TEMP_DIR="$PROJECT_ROOT/${TEMP_DIR:2}"
fi

# Parse arguments
BRIEF=false
CLEANUP=false
SOURCE_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --brief)
            BRIEF=true
            shift
            ;;
        --cleanup)
            CLEANUP=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <file_path> [--brief] [--cleanup]"
            echo "  --brief    Only show if files differ"
            echo "  --cleanup  Delete backup after diff"
            exit 0
            ;;
        *)
            SOURCE_FILE="$1"
            shift
            ;;
    esac
done

if [ -z "$SOURCE_FILE" ]; then
    echo "Usage: $0 <file_path> [--brief] [--cleanup]"
    exit 1
fi

# Check if file exists
if [ ! -f "$SOURCE_FILE" ]; then
    echo "Error: File not found: $SOURCE_FILE"
    exit 1
fi

# Get absolute path
SOURCE_ABS=$(realpath "$SOURCE_FILE")

# Find backup
REL_PATH="${SOURCE_ABS#$PROJECT_ROOT/}"
BACKUP_PATH="$TEMP_DIR/$REL_PATH"

if [ ! -f "$BACKUP_PATH" ]; then
    echo "Error: No backup found at: $BACKUP_PATH"
    echo "Run backup-file.sh first"
    exit 1
fi

# Perform diff
echo "=== Diff: $SOURCE_FILE vs backup ==="
echo "Current:  $SOURCE_ABS"
echo "Backup:   $BACKUP_PATH"
echo "==========================================="

if [ "$BRIEF" == "true" ]; then
    if diff -q "$BACKUP_PATH" "$SOURCE_FILE" > /dev/null 2>&1; then
        echo "Files are identical"
        DIFF_RESULT=0
    else
        echo "Files differ"
        DIFF_RESULT=1
    fi
else
    # Show full diff with color if available
    if command -v colordiff &> /dev/null; then
        colordiff -u "$BACKUP_PATH" "$SOURCE_FILE" || true
    else
        diff -u "$BACKUP_PATH" "$SOURCE_FILE" || true
    fi
    DIFF_RESULT=$?
fi

# Cleanup if requested
if [ "$CLEANUP" == "true" ]; then
    rm -f "$BACKUP_PATH"
    echo ""
    echo "Backup deleted: $BACKUP_PATH"

    # Remove empty parent directories
    rmdir --ignore-fail-on-non-empty -p "$(dirname "$BACKUP_PATH")" 2>/dev/null || true
fi

exit $DIFF_RESULT
