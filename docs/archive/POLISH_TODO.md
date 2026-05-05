# Automatica — Daily Polish TODO
# Auto-managed by scripts/polish_loop.py
# Format: [ ] pending  [~] running  [x] done  [!] failed  [-] skipped
# Each item: TARGET | PRIORITY | DESCRIPTION
# TARGET: ENGINE=H:/Github/Automatica  UI=H:/Github/Automatica-App

---

## ✅ QUICK WINS — New Files (achievable with local 7B models)

- [x] ENGINE | HIGH | Create scripts/check_ollama.py: standalone CLI that calls http://localhost:11434/api/tags, prints each installed model name and size, exits 0 if all ok or exits 1 with error message if Ollama not reachable
- [x] ENGINE | HIGH | Create scripts/db_inspect.py: read-only CLI that opens .automatica/project.db, prints task counts by status (backlog/in_progress/done/failed), prints last 5 sprint summaries (number, status, completed/total tasks), no external deps
- [x] ENGINE | HIGH | Create tests/test_project_scanner.py: pytest unit tests for list_project_files() — create temp dir with 3 .py files, call list_project_files(), assert returns list with dicts containing path/language keys, assert correct file count
- [x] ENGINE | HIGH | Create tests/test_automatica_db.py: pytest unit tests for create_task and update_task functions — create in-memory sqlite DB, call init_project_db(), create a task, update its status, assert status changed correctly
- [x] ENGINE | HIGH | Create scripts/sprint_summary.py: CLI that takes --project PATH arg, reads .automatica/project.db, prints a human-readable table of all sprints with columns: sprint#, status, tasks_done/total, velocity, date
- [x] ENGINE | MEDIUM | Create docs/adapter_guide.md: markdown doc listing all AI adapter types (claude, ollama, http_local_chat, gemini, grok), with config keys, example settings.json snippets, and when to use each
- [x] ENGINE | MEDIUM | Create scripts/backup_db.py: copies .automatica/project.db to .automatica/backups/project_YYYYMMDD_HHMMSS.db, keeps last 10 backups, prints what was saved
- [x] ENGINE | MEDIUM | Create tests/test_sprint_runner_init.py: pytest test that imports SprintRunner, instantiates it with a temp project dir and provider=ollama config dict, asserts it initializes without exceptions
- [x] UI | MEDIUM | Create a simple H:/Github/Automatica-App/tests/test_nav_data.py: import nav_data, assert NAV_GROUPS has 4 groups with keys global/config/manager/project, assert project_sub_tabs() returns 4 items, assert get_item_tint('overview') returns 4-tuple
- [x] ENGINE | LOW | Create docs/polish_loop_guide.md: markdown doc explaining polish_loop.py — how to run it, what POLISH_TODO.md format is, what the cycle does (backup/register/sprint/mark), troubleshooting common errors (model not installed, wrong provider type, json truncation)

---

## 🔧 ENGINE — Core Stability & Self-Recovery

