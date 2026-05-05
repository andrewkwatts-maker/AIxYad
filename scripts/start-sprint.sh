#!/bin/bash
#====== Automatica/scripts/start-sprint.sh ======
# Starts a new sprint with tracking metadata
# Usage: ./start-sprint.sh -n "Sprint Name" [-g "Sprint Goal"] [-d 14]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SPRINT_FILE="$PROJECT_ROOT/CURRENT_SPRINT.json"
LOG_FILE="$PROJECT_ROOT/logs/sprint.log"

# Defaults
SPRINT_NAME=""
SPRINT_GOAL=""
DURATION_DAYS=14

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--name)
            SPRINT_NAME="$2"
            shift 2
            ;;
        -g|--goal)
            SPRINT_GOAL="$2"
            shift 2
            ;;
        -d|--duration)
            DURATION_DAYS="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 -n \"Sprint Name\" [-g \"Sprint Goal\"] [-d duration_days]"
            echo "  -n, --name      Sprint name (required)"
            echo "  -g, --goal      Sprint goal description"
            echo "  -d, --duration  Sprint duration in days (default: 14)"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

if [ -z "$SPRINT_NAME" ]; then
    echo "ERROR: Sprint name required"
    echo "Usage: $0 -n \"Sprint Name\""
    exit 1
fi

# Check for existing sprint
if [ -f "$SPRINT_FILE" ]; then
    EXISTING=$(grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$SPRINT_FILE" | cut -d'"' -f4)
    echo "WARNING: Sprint already active: $EXISTING"
    echo "Run ./scripts/end-sprint.sh first to complete it"
    exit 1
fi

# Determine sprint number from log
SPRINT_NUMBER=1
if [ -f "$LOG_FILE" ]; then
    LAST_NUM=$(grep -o 'Sprint #[0-9]*' "$LOG_FILE" | tail -1 | grep -o '[0-9]*')
    if [ -n "$LAST_NUM" ]; then
        SPRINT_NUMBER=$((LAST_NUM + 1))
    fi
fi

START_DATE=$(date '+%Y-%m-%d')

# Create sprint file
cat > "$SPRINT_FILE" <<EOF
{
  "number": $SPRINT_NUMBER,
  "name": "$SPRINT_NAME",
  "goal": "$SPRINT_GOAL",
  "start_date": "$START_DATE",
  "duration_days": $DURATION_DAYS,
  "end_date": "",
  "status": "active",
  "started_by": "$(git config user.name 2>/dev/null || echo 'unknown')"
}
EOF

# Log it
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Started Sprint #$SPRINT_NUMBER: $SPRINT_NAME (goal: $SPRINT_GOAL)" >> "$LOG_FILE"

echo "========================================"
echo "Sprint Started"
echo "========================================"
echo "  Number:   #$SPRINT_NUMBER"
echo "  Name:     $SPRINT_NAME"
echo "  Goal:     ${SPRINT_GOAL:-None set}"
echo "  Start:    $START_DATE"
echo "  Duration: $DURATION_DAYS days"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/pop-task.sh to start working"
echo "  2. Run ./scripts/end-sprint.sh when complete"
