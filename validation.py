#!/usr/bin/env python3
"""
AIxYad Action Validation & Transaction Rollback.

Validates every AI action against the schema before execution.
Tracks file modifications for rollback on failure.
Bumps task complexity when agents fail repeatedly.
"""

import json
import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from datetime import datetime


# ── WF-4: failure classification ─────────────────────────────────────────────
# Classify task-failure reasons so the loop can react appropriately. Transient
# errors (network, connection-reset, rate-limit) shouldn't burn retry budget;
# refusals (Claude declining, "I cannot") shouldn't loop forever; build/test
# failures get counted normally toward the cap.

_TRANSIENT_PATTERNS = [
    r'connection (refused|reset|aborted)', r'connectionerror', r'timeout(error)?',
    r'timed out', r'temporarily unavailable', r'rate limit', r'rate[-_]?limited',
    r'429', r"hit your limit",  # Claude CLI subscription-quota message
    r'service unavailable', r'502 bad gateway', r'503 ',
    r'network is unreachable', r'name resolution', r'dns',
    r'broken pipe',
]
_REFUSAL_PATTERNS = [
    r"\bi (can|could) ?not\b", r"\bcannot\b", r"\bcan'?t (do|help|complete|implement)\b",
    r'unable to (proceed|complete|implement|do)', r"won'?t (do|help)",
    r"i (don'?t|do not) have (the )?(necessary|enough|sufficient)",
    r'declines? to', r'refuses? to', r'against (my|the) (guidelines|policy)',
    r'requires (more|additional) (information|context|access)',
]
_BUILD_PATTERNS = [
    r'cmake (--build|--fresh|configure)', r'compilation failed',
    r'CMakeLists\.txt', r'make: \*\*\*', r'msbuild', r'cl\.exe', r'ld\.exe',
    r'undefined reference', r'cannot find -l', r'fatal error',
]
_TEST_PATTERNS = [
    r'pytest', r'ctest', r'gtest', r'failed assertion',
    r'AssertionError', r'TestCase', r'\d+ (test|tests) failed',
]

def _classify_failure(reason: str) -> str:
    """Return one of: transient | refusal | build | test | unknown."""
    if not reason:
        return 'unknown'
    s = reason.lower()
    for p in _TRANSIENT_PATTERNS:
        if re.search(p, s):
            return 'transient'
    for p in _REFUSAL_PATTERNS:
        if re.search(p, s):
            return 'refusal'
    for p in _BUILD_PATTERNS:
        if re.search(p, s):
            return 'build'
    for p in _TEST_PATTERNS:
        if re.search(p, s):
            return 'test'
    return 'unknown'


# ═══════════════════════════════════════════════════════════════════
# Action Schema Validation
# ═══════════════════════════════════════════════════════════════════

