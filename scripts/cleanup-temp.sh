#!/bin/bash
#====== Automatica/scripts/cleanup-temp.sh ======
# Cleans up the temp backup directory
# Usage: ./cleanup-temp.sh [--force] [--older-than N] [--dry-run]

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
FORCE=false
OLDER_THAN=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --force|-f)
            FORCE=true
            shift
            ;;
        --older-than)
            OLDER_THAN="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--force] [--older-than N] [--dry-run]"
            echo "  --force       Don't ask for confirmation"
            echo "  --older-than  Only delete files older than N days"
            echo "  --dry-run     Show what would be deleted"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

if [ ! -d "$TEMP_DIR" ]; then
    echo "Temp directory does not exist: $TEMP_DIR"
    exit 0
fi

# Count files
if [ -n "$OLDER_THAN" ]; then
    FILE_COUNT=$(find "$TEMP_DIR" -type f -mtime +"$OLDER_THAN" 2>/dev/null | wc -l)
else
    FILE_COUNT=$(find "$TEMP_DIR" -type f 2>/dev/null | wc -l)
fi

if [ "$FILE_COUNT" -eq 0 ]; then
    echo "No files to clean up"
    exit 0
fi

echo "Found $FILE_COUNT backup file(s) in $TEMP_DIR"

if [ "$DRY_RUN" == "true" ]; then
    echo ""
    echo "Files that would be deleted:"
    if [ -n "$OLDER_THAN" ]; then
        find "$TEMP_DIR" -type f -mtime +"$OLDER_THAN" -print
    else
        find "$TEMP_DIR" -type f -print
    fi
    exit 0
fi

if [ "$FORCE" != "true" ]; then
    read -p "Delete all backup files? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled"
        exit 0
    fi
fi

# Delete files
if [ -n "$OLDER_THAN" ]; then
    find "$TEMP_DIR" -type f -mtime +"$OLDER_THAN" -delete
else
    find "$TEMP_DIR" -type f -delete
fi

# Remove empty directories
find "$TEMP_DIR" -type d -empty -delete 2>/dev/null || true

echo "Cleaned up $FILE_COUNT file(s)"

# Log the operation
LOG_FILE="$PROJECT_ROOT/logs/cleanup.log"
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleaned up $FILE_COUNT backup files" >> "$LOG_FILE"
