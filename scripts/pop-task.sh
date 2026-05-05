#!/bin/bash
#====== Automatica/scripts/pop-task.sh ======
# Extracts the next task from TODO.md into CURRENT_TASK.md
# Cross-platform replacement for PopTask.bat
# Usage: ./pop-task.sh [--peek]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"
LOG_FILE="$PROJECT_ROOT/logs/pop-task.log"

# Read paths from settings or use defaults
if [ -f "$SETTINGS_FILE" ]; then
    TODO_FILE=$(grep -o '"todo_file"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    CURRENT_TASK_FILE=$(grep -o '"current_task_file"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
fi

# Default paths
TODO_FILE="${TODO_FILE:-./TODO.md}"
CURRENT_TASK_FILE="${CURRENT_TASK_FILE:-./CURRENT_TASK.md}"

# Convert relative to absolute
if [[ "$TODO_FILE" == ./* ]]; then
    TODO_FILE="$PROJECT_ROOT/${TODO_FILE:2}"
fi
if [[ "$CURRENT_TASK_FILE" == ./* ]]; then
    CURRENT_TASK_FILE="$PROJECT_ROOT/${CURRENT_TASK_FILE:2}"
fi

# Parse arguments
PEEK_ONLY=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --peek)
            PEEK_ONLY=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--peek]"
            echo "  --peek    Show next task without extracting"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Check if CURRENT_TASK.md already exists
if [ -f "$CURRENT_TASK_FILE" ] && [ "$PEEK_ONLY" == "false" ]; then
    echo "Error: CURRENT_TASK.md already exists"
    echo "Complete the current task first with end-task.sh"
    echo ""
    echo "Current task:"
    cat "$CURRENT_TASK_FILE"
    exit 1
fi

# Check if TODO.md exists
if [ ! -f "$TODO_FILE" ]; then
    echo "No TODO.md found - nothing to do"
    exit 0
fi

# Find the first task block
TASK_START_LINE=$(grep -n "^---TASK_START---" "$TODO_FILE" | head -1 | cut -d: -f1)

if [ -z "$TASK_START_LINE" ]; then
    echo "No tasks in TODO.md - all done!"
    exit 0
fi

# Find the corresponding end
TASK_END_LINE=$(tail -n +"$TASK_START_LINE" "$TODO_FILE" | grep -n "^---TASK_END---" | head -1 | cut -d: -f1)

if [ -z "$TASK_END_LINE" ]; then
    echo "Error: Found TASK_START but no TASK_END"
    exit 1
fi

# Calculate actual end line number
TASK_END_LINE=$((TASK_START_LINE + TASK_END_LINE - 1))

# Extract task content (including delimiters)
TASK_CONTENT=$(sed -n "${TASK_START_LINE},${TASK_END_LINE}p" "$TODO_FILE")

# Show the task
echo "========================================"
echo "Next Task"
echo "========================================"
echo "$TASK_CONTENT"
echo "========================================"

if [ "$PEEK_ONLY" == "true" ]; then
    echo ""
    echo "(Peek mode - task not extracted)"
    exit 0
fi

# Write to CURRENT_TASK.md
echo "$TASK_CONTENT" > "$CURRENT_TASK_FILE"

# Remove task from TODO.md
# Create temp file without the extracted task
{
    head -n $((TASK_START_LINE - 1)) "$TODO_FILE" 2>/dev/null || true
    tail -n +$((TASK_END_LINE + 1)) "$TODO_FILE" 2>/dev/null || true
} > "${TODO_FILE}.tmp"

# Clean up empty lines at the extraction point
sed -i '/^$/N;/^\n$/d' "${TODO_FILE}.tmp" 2>/dev/null || \
    sed -e '/^$/N' -e '/^\n$/d' "${TODO_FILE}.tmp" > "${TODO_FILE}.tmp2" && mv "${TODO_FILE}.tmp2" "${TODO_FILE}.tmp"

mv "${TODO_FILE}.tmp" "$TODO_FILE"

echo ""
echo "Task extracted to: $CURRENT_TASK_FILE"
echo "TODO.md updated"

# Extract task title for logging
TASK_TITLE=$(echo "$TASK_CONTENT" | sed -n '/^\[/d; /^---/d; /^$/d; p' | head -1)

# Log the operation
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Popped task: $TASK_TITLE" >> "$LOG_FILE"