# Required params per action (None = no required params)
ACTION_SCHEMA = {
    # File operations
    'read_file':        {'required': ['path'], 'optional': ['offset', 'limit']},
    'write_file':       {'required': ['path', 'content'], 'optional': ['overwrite']},
    'edit_file':        {'required': ['path', 'old_text', 'new_text'], 'optional': []},
    'search':           {'required': ['pattern'], 'optional': ['path', 'glob', 'max_matches']},
    'list_files':       {'required': ['pattern'], 'optional': ['path']},
    'run_command':      {'required': ['command'], 'optional': ['timeout']},
    'run_script':       {'required': ['script'], 'optional': ['args']},
    'commit':           {'required': ['files', 'message'], 'optional': []},
    'step_done':        {'required': ['summary'], 'optional': ['files_changed']},
    'need_help':        {'required': ['reason'], 'optional': ['escalate_to']},

    # DB task operations
    'query_tasks':      {'required': [], 'optional': ['status', 'priority', 'complexity', 'agent', 'limit']},
    'query_task_detail': {'required': ['task_id'], 'optional': []},
    'update_task':      {'required': ['task_id'], 'optional': ['status', 'priority', 'complexity', 'agent_slug', 'description', 'blocked_reason']},
    'add_task':         {'required': ['title'], 'optional': ['description', 'priority', 'complexity', 'agent_slug', 'parent_id', 'acceptance_criteria']},
    'reorder_tasks':    {'required': ['task_id', 'position'], 'optional': []},
    'add_checklist_item': {'required': ['task_id', 'title'], 'optional': []},
    'check_item':       {'required': ['item_id'], 'optional': ['checked']},

    # Sprint operations
    'query_sprint':     {'required': [], 'optional': ['sprint_number']},
    'query_sprint_summary': {'required': [], 'optional': ['sprint_number']},
    'query_history':    {'required': [], 'optional': ['task_id', 'actor', 'session_id', 'limit']},
    'query_task_timeline': {'required': ['task_id'], 'optional': []},

    # File linking
    'link_file_to_task': {'required': ['task_id', 'file_path'], 'optional': ['relationship', 'notes']},
    'log_file_modification': {'required': ['task_id', 'file_path', 'action'], 'optional': ['before_snippet', 'after_snippet']},

    # Reservation
    'reserve_task':     {'required': ['task_id'], 'optional': ['agent']},
    'release_task':     {'required': ['task_id'], 'optional': []},
    'assign_task':      {'required': ['task_id', 'agent'], 'optional': ['provider']},
    'reserve_subtask':  {'required': ['subtask_id'], 'optional': ['agent']},

    # Discard/restore
    'discard_task':     {'required': ['task_id'], 'optional': ['reason']},
    'restore_task':     {'required': ['task_id'], 'optional': ['restore_to']},
    'discard_file':     {'required': ['file_path'], 'optional': ['reason']},
    'restore_file':     {'required': [], 'optional': ['discard_id', 'original_path']},
    'query_discarded_files': {'required': [], 'optional': ['path', 'since', 'until']},
    'query_file_history': {'required': ['file_path'], 'optional': []},

    # Scanner
    'query_file_structure': {'required': ['file_path'], 'optional': []},
    'query_symbols':    {'required': [], 'optional': ['name', 'symbol_type', 'file_glob']},
    'query_files':      {'required': [], 'optional': ['extension', 'path_contains']},
    'query_file_section': {'required': ['file_path'], 'optional': ['line_start', 'line_end', 'section_name']},
    # In-source summary index (summary_indexer.py)
    'get_summary':      {'required': ['path'], 'optional': []},
    'find_relevant_files': {'required': ['query'], 'optional': ['top_k']},
    # File ops (copy + soft-delete with backup)
    'copy_file':        {'required': ['src', 'dst'], 'optional': ['overwrite']},
    # take_note: persistent per-task scratchpad
    'take_note':        {'required': ['text'], 'optional': ['mode', 'task_id']},

    # GUID sections
    'create_section':   {'required': ['description', 'section_type'], 'optional': ['file_path', 'line_start', 'content', 'persistent_id']},
    'edit_section':     {'required': ['guid'], 'optional': ['new_content', 'new_description']},
    'find_section':     {'required': [], 'optional': ['guid', 'search', 'file_path', 'section_type']},
    'get_section_history': {'required': ['guid'], 'optional': []},

    # File locking
    'lock_file':        {'required': ['file_path'], 'optional': ['agent']},
    'unlock_file':      {'required': ['file_path'], 'optional': []},
    'check_file_lock':  {'required': ['file_path'], 'optional': []},

    # Prompt logs
    'query_prompt_logs': {'required': [], 'optional': ['session_id', 'task_id', 'provider', 'limit']},
}

# Valid enum values for validation
VALID_STATUS = {'backlog', 'sprint_planned', 'in_progress', 'in_review', 'done', 'blocked', 'archived'}
VALID_PRIORITY = {'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'}
VALID_COMPLEXITY = {1, 2, 3, 5, 8, 13, 21, 34, 55, 89}
VALID_RELATIONSHIP = {'related', 'modified', 'created', 'reviewed', 'depends_on', 'output'}
VALID_SECTION_TYPES = {'function', 'method', 'class', 'struct', 'enum', 'namespace', 'section', 'param', 'config', 'block'}