- [x] ENGINE | HIGH | Add try/except with provider fallback in sprint_runner._execute_single_task: if primary provider raises ConnectionError or TimeoutError, retry once with exponential backoff (2s, 4s) then fallback to next enabled provider in DB
- [x] ENGINE | HIGH | Add self-healing in run_autonomous.main loop: if a sprint produces 0 completed tasks and 3+ failed tasks in a row, auto-bump task complexity by 1 Fibonacci step and re-queue before next sprint
- [x] ENGINE | HIGH | Fix agent_protocol.py: add `query_wiki` action that calls project_scanner.get_file_index() and returns a compact JSON tree of all project files with their GUIDs and line counts — agents use this during planning
- [x] ENGINE | HIGH | Improve build_request in agent_protocol.py: include a `wiki_summary` field (top 30 files by recency from file index) and `task_dependency_chain` (all ancestor tasks this task depends on, with their status) so agents have full planning context before first action
- [x] ENGINE | HIGH | Add recursive subtask system: new DB table `subtasks` (id, task_id, title, status, iterations, result, created_at). Agent can call `create_subtask` action, work on it, call `complete_subtask` with result. Parent task only calls step_done after all subtasks are done or explicitly abandoned
- [x] ENGINE | MEDIUM | Add compact history storage: DB table `agent_history` (id, task_id, sprint_id, iteration, role, content_compressed, token_estimate, created_at). Store history blobs compressed with zlib. Load last N items on resume
- [!] ENGINE | MEDIUM | Add `query_file_section` action to agent_executor: given file path + section GUID, return only that section (between GUID markers). Faster than read_file for large files
- [x] ENGINE | MEDIUM | Improve GUID system in agent_executor.write_file and edit_file: automatically add/update `# [GUID:xxxx]` section markers at function/class boundaries for Python, JS, TS, C, C++, Go, Rust, Java files using AST/regex detection
- [x] ENGINE | MEDIUM | Add `search_by_guid` action: scan all project files for a specific GUID tag, return file path + line number + surrounding context (10 lines). Enables agents to find and update specific code sections by ID
- [x] ENGINE | MEDIUM | Add wiki index generation to project_scanner.py: new function `build_file_wiki(project_dir)` that returns dict of {path: {size, lines, guids[], last_modified, language}} for all source files. Cache result to .automatica/wiki_index.json, refresh if any file mtime is newer
- [x] ENGINE | MEDIUM | Update settings.json ollama action_filter to include search, query_file_structure, query_files, query_file_section, query_wiki actions for gemma3:4b (already done in this session — verify and test)
- [x] ENGINE | LOW | Add `estimate_tokens` utility: before sending request to Ollama, estimate token count of prompt (rough: chars/3.5). If over 3000 tokens, trim history more aggressively. Log estimated token counts to action_log
- [x] ENGINE | LOW | Add sprint velocity trend to sprint_review output: show last 5 sprints' velocity + rolling average. Store velocity history in project_settings as JSON array
- [x] ENGINE | LOW | Improve `need_help` action handling: when agent fires need_help, write a structured escalation record to DB (escalation_log table) with task_id, iteration, reason, context snapshot. Show these in sprint review output

---

## 🧠 ENGINE — Planning Phase Intelligence

- [x] ENGINE | HIGH | Before task execution begins, run a lightweight planning pass: agent receives wiki_summary + task description + dependency chain, responds with a JSON plan: {files_to_read: [], files_to_create: [], files_to_edit: [], subtasks: [], estimated_iterations: int, approach: str}. Store plan in task.description_json. Validate plan has required keys before proceeding
- [x] ENGINE | HIGH | Share planning context with each subtask: when a subtask is created, automatically include: parent task title + plan, the specific files identified for this subtask, and any results from previously completed subtasks of the same parent task
- [x] ENGINE | MEDIUM | Add planning quality gate: after planning pass, run a second model call asking "Is this plan complete and specific enough to execute? List any missing information." If model returns missing items, do one round of clarification queries (read relevant files) before starting execution
- [x] ENGINE | MEDIUM | Implement agent handoff protocol: when workflow requires Developer → Tester → CodeReviewer, pass a structured handoff dict {files_changed, tests_written, coverage_notes, known_issues} between agents. Store handoff in sprint_tasks.handoff_json column (add migration)
- [ ] ENGINE | LOW | Add task complexity auto-estimation: before sprint planning, for each backlog task with complexity=3 (default), call a fast model pass to estimate actual complexity based on: task description, wiki file count, similar past task velocities. Update complexity in DB

---

## 🔌 ENGINE — Provider & Model Management

- [x] ENGINE | HIGH | Register gemma3:4b as the default Ollama provider in global DB if not already present: run cmd_add_provider with name=Gemma3-4b, type=http_local_chat, url=http://localhost:11434, model=gemma3:4b. Also add gemma3:12b as a fallback
- [x] ENGINE | HIGH | Add model auto-detection on startup: call ollama_utils.list_installed_models() at sprint start, warn if configured model not found, suggest available alternatives. Fall back to first available model if configured one missing
- [x] ENGINE | MEDIUM | Add provider health check before sprint: ping Ollama /api/tags endpoint, measure latency, log to provider_performance table. Skip sprint and log error if Ollama not responding after 3 retries (5s each)
- [x] ENGINE | MEDIUM | Add provider routing by task complexity: tasks with complexity<=3 → gemma3:4b (fast), complexity 4-8 → gemma3:12b (better), complexity>8 → phi4 or claude (if configured). Stored in complexity_routing table
- [ ] ENGINE | LOW | Add token/cost tracking per task: record estimated token usage in usage_log. Add total_tokens column to sprint_tasks table. Show token budget in sprint review

