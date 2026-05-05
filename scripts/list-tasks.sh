#!/bin/bash
#====== Automatica/scripts/list-tasks.sh ======
# Lists all tasks from TODO.md with summary info
# Usage: ./list-tasks.sh [--priority HIGH|MEDIUM|LOW] [--agent AgentName] [--brief]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TODO_FILE="$PROJECT_ROOT/TODO.md"

# Parse arguments
FILTER_PRIORITY=""
FILTER_AGENT=""
BRIEF=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --priority|-p)
            FILTER_PRIORITY="${2^^}"
            shift 2
            ;;
        --agent|-a)
            FILTER_AGENT="$2"
            shift 2
            ;;
        --brief|-b)
            BRIEF=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--priority HIGH|MEDIUM|LOW] [--agent AgentName] [--brief]"
            echo "  --priority   Filter by priority"
            echo "  --agent      Filter by assigned agent"
            echo "  --brief      Show only task titles"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

if [ ! -f "$TODO_FILE" ]; then
    echo "No TODO.md found"
    exit 0
fi

echo "========================================"
echo "Automatica Task Backlog"
echo "========================================"

# Count tasks
TOTAL=$(grep -c "^---TASK_START---" "$TODO_FILE" || echo 0)
HIGH=$(grep -c "\[PRIORITY: HIGH\]" "$TODO_FILE" || echo 0)
MEDIUM=$(grep -c "\[PRIORITY: MEDIUM\]" "$TODO_FILE" || echo 0)
LOW=$(grep -c "\[PRIORITY: LOW\]" "$TODO_FILE" || echo 0)

echo "Total: $TOTAL tasks (HIGH: $HIGH, MEDIUM: $MEDIUM, LOW: $LOW)"
echo "========================================"
echo ""

# Process each task
TASK_NUM=0
IN_TASK=false
CURRENT_TASK=""
CURRENT_PRIORITY=""
CURRENT_AGENT=""
CURRENT_TITLE=""

while IFS= read -r line; do
    if [[ "$line" == "---TASK_START---" ]]; then
        IN_TASK=true
        TASK_NUM=$((TASK_NUM + 1))
        CURRENT_TASK=""
        CURRENT_PRIORITY=""
        CURRENT_AGENT=""
        CURRENT_TITLE=""
        continue
    fi

    if [[ "$line" == "---TASK_END---" ]]; then
        IN_TASK=false

        # Apply filters
        SHOW_TASK=true
        if [ -n "$FILTER_PRIORITY" ] && [ "$CURRENT_PRIORITY" != "$FILTER_PRIORITY" ]; then
            SHOW_TASK=false
        fi
        if [ -n "$FILTER_AGENT" ] && [ "$CURRENT_AGENT" != "$FILTER_AGENT" ]; then
            SHOW_TASK=false
        fi

        if [ "$SHOW_TASK" == "true" ]; then
            if [ "$BRIEF" == "true" ]; then
                echo "[$TASK_NUM] [$CURRENT_PRIORITY] $CURRENT_TITLE"
            else
                echo "--- Task #$TASK_NUM ---"
                echo "Priority: $CURRENT_PRIORITY"
                echo "Agent: $CURRENT_AGENT"
                echo "Title: $CURRENT_TITLE"
                echo ""
            fi
        fi
        continue
    fi

    if [ "$IN_TASK" == "true" ]; then
        # Extract metadata
        if [[ "$line" =~ ^\[PRIORITY:[[:space:]]*([A-Z]+)\] ]]; then
            CURRENT_PRIORITY="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^\[AGENT:[[:space:]]*([^\]]+)\] ]]; then
            CURRENT_AGENT="${BASH_REMATCH[1]}"
        elif [ -z "$CURRENT_TITLE" ] && [[ ! "$line" =~ ^\[ ]] && [ -n "$line" ]; then
            CURRENT_TITLE="$line"
        fi
    fi
done < "$TODO_FILE"

if [ "$TASK_NUM" -eq 0 ]; then
    echo "No tasks in backlog"
fi