def validate_action(action: str, params: dict) -> Optional[str]:
    """Validate an action against its schema.
    Returns None if valid, or an error message string if invalid."""

    if action not in ACTION_SCHEMA:
        return f"Unknown action '{action}'. Valid actions: {', '.join(sorted(ACTION_SCHEMA.keys()))}"

    schema = ACTION_SCHEMA[action]

    # Check required params
    for req in schema['required']:
        val = params.get(req)
        if val is None:
            return f"Action '{action}' requires param '{req}'"
        # Allow empty string for 'content' (e.g. writing an empty __init__.py)
        if val == '' and req != 'content':
            return f"Action '{action}' requires param '{req}'"

    # Validate enum values where applicable
    if 'status' in params and params['status']:
        if params['status'] not in VALID_STATUS:
            return f"Invalid status '{params['status']}'. Valid: {', '.join(sorted(VALID_STATUS))}"

    if 'priority' in params and params['priority']:
        if params['priority'] not in VALID_PRIORITY:
            return f"Invalid priority '{params['priority']}'. Valid: {', '.join(sorted(VALID_PRIORITY))}"

    if 'complexity' in params and params['complexity'] is not None:
        try:
            c = int(params['complexity'])
            if c not in VALID_COMPLEXITY:
                return f"Invalid complexity {c}. Must be Fibonacci: {sorted(VALID_COMPLEXITY)}"
        except (ValueError, TypeError):
            return f"Complexity must be an integer. Got: {params['complexity']}"

    if 'relationship' in params and params['relationship']:
        if params['relationship'] not in VALID_RELATIONSHIP:
            return f"Invalid relationship '{params['relationship']}'. Valid: {', '.join(sorted(VALID_RELATIONSHIP))}"

    if 'section_type' in params and params['section_type']:
        if params['section_type'] not in VALID_SECTION_TYPES:
            return f"Invalid section_type '{params['section_type']}'. Valid: {', '.join(sorted(VALID_SECTION_TYPES))}"

    # restore_file needs at least one of discard_id or original_path
    if action == 'restore_file':
        if not params.get('discard_id') and not params.get('original_path'):
            return "restore_file requires either 'discard_id' or 'original_path'"

    return None  # Valid


# ═══════════════════════════════════════════════════════════════════
# Transaction Tracking & Rollback
# ═══════════════════════════════════════════════════════════════════

class TransactionTracker:
    """Tracks file modifications during a task session for rollback on failure."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir).resolve()
        self.modifications: List[Dict] = []
        self.locked_files: Set[str] = set()
        self.created_files: Set[str] = set()
        self.original_hashes: Dict[str, str] = {}  # path → SHA-256 before changes

    def record_before(self, file_path: str):
        """Record a file's state before modification."""
        full = self.project_dir / file_path
        if full.is_file() and file_path not in self.original_hashes:
            self.original_hashes[file_path] = hashlib.sha256(full.read_bytes()).hexdigest()

    def record_modification(self, file_path: str, action: str, success: bool):
        """Record that a modification was attempted."""
        self.modifications.append({
            'file_path': file_path,
            'action': action,
            'success': success,
            'timestamp': datetime.now().isoformat(),
        })
        if action == 'create' and success:
            self.created_files.add(file_path)

    def record_lock(self, file_path: str):
        self.locked_files.add(file_path)

    def record_unlock(self, file_path: str):
        self.locked_files.discard(file_path)

    def rollback(self, db_conn=None) -> dict:
        """Rollback all modifications made during this transaction.
        Returns {files_rolled_back, files_deleted, locks_released}."""
        rolled_back = []
        deleted = []

        # Restore modified files from git (if available)
        for path, orig_hash in self.original_hashes.items():
            full = self.project_dir / path
            if full.is_file():
                current_hash = hashlib.sha256(full.read_bytes()).hexdigest()
                if current_hash != orig_hash:
                    # Try git checkout to restore
                    import subprocess
                    try:
                        subprocess.run(
                            ['git', 'checkout', '--', path],
                            cwd=str(self.project_dir),
                            capture_output=True, timeout=10
                        )
                        rolled_back.append(path)
                    except Exception:
                        pass  # Can't rollback — file stays modified

        # Delete files that were created during this transaction
        for path in self.created_files:
            full = self.project_dir / path
            if full.is_file():
                # Move to _Discarded instead of hard delete
                discard_dir = self.project_dir / '_Discarded'
                discard_dir.mkdir(exist_ok=True)
                try:
                    shutil.move(str(full), str(discard_dir / full.name))
                    deleted.append(path)
                except Exception:
                    pass

        # Release all file locks
        locks_released = 0
        if db_conn:
            for path in self.locked_files:
                try:
                    db_conn.execute("DELETE FROM file_locks WHERE file_path = ?", (path,))
                    locks_released += 1
                except Exception:
                    pass
            db_conn.commit()

        self.locked_files.clear()

        return {
            'files_rolled_back': rolled_back,
            'files_deleted': deleted,
            'locks_released': locks_released,
            'total_modifications': len(self.modifications),
        }

    def get_summary(self) -> str:
        """Get a human-readable summary of all modifications."""
        lines = [f"Transaction: {len(self.modifications)} modifications"]
        for m in self.modifications:
            status = "OK" if m['success'] else "FAIL"
            lines.append(f"  [{status}] {m['action']} {m['file_path']}")
        if self.locked_files:
            lines.append(f"  Locks held: {', '.join(sorted(self.locked_files))}")
        return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════
# Complexity Bumping on Repeated Failure
# ═══════════════════════════════════════════════════════════════════

