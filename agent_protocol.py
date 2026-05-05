#!/usr/bin/env python3
"""
Automatica Agent Protocol — Orchestrates the request/response loop with external AI agents.

Usage:
    # As a library
    from agent_protocol import AgentProtocol
    protocol = AgentProtocol('Ollama', config, project_dir)
    result = protocol.execute_loop(task, context, instructions)

    # As CLI
    python agent_protocol.py --provider Ollama --project-dir H:\\Github\\Nova3D \\
        --task-file CURRENT_TASK.md --step 8 --config pipeline-config.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime


def _format_criteria_bullets(criteria) -> str:
    """Render acceptance_criteria as a bullet list whether stored as a string
    or a list. Iterating a bare string yields characters, which has produced
    one-character-per-bullet prompts that triggered Claude's safety filter
    (refusal with 'violates Usage Policy'). Always normalise to list-of-lines."""
    if not criteria:
        return "  (none specified)"
    if isinstance(criteria, list):
        items = [str(c).strip() for c in criteria if str(c).strip()]
    else:
        text = str(criteria).strip()
        # If it's a multi-line string, treat each non-empty line as a bullet.
        # Otherwise emit the whole string as a single bullet.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        items = lines if len(lines) > 1 else [text]
    return '\n'.join(f"  - {c}" for c in items) if items else "  (none specified)"


from agent_executor import AgentExecutor
from ai_adapters import get_adapter
from db_actions import DbActionHandler
from project_scanner import get_file_structure, search_symbols, list_project_files, get_file_section

try:
    from vision_actions import VisionActionHandler as _VisionActionHandler
    _VISION_ACTIONS_AVAILABLE = True
except ImportError:
    _VisionActionHandler = None  # type: ignore
    _VISION_ACTIONS_AVAILABLE = False

try:
    from view_actions import ViewActionHandler as _ViewActionHandler
    _VIEW_ACTIONS_AVAILABLE = True
except ImportError:
    _ViewActionHandler = None  # type: ignore
    _VIEW_ACTIONS_AVAILABLE = False


# Default permissions (safe defaults — read + search + limited commands)
DEFAULT_PERMISSIONS = {
    'can_read_file': True,
    'can_write_file': True,
    'can_edit_file': True,
    'can_delete_file': False,
    'can_create_file': True,
    'can_run_command': True,
    'can_search': True,
    'can_commit': True,
    'can_push': False,
    # Vision: screenshots enabled; UI input (clicks/typing) disabled by default
    'can_screenshot': True,
    'can_ui_input': False,
    'allowed_commands': ['cmake', 'ctest', 'git add', 'git commit', 'git status',
                         'git diff', 'git log', 'python', 'pip',
                         'powershell -command', 'powershell -noprofile',
                         'cmd /c type', 'cmd /c dir', 'cmd /c echo', 'cmd /c findstr'],
    'blocked_commands': ['git push', 'git reset --hard', 'rm -rf', 'del /s', 'format',
                         'shutdown', 'taskkill'],
    'blocked_paths': ['.env', '*.key', 'credentials*', '*.pem', '*.secret'],
}

# Available actions schema (sent to AI so it knows what it can do)
AVAILABLE_ACTIONS = [
    {"name": "read_file", "description": "Read file contents (returns full text)",
     "params": {"path": "string (relative to project)", "offset": "int (optional, line start)", "limit": "int (optional, max lines)"}},
    {"name": "write_file", "description": "Create a NEW file (fails if exists)",
     "params": {"path": "string", "content": "string"}},
    {"name": "edit_file", "description": "Replace text in existing file",
     "params": {"path": "string", "old_text": "string (exact match)", "new_text": "string"}},
    {"name": "search", "description": "Search files for a regex pattern (returns file:line: matches)",
     "params": {"pattern": "string (regex)", "path": "string (optional dir)", "glob": "string (optional file pattern)"}},
    {"name": "list_files", "description": "List files matching a glob",
     "params": {"pattern": "string (glob like *.cpp)", "path": "string (optional dir)"}},
    {"name": "run_command", "description": "Run an allowed shell command",
     "params": {"command": "string", "timeout": "int (optional, seconds)"}},
    {"name": "run_script", "description": "Run a .bat/.py from scripts/ directory",
     "params": {"script": "string (filename)", "args": "string[] (optional)"}},
    {"name": "commit", "description": "Git add + commit",
     "params": {"files": "string[] (paths to stage)", "message": "string"}},
    {"name": "query_wiki", "description": (
        "Get a compact file index of the project (path, language, line_count, last_modified) "
        "sorted by recency. Use this at the start of a task to understand the codebase."),
     "params": {"limit": "int (optional, max files to return, default 25)"}},
    {"name": "get_summary", "description": (
        "Cheap fetch of a path's existing in-source summary — module "
        "docstring for .py files, _folder_summary.md for folders, first "
        "paragraph for .md/.rst. Use this BEFORE read_file when you only "
        "need orientation; avoids pulling whole files into context."),
     "params": {"path": "string (file or folder, relative to project)"}},
    {"name": "find_relevant_files", "description": (
        "Rank project files by relevance to a free-text query. Scores by "
        "keyword matches against file paths, module docstrings, and folder "
        "summaries. Returns top-K paths with a one-line snippet each. Use "
        "this when planning a task to find which files matter."),
     "params": {"query": "string (free-text, e.g. 'paddle ai tracking')",
                 "top_k": "int (optional, default 10, max 30)"}},
    {"name": "search_by_guid", "description": (
        "Find a specific GUID tag across all project files. Returns the file path, "
        "line number, and 10 lines of surrounding context."),
     "params": {"guid": "string — the GUID to search for"}},
    # S3-W02: additional discovery actions
    {"name": "get_json_keys", "description": "List top-level keys of a JSON config file with types and previews",
     "params": {"file_path": "string"}},
    # Vision / runtime-state review actions. These already exist in
    # vision_actions.py and route through agent_executor — but until they
    # appeared here the agent didn't know they were callable. Use them when
    # a task description involves "review the running app" or "screenshot
    # the UI and check for X". Default permission can_screenshot=True allows
    # them; set provider permissions to disable.
    {"name": "launch_app", "description": "Spawn an app (e.g. python pong.py) so it can be screenshotted later",
     "params": {"command": "string", "wait_seconds": "int (optional, default 2)"}},
    {"name": "list_windows", "description": "List visible OS windows (title + handle) — use this to find the app you launched",
     "params": {}},
    {"name": "screenshot", "description": "Capture the whole screen to a PNG and return its path",
     "params": {"path": "string (optional output filename, defaults under .automatica/screenshots/)"}},
    {"name": "screenshot_window", "description": "Capture a specific window by title (substring match) to a PNG",
     "params": {"window_title": "string"}},
    {"name": "analyze_screenshot", "description": (
        "Send a screenshot through a local vision model and return a text "
        "description. Useful for 'does the game look right?' style review."),
     "params": {"image_path": "string", "prompt": "string (what to look for)", "model": "string (optional, default moondream)"}},
    {"name": "step_done", "description": "Signal task complete",
     "params": {"summary": "string", "files_changed": "string[] (optional)"}},
    {"name": "need_help", "description": "Escalate — stuck or need human review",
     "params": {"reason": "string", "escalate_to": "string (optional, provider name)"}},
]


# ---------------------------------------------------------------------------
# Core AgentProtocol class
# ---------------------------------------------------------------------------

class AgentProtocol:
    """Manages the request/response loop between Automatica and external AI agents."""

    def __init__(self, provider: str, provider_config: dict, project_dir: Path,
                 max_iterations: int = 50, log_dir: Optional[Path] = None,
                 project_db=None, global_db=None, confirm_before_done: bool = True,
                 broadcast_fn=None):
        """
        Initialize AgentProtocol.

        Args:
            provider: Provider type (e.g., 'Ollama', 'Claude', 'http_local_chat')
            provider_config: Config dict (model, endpoint, etc.)
            project_dir: Project root directory
            max_iterations: Max loop iterations per task
            log_dir: Optional directory for session logs
            project_db: Optional project DB connection
            global_db: Optional global DB connection
            confirm_before_done: Whether to run a confirmation round before accepting step_done
            broadcast_fn: Optional function to broadcast WS events (signature: fn(event_type, data))
        """
        self.provider = provider
        self.provider_config = provider_config or {}
        self.project_dir = Path(project_dir)
        self.max_iterations = max_iterations
        self.log_dir = Path(log_dir) if log_dir else None
        self._broadcast_fn = broadcast_fn

        # Load adapter
        self.adapter = get_adapter(provider, provider_config)

        # Change-driven summary indexer (docstrings, _folder_summary.md,
        # README.md auto-section). Hooked into agent_executor below so every
        # write_file / edit_file enqueues the path; SprintRunner drains the
        # queue at sprint end. Uses a cheap local model (default llama3.2:3b)
        # so summaries don't compete for the main agent's tokens.
        try:
            from summary_indexer import SummaryIndexer
            _summary_endpoint = (provider_config or {}).get("endpoint",
                                                             "http://localhost:11434")
            _summary_model = (provider_config or {}).get("summary_model",
                                                          "llama3.2:3b")
            self.summary_indexer = SummaryIndexer(
                project_dir=self.project_dir,
                endpoint=_summary_endpoint,
                model=_summary_model,
            )
        except Exception as exc:
            log.debug("summary indexer unavailable: %s", exc)
            self.summary_indexer = None  # type: ignore[assignment]

        # CRITICAL: pin the adapter's subprocess cwd to the project directory.
        # Without this the Claude CLI subprocess inherits Automatica's own cwd
        # and edits files in the wrong repository — every "file already exists"
        # / "file does not exist" claim from Claude was decided against the
        # WRONG working tree. Always force the agent to operate inside
        # *project_dir* so its tool calls see the target codebase.
        if hasattr(self.adapter, 'config') and isinstance(self.adapter.config, dict):
            self.adapter.config.setdefault('cwd', str(self.project_dir))

        # Agentic-mode adapters (e.g. Claude CLI in -p mode) do their own
        # tool work outside the executor, so the action_log is always empty.
        # Skip the confirm-before-done round for them — the synthesised
        # step_done IS the success signal.
        _is_agentic = (
            isinstance(self.provider_config, dict)
            and (self.provider_config.get('mode') or '').lower() == 'agentic'
        )
        self.confirm_before_done = confirm_before_done and not _is_agentic
        self._is_agentic = _is_agentic

        # Load permissions (from config or defaults)
        self.permissions = provider_config.get('permissions', DEFAULT_PERMISSIONS)

        # Session state
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.history: List[Dict] = []

        # Executor handles file I/O and commands
        if project_db or global_db:
            db_handler = DbActionHandler(project_db=project_db, global_db=global_db)
        else:
            db_handler = None

        vision_handler = None
        if _VISION_ACTIONS_AVAILABLE and _VisionActionHandler:
            try:
                vision_handler = _VisionActionHandler(project_dir=self.project_dir)
            except TypeError:
                vision_handler = _VisionActionHandler()

        view_handler = None
        if _VIEW_ACTIONS_AVAILABLE and _ViewActionHandler:
            view_handler = _ViewActionHandler(project_dir=self.project_dir)

        self.executor = AgentExecutor(
            project_dir=self.project_dir,
            permissions=self.permissions,
        )
        if db_handler:
            self.executor.set_db_handler(db_handler, session_id=self.session_id)
        if view_handler:
            self.executor.set_view_handler(view_handler)
        if self.summary_indexer is not None:
            self.executor.set_summary_indexer(self.summary_indexer)

        self.db_handler = db_handler

    def build_request(self, task: dict, context: dict,
                      instructions: dict, sequence: int) -> dict:
        """Build a request payload for the AI adapter."""
        # Apply action filter if configured (subset of allowed actions per provider)
        cfg = self.adapter.config if hasattr(self.adapter, 'config') else {}
        action_filter = cfg.get('action_filter')
        if action_filter:
            actions = [a for a in AVAILABLE_ACTIONS if a['name'] in action_filter]
        else:
            actions = AVAILABLE_ACTIONS

        # Cap history to avoid prompt bloat — configurable per provider
        max_history = cfg.get('max_history_items', 20)  # default 20 exchanges = 40 entries
        max_result_chars = cfg.get('max_result_chars', 600)  # truncate long action results

        def _trim_history_entry(entry: dict) -> dict:
            """Truncate long result messages so history doesn't balloon."""
            if entry.get('role') == 'system':
                content = entry.get('content', '')
                if len(content) > max_result_chars:
                    entry = {**entry, 'content': content[:max_result_chars] + ' …[truncated]'}
            return entry

        trimmed_history = [_trim_history_entry(e) for e in self.history[-(max_history * 2):]]

        request = {
            'protocol': 'automatica-agent/1.0',
            'session_id': self.session_id,
            'sequence': sequence,
            'task': task,
            'context': context,
            'instructions': instructions,
            'history': trimmed_history,
            'permissions': self.permissions,
            'available_actions': actions,
        }

        # Enrich with DB-backed context if available
        if self.db_handler and task.get('display_id'):
            try:
                from automatica_db import build_task_context
                db_context = build_task_context(self.db_handler.pdb, task['display_id'])
                if db_context and 'error' not in db_context:
                    request['task_context'] = db_context
            except Exception:
                pass  # DB context is best-effort; don't break the loop

        # S3-W08: inject prior agent handoff into system prompt and request
        prior_handoff = context.get('prior_handoff')
        if prior_handoff:
            try:
                handoff_str = json.dumps(prior_handoff) if not isinstance(prior_handoff, str) else prior_handoff
                request['prior_handoff'] = prior_handoff
                # Append to system prompt so the model sees it early
                existing_step_prompt = request.get('instructions', {}).get('step_prompt', '')
                handoff_note = f"\n\nPrior agent handoff: {handoff_str}"
                if 'instructions' in request:
                    request['instructions']['step_prompt'] = existing_step_prompt + handoff_note
                else:
                    request['instructions'] = {'step_prompt': handoff_note}
            except Exception:
                pass

        # Add wiki summary: top 25 files by recency so agents know the codebase layout
        try:
            _files = list_project_files(self.project_dir)
            if _files:
                # Sort by mtime descending (most recently changed first)
                _files.sort(key=lambda f: f.get('mtime', 0.0), reverse=True)
                request['wiki_summary'] = [
                    {k: f[k] for k in ('path', 'language', 'line_count', 'size_bytes') if k in f}
                    for f in _files[:25]
                ]
        except Exception:
            pass  # wiki summary is best-effort

        # Add task dependency chain (ancestors up to 5 levels)
        if self.db_handler and hasattr(self.db_handler, 'pdb') and self.db_handler.pdb:
            try:
                chain: list[dict] = []
                _did = task.get('display_id', '')
                _visited: set[str] = set()
                for _ in range(5):
                    if not _did or _did in _visited:
                        break
                    _visited.add(_did)
                    rows = self.db_handler.pdb.execute(
                        "SELECT t2.display_id, t2.title, t2.status "
                        "FROM tasks t2 JOIN tasks t1 ON t1.depends_on_task_id = t2.id "
                        "WHERE t1.display_id = ?", (_did,)
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        chain.append({'display_id': row[0], 'title': row[1], 'status': row[2]})
                    _did = rows[0][0]
                if chain:
                    request['task_dependency_chain'] = chain
            except Exception:
                pass

        return request

    def _do_read_file(self, file_path: str) -> str:
        """Read a file relative to project_dir. Returns content string or error."""
        try:
            p = self.project_dir / file_path
            if not p.is_file():
                # Try as absolute path
                p2 = Path(file_path)
                if p2.is_file():
                    return p2.read_text(encoding='utf-8', errors='replace')[:8000]
                return f"[File not found: {file_path}]"
            return p.read_text(encoding='utf-8', errors='replace')[:8000]
        except Exception as exc:
            return f"[Error reading {file_path}: {exc}]"

    def _planning_quality_gate(self, task: dict, context: dict, instructions: dict) -> Optional[dict]:
        """S3-W01: After initial planning pass, validate the plan completeness.

        Sends a second model call asking whether the plan is complete and specific
        enough to execute. If not, reads up to 3 mentioned files and appends them
        to history so the agent has the context it needs before entering the main loop.
        
        Returns the evaluation result dict or None on error.
        """
        try:
            from planning_quality_gate import (
                evaluate_plan_completeness,
                perform_clarification_round,
            )
            
            # Evaluate plan completeness
            evaluation = evaluate_plan_completeness(
                adapter=self.adapter,
                task=task,
                context=context,
                instructions=instructions,
                history=self.history,
                permissions=self.permissions,
                session_id=self.session_id,
                project_dir=self.project_dir,
            )
            
            # Log evaluation result
            if self.log_dir:
                self._write_log(-1, 'planning_evaluation', evaluation)
            
            # If plan is incomplete, perform clarification round
            if not evaluation.get('is_complete', True):
                files_read = perform_clarification_round(
                    evaluation=evaluation,
                    project_dir=self.project_dir,
                    history=self.history,
                )
                
                # Log clarification round
                if self.log_dir and files_read:
                    self._write_log(-1, 'planning_clarification', {
                        'files_read': files_read,
                        'missing_items': evaluation.get('missing_items', []),
                    })
            
            return evaluation
        except Exception as exc:
            # Planning gate failures must never crash execution
            if self.log_dir:
                try:
                    self._write_log(-1, 'planning_gate_error', {'error': str(exc)})
                except Exception:
                    pass
            return None

    def execute_loop(self, task: dict, context: dict,
                     instructions: dict) -> dict:
        """Main loop: build request -> send to AI -> execute action -> repeat until step_done."""

        start_time = time.time()
        # Track every successful action outside of history so it survives history trimming.
        # Injected into every request as task['_completed'] so the model doesn't repeat work.
        completed_steps: list = []
        # Track consecutive failures of the same action+path to detect stuck loops
        _last_fail_key: str = ""
        _fail_streak: int = 0

        # S3-W01: planning quality gate — validate plan completeness before main loop
        planning_result = self._planning_quality_gate(task, context, instructions)

        # S5-W08: emit task_planning WS event after planning pass
        if self._broadcast_fn:
            try:
                # Build plan JSON from evaluation result or task description
                plan_json = {
                    'task_id': task.get('display_id'),
                    'title': task.get('title', ''),
                    'description': task.get('description', ''),
                    'acceptance_criteria': task.get('acceptance_criteria', []),
                }
                
                # Include planning evaluation result if available
                if planning_result:
                    plan_json['planning_evaluation'] = {
                        'is_complete': planning_result.get('is_complete', True),
                        'missing_items': planning_result.get('missing_items', []),
                        'reasoning': planning_result.get('reasoning', ''),
                    }
                
                # Include wiki summary if present in context
                if 'wiki_summary' in context:
                    plan_json['wiki_summary_count'] = len(context.get('wiki_summary', []))
                
                self._broadcast_fn('task_planning', plan_json)
            except Exception:
                pass

        for i in range(self.max_iterations):
            # Inject iteration budget + completed steps manifest into task
            remaining = self.max_iterations - i
            task_with_budget = dict(task)
            # Completed steps survive history trimming — model always knows what's done
            if completed_steps:
                task_with_budget['_completed'] = (
                    "ALREADY DONE (do NOT repeat these): " + "; ".join(completed_steps)
                )
            if remaining <= 5:
                task_with_budget['_budget_warning'] = (
                    f"WARNING: Only {remaining} iteration(s) left. "
                    f"If acceptance criteria are met, call step_done NOW. "
                    f"If stuck, call need_help NOW."
                )
            elif i == 0:
                task_with_budget['_budget_note'] = (
                    f"You have {remaining} iterations total. "
                    f"Call step_done as soon as all acceptance criteria are satisfied."
                )

            # Build and send request
            request = self.build_request(task_with_budget, context, instructions, i)

            # S3-W02: configurable loop delay (default 0.5s)
            cfg = self.adapter.config if hasattr(self.adapter, 'config') else {}
            delay = cfg.get('loop_delay_s', 0.5)

            if self.log_dir:
                # Strip history + available_actions from the log (too verbose)
                log_req = {k: v for k, v in request.items()
                           if k not in ('history', 'available_actions', 'permissions')}
                log_req['history_count'] = len(request.get('history', []))
                log_req['actions_count'] = len(request.get('available_actions', []))
                self._write_log(i, 'request', log_req)

            try:
                response = self.adapter.send(request)
            except Exception as exc:
                return {
                    'success': False,
                    'reason': f'Adapter error: {exc}',
                    'iterations': i + 1,
                    'duration': time.time() - start_time,
                    'action_log': self.executor.get_log(),
                }

            if self.log_dir:
                self._write_log(i, 'response', response)

            # Normalise to a list. Single-action shape -> 1-element list;
            # multi-action batch -> the response['actions'] list. Same loop
            # body handles both.
            batch = response.get('actions')
            if isinstance(batch, list) and batch:
                actions_iter = batch
            else:
                actions_iter = [{
                    'action': response.get('action', 'need_help'),
                    'params': response.get('params', {}) or {},
                    'reasoning': response.get('reasoning', ''),
                }]

            # Append the response (whatever shape) to history once.
            self.history.append({'role': 'assistant', 'content': json.dumps(response)})

            from validation import validate_action
            terminal: Optional[dict] = None
            stop_batch = False

            for sub_idx, sub in enumerate(actions_iter):
                action = sub.get('action', 'need_help')
                params = sub.get('params', {}) or {}
                _is_batch = len(actions_iter) > 1
                _prefix = f"batch[{sub_idx}] " if _is_batch else ""

                if action == 'step_done':
                    # In legacy mode we still honour confirm_before_done.
                    if self.confirm_before_done:
                        confirm_result = self._confirm_done(task, request, sub, i)
                        if confirm_result == 'retry':
                            stop_batch = True
                            break
                    terminal = {
                        'success': True,
                        'summary': params.get('summary', 'Task completed'),
                        'files_changed': params.get('files_changed', []),
                        'iterations': i + 1,
                        'duration': time.time() - start_time,
                        'action_log': self.executor.get_log(),
                    }
                    break

                if action == 'need_help':
                    if (self.db_handler and hasattr(self.db_handler, 'pdb')
                            and self.db_handler.pdb):
                        try:
                            from automatica_db import log_escalation
                            log_escalation(
                                self.db_handler.pdb,
                                task.get('display_id', ''),
                                self.session_id,
                                i,
                                params.get('reason', 'need_help'),
                                json.dumps(request)[:5000],
                            )
                        except Exception:
                            pass
                    terminal = {
                        'success': False,
                        'reason': params.get('reason', 'Agent requested help'),
                        'iterations': i + 1,
                        'duration': time.time() - start_time,
                        'action_log': self.executor.get_log(),
                    }
                    break

                _schema_result = validate_action(action, params)
                if _schema_result and _schema_result.startswith("Unknown action"):
                    validation = self._validate_params(action, params)
                else:
                    validation = _schema_result
                if validation:
                    self.history.append({
                        'role': 'system',
                        'content': f"{_prefix}PARAM ERROR ({action}): {validation}"
                    })
                    stop_batch = True
                    break

                try:
                    result = self.executor.execute({
                        'action': action,
                        'params': params,
                        'reasoning': sub.get('reasoning', ''),
                    })
                except Exception as _exec_exc:
                    import traceback as _tb
                    _tb.print_exc()
                    result = {"success": False,
                              "message": f"EXECUTOR ERROR: {_exec_exc}"}

                if not result.get('success', False):
                    fail_key = f"{action}:{params.get('path', params.get('pattern', ''))}"
                    if fail_key == _last_fail_key:
                        _fail_streak += 1
                    else:
                        _fail_streak = 1
                        _last_fail_key = fail_key

                    if _fail_streak >= 3:
                        self.history.append({'role': 'system', 'content': (
                            f"STUCK LOOP DETECTED: {action} on {fail_key} failed "
                            f"{_fail_streak} times in a row. You must either: "
                            f"(1) try a different approach, (2) call need_help, "
                            f"or (3) if the task is actually done, call step_done."
                        )})
                        stop_batch = True
                        break
                    self.history.append({
                        'role': 'system',
                        'content': (
                            f"{_prefix}{result.get('message','')}\n"
                            + (f"(batch aborted; remaining {len(actions_iter)-sub_idx-1} "
                               f"action(s) skipped — fix and retry next turn)"
                               if _is_batch else "")
                        )
                    })
                    stop_batch = True
                    break

                self.history.append({'role': 'system',
                                       'content': _prefix + result.get('message', '')})
                if action in ('write_file', 'edit_file', 'run_command', 'commit'):
                    summary = f"{action}({params.get('path', params.get('command', ''))})"
                    completed_steps.append(summary)

            if terminal is not None:
                return terminal
            time.sleep(delay)
            if stop_batch:
                continue

        # Max iterations reached
        return {
            'success': False,
            'reason': f'Max iterations ({self.max_iterations}) reached',
            'iterations': self.max_iterations,
            'duration': time.time() - start_time,
            'action_log': self.executor.get_log(),
        }

    def _confirm_done(self, task: dict, request: dict, done_response: dict, iteration: int) -> str:
        """S3-W02: Confirmation round before accepting step_done.

        Returns 'confirmed' if all acceptance criteria are met,
                'retry' if issues found,
                'error' on adapter/parsing failures (fail-open: accept step_done anyway).
        """
        # Short-circuit if no actions taken (agent called step_done on iteration 0 with no work)
        if len(self.executor.action_log) == 0:
            self.history.append({
                'role': 'system',
                'content': (
                    "No actions were taken — please do the task before calling step_done. "
                    "Read files, write code, or run tests as needed."
                ),
            })
            return 'retry'

        summary = done_response.get('params', {}).get('summary', '')
        files_changed = done_response.get('params', {}).get('files_changed', [])

        confirm_prompt = {
            **request,
            'sequence': iteration + 0.5,  # sentinel for logs
            'instructions': {
                'step_prompt': (
                    f"You just called step_done with summary: {summary!r}\n"
                    f"Files changed: {files_changed}\n\n"
                    f"CONFIRM: Are ALL acceptance criteria truly satisfied?\n"
                    f"Acceptance criteria:\n" +
                    _format_criteria_bullets(task.get('acceptance_criteria')) +
                    "\n\nIf YES (all criteria met): respond with {\"action\": \"step_done\", ...}\n"
                    "If NO (something is incomplete or wrong): respond with the action you "
                    "need to do next to fix it."
                ),
            },
            'available_actions': request.get('available_actions', AVAILABLE_ACTIONS),
        }

        try:
            confirm_response = self.adapter.send(confirm_prompt)
        except Exception as _exc:
            # Adapter error during confirm — fail open (accept the original step_done)
            return 'error'

        confirm_action = confirm_response.get('action', 'need_help')
        if confirm_action == 'step_done':
            # Confirmation passed
            self.history.append({'role': 'system', 'content': '[Confirmation: step_done accepted]'})
            return 'confirmed'
        else:
            # Agent found issues — retry with the corrective action
            self.history.append({
                'role': 'system',
                'content': (
                    f"[Confirmation failed] Agent found issues during step_done confirmation: "
                    f"next action is {confirm_action}. Continuing execution to fix."
                ),
            })
            return 'retry'

    def execute_loop_atomic(self, task: dict, context: dict,
                            instructions: dict) -> dict:
        """Atomic step execution: parse numbered steps from task description and
        run each as a single-action micro-loop (max 3 iterations per step).

        This sidesteps the small-model state-tracking problem entirely — the model
        only ever sees ONE required action at a time and calls step_done after it.
        Falls back to execute_loop() if the description has no numbered steps.
        """
        description = task.get('description', '')
        steps = self._parse_numbered_steps(description)

        # S3-W08: load prior agent handoff from DB if not already in context
        if 'prior_handoff' not in context and self.db_handler:
            try:
                _did = task.get('display_id', '')
                if _did and hasattr(self.db_handler, 'pdb') and self.db_handler.pdb:
                    _row = self.db_handler.pdb.execute(
                        "SELECT handoff_json FROM tasks WHERE display_id = ? AND handoff_json != '' LIMIT 1",
                        (_did,)
                    ).fetchone()
                    if _row and _row[0]:
                        try:
                            context['prior_handoff'] = json.loads(_row[0])
                        except Exception:
                            context['prior_handoff'] = _row[0]
            except Exception:
                pass

        # S5-W08: emit task_planning WS event before atomic execution
        if self._broadcast_fn:
            try:
                plan_json = {
                    'task_id': task.get('display_id'),
                    'title': task.get('title', ''),
                    'description': task.get('description', ''),
                    'acceptance_criteria': task.get('acceptance_criteria', []),
                    'atomic_steps': len(steps) if steps else 0,
                }
                if 'wiki_summary' in context:
                    plan_json['wiki_summary_count'] = len(context.get('wiki_summary', []))
                
                self._broadcast_fn('task_planning', plan_json)
            except Exception:
                pass

        # Fall back to regular loop if task has no numbered steps
        if not steps:
            return self.execute_loop(task, context, instructions)

        start_time = time.time()
        total_iterations = 0
        all_files_changed: list = []
        all_steps_ok = True

        for step_num, step_text in enumerate(steps, 1):
            # Build a minimal single-step task — model only sees THIS one action
            step_task = dict(task)
            step_task['description'] = (
                f"SINGLE STEP TASK — do exactly this ONE action, then call step_done immediately.\n\n"
                f"Step {step_num} of {len(steps)}: {step_text}"
            )
            # Keep acceptance_criteria simple — DO NOT echo the step_text here because
            # small models confuse the echoed instruction with file content to write.
            step_task['acceptance_criteria'] = f"Step {step_num} of {len(steps)} is complete."
            step_task['_step_num'] = f"{step_num}/{len(steps)}"

            step_result = self._run_single_step(step_task, context, instructions, step_num)
            total_iterations += step_result.get('iterations', 1)
            all_files_changed.extend(step_result.get('files_changed', []))

            if not step_result.get('success', False):
                reason = step_result.get('reason', 'Step failed')
                all_steps_ok = False
                return {
                    'success': False,
                    'reason': f"Step {step_num}/{len(steps)} failed: {reason}",
                    'iterations': total_iterations,
                    'duration': time.time() - start_time,
                    'action_log': self.executor.get_log(),
                }

        return {
            'success': True,
            'summary': f"Completed all {len(steps)} steps: " + "; ".join(steps[:3]),
            'files_changed': list(set(all_files_changed)),
            'iterations': total_iterations,
            'duration': time.time() - start_time,
            'action_log': self.executor.get_log(),
        }

    def _run_single_step(self, step_task: dict, context: dict,
                          instructions: dict, step_num: int,
                          max_step_iters: Optional[int] = None) -> dict:
        """Run a single micro-step with up to max_step_iters attempts.

        Default is 8: smaller local models routinely use 3 iterations just
        exploring (list_files -> read_file -> read_another) before they're
        ready to edit; once they start editing they often loop edit_file on
        the same file 4–5 times instead of calling step_done. The combined
        budget plus the post-success nudge below keeps them from hitting the
        cap without progress. Override per-provider via
        provider_config['max_step_iters']."""
        if max_step_iters is None:
            cfg = getattr(self.adapter, 'config', {}) or {}
            max_step_iters = int(cfg.get('max_step_iters', 8))
        successful_actions = 0  # actions in this step whose executor result was success
        productive_actions = 0  # write/edit/run_command — concrete state-changing work
        start = time.time()
        _last_fail_key = ""
        _fail_streak = 0
        _PRODUCTIVE = {"write_file", "edit_file", "run_command", "run_script",
                       "edit_section", "create_section", "replace_section",
                       "insert_line", "remove_section", "rename_symbol"}

        for i in range(max_step_iters):
            remaining = max_step_iters - i
            t = dict(step_task)
            # Escalating nudges to call step_done. Local models routinely loop
            # edit_file 4-5x on the same file rather than declaring the step
            # done — this hand-holds them out of the loop. Triggers once 2+
            # tool calls have already succeeded for this step.
            if remaining == 1:
                t['_budget_warning'] = (
                    "LAST ATTEMPT. If the step is done, call step_done NOW. "
                    "Otherwise need_help."
                )
            elif successful_actions >= 2:
                t['_budget_warning'] = (
                    f"You have already completed {successful_actions} successful "
                    f"action(s) this step. If the step's goal is achieved, call "
                    f"step_done IMMEDIATELY — do not run more reads or edits. "
                    f"Only continue if a concrete part of the step is still missing."
                )

            request = self.build_request(t, context, instructions, i)
            if self.log_dir:
                log_req = {k: v for k, v in request.items()
                           if k not in ('history', 'available_actions', 'permissions')}
                log_req['history_count'] = len(request.get('history', []))
                log_req['actions_count'] = len(request.get('available_actions', []))
                self._write_log(i, f'step{step_num}_request', log_req)

            try:
                response = self.adapter.send(request)
            except Exception as exc:
                return {'success': False, 'reason': f'Adapter error: {exc}', 'iterations': i + 1}

            if self.log_dir:
                self._write_log(i, f'step{step_num}_response', response)

            # Batch shape: response['actions'] is a list. Otherwise single
            # action via response['action']/['params']. Normalise to a list
            # so the inner loop only deals with one shape.
            batch = response.get('actions')
            if isinstance(batch, list) and batch:
                actions_iter = batch
            else:
                actions_iter = [{
                    'action': response.get('action', 'need_help'),
                    'params': response.get('params', {}) or {},
                    'reasoning': response.get('reasoning', ''),
                }]

            # Execute each action in the batch in order. step_done / need_help
            # are terminal — emit them and bail. Any failed action also bails
            # so the model gets the error context next turn rather than its
            # later batch entries silently being skipped.
            self.history.append({'role': 'assistant', 'content': json.dumps(response)})

            from validation import validate_action
            terminal_result: Optional[dict] = None
            batch_failed = False
            for sub_idx, sub in enumerate(actions_iter):
                action = sub.get('action', 'need_help')
                params = sub.get('params', {}) or {}

                if action == 'step_done':
                    terminal_result = {
                        'success': True,
                        'summary': params.get('summary', f'step {step_num} done'),
                        'files_changed': params.get('files_changed', []),
                        'iterations': i + 1,
                        'duration': time.time() - start,
                    }
                    break
                if action == 'need_help':
                    terminal_result = {
                        'success': False,
                        'reason': params.get('reason', 'need_help'),
                        'iterations': i + 1,
                        'duration': time.time() - start,
                    }
                    break

                _schema_result = validate_action(action, params)
                if _schema_result and _schema_result.startswith("Unknown action"):
                    validation = self._validate_params(action, params)
                else:
                    validation = _schema_result
                if validation:
                    self.history.append({
                        'role': 'system',
                        'content': (f"PARAM ERROR (batch[{sub_idx}] {action}): "
                                    f"{validation}")
                    })
                    batch_failed = True
                    break

                # Execute the single action via the standard executor path.
                try:
                    result = self.executor.execute({
                        'action': action,
                        'params': params,
                        'reasoning': sub.get('reasoning', ''),
                    })
                except Exception as _exec_exc:
                    import traceback as _tb
                    _tb.print_exc()
                    result = {"success": False,
                              "message": f"EXECUTOR ERROR: {_exec_exc}"}

                # Track success / failure streaks across the WHOLE step,
                # not just this iteration — the implicit-step-done fallback
                # downstream looks at these.
                if not result.get('success', False):
                    fail_key = f"{action}:{params.get('path', params.get('pattern', ''))}"
                    if fail_key == _last_fail_key:
                        _fail_streak += 1
                    else:
                        _fail_streak = 1
                        _last_fail_key = fail_key
                    self.history.append({
                        'role': 'system',
                        'content': (f"batch[{sub_idx}] {result.get('message','')}\n"
                                    f"(batch aborted; remaining {len(actions_iter)-sub_idx-1} "
                                    f"action(s) skipped — fix and retry next turn)")
                    })
                    batch_failed = True
                    break
                successful_actions += 1
                if action in _PRODUCTIVE:
                    productive_actions += 1
                _fail_streak = 0
                _last_fail_key = ""
                self.history.append({
                    'role': 'system',
                    'content': (f"batch[{sub_idx}] " if len(actions_iter) > 1 else "")
                              + result.get('message', '')
                })

            if terminal_result is not None:
                return terminal_result
            time.sleep(0.3)
            if batch_failed:
                continue  # next outer iteration; model sees the error

        # Implicit success: budget exhausted but the agent did real work.
        # Two acceptance paths so different step shapes are covered:
        #   (a) >=1 productive action (write/edit/run_command) — concrete
        #       state-change happened.
        #   (b) >=4 successful actions of any kind — agent spent budget
        #       exploring/reading but the step's intent may have been
        #       investigative; trust progress over a strict "must edit" rule.
        # Both paths require fail_streak<=1 so we don't reward thrashing.
        _eligible = (productive_actions >= 1 or successful_actions >= 4) \
                    and _fail_streak <= 1
        if _eligible:
            _log_msg = (
                f'[implicit-step-done] step {step_num} budget exhausted '
                f'after {productive_actions} productive / {successful_actions} '
                f'total successful actions; treating as complete'
            )
            print(_log_msg, flush=True)
            return {
                'success': True,
                'summary': _log_msg,
                'files_changed': [],
                'iterations': max_step_iters,
                'duration': time.time() - start,
            }

        return {
            'success': False,
            'reason': (
                f'Max {max_step_iters} attempts for step {step_num} '
                f'(productive_actions={productive_actions}, '
                f'successful_actions={successful_actions}, '
                f'fail_streak={_fail_streak})'
            ),
            'iterations': max_step_iters,
            'duration': time.time() - start,
        }

    def _parse_numbered_steps(self, description: str) -> List[str]:
        """Extract numbered steps from description (1., 2., etc.) or return empty list."""
        lines = description.splitlines()
        steps: List[str] = []
        for line in lines:
            m = re.match(r'^\s*(\d+)\.\s+(.+)$', line.strip())
            if m:
                steps.append(m.group(2))
        return steps

    def _validate_params(self, action: str, params: dict) -> str:
        """Legacy param validation (fallback when validation.py has no schema)."""
        # Basic checks
        if action == 'read_file' and not params.get('path'):
            return 'read_file requires path'
        if action in ('write_file', 'edit_file') and not params.get('path'):
            return f'{action} requires path'
        if action == 'edit_file' and (not params.get('old_text') or not params.get('new_text')):
            return 'edit_file requires old_text and new_text'
        if action == 'search' and not params.get('pattern'):
            return 'search requires pattern'
        if action == 'list_files' and not params.get('pattern'):
            return 'list_files requires pattern'
        if action == 'run_command' and not params.get('command'):
            return 'run_command requires command'
        if action == 'commit' and not params.get('message'):
            return 'commit requires message'
        return ''

    def _write_log(self, iteration: int, event: str, data: Any):
        """Write a JSONL log entry."""
        if not self.log_dir:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.log_dir / f'session_{self.session_id}.jsonl'
            with log_file.open('a', encoding='utf-8') as f:
                entry = {
                    'timestamp': datetime.now().isoformat(),
                    'iteration': iteration,
                    'event': event,
                    'data': data,
                }
                f.write(json.dumps(entry) + '\n')
        except Exception:
            pass  # Log write failures must never crash execution


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def load_context(project_dir: Path) -> dict:
    """Load project context (README, wiki, settings)."""
    context = {}
    readme = project_dir / 'README.md'
    if readme.exists():
        try:
            context['readme'] = readme.read_text(encoding='utf-8', errors='replace')[:2000]
        except Exception:
            pass
    return context


def load_instructions(config: dict, step: int, agent_role: str,
                      project_dir: Path, standards_dir: Optional[Path] = None,
                      agents_dir: Optional[Path] = None,
                      provider_config: Optional[dict] = None) -> dict:
    """Load step prompt + agent prompt from config."""
    steps_cfg = config.get('steps', [])
    step_cfg = next((s for s in steps_cfg if isinstance(s, dict) and s.get('id') == step), {})
    step_prompt = step_cfg.get('prompt', '')

    # Load agent-specific prompt
    agent_prompt = ''
    if agents_dir:
        agent_file = agents_dir / f'{agent_role.lower().replace(" ", "_")}.md'
        if agent_file.exists():
            try:
                agent_prompt = agent_file.read_text(encoding='utf-8', errors='replace')[:3000]
            except Exception:
                pass

    # Load standards if available
    standards_text = ''
    if standards_dir and standards_dir.exists():
        try:
            for std_file in standards_dir.glob('*.md'):
                standards_text += f'\n\n## {std_file.stem}\n'
                standards_text += std_file.read_text(encoding='utf-8', errors='replace')[:2000]
        except Exception:
            pass

    return {
        'step_id': step,
        'step_prompt': step_prompt,
        'agent_role': agent_role,
        'agent_prompt': agent_prompt,
        'standards': standards_text,
    }


def load_settings(project_dir: Path) -> dict:
    """Load project settings.json if it exists."""
    settings_file = project_dir / '.automatica' / 'settings.json'
    if settings_file.exists():
        try:
            return json.loads(settings_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def get_provider_config(global_db, provider_name: str) -> dict:
    """Fetch provider config from global DB, merging config_json fields."""
    try:
        row = global_db.execute(
            "SELECT provider_type, base_url, model_name, config_json FROM providers WHERE name = ?",
            (provider_name,)
        ).fetchone()
        if row:
            base = {
                'provider_type': row['provider_type'],
                'endpoint': row['base_url'] or '',
                'model': row['model_name'] or '',
            }
            # Merge provider-specific keys from config_json (e.g. executable, args, timeout)
            import json as _json
            try:
                extra = _json.loads(row['config_json']) if row['config_json'] else {}
                if isinstance(extra, dict):
                    base.update(extra)
            except Exception:
                pass
            return base
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Automatica Agent Protocol — run a single agent task')
    parser.add_argument('--provider', required=True, help='Provider name (Ollama, Claude, etc.)')
    parser.add_argument('--project-dir', required=True, help='Project directory')
    parser.add_argument('--task-file', required=True, help='Task description markdown file')
    parser.add_argument('--step', type=int, default=8, help='Pipeline step ID')
    parser.add_argument('--config', default='config/pipeline-config.json', help='Pipeline config file')
    parser.add_argument('--max-iterations', type=int, default=50, help='Max iterations per task')
    parser.add_argument('--log-dir', help='Optional log directory')
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    task_file = Path(args.task_file).resolve()

    if not project_dir.exists():
        print(f"[ERROR] Project directory not found: {project_dir}")
        sys.exit(1)
    if not task_file.exists():
        print(f"[ERROR] Task file not found: {task_file}")
        sys.exit(1)

    # Load task
    task_text = task_file.read_text(encoding='utf-8')
    task = {
        'title': task_file.stem,
        'description': task_text,
        'acceptance_criteria': ['Task described in file is completed'],
    }

    # Load config
    config_file = Path(args.config)
    if config_file.exists():
        config = json.loads(config_file.read_text(encoding='utf-8'))
    else:
        config = {'steps': []}

    # Build provider config (basic Ollama example)
    provider_config = {
        'endpoint': 'http://localhost:11434',
        'model': 'gemma3:4b',
    }

    # Load context and instructions
    context = load_context(project_dir)
    instructions = load_instructions(
        config, args.step, 'Developer', project_dir,
        provider_config=provider_config,
    )

    # Create protocol
    log_dir = Path(args.log_dir) if args.log_dir else None
    protocol = AgentProtocol(
        provider=args.provider,
        provider_config=provider_config,
        project_dir=project_dir,
        max_iterations=args.max_iterations,
        log_dir=log_dir,
    )

    # Run
    print(f"[AgentProtocol] Running task: {task['title']}")
    result = protocol.execute_loop(task, context, instructions)

    print(f"\n[Result] Success: {result['success']}")
    print(f"  Iterations: {result['iterations']}")
    print(f"  Duration: {result['duration']:.1f}s")
    if result['success']:
        print(f"  Summary: {result.get('summary', '')}")
        print(f"  Files changed: {result.get('files_changed', [])}")
    else:
        print(f"  Reason: {result.get('reason', '')}")

    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()
