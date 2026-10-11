#!/bin/bash
#====== AIxYad/scripts/end-task.sh ======
# Completes the current task, archives it, and optionally commits/pushes
# Cross-platform replacement for EndTask.bat
# Usage: ./end-task.sh [--no-commit] [--push]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"
LOG_FILE="$PROJECT_ROOT/logs/end-task.log"

# Read settings
if [ -f "$SETTINGS_FILE" ]; then
    CURRENT_TASK_FILE=$(grep -o '"current_task_file"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    COMPLETED_FILE=$(grep -o '"completed_file"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    VCS_ENABLED=$(grep -o '"enabled"[[:space:]]*:[[:space:]]*[^,}]*' "$SETTINGS_FILE" | head -1 | awk -F: '{print $2}' | tr -d ' ')
    PUSH_ON_COMPLETE=$(grep -o '"push_on_task_complete"[[:space:]]*:[[:space:]]*[^,}]*' "$SETTINGS_FILE" | awk -F: '{print $2}' | tr -d ' ')
fi

# Defaults
CURRENT_TASK_FILE="${CURRENT_TASK_FILE:-./CURRENT_TASK.md}"
COMPLETED_FILE="${COMPLETED_FILE:-./Completed.md}"
VCS_ENABLED="${VCS_ENABLED:-true}"
PUSH_ON_COMPLETE="${PUSH_ON_COMPLETE:-false}"

# Convert relative to absolute
if [[ "$CURRENT_TASK_FILE" == ./* ]]; then
    CURRENT_TASK_FILE="$PROJECT_ROOT/${CURRENT_TASK_FILE:2}"
fi
if [[ "$COMPLETED_FILE" == ./* ]]; then
    COMPLETED_FILE="$PROJECT_ROOT/${COMPLETED_FILE:2}"
fi

# Parse arguments
NO_COMMIT=false
FORCE_PUSH=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-commit)
            NO_COMMIT=true
            shift
            ;;
        --push)
            FORCE_PUSH=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--no-commit] [--push]"
            echo "  --no-commit    Don't commit changes"
            echo "  --push         Force push after commit"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Check if CURRENT_TASK.md exists
if [ ! -f "$CURRENT_TASK_FILE" ]; then
    echo "No current task found"
    echo "Run pop-task.sh first to get a task"
    exit 1
fi

# Read task content
TASK_CONTENT=$(cat "$CURRENT_TASK_FILE")

# Extract task title
TASK_TITLE=$(echo "$TASK_CONTENT" | sed -n '/^\[/d; /^---/d; /^$/d; p' | head -1)

echo "========================================"
echo "Completing Task"
echo "========================================"
echo "Title: $TASK_TITLE"
echo "========================================"

# Archive to Completed.md
{
    echo ""
    echo "## [$(date '+%Y-%m-%d %H:%M:%S')] Completed"
    echo ""
    echo "$TASK_CONTENT"
    echo ""
    echo "---"
} >> "$COMPLETED_FILE"

echo "Task archived to: $COMPLETED_FILE"

# Commit if VCS enabled and not disabled
if [ "$VCS_ENABLED" == "true" ] && [ "$NO_COMMIT" == "false" ]; then
    echo ""
    echo "Committing changes..."

    # Use commit-task.sh for actual commit
    PUSH_ARG=""
    if [ "$FORCE_PUSH" == "true" ] || [ "$PUSH_ON_COMPLETE" == "true" ]; then
        PUSH_ARG="--push"
    fi

    "$SCRIPT_DIR/commit-task.sh" $PUSH_ARG || echo "Commit skipped (no changes or error)"
fi

# Remove CURRENT_TASK.md
rm -f "$CURRENT_TASK_FILE"
echo ""
echo "Current task cleared"

# Clean up temp directory
TEMP_DIR="$PROJECT_ROOT/temp"
if [ -d "$TEMP_DIR" ]; then
    TEMP_FILES=$(find "$TEMP_DIR" -type f 2>/dev/null | wc -l)
    if [ "$TEMP_FILES" -gt 0 ]; then
        echo ""
        echo "Warning: $TEMP_FILES backup file(s) remaining in temp/"
        echo "Run: ./scripts/cleanup-temp.sh"
    fi
fi

# Log the operation
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Completed task: $TASK_TITLE" >> "$LOG_FILE"

# Check if this was the last task in an active sprint
SPRINT_FILE="$PROJECT_ROOT/CURRENT_SPRINT.json"
if [ -f "$SPRINT_FILE" ]; then
    REMAINING_TASKS=0
    if [ -f "$PROJECT_ROOT/TODO.md" ]; then
        REMAINING_TASKS=$(grep -c "^---TASK_START---" "$PROJECT_ROOT/TODO.md" 2>/dev/null || echo 0)
    fi
    if [ "$REMAINING_TASKS" -eq 0 ] && [ ! -f "$PROJECT_ROOT/CURRENT_TASK.md" ]; then
        SPRINT_NAME=$(grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$SPRINT_FILE" | cut -d'"' -f4)
        echo ""
        echo "========================================"
        echo "All sprint tasks complete!"
        echo "Sprint: $SPRINT_NAME"
        echo "========================================"
        echo "Run ./scripts/end-sprint.sh to finalize and send the report"
    fi
fi

echo ""
echo "========================================"
echo "Task Completed!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/pop-task.sh for next task"
echo "  2. Or ./scripts/list-tasks.sh to view backlog"
