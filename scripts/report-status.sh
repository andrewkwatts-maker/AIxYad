#!/bin/bash
#====== AIxYad/scripts/report-status.sh ======
# Generates a status report of current project state
# Usage: ./report-status.sh [--json] [--brief]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TODO_FILE="$PROJECT_ROOT/TODO.md"
CURRENT_TASK_FILE="$PROJECT_ROOT/CURRENT_TASK.md"
COMPLETED_FILE="$PROJECT_ROOT/Completed.md"

# Parse arguments
JSON_OUTPUT=false
BRIEF=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --brief)
            BRIEF=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--json] [--brief]"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Count tasks in backlog
TOTAL_BACKLOG=0
HIGH_PRIORITY=0
MEDIUM_PRIORITY=0
LOW_PRIORITY=0

if [ -f "$TODO_FILE" ]; then
    TOTAL_BACKLOG=$(grep -c "^---TASK_START---" "$TODO_FILE" 2>/dev/null || echo 0)
    HIGH_PRIORITY=$(grep -c "\[PRIORITY: HIGH\]" "$TODO_FILE" 2>/dev/null || echo 0)
    MEDIUM_PRIORITY=$(grep -c "\[PRIORITY: MEDIUM\]" "$TODO_FILE" 2>/dev/null || echo 0)
    LOW_PRIORITY=$(grep -c "\[PRIORITY: LOW\]" "$TODO_FILE" 2>/dev/null || echo 0)
fi

# Check current task
CURRENT_TASK=""
CURRENT_AGENT=""
if [ -f "$CURRENT_TASK_FILE" ]; then
    CURRENT_TASK=$(sed -n '/^\[/d; /^---/d; /^$/d; p' "$CURRENT_TASK_FILE" | head -1)
    CURRENT_AGENT=$(grep -o '\[AGENT:[^]]*\]' "$CURRENT_TASK_FILE" 2>/dev/null | cut -d: -f2 | tr -d ']' | xargs || echo "Unknown")
fi

# Count completed today
COMPLETED_TODAY=0
if [ -f "$COMPLETED_FILE" ]; then
    TODAY=$(date '+%Y-%m-%d')
    COMPLETED_TODAY=$(grep -c "\[$TODAY" "$COMPLETED_FILE" 2>/dev/null || echo 0)
fi

# Get git status
GIT_BRANCH=""
UNCOMMITTED=0
if [ -d "$PROJECT_ROOT/.git" ]; then
    GIT_BRANCH=$(git -C "$PROJECT_ROOT" branch --show-current 2>/dev/null || echo "")
    UNCOMMITTED=$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null | wc -l || echo 0)
fi

if [ "$JSON_OUTPUT" == "true" ]; then
    cat <<EOF
{
  "timestamp": "$(date -Iseconds)",
  "current_task": {
    "title": "$CURRENT_TASK",
    "agent": "$CURRENT_AGENT",
    "active": $([ -n "$CURRENT_TASK" ] && echo "true" || echo "false")
  },
  "backlog": {
    "total": $TOTAL_BACKLOG,
    "high": $HIGH_PRIORITY,
    "medium": $MEDIUM_PRIORITY,
    "low": $LOW_PRIORITY
  },
  "completed_today": $COMPLETED_TODAY,
  "git": {
    "branch": "$GIT_BRANCH",
    "uncommitted_changes": $UNCOMMITTED
  }
}
EOF
    exit 0
fi

if [ "$BRIEF" == "true" ]; then
    echo "Current: ${CURRENT_TASK:-None} | Backlog: $TOTAL_BACKLOG (H:$HIGH_PRIORITY M:$MEDIUM_PRIORITY L:$LOW_PRIORITY) | Completed Today: $COMPLETED_TODAY"
    exit 0
fi

echo "========================================"
echo "Project Status Report"
echo "========================================"
echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

echo "## Current Task"
if [ -n "$CURRENT_TASK" ]; then
    echo "  Title: $CURRENT_TASK"
    echo "  Agent: $CURRENT_AGENT"
else
    echo "  No active task"
    echo "  Run: ./scripts/pop-task.sh"
fi
echo ""

echo "## Backlog Summary"
echo "  Total Tasks:    $TOTAL_BACKLOG"
echo "  ├─ HIGH:        $HIGH_PRIORITY"
echo "  ├─ MEDIUM:      $MEDIUM_PRIORITY"
echo "  └─ LOW:         $LOW_PRIORITY"
echo ""

echo "## Progress"
echo "  Completed Today: $COMPLETED_TODAY"
echo ""

if [ -n "$GIT_BRANCH" ]; then
    echo "## Git Status"
    echo "  Branch: $GIT_BRANCH"
    if [ "$UNCOMMITTED" -gt 0 ]; then
        echo "  Uncommitted: $UNCOMMITTED file(s)"
    else
        echo "  Working tree clean"
    fi
    echo ""
fi

# Show next task preview
if [ "$TOTAL_BACKLOG" -gt 0 ]; then
    echo "## Next Task Preview"
    NEXT_TASK=$(sed -n '/---TASK_START---/,/---TASK_END---/{
        /^\[/d
        /^---/d
        /^$/d
        p
    }' "$TODO_FILE" | head -1)
    NEXT_PRIORITY=$(grep -o '\[PRIORITY:[^]]*\]' "$TODO_FILE" | head -1 | cut -d: -f2 | tr -d ']' | xargs)
    echo "  $NEXT_TASK"
    echo "  Priority: $NEXT_PRIORITY"
fi
