#!/usr/bin/env python3
"""
Sprint Runner — Executes an entire sprint: planning, parallel task execution, review.

Orchestrates the full sprint lifecycle:
  1. Planning  — select tasks from backlog, create sprint, assign tasks
  2. Execution — run tasks through AgentProtocol (parallel where possible)
  3. Review    — summarize results, record velocity, log ceremonies

Usage:
    python sprint_runner.py --project-dir H:\\Github\\Nova3D --sprint-number 12 \\
        --goal "Implement physics constraints" --provider Ollama --max-parallel 3

    # Or as a library:
    from sprint_runner import SprintRunner
    runner = SprintRunner(project_dir, provider, provider_config)
    result = runner.run_sprint(sprint_number=12, goal="...", max_points=40)
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from automatica_db import (
    init_project_db,
    init_global_db,
    create_sprint,
    assign_task_to_sprint,
    update_task,
    log_action,
    build_task_context,
    get_backlog,
    get_sprint_tasks,
    build_sprint_summary,
    create_task,
    init_connection,
    get_pipeline_steps,
)
from agent_protocol import (
    AgentProtocol,
    load_context,
    load_instructions,
    load_settings,
    get_provider_config,
)


# ---------------------------------------------------------------------------
# S5-W08: Safe push_event accessor (no Kivy dependency)
# ---------------------------------------------------------------------------

def _get_push_event_fn():
    """Return app.state.push_event if the API server is running, else None."""
    try:
        from api_server import _PUSH_EVENT_FN
        return _PUSH_EVENT_FN
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Rate-limit sentinel (written by adapters/subprocess_cli.py when the Claude
# CLI reports "You've hit your limit"). When fresh, abort sprints cleanly so
# we don't burn complexity bumps on what is really a "come back later" state.
# ---------------------------------------------------------------------------

RATE_LIMIT_COOLDOWN_SEC = 30 * 60  # minimum floor — actual cooldown extends
                                   # to the parsed reset time when available


def _parse_reset_at_to_dt(reset_at_text: str, now: datetime) -> Optional[datetime]:
    """Parse Claude CLI's 'resets X' text into the next wall-clock occurrence.

    Examples accepted: '7:50am', '12:30pm', '9am', '2:30 PM'. Returns the next
    future datetime matching that h:mm am/pm. Returns None if unparseable."""
    if not reset_at_text:
        return None
    import re as _re
    m = _re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*",
                  reset_at_text.strip(), _re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    is_pm = m.group(3).lower() == 'p'
    if hour == 12:
        hour = 12 if is_pm else 0
    elif is_pm:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        # Already past today — assume tomorrow.
        from datetime import timedelta as _td
        candidate = candidate + _td(days=1)
    return candidate


def _check_rate_limit_sentinel() -> Optional[str]:
    """If the rate-limit sentinel says we should still be cooling off, return
    a human-readable skip reason; else None.

    Cooldown extends to the parsed reset time (from Claude's reset-text) when
    available, with a 30-min floor. This prevents a one-shot run during the
    rate-limit window from re-attempting and writing a fresh sentinel just
    after the previous cooldown expires — which produced the 4-hour rate-limit
    period observed earlier in this session."""
    base = Path(os.environ.get("AUTOMATICA_HOME") or
                Path(__file__).resolve().parent)
    path = base / ".automatica" / "rate_limited.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = data.get("last_detected_at") or ""
        if not last:
            return None
        last_dt = datetime.fromisoformat(last)
        now = datetime.now()
        age = (now - last_dt).total_seconds()
        reset_text = data.get("reset_at_text") or ""

        # Compute deadline as max(last_detected + 30min floor, parsed reset).
        floor_deadline = last_dt.timestamp() + RATE_LIMIT_COOLDOWN_SEC
        parsed_reset = _parse_reset_at_to_dt(reset_text, last_dt)
        parsed_ts = parsed_reset.timestamp() if parsed_reset else 0
        deadline_ts = max(floor_deadline, parsed_ts)
        secs_left = deadline_ts - now.timestamp()

        if secs_left > 0:
            mins_left = int(secs_left / 60)
            hits = data.get("hit_count") or 0
            return (f"rate-limited (resets {reset_text or 'unknown'}, "
                    f"{hits} hit(s), ~{mins_left}m remaining; clear "
                    f"{path} to override)")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# S3-W05: Provider health check (module-level)
# ---------------------------------------------------------------------------

def _check_provider_health(provider_config: dict) -> tuple:
    """Check if the configured provider is reachable. Returns (bool, latency_ms).

    For subprocess_cli providers, checks that the executable exists.
    For HTTP providers, pings the endpoint (up to 3 attempts).
    Returns (True, latency_ms) on success, (False, -1) on failure.
    """
    provider_type = (provider_config or {}).get('provider_type', '')

    # Bug fix: an empty provider_type means the provider isn't configured
    # (e.g. the CLI was invoked with --provider Claude-Opus but no row by that
    # name exists in the global DB, so get_provider_config returned {}).
    # Previously this fell through to the HTTP path and probed
    # http://localhost:11434/api/tags, which an unrelated local Ollama
    # instance would happily answer — masking the real config error as a
    # 5ms "Provider health OK" line. Refuse to call it healthy.
    if not provider_type:
        return False, -1

    # subprocess_cli: verify the executable is on PATH or filesystem
    if provider_type == 'subprocess_cli':
        import shutil as _shutil
        from pathlib import Path as _Path
        executable = (provider_config.get('executable') or '').strip()
        if not executable:
            return False, -1
        t0 = time.monotonic()
        found = _shutil.which(executable) or _Path(executable).is_file()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return (True, latency_ms) if found else (False, -1)

    # HTTP-based providers: ping the endpoint
    try:
        import requests as _requests
    except ImportError:
        return False, -1

    url = provider_config.get('url', '') or provider_config.get('endpoint', 'http://localhost:11434')
    url = url.rstrip('/')
    for attempt in range(3):
        try:
            t0 = time.monotonic()
            r = _requests.get(f'{url}/api/tags', timeout=5)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return True, latency_ms
        except Exception:
            pass
        if attempt < 2:
            time.sleep(5)
    return False, -1


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    """Result from executing a single task."""
    task_display_id: str
    title: str
    success: bool
    summary: str = ""
    files_changed: List[str] = field(default_factory=list)
    iterations: int = 0
    duration: float = 0.0
    error: str = ""


@dataclass
class SprintResult:
    """Aggregate result of an entire sprint run."""
    sprint_number: int
    goal: str
    status: str = "completed"
    tasks_planned: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_blocked: int = 0
    points_planned: int = 0
    points_completed: int = 0
    total_duration: float = 0.0
    task_results: List[TaskResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def velocity(self) -> float:
        return self.points_completed / max(self.total_duration / 3600, 0.01)

    def to_dict(self) -> dict:
        return {
            "sprint_number": self.sprint_number,
            "goal": self.goal,
            "status": self.status,
            "tasks_planned": self.tasks_planned,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_blocked": self.tasks_blocked,
            "points_planned": self.points_planned,
            "points_completed": self.points_completed,
            "velocity": round(self.velocity, 2),
            "total_duration_s": round(self.total_duration, 1),
            "task_results": [
                {
                    "task_display_id": r.task_display_id,
                    "title": r.title,
                    "success": r.success,
                    "summary": r.summary,
                    "files_changed": r.files_changed,
                    "iterations": r.iterations,
                    "duration": round(r.duration, 1),
                    "error": r.error,
                }
                for r in self.task_results
            ],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Sprint Runner
# ---------------------------------------------------------------------------

class SprintRunner:
    """Orchestrates a full sprint: plan, execute tasks, review."""

    def __init__(
        self,
        project_dir: Path,
        provider: str,
        provider_config: dict,
        max_parallel: int = 3,
        max_iterations_per_task: int = 50,
        log_dir: Path = None,
        pipeline_config: dict = None,
        standards_dir: Path = None,
        agents_dir: Path = None,
        global_db_path: Path = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.provider = provider
        self.provider_config = provider_config
        self.max_parallel = max(1, max_parallel)

        # Agentic CLIs (Claude in -p mode) do their own direct file I/O
        # without going through the protocol's lock_file action, so the
        # advisory file_locks table can't protect against parallel agents
        # racing on the same OS file. Force max_parallel=1 in this mode.
        if (provider_config or {}).get('mode', '').lower() == 'agentic' and self.max_parallel > 1:
            _log(f"[GUARD] provider_config.mode=agentic — forcing max_parallel "
                 f"from {self.max_parallel} to 1 (agentic CLI bypasses advisory locks)")
            self.max_parallel = 1
        self.max_iterations_per_task = max_iterations_per_task
        self.log_dir = log_dir
        self.pipeline_config = pipeline_config or {}
        self.standards_dir = standards_dir
        self.agents_dir = agents_dir
        self.global_db_path = global_db_path

        # Each thread gets its own DB connection — store the path, not the conn
        self._project_db_path = self.project_dir / ".automatica" / "project.db"

        # Main-thread connections (for planning/review)
        self._project_conn: Optional[sqlite3.Connection] = None
        self._global_conn: Optional[sqlite3.Connection] = None

        # Thread-local storage for DB connections
        self._local = threading.local()

        # Lock for shared state
        self._lock = threading.Lock()
        self._active_tasks: Dict[str, str] = {}  # display_id -> status
        self._stop_requested = False

    # -- Connection management (thread-safe) --------------------------------

    def _get_project_conn(self) -> sqlite3.Connection:
        """Get or create a project DB connection for the current thread."""
        conn = getattr(self._local, "project_conn", None)
        if conn is None:
            conn = init_project_db(self.project_dir)
            self._local.project_conn = conn
        return conn

    def _get_global_conn(self) -> sqlite3.Connection:
        """Get or create a global DB connection for the current thread."""
        conn = getattr(self._local, "global_conn", None)
        if conn is None:
            conn = init_global_db(self.global_db_path)
            self._local.global_conn = conn
        return conn

    def _get_main_project_conn(self) -> sqlite3.Connection:
        """Get the main-thread project connection (for planning/review)."""
        if self._project_conn is None:
            self._project_conn = init_project_db(self.project_dir)
        return self._project_conn

    def _get_main_global_conn(self) -> sqlite3.Connection:
        """Get the main-thread global connection."""
        if self._global_conn is None:
            self._global_conn = init_global_db(self.global_db_path)
        return self._global_conn

    def _check_model_availability(self):
        """Check if configured model is available in Ollama, warn and suggest alternatives if not."""
        # Only check for Ollama provider
        if self.provider.lower() != "ollama":
            return
        
        # Get configured model
        configured_model = self.provider_config.get("model", "") if self.provider_config else ""
        if not configured_model:
            _log("[MODEL] Warning: No model configured in provider_config")
            return
        
        # Import ollama_utils to check installed models
        try:
            from ollama_utils import list_installed_models
            endpoint = self.provider_config.get("endpoint", "http://localhost:11434")
            installed_models = list_installed_models(endpoint=endpoint, timeout=5)
            
            if not installed_models:
                _log("[MODEL] Warning: Could not retrieve installed models from Ollama")
                _log(f"[MODEL] Configured model: {configured_model}")
                return
            
            # Extract base model name (remove tag)
            configured_base = configured_model.split(":")[0]
            
            # Check if configured model is installed
            model_found = any(
                configured_model == m or configured_base == m 
                for m in installed_models
            )
            
            if model_found:
                _log(f"[MODEL] Using configured model: {configured_model}")
            else:
                _log(f"[MODEL] WARNING: Configured model '{configured_model}' not found in Ollama")
                _log(f"[MODEL] Available models: {', '.join(installed_models[:5])}")
                if len(installed_models) > 5:
                    _log(f"[MODEL] ... and {len(installed_models) - 5} more")
                
                # Fall back to first available model
                if installed_models:
                    fallback_model = installed_models[0]
                    _log(f"[MODEL] Falling back to first available model: {fallback_model}")
                    self.provider_config["model"] = fallback_model
                else:
                    _log("[MODEL] ERROR: No models available in Ollama")
        except ImportError:
            _log("[MODEL] Warning: ollama_utils not available, skipping model check")
        except Exception as exc:
            _log(f"[MODEL] Warning: Model check failed: {exc}")

    # -- Planning -----------------------------------------------------------

    def plan_sprint(
        self,
        sprint_number: int,
        goal: str = "",
        max_points: int = 40,
        task_filter: Optional[Dict[str, Any]] = None,
    ) -> Tuple[dict, List[dict]]:
        """Create a sprint and select tasks from backlog.

        Returns (sprint_info, selected_tasks).

        Selection strategy:
          1. Pull backlog tasks ordered by priority/sort_order
          2. Respect max_points capacity
          3. Skip tasks with unmet dependencies
          4. Prefer parallel-ok tasks when near capacity
        """
        conn = self._get_main_project_conn()

        # Create the sprint
        sprint_info = create_sprint(conn, sprint_number, goal=goal)
        sprint_id = sprint_info["id"]
        _log(f"[PLAN] Created sprint #{sprint_number} (id={sprint_id}): {goal}")

        # Log planning ceremony
        self._log_ceremony(conn, sprint_id, "planning", f"Sprint goal: {goal}")

        # Auto-recover stale-blocked tasks (cool-down expired) before planning,
        # so a transient hang or rate-limit blip from hours ago doesn't keep
        # the task permanently ineligible.
        self._recover_stale_blocks(conn, cool_down_hours=1.0)

        # Get backlog
        backlog = get_backlog(conn, limit=100)
        if task_filter:
            backlog = self._apply_filter(backlog, task_filter)

        _log(f"[PLAN] Backlog has {len(backlog)} tasks available")

        # Select tasks up to capacity
        selected = []
        total_points = 0
        blocked_ids = self._get_blocked_task_ids(conn, backlog)

        for task in backlog:
            tid = task["display_id"]
            complexity = task.get("complexity", 3)

            # Skip blocked tasks
            if task["id"] in blocked_ids:
                _log(f"[PLAN] Skipping {tid} — blocked dependency")
                continue

            # Check capacity
            if total_points + complexity > max_points:
                # If we still have room for small tasks, keep looking
                if total_points + 1 > max_points:
                    break
                continue

            # Assign to sprint
            result = assign_task_to_sprint(conn, tid, sprint_number, story_points=complexity)
            if "error" in result:
                _log(f"[PLAN] Failed to assign {tid}: {result['error']}")
                continue

            selected.append(task)
            total_points += complexity
            _log(f"[PLAN] Assigned {tid} ({complexity}pts): {task['title'][:60]}")

        # Update sprint planned points
        conn.execute(
            "UPDATE sprints SET planned_points = ?, status = 'active', "
            "start_date = datetime('now') WHERE id = ?",
            (total_points, sprint_id),
        )
        conn.commit()

        log_action(
            conn, "sprint", sprint_id, "sprint_runner", "sprint_planned",
            detail={"tasks": len(selected), "points": total_points, "goal": goal},
        )

        _log(f"[PLAN] Sprint #{sprint_number}: {len(selected)} tasks, {total_points} points")
        return sprint_info, selected

    def _get_blocked_task_ids(self, conn: sqlite3.Connection, tasks: List[dict]) -> set:
        """Find tasks whose dependencies are not yet done."""
        blocked = set()
        for task in tasks:
            dep_id = task.get("depends_on_task_id")
            if dep_id is not None:
                dep_row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (dep_id,)
                ).fetchone()
                if dep_row and dep_row["status"] != "done":
                    blocked.add(task["id"])
        return blocked

    def _apply_filter(self, tasks: List[dict], filters: Dict[str, Any]) -> List[dict]:
        """Filter backlog tasks by criteria."""
        result = tasks

        if "max_complexity" in filters:
            result = [t for t in result if t.get("complexity", 3) <= filters["max_complexity"]]
        if "min_complexity" in filters:
            result = [t for t in result if t.get("complexity", 3) >= filters["min_complexity"]]
        if "priority" in filters:
            allowed = filters["priority"] if isinstance(filters["priority"], list) else [filters["priority"]]
            result = [t for t in result if t.get("priority", "MEDIUM") in allowed]
        if "agent_slug" in filters:
            result = [t for t in result if t.get("agent_slug", "") == filters["agent_slug"]]

        return result

    # -- Execution ----------------------------------------------------------

    def execute_sprint(self, sprint_number: int) -> SprintResult:
        """Execute all tasks in a sprint, running parallel-ok tasks concurrently.

        Returns SprintResult with per-task outcomes.
        """
        # Rate-limit cool-down: if the Claude CLI hit a subscription limit
        # within the last RATE_LIMIT_COOLDOWN_SEC, abort the sprint cleanly
        # rather than churning every task into a wasted retry. The sentinel is
        # written by adapters/subprocess_cli.py.
        rl_skip = _check_rate_limit_sentinel()
        if rl_skip:
            _log(f"[EXEC] Skipping sprint #{sprint_number} — {rl_skip}")
            return SprintResult(sprint_number=sprint_number, goal="",
                                 status="rate_limited",
                                 errors=[rl_skip])

        conn = self._get_main_project_conn()
        sprint_tasks = get_sprint_tasks(conn, sprint_number)

        if not sprint_tasks:
            _log(f"[EXEC] No tasks found for sprint #{sprint_number}")
            return SprintResult(sprint_number=sprint_number, goal="", status="empty")

        # Get sprint info
        sprint_row = conn.execute(
            "SELECT * FROM sprints WHERE sprint_number = ?", (sprint_number,)
        ).fetchone()
        goal = sprint_row["goal"] if sprint_row else ""
        sprint_id = sprint_row["id"] if sprint_row else 0

        result = SprintResult(
            sprint_number=sprint_number,
            goal=goal,
            tasks_planned=len(sprint_tasks),
            points_planned=sum(t.get("story_points", t.get("complexity", 3)) for t in sprint_tasks),
        )

        start_time = time.time()

        # Partition tasks: parallel-safe vs sequential
        # Also skip tasks that have already hit the retry cap.
        parallel_tasks = []
        sequential_tasks = []
        _retry_cap_skipped = 0
        for st in sprint_tasks:
            if st.get("status") in ("done", "blocked"):
                continue
            # Check if underlying task allows parallel execution
            task_row = conn.execute(
                "SELECT parallel_ok, depends_on_task_id, failure_count FROM tasks WHERE display_id = ?",
                (st["task_display_id"],)
            ).fetchone()
            # Skip tasks that have reached the retry cap
            if task_row:
                _fc = task_row["failure_count"] if "failure_count" in task_row.keys() else 0
                try:
                    from automatica_db import MAX_TASK_RETRIES
                    if _fc >= MAX_TASK_RETRIES:
                        _log(f"[EXEC] Skipping {st['task_display_id']} — retry cap "
                             f"({_fc}/{MAX_TASK_RETRIES} failures)")
                        _retry_cap_skipped += 1
                        result.tasks_blocked += 1
                        result.task_results.append(TaskResult(
                            task_display_id=st["task_display_id"],
                            title=st.get("title", ""),
                            success=False,
                            error=f"Retry cap reached ({_fc}/{MAX_TASK_RETRIES} failures)",
                        ))
                        continue
                except ImportError:
                    pass
            if task_row and task_row["parallel_ok"] and not task_row["depends_on_task_id"]:
                parallel_tasks.append(st)
            else:
                sequential_tasks.append(st)

        _log(f"[EXEC] Sprint #{sprint_number}: {len(parallel_tasks)} parallel, "
             f"{len(sequential_tasks)} sequential tasks")

        # Log daily standup
        self._log_ceremony(
            conn, sprint_id, "daily",
            f"Starting execution: {len(parallel_tasks)} parallel + "
            f"{len(sequential_tasks)} sequential tasks",
        )

        # Execute parallel tasks first
        if parallel_tasks:
            parallel_results = self._execute_parallel(parallel_tasks, sprint_number)
            result.task_results.extend(parallel_results)

        # Then execute sequential tasks in order
        for st in sequential_tasks:
            # Check if dependency is now met (might have been completed in parallel batch)
            task_row = conn.execute(
                "SELECT depends_on_task_id FROM tasks WHERE display_id = ?",
                (st["task_display_id"],)
            ).fetchone()
            dep_id = task_row["depends_on_task_id"] if task_row else None
            if dep_id:
                dep_row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (dep_id,)
                ).fetchone()
                if dep_row and dep_row["status"] != "done":
                    _log(f"[EXEC] Skipping {st['task_display_id']} — dependency not done")
                    result.tasks_blocked += 1
                    result.task_results.append(TaskResult(
                        task_display_id=st["task_display_id"],
                        title=st.get("title", ""),
                        success=False,
                        error="Blocked: dependency not completed",
                    ))
                    continue

            task_result = self._execute_single_task(st, sprint_number)
            result.task_results.append(task_result)

        result.total_duration = time.time() - start_time

        # Tally results
        for tr in result.task_results:
            if tr.success:
                result.tasks_completed += 1
                # Look up points for this task
                st_match = next(
                    (s for s in sprint_tasks if s["task_display_id"] == tr.task_display_id),
                    None,
                )
                if st_match:
                    result.points_completed += st_match.get(
                        "story_points", st_match.get("complexity", 3)
                    )
            elif tr.error and "blocked" in tr.error.lower():
                pass  # already counted
            else:
                result.tasks_failed += 1

        # Determine overall status
        if result.tasks_completed == result.tasks_planned:
            result.status = "completed"
        elif result.tasks_completed > 0:
            result.status = "partial"
        else:
            result.status = "failed"

        # S3-W03: zero-velocity self-healing guard
        if result.tasks_completed == 0 and result.tasks_failed >= 3:
            self._bump_all_failed_complexities(conn, result.task_results)

        return result

    def _bump_all_failed_complexities(
        self, conn: sqlite3.Connection, task_results: List["TaskResult"]
    ) -> None:
        """S3-W03: Bump complexity for all failed tasks after a zero-velocity sprint.

        Bug fix (2026-05-03): previously this path called handle_agent_failure
        which counted toward failure_count. A single real failure could then
        amplify across all planned tasks in the same sprint, blocking 3+ tasks
        permanently from one root cause. Now we ONLY bump complexity (Fibonacci
        step up). The per-task failure_count is incremented exactly once per
        actual execution attempt elsewhere.

        Tasks at retry cap are skipped (already blocked).
        """
        from automatica_db import get_task_failure_count, MAX_TASK_RETRIES
        _fib = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
        bumped = 0
        skipped_cap = 0
        for tr in task_results:
            if tr.success:
                continue
            try:
                if get_task_failure_count(conn, tr.task_display_id) >= MAX_TASK_RETRIES:
                    skipped_cap += 1
                    continue
            except Exception:
                pass
            try:
                row = conn.execute(
                    "SELECT complexity FROM tasks WHERE display_id = ?",
                    (tr.task_display_id,)
                ).fetchone()
                if row:
                    current = row["complexity"] if isinstance(row, dict) else row[0]
                    new_c = next((f for f in _fib if f > (current or 0)), 89)
                    conn.execute(
                        "UPDATE tasks SET complexity = ? WHERE display_id = ?",
                        (new_c, tr.task_display_id)
                    )
                    conn.commit()
                    bumped += 1
            except Exception:
                pass
        _msg = f"[HEAL] Bumped complexity for {bumped} task(s) (no failure_count change)"
        if skipped_cap:
            _msg += f" ({skipped_cap} skipped — at retry cap)"
        _log(_msg)

    def _recover_stale_blocks(
        self, conn: sqlite3.Connection, cool_down_hours: float = 1.0
    ) -> int:
        """Auto-recover blocked tasks whose last_failure_at is older than the
        cool-down window. Resets status to 'backlog' and failure_count to 0 so
        the planner gives them another shot.

        Without this, MAX_TASK_RETRIES is effectively a permanent ban — one
        transient hang or rate-limit blip kills the task forever. With this,
        the system self-heals after waiting cool_down_hours.
        """
        try:
            cur = conn.execute(
                "UPDATE tasks SET status='backlog', failure_count=0, blocked_reason=NULL "
                "WHERE status='blocked' "
                "  AND last_failure_at IS NOT NULL "
                "  AND datetime(last_failure_at, '+' || ? || ' hours') < datetime('now')"
                "  AND (blocked_reason IS NULL OR blocked_reason LIKE '%Retry cap%' "
                "       OR blocked_reason LIKE '%CLI exited%' "
                "       OR blocked_reason LIKE '%timed out%' "
                "       OR blocked_reason LIKE '%auto-bump%')",
                (cool_down_hours,)
            )
            n = cur.rowcount
            conn.commit()
            if n:
                _log(f"[HEAL] Recovered {n} stale-blocked task(s) past {cool_down_hours}h cool-down")
            return n
        except Exception as exc:
            _log(f"[HEAL] _recover_stale_blocks failed: {exc}")
            return 0

    def _execute_parallel(
        self, tasks: List[dict], sprint_number: int
    ) -> List[TaskResult]:
        """Run multiple tasks in parallel using a thread pool."""
        results: List[TaskResult] = []

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            future_map: Dict[Future, dict] = {}
            for st in tasks:
                future = pool.submit(self._execute_single_task, st, sprint_number)
                future_map[future] = st

            for future in as_completed(future_map):
                st = future_map[future]
                try:
                    task_result = future.result(timeout=1800)  # 30 min max per task
                    results.append(task_result)
                except Exception as exc:
                    _log(f"[EXEC] Task {st.get('task_display_id', '?')} raised exception: {exc}")
                    results.append(TaskResult(
                        task_display_id=st.get("task_display_id", "?"),
                        title=st.get("title", ""),
                        success=False,
                        error=str(exc),
                    ))

        return results

    def _execute_single_task(self, sprint_task: dict, sprint_number: int) -> TaskResult:
        """Execute a single sprint task through AgentProtocol.

        Each call gets its own DB connections (thread-safe).
        Respects the per-task retry cap (MAX_TASK_RETRIES) and applies
        exponential backoff between retries based on cumulative failure_count.
        """
        display_id = sprint_task.get("task_display_id", "")
        title = sprint_task.get("title", "")
        _log(f"[TASK] Starting: {display_id} — {title[:60]}")

        # Thread-local DB connections
        pconn = self._get_project_conn()
        gconn = self._get_global_conn()

        # ── Retry-cap gate: skip if this task already hit the failure limit ──
        try:
            from automatica_db import (
                get_task_failure_count, get_retry_backoff_seconds,
                MAX_TASK_RETRIES,
            )
            _db_failure_count = get_task_failure_count(pconn, display_id)
            if _db_failure_count >= MAX_TASK_RETRIES:
                _log(f"[TASK] {display_id} SKIPPED — retry cap reached "
                     f"({_db_failure_count}/{MAX_TASK_RETRIES} failures)")
                return TaskResult(
                    task_display_id=display_id,
                    title=title,
                    success=False,
                    error=f"Retry cap reached ({_db_failure_count}/{MAX_TASK_RETRIES} failures)",
                )

            # Apply exponential backoff if this is a retry (failure_count > 0)
            if _db_failure_count > 0:
                backoff_s = get_retry_backoff_seconds(_db_failure_count)
                _log(f"[TASK] {display_id}: retry #{_db_failure_count + 1} — "
                     f"backoff {backoff_s:.0f}s before execution")
                time.sleep(backoff_s)
        except ImportError:
            _db_failure_count = 0  # graceful degradation

        # Count prior failed/blocked attempts for this task (for accurate retry tracking)
        try:
            _attempt_number = pconn.execute(
                "SELECT COUNT(*) FROM sprint_tasks st "
                "JOIN tasks t ON t.id = st.task_id "
                "WHERE t.display_id = ? AND st.status IN ('blocked', 'failed')",
                (display_id,)
            ).fetchone()[0] + 1
        except Exception:
            _attempt_number = 1

        # Mark task as in-progress (with timestamps and provider/model info)
        model_name = self.provider_config.get('model', '') if self.provider_config else ''
        try:
            update_task(pconn, display_id, status="in_progress",
                        assigned_provider=self.provider, assigned_model=model_name,
                        started_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
            self._update_sprint_task_status(pconn, display_id, sprint_number, "in_progress",
                                            provider=self.provider, model=model_name)
        except Exception as exc:
            _log(f"[TASK] Warning: failed to update status for {display_id}: {exc}")

        with self._lock:
            self._active_tasks[display_id] = "in_progress"

        # Build task dict for AgentProtocol
        task_dict = {
            "display_id": display_id,
            "title": title,
            "description": sprint_task.get("description", ""),
            "acceptance_criteria": sprint_task.get("acceptance_criteria", ""),
            "complexity": sprint_task.get("complexity", 3),
        }

        # S3-W06: route provider by task complexity
        complexity = task_dict.get('complexity', 3)
        routed_provider_config = self._route_provider_by_complexity(complexity)

        # Build context and instructions
        context = load_context(self.project_dir)
        context["sprint_number"] = sprint_number
        context["sprint_goal"] = sprint_task.get("goal", "")

        # Enrich with DB task context
        try:
            db_context = build_task_context(pconn, display_id)
            if db_context and "error" not in db_context:
                context["task_context"] = db_context
        except Exception:
            pass

        # S3-W04: inject wiki_summary (top 20 recent files) into context before protocol
        try:
            from project_scanner import list_project_files
            files = list_project_files(self.project_dir)
            file_count = len(files) if files else 0
            if file_count > 0:
                top20 = sorted(files, key=lambda f: f.get('mtime_epoch', 0), reverse=True)[:20]
                context['wiki_summary'] = top20
        except Exception:
            pass

        _pkg_dir = Path(__file__).resolve().parent
        instructions = load_instructions(
            config=self.pipeline_config,
            step=8,  # main task execution step
            agent_role=sprint_task.get("assigned_agent", "Developer"),
            project_dir=self.project_dir,
            standards_dir=self.standards_dir,
            agents_dir=self.agents_dir or (_pkg_dir / "agents"),
            provider_config=self.provider_config,
        )

        # Build log directory for this task
        task_log_dir = None
        if self.log_dir:
            task_log_dir = self.log_dir / f"sprint_{sprint_number}" / display_id
            task_log_dir.mkdir(parents=True, exist_ok=True)

        # Create protocol instance (each task gets its own).
        # provider arg to AgentProtocol must be the adapter TYPE (e.g.
        # 'subprocess_cli'), not the provider name (e.g. 'Claude-Sonnet').
        # Bug fix: previously this fell back to `self.provider` (the friendly
        # name) when the routed config was empty, which silently passed an
        # invalid name to get_adapter() and crashed every task in ~10s. Now
        # raise loudly so misconfigured providers (e.g. a CLI flag pointing
        # at a provider name that doesn't exist in the global DB) surface
        # immediately instead of producing a 15-sprint zero-velocity run.
        _adapter_type = (routed_provider_config or {}).get('provider_type', '')
        if not _adapter_type:
            raise RuntimeError(
                f"Provider {self.provider!r} has no usable provider_type "
                f"(provider_config is empty or missing the 'provider_type' "
                f"key). Check that a row named {self.provider!r} exists in "
                f"the global providers table and has provider_type set "
                f"(e.g. 'subprocess_cli')."
            )
        protocol = AgentProtocol(
            provider=_adapter_type,
            provider_config=routed_provider_config,  # S3-W06: complexity-routed config
            project_dir=self.project_dir,
            max_iterations=self.max_iterations_per_task,
            log_dir=task_log_dir,
            project_db=pconn,
            global_db=gconn,
            confirm_before_done=True,
            broadcast_fn=_get_push_event_fn(),  # S5-W08: WS event emission
        )

        # Execute the agent loop — use atomic step execution for stepped tasks
        start = time.time()

        def _is_connection_err(reason: str) -> bool:
            kws = ("connectionerror", "timeouterror", "adapter error",
                   "connection refused", "timed out", "timeout", "connection reset")
            r = reason.lower()
            return any(k in r for k in kws)

        try:
            loop_result = protocol.execute_loop_atomic(task_dict, context, instructions)
        except Exception as exc:
            _log(f"[TASK] {display_id} crashed: {exc}")
            traceback.print_exc()
            loop_result = {"success": False, "reason": str(exc), "iterations": 0, "duration": 0}

        # Retry with exponential backoff on transient connection errors
        if not loop_result.get("success") and _is_connection_err(loop_result.get("reason", "")):
            for backoff_s in (2, 4):
                _log(f"[TASK] {display_id}: connection error — retrying in {backoff_s}s "
                     f"({loop_result.get('reason', '')[:60]})")
                time.sleep(backoff_s)
                try:
                    loop_result = protocol.execute_loop_atomic(task_dict, context, instructions)
                except Exception as exc:
                    loop_result = {"success": False, "reason": str(exc), "iterations": 0, "duration": 0}
                if loop_result.get("success") or not _is_connection_err(loop_result.get("reason", "")):
                    break

        # If still a connection error after retries, fall back to next enabled provider
        if not loop_result.get("success") and _is_connection_err(loop_result.get("reason", "")) and gconn:
            try:
                fallback_row = gconn.execute(
                    "SELECT name, provider_type, base_url, model_name FROM providers "
                    "WHERE enabled=1 AND LOWER(name) != LOWER(?) ORDER BY id LIMIT 1",
                    (self.provider,)
                ).fetchone()
                if fallback_row:
                    fb_name = fallback_row["name"]
                    _log(f"[TASK] {display_id}: falling back to provider '{fb_name}'")
                    fb_config = {
                        "endpoint": fallback_row["base_url"] or "",
                        "model":    fallback_row["model_name"] or "",
                    }
                    fb_protocol = AgentProtocol(
                        provider=fallback_row["provider_type"],
                        provider_config=fb_config,
                        project_dir=self.project_dir,
                        max_iterations=self.max_iterations_per_task,
                        log_dir=task_log_dir,
                        project_db=pconn,
                        global_db=gconn,
                        confirm_before_done=True,
                    )
                    try:
                        loop_result = fb_protocol.execute_loop_atomic(task_dict, context, instructions)
                    except Exception as exc:
                        loop_result = {"success": False, "reason": str(exc), "iterations": 0, "duration": 0}
            except Exception as exc:
                _log(f"[TASK] Provider fallback lookup failed: {exc}")

        duration = time.time() - start
        success = loop_result.get("success", False)

        # S3-W09: persist total token estimate to sprint_tasks row
        try:
            _total_tokens = getattr(protocol, '_total_tokens', 0)
            if _total_tokens > 0:
                pconn.execute("""
                    UPDATE sprint_tasks SET total_tokens = ?
                    WHERE task_id = (SELECT id FROM tasks WHERE display_id = ?)
                      AND sprint_id = (SELECT id FROM sprints WHERE sprint_number = ?)
                """, (_total_tokens, display_id, sprint_number))
                pconn.commit()
        except Exception:
            pass  # token tracking is best-effort

        # Build TaskResult
        task_result = TaskResult(
            task_display_id=display_id,
            title=title,
            success=success,
            summary=loop_result.get("summary", loop_result.get("reason", "")),
            files_changed=loop_result.get("files_changed", []),
            iterations=loop_result.get("iterations", 0),
            duration=duration,
            error="" if success else loop_result.get("reason", "Unknown error"),
        )

        # Handle success or failure with rollback + complexity bumping
        if success:
            try:
                update_task(pconn, display_id, status="done",
                            completed_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
                self._update_sprint_task_status(pconn, display_id, sprint_number, "done",
                                                iterations=task_result.iterations,
                                                duration_ms=int(duration * 1000))
                log_action(
                    pconn, "task", self._get_task_id(pconn, display_id), "sprint_runner",
                    "completed",
                    detail={"summary": task_result.summary[:500],
                            "iterations": task_result.iterations,
                            "duration_s": round(duration, 1),
                            "files_changed": task_result.files_changed[:20]},
                )
                # Record success in provider performance
                if gconn:
                    try:
                        from automatica_db import record_provider_attempt, record_usage_log
                        prov = gconn.execute("SELECT id FROM providers WHERE name = ?",
                                              (self.provider,)).fetchone()
                        if prov:
                            record_provider_attempt(gconn, prov['id'],
                                                      task_dict.get('complexity', 3), True,
                                                      int(duration * 1000))
                            record_usage_log(
                                gconn, prov['id'],
                                agent_slug=sprint_task.get('assigned_agent', ''),
                                duration_ms=int(duration * 1000),
                                success=True,
                                session_id=f"sprint_{sprint_number}_{display_id}",
                            )
                    except Exception:
                        pass
            except Exception as exc:
                _log(f"[TASK] Warning: failed to update success status for {display_id}: {exc}")
        else:
            # FAILURE: rollback changes and bump complexity
            _log(f"[TASK] {display_id} FAILED — initiating rollback + complexity bump")
            # Record failure in usage_log for per-agent analytics
            if gconn:
                try:
                    from automatica_db import record_usage_log
                    prov = gconn.execute("SELECT id FROM providers WHERE name = ?",
                                          (self.provider,)).fetchone()
                    if prov:
                        record_usage_log(
                            gconn, prov['id'],
                            agent_slug=sprint_task.get('assigned_agent', ''),
                            duration_ms=int(duration * 1000),
                            success=False,
                            error_message=loop_result.get('reason', 'Unknown failure'),
                            session_id=f"sprint_{sprint_number}_{display_id}",
                        )
                except Exception:
                    pass
            try:
                from validation import handle_agent_failure, TransactionTracker
                failure_result = handle_agent_failure(
                    pconn, gconn, display_id,
                    agent=sprint_task.get('assigned_agent', 'Developer'),
                    provider=self.provider,
                    reason=loop_result.get('reason', 'Unknown failure'),
                    attempt_number=_attempt_number,
                    max_attempts=3,
                )
                _fc = failure_result.get('failure_count', '?')
                _mr = failure_result.get('max_retries', '?')
                if failure_result['action'] == 'retry_cap_reached':
                    _log(f"[TASK] {display_id} RETRY CAP REACHED "
                         f"({_fc}/{_mr} failures) — marked blocked, escalated")
                elif failure_result['action'] == 'bumped':
                    _log(f"[TASK] {display_id} complexity bumped: "
                         f"{failure_result['old_complexity']} → {failure_result['new_complexity']} "
                         f"(failure {_fc}/{_mr})")
                elif failure_result['action'] == 'exhausted':
                    _log(f"[TASK] {display_id} EXHAUSTED — at max complexity, needs human intervention")

                update_task(pconn, display_id, status="blocked",
                            completed_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
                self._update_sprint_task_status(pconn, display_id, sprint_number, "blocked",
                                                iterations=task_result.iterations,
                                                duration_ms=int(duration * 1000))
            except Exception as exc:
                _log(f"[TASK] Warning: rollback/bump failed for {display_id}: {exc}")
                try:
                    update_task(pconn, display_id, status="blocked")
                except Exception:
                    pass

            # E-07: check for fallback agent and retry if configured
            if not success:
                try:
                    _agent_slug_primary = sprint_task.get('assigned_agent', '') or ''
                    fallback_row = gconn.execute(
                        "SELECT fallback_slug FROM agent_definitions WHERE slug = ?",
                        (_agent_slug_primary,),
                    ).fetchone() if gconn and _agent_slug_primary else None
                    fallback_slug = fallback_row['fallback_slug'] if fallback_row else ''
                    if fallback_slug:
                        _log(f"[TASK] {display_id}: retrying with fallback agent '{fallback_slug}'")
                        fallback_task = dict(sprint_task)
                        fallback_task['assigned_agent'] = fallback_slug
                        fb_result = self._execute_single_task(fallback_task, sprint_number)
                        if fb_result.success:
                            task_result = fb_result
                            success = True
                            _log(f"[TASK] {display_id}: fallback agent '{fallback_slug}' succeeded")
                        else:
                            _log(f"[TASK] {display_id}: fallback agent '{fallback_slug}' also failed")
                except Exception as _fb_exc:
                    _log(f"[TASK] E-07 fallback check failed: {_fb_exc}")

        with self._lock:
            self._active_tasks[display_id] = "done" if success else "failed"

        # F-08: Auto-commit on task success if enabled in settings
        if success:
            try:
                from pathlib import Path as _P
                import json as _j
                _cfg_path = _P.home() / '.automatica' / 'settings.json'
                _settings_f08 = _j.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
                if _settings_f08.get('git_auto_commit', False):
                    _prefix = _settings_f08.get('git_commit_prefix', 'feat: ')
                    _cmsg = f"{_prefix}{title[:100]}"
                    import subprocess as _sp
                    _sp.run(['git', 'add', '-A'], cwd=str(self.project_dir),
                            capture_output=True, timeout=15)
                    _sp.run(['git', 'commit', '-m', _cmsg],
                            cwd=str(self.project_dir),
                            capture_output=True, timeout=15)
                    _log(f"[VCS] Auto-committed: {_cmsg}")
            except Exception as _vcs_exc:
                _log(f"[VCS] Auto-commit failed: {_vcs_exc}")

        # E-21: record agent execution history in project DB
        try:
            from automatica_db import record_agent_execution
            _exec_status = 'done' if success else ('blocked' if 'blocked' in task_result.error.lower() else 'failed')
            _agent_slug = sprint_task.get('assigned_agent', '') or ''
            _session_id = getattr(protocol, '_session_id', '') if 'protocol' in dir() else ''
            _sprint_id_val = pconn.execute(
                "SELECT id FROM sprints WHERE sprint_number = ?", (sprint_number,)
            ).fetchone()
            _sprint_id_val = _sprint_id_val['id'] if _sprint_id_val else None
            _files_n = len(task_result.files_changed)
            _total_tok = getattr(protocol, '_total_tokens', 0)
            record_agent_execution(
                pconn, _agent_slug, display_id, _session_id,
                _exec_status, round(duration, 2), _total_tok, _files_n,
                sprint_id=_sprint_id_val,
            )
        except Exception:
            pass  # best-effort

        status_icon = "OK" if success else "FAIL"
        _log(f"[TASK] {status_icon}: {display_id} in {duration:.1f}s "
             f"({task_result.iterations} iterations)")

        return task_result

    def _route_provider_by_complexity(self, complexity: int) -> dict:
        """S3-W06: Select provider config based on task complexity.

        - complexity <= 3  → fast provider (gemma3:4b or "fast")
        - complexity 4-8   → medium provider (gemma3:12b or "medium")
        - complexity > 8   → self.provider (assumed best available)

        Falls back to self.provider gracefully if nothing matches.
        """
        try:
            gconn = self._get_global_conn()
            if complexity <= 3:
                preferred_names = ('gemma3:4b', 'fast')
            elif complexity <= 8:
                preferred_names = ('gemma3:12b', 'medium')
            else:
                return self.provider_config or {}

            for pname in preferred_names:
                row = gconn.execute(
                    "SELECT provider_type, base_url, model_name, config_json FROM providers "
                    "WHERE enabled=1 AND (LOWER(name)=LOWER(?) OR LOWER(model_name)=LOWER(?)) LIMIT 1",
                    (pname, pname)
                ).fetchone()
                if row:
                    cfg = {'provider_type': row['provider_type'],
                           'endpoint': row['base_url'] or '',
                           'model': row['model_name'] or ''}
                    try:
                        extra = json.loads(row['config_json'] or '{}')
                        if isinstance(extra, dict):
                            cfg.update(extra)
                    except Exception:
                        pass
                    return cfg
        except Exception:
            pass
        return self.provider_config or {}

    def _update_sprint_task_status(
        self, conn: sqlite3.Connection, task_display_id: str,
        sprint_number: int, status: str,
        provider: str = '', model: str = '',
        iterations: int = 0, duration_ms: int = 0,
    ):
        """Update the sprint_tasks row status for a given task."""
        conn.execute("""
            UPDATE sprint_tasks SET status = ?
            WHERE task_id = (SELECT id FROM tasks WHERE display_id = ?)
              AND sprint_id = (SELECT id FROM sprints WHERE sprint_number = ?)
        """, (status, task_display_id, sprint_number))
        if status == "in_progress":
            conn.execute("""
                UPDATE sprint_tasks
                SET started_at = datetime('now'),
                    assigned_provider = CASE WHEN ? != '' THEN ? ELSE assigned_provider END,
                    assigned_model    = CASE WHEN ? != '' THEN ? ELSE assigned_model END
                WHERE task_id = (SELECT id FROM tasks WHERE display_id = ?)
                  AND sprint_id = (SELECT id FROM sprints WHERE sprint_number = ?)
            """, (provider, provider, model, model, task_display_id, sprint_number))
        elif status in ("done", "blocked"):
            conn.execute("""
                UPDATE sprint_tasks
                SET completed_at = datetime('now'),
                    iterations   = CASE WHEN ? > 0 THEN ? ELSE iterations END,
                    duration_ms  = CASE WHEN ? > 0 THEN ? ELSE duration_ms END
                WHERE task_id = (SELECT id FROM tasks WHERE display_id = ?)
                  AND sprint_id = (SELECT id FROM sprints WHERE sprint_number = ?)
            """, (iterations, iterations, duration_ms, duration_ms, task_display_id, sprint_number))
        conn.commit()

    def _get_task_id(self, conn: sqlite3.Connection, display_id: str) -> int:
        """Resolve display_id to internal task id."""
        row = conn.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (display_id,)
        ).fetchone()
        return row["id"] if row else 0

    # -- E-22: Single-task runner -------------------------------------------

    def run_single_task(self, task_display_id: str) -> TaskResult:
        """Execute a single task outside of a full sprint. (E-22)

        Finds or creates a sprint context for the task, then calls
        _execute_single_task with that context.
        """
        pconn = self._get_main_project_conn()

        # Look up the task row
        task_row = pconn.execute(
            "SELECT * FROM tasks WHERE display_id = ?", (task_display_id,)
        ).fetchone()
        if not task_row:
            return TaskResult(
                task_display_id=task_display_id,
                title='',
                success=False,
                error=f'Task {task_display_id!r} not found',
            )

        # Find the most recent sprint this task belongs to, or use sprint 0
        st_row = pconn.execute(
            """SELECT st.*, s.sprint_number, s.goal
               FROM sprint_tasks st
               JOIN sprints s ON st.sprint_id = s.id
               WHERE st.task_id = ?
               ORDER BY s.sprint_number DESC LIMIT 1""",
            (task_row['id'],),
        ).fetchone()

        if st_row:
            sprint_number = st_row['sprint_number']
            sprint_task = dict(st_row)
            sprint_task.setdefault('task_display_id', task_display_id)
            sprint_task.setdefault('title', task_row['title'])
            sprint_task.setdefault('description', task_row['description'])
            sprint_task.setdefault('acceptance_criteria', task_row['acceptance_criteria'])
            sprint_task.setdefault('complexity', task_row['complexity'])
        else:
            # No sprint — create an ad-hoc sprint 0 entry
            sprint_number = 0
            sprint_task = {
                'task_display_id': task_display_id,
                'title': task_row['title'],
                'description': task_row['description'],
                'acceptance_criteria': task_row['acceptance_criteria'],
                'complexity': task_row['complexity'],
                'assigned_agent': task_row['agent_slug'],
                'goal': '',
            }

        return self._execute_single_task(sprint_task, sprint_number)

    # -- Review -------------------------------------------------------------

    def review_sprint(self, sprint_number: int, sprint_result: SprintResult) -> dict:
        """Post-sprint review: update DB, record velocity, log ceremonies.

        Returns the sprint summary dict.
        """
        conn = self._get_main_project_conn()

        sprint_row = conn.execute(
            "SELECT id FROM sprints WHERE sprint_number = ?", (sprint_number,)
        ).fetchone()
        if not sprint_row:
            return {"error": f"Sprint {sprint_number} not found"}
        sprint_id = sprint_row["id"]

        # Update sprint record
        conn.execute("""
            UPDATE sprints SET
                status = 'completed',
                completed_points = ?,
                velocity = ?,
                end_date = datetime('now')
            WHERE id = ?
        """, (sprint_result.points_completed, round(sprint_result.velocity, 2), sprint_id))

        # Persist velocity history in project_settings (rolling last 10)
        try:
            from automatica_db import get_project_setting_json, set_project_setting_json
            hist = get_project_setting_json(conn, 'velocity_history') or []
            hist.append({
                'sprint': sprint_number,
                'velocity': round(sprint_result.velocity, 2),
                'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            })
            set_project_setting_json(conn, 'velocity_history', hist[-10:])
        except Exception:
            pass

        conn.commit()

        # Log review ceremony
        review_notes = (
            f"Sprint #{sprint_number} complete. "
            f"{sprint_result.tasks_completed}/{sprint_result.tasks_planned} tasks done. "
            f"{sprint_result.points_completed}/{sprint_result.points_planned} points. "
            f"Velocity: {sprint_result.velocity:.1f} pts/hr. "
            f"Duration: {sprint_result.total_duration:.0f}s."
        )
        self._log_ceremony(conn, sprint_id, "review", review_notes)

        # Log retro ceremony with lessons
        retro_items = []
        for tr in sprint_result.task_results:
            if not tr.success and tr.error:
                retro_items.append(f"FAIL {tr.task_display_id}: {tr.error[:100]}")
            elif tr.success and tr.iterations > 30:
                retro_items.append(
                    f"SLOW {tr.task_display_id}: took {tr.iterations} iterations"
                )
        if retro_items:
            self._log_ceremony(conn, sprint_id, "retro", "\n".join(retro_items))

        # Move failed tasks back to backlog for next sprint, unless they've failed too often.
        # Bug fix: previously this counted *historical* sprint_tasks rows
        # in 'failed'/'blocked' state across all time, which double-counted
        # with validation.handle_agent_failure's persistent counter and
        # inflated rapidly (the log showed e.g. "failed 14x" right after a
        # task's 1st real failure in this cycle, because 13 sprint_tasks
        # rows were left over from previous days). Use tasks.failure_count
        # — the same counter validation.handle_agent_failure manages, which
        # resets to 0 on success and is the authoritative consecutive-fail
        # count. Threshold lifted to MAX_TASK_RETRIES so the two layers agree.
        from automatica_db import get_task_failure_count as _get_task_fc
        try:
            from validation import MAX_TASK_RETRIES as _MAX_RETRIES
        except ImportError:
            _MAX_RETRIES = 5
        for tr in sprint_result.task_results:
            if not tr.success:
                try:
                    attempts = _get_task_fc(conn, tr.task_display_id) or 0
                    if attempts >= _MAX_RETRIES:
                        _log(f"[PLAN] {tr.task_display_id} failed {attempts}x — marking abandoned")
                        update_task(conn, tr.task_display_id, status="failed")
                    else:
                        update_task(conn, tr.task_display_id, status="backlog")
                except Exception:
                    pass

        # Build and return summary
        summary = build_sprint_summary(conn, sprint_number)

        log_action(
            conn, "sprint", sprint_id, "sprint_runner", "sprint_reviewed",
            detail={
                "completed": sprint_result.tasks_completed,
                "failed": sprint_result.tasks_failed,
                "blocked": sprint_result.tasks_blocked,
                "velocity": round(sprint_result.velocity, 2),
            },
        )

        _log(f"[REVIEW] Sprint #{sprint_number}: "
             f"{sprint_result.tasks_completed}/{sprint_result.tasks_planned} done, "
             f"velocity={sprint_result.velocity:.1f} pts/hr")

        # F-14/F-15/F-16: Send notifications on sprint completion
        try:
            from notifications import send_sprint_complete, send_webhook, send_slack
            result_dict = sprint_result.to_dict()
            send_sprint_complete(result_dict)
            send_webhook('sprint_complete', result_dict)
            send_slack(
                f"Sprint {sprint_number} complete: "
                f"{sprint_result.tasks_completed}/{sprint_result.tasks_planned} tasks, "
                f"velocity {sprint_result.velocity:.1f} pts/hr"
            )
        except Exception as _notif_exc:
            _log(f"[NOTIFY] Notification failed (non-fatal): {_notif_exc}")

        return summary

    def _log_ceremony(
        self, conn: sqlite3.Connection, sprint_id: int,
        ceremony_type: str, notes: str,
    ):
        """Record a sprint ceremony (planning, daily, review, retro)."""
        conn.execute("""
            INSERT INTO sprint_ceremonies (sprint_id, ceremony_type, notes)
            VALUES (?, ?, ?)
        """, (sprint_id, ceremony_type, notes))
        conn.commit()

    # -- Full Sprint Run ----------------------------------------------------

    def run_sprint(
        self,
        sprint_number: int,
        goal: str = "",
        max_points: int = 40,
        task_filter: Optional[Dict[str, Any]] = None,
        skip_planning: bool = False,
    ) -> SprintResult:
        """Run a complete sprint by iterating through DB-driven pipeline steps.

        Pipeline steps are read from the project DB (pipeline_steps table).
        Each step's phase_type determines how it is dispatched:
          - 'agent'   -> run AgentProtocol with the step's prompt and agent_slug
          - 'cmd'     -> run a subprocess command
          - 'builtin' -> special handling (task execution loop)
          - 'review'  -> run AgentProtocol in review/summarize mode

        Args:
            sprint_number: The sprint number to create/run.
            goal: Sprint goal description.
            max_points: Maximum story points to pull from backlog.
            task_filter: Optional dict to filter backlog tasks.
            skip_planning: If True, skip pipeline steps that perform planning.

        Returns:
            SprintResult with full details.
        """
        _log(f"{'=' * 60}")
        _log(f"[SPRINT] Starting sprint #{sprint_number}: {goal}")
        _log(f"{'=' * 60}")

        # F-15: Webhook on sprint start
        try:
            from notifications import send_webhook
            send_webhook('sprint_start', {'sprint_number': sprint_number, 'goal': goal})
        except Exception:
            pass

        # Model auto-detection on startup
        self._check_model_availability()

        # S3-W05: pre-sprint provider health check
        _health_ok, _latency_ms = _check_provider_health(self.provider_config or {})
        if not _health_ok:
            _log(f"[SPRINT] Provider health check FAILED — aborting sprint #{sprint_number}")
            try:
                from automatica_db import log_provider_health as _lph
                gconn = self._get_main_global_conn()
                _lph(gconn, self.provider, -1, 'unreachable')
                # Also log to provider_performance table
                from automatica_db import record_provider_attempt
                prov = gconn.execute("SELECT id FROM providers WHERE name = ?", (self.provider,)).fetchone()
                if prov:
                    record_provider_attempt(gconn, prov['id'], 1, False, -1)
            except Exception:
                pass
            return SprintResult(
                sprint_number=sprint_number,
                goal=goal,
                status='provider_unavailable',
            )
        else:
            _log(f"[SPRINT] Provider health OK (latency={_latency_ms}ms)")
            try:
                from automatica_db import log_provider_health as _lph
                gconn = self._get_main_global_conn()
                _lph(gconn, self.provider, _latency_ms, 'ok')
                # Also log to provider_performance table
                from automatica_db import record_provider_attempt
                prov = gconn.execute("SELECT id FROM providers WHERE name = ?", (self.provider,)).fetchone()
                if prov:
                    record_provider_attempt(gconn, prov['id'], 1, True, _latency_ms)
            except Exception:
                pass

        overall_start = time.time()
        self._stop_requested = False

        # Store run context for phase dispatch
        self._run_ctx = {
            "sprint_number": sprint_number,
            "goal": goal,
            "max_points": max_points,
            "task_filter": task_filter,
            "skip_planning": skip_planning,
            "sprint_result": None,  # populated by builtin execution phase
            "planning_done": False,
        }

        # Read pipeline steps from DB
        conn = self._get_main_project_conn()
        steps = get_pipeline_steps(conn, enabled_only=True)

        if not steps:
            _log("[SPRINT] No pipeline steps found in DB — falling back to hardcoded phases")
            return self._run_sprint_legacy(
                sprint_number, goal, max_points, task_filter, skip_planning,
            )

        _log(f"[SPRINT] Pipeline has {len(steps)} enabled steps")

        for step in steps:
            if self._stop_requested:
                _log("[SPRINT] Stop requested — aborting pipeline")
                break
            # Rate-limit mid-pipeline: if the builtin task-execution phase wrote
            # a fresh sentinel (Claude's subscription quota hit), skip the
            # remaining cmd/review phases. Build/test verification on a clean
            # tree takes 5–15 min and provides zero useful signal when the
            # task itself never executed — observed sprint #2654 burnt 10 min
            # on cmake+ctest after a rate-limit hit.
            rl_skip = _check_rate_limit_sentinel()
            if rl_skip:
                _log(f"[SPRINT] {rl_skip} — skipping remaining phases")
                break
            self._execute_phase(step)

        # Collect result
        sprint_result = self._run_ctx.get("sprint_result")
        if sprint_result is None:
            # No builtin execution step ran — create a minimal result
            sprint_result = SprintResult(
                sprint_number=sprint_number, goal=goal,
                status="completed", total_duration=time.time() - overall_start,
            )

        sprint_result.total_duration = time.time() - overall_start

        # Drain summary indexer queue — refresh module docstrings, folder
        # _folder_summary.md, and the README auto-section for every file
        # that was edited this sprint. Cheap (only files whose non-comment
        # hash changed actually call the LLM) and runs after the sprint so
        # it doesn't block per-action latency.
        try:
            self._drain_summary_indexer()
        except Exception as exc:
            _log(f"[SUMMARY] indexer drain failed: {exc}")

        _log(f"{'=' * 60}")
        _log(f"[SPRINT] Sprint #{sprint_number} finished in "
             f"{sprint_result.total_duration:.1f}s — {sprint_result.status}")
        _log(f"{'=' * 60}")

        return sprint_result

    def _drain_summary_indexer(self) -> None:
        """If a SummaryIndexer was attached during task execution, drain its
        queue. Each task's AgentProtocol owns its own indexer; we collect
        them via _executors_seen in _execute_single_task."""
        # SprintRunner doesn't keep a single AgentProtocol — each task creates
        # its own. The simplest hook: build an indexer here scoped to the
        # project dir and run a *full walk* limited to files Git considers
        # modified since the sprint start. This keeps the drain self-contained
        # and avoids cross-process state.
        try:
            from summary_indexer import SummaryIndexer
        except ImportError:
            return
        endpoint = (self.provider_config or {}).get(
            "endpoint", "http://localhost:11434"
        )
        # Cheap dedicated summary model — overrideable per provider config.
        model = (self.provider_config or {}).get("summary_model", "llama3.2:3b")
        indexer = SummaryIndexer(
            project_dir=self.project_dir, endpoint=endpoint, model=model,
        )
        # Find files modified since the sprint started (use git status). We
        # only need source files; fall back to "everything Python" if git is
        # unavailable.
        try:
            import subprocess as _sp
            result = _sp.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=str(self.project_dir), timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    # Porcelain format: " M path", "?? path", "AM path", etc.
                    p = line[3:].strip().strip('"')
                    if " -> " in p:
                        p = p.split(" -> ", 1)[1]
                    if p.endswith((".py", ".pyi")):
                        indexer.enqueue(self.project_dir / p)
        except Exception:
            pass
        counts = indexer.drain()
        total = counts["files"] + counts["folders"] + counts["project"]
        if total > 0:
            _log(f"[SUMMARY] refreshed {counts['files']} file / "
                 f"{counts['folders']} folder / {counts['project']} project "
                 f"summary block(s)")

    def _execute_phase(self, step: dict):
        """Dispatch a single pipeline step based on its phase_type.

        Args:
            step: A pipeline_steps row dict with keys including
                  phase_type, name, agent_slug, prompt, description, config_json.
        """
        phase_type = step.get("phase_type", "agent")
        step_name = step.get("name", f"Step {step.get('step_order', '?')}")
        _log(f"[PHASE] === {step_name} (type={phase_type}) ===")

        ctx = self._run_ctx
        sprint_number = ctx["sprint_number"]

        try:
            if phase_type == "agent":
                self._execute_agent_phase(step)
            elif phase_type == "cmd":
                self._execute_cmd_phase(step)
            elif phase_type == "builtin":
                self._execute_builtin_phase(step)
            elif phase_type == "review":
                self._execute_review_phase(step)
            else:
                _log(f"[PHASE] Unknown phase_type '{phase_type}' — skipping")
        except Exception as exc:
            _log(f"[PHASE] {step_name} failed: {exc}")
            traceback.print_exc()
            # Record error but don't abort the entire pipeline
            result = ctx.get("sprint_result")
            if result:
                result.errors.append(f"{step_name} failed: {exc}")

    def _execute_agent_phase(self, step: dict):
        """Run an agent phase via AgentProtocol (e.g. backlog refinement, sprint planning)."""
        ctx = self._run_ctx
        sprint_number = ctx["sprint_number"]
        step_name = step.get("name", "")
        agent_slug = step.get("agent_slug", "")
        prompt = step.get("prompt", "")

        # For planning-type agent steps, use the built-in plan_sprint method
        # if the step name indicates planning and we have that capability
        name_lower = step_name.lower()
        if "planning" in name_lower:
            if ctx.get("skip_planning"):
                _log(f"[PHASE] Skipping {step_name} (--skip-planning)")
                return
            try:
                sprint_info, selected = self.plan_sprint(
                    sprint_number, goal=ctx["goal"],
                    max_points=ctx["max_points"], task_filter=ctx["task_filter"],
                )
                ctx["planning_done"] = True
                if not selected:
                    _log(f"[PHASE] {step_name}: no tasks selected — backlog may be empty")
                    self._stop_requested = True
                    ctx["sprint_result"] = SprintResult(
                        sprint_number=sprint_number, goal=ctx["goal"], status="empty",
                    )
            except Exception as exc:
                _log(f"[PHASE] {step_name} failed: {exc}")
                traceback.print_exc()
                self._stop_requested = True
                ctx["sprint_result"] = SprintResult(
                    sprint_number=sprint_number, goal=ctx["goal"],
                    status="planning_failed", errors=[f"Planning failed: {exc}"],
                )
            return

        # Generic agent phase: run AgentProtocol with the step's prompt
        if not prompt:
            _log(f"[PHASE] {step_name}: no prompt configured — skipping")
            return

        conn = self._get_main_project_conn()
        gconn = self._get_main_global_conn()

        context = load_context(self.project_dir)
        context["sprint_number"] = sprint_number
        context["sprint_goal"] = ctx["goal"]
        context["phase_name"] = step_name

        _pkg_dir = Path(__file__).resolve().parent
        instructions = load_instructions(
            config=self.pipeline_config,
            step=step.get("step_order", 1),
            agent_role=agent_slug or "Developer",
            project_dir=self.project_dir,
            standards_dir=self.standards_dir,
            agents_dir=self.agents_dir or (_pkg_dir / "agents"),
            provider_config=self.provider_config,
        )

        task_dict = {
            "display_id": f"phase-{step.get('step_order', 0)}",
            "title": step_name,
            "description": prompt,
            "acceptance_criteria": step.get("description", ""),
            "complexity": 1,
        }

        task_log_dir = None
        if self.log_dir:
            task_log_dir = self.log_dir / f"sprint_{sprint_number}" / f"phase_{step.get('step_order', 0)}"
            task_log_dir.mkdir(parents=True, exist_ok=True)

        _adapter_type = (self.provider_config or {}).get('provider_type', self.provider)
        protocol = AgentProtocol(
            provider=_adapter_type,
            provider_config=self.provider_config,
            project_dir=self.project_dir,
            max_iterations=self.max_iterations_per_task,
            log_dir=task_log_dir,
            project_db=conn,
            global_db=gconn,
            confirm_before_done=False,
        )

        loop_result = protocol.execute_loop(task_dict, context, instructions)
        success = loop_result.get("success", False)
        _log(f"[PHASE] {step_name}: {'OK' if success else 'FAIL'} "
             f"({loop_result.get('iterations', 0)} iterations)")

    # Patterns in stderr/stdout that indicate a stale or corrupt build directory
    _STALE_BUILD_PATTERNS = (
        'no such file or directory',
        'cannot find path',
        'system cannot find the file',
        'system cannot find the path',
        'ctest configuration: no tests found',
        'unable to find executable',
        'does not exist',
    )

    def _is_stale_build_failure(self, combined_output: str) -> bool:
        """Return True if combined stdout+stderr suggests a stale/corrupt build dir."""
        lower = combined_output.lower()
        return any(pat in lower for pat in self._STALE_BUILD_PATTERNS)

    def _has_stale_project_paths(self, build_dir: str, source_dir: str) -> bool:
        """Check if CTestTestfile.cmake files reference a different project directory.

        This catches the case where a build directory was generated for a
        different (or renamed) project and still contains hardcoded paths
        to the old project location — e.g. Old3DEngine paths in a Nova3D build.
        """
        build_path = Path(build_dir)
        if not build_path.exists():
            return False

        source_path = Path(source_dir).resolve()
        source_name = source_path.name  # e.g. "Nova3D"

        # Resolve the source root once for prefix comparison.
        try:
            source_root = source_path.resolve()
        except Exception:
            source_root = source_path
        source_root_str = str(source_root).replace('\\', '/').rstrip('/').lower()

        for cmake_file in build_path.rglob('CTestTestfile.cmake'):
            try:
                content = cmake_file.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    # Check comment lines too — cmake puts source/build dirs in comments
                    if stripped.startswith('# Source directory:') or stripped.startswith('# Build directory:'):
                        dir_path = stripped.split(':', 1)[1].strip()
                        if not dir_path:
                            continue
                        # Stale only if the referenced path is OUTSIDE the source root.
                        # (A 'tests' subdir legitimately has its own CTestTestfile.cmake
                        #  that references H:/Github/Nova3D/tests — not stale.)
                        try:
                            ref_path = str(Path(dir_path.replace('\\', '/')).resolve()).replace('\\', '/').lower()
                        except Exception:
                            continue
                        if not ref_path.startswith(source_root_str):
                            _log(f'[STALE-CHECK] {cmake_file.name} references "{dir_path}" '
                                 f'which is outside source root {source_root}')
                            return True
                    continue
                # Check include(), subdirs(), and add_test() for paths outside the expected project
                path_cmds = ('include(', 'subdirs(', 'add_test(')
                if any(cmd in stripped for cmd in path_cmds) and '/' in stripped:
                    # Extract the path argument
                    for prefix in ('include("', "include('", 'subdirs("', "subdirs('",
                                   'add_test("', "add_test('"):
                        if prefix in stripped:
                            path_start = stripped.index(prefix) + len(prefix)
                            path_end = stripped.find('"', path_start)
                            if path_end == -1:
                                path_end = stripped.find("'", path_start)
                            if path_end > path_start:
                                ref_path = stripped[path_start:path_end].replace('\\', '/')
                                # Absolute path referencing a different project
                                if ref_path.startswith('/') or (len(ref_path) > 2 and ref_path[1] == ':'):
                                    ref_project = Path(ref_path).parts
                                    src_parts = source_path.parts
                                    # Compare up to common depth — if top-level dirs diverge, it's stale
                                    if len(ref_project) >= 3 and len(src_parts) >= 3:
                                        if ref_project[2].lower() != src_parts[2].lower():
                                            _log(f'[STALE-CHECK] {cmake_file} has absolute path '
                                                 f'referencing "{ref_project[2]}" instead of "{src_parts[2]}"')
                                            return True
        return False

    def _clean_stale_build_dirs(self, source_dir: str) -> list:
        """Find and remove any stale build directories under source_dir.

        Returns a list of directories that were cleaned.
        Checks common build directory names: build, build_new, build_old, cmake-build-*.
        """
        import shutil
        import stat
        source_path = Path(source_dir)
        cleaned = []

        def _force_remove(_func, _path, _exc_info):
            try:
                os.chmod(_path, stat.S_IWRITE)
                _func(_path)
            except Exception:
                pass

        # Common cmake build directory patterns
        build_dir_candidates = ['build_new', 'build_old', 'build_debug', 'build_release']
        for pattern in ['cmake-build-*']:
            cleaned_dirs = list(source_path.glob(pattern))
            build_dir_candidates.extend(d.name for d in cleaned_dirs if d.is_dir())

        for name in build_dir_candidates:
            candidate = source_path / name
            if candidate.is_dir() and self._has_stale_project_paths(str(candidate), source_dir):
                _log(f'[STALE-CLEANUP] Removing stale build dir: {candidate}')
                try:
                    shutil.rmtree(candidate, onexc=_force_remove)
                    cleaned.append(str(candidate))
                    _log(f'[STALE-CLEANUP] Removed: {candidate}')
                except Exception as exc:
                    _log(f'[STALE-CLEANUP] Failed to remove {candidate}: {exc}')

        return cleaned

    def _clean_reconfigure_build(self, build_dir: str, source_dir: str) -> bool:
        """Delete the build directory and run cmake -B to reconfigure from scratch.

        Returns True if the reconfigure succeeded.
        """
        import shutil
        import stat
        build_path = Path(build_dir)
        if not build_path.exists():
            _log(f"[RECONFIGURE] Build dir does not exist: {build_dir}")
            return False

        def _force_remove(_func, _path, _exc_info):
            """Handle read-only files (e.g. .git pack files on Windows)."""
            try:
                os.chmod(_path, stat.S_IWRITE)
                _func(_path)
            except Exception:
                pass

        _log(f"[RECONFIGURE] Removing stale build dir: {build_dir}")
        try:
            shutil.rmtree(build_path, onexc=_force_remove)
        except Exception as exc:
            _log(f"[RECONFIGURE] Failed to remove build dir: {exc}")
            return False

        _log(f"[RECONFIGURE] Running cmake -B {build_dir} in {source_dir}")
        try:
            proc = subprocess.run(
                ['cmake', '-B', build_dir],
                capture_output=True, encoding='utf-8', errors='replace',
                cwd=source_dir, timeout=1800,  # large C++ projects take ~3 min to configure
            )
            if proc.returncode == 0:
                _log('[RECONFIGURE] cmake configure succeeded')
                return True
            else:
                _log(f'[RECONFIGURE] cmake configure failed (exit {proc.returncode})')
                if proc.stderr.strip():
                    for line in proc.stderr.strip().splitlines()[-5:]:
                        _log(f'[RECONFIGURE]   {line}')
                return False
        except Exception as exc:
            _log(f'[RECONFIGURE] cmake configure error: {exc}')
            return False

    def _execute_cmd_phase(self, step: dict):
        """Run a command-line phase (e.g. build verification, test verification, VCS push).

        If a cmake/ctest command fails with signs of a stale build directory
        (missing files, corrupt cache), this method automatically performs a
        clean reconfigure (rm build + cmake -B build) and retries once.
        """
        step_name = step.get("name", "")
        config_str = step.get("config_json", "{}")
        try:
            config = json.loads(config_str) if isinstance(config_str, str) else config_str
        except (json.JSONDecodeError, TypeError):
            config = {}

        # Get command from config_json, falling back to description
        command = config.get("command", "") or step.get("description", "")
        if not command:
            _log(f"[PHASE] {step_name}: no command configured — skipping")
            return

        cwd = config.get("cwd", str(self.project_dir))
        # 5 min was too tight for engine-class projects: sprint #2654 timed out
        # mid-build on Nova3D, then a clean reconfigure took another 5 min and
        # the retry hit the same wall. 10 min covers a full clean configure
        # for typical C++ engines while still bounding runaway builds.
        timeout = config.get("timeout", 600)

        # --- Handle "cd <subdir> && <real_command>" compound commands --------
        # subprocess.run() without shell=True cannot handle "cd X && Y", so
        # we strip the cd prefix, adjust cwd, and run only the real command.
        import re as _re
        cd_match = _re.match(r'^cd\s+(\S+)\s*&&\s*(.+)$', command, _re.IGNORECASE)
        if cd_match:
            cd_target = cd_match.group(1)
            command = cd_match.group(2).strip()
            cwd = str(Path(cwd) / cd_target)
            _log(f'[PHASE] {step_name}: decomposed cd prefix — cwd={cwd}, cmd={command}')

        # --- Pre-flight: proactive stale-path detection for build commands ---
        is_build_cmd = any(kw in command.lower() for kw in ('cmake', 'ctest'))
        if is_build_cmd:
            # Determine build_dir and source_dir from command context.
            # If cwd already IS the build dir (ends with /build or \build),
            # source_dir is its parent. Otherwise build is a child of cwd.
            if cwd.replace('\\', '/').rstrip('/').endswith('/build'):
                pf_build_dir = cwd
                pf_source_dir = str(Path(cwd).parent)
            else:
                pf_build_dir = str(Path(cwd) / 'build')
                pf_source_dir = cwd

            # Check primary build dir for wrong project paths
            if Path(pf_build_dir).exists() and self._has_stale_project_paths(pf_build_dir, pf_source_dir):
                _log(f'[PHASE] {step_name}: pre-flight detected stale project paths in '
                     f'{pf_build_dir} — triggering clean reconfigure')
                self._clean_reconfigure_build(pf_build_dir, pf_source_dir)

            # Also clean any stale secondary build dirs (build_new, build_old, etc.)
            self._clean_stale_build_dirs(pf_source_dir)

        _log(f"[PHASE] {step_name}: running '{command}' in {cwd}")

        try:
            import shlex as _shlex
            cmd_tokens = _shlex.split(command, posix=(os.name != 'nt'))
            proc = subprocess.run(
                cmd_tokens, capture_output=True,
                encoding='utf-8', errors='replace',
                cwd=cwd, timeout=timeout,
            )
            if proc.returncode == 0:
                _log(f"[PHASE] {step_name}: command succeeded (exit 0)")
                if proc.stdout.strip():
                    # Log last few lines of output
                    lines = proc.stdout.strip().splitlines()
                    for line in lines[-5:]:
                        _log(f"[PHASE]   {line}")
                return  # success — done
            else:
                _log(f"[PHASE] {step_name}: command failed (exit {proc.returncode})")
                if proc.stderr.strip():
                    for line in proc.stderr.strip().splitlines()[-10:]:
                        _log(f"[PHASE]   {line}")

                # --- Auto clean-reconfigure on stale build dir ---
                combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
                if is_build_cmd:
                    # Reuse same logic as pre-flight: if cwd IS the build dir,
                    # source is its parent; otherwise build is cwd/build.
                    if cwd.replace('\\', '/').rstrip('/').endswith('/build'):
                        build_dir = cwd
                        source_dir = str(Path(cwd).parent)
                    else:
                        build_dir = str(Path(cwd) / 'build')
                        source_dir = cwd

                    stale_output = self._is_stale_build_failure(combined)
                    stale_paths = self._has_stale_project_paths(build_dir, source_dir)
                    if stale_output or stale_paths:
                        reason = 'stale output patterns' if stale_output else 'wrong project paths'
                        _log(f"[PHASE] {step_name}: detected stale build directory "
                             f"({reason}) — triggering clean reconfigure")
                        if self._clean_reconfigure_build(build_dir, source_dir):
                            _log(f"[PHASE] {step_name}: retrying command after reconfigure")
                            retry_proc = subprocess.run(
                                cmd_tokens, capture_output=True,
                                encoding='utf-8', errors='replace',
                                cwd=cwd, timeout=timeout,
                            )
                            if retry_proc.returncode == 0:
                                _log(f"[PHASE] {step_name}: retry succeeded after reconfigure")
                                if retry_proc.stdout.strip():
                                    for line in retry_proc.stdout.strip().splitlines()[-5:]:
                                        _log(f"[PHASE]   {line}")
                                return  # success on retry
                            else:
                                _log(f"[PHASE] {step_name}: retry also failed "
                                     f"(exit {retry_proc.returncode})")
                                if retry_proc.stderr.strip():
                                    for line in retry_proc.stderr.strip().splitlines()[-10:]:
                                        _log(f"[PHASE]   {line}")

                # Record failure but don't stop the pipeline by default
                fail_action = config.get("on_failure", "warn")
                if fail_action == "stop":
                    self._stop_requested = True
                    result = self._run_ctx.get("sprint_result")
                    if result:
                        result.errors.append(f"{step_name} command failed (exit {proc.returncode})")
        except subprocess.TimeoutExpired:
            _log(f"[PHASE] {step_name}: command timed out after {timeout}s")
        except Exception as exc:
            _log(f"[PHASE] {step_name}: command error: {exc}")

    def _execute_builtin_phase(self, step: dict):
        """Run the builtin task execution loop (parallel + sequential with dependencies)."""
        ctx = self._run_ctx
        sprint_number = ctx["sprint_number"]
        step_name = step.get("name", "").lower()

        # Backlog refinement builtin: auto-generate subtasks from task descriptions
        if "backlog" in step_name or "refinement" in step_name:
            self._builtin_backlog_refinement(sprint_number)
            return

        # If planning hasn't happened yet and not skipped, do it now
        if not ctx.get("planning_done") and not ctx.get("skip_planning"):
            _log("[PHASE] Builtin execution: planning not yet done, running plan_sprint first")
            try:
                sprint_info, selected = self.plan_sprint(
                    sprint_number, goal=ctx["goal"],
                    max_points=ctx["max_points"], task_filter=ctx["task_filter"],
                )
                ctx["planning_done"] = True
                if not selected:
                    _log("[PHASE] No tasks selected — backlog may be empty")
                    ctx["sprint_result"] = SprintResult(
                        sprint_number=sprint_number, goal=ctx["goal"], status="empty",
                    )
                    self._stop_requested = True
                    return
            except Exception as exc:
                _log(f"[PHASE] Auto-planning failed: {exc}")
                traceback.print_exc()
                ctx["sprint_result"] = SprintResult(
                    sprint_number=sprint_number, goal=ctx["goal"],
                    status="planning_failed", errors=[f"Planning failed: {exc}"],
                )
                self._stop_requested = True
                return

        # Guard: only run execute_sprint once. If Sprint Planning already ran it
        # (common when planning is type=builtin), skip re-executing here so we
        # don't overwrite ctx["sprint_result"] with an empty result (0 tasks left).
        if ctx.get("tasks_executed"):
            existing = ctx.get("sprint_result")
            if existing and (existing.task_results or existing.tasks_completed > 0):
                _log("[PHASE] Tasks already executed in planning phase — skipping re-execution")
                return

        try:
            sprint_result = self.execute_sprint(sprint_number)
            ctx["tasks_executed"] = True
            ctx["sprint_result"] = sprint_result
        except Exception as exc:
            _log(f"[PHASE] Task execution failed: {exc}")
            traceback.print_exc()
            ctx["sprint_result"] = SprintResult(
                sprint_number=sprint_number, goal=ctx["goal"],
                status="execution_failed", errors=[f"Execution failed: {exc}"],
            )

    def _generate_tasks_from_goal(self, conn) -> int:
        """Ask the AI to generate a task backlog from the project goal stored in settings.
        Returns number of tasks inserted."""
        from automatica_db import get_project_setting_json, get_project_setting
        from ai_adapters import get_adapter

        # Hierarchical goal fallback: project_goal → pipeline_config.project_goal → global_goal
        goal = get_project_setting(conn, "project_goal", "")
        if not goal:
            pipeline_cfg = get_project_setting_json(conn, "pipeline_config") or {}
            goal = pipeline_cfg.get("project_goal", "")
        if not goal:
            goal = get_project_setting(conn, "global_goal", "")

        if not goal:
            _log("[BACKLOG] No project_goal in settings — cannot auto-generate tasks")
            return 0

        _log(f"[BACKLOG] Backlog is empty. Generating tasks from project goal: {goal[:80]}...")

        # Call the LLM directly (bypass the action/params adapter wrapper which adds
        # too much structure and confuses llama3.2:3b into returning 'add_task' actions).
        prompt = (
            f"You are a senior software engineer and scrum master.\n"
            f"Project goal: {goal}\n\n"
            f"Create a task backlog of 5-8 tasks to build this project.\n"
            f"Order tasks by dependency (foundational first).\n"
            f"Each task is executed by an AI coding agent.\n\n"
            f"Respond ONLY with a JSON array. No markdown. No text outside the array.\n"
            f'Each description must be 1-3 numbered action steps. NO code in descriptions.\n'
            f'Example:\n'
            f'[{{"title":"Create project structure","description":"1. Create requirements.txt listing dependencies\\n2. Create src/__init__.py as empty file","priority":"HIGH","complexity":2}},'
            f'{{"title":"Create game window","description":"1. Create src/main.py with an 800x600 pygame window, black background, 60fps game loop, QUIT event handler","priority":"HIGH","complexity":2}},'
            f'{{"title":"Add bouncing ball","description":"1. Add Ball class to src/ball.py (position, velocity, update, draw)\\n2. Integrate Ball into src/main.py","priority":"HIGH","complexity":3}}]\n\n'
            f"Rules: priority=HIGH/MEDIUM/LOW. complexity=2(1-2 files) or 3(multiple files).\n"
            f"IMPORTANT: Descriptions must NOT contain code. Just plain English instructions."
        )

        # Direct HTTP call to Ollama /api/generate (not the adapter)
        try:
            import urllib.request as _urlreq
            _endpoint = (self.provider_config or {}).get("endpoint", "http://localhost:11434")
            _model = (self.provider_config or {}).get("model", "llama3.2:3b")
            _body = json.dumps({
                "model": _model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 2048, "temperature": 0.2},
            }).encode()
            _req = _urlreq.Request(
                f"{_endpoint}/api/generate",
                data=_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlreq.urlopen(_req, timeout=60) as _resp:
                _data = json.loads(_resp.read().decode())
                raw = _data.get("response", "")
        except Exception as exc:
            _log(f"[BACKLOG] Ollama HTTP call failed: {exc}")
            return 0

        # Extract JSON array from raw response
        import re as _re
        tasks_data = []
        # Try to find a JSON array anywhere in the response
        for m in _re.finditer(r'\[[\s\S]+\]', raw):
            try:
                parsed = json.loads(m.group())
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    tasks_data = parsed
                    break
            except Exception:
                pass
        # Also try parsing the full response as a JSON array
        if not tasks_data:
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, list):
                    tasks_data = parsed
            except Exception:
                pass

        if not tasks_data:
            _log(f"[BACKLOG] Failed to parse task list from AI response")
            _log(f"[BACKLOG] Raw response (first 300 chars): {raw[:300]}")
            return 0

        if not isinstance(tasks_data, list) or not tasks_data:
            _log(f"[BACKLOG] AI returned empty or invalid task list")
            return 0

        inserted = 0
        for i, task in enumerate(tasks_data[:10]):
            if not isinstance(task, dict) or not task.get("title"):
                continue
            display_id = f"TASK-{i+1:03d}"
            # Avoid duplicate display_ids
            existing = conn.execute("SELECT id FROM tasks WHERE display_id=?", (display_id,)).fetchone()
            if existing:
                display_id = f"TASK-G{i+1:03d}"
            try:
                # Complexity must be a valid Fibonacci-style point value
                _VALID_COMPLEXITY = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)
                raw_complexity = int(task.get("complexity", 3))
                complexity = min(_VALID_COMPLEXITY, key=lambda v: abs(v - raw_complexity))
                conn.execute(
                    "INSERT INTO tasks (display_id, title, priority, complexity, description, status) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        display_id,
                        task.get("title", "Untitled")[:200],
                        task.get("priority", "MEDIUM").upper(),
                        complexity,
                        task.get("description", ""),
                        "backlog",
                    )
                )
                inserted += 1
                _log(f"[BACKLOG] Generated: {display_id} — {task.get('title', '')[:60]}")
            except Exception as exc:
                _log(f"[BACKLOG] Failed to insert task {i}: {exc}")

        conn.commit()
        _log(f"[BACKLOG] Auto-generated {inserted} tasks from project goal")
        return inserted

    def _builtin_backlog_refinement(self, sprint_number: int):
        """Auto-generate subtasks for backlog tasks; generate tasks from goal if backlog empty."""
        conn = self._get_main_project_conn()

        # If no tasks exist at all, try to generate from project goal
        total_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if total_tasks == 0:
            generated = self._generate_tasks_from_goal(conn)
            if generated == 0:
                _log("[BACKLOG] No tasks generated — project will be empty")
                return

        tasks = conn.execute(
            "SELECT id, display_id, title, description, complexity "
            "FROM tasks WHERE status IN ('backlog','sprint_planned') ORDER BY id"
        ).fetchall()

        SUBTASK_TEMPLATES = {
            1: ["Set up basic structure", "Verify with a smoke test"],
            2: ["Implement core logic", "Handle edge cases", "Verify output"],
            3: ["Implement core logic", "Handle edge cases", "Write integration check", "Verify output"],
            5: ["Design approach", "Implement core logic", "Handle edge cases", "Integration check", "Verify and document"],
        }

        added = 0
        for task in tasks:
            task_id = dict(task)["display_id"]
            complexity = dict(task).get("complexity", 3)
            title = dict(task).get("title", "")

            # Pick template based on complexity
            template_key = min(SUBTASK_TEMPLATES.keys(), key=lambda k: abs(k - complexity))
            subtask_titles = SUBTASK_TEMPLATES[template_key]

            # Check existing subtasks to avoid duplicates
            existing = conn.execute(
                "SELECT COUNT(*) FROM subtasks WHERE task_id = (SELECT id FROM tasks WHERE display_id = ?)",
                (task_id,)
            ).fetchone()[0]
            if existing >= 2:
                continue  # Already refined

            task_row = conn.execute("SELECT id FROM tasks WHERE display_id = ?", (task_id,)).fetchone()
            if not task_row:
                continue
            for i, sub_title in enumerate(subtask_titles):
                try:
                    # display_id is auto-set by trigger on subtasks table
                    conn.execute("""
                        INSERT INTO subtasks (display_id, task_id, title, status, sort_order)
                        VALUES ('TEMP', ?, ?, 'todo', ?)
                    """, (task_row[0], f"{sub_title}: {title[:40]}", i))
                    added += 1
                except Exception:
                    pass

        conn.commit()
        _log(f"[BACKLOG] Auto-generated {added} subtasks for {len(tasks)} tasks")

    def replenish_backlog_from_review(self, conn) -> int:
        """When the backlog has emptied, review the current project and propose
        a fresh batch of follow-on tasks: missing unit tests, refactor / SOLID
        opportunities, performance hotspots, and polish items. Inserts the
        proposed tasks into the project DB and returns the insert count.

        Used by run_autonomous to keep the loop alive — without this an
        Ollama-only loop terminates the moment its first auto-generated batch
        finishes, which isn't 'continuous through the day'.
        """
        from automatica_db import get_project_setting_json, get_project_setting

        # Pull the goal so the LLM has the same orientation as a first-pass
        # generation, plus a project file census so it can ground its
        # suggestions in what actually exists.
        goal = (get_project_setting(conn, "project_goal", "") or "").strip()
        if not goal:
            pipeline_cfg = get_project_setting_json(conn, "pipeline_config") or {}
            goal = (pipeline_cfg.get("project_goal") or "").strip()
        if not goal:
            goal = (get_project_setting(conn, "global_goal", "") or "").strip()

        # File census — list project files (already filters out venv/build/etc
        # via project_scanner.SKIP_DIRS). Group by extension so the LLM sees
        # the codebase shape without us flooding tokens with every path.
        try:
            from project_scanner import list_project_files
            files = list_project_files(self.project_dir) or []
        except Exception:
            files = []
        # Skip empty / lockfile / generated entries
        files = [f for f in files if f.get("size_bytes", 0) > 0
                 and not f.get("path", "").endswith(".lock")]
        sample_paths = [f["path"] for f in files][:30]

        # Pull a small sample of source-file content so the review is grounded
        # in real code, not just paths. Cap at 8 files / 800 chars each so the
        # prompt stays well below local-model context limits.
        snippets: list[str] = []
        for f in files[:8]:
            p = self.project_dir / f["path"]
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            snippets.append(f"### {f['path']}\n{txt[:800]}")

        # Pull recent done tasks so the LLM doesn't propose work already done.
        recent_done = [dict(r) for r in conn.execute(
            "SELECT display_id, title FROM tasks WHERE status='done' "
            "ORDER BY completed_at DESC NULLS LAST LIMIT 12"
        ).fetchall()]
        done_summary = "\n".join(
            f"- {t['display_id']}: {t['title']}" for t in recent_done
        ) or "(none)"

        prompt = (
            "You are a senior software engineer reviewing an autonomous "
            "project after its initial backlog has been completed.\n\n"
            f"Project goal: {goal or '(no goal set)'}\n\n"
            f"Recently completed tasks:\n{done_summary}\n\n"
            f"Project file census ({len(files)} files; first 30):\n  "
            + "\n  ".join(sample_paths) + "\n\n"
            f"Code samples:\n\n" + "\n\n".join(snippets) + "\n\n"
            "Propose 5-8 NEW tasks across these categories (mix them — don't "
            "stack the list with one type):\n"
            "  • Unit tests for under-tested modules\n"
            "  • Refactors that apply SOLID principles "
            "(single-responsibility splits, dependency inversion, etc.)\n"
            "  • Extract common helpers / dedupe logic\n"
            "  • Performance: tighten hot loops, avoid per-frame allocations\n"
            "  • Polish: error handling, docstrings, small UX wins\n"
            "  • Screenshot/visual review if the app has a UI worth checking\n\n"
            "Constraints:\n"
            "  - DO NOT repeat the recently-completed tasks above.\n"
            "  - Each task must reference a real file/path from the census.\n"
            "  - Keep titles concrete (\"Refactor Paddle.update to extract "
            "AI-tracking helper\" not \"Improve paddle\").\n"
            "  - Each description: 1-3 numbered action steps in plain English. "
            "NO code in descriptions.\n\n"
            "Respond ONLY with a JSON array. No markdown. No prose.\n"
            'Example: [{"title":"Add unit tests for src/ball.py update()",'
            '"description":"1. Create tests/test_ball.py\\n2. Cover '
            'horizontal/vertical bounce paths","priority":"HIGH",'
            '"complexity":2}]\n'
            "Rules: priority=HIGH/MEDIUM/LOW. complexity in {2,3,5,8}."
        )

        try:
            import urllib.request as _urlreq
            _endpoint = (self.provider_config or {}).get("endpoint", "http://localhost:11434")
            _model = (self.provider_config or {}).get("model", "llama3.2:3b")
            _body = json.dumps({
                "model": _model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 2048, "temperature": 0.3},
            }).encode()
            _req = _urlreq.Request(
                f"{_endpoint}/api/generate",
                data=_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlreq.urlopen(_req, timeout=180) as _resp:
                _data = json.loads(_resp.read().decode())
                raw = _data.get("response", "")
        except Exception as exc:
            _log(f"[REPLENISH] Ollama HTTP call failed: {exc}")
            return 0

        # Parse the JSON array from the response (same strategy as
        # _generate_tasks_from_goal).
        import re as _re
        tasks_data: list = []
        for m in _re.finditer(r'\[[\s\S]+\]', raw):
            try:
                parsed = json.loads(m.group())
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    tasks_data = parsed
                    break
            except Exception:
                pass
        if not tasks_data:
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, list):
                    tasks_data = parsed
            except Exception:
                pass

        if not tasks_data:
            _log("[REPLENISH] Failed to parse review task list from AI response")
            _log(f"[REPLENISH] Raw response (first 300 chars): {raw[:300]}")
            return 0

        # Pick the next available TASK-### display_id by scanning existing.
        existing_ids = {
            row[0] for row in conn.execute(
                "SELECT display_id FROM tasks WHERE display_id LIKE 'TASK-%'"
            ).fetchall()
        }
        next_idx = 1
        def _next_display_id() -> str:
            nonlocal next_idx
            while True:
                did = f"TASK-{next_idx:03d}"
                next_idx += 1
                if did not in existing_ids:
                    existing_ids.add(did)
                    return did

        _VALID_COMPLEXITY = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)
        inserted = 0
        for task in tasks_data[:10]:
            if not isinstance(task, dict) or not task.get("title"):
                continue
            try:
                raw_complexity = int(task.get("complexity", 3))
            except (TypeError, ValueError):
                raw_complexity = 3
            complexity = min(_VALID_COMPLEXITY, key=lambda v: abs(v - raw_complexity))
            display_id = _next_display_id()
            try:
                conn.execute(
                    "INSERT INTO tasks (display_id, title, priority, complexity, "
                    "description, status) VALUES (?,?,?,?,?,?)",
                    (
                        display_id,
                        str(task.get("title", "Untitled"))[:200],
                        str(task.get("priority", "MEDIUM")).upper(),
                        complexity,
                        str(task.get("description", "")),
                        "backlog",
                    ),
                )
                inserted += 1
                _log(f"[REPLENISH] Added: {display_id} — {str(task.get('title',''))[:60]}")
            except Exception as exc:
                _log(f"[REPLENISH] Failed to insert task: {exc}")

        conn.commit()
        _log(f"[REPLENISH] Backlog refilled with {inserted} review-driven task(s)")
        return inserted

    def _execute_review_phase(self, step: dict):
        """Run a review/retrospective phase via AgentProtocol or built-in review."""
        ctx = self._run_ctx
        sprint_number = ctx["sprint_number"]
        sprint_result = ctx.get("sprint_result")
        step_name = step.get("name", "")

        if sprint_result is None:
            _log(f"[PHASE] {step_name}: no sprint result available — skipping")
            return

        # Use built-in review_sprint for the main sprint review
        name_lower = step_name.lower()
        if "review" in name_lower and "retro" not in name_lower:
            try:
                self.review_sprint(sprint_number, sprint_result)
            except Exception as exc:
                _log(f"[PHASE] {step_name} failed: {exc}")
                sprint_result.errors.append(f"{step_name} failed: {exc}")
            return

        # For retrospectives or other review-type steps, run via AgentProtocol
        prompt = step.get("prompt", "")
        if not prompt:
            _log(f"[PHASE] {step_name}: no prompt configured — skipping")
            return

        agent_slug = step.get("agent_slug", "")
        conn = self._get_main_project_conn()
        gconn = self._get_main_global_conn()

        # Enrich context with sprint result data + audit context
        context = load_context(self.project_dir)
        context["sprint_number"] = sprint_number
        context["sprint_goal"] = ctx["goal"]
        context["phase_name"] = step_name

        # For audit phases, include comprehensive audit data
        if "audit" in step_name.lower():
            try:
                from automatica_db import build_sprint_audit_context
                audit = build_sprint_audit_context(conn, sprint_number)
                context["audit"] = {
                    'files_modified': audit.get('files_modified', []),
                    'action_counts': audit.get('action_counts', {}),
                    'error_patterns': audit.get('error_patterns', [])[:10],
                    'prompt_count': audit.get('prompt_count', 0),
                    'error_count': audit.get('error_count', 0),
                    'completed': audit.get('completed_count', 0),
                    'failed': audit.get('failed_count', 0),
                }
            except Exception:
                pass

        context["sprint_result_summary"] = {
            "tasks_completed": sprint_result.tasks_completed,
            "tasks_failed": sprint_result.tasks_failed,
            "tasks_blocked": sprint_result.tasks_blocked,
            "points_completed": sprint_result.points_completed,
            "points_planned": sprint_result.points_planned,
            "velocity": round(sprint_result.velocity, 2),
            "errors": sprint_result.errors[:10],
        }

        _pkg_dir = Path(__file__).resolve().parent
        instructions = load_instructions(
            config=self.pipeline_config,
            step=step.get("step_order", 6),
            agent_role=agent_slug or "scrum-master",
            project_dir=self.project_dir,
            standards_dir=self.standards_dir,
            agents_dir=self.agents_dir or (_pkg_dir / "agents"),
            provider_config=self.provider_config,
        )

        task_dict = {
            "display_id": f"review-{step.get('step_order', 0)}",
            "title": step_name,
            "description": prompt,
            "acceptance_criteria": step.get("description", ""),
            "complexity": 1,
        }

        task_log_dir = None
        if self.log_dir:
            task_log_dir = self.log_dir / f"sprint_{sprint_number}" / f"phase_{step.get('step_order', 0)}"
            task_log_dir.mkdir(parents=True, exist_ok=True)

        _adapter_type = (self.provider_config or {}).get('provider_type', self.provider)
        protocol = AgentProtocol(
            provider=_adapter_type,
            provider_config=self.provider_config,
            project_dir=self.project_dir,
            max_iterations=min(self.max_iterations_per_task, 10),  # reviews should be brief
            log_dir=task_log_dir,
            project_db=conn,
            global_db=gconn,
            confirm_before_done=False,
        )

        loop_result = protocol.execute_loop(task_dict, context, instructions)
        success = loop_result.get("success", False)
        _log(f"[PHASE] {step_name}: {'OK' if success else 'FAIL'} "
             f"({loop_result.get('iterations', 0)} iterations)")

    def _run_sprint_legacy(
        self,
        sprint_number: int,
        goal: str = "",
        max_points: int = 40,
        task_filter: Optional[Dict[str, Any]] = None,
        skip_planning: bool = False,
    ) -> SprintResult:
        """Legacy hardcoded sprint run — used as fallback when no DB pipeline steps exist."""
        overall_start = time.time()

        # Phase 1: Planning
        if not skip_planning:
            try:
                sprint_info, selected = self.plan_sprint(
                    sprint_number, goal=goal,
                    max_points=max_points, task_filter=task_filter,
                )
                if not selected:
                    _log("[SPRINT] No tasks selected — backlog may be empty")
                    return SprintResult(
                        sprint_number=sprint_number, goal=goal,
                        status="empty", total_duration=time.time() - overall_start,
                    )
            except Exception as exc:
                _log(f"[SPRINT] Planning failed: {exc}")
                traceback.print_exc()
                return SprintResult(
                    sprint_number=sprint_number, goal=goal,
                    status="planning_failed",
                    total_duration=time.time() - overall_start,
                    errors=[f"Planning failed: {exc}"],
                )
        else:
            _log(f"[SPRINT] Skipping planning, using existing sprint #{sprint_number} tasks")

        # Phase 2: Execution
        try:
            sprint_result = self.execute_sprint(sprint_number)
        except Exception as exc:
            _log(f"[SPRINT] Execution failed: {exc}")
            traceback.print_exc()
            sprint_result = SprintResult(
                sprint_number=sprint_number, goal=goal,
                status="execution_failed",
                total_duration=time.time() - overall_start,
                errors=[f"Execution failed: {exc}"],
            )

        sprint_result.total_duration = time.time() - overall_start

        # Phase 3: Review
        try:
            self.review_sprint(sprint_number, sprint_result)
        except Exception as exc:
            _log(f"[SPRINT] Review failed: {exc}")
            sprint_result.errors.append(f"Review failed: {exc}")

        return sprint_result

    # -- Status / Query -----------------------------------------------------

    def stop(self):
        """Request graceful stop of the current sprint pipeline."""
        self._stop_requested = True

    def get_active_tasks(self) -> Dict[str, str]:
        """Return snapshot of currently active tasks."""
        with self._lock:
            return dict(self._active_tasks)

    def close(self):
        """Close main-thread DB connections."""
        if self._project_conn:
            self._project_conn.close()
            self._project_conn = None
        if self._global_conn:
            self._global_conn.close()
            self._global_conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str):
    """Print a timestamped log message to stderr. Strips non-ASCII to stay cp1252-safe."""
    ts = datetime.now().strftime("%H:%M:%S")
    safe = str(msg).encode("ascii", errors="replace").decode("ascii")
    print(f"[{ts}] {safe}", file=sys.stderr, flush=True)


def _next_sprint_number(conn: sqlite3.Connection, *,
                        reuse_zero_velocity: bool = True) -> int:
    """Determine the sprint number for the next run.

    By default (``reuse_zero_velocity=True``), if the most recent sprint
    completed zero tasks (typical of a rate-limit / retry-cap / config
    failure where the cycle burned a sprint number without making any
    real progress), reuse that same sprint number so the work-in-flight
    isn't fragmented across N sprint records that achieved nothing.

    With ``reuse_zero_velocity=False`` (legacy behaviour), always advance
    to ``MAX(sprint_number) + 1``.

    Either way, gaps left by crashed runs are skipped.
    """
    row = conn.execute("SELECT MAX(sprint_number) FROM sprints").fetchone()
    last_num = (row[0] or 0) if row else 0

    if reuse_zero_velocity and last_num > 0:
        # Did the most recent sprint actually complete any tasks?
        done = conn.execute(
            "SELECT COUNT(*) FROM sprint_tasks st "
            "JOIN sprints s ON s.id = st.sprint_id "
            "WHERE s.sprint_number = ? AND st.status = 'done'",
            (last_num,),
        ).fetchone()
        if (done[0] if done else 0) == 0:
            # Zero-velocity prior sprint — reuse the number rather than
            # burning a new one. The next planning pass re-pulls eligible
            # tasks (typically with bumped complexity from the heal step).
            return last_num

    candidate = last_num + 1 if last_num else 1
    while conn.execute(
        "SELECT 1 FROM sprints WHERE sprint_number = ?", (candidate,)
    ).fetchone():
        candidate += 1
    return candidate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sprint Runner — Execute a full sprint lifecycle.",
    )
    parser.add_argument("--project-dir", required=True,
                        help="Root directory of the project")
    parser.add_argument("--sprint-number", type=int, default=None,
                        help="Sprint number (auto-increments if not given)")
    parser.add_argument("--goal", default="",
                        help="Sprint goal description")
    parser.add_argument("--provider", required=True,
                        help="AI provider name — must match a configured provider in the DB")
    parser.add_argument("--max-parallel", type=int, default=3,
                        help="Max parallel task threads (default: 3)")
    parser.add_argument("--max-points", type=int, default=40,
                        help="Max story points to pull into sprint (default: 40)")
    parser.add_argument("--max-iterations", type=int, default=50,
                        help="Max agent loop iterations per task (default: 50)")
    parser.add_argument("--settings", default=None,
                        help="Path to settings.json")
    parser.add_argument("--config", default=None,
                        help="Path to pipeline-config.json")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for sprint execution logs")
    parser.add_argument("--standards-dir", default=None,
                        help="Directory containing coding standards")
    parser.add_argument("--agents-dir", default=None,
                        help="Directory containing agent definition .md files")
    parser.add_argument("--skip-planning", action="store_true",
                        help="Skip planning, execute existing sprint tasks")
    parser.add_argument("--filter-max-complexity", type=int, default=None,
                        help="Only select tasks up to this complexity")
    parser.add_argument("--filter-priority", nargs="+", default=None,
                        help="Only select tasks with these priorities")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only, do not execute tasks")
    parser.add_argument("--output", default=None,
                        help="Write JSON result to this file")

    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(json.dumps({"success": False, "error": f"Project dir not found: {project_dir}"}))
        sys.exit(1)

    _pkg_dir = Path(__file__).resolve().parent

    # Load settings and provider config
    settings_path = Path(args.settings) if args.settings else (_pkg_dir / "settings.json")
    settings = load_settings(settings_path)
    # Use global DB for provider config (authoritative); fall back to settings.json
    _global_db_path = Path.home() / ".automatica" / "automatica.db"
    _gdb_for_cfg = None
    try:
        import sqlite3 as _sq3c
        _gdb_for_cfg = _sq3c.connect(str(_global_db_path))
        _gdb_for_cfg.row_factory = _sq3c.Row
    except Exception:
        pass
    provider_config = get_provider_config(_gdb_for_cfg or settings, args.provider)
    if _gdb_for_cfg:
        _gdb_for_cfg.close()

    # Load pipeline config — prefer project DB, fall back to file
    pipeline_config = {}
    try:
        from automatica_db import init_project_db as _ipdb, get_project_setting_json
        _tmp_conn = _ipdb(project_dir)
        db_pipeline = get_project_setting_json(_tmp_conn, "pipeline_config")
        if db_pipeline:
            pipeline_config = db_pipeline
            _log("[CLI] Loaded pipeline_config from project DB")
        _tmp_conn.close()
    except Exception:
        pass
    if not pipeline_config:
        config_path = Path(args.config) if args.config else (_pkg_dir / "config" / "pipeline-config.json")
        if config_path.is_file():
            try:
                pipeline_config = json.loads(config_path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                pass

    # WF-6: validate pipeline_config.provider on load — warn loudly when missing
    # so misconfigured runs without a CLI override are caught immediately
    # instead of silently picking up the wrong provider.
    _cfg_provider = (pipeline_config.get("provider") or "").strip() if pipeline_config else ""
    if pipeline_config and not _cfg_provider:
        _msg = (f"[WARN] pipeline_config.provider is unset for project '{project_dir.name}'. "
                f"Using --provider={args.provider} as fallback. "
                f"Fix with: python run_autonomous.py --SetPipelineProvider <name> --Project {project_dir}")
        _log(_msg)
        # Backfill the in-memory pipeline_config so downstream code sees a value
        pipeline_config["provider"] = args.provider

    # Resolve directories
    log_dir = Path(args.log_dir) if args.log_dir else (_pkg_dir / "logs" / "sprints")
    standards_dir = Path(args.standards_dir) if args.standards_dir else None
    agents_dir = Path(args.agents_dir) if args.agents_dir else None

    # Determine sprint number
    sprint_number = args.sprint_number
    if sprint_number is None:
        conn = init_project_db(project_dir)
        sprint_number = _next_sprint_number(conn)
        conn.close()

    # Build task filter
    task_filter = {}
    if args.filter_max_complexity is not None:
        task_filter["max_complexity"] = args.filter_max_complexity
    if args.filter_priority:
        task_filter["priority"] = args.filter_priority

    _log(f"[CLI] Provider={args.provider} Sprint=#{sprint_number} "
         f"MaxParallel={args.max_parallel} MaxPoints={args.max_points}")
    _log(f"[CLI] Project={project_dir}")

    # Create runner
    runner = SprintRunner(
        project_dir=project_dir,
        provider=args.provider,
        provider_config=provider_config,
        max_parallel=args.max_parallel,
        max_iterations_per_task=args.max_iterations,
        log_dir=log_dir,
        pipeline_config=pipeline_config,
        standards_dir=standards_dir,
        agents_dir=agents_dir,
    )

    try:
        if args.dry_run:
            # Plan only
            sprint_info, selected = runner.plan_sprint(
                sprint_number, goal=args.goal,
                max_points=args.max_points, task_filter=task_filter or None,
            )
            output = {
                "mode": "dry_run",
                "sprint": sprint_info,
                "tasks_selected": [
                    {"display_id": t["display_id"], "title": t["title"],
                     "complexity": t.get("complexity", 3), "priority": t.get("priority", "MEDIUM")}
                    for t in selected
                ],
                "total_points": sum(t.get("complexity", 3) for t in selected),
            }
            print(json.dumps(output, indent=2))
        else:
            # Full sprint
            result = runner.run_sprint(
                sprint_number=sprint_number,
                goal=args.goal,
                max_points=args.max_points,
                task_filter=task_filter or None,
                skip_planning=args.skip_planning,
            )
            output = result.to_dict()
            print(json.dumps(output, indent=2))

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(output, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _log(f"[CLI] Result written to {output_path}")

            sys.exit(0 if result.status in ("completed", "partial") else 1)
    except KeyboardInterrupt:
        _log("[CLI] Interrupted by user")
        sys.exit(130)
    except Exception as exc:
        _log(f"[CLI] Fatal error: {exc}")
        traceback.print_exc()
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
    finally:
        runner.close()


if __name__ == "__main__":
    main()