---

## 🧪 ENGINE — Testing

- [!] ENGINE | HIGH | Run full test suite: cd H:/Github/Automatica && python -m pytest test_automatica_db.py test_protocol.py -v --timeout=60 and report results. Fix any failures found
- [x] ENGINE | HIGH | Add integration test for wiki index: create a test project dir, write 5 files with GUID markers, call build_file_wiki(), assert all files indexed and GUIDs found
- [x] ENGINE | MEDIUM | Add integration test for subtask system: create a task, create 2 subtasks, complete them, verify parent task auto-completes after all subtasks done
- [x] ENGINE | MEDIUM | Add smoke test for gemma3:4b: if Ollama running and gemma3:4b installed, run a 1-task sprint on a trivial task (write hello world), assert task completes within 5 iterations
- [ ] ENGINE | LOW | Add test for provider fallback: mock primary provider to raise ConnectionError, assert fallback provider is used and task completes

---

## 🎨 UI — Automatica-App Polish

- [x] UI | HIGH | Verify app launches clean: run python main.py with 18s timeout, confirm EXIT:124 (timeout = clean run), no crash_*.txt files created
- [x] UI | HIGH | Run test_navigation.py smoke test: python test_navigation.py — assert 5 pass, 1 skip (headless skip is correct). Fix any actual failures
- [x] UI | HIGH | Fix nav rail active state on first load: in SoftNavRail._rebuild(), after building all buttons, ensure set_active(_active_key) is called via Clock.schedule_once(lambda dt: self.set_active(self._active_key), 0) so it fires after layout is complete
- [x] UI | MEDIUM | Add project wiki viewer tab to project_detail.py: new tab key 'wiki' that shows project file index (calls api_client.get_file_wiki or db.get_file_wiki if server mode), renders as a SectionList with file paths + sizes + GUID counts
- [x] UI | MEDIUM | Polish management.py Projects tab: add last-opened timestamp to each project row, format as "X days ago" or date string. Add search/filter field above the list
- [x] UI | MEDIUM | Polish overview.py Stats tab: animate stat card values counting up from 0 on each activate (not just opacity fade). Use Clock.schedule_interval with 0.4s total duration
- [x] UI | MEDIUM | Add empty-state illustrations to overview stats: when no project open, show centered message with icon (folder-open-outline) and "Open a project to see stats" text in _TEXT color
- [ ] UI | LOW | Ensure SoftTabBar width fills available space: if tab items are few and narrow, distribute remaining space evenly as padding rather than left-aligning all tabs
- [ ] UI | LOW | Add keyboard shortcut hints to nav rail: small superscript labels showing Ctrl+1 through Ctrl+9 for the first 9 nav items (desktop only — check platform)
- [ ] UI | LOW | Polish setup_wizard.py: add a smooth fade transition between steps using Animation(opacity=0) → switch content → Animation(opacity=1) on the step card

---

## 🔗 ENGINE+UI — Integration

- [x] ENGINE | HIGH | Add REST endpoint GET /api/wiki/{project_path} to api_server.py: returns the wiki index JSON for a project path. UI wiki viewer calls this in client mode
- [x] ENGINE | MEDIUM | Add WebSocket event `task_planning` pushed when agent starts planning pass (before execution): includes task_id, plan JSON. UI can show live planning state in logs screen
- [x] ENGINE | MEDIUM | Add WebSocket event `subtask_created` and `subtask_completed`: UI project detail Logs tab shows subtask progress in real time
- [x] UI | MEDIUM | Wire LogsContent to consume WebSocket events from api_client: when api_client fires on_log_entry callback, append entry to live log list without full refresh. Currently _load_logs() does full reload each poll — optimize to append-only

