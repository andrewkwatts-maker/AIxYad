#!/bin/bash
#====== Automatica/scripts/backup-file.sh ======
# Creates a backup of a file in the temp directory
# Usage: ./backup-file.sh <file_path>
# Returns: Path to backup file

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
if [ -z "$1" ]; then
    echo "Usage: $0 <file_path>"
    echo "Creates a backup of the specified file in $TEMP_DIR"
    exit 1
fi

SOURCE_FILE="$1"

# Check if file exists
if [ ! -f "$SOURCE_FILE" ]; then
    echo "Error: File not found: $SOURCE_FILE"
    exit 1
fi

# Get absolute path of source
SOURCE_ABS=$(realpath "$SOURCE_FILE")

# Create relative path structure in temp dir
REL_PATH="${SOURCE_ABS#$PROJECT_ROOT/}"
BACKUP_PATH="$TEMP_DIR/$REL_PATH"
BACKUP_DIR=$(dirname "$BACKUP_PATH")

# Check if backup already exists
if [ -f "$BACKUP_PATH" ]; then
    echo "Backup already exists: $BACKUP_PATH"
    echo "$BACKUP_PATH"
    exit 0
fi

# Create backup directory structure
mkdir -p "$BACKUP_DIR"

# Copy file
cp "$SOURCE_FILE" "$BACKUP_PATH"

echo "Backup created: $BACKUP_PATH"
echo "$BACKUP_PATH"

# Log the operation
LOG_FILE="$PROJECT_ROOT/logs/backup.log"
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backed up: $SOURCE_FILE -> $BACKUP_PATH" >> "$LOG_FILE"
