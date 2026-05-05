# Agents

Agents are AI workers that autonomously complete tasks.

**Agent roles:**
- **Developer**: Writes and edits code
- **Tester**: Creates and runs tests
- **Reviewer**: Reviews code quality and standards
- **Scrum Master**: Manages sprint backlog

**Configuration:**
- Each agent maps to an AI provider (Ollama, Claude, GPT, etc.)
- Complexity routing directs tasks to the right agent by story points
- Fallback chains ensure tasks complete even if the primary agent fails

**Health dashboard:**
- Green dot = agent healthy and available
- Red dot = agent offline or rate-limited