---

## 📚 ENGINE — Documentation & Config

- [ ] ENGINE | LOW | Update README.md: add section on gemma3:4b as default model, how to install (ollama pull gemma3:4b), add section on wiki index system, subtask system, and planning phase
- [ ] ENGINE | LOW | Add AGENTS.md: document all 12 agent roles, their complexity ranges, triggers, and outputs. Cross-reference with agents-config.json
- [ ] ENGINE | LOW | Update pipeline-config.json: add a wiki_index step before Task Execution that runs project_scanner.build_file_wiki() and caches result. Add subtask review step after Task Execution

---

## 🎯 SPECIFIC IMPLEMENTATION TASKS — File/Method Level

### agent_protocol.py — AgentProtocol class

- [x] ENGINE | HIGH | In agent_protocol.py `AgentProtocol.build_request()` (line ~177): add `wiki_summary` key to the returned `request` dict. Call `list_project_files(self.project_dir)` from project_scanner, sort by `mtime` descending (add `mtime` field to `list_project_files` return via `file_path.stat().st_mtime`), take top 25, store as `request['wiki_summary'] = [{'path', 'language', 'line_count', 'size_bytes'}, ...]`. Only add if project_dir has more than 0 source files. Guard with try/except.

- [x] ENGINE | HIGH | In agent_protocol.py `AgentProtocol.build_request()` (line ~213): add `task_dependency_chain` key — query `pdb.execute("SELECT t2.display_id, t2.title, t2.status FROM tasks t2 JOIN tasks t1 ON t1.depends_on_task_id = t2.id WHERE t1.display_id = ?", (task.get('display_id',''),))` and recurse up to 5 levels. Append list of `{display_id, title, status}` dicts to `request['task_dependency_chain']`. Guard entire block in `if self.db_handler and self.db_handler.pdb` and try/except.

- [x] ENGINE | HIGH | In agent_protocol.py `AgentProtocol.__init__()` (line ~148): add `self.provider_config = provider_config` assignment so `_confirm_done` and `execute_loop_atomic` can access raw config (currently only `self.adapter.config` is used, which loses keys set before adapter construction). Store as `self._raw_provider_config = provider_config or {}`.

- [x] ENGINE | MEDIUM | In agent_protocol.py `AgentProtocol.execute_loop()` (line ~271): replace bare `except Exception as exc:` around `self.adapter.send(request)` with a retry loop: catch `(ConnectionError, TimeoutError, OSError)` separately, sleep 2s then retry once with the same request; only on second failure OR on other exceptions fall through to the current `return {'success': False, ...}`. Log retry attempt to `self.history` with role='system'.

- [x] ENGINE | MEDIUM | In agent_protocol.py `AgentProtocol.execute_loop_atomic()` (line ~467): the inner step loop currently returns `success=True` mid-loop when the step action matches (lines ~639-651) but discards `files_changed` from earlier steps. Fix: accumulate `files_changed` at the outer loop level (`files_changed.extend(...)`) and return the full list in the final success dict at line ~669 instead of only the current step's path.

- [x] ENGINE | MEDIUM | In agent_protocol.py `AgentProtocol._confirm_done()` (line ~737): add a short-circuit: if `len(self.executor.action_log) == 0` (agent called step_done on iteration 0 with no actions), skip the confirm round-trip and return `'retry'` immediately with a history message saying "No actions were taken — please do the task before calling step_done."

- [ ] ENGINE | LOW | In agent_protocol.py module-level `AVAILABLE_ACTIONS` list (line ~69): add `query_wiki` action entry — `{"name": "query_wiki", "description": "Get a compact file index of the project (path, language, line_count, last_modified) sorted by recency", "params": {"limit": "int (optional, max files, default 25)"}}`. Also add `get_json_keys` action entry: `{"name": "get_json_keys", "description": "List top-level keys of a JSON config file with types and previews", "params": {"file_path": "string"}}`.

