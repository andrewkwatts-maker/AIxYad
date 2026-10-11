# AIxYad Agent Roles

This document describes every agent role shipped with AIxYad. Agent definitions
live in `agents/<slug>.md` as plain markdown — edit or replace them without changing
any code.

---

## Role Reference

| Role | Slug | Complexity Range | Trigger Condition | Primary Actions | Output |
|------|------|-----------------|-------------------|-----------------|--------|
| **Orchestrator** | `orchestrator` | any | Backlog empty; cross-cutting decisions; workflow routing | Route tasks, coordinate handoffs, enforce quality gates | Routing decisions, status updates |
| **Product Owner** | `product-owner` | 1–3 | Unclear scope; missing acceptance criteria; backlog reprioritisation | Write/refine user stories, define acceptance criteria | Prioritised TODO.md entries |
| **Scrum Master** | `scrum-master` | 1–3 | Task too large (>1 day); parallel opportunities exist | Decompose tasks, identify dependencies, inject sub-tasks | Atomic task list |
| **Architect** | `architect` | 5–13 | Architectural decision needed; system design spike | Design components, define API contracts, produce ADRs | Architecture diagrams, interface specs |
| **Developer** | `developer` | 1–8 | Implementation ready; bug fix; code change | Write/edit source files, unit tests, self-review | Production code + tests |
| **Tester** | `tester` | 1–5 | Test coverage gap; regression found; post-implementation verify | Write test suites, run coverage, report bugs | Test files, bug tickets |
| **Code Reviewer** | `code-reviewer` | 1–5 | Pre-merge validation; security check; standards compliance | Review diffs, run linters, check OWASP checklist | Review report (APPROVED / CHANGES\_REQUESTED) |
| **Security Auditor** | `security-auditor` | 3–13 | Security concern flagged; new auth/data pathway; dependency change | Threat modelling, OWASP Top 10 scan, compliance check | Security report, remediation plan |
| **Safety & Compliance** | `safety-compliance` | 5–13 | Safety-critical code; embedded/real-time system; regulatory target | Power of 10 rules, MISRA C, CERT standards review | Compliance checklist, violations list |
| **DevOps** | `devops` | 2–8 | CI/CD change; deployment; infrastructure update | Pipeline config, container setup, release management | Deployment scripts, runbooks |
| **Documentarian** | `documentarian` | 1–3 | Docs missing or stale; post-implementation docs pass | Write docstrings, API docs, README updates | Markdown docs, inline comments |
| **Researcher** | `researcher` | 3–8 | Evaluate libraries/approaches; unknown domain; architectural spike | Search docs, compare options, produce recommendations | Research report with decision |

---

## Architecture

### AgentProtocol Dispatch

`agent_protocol.py` (`AgentProtocol`) is the central request/response loop.
It wraps any AI adapter and drives a tool-use conversation until the agent calls
`task_done` or the iteration cap is reached.

```
SprintRunner
  └─► AgentProtocol(adapter, provider_config)
        ├─ planning_quality_gate()   — pre-execution plan validation
        ├─ execute_loop()            — iterative tool-use conversation
        │    ├─ build_request()      — inject task, wiki, history, standards
        │    ├─ adapter.send()       — call AI model
        │    └─ AgentExecutor.run()  — execute returned action(s)
        └─ result → SprintRunner     — success/failure + files changed
```

### Provider Routing

Before dispatching a task, `SprintRunner._route_provider_by_complexity()` selects
the AI provider based on task complexity:

- **complexity ≤ 3** — looks up a provider named `gemma3:4b` or `fast` in the DB
- **complexity 4–8** — looks up `gemma3:12b` or `medium`
- **complexity > 8** — uses the primary configured provider

If the named provider is not registered, routing falls back to the primary provider
without error.

### Handoff Protocol

When one agent completes a task that requires another agent's attention, it writes
a `handoff_json` blob into the task row:

```json
{
  "from_agent": "developer",
  "to_agent":   "tester",
  "context":    "...",
  "deliverables": ["src/foo.py"],
  "blockers":   []
}
```

The sprint runner reads `handoff_json` on task completion and may route a follow-up
task to the nominated agent in the next iteration.

### Planning Phase

Every task execution runs a **planning pass** before the main agent loop:

1. The agent receives task + context and produces a plan.
2. A second model call acts as a **quality gate** — it verifies completeness and
   reads any files the plan references but that were not yet loaded.
3. The validated plan is injected as a `_plan` key in the task dict so all
   subsequent iterations see it as grounding context.

### Subtask System

Agents can decompose tasks at runtime using three actions:

| Action | Effect |
|--------|--------|
| `create_subtask` | Insert a subtask row linked to the current task |
| `complete_subtask` | Mark subtask done; triggers parent auto-complete when all done |
| `check_subtask` | Read current status of a subtask |

