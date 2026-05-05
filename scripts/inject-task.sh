#!/bin/bash
#====== Automatica/scripts/inject-task.sh ======
# Injects a new task into TODO.md in standardized format
# Usage: ./inject-task.sh [options]
#   -t, --title      Task title (required)
#   -d, --desc       Task description
#   -p, --priority   Priority: HIGH, MEDIUM, LOW (default: MEDIUM)
#   -a, --agent      Recommended agent (default: Developer)
#   -v, --value      Business value description
#   -c, --criteria   Acceptance criteria (comma-separated)
#   --depends        Dependencies (comma-separated task refs)
#   --parallel       Parallel group name
#   --position       Insert position: top, bottom, after:N (default: bottom)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TODO_FILE="$PROJECT_ROOT/TODO.md"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"

# Default values
PRIORITY="MEDIUM"
AGENT="Developer"
POSITION="bottom"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -t|--title)
            TITLE="$2"
            shift 2
            ;;
        -d|--desc)
            DESC="$2"
            shift 2
            ;;
        -p|--priority)
            PRIORITY="${2^^}"  # Uppercase
            shift 2
            ;;
        -a|--agent)
            AGENT="$2"
            shift 2
            ;;
        -v|--value)
            VALUE="$2"
            shift 2
            ;;
        -c|--criteria)
            CRITERIA="$2"
            shift 2
            ;;
        --depends)
            DEPENDS="$2"
            shift 2
            ;;
        --parallel)
            PARALLEL="$2"
            shift 2
            ;;
        --position)
            POSITION="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo "  -t, --title      Task title (required)"
            echo "  -d, --desc       Task description"
            echo "  -p, --priority   Priority: HIGH, MEDIUM, LOW (default: MEDIUM)"
            echo "  -a, --agent      Recommended agent (default: Developer)"
            echo "  -v, --value      Business value description"
            echo "  -c, --criteria   Acceptance criteria (comma-separated)"
            echo "  --depends        Dependencies (comma-separated)"
            echo "  --parallel       Parallel group name"
            echo "  --position       Insert position: top, bottom (default: bottom)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required fields
if [ -z "$TITLE" ]; then
    echo "Error: Task title is required (-t or --title)"
    exit 1
fi

# Validate priority
if [[ ! "$PRIORITY" =~ ^(HIGH|MEDIUM|LOW)$ ]]; then
    echo "Error: Priority must be HIGH, MEDIUM, or LOW"
    exit 1
fi

# Build task block
TASK_BLOCK="---TASK_START---
[PRIORITY: $PRIORITY]
[AGENT: $AGENT]"

if [ -n "$VALUE" ]; then
    TASK_BLOCK="$TASK_BLOCK
[VALUE: $VALUE]"
fi

if [ -n "$DEPENDS" ]; then
    TASK_BLOCK="$TASK_BLOCK
[DEPENDS: $DEPENDS]"
fi

if [ -n "$PARALLEL" ]; then
    TASK_BLOCK="$TASK_BLOCK
[PARALLEL_GROUP: $PARALLEL]"
fi

TASK_BLOCK="$TASK_BLOCK

$TITLE"

if [ -n "$DESC" ]; then
    TASK_BLOCK="$TASK_BLOCK

$DESC"
fi

# Add acceptance criteria
if [ -n "$CRITERIA" ]; then
    TASK_BLOCK="$TASK_BLOCK

Acceptance Criteria:"
    IFS=',' read -ra CRITERIA_ARRAY <<< "$CRITERIA"
    for criterion in "${CRITERIA_ARRAY[@]}"; do
        # Trim whitespace
        criterion=$(echo "$criterion" | xargs)
        TASK_BLOCK="$TASK_BLOCK
- [ ] $criterion"
    done
fi

TASK_BLOCK="$TASK_BLOCK
---TASK_END---"

# Create TODO.md if it doesn't exist
if [ ! -f "$TODO_FILE" ]; then
    echo "# Automatica Task Backlog" > "$TODO_FILE"
    echo "" >> "$TODO_FILE"
fi

# Insert task at specified position
case $POSITION in
    top)
        # Insert after header
        if grep -q "^# " "$TODO_FILE"; then
            # Find first task or end of file
            HEADER_END=$(grep -n "^---TASK_START---" "$TODO_FILE" | head -1 | cut -d: -f1)
            if [ -z "$HEADER_END" ]; then
                # No tasks yet, append
                echo "" >> "$TODO_FILE"
                echo "$TASK_BLOCK" >> "$TODO_FILE"
            else
                # Insert before first task
                head -n $((HEADER_END - 1)) "$TODO_FILE" > "${TODO_FILE}.tmp"
                echo "" >> "${TODO_FILE}.tmp"
                echo "$TASK_BLOCK" >> "${TODO_FILE}.tmp"
                echo "" >> "${TODO_FILE}.tmp"
                tail -n +$HEADER_END "$TODO_FILE" >> "${TODO_FILE}.tmp"
                mv "${TODO_FILE}.tmp" "$TODO_FILE"
            fi
        else
            echo "$TASK_BLOCK" > "${TODO_FILE}.tmp"
            echo "" >> "${TODO_FILE}.tmp"
            cat "$TODO_FILE" >> "${TODO_FILE}.tmp"
            mv "${TODO_FILE}.tmp" "$TODO_FILE"
        fi
        ;;
    bottom|*)
        echo "" >> "$TODO_FILE"
        echo "$TASK_BLOCK" >> "$TODO_FILE"
        ;;
esac

echo "Task injected successfully: $TITLE"
echo "Priority: $PRIORITY | Agent: $AGENT | Position: $POSITION"

# Log the operation
LOG_FILE="$PROJECT_ROOT/logs/inject-task.log"
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Injected task: $TITLE (Priority: $PRIORITY, Agent: $AGENT)" >> "$LOG_FILE"