- [x] ENGINE | LOW | In agent_protocol.py `AgentProtocol.execute_loop()` (line ~434): replace `time.sleep(0.5)` with a configurable delay: `delay = getattr(self.adapter, 'config', {}).get('loop_delay_s', 0.5)` then `time.sleep(delay)`. This allows Ollama configs to set `"loop_delay_s": 0` for faster iteration, and cloud APIs to set higher values for rate limiting.

---

### agent_executor.py — AgentExecutor class

- [x] ENGINE | HIGH | In agent_executor.py `AgentExecutor.execute()` (line ~175): add `query_wiki` and `get_json_keys` to the dispatch chain — these are scanner actions, not DB actions, so add them to `PERMISSION_MAP` (line ~91) as `"query_wiki": "can_search"` and `"get_json_keys": "can_search"`, then implement `_do_query_wiki(self, params)` and `_do_get_json_keys(self, params)` methods. `_do_query_wiki` calls `list_project_files(self.project_dir)`, adds mtime via `Path(self.project_dir / f['path']).stat().st_mtime`, sorts descending, returns top `params.get('limit', 25)` as formatted text. `_do_get_json_keys` calls `project_scanner.get_json_keys(self.project_dir, params['file_path'])`.

- [x] ENGINE | HIGH | In agent_executor.py `AgentExecutor._do_write_file()` (line ~344): currently injects a `# GUID: {uuid}` header only for `.py` files. Extend to also inject for `.js`, `.ts`, `.tsx` files using `// GUID: {uuid}` prefix, and for `.cpp`, `.hpp`, `.h`, `.c` files using `// GUID: {uuid}` prefix. Check `rel_path` suffix before choosing comment style.

- [x] ENGINE | HIGH | In agent_executor.py `AgentExecutor._DB_ACTIONS` frozenset (line ~210): add `'get_config'`, `'set_config'`, `'get_agent_def'` to the frozenset — these are currently declared in `PERMISSION_MAP` and `AVAILABLE_ACTIONS` but are missing from `_DB_ACTIONS`, so they fall through to the `_do_*` dispatch which has no handler and returns UNKNOWN ACTION. Verify `DbActionHandler.handle()` accepts these three action names.

- [x] ENGINE | MEDIUM | In agent_executor.py `AgentExecutor._do_edit_file()` (line ~378): after a successful write at line ~436, record the file read hash so subsequent edits in the same session pass the freshness check. Call `record_file_read(self._db_handler.pdb, rel_path, self._session_id, project_dir=self.project_dir)` inside the existing `if self._db_handler` block — currently only `_do_read_file` calls this, so editing without a prior read causes a false freshness failure on the second edit.

- [x] ENGINE | MEDIUM | In agent_executor.py `AgentExecutor._do_search()` (line ~571): add `last_modified` to the search result output per matched file — after collecting matches, stat each unique file path and append `(modified: YYYY-MM-DD)` to the file header line. Cap at 50 unique files to avoid stat spam. This helps agents know which files are stale vs. recently changed without a separate `list_files` call.

- [ ] ENGINE | LOW | In agent_executor.py `AgentExecutor._do_list_files()` (wherever it is): add `last_modified` ISO timestamp to each result entry by calling `Path(full_path).stat().st_mtime` and formatting with `datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')`. Append to the output line so agents see recency without a separate stat call.

- [ ] ENGINE | LOW | In agent_executor.py `AgentExecutor._log()` (line ~1040 area): currently `summary` is truncated to 80 chars from `result['message']`. Change truncation source to strip the "ACTION: X — SUCCESS/FAIL" prefix first (split on " — ", take index 1 if present) so the summary field in `get_log()` contains the meaningful part, not the boilerplate prefix.

---

### sprint_runner.py — SprintRunner class

- [x] ENGINE | HIGH | In sprint_runner.py `SprintRunner._execute_single_task()` (line ~459): add per-task attempt tracking. The current `attempt_number=1` hardcode at line ~626 means `handle_agent_failure` always treats it as the first attempt. Add a DB query before the protocol run: `attempt_number = pconn.execute("SELECT COUNT(*) FROM sprint_tasks st JOIN tasks t ON t.id=st.task_id WHERE t.display_id=? AND st.status IN ('blocked','failed')", (display_id,)).fetchone()[0] + 1` and pass that value to `handle_agent_failure`.

