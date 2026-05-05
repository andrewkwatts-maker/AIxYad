#!/bin/bash
#====== Automatica/scripts/assign-agent.sh ======
# Analyzes a task and recommends the appropriate agent
# Usage: ./assign-agent.sh ["task description" | --current]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CURRENT_TASK_FILE="$PROJECT_ROOT/CURRENT_TASK.md"

# Parse arguments
TASK_DESC=""
if [ "$1" == "--current" ] && [ -f "$CURRENT_TASK_FILE" ]; then
    TASK_DESC=$(cat "$CURRENT_TASK_FILE")
elif [ -n "$1" ]; then
    TASK_DESC="$1"
else
    echo "Usage: $0 [\"task description\" | --current]"
    echo "  --current    Analyze CURRENT_TASK.md"
    exit 1
fi

# Convert to lowercase for matching
TASK_LOWER=$(echo "$TASK_DESC" | tr '[:upper:]' '[:lower:]')

# Determine agent based on keywords
determine_agent() {
    # Check for explicit agent assignment first
    if echo "$TASK_DESC" | grep -q "\[AGENT:"; then
        ASSIGNED=$(echo "$TASK_DESC" | grep -o '\[AGENT:[^]]*\]' | cut -d: -f2 | tr -d ']' | xargs)
        if [ -n "$ASSIGNED" ]; then
            echo "$ASSIGNED"
            return
        fi
    fi

    # Priority keywords (check these first)
    if echo "$TASK_LOWER" | grep -qE "security|vulnerab|cve|owasp|injection|xss|csrf|auth.*breach"; then
        echo "SecurityAuditor"
        return
    fi

    # ProductOwner keywords
    if echo "$TASK_LOWER" | grep -qE "priorit|backlog|requirement|acceptance|criteria|stakeholder|user story|value"; then
        echo "ProductOwner"
        return
    fi

    # ScrumMaster keywords
    if echo "$TASK_LOWER" | grep -qE "break.*down|decompos|sprint|coordinate|blocker|impediment|refine"; then
        echo "ScrumMaster"
        return
    fi

    # Architect keywords
    if echo "$TASK_LOWER" | grep -qE "design|architect|plan|structure|pattern|system|interface|api.*design|schema"; then
        echo "Architect"
        return
    fi

    # Tester keywords
    if echo "$TASK_LOWER" | grep -qE "test|coverage|qa|quality|regression|unit test|integration test|e2e"; then
        echo "Tester"
        return
    fi

    # CodeReviewer keywords
    if echo "$TASK_LOWER" | grep -qE "review|code review|pr review|merge|approve|feedback"; then
        echo "CodeReviewer"
        return
    fi

    # Documentarian keywords
    if echo "$TASK_LOWER" | grep -qE "document|readme|comment|jsdoc|api doc|changelog|docs"; then
        echo "Documentarian"
        return
    fi

    # DevOps keywords
    if echo "$TASK_LOWER" | grep -qE "deploy|ci/cd|pipeline|docker|kubernetes|infrastructure|build|release|monitor"; then
        echo "DevOps"
        return
    fi

    # Researcher keywords
    if echo "$TASK_LOWER" | grep -qE "research|investigate|compare|evaluate|find.*library|alternative|option"; then
        echo "Researcher"
        return
    fi

    # Default to Developer for implementation tasks
    if echo "$TASK_LOWER" | grep -qE "implement|create|add|fix|bug|feature|code|function|class|module|component"; then
        echo "Developer"
        return
    fi

    # If unclear, suggest Orchestrator to route
    echo "Orchestrator"
}

AGENT=$(determine_agent)

# Output result
echo "========================================"
echo "Task Analysis"
echo "========================================"
echo ""
echo "Task Preview:"
echo "$TASK_DESC" | head -5
if [ $(echo "$TASK_DESC" | wc -l) -gt 5 ]; then
    echo "..."
fi
echo ""
echo "========================================"
echo "Recommended Agent: $AGENT"
echo "========================================"
echo ""

# Provide reasoning
case $AGENT in
    ProductOwner)
        echo "Reason: Task involves requirements, prioritization, or stakeholder concerns"
        ;;
    ScrumMaster)
        echo "Reason: Task involves coordination, breakdown, or process management"
        ;;
    Architect)
        echo "Reason: Task involves design, planning, or system structure"
        ;;
    Developer)
        echo "Reason: Task involves code implementation or bug fixes"
        ;;
    Tester)
        echo "Reason: Task involves testing, coverage, or quality assurance"
        ;;
    CodeReviewer)
        echo "Reason: Task involves code review or approval"
        ;;
    Documentarian)
        echo "Reason: Task involves documentation or comments"
        ;;
    DevOps)
        echo "Reason: Task involves deployment, CI/CD, or infrastructure"
        ;;
    SecurityAuditor)
        echo "Reason: Task involves security concerns or vulnerability"
        ;;
    Researcher)
        echo "Reason: Task requires investigation or comparison"
        ;;
    Orchestrator)
        echo "Reason: Task is unclear or requires coordination"
        ;;
esac

echo ""
echo "To invoke: /agent-$(echo "$AGENT" | tr '[:upper:]' '[:lower:]' | sed 's/auditor/-auditor/')"
