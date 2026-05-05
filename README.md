# Automatica

> Multi-agent AI software development framework — autonomous sprint runner, headless server, agent protocol

Automatica is a provider-agnostic engine for running AI agents against software projects.
It manages sprints, tasks, agent loops, and project context — controlled entirely from the
command line or connected to via REST + WebSocket from a UI.

---

## Install

```bash
pip install automatica

# With API server support:
pip install automatica[server]

# With vision/screenshot support:
pip install automatica[vision]

# Everything:
pip install automatica[full]
```

---

## Default AI Model — gemma3:4b via Ollama

Automatica ships with **gemma3:4b** as the default local model for simple tasks,
routed automatically via Ollama.

```bash
# 1. Install Ollama (https://ollama.com)
# 2. Pull the default model
ollama pull gemma3:4b

# 3. Register it in Automatica (one-time)
automatica --AddProvider "Ollama" \
    --ProviderType http_local_chat \
    --ProviderUrl   http://localhost:11434 \
    --ProviderModel gemma3:4b
```

Automatica auto-registers `gemma3:4b` as the "fast" provider on first run when
Ollama is reachable. It is used for low-complexity tasks (complexity ≤ 3) so that
API quota on larger models is preserved for harder work.

---

## First-Time Setup

Add an AI provider before running sprints. Providers are stored in the global DB
(`~/.automatica/automatica.db`) — not hardcoded.

```bash
# List currently configured providers
automatica --ListProviders

# Add a local inference server (e.g. Ollama)
automatica --AddProvider "Ollama" \
    --ProviderType http_local_chat \
    --ProviderUrl   http://localhost:11434 \
    --ProviderModel llama3.2:3b

# Add any OpenAI-format cloud API
automatica --AddProvider "My Cloud Provider" \
    --ProviderType http_chat_completions \
    --ProviderUrl   https://api.example.com \
    --ProviderModel model-name \
    --ProviderKeyEnv MY_API_KEY_ENV_VAR

# Remove a provider
automatica --RemoveProvider "My Cloud Provider"
```

See [adapters/README.md](adapters/README.md) for detailed setup examples for common
API formats (OpenAI-compatible, generateContent, local servers, CLI tools).

---

## Quick Start

```bash
# Run one sprint on a project
automatica --Project /path/to/project --StartSprint --provider Ollama

# Continuous polling (re-run sprint every 5 minutes)
automatica --Project /path/to/project --Polling 5min --provider Ollama

# Start headless API server (connects to Automatica App on phone/PC)
automatica --Serve

# Server + continuous sprint loop
automatica --Serve --Project /path/to/project --Polling 5min --provider Ollama

# Initialize a project with a goal
automatica --Project /path/to/project --ProjectGoal "Build a 2D pong game" --provider Ollama
```

---

## CLI Reference

### Sprint & Project Commands

| Flag | Description |
|------|-------------|
| `--Project <path>` | Project directory |
| `--ProjectGoal <text>` | Initialize project with a goal |
| `--StartSprint` | Run next sprint and exit |
| `--Polling <interval>` | Re-run sprints continuously (e.g. `5min`, `30s`, `1h`) |
| `--AddTask <title>` | Add a task to the project backlog |
| `--AddSprint <goal>` | Create a sprint (dry-run planning only) |
| `--OrderBacklog` | Reprioritise backlog |
| `--SprintReview` | Show last sprint summary |
| `--provider <name>` | AI provider name — must match a configured provider in the DB |
| `--Model <name>` | Model name for local providers (default: `llama3.2:3b`) |
| `--max-minutes <n>` | Sprint time limit in minutes (default: 15) |
| `--max-sprints <n>` | Max sprints in polling mode (default: 10) |
| `--max-iterations <n>` | Max agent iterations per task (default: 12) |

### Server Commands

| Flag | Description |
|------|-------------|
| `--Serve` | Start REST + WebSocket API server |
| `--Port <n>` | API server port (default: 8765) |
| `--Host <addr>` | API server bind host (default: 0.0.0.0) |

### Provider Management (global — no project required)

| Flag | Description |
|------|-------------|
| `--ListProviders` | List all configured AI providers |
| `--AddProvider <name>` | Add a provider (use with flags below) |
| `--ProviderType <type>` | Adapter: `http_chat_completions` · `http_local_chat` · `http_generate_content` · `subprocess_cli` |
| `--ProviderUrl <url>` | Provider base URL |
| `--ProviderModel <model>` | Default model name |
| `--ProviderKeyEnv <var>` | Env var name holding the API key |
| `--RemoveProvider <name>` | Remove a provider by name or ID |

---

## AI Adapters

Automatica uses protocol-named adapters — no provider is hardcoded.
Bring your own credentials and configure any compatible service.

| Adapter type | Protocol | Typical use |
|---|---|---|
| `http_chat_completions` | `POST /v1/chat/completions` + Bearer auth | Any OpenAI-format API |
| `http_generate_content` | `POST /:model:generateContent?key=X` | Google-style generateContent APIs |
| `http_local_chat` | `POST /api/chat`, no auth | Local inference servers (Ollama-style) |
| `subprocess_cli` | Shells out to a CLI binary | Any AI tool with a CLI |

Full setup guide with examples: [adapters/README.md](adapters/README.md)

---

## Architecture