- [x] ENGINE | HIGH | In sprint_runner.py `SprintRunner.review_sprint()` (line ~696): add velocity history persistence. After computing `sprint_result.velocity`, load existing velocity array from project settings: `hist = json.loads(pconn.execute("SELECT value FROM project_settings WHERE key='velocity_history'").fetchone()['value'] or '[]')`, append `{"sprint": sprint_number, "velocity": round(velocity, 2), "date": datetime.now().isoformat()}`, trim to last 10 entries, write back with `pconn.execute("INSERT OR REPLACE INTO project_settings(key,value) VALUES(?,?)", ('velocity_history', json.dumps(hist)))`. Use this in sprint review output to show trend.

- [x] ENGINE | MEDIUM | In sprint_runner.py `SprintRunner.execute_sprint()` (line ~316): add a zero-velocity self-healing guard. After tallying results (line ~404), check: `if result.tasks_completed == 0 and result.tasks_failed >= 3`. If true, call `_bump_all_failed_complexities(conn, sprint_result.task_results)` — a new private method that calls `handle_agent_failure` for each failed task with `attempt_number=1` to trigger a complexity bump, then logs a ceremony note "Auto-bumped complexity for {N} tasks due to 0-velocity sprint".

- [x] ENGINE | MEDIUM | In sprint_runner.py `SprintRunner._execute_single_task()` (line ~525): before creating the `AgentProtocol` instance, call `project_scanner.list_project_files(self.project_dir)` to get a file count; if count > 0, build a compact `context['wiki_summary']` (top 20 files by mtime) and add it to the `context` dict passed to `load_context`. This ensures the wiki is available even before `build_request` adds it, so it appears in the task's initial context (not just the request header).

- [ ] ENGINE | LOW | In sprint_runner.py `SprintRunner.run_sprint()` (line ~796): add a pre-sprint Ollama health check. Before `steps = get_pipeline_steps(...)`, call `_check_provider_health(self.provider, self.provider_config)` — a new module-level function that POSTs to `{url}/api/tags`, measures latency, logs to `provider_performance` table via `gconn`, and returns `(ok: bool, latency_ms: int)`. If `not ok` after 3 retries with 5s sleep, return an empty `SprintResult` with `status='provider_unavailable'` and log the error.

---

### project_scanner.py — new and modified functions

- [x] ENGINE | HIGH | In project_scanner.py: add new module-level function `build_file_wiki(project_dir: Path, cache_path: Path = None) -> dict`. Walk all project files via `_walk_files()`, collect `{path, size_bytes, line_count, language, last_modified (ISO from st_mtime), guids: []}` for each. Scan file content for `# GUID:`, `// GUID:` patterns and populate `guids` list. Cache result to `project_dir / '.automatica' / 'wiki_index.json'`; skip rebuild if cache exists AND no file has `st_mtime` newer than the cache file's own `st_mtime`. Return `{"files": [...], "generated_at": ISO, "total_files": N, "total_lines": N}`.

- [x] ENGINE | HIGH | In project_scanner.py `list_project_files()` (line ~601): add `last_modified` field to each returned dict — `'last_modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%dT%H:%M:%S')` using the `stat` already computed at line ~629. Also add `mtime_epoch` (float) so callers can sort without re-parsing. Import `datetime` at module top if not already present.

- [x] ENGINE | MEDIUM | In project_scanner.py `_walk_files()` (line ~735): add an optional `sort_by_mtime: bool = False` parameter. When `True`, collect all matching paths into a list first, sort by `path.stat().st_mtime` descending, then yield. This lets `build_file_wiki` and `list_project_files` return recency-sorted results without a post-processing sort in callers. Default is `False` to preserve current behavior (fast streaming).

