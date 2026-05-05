#!/bin/bash
#====== Automatica/scripts/commit-task.sh ======
# Commits changes using the current task as commit message
# Reads settings from settings.json
# Usage: ./commit-task.sh [--push] [--message "optional override"]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SETTINGS_FILE="$PROJECT_ROOT/settings.json"
CURRENT_TASK_FILE="$PROJECT_ROOT/CURRENT_TASK.md"
LOG_FILE="$PROJECT_ROOT/logs/commit-task.log"

# Parse arguments
PUSH_AFTER=false
CUSTOM_MESSAGE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --push)
            PUSH_AFTER=true
            shift
            ;;
        --message|-m)
            CUSTOM_MESSAGE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--push] [--message \"custom message\"]"
            echo "  --push       Push after commit"
            echo "  --message    Override commit message"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Read settings
if [ -f "$SETTINGS_FILE" ]; then
    VCS_TYPE=$(grep -o '"type"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | head -1 | cut -d'"' -f4)
    VCS_ENABLED=$(grep -o '"enabled"[[:space:]]*:[[:space:]]*[^,}]*' "$SETTINGS_FILE" | head -1 | awk -F: '{print $2}' | tr -d ' ')
    USE_TASK_MSG=$(grep -o '"use_task_as_commit_message"[[:space:]]*:[[:space:]]*[^,}]*' "$SETTINGS_FILE" | awk -F: '{print $2}' | tr -d ' ')
    AUTO_PUSH=$(grep -o '"push_on_task_complete"[[:space:]]*:[[:space:]]*[^,}]*' "$SETTINGS_FILE" | awk -F: '{print $2}' | tr -d ' ')
    REMOTE=$(grep -o '"remote"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
    BRANCH=$(grep -o '"branch"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
else
    echo "Warning: settings.json not found, using defaults"
    VCS_TYPE="git"
    VCS_ENABLED="true"
    USE_TASK_MSG="true"
    AUTO_PUSH="false"
    REMOTE="origin"
    BRANCH="main"
fi

# Check if VCS is enabled
if [ "$VCS_ENABLED" != "true" ]; then
    echo "VCS is disabled in settings. Skipping commit."
    exit 0
fi

# Determine commit message
if [ -n "$CUSTOM_MESSAGE" ]; then
    COMMIT_MSG="$CUSTOM_MESSAGE"
elif [ "$USE_TASK_MSG" == "true" ] && [ -f "$CURRENT_TASK_FILE" ]; then
    # Extract task title (first non-metadata line after TASK_START)
    COMMIT_MSG=$(sed -n '/---TASK_START---/,/---TASK_END---/{
        /^\[/d
        /^---/d
        /^$/d
        p
    }' "$CURRENT_TASK_FILE" | head -1)

    if [ -z "$COMMIT_MSG" ]; then
        COMMIT_MSG="Complete task from CURRENT_TASK.md"
    fi
else
    COMMIT_MSG="Update from Automatica"
fi

# Log operation start
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting commit: $COMMIT_MSG" >> "$LOG_FILE"

# Perform commit based on VCS type
case $VCS_TYPE in
    git)
        # Check for changes
        if git diff --quiet && git diff --cached --quiet; then
            echo "No changes to commit"
            exit 0
        fi

        # Stage all changes
        git add -A

        # Commit
        git commit -m "$COMMIT_MSG"
        echo "Committed: $COMMIT_MSG"

        # Push if requested or auto-push enabled
        if [ "$PUSH_AFTER" == "true" ] || [ "$AUTO_PUSH" == "true" ]; then
            CURRENT_BRANCH=$(git branch --show-current)
            echo "Pushing to $REMOTE/$CURRENT_BRANCH..."
            git push -u "$REMOTE" "$CURRENT_BRANCH"
            echo "Pushed successfully"
        fi
        ;;
    svn)
        SVN_EXE=$(grep -o '"executable"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS_FILE" | cut -d'"' -f4)
        if [ -z "$SVN_EXE" ] || [ ! -f "$SVN_EXE" ]; then
            SVN_EXE="svn"
        fi

        # Add new files
        "$SVN_EXE" status | grep '^?' | awk '{print $2}' | xargs -r "$SVN_EXE" add

        # Remove deleted files
        "$SVN_EXE" status | grep '^!' | awk '{print $2}' | xargs -r "$SVN_EXE" delete

        # Commit
        "$SVN_EXE" commit -m "$COMMIT_MSG"
        echo "Committed: $COMMIT_MSG"
        ;;
    *)
        echo "Error: Unknown VCS type: $VCS_TYPE"
        exit 1
        ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Commit completed: $COMMIT_MSG" >> "$LOG_FILE"
