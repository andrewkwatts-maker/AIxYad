# Pipeline

The Pipeline screen shows the configured execution phases for sprint tasks.

**Phase types:**
- **Agent**: An AI agent runs a prompt and performs file operations
- **Command**: A shell command runs (e.g., `pytest`, `npm test`)
- **Builtin**: A built-in Automatica operation (e.g., lint, security scan)
- **Review**: A review gate that may block task completion

**Ordering:**
- Phases run in step_order sequence (lower = earlier)
- Disabled phases are skipped
- Failed phases can escalate to the next agent in the fallback chain

**Customization:**
- Add custom phases via the Pipeline editor
- Each project can override the global default pipeline