- [x] ENGINE | MEDIUM | In project_scanner.py `get_file_structure()` (line ~171): add a `guids` key to the returned dict. After reading file content, scan for `r'(?:#|//)\s*GUID:\s*([\w-]+)'` using `re.findall` and populate `result['guids'] = [...]`. This makes GUID extraction available to `build_file_wiki` and to agents calling `query_file_structure` without a separate search pass.

- [ ] ENGINE | LOW | In project_scanner.py `search_symbols()` (line ~537): add `last_modified` to each result entry — `entry['last_modified'] = datetime.fromtimestamp(file_path.stat().st_mtime).strftime('%Y-%m-%d')` where `file_path` is the full path resolved inside the loop. Cache the stat per file (dict keyed by rel_path) to avoid repeated stat calls when the same file yields multiple symbols.

---

### Database — new columns and tables

- [x] ENGINE | HIGH | Add DB migration: `ALTER TABLE sprint_tasks ADD COLUMN attempt_number INTEGER DEFAULT 1` — run this in `automatica_db.py init_project_db()` inside a `try/except OperationalError` (column already exists guard). Update `SprintRunner._execute_single_task()` to write `attempt_number` when calling `_update_sprint_task_status()`. This enables accurate per-task retry counting.

- [x] ENGINE | HIGH | Add DB migration: `ALTER TABLE tasks ADD COLUMN handoff_json TEXT DEFAULT NULL` — run in `init_project_db()` with OperationalError guard. Add `handoff_json` to the `update_task()` helper's accepted kwargs. Add `store_handoff` action to `AgentExecutor._DB_ACTIONS` and `DbActionHandler` that writes `{"files_changed": [], "tests_written": [], "coverage_notes": "", "known_issues": ""}` to this column for a given `display_id`.

- [x] ENGINE | MEDIUM | Add new DB table to `automatica_db.py init_project_db()`: `CREATE TABLE IF NOT EXISTS subtasks (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), display_id TEXT UNIQUE, title TEXT NOT NULL, description TEXT, status TEXT DEFAULT 'backlog', result TEXT, iterations INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')), completed_at TEXT, FOREIGN KEY(task_id) REFERENCES tasks(id))`. Add `create_subtask(pconn, parent_display_id, title, description)` and `complete_subtask(pconn, subtask_display_id, result)` helper functions.

- [x] ENGINE | MEDIUM | Add new DB table to `automatica_db.py init_project_db()`: `CREATE TABLE IF NOT EXISTS escalation_log (id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id), sprint_id INTEGER REFERENCES sprints(id), session_id TEXT, iteration INTEGER, reason TEXT, context_snapshot TEXT, created_at TEXT DEFAULT (datetime('now')))`. Add `log_escalation(pconn, task_display_id, session_id, iteration, reason, context_snapshot)` helper. Call this from `AgentProtocol.execute_loop()` when `action == 'need_help'` (line ~316), storing `json.dumps(request)[:5000]` as `context_snapshot`.

- [ ] ENGINE | LOW | Add `total_tokens` column: `ALTER TABLE sprint_tasks ADD COLUMN total_tokens INTEGER DEFAULT 0` in `init_project_db()` with OperationalError guard. In `agent_protocol.py log_prompt_response()` call sites (lines ~414, ~607), estimate tokens as `len(json.dumps(request)) // 3` (rough char/3 heuristic) and accumulate. After `execute_loop` returns, sum `tokens_in + tokens_out` across all `prompt_logs` for this `session_id` and write to `sprint_tasks.total_tokens` in `SprintRunner._execute_single_task()` after the loop completes.

---

### New action handlers to add to agent_executor.py dispatch

- [x] ENGINE | HIGH | Add `_do_create_subtask(self, params)` handler to `AgentExecutor` — params: `{parent_task_id: str, title: str, description: str (optional)}`. Requires `self._db_handler`. Calls `automatica_db.create_subtask(pdb, parent_display_id, title, description)`. Returns `{success, message, subtask_display_id}`. Add `'create_subtask'` to `_DB_ACTIONS` frozenset and `PERMISSION_MAP` as `"create_subtask": "can_write_file"`. Add action schema entry to `AVAILABLE_ACTIONS` in agent_protocol.py.