def handle_agent_failure(project_db, global_db, task_display_id: str,
                          agent: str, provider: str, reason: str,
                          attempt_number: int, max_attempts: int = 3) -> dict:
    """Handle an agent failing a task.

    Uses the persistent failure_count stored in the tasks table to enforce a
    hard cap of MAX_TASK_RETRIES (5) cumulative failures across all sprints.
    After the cap is reached the task is marked blocked with a reason and an
    escalation is logged + notified via the orchestrator.

    The *attempt_number* / *max_attempts* params are still accepted for
    backward compatibility but the authoritative count comes from the DB.

    Returns {action: 'retry'|'bumped'|'retry_cap_reached'|'exhausted', ...}.
    """
    from aixyad_db import (
        get_task_by_id, bump_complexity, release_task_reservation,
        unlock_file, get_all_locks, log_action, record_provider_attempt,
        increment_task_failure_count, get_task_failure_count,
        MAX_TASK_RETRIES, get_retry_backoff_seconds,
        log_escalation,
    )

    task = get_task_by_id(project_db, task_display_id)
    if not task:
        return {'action': 'error', 'reason': 'Task not found'}

    # Record the failure in provider performance
    if global_db:
        try:
            provider_row = global_db.execute(
                "SELECT id FROM providers WHERE name = ?", (provider,)
            ).fetchone()
            if provider_row:
                record_provider_attempt(global_db, provider_row['id'],
                                         task['complexity'], success=False)
        except Exception:
            pass

    # ── WF-4: classify failure → don't burn retry budget on transients ───
    failure_class = _classify_failure(reason)
    if failure_class == 'transient':
        # Network blip / connection reset / rate-limit-cooldown — retry next
        # cycle without consuming a retry slot. Log so we can see frequency.
        try:
            from aixyad_db import log_action
            log_action(project_db, 'task',
                       (get_task_by_id(project_db, task_display_id) or {}).get('id'),
                       agent, 'transient_failure_skipped',
                       {'reason': reason[:200], 'class': failure_class})
        except Exception:
            pass
        return {'action': 'retry', 'reason': f'transient: {reason[:100]}',
                'failure_count': get_task_failure_count(project_db, task_display_id)}

    if failure_class == 'refusal':
        # Claude/agent refused or said "cannot" — retrying won't help.
        # Mark blocked immediately for human review (no retry consumed).
        try:
            project_db.execute(
                'UPDATE tasks SET status = ?, blocked_reason = ? WHERE display_id = ?',
                ('blocked',
                 f'Agent refusal — needs human review. Reason: {reason[:300]}',
                 task_display_id),
            )
            project_db.commit()
        except Exception:
            pass
        return {'action': 'needs_human', 'reason': f'refusal: {reason[:100]}',
                'failure_count': get_task_failure_count(project_db, task_display_id)}

    # ── Increment the persistent failure counter ──────────────────
    new_failure_count = increment_task_failure_count(
        project_db, task_display_id, reason=reason,
    )

    # ── Hard cap: stop retrying after MAX_TASK_RETRIES failures ───
    if new_failure_count >= MAX_TASK_RETRIES:
        blocked_reason = (
            f'Retry cap reached ({MAX_TASK_RETRIES} failures). '
            f'Last failure: {reason[:200]}'
        )

        # Mark task blocked with reason
        try:
            project_db.execute(
                'UPDATE tasks SET status = ?, blocked_reason = ? WHERE display_id = ?',
                ('blocked', blocked_reason, task_display_id),
            )
            project_db.commit()
        except Exception:
            pass

        # Release reservation + file locks
        release_task_reservation(project_db, task_display_id)
        locks = get_all_locks(project_db, agent=agent)
        for lock in locks:
            unlock_file(project_db, lock['file_path'], agent)

        # Escalate via orchestrator — log + notify
        log_action(project_db, 'task', task['id'], agent, 'retry_cap_reached',
                   {'failure_count': new_failure_count,
                    'max_retries': MAX_TASK_RETRIES,
                    'reason': reason, 'provider': provider})
        try:
            log_escalation(
                project_db, task_display_id,
                session_id=f'{agent}/{provider}',
                iteration=new_failure_count,
                reason=blocked_reason,
                context_snapshot=json.dumps({
                    'failure_count': new_failure_count,
                    'complexity': task['complexity'],
                    'agent': agent, 'provider': provider,
                }),
            )
        except Exception:
            pass

        # Send notification (email / webhook / slack)
        try:
            from notifications import send_webhook, send_slack
            _notif_data = {
                'task_display_id': task_display_id,
                'title': task.get('title', ''),
                'failure_count': new_failure_count,
                'last_reason': reason[:300],
                'complexity': task['complexity'],
            }
            send_webhook('task_retry_cap_reached', _notif_data)
            send_slack(
                f':warning: Task {task_display_id} hit retry cap '
                f'({new_failure_count}/{MAX_TASK_RETRIES} failures). '
                f'Marked blocked — needs human review.'
            )
        except Exception:
            pass  # notifications are best-effort

        return {
            'action': 'retry_cap_reached',
            'failure_count': new_failure_count,
            'max_retries': MAX_TASK_RETRIES,
            'complexity': task['complexity'],
            'blocked_reason': blocked_reason,
        }

    # ── Still under the cap — decide: retry or bump complexity ────
    log_action(project_db, 'task', task['id'], agent, 'attempt_failed',
               {'failure_count': new_failure_count,
                'max_retries': MAX_TASK_RETRIES,
                'backoff_s': get_retry_backoff_seconds(new_failure_count),
                'reason': reason, 'provider': provider})

    # Use the legacy per-sprint attempt_number for complexity bumping
    if attempt_number < max_attempts:
        return {
            'action': 'retry',
            'failure_count': new_failure_count,
            'max_retries': MAX_TASK_RETRIES,
            'backoff_s': get_retry_backoff_seconds(new_failure_count),
            'attempt': attempt_number + 1,
            'max_attempts': max_attempts,
            'complexity': task['complexity'],
        }

    # Per-sprint max attempts reached — bump complexity and release
    old_complexity = task['complexity']
    new_complexity = bump_complexity(project_db, task_display_id,
                                      bumped_by=f'{agent}/{provider}',
                                      reason=f'Failed {max_attempts} attempts: {reason}')

    # Release reservation so another agent/provider can pick it up
    release_task_reservation(project_db, task_display_id)

    # Release any file locks held by this agent
    locks = get_all_locks(project_db, agent=agent)
    for lock in locks:
        unlock_file(project_db, lock['file_path'], agent)

    if new_complexity:
        log_action(project_db, 'task', task['id'], agent, 'complexity_bumped',
                   {'old_complexity': old_complexity, 'new_complexity': new_complexity,
                    'failure_count': new_failure_count,
                    'reason': f'Agent {agent}/{provider} failed {max_attempts}x',
                    'failure_reason': reason})
        return {
            'action': 'bumped',
            'old_complexity': old_complexity,
            'new_complexity': new_complexity,
            'failure_count': new_failure_count,
            'reason': reason,
        }

    # Already at max complexity (89) — task is exhausted
    log_action(project_db, 'task', task['id'], agent, 'task_exhausted',
               {'complexity': old_complexity, 'failure_count': new_failure_count,
                'reason': f'At max complexity, all agents failed'})
    return {
        'action': 'exhausted',
        'complexity': old_complexity,
        'failure_count': new_failure_count,
        'reason': 'At maximum complexity (89), all attempts failed',
    }