```
run_autonomous.py     CLI entry point — flags, provider management, sprint loop
sprint_runner.py      Sprint lifecycle (Refinement → Planning → Execution → Review)
agent_protocol.py     Agent request/response loop with AI providers
agent_executor.py     Sandboxed action execution (file ops, shell, DB, vision)
automatica_db.py      SQLite — global DB (providers, agents) + per-project DB
ai_adapters.py        Adapter registry shim (protocol-named adapters in adapters/)
adapters/             Pluggable adapter package — add/remove without touching other code
db_actions.py         DB operations exposed to agents
validation.py         Action schema validation + rollback
view_actions.py       Hierarchical file view/edit system for agents
vision_actions.py     Screenshot + UI automation actions
project_scanner.py    Live file structure queries
api_server.py         FastAPI + WebSocket server (--Serve mode)
agents/               Agent role definitions (markdown, loaded at runtime)
standards/            Language-specific coding standards (markdown, loaded at runtime)
```

---

## Wiki Index

`project_scanner.build_file_wiki(project_dir)` builds a compact JSON index of every
source file in a project — path, language, line count, last-modified timestamp, and a
one-line summary extracted from docstrings or comments.

```
.automatica/wiki_index.json
```

The wiki is regenerated automatically at sprint start and cached; subsequent calls
use the cache unless a file has changed.  Agents consume it via the `query_wiki`
action, which lets them look up any file's metadata before deciding what to read or
edit — avoiding wasted reads on irrelevant files.

---

## Subtask System

Agents can decompose any task into an ordered checklist of subtasks at runtime:

| Action | Description |
|--------|-------------|
| `create_subtask` | Add a subtask to the current task |
| `complete_subtask` | Mark a subtask done; auto-completes parent when all done |
| `check_subtask` | Read subtask status |

Subtasks are stored in the project DB and survive between agent iterations.
When the last subtask is completed, the parent task is automatically marked done.
This enables recursive decomposition: a complex task gets broken into N subtasks,
each of which can itself have subtasks.

---

## Planning Phase

Every task execution begins with a **planning pass** before the main agent loop.
The agent receives the task and codebase context and produces a structured plan.
A **quality gate** then validates the plan:

1. A second model call reviews the plan for completeness and specificity.
2. If the plan references files that weren't read yet, the gate reads them.
3. The validated plan is injected into the main agent loop as grounding context.

This prevents agents from starting implementation before they understand scope,
and dramatically reduces wasted iterations.

---

## Provider Routing

Automatica routes tasks to different AI providers based on **task complexity** (1–13 scale):

| Complexity | Provider | Typical model |
|------------|----------|---------------|
| ≤ 3 (simple) | fast | gemma3:4b |
| 4–8 (medium) | medium | gemma3:12b |
| > 8 (complex) | primary | configured provider |

Provider names (`fast`, `medium`) are looked up by name in the global DB.
If a named provider isn't registered, routing falls back to the primary provider.
When tasks repeatedly fail, complexity is auto-bumped to the next Fibonacci step,
which eventually escalates them to a more capable model.

---

## REST API

When running with `--Serve`, Automatica exposes a REST + WebSocket API:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Engine health and active sprint info |
| `GET` | `/api/wiki/{project_path}` | File wiki index for a project |
| `GET` | `/api/projects` | List all known projects |
| `GET` | `/api/projects/{id}/tasks` | Tasks for a project |
| `POST` | `/api/projects/{id}/tasks` | Create a task |
| `POST` | `/api/sprints/start` | Start a sprint |
| `GET` | `/api/logs` | Recent log entries |
| `GET` | `/api/providers` | List configured providers |
| `POST` | `/api/providers` | Add a provider |
| `WS` | `/ws/logs` | Live log stream |
| `WS` | `/ws/status` | Live sprint status updates |

Default port: **8765**.  Connect the Automatica App by pointing it at
`http://<host>:8765`.

---

## Agent System

Agents are defined in `agents/` as markdown role files. Each defines responsibilities,
methodology, and quality standards for one role in the sprint workflow.

| Agent | Slug | Role |
|-------|------|------|
| Orchestrator | `orchestrator` | Coordinate workflow, route tasks |
| Product Owner | `product-owner` | Prioritise backlog, define acceptance criteria |
| Scrum Master | `scrum-master` | Break down tasks, identify parallel work |
| Architect | `architect` | System design, implementation plans |
| Developer | `developer` | Write code, unit tests |
| Tester | `tester` | Test coverage, regression testing |
| Code Reviewer | `code-reviewer` | Security review, standards compliance |
| Security Auditor | `security-auditor` | Vulnerability scanning, OWASP compliance |
| Safety & Compliance | `safety-compliance` | Power of 10, MISRA C, CERT standards |
| DevOps | `devops` | CI/CD, deployment, infrastructure |
| Documentarian | `documentarian` | Code comments, API docs |
| Researcher | `researcher` | Evaluate options, find solutions |

Agent definitions are in `agents/<slug>.md`. Add or replace agents without changing code.

---

## Project Instructions

Place an `AGENTS.md` file at the root of your project to provide context to agents —
role guidance, coding standards, project goals, file structure, or anything else.

```
your-project/
  AGENTS.md           ← project-specific instructions for agents
  .automatica/
    project.db        ← tasks, sprints, backlog
```

---

## Database Locations

| DB | Path | Contents |
|----|------|----------|
| Global | `~/.automatica/automatica.db` | Providers, agent configs, global metrics |
| Per-project | `<project>/.automatica/project.db` | Tasks, sprints, backlog, settings |

---

## Server Mode

```bash
automatica --Serve
# → API server on http://0.0.0.0:8765

automatica --Serve --Project /path/to/project --Polling 5min --provider Ollama
# → API server + continuous sprint loop
```

Connect from the **Automatica App** (PC or phone) by pointing it at `http://<host>:8765`.

---

## License

Copyright (c) 2025 Andrew Keith Watts. All rights reserved.

Proprietary software. Unauthorized reproduction, distribution, or modification
is prohibited without express written permission.

Contact: AndrewKWatts@Gmail.com
<!-- automatica-pipeline-ok 2026-05-05 -->