- [!] ENGINE | HIGH | Add `_do_complete_subtask(self, params)` handler to `AgentExecutor` — params: `{subtask_id: str, result: str}`. Calls `automatica_db.complete_subtask(pdb, subtask_display_id, result)`, then checks if all sibling subtasks are done via `SELECT COUNT(*) FROM subtasks WHERE task_id=? AND status != 'done'`; if zero remaining, calls `update_task(pdb, parent_display_id, status='done')`. Add to `_DB_ACTIONS` and `PERMISSION_MAP` as `"complete_subtask": "can_write_file"`.

- [x] ENGINE | MEDIUM | Add `_do_store_handoff(self, params)` handler to `AgentExecutor` — params: `{task_id: str, files_changed: list, tests_written: list, coverage_notes: str, known_issues: str}`. Validates params are present and list fields are lists. Calls `update_task(pdb, task_id, handoff_json=json.dumps({...}))`. Add to `_DB_ACTIONS` and `PERMISSION_MAP` as `"store_handoff": "can_write_file"`.

---

### Tests to add

- [x] ENGINE | HIGH | In test_protocol.py: add `class TestBuildRequest` with test `test_wiki_summary_included` — create a temp project dir with 5 `.py` files, init `AgentProtocol` with that dir (no DB), call `protocol.build_request(task, {}, {}, 0)`, assert `'wiki_summary'` key exists in result and `len(result['wiki_summary']) <= 25`. Add `test_action_filter_applied` — set `adapter.config['action_filter'] = ['read_file', 'step_done']`, call `build_request`, assert `len(result['available_actions']) == 2`.

- [x] ENGINE | HIGH | In test_protocol.py: add `class TestExecuteLoopAtomicFilesAccumulation` — create a task description with 3 numbered steps each writing a different file, mock `adapter.send()` to return `write_file` action for each step then `step_done`, run `execute_loop_atomic`, assert `len(result['files_changed']) == 3` (all three files accumulated, not just the last step's file).

- [x] ENGINE | HIGH | In test_automatica_db.py: add `class TestSubtaskSystem` with tests: `test_create_subtask_links_to_parent` (assert subtask row exists with correct task_id), `test_complete_subtask_updates_status` (assert subtask status='done' and result stored), `test_all_subtasks_done_completes_parent` (create 2 subtasks, complete both, assert parent task status='done').

- [x] ENGINE | MEDIUM | In test_automatica_db.py: add `class TestHandoffJson` with tests: `test_update_task_stores_handoff_json` (call `update_task(pconn, did, handoff_json='{"files_changed":["a.py"]}')`, fetch row, assert `json.loads(row['handoff_json'])['files_changed'] == ['a.py']`), `test_handoff_json_null_by_default` (create task, assert `handoff_json IS NULL`).

- [x] ENGINE | MEDIUM | In test_protocol.py: add `class TestExecutorQueryWiki` — create a temp dir with 3 `.py` files and 1 `.cpp` file, init `AgentExecutor`, call `executor.execute({'action': 'query_wiki', 'params': {}})`, assert `result['success'] is True` and all 4 file paths appear in `result['message']`. Also assert result message contains `'language'` substring.

- [x] ENGINE | MEDIUM | In test_automatica_db.py: add `class TestEscalationLog` with `test_log_escalation_stores_reason` — call `log_escalation(pconn, 'TASK-001', 'session_x', 5, 'model stuck', '{"context": "..."}')`, query `SELECT * FROM escalation_log WHERE session_id='session_x'`, assert row exists with correct `reason` and `iteration=5`.

- [ ] ENGINE | LOW | In test_protocol.py: add `class TestConfirmDoneNoActions` — mock `executor.action_log = []`, call `protocol._confirm_done(task, request, done_response, 1)`, assert return value is `'retry'` without making an adapter send call (confirm the short-circuit fires before the network round-trip).

---
_Last updated by polish_loop.py — see logs/polish_loop_YYYYMMDD.log for run history_