# ═══════════════════════════════════════════════════════════════════
# Action Documentation for Prompts
# ═══════════════════════════════════════════════════════════════════

def get_action_documentation() -> str:
    """Generate comprehensive action documentation for AI prompts."""
    lines = ["== AVAILABLE ACTIONS WITH SCHEMA ==\n"]

    for action, schema in sorted(ACTION_SCHEMA.items()):
        req = schema['required']
        opt = schema['optional']
        req_str = ', '.join(f'{p} (REQUIRED)' for p in req) if req else '(none required)'
        opt_str = ', '.join(f'{p}' for p in opt) if opt else ''

        lines.append(f"  {action}:")
        lines.append(f"    Required: {req_str}")
        if opt_str:
            lines.append(f"    Optional: {opt_str}")

    lines.append("\n== VALID ENUM VALUES ==")
    lines.append(f"  status: {', '.join(sorted(VALID_STATUS))}")
    lines.append(f"  priority: {', '.join(sorted(VALID_PRIORITY))}")
    lines.append(f"  complexity: {', '.join(str(c) for c in sorted(VALID_COMPLEXITY))}")
    lines.append(f"  relationship: {', '.join(sorted(VALID_RELATIONSHIP))}")
    lines.append(f"  section_type: {', '.join(sorted(VALID_SECTION_TYPES))}")

    lines.append("\n== RESPONSE FORMAT ==")
    lines.append('  {"action": "action_name", "params": {...}, "reasoning": "why"}')
    lines.append('  Always respond with ONLY a JSON object. No markdown, no explanation outside JSON.')

    return '\n'.join(lines)