Subtasks survive agent iterations and are visible to all subsequent agents on the
same task, preventing duplication.

---

## Adding a Custom Agent

1. Create `agents/<slug>.md` following the structure of any existing role file.
2. Reference the slug when calling `AgentProtocol` or injecting a task with
   `agent: <slug>` in the pipeline config.
3. No code changes required — the protocol loads role files by slug at runtime.

---

## Pipeline Config (`pipeline-config.json`)

The pipeline config defines the ordered steps executed per sprint. Each step can
invoke a builtin, run a shell command, or call a named agent role. See
`pipeline-config.json` at the repo root for the default configuration.

---

## Sprint E — Advanced Agent & Provider Configuration

### Provider Routing Configuration (Complexity Tiers)

AIxYad routes tasks to different AI providers based on task complexity.
Routing is stored in the `complexity_routing` table in the global DB.

**Default tiers** (configured automatically by `SprintRunner._route_provider_by_complexity`):

| Complexity | Tier | Default Provider Lookup |
|------------|------|------------------------|
| 1–3        | Low  | Provider named `gemma3:4b` or `fast` |
| 4–8        | Med  | Provider named `gemma3:12b` or `medium` |
| 9–13       | High | Primary configured provider |

**To customise via UI:** Settings → Routing tab. Each tier shows a dropdown of
all registered providers. Click "Save Routing" to persist.

**To customise via DB:**
```sql
INSERT INTO complexity_routing
  (project_name, min_complexity, max_complexity, provider_id)
VALUES ('*', 1, 3, 2);   -- route complexity 1-3 to provider id=2
```

### Agent Health Monitoring

Every time an agent completes a task, a row is written to
`agent_execution_history` in the project DB:

| Column | Description |
|--------|-------------|
| `agent_slug` | Which agent ran |
| `task_display_id` | Task reference |
| `status` | `done`, `failed`, or `blocked` |
| `duration_s` | Wall-clock seconds |
| `total_tokens` | Prompt + completion tokens used |
| `files_changed` | Number of files written/edited |

**To query metrics:**
```python
from aixyad_db import get_agent_stats, get_all_agent_stats
stats = get_agent_stats(conn, 'developer')
# {'slug': 'developer', 'runs': 12, 'success_rate': 83.3,
#  'avg_duration': 45.2, 'total_tokens': 18400, 'last_run': '2026-04-09 ...' }
```

**In UI:** Each agent row in the Agents screen has a bar-chart icon (Performance)
showing these metrics. The "Metrics" top-level tab shows a table of all agents.

### Fallback Chain Configuration

If an agent fails a task (returns `failed` or `blocked` status), AIxYad can
automatically retry with a different agent.

**Set via agent definition DB column `fallback_slug`:**
```sql
UPDATE agent_definitions SET fallback_slug = 'senior-developer'
WHERE slug = 'developer';
```

**Set via UI:** Agents screen → select agent → "Fallback Agent" dropdown.

**Behaviour:** When sprint_runner detects a failure and the agent definition has
`fallback_slug` set, it re-queues the task with the fallback agent before
invoking complexity bumping and rollback.

### Action Filter Configuration

Agents can be restricted to a whitelist of allowed actions. If `action_filter`
is set on an agent definition, any action not in the list returns:

```
{"success": false, "message": "Action 'write_file' not permitted for agent 'reviewer'"}
```

**Set via UI:** Agents screen → select agent → "Action Filter" field.
Enter comma-separated action names, e.g.:
```
read_file, search, query_tasks, step_done
```

**Set via DB:**
```sql
UPDATE agent_definitions
SET permissions = json_set(COALESCE(permissions,'{}'), '$.action_filter',
                           json('["read_file","search","step_done"]'))
WHERE slug = 'reviewer';
```

**Common action names:**
- `read_file`, `write_file`, `edit_file` — file operations
- `search`, `list_files`, `query_files` — search/discovery
- `run_command`, `run_script` — shell execution
- `query_tasks`, `update_task`, `add_task` — DB task management
- `step_done`, `need_help` — always allowed regardless of filter

### Import/Export Format

Agent definitions can be exported and imported as JSON via the Agents screen
(Export/Import buttons in the header).

**Format:**
```json
[
  {
    "slug": "developer",
    "definition": "# developer\n\nYou are a senior software developer..."
  },
  {
    "slug": "tester",
    "definition": "# tester\n\nYou write comprehensive test suites..."
  }
]
```

Single agent export is also valid (object instead of array):
```json
{
  "slug": "my-agent",
  "definition": "# my-agent\n\n..."
}
```

**Import behaviour:** If a slug already exists, its definition is updated.
New slugs are inserted. Other agent fields (complexity range, provider, etc.)
are not modified by import — edit those via the agent editor after importing.
