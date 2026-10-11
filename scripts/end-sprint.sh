#!/bin/bash
#====== AIxYad/scripts/end-sprint.sh ======
# Completes the current sprint, generates report, and sends email
# Usage: ./end-sprint.sh [--no-email] [--output path]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SPRINT_FILE="$PROJECT_ROOT/CURRENT_SPRINT.json"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"
LOG_FILE="$PROJECT_ROOT/logs/sprint.log"
ARCHIVE_DIR="$PROJECT_ROOT/logs/sprints"

# Parse arguments
NO_EMAIL=false
OUTPUT_PATH=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-email)
            NO_EMAIL=true
            shift
            ;;
        --output)
            OUTPUT_PATH="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--no-email] [--output path]"
            echo "  --no-email   Skip sending email report"
            echo "  --output     Custom output path for HTML report"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Check sprint exists
if [ ! -f "$SPRINT_FILE" ]; then
    echo "ERROR: No active sprint found"
    echo "Run ./scripts/start-sprint.sh first"
    exit 1
fi

# Read sprint info
SPRINT_NAME=$(grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$SPRINT_FILE" | cut -d'"' -f4)
SPRINT_NUMBER=$(grep -o '"number"[[:space:]]*:[[:space:]]*[0-9]*' "$SPRINT_FILE" | grep -o '[0-9]*')
START_DATE=$(grep -o '"start_date"[[:space:]]*:[[:space:]]*"[^"]*"' "$SPRINT_FILE" | cut -d'"' -f4)

END_DATE=$(date '+%Y-%m-%d %H:%M:%S')

echo "========================================"
echo "Completing Sprint #$SPRINT_NUMBER"
echo "========================================"
echo "  Name:  $SPRINT_NAME"
echo "  Start: $START_DATE"
echo "  End:   $END_DATE"
echo "========================================"

# Update sprint file with end date
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "ERROR: Python required for report generation"
    exit 1
fi

$PYTHON_CMD -c "
import json
with open('$SPRINT_FILE') as f:
    s = json.load(f)
s['end_date'] = '$END_DATE'
s['status'] = 'completed'
with open('$SPRINT_FILE', 'w') as f:
    json.dump(s, f, indent=2)
"

echo ""
echo "Generating sprint report..."

# Build report command
REPORT_CMD="$PYTHON_CMD $SCRIPT_DIR/generate-sprint-report.py"

if [ -n "$OUTPUT_PATH" ]; then
    REPORT_CMD="$REPORT_CMD --output $OUTPUT_PATH"
fi

# Check if email is configured and requested
EMAIL_ENABLED=false
if [ "$NO_EMAIL" == "false" ]; then
    # Check settings for email config
    if [ -f "$SETTINGS_FILE" ]; then
        SMTP_HOST=$(grep -o '"smtp_host"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
        if [ -n "$SMTP_HOST" ] && [ "$SMTP_HOST" != "" ]; then
            EMAIL_ENABLED=true
            REPORT_CMD="$REPORT_CMD --send"
        fi
    fi
fi

# Run report generator
$REPORT_CMD

# Archive sprint file
mkdir -p "$ARCHIVE_DIR"
cp "$SPRINT_FILE" "$ARCHIVE_DIR/sprint_${SPRINT_NUMBER}.json"
rm -f "$SPRINT_FILE"

# Log completion
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Completed Sprint #$SPRINT_NUMBER: $SPRINT_NAME ($START_DATE -> $END_DATE)" >> "$LOG_FILE"

echo ""
echo "========================================"
echo "Sprint #$SPRINT_NUMBER Complete!"
echo "========================================"
echo ""
echo "  Report: logs/sprint_${SPRINT_NUMBER}_report.html"
echo "  Archive: logs/sprints/sprint_${SPRINT_NUMBER}.json"

if [ "$EMAIL_ENABLED" == "true" ]; then
    echo "  Email: Sent"
else
    if [ "$NO_EMAIL" == "true" ]; then
        echo "  Email: Skipped (--no-email)"
    else
        echo "  Email: Not configured (add email section to settings.json)"
    fi
fi

echo ""
echo "Next steps:"
echo "  1. Run ./scripts/start-sprint.sh -n \"Sprint Name\" for next sprint"
echo "  2. Or ./scripts/report-status.sh to review backlog"
