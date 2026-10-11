#!/usr/bin/env python3
"""
AIxYad DB Actions — Structured database operations for AI agents.

These handlers are called by AgentExecutor when an AI requests a DB action.
Each returns {success: bool, message: str} with formatted results.

Usage:
    from db_actions import DbActionHandler
    handler = DbActionHandler(project_db)
    result = handler.handle('query_task_detail', {'task_id': 'TASK-042'}, actor='Developer')
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aixyad_db import (
    build_task_context,
    build_task_timeline,
    build_sprint_summary,
    create_task,
    update_task,
    add_subtask,
    create_subtask,
    complete_subtask,
    check_subtask,
    reorder_task,
    log_action,
    log_file_modification,
    get_tasks_by_status,
    get_backlog,
    get_sprint_tasks,
)


# Priority display order for table formatting
_PRIORITY_ORDER = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}


def _now_iso() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require(params: dict, *keys: str) -> Optional[str]:
    """Check required params. Returns error message or None."""
    missing = [k for k in keys if not params.get(k)]
    if missing:
        return f"Missing required params: {', '.join(missing)}"
    return None


def _format_task_row(t: dict) -> str:
    """Format a single task dict as a table row."""
    display_id = t.get('display_id', '?')
    title = (t.get('title', '') or '')[:50]
    status = t.get('status', '?')
    priority = t.get('priority', '?')
    complexity = t.get('complexity', '?')
    agent = t.get('agent_slug', '') or t.get('assigned_agent', '') or '-'
    return f"{display_id:<12} {title:<50} {status:<14} {priority:<10} {str(complexity):<6} {agent}"


def _format_task_table(tasks: list) -> str:
    """Format a list of task dicts as an aligned table."""
    if not tasks:
        return "(no tasks)"
    header = f"{'ID':<12} {'Title':<50} {'Status':<14} {'Priority':<10} {'Cmplx':<6} Agent"
    sep = "-" * len(header)
    rows = [_format_task_row(t) for t in tasks]
    return f"{header}\n{sep}\n" + "\n".join(rows)


class DbActionHandler:
    """Handles database query/mutation actions from AI agents."""

    def __init__(self, project_db: sqlite3.Connection, global_db: sqlite3.Connection = None):
        self.pdb = project_db
        self.gdb = global_db

    def handle(self, action: str, params: dict,
               actor: str = '', session_id: str = '') -> dict:
        """Dispatch a DB action. Returns {success, message}."""
        handler = getattr(self, f'_do_{action}', None)
        if not handler:
            return {'success': False, 'message': f'ACTION: {action} — UNKNOWN DB ACTION'}
        try:
            return handler(params, actor, session_id)
        except sqlite3.Error as exc:
            return {'success': False, 'message': f'ACTION: {action} — DB ERROR: {exc}'}
        except Exception as exc:
            return {'success': False, 'message': f'ACTION: {action} — ERROR: {exc}'}

    # ── Query Actions ──────────────────────────────────────────────────

    def _do_query_tasks(self, params: dict, actor: str, session_id: str) -> dict:
        """Query tasks by status, priority, complexity.

        Params:
            status   — filter by status (backlog, in_progress, done, etc.)
            priority — filter by priority (CRITICAL, HIGH, MEDIUM, LOW)
            complexity — filter by complexity (fibonacci number)
            agent    — filter by agent_slug
            limit    — max results (default 50)
        """
        status = params.get('status', '')
        priority = params.get('priority', '')
        complexity = params.get('complexity')
        agent = params.get('agent', '')
        limit = min(int(params.get('limit', 50)), 200)

        # Use get_backlog for backlog queries, get_tasks_by_status for others
        if status == 'backlog' and not priority and not complexity and not agent:
            tasks = get_backlog(self.pdb, limit=limit)
        elif status:
            tasks = get_tasks_by_status(self.pdb, status, limit=limit)
        else:
            # Generic query with filters
            clauses = []
            values: list = []
            if status:
                clauses.append("status = ?")
                values.append(status)
            if priority:
                clauses.append("priority = ?")
                values.append(priority.upper())
            if complexity:
                clauses.append("complexity = ?")
                values.append(int(complexity))
            if agent:
                clauses.append("agent_slug = ?")
                values.append(agent)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT * FROM tasks {where} ORDER BY sort_order, id LIMIT ?"
            values.append(limit)
            tasks = [dict(r) for r in self.pdb.execute(sql, values).fetchall()]

        # Post-filter if we used a convenience function but also have extra filters
        if status == 'backlog' and (priority or complexity or agent):
            if priority:
                tasks = [t for t in tasks if t.get('priority') == priority.upper()]
            if complexity:
                tasks = [t for t in tasks if t.get('complexity') == int(complexity)]
            if agent:
                tasks = [t for t in tasks if t.get('agent_slug') == agent]

        table = _format_task_table(tasks)
        filters_desc = []
        if status:
            filters_desc.append(f"status={status}")
        if priority:
            filters_desc.append(f"priority={priority}")
        if complexity:
            filters_desc.append(f"complexity={complexity}")
        if agent:
            filters_desc.append(f"agent={agent}")
        filter_str = f" ({', '.join(filters_desc)})" if filters_desc else ""

        return {
            'success': True,
            'message': (
                f"ACTION: query_tasks{filter_str} — {len(tasks)} RESULTS\n\n"
                f"--- RESULTS ---\n{table}\n--- END RESULTS ---"
            ),
        }

    def _do_query_task_detail(self, params: dict, actor: str, session_id: str) -> dict:
        """Get full task detail including context, subtasks, history, files.

        Params:
            task_id — display ID (e.g. TASK-042) [required]
        """
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: query_task_detail — {err}'}

        task_id = params['task_id']
        ctx = build_task_context(self.pdb, task_id)

        if 'error' in ctx:
            return {'success': False, 'message': f'ACTION: query_task_detail {task_id} — {ctx["error"]}'}

        task = ctx['task']
        lines = [
            f"ACTION: query_task_detail {task_id} — SUCCESS",
            "",
            "--- TASK ---",
            f"ID: {task.get('display_id', '?')}",
            f"Title: {task.get('title', '')}",
            f"Status: {task.get('status', '?')} | Priority: {task.get('priority', '?')} | "
            f"Complexity: {task.get('complexity', '?')}",
            f"Agent: {task.get('agent_slug', '') or '-'} | "
            f"Provider: {task.get('provider_name', '') or '-'}",
        ]

        if task.get('description'):
            lines.append(f"Description: {task['description']}")
        if task.get('acceptance_criteria'):
            lines.append(f"Acceptance Criteria: {task['acceptance_criteria']}")
        if task.get('blocked_reason'):
            lines.append(f"Blocked: {task['blocked_reason']}")
        if task.get('value'):
            lines.append(f"Value: {task['value']}")
        if task.get('created_at'):
            lines.append(f"Created: {task['created_at']}")
        if task.get('started_at'):
            lines.append(f"Started: {task['started_at']}")
        if task.get('completed_at'):
            lines.append(f"Completed: {task['completed_at']}")

        # Parent chain
        parent_chain = ctx.get('parent_chain', [])
        project_task = ctx.get('project_task')
        if parent_chain or project_task:
            lines.append("")
            lines.append("--- PARENT CHAIN ---")
            chain_parts = []
            if project_task:
                chain_parts.append(project_task.get('display_id', 'PROJ-?'))
            chain_parts.extend(p.get('display_id', '?') for p in parent_chain)
            chain_parts.append(task.get('display_id', '?'))
            lines.append(" > ".join(chain_parts))

        # Children
        children = ctx.get('children', [])
        if children:
            lines.append("")
            lines.append(f"--- CHILD TASKS ({len(children)}) ---")
            for c in children:
                lines.append(
                    f"  {c.get('display_id', '?')}: {c.get('title', '')} "
                    f"[{c.get('status', '?')}]"
                )

        # Subtasks (checklist)
        subtasks = ctx.get('subtasks', [])
        if subtasks:
            done_count = sum(1 for s in subtasks if s.get('status') == 'done')
            lines.append("")
            lines.append(f"--- SUBTASKS ({done_count}/{len(subtasks)} done) ---")
            for s in subtasks:
                check = 'x' if s.get('status') == 'done' else ' '
                lines.append(f"  [{check}] {s.get('display_id', '?')}: {s.get('title', '')}")

        # Linked files
        linked_files = ctx.get('linked_files', [])
        if linked_files:
            lines.append("")
            lines.append("--- LINKED FILES ---")
            for f in linked_files:
                rel = f.get('relationship', 'related')
                lines.append(f"  - {f.get('path', '?')} ({rel})")

        # File modifications
        modifications = ctx.get('file_modifications', [])
        if modifications:
            lines.append("")
            lines.append(f"--- FILE MODIFICATIONS (last {len(modifications)}) ---")
            for m in modifications:
                lines.append(
                    f"  - {m.get('timestamp', '?')} {m.get('action', '?')}: {m.get('path', '?')}"
                )

        # Recent history
        history = ctx.get('recent_history', [])
        if history:
            lines.append("")
            lines.append(f"--- RECENT HISTORY (last {len(history)}) ---")
            for h in history:
                detail = {}
                try:
                    detail = json.loads(h.get('detail', '{}')) if h.get('detail') else {}
                except (json.JSONDecodeError, TypeError):
                    pass
                msg = detail.get('message', '') or h.get('action', '')
                actor_name = h.get('actor', '?')
                lines.append(f"  - {h.get('timestamp', '?')} [{actor_name}] {msg}")

        # Sprint context
        sprint_ctx = ctx.get('sprint_context')
        if sprint_ctx:
            lines.append("")
            lines.append("--- SPRINT CONTEXT ---")
            sprint_num = sprint_ctx.get('sprint_number', '?')
            sprint_name = sprint_ctx.get('sprint_name', '')
            sprint_status = sprint_ctx.get('sprint_status', '?')
            goal = sprint_ctx.get('goal', '')
            planned = sprint_ctx.get('planned_points', 0)
            completed = sprint_ctx.get('completed_points', 0)
            lines.append(
                f"Sprint {sprint_num} ({sprint_status})"
                f"{': ' + sprint_name if sprint_name else ''}"
            )
            if goal:
                lines.append(f"Goal: {goal}")
            lines.append(f"Points: {completed}/{planned} completed")

        lines.append("--- END ---")
        return {'success': True, 'message': "\n".join(lines)}

    def _do_update_task(self, params: dict, actor: str, session_id: str) -> dict:
        """Update task status, priority, complexity, agent, or other fields.

        Params:
            task_id    — display ID (e.g. TASK-042) [required]
            status     — new status (backlog, sprint_planned, in_progress, in_review, done, blocked)
            priority   — new priority (CRITICAL, HIGH, MEDIUM, LOW)
            complexity — new complexity (fibonacci: 1,2,3,5,8,13,21,34,55,89)
            agent      — agent_slug to assign
            title      — new title
            description — new description
            blocked_reason — reason for blocking
        """
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: update_task — {err}'}

        task_id = params['task_id']

        # Get current state for logging
        old_row = self.pdb.execute(
            "SELECT * FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        if not old_row:
            return {'success': False, 'message': f'ACTION: update_task {task_id} — TASK NOT FOUND'}
        old_task = dict(old_row)

        # Build update fields
        fields = {}
        field_map = {
            'status': 'status',
            'priority': 'priority',
            'complexity': 'complexity',
            'agent': 'agent_slug',
            'title': 'title',
            'description': 'description',
            'blocked_reason': 'blocked_reason',
            'acceptance_criteria': 'acceptance_criteria',
            'value': 'value',
            'provider': 'provider_name',
        }
        for param_key, db_key in field_map.items():
            if param_key in params and params[param_key] is not None:
                fields[db_key] = params[param_key]

        if not fields:
            return {'success': False, 'message': f'ACTION: update_task {task_id} — no fields to update'}

        ok = update_task(self.pdb, task_id, **fields)
        if not ok:
            return {'success': False, 'message': f'ACTION: update_task {task_id} — UPDATE FAILED (invalid fields?)'}

        # Log the change
        changes = []
        for param_key, db_key in field_map.items():
            if db_key in fields:
                old_val = str(old_task.get(db_key, ''))
                new_val = str(fields[db_key])
                if old_val != new_val:
                    changes.append(f"{param_key}: {old_val} -> {new_val}")

        detail = {'changes': changes, 'fields': fields}

        # Determine action type for the log
        if 'status' in fields:
            action_type = 'status_change'
            detail['old_value'] = old_task.get('status', '')
            detail['new_value'] = fields['status']
        elif 'agent_slug' in fields:
            action_type = 'assigned'
            detail['new_value'] = fields['agent_slug']
        else:
            action_type = 'updated'

        log_action(self.pdb, 'task', old_task['id'], actor, action_type,
                   detail=detail, session_id=session_id)

        changes_str = "; ".join(changes) if changes else "no visible changes"
        return {
            'success': True,
            'message': (
                f"ACTION: update_task {task_id} — SUCCESS\n\n"
                f"--- CHANGES ---\n{changes_str}\n--- END ---"
            ),
        }

    def _do_add_task(self, params: dict, actor: str, session_id: str) -> dict:
        """Create a new task.

        Params:
            title       — task title [required]
            description — task description
            priority    — CRITICAL, HIGH, MEDIUM, LOW (default MEDIUM)
            complexity  — fibonacci number (default 3)
            agent       — agent_slug to assign
            parent_task_id — display ID of parent task
        """
        err = _require(params, 'title')
        if err:
            return {'success': False, 'message': f'ACTION: add_task — {err}'}

        # Resolve parent_task_id from display_id if given
        parent_id = None
        parent_display = params.get('parent_task_id', '')
        if parent_display:
            parent_row = self.pdb.execute(
                "SELECT id FROM tasks WHERE display_id = ?", (parent_display,)
            ).fetchone()
            if parent_row:
                parent_id = parent_row['id']
            else:
                return {
                    'success': False,
                    'message': f'ACTION: add_task — parent task {parent_display} not found',
                }

        result = create_task(
            self.pdb,
            title=params['title'],
            description=params.get('description', ''),
            priority=params.get('priority', 'MEDIUM').upper(),
            complexity=int(params.get('complexity', 3)),
            agent_slug=params.get('agent', ''),
            parent_task_id=parent_id,
        )

        if 'error' in result:
            return {'success': False, 'message': f'ACTION: add_task — {result["error"]}'}

        # Log creation
        log_action(self.pdb, 'task', result['id'], actor, 'created',
                   detail={'title': params['title']}, session_id=session_id)

        return {
            'success': True,
            'message': (
                f"ACTION: add_task — SUCCESS\n\n"
                f"--- CREATED ---\n"
                f"ID: {result['display_id']}\n"
                f"Title: {params['title']}\n"
                f"Priority: {params.get('priority', 'MEDIUM').upper()} | "
                f"Complexity: {params.get('complexity', 3)}\n"
                f"--- END ---"
            ),
        }

    def _do_reorder_tasks(self, params: dict, actor: str, session_id: str) -> dict:
        """Move task to a new position within its status group.

        Params:
            task_id  — display ID [required]
            position — new 0-based position [required]
        """
        err = _require(params, 'task_id', 'position')
        if err:
            return {'success': False, 'message': f'ACTION: reorder_tasks — {err}'}

        task_id = params['task_id']
        position = int(params['position'])

        ok = reorder_task(self.pdb, task_id, position)
        if not ok:
            return {'success': False, 'message': f'ACTION: reorder_tasks {task_id} — FAILED (task not found?)'}

        log_action(self.pdb, 'task', 0, actor, 'reorder',
                   detail={'task_id': task_id, 'new_position': position},
                   session_id=session_id)

        return {
            'success': True,
            'message': f"ACTION: reorder_tasks {task_id} — SUCCESS (moved to position {position})",
        }

    def _do_add_checklist_item(self, params: dict, actor: str, session_id: str) -> dict:
        """Add a subtask/checklist item to a task.

        Params:
            task_id     — parent task display ID [required]
            title       — subtask title [required]
            description — subtask description [optional]
        """
        err = _require(params, 'task_id', 'title')
        if err:
            return {'success': False, 'message': f'ACTION: add_checklist_item — {err}'}

        task_id = params['task_id']
        title = params['title']
        description = params.get('description', '')

        result = add_subtask(self.pdb, task_id, title, description=description)

        if 'error' in result:
            return {'success': False, 'message': f'ACTION: add_checklist_item — {result["error"]}'}

        # Log
        task_row = self.pdb.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        if task_row:
            log_action(self.pdb, 'checklist', task_row['id'], actor, 'checklist_add',
                       detail={'subtask_id': result['display_id'], 'title': title},
                       session_id=session_id)

        return {
            'success': True,
            'message': (
                f"ACTION: add_checklist_item {task_id} — SUCCESS\n\n"
                f"--- ADDED ---\n"
                f"ID: {result['display_id']}\n"
                f"Title: {title}\n"
                f"Parent: {task_id}\n"
                f"--- END ---"
            ),
        }

    def _do_check_item(self, params: dict, actor: str, session_id: str) -> dict:
        """Mark a checklist item complete or incomplete.

        Params:
            subtask_id — subtask display ID [required]
            checked    — true/false (default true)
        """
        err = _require(params, 'subtask_id')
        if err:
            return {'success': False, 'message': f'ACTION: check_item — {err}'}

        subtask_display = params['subtask_id']
        checked = params.get('checked', True)
        if isinstance(checked, str):
            checked = checked.lower() in ('true', '1', 'yes')

        # Resolve subtask ID
        sub_row = self.pdb.execute(
            "SELECT id, task_id, title FROM subtasks WHERE display_id = ?",
            (subtask_display,)
        ).fetchone()
        if not sub_row:
            return {
                'success': False,
                'message': f'ACTION: check_item {subtask_display} — SUBTASK NOT FOUND',
            }

        ok = check_subtask(self.pdb, sub_row['id'], checked=checked, by=actor)
        if not ok:
            return {
                'success': False,
                'message': f'ACTION: check_item {subtask_display} — UPDATE FAILED',
            }

        # Log
        status_word = "checked" if checked else "unchecked"
        if sub_row['task_id']:
            log_action(self.pdb, 'checklist', sub_row['task_id'], actor,
                       f'checklist_{status_word}',
                       detail={'subtask_id': subtask_display, 'title': sub_row['title']},
                       session_id=session_id)

        mark = 'x' if checked else ' '
        return {
            'success': True,
            'message': (
                f"ACTION: check_item {subtask_display} — SUCCESS\n\n"
                f"[{mark}] {sub_row['title']}"
            ),
        }

    # ── Subtask Management ─────────────────────────────────────────────

    def _do_create_subtask(self, params: dict, actor: str, session_id: str) -> dict:
        """Create a subtask under a parent task.

        Params:
            parent_task_id — display ID of the parent task [required]
            title          — subtask title [required]
            description    — optional description
        """
        err = _require(params, 'parent_task_id', 'title')
        if err:
            return {'success': False, 'message': f'ACTION: create_subtask — {err}'}

        parent_display_id = params['parent_task_id']
        title = params['title']
        description = params.get('description', '')

        result = create_subtask(self.pdb, parent_display_id, title, description)
        if 'error' in result:
            return {'success': False, 'message': f'ACTION: create_subtask — {result["error"]}'}

        display_id = result.get('display_id', '?')
        # S5-W09: emit WS event for real-time UI updates
        try:
            from datetime import datetime, timezone
            from api_server import _push_event_sync
            _push_event_sync('subtask_created', {
                'id': result.get('id'),
                'subtask_id': display_id,
                'parent_task_id': parent_display_id,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'actor': actor,
                'action': 'create_subtask',
                'status': 'created',
                'detail': {
                    'summary': f'Created subtask {display_id} under {parent_display_id}: {title}',
                    'subtask_id': display_id,
                    'parent_task_id': parent_display_id,
                    'title': title,
                },
            })
        except Exception:
            pass
        return {
            'success': True,
            'message': f'ACTION: create_subtask — Created subtask {display_id} under {parent_display_id}: {title}',
            'subtask_display_id': display_id,
        }

    def _do_complete_subtask(self, params: dict, actor: str, session_id: str) -> dict:
        """Mark a subtask as done with a result.

        Params:
            subtask_id — display ID of the subtask [required]
            result     — result/summary string [required]
        """
        err = _require(params, 'subtask_id', 'result')
        if err:
            return {'success': False, 'message': f'ACTION: complete_subtask — {err}'}

        subtask_display_id = params['subtask_id']
        result_text = params['result']

        outcome = complete_subtask(self.pdb, subtask_display_id, result_text)
        if not outcome.get('success'):
            return {
                'success': False,
                'message': f'ACTION: complete_subtask {subtask_display_id} — SUBTASK NOT FOUND',
            }

        parent_completed = outcome.get('parent_completed', False)
        if parent_completed:
            completion_note = 'All subtasks complete for parent task. Parent task marked done.'
        else:
            # Count remaining
            sub_row = self.pdb.execute(
                "SELECT task_id FROM subtasks WHERE display_id = ?", (subtask_display_id,)
            ).fetchone()
            remaining = 0
            if sub_row and sub_row['task_id']:
                remaining = self.pdb.execute(
                    "SELECT COUNT(*) FROM subtasks WHERE task_id = ? AND status != 'done'",
                    (sub_row['task_id'],)
                ).fetchone()[0]
            completion_note = f'{remaining} subtask(s) remaining.'

        # S5-W09: emit WS event for real-time UI updates
        try:
            from datetime import datetime, timezone
            from api_server import _push_event_sync
            _push_event_sync('subtask_completed', {
                'subtask_id': subtask_display_id,
                'parent_completed': parent_completed,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'actor': actor,
                'action': 'complete_subtask',
                'status': 'completed' if parent_completed else 'done',
                'detail': {
                    'summary': f'Completed {subtask_display_id}. {completion_note}',
                    'subtask_id': subtask_display_id,
                    'parent_completed': parent_completed,
                    'result': result_text,
                },
            })
        except Exception:
            pass
        return {
            'success': True,
            'parent_completed': parent_completed,
            'message': f'ACTION: complete_subtask — Completed {subtask_display_id}. {completion_note}',
        }

    def _do_store_handoff(self, params: dict, actor: str, session_id: str) -> dict:
        """Store a handoff record on a task.

        Params:
            task_id        — display ID of the task [required]
            files_changed  — list of file paths changed
            tests_written  — list of test file paths written
            coverage_notes — string describing coverage
            known_issues   — string describing known issues
        """
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: store_handoff — {err}'}

        task_id = params['task_id']
        files_changed = params.get('files_changed', [])
        tests_written = params.get('tests_written', [])
        coverage_notes = params.get('coverage_notes', '')
        known_issues = params.get('known_issues', '')

        if isinstance(files_changed, str):
            files_changed = [files_changed]
        if isinstance(tests_written, str):
            tests_written = [tests_written]

        handoff = {
            'files_changed': files_changed,
            'tests_written': tests_written,
            'coverage_notes': coverage_notes,
            'known_issues': known_issues,
        }

        ok = update_task(self.pdb, task_id, handoff_json=json.dumps(handoff))
        if not ok:
            return {
                'success': False,
                'message': f'ACTION: store_handoff {task_id} — TASK NOT FOUND or no changes',
            }

        return {
            'success': True,
            'message': (
                f'ACTION: store_handoff {task_id} — Handoff stored '
                f'({len(files_changed)} files changed, {len(tests_written)} tests written)'
            ),
        }

    # ── Sprint Queries ─────────────────────────────────────────────────

    def _do_query_sprint(self, params: dict, actor: str, session_id: str) -> dict:
        """Get current sprint info with all tasks.

        Params:
            sprint_number — specific sprint number (default: latest)
        """
        sprint_number = params.get('sprint_number')
        if sprint_number is not None:
            sprint_number = int(sprint_number)

        summary = build_sprint_summary(self.pdb, sprint_number=sprint_number)

        if 'error' in summary:
            return {'success': False, 'message': f'ACTION: query_sprint — {summary["error"]}'}

        sprint = summary['sprint']
        status_counts = summary.get('status_counts', {})
        tasks = summary.get('tasks', [])
        ceremonies = summary.get('ceremonies', [])

        lines = [
            f"ACTION: query_sprint {sprint.get('sprint_number', '?')} — SUCCESS",
            "",
            "--- SPRINT ---",
            f"Sprint {sprint.get('sprint_number', '?')}: {sprint.get('name', '')}",
            f"Status: {sprint.get('status', '?')}",
        ]
        if sprint.get('goal'):
            lines.append(f"Goal: {sprint['goal']}")
        lines.append(
            f"Points: {sprint.get('completed_points', 0)}/{sprint.get('planned_points', 0)} "
            f"| Velocity: {sprint.get('velocity', 0)}"
        )
        if sprint.get('start_date'):
            lines.append(f"Dates: {sprint['start_date']} to {sprint.get('end_date', '?')}")

        # Status breakdown
        if status_counts:
            lines.append("")
            lines.append("--- STATUS BREAKDOWN ---")
            for status, data in sorted(status_counts.items()):
                lines.append(f"  {status}: {data['count']} tasks ({data['points']} points)")

        # Tasks
        if tasks:
            lines.append("")
            lines.append(f"--- SPRINT TASKS ({len(tasks)}) ---")
            header = f"  {'Sprint ID':<12} {'Task ID':<12} {'Title':<40} {'Status':<12} {'Points':<6} Agent"
            lines.append(header)
            lines.append("  " + "-" * (len(header) - 2))
            for t in tasks:
                lines.append(
                    f"  {t.get('sprint_task_id', '?'):<12} "
                    f"{t.get('task_display_id', '?'):<12} "
                    f"{(t.get('title', '') or '')[:40]:<40} "
                    f"{t.get('status', '?'):<12} "
                    f"{str(t.get('story_points', '-')):<6} "
                    f"{t.get('assigned_agent', '') or '-'}"
                )

        # Ceremonies
        if ceremonies:
            lines.append("")
            lines.append(f"--- CEREMONIES ({len(ceremonies)}) ---")
            for c in ceremonies:
                lines.append(f"  - {c.get('created_at', '?')} [{c.get('ceremony_type', '?')}] "
                             f"{(c.get('notes', '') or '')[:80]}")

        lines.append("--- END ---")
        return {'success': True, 'message': "\n".join(lines)}

    def _do_query_sprint_summary(self, params: dict, actor: str, session_id: str) -> dict:
        """Get sprint overview — alias for query_sprint with summary focus.

        Params:
            sprint_number — specific sprint number (default: latest)
        """
        return self._do_query_sprint(params, actor, session_id)

    # ── History/Timeline Queries ───────────────────────────────────────

    def _do_query_history(self, params: dict, actor: str, session_id: str) -> dict:
        """Get action history for a task, agent, or session.

        Params:
            task_id    — filter by task display ID
            actor      — filter by actor name
            session_id — filter by session ID
            limit      — max results (default 30)
        """
        task_id_filter = params.get('task_id', '')
        actor_filter = params.get('actor', '')
        sid_filter = params.get('session_id', '')
        limit = min(int(params.get('limit', 30)), 200)

        clauses = []
        values: list = []

        if task_id_filter:
            task_row = self.pdb.execute(
                "SELECT id FROM tasks WHERE display_id = ?", (task_id_filter,)
            ).fetchone()
            if task_row:
                clauses.append("(scope = 'task' AND scope_id = ?)")
                values.append(task_row['id'])
            else:
                return {
                    'success': False,
                    'message': f'ACTION: query_history — task {task_id_filter} not found',
                }

        if actor_filter:
            clauses.append("actor = ?")
            values.append(actor_filter)

        if sid_filter:
            clauses.append("session_id = ?")
            values.append(sid_filter)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM action_log {where} ORDER BY timestamp DESC LIMIT ?"
        values.append(limit)

        rows = [dict(r) for r in self.pdb.execute(sql, values).fetchall()]

        if not rows:
            return {
                'success': True,
                'message': "ACTION: query_history — 0 RESULTS\n\n(no matching history entries)",
            }

        lines = [f"ACTION: query_history — {len(rows)} RESULTS", "", "--- HISTORY ---"]
        for r in rows:
            detail = {}
            try:
                detail = json.loads(r.get('detail', '{}')) if r.get('detail') else {}
            except (json.JSONDecodeError, TypeError):
                pass
            msg = detail.get('message', '') or r.get('action', '')
            lines.append(
                f"  - {r.get('timestamp', '?')} [{r.get('actor', '?')}] "
                f"{r.get('scope', '?')}:{r.get('scope_id', '?')} — {msg}"
            )
        lines.append("--- END HISTORY ---")

        return {'success': True, 'message': "\n".join(lines)}

    def _do_query_task_timeline(self, params: dict, actor: str, session_id: str) -> dict:
        """Get chronological dot-point summary of all actions on a task.

        Params:
            task_id — display ID [required]
        """
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: query_task_timeline — {err}'}

        task_id = params['task_id']
        timeline = build_task_timeline(self.pdb, task_id)

        if not timeline:
            return {
                'success': True,
                'message': f"ACTION: query_task_timeline {task_id} — 0 EVENTS\n\n(no timeline entries)",
            }

        lines = [
            f"ACTION: query_task_timeline {task_id} — {len(timeline)} EVENTS",
            "",
            "--- TIMELINE ---",
        ]
        for entry in timeline:
            lines.append(
                f"  - {entry.get('timestamp', '?')} [{entry.get('actor', '?')}] "
                f"{entry.get('summary', entry.get('action', '?'))}"
            )
        lines.append("--- END TIMELINE ---")

        return {'success': True, 'message': "\n".join(lines)}

    # ── File Linking ───────────────────────────────────────────────────

    def _do_link_file_to_task(self, params: dict, actor: str, session_id: str) -> dict:
        """Associate a file with a task.

        Params:
            task_id      — task display ID [required]
            file_path    — relative file path [required]
            relationship — related, modified, created, reviewed, depends_on, output (default: related)
            notes        — optional notes about the relationship
        """
        err = _require(params, 'task_id', 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: link_file_to_task — {err}'}

        task_id = params['task_id']
        file_path = params['file_path']
        relationship = params.get('relationship', 'related')
        notes = params.get('notes', '')

        # Resolve task
        task_row = self.pdb.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        if not task_row:
            return {
                'success': False,
                'message': f'ACTION: link_file_to_task — task {task_id} not found',
            }

        # Ensure file exists in project_files (upsert)
        self.pdb.execute(
            "INSERT INTO project_files (path) VALUES (?) ON CONFLICT(path) DO NOTHING",
            (file_path,)
        )
        file_row = self.pdb.execute(
            "SELECT id FROM project_files WHERE path = ?", (file_path,)
        ).fetchone()
        file_id = file_row['id']

        # Insert link
        self.pdb.execute("""
            INSERT INTO entity_files (entity_type, entity_id, file_id, relationship, notes, added_by)
            VALUES ('task', ?, ?, ?, ?, ?)
        """, (task_row['id'], file_id, relationship, notes, actor))
        self.pdb.commit()

        # Log
        log_action(self.pdb, 'file', task_row['id'], actor, 'file_linked',
                   detail={'file_path': file_path, 'relationship': relationship},
                   session_id=session_id)

        return {
            'success': True,
            'message': (
                f"ACTION: link_file_to_task {task_id} — SUCCESS\n\n"
                f"Linked: {file_path} ({relationship}) to {task_id}"
            ),
        }

    def _do_log_file_modification(self, params: dict, actor: str, session_id: str) -> dict:
        """Record a file modification linked to a task.

        Params:
            task_id        — task display ID [required]
            file_path      — relative file path [required]
            action         — create, edit, delete [required]
            before_snippet — content before change (optional)
            after_snippet  — content after change (optional)
        """
        err = _require(params, 'task_id', 'file_path', 'action')
        if err:
            return {'success': False, 'message': f'ACTION: log_file_modification — {err}'}

        task_id = params['task_id']
        file_path = params['file_path']
        mod_action = params['action']

        task_row = self.pdb.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        if not task_row:
            return {
                'success': False,
                'message': f'ACTION: log_file_modification — task {task_id} not found',
            }

        mod_id = log_file_modification(
            self.pdb,
            entity_type='task',
            entity_id=task_row['id'],
            file_path=file_path,
            action=mod_action,
            before_snippet=params.get('before_snippet', ''),
            after_snippet=params.get('after_snippet', ''),
            session_id=session_id,
        )

        # Also log to action_log
        log_action(self.pdb, 'task', task_row['id'], actor, 'file_modified',
                   detail={'file_path': file_path, 'action': mod_action},
                   session_id=session_id)

        return {
            'success': True,
            'message': (
                f"ACTION: log_file_modification {task_id} — SUCCESS\n\n"
                f"Recorded: {mod_action} {file_path} (modification #{mod_id})"
            ),
        }

    # ── Task Reservation & Assignment ─────────────────────────────────

    def _do_reserve_task(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: reserve_task — {err}'}
        agent = params.get('agent', actor)
        from aixyad_db import reserve_task
        ok = reserve_task(self.pdb, params['task_id'], agent)
        if ok:
            return {'success': True, 'message': f"ACTION: reserve_task {params['task_id']} — RESERVED by {agent}"}
        return {'success': False, 'message': f"ACTION: reserve_task {params['task_id']} — FAILED (already reserved by another agent)"}

    def _do_release_task(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: release_task — {err}'}
        from aixyad_db import release_task_reservation
        release_task_reservation(self.pdb, params['task_id'])
        return {'success': True, 'message': f"ACTION: release_task {params['task_id']} — RELEASED"}

    def _do_assign_task(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'task_id', 'agent')
        if err:
            return {'success': False, 'message': f'ACTION: assign_task — {err}'}
        from aixyad_db import assign_task
        ok = assign_task(self.pdb, params['task_id'], params['agent'], params.get('provider', ''))
        if ok:
            return {'success': True, 'message': f"ACTION: assign_task {params['task_id']} — ASSIGNED to {params['agent']}"}
        return {'success': False, 'message': f"ACTION: assign_task {params['task_id']} — FAILED (task not found)"}

    def _do_reserve_subtask(self, params: dict, actor: str, session_id: str) -> dict:
        if 'subtask_id' not in params:
            return {'success': False, 'message': 'ACTION: reserve_subtask — missing required param: subtask_id'}
        agent = params.get('agent', actor)
        from aixyad_db import reserve_subtask
        ok = reserve_subtask(self.pdb, int(params['subtask_id']), agent)
        if ok:
            return {'success': True, 'message': f"ACTION: reserve_subtask {params['subtask_id']} — RESERVED by {agent}"}
        return {'success': False, 'message': f"ACTION: reserve_subtask {params['subtask_id']} — FAILED (already reserved)"}

    # ── Safe Delete / Discard / Restore ──────────────────────────────

    def _do_discard_task(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: discard_task — {err}'}
        from aixyad_db import discard_task
        ok = discard_task(self.pdb, params['task_id'], actor, params.get('reason', ''))
        if ok:
            return {'success': True, 'message': f"ACTION: discard_task {params['task_id']} — ARCHIVED (can be restored)"}
        return {'success': False, 'message': f"ACTION: discard_task {params['task_id']} — FAILED (not found)"}

    def _do_restore_task(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'task_id')
        if err:
            return {'success': False, 'message': f'ACTION: restore_task — {err}'}
        from aixyad_db import restore_task
        ok = restore_task(self.pdb, params['task_id'], params.get('restore_to', 'backlog'))
        if ok:
            return {'success': True, 'message': f"ACTION: restore_task {params['task_id']} — RESTORED"}
        return {'success': False, 'message': f"ACTION: restore_task {params['task_id']} — FAILED (not found or not archived)"}

    def _do_discard_file(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: discard_file — {err}'}
        from aixyad_db import discard_file
        from pathlib import Path
        project_dir = Path(params.get('project_dir', '.')).resolve()
        result = discard_file(self.pdb, project_dir, params['file_path'], actor, params.get('reason', ''))
        if result:
            return {'success': True, 'message': f"ACTION: discard_file {params['file_path']} — MOVED to _Discarded/{result['discard_name']} (ID: {result['id']})"}
        return {'success': False, 'message': f"ACTION: discard_file {params['file_path']} — FAILED (file not found)"}

    def _do_restore_file(self, params: dict, actor: str, session_id: str) -> dict:
        from aixyad_db import restore_file
        from pathlib import Path
        project_dir = Path(params.get('project_dir', '.')).resolve()
        ok = restore_file(self.pdb, project_dir,
                           discard_id=params.get('discard_id'),
                           original_path=params.get('original_path'))
        if ok:
            return {'success': True, 'message': f"ACTION: restore_file — RESTORED to original location"}
        return {'success': False, 'message': f"ACTION: restore_file — FAILED (not found in _Discarded)"}

    def _do_query_discarded_files(self, params: dict, actor: str, session_id: str) -> dict:
        from aixyad_db import get_discarded_files
        files = get_discarded_files(self.pdb,
                                     original_path=params.get('path'),
                                     since=params.get('since'),
                                     until=params.get('until'))
        lines = [f"  {f['id']}. {f['original_path']} → _Discarded/{f['discard_name']} "
                 f"(by {f['discarded_by']}, {f['discarded_at']})"
                 for f in files]
        return {
            'success': True,
            'message': f"ACTION: query_discarded_files — {len(files)} RESULTS\n\n--- DISCARDED FILES ---\n" +
                       '\n'.join(lines) + "\n--- END ---"
        }

    def _do_query_file_history(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: query_file_history — {err}'}
        from aixyad_db import get_file_history
        events = get_file_history(self.pdb, params['file_path'])
        lines = []
        for e in events:
            if e.get('event_type') == 'discard':
                lines.append(f"  DISCARDED by {e.get('discarded_by','')} at {e.get('event_time','')} — {e.get('reason','')}")
            else:
                lines.append(f"  {e.get('action','')} at {e.get('event_time','')} — session {e.get('session_id','')}")
        return {
            'success': True,
            'message': f"ACTION: query_file_history {params['file_path']} — {len(events)} EVENTS\n\n--- HISTORY ---\n" +
                       '\n'.join(lines) + "\n--- END ---"
        }

    # ── Prompt Log Queries ─────────────────────────────────────────────

    def _do_query_prompt_logs(self, params: dict, actor: str, session_id: str) -> dict:
        """Query stored prompt/response pairs."""
        from aixyad_db import get_prompt_logs
        logs = get_prompt_logs(
            self.pdb,
            session_id=params.get('session_id'),
            task_display_id=params.get('task_id'),
            provider_name=params.get('provider'),
            limit=min(int(params.get('limit', 20)), 100),
        )
        lines = []
        for log in logs:
            status = 'OK' if log.get('success') else 'FAIL'
            lines.append(
                f"  [{status}] {log.get('timestamp', '?')[:16]} "
                f"task={log.get('task_display_id', '?')} "
                f"provider={log.get('provider_name', '?')} "
                f"agent={log.get('agent_slug', '?')} "
                f"tokens={log.get('tokens_in', 0)}/{log.get('tokens_out', 0)}"
            )
        return {
            'success': True,
            'message': (
                f"ACTION: query_prompt_logs — {len(logs)} ENTRIES\n\n"
                f"--- PROMPT LOGS ---\n" + '\n'.join(lines) + "\n--- END ---"
            ),
        }

    # ── File Locking ──────────────────────────────────────────────────

    def _do_lock_file(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: lock_file — {err}'}
        from aixyad_db import lock_file
        agent = params.get('agent', actor)
        result = lock_file(self.pdb, params['file_path'], agent, session_id)
        if result['locked']:
            return {'success': True, 'message': f"ACTION: lock_file {params['file_path']} — LOCKED by {agent}"}
        return {'success': False, 'message': f"ACTION: lock_file {params['file_path']} — ALREADY LOCKED by {result['holder']} since {result.get('locked_at', '?')}"}

    def _do_unlock_file(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: unlock_file — {err}'}
        from aixyad_db import unlock_file
        unlock_file(self.pdb, params['file_path'], actor)
        return {'success': True, 'message': f"ACTION: unlock_file {params['file_path']} — UNLOCKED"}

    def _do_check_file_lock(self, params: dict, actor: str, session_id: str) -> dict:
        err = _require(params, 'file_path')
        if err:
            return {'success': False, 'message': f'ACTION: check_file_lock — {err}'}
        from aixyad_db import check_file_lock
        lock = check_file_lock(self.pdb, params['file_path'])
        if lock:
            return {'success': True, 'message': f"ACTION: check_file_lock {params['file_path']} — LOCKED by {lock['locked_by']} since {lock['locked_at']}"}
        return {'success': True, 'message': f"ACTION: check_file_lock {params['file_path']} — UNLOCKED (available)"}

    # ── GUID Code Section Tracking ────────────────────────────────────

    def _do_create_section(self, params: dict, actor: str, session_id: str) -> dict:
        """Create a new GUID-tagged code section record."""
        err = _require(params, 'description', 'section_type')
        if err:
            return {'success': False, 'message': f'ACTION: create_section — {err}'}
        from aixyad_db import create_code_section
        result = create_code_section(
            self.pdb,
            description=params['description'],
            section_type=params['section_type'],
            file_path=params.get('file_path', ''),
            language=params.get('language', ''),
            line_start=params.get('line_start'),
            line_end=params.get('line_end'),
            task_id=params.get('task_id'),
            subtask_id=params.get('subtask_id'),
            sprint_id=params.get('sprint_id'),
            created_by=actor,
            replaces=params.get('replaces'),
            guid=params.get('guid'),
        )
        return {
            'success': True,
            'message': (f"ACTION: create_section — CREATED GUID {result['guid']} "
                        f"(rev {result['revision']}, type={params['section_type']}, "
                        f"file={params.get('file_path', 'N/A')})")
        }

    def _do_edit_section(self, params: dict, actor: str, session_id: str) -> dict:
        """Replace a GUID-tagged code section with a new revision."""
        err = _require(params, 'guid', 'new_description')
        if err:
            return {'success': False, 'message': f'ACTION: edit_section — {err}'}
        from aixyad_db import replace_code_section
        result = replace_code_section(
            self.pdb,
            old_guid=params['guid'],
            new_description=params['new_description'],
            created_by=actor,
            task_id=params.get('task_id'),
            file_path=params.get('file_path'),
            line_start=params.get('line_start'),
            line_end=params.get('line_end'),
        )
        if not result:
            return {'success': False, 'message': f"ACTION: edit_section — GUID {params['guid']} NOT FOUND"}
        return {
            'success': True,
            'message': (f"ACTION: edit_section — REPLACED {params['guid']} with "
                        f"{result['guid']} (rev {result['revision']})")
        }

    def _do_find_section(self, params: dict, actor: str, session_id: str) -> dict:
        """Find GUID-tagged sections by GUID, description, file, or type."""
        from aixyad_db import get_code_section, search_code_sections
        # Exact GUID lookup
        if params.get('guid'):
            section = get_code_section(self.pdb, params['guid'])
            if section:
                lines = [
                    f"  GUID: {section['guid']}",
                    f"  Description: {section['description']}",
                    f"  Type: {section['section_type']}",
                    f"  File: {section.get('file_path', 'N/A')}",
                    f"  Lines: {section.get('line_start', '?')}-{section.get('line_end', '?')}",
                    f"  Revision: {section['revision']}",
                    f"  Replaced by: {section.get('replaced_by', 'None (current)')}",
                    f"  Created: {section['created_at']} by {section.get('created_by', '?')}",
                ]
                return {'success': True, 'message': f"ACTION: find_section — FOUND\n\n" + '\n'.join(lines)}
            return {'success': False, 'message': f"ACTION: find_section — GUID {params['guid']} NOT FOUND"}

        # Search
        results = search_code_sections(
            self.pdb,
            description=params.get('search'),
            section_type=params.get('section_type'),
            file_path=params.get('file_path'),
            task_id=params.get('task_id'),
            limit=params.get('limit', 20),
        )
        if not results:
            return {'success': True, 'message': "ACTION: find_section — 0 RESULTS"}
        lines = []
        for s in results:
            stale = " [STALE]" if s.get('location_stale') else ""
            lines.append(
                f"  {s['guid'][:12]}.. {s['section_type']:<10} "
                f"{s.get('file_path', 'N/A'):<40} "
                f"L{s.get('line_start', '?')}-{s.get('line_end', '?')}{stale} "
                f"rev{s['revision']} — {s['description'][:60]}"
            )
        return {
            'success': True,
            'message': f"ACTION: find_section — {len(results)} RESULTS\n\n" + '\n'.join(lines)
        }

    def _do_get_section_history(self, params: dict, actor: str, session_id: str) -> dict:
        """Get the revision chain for a GUID."""
        err = _require(params, 'guid')
        if err:
            return {'success': False, 'message': f'ACTION: get_section_history — {err}'}
        from aixyad_db import get_revision_chain
        chain = get_revision_chain(self.pdb, params['guid'])
        if not chain:
            return {'success': False, 'message': f"ACTION: get_section_history — GUID {params['guid']} NOT FOUND"}
        lines = []
        for s in chain:
            current = " (CURRENT)" if not s.get('replaced_by') else ""
            lines.append(
                f"  rev{s['revision']}: {s['guid'][:12]}.. "
                f"{s.get('file_path', 'N/A')} "
                f"by {s.get('created_by', '?')} at {s['created_at']}{current}\n"
                f"           {s['description'][:80]}"
            )
        return {
            'success': True,
            'message': f"ACTION: get_section_history — {len(chain)} REVISIONS\n\n" + '\n'.join(lines)
        }

    # ── Utility ────────────────────────────────────────────────────────

    @classmethod
    def get_available_actions(cls) -> list:
        """Return the list of DB actions for inclusion in AVAILABLE_ACTIONS."""
        return [
            {"name": "query_tasks",
             "description": "Query tasks by status/priority/complexity/agent",
             "params": {"status": "string (optional)", "priority": "string (optional)",
                        "complexity": "int (optional)", "agent": "string (optional)",
                        "limit": "int (optional, default 50)"}},
            {"name": "query_task_detail",
             "description": "Get full task detail with subtasks, history, files, sprint context",
             "params": {"task_id": "string (display ID, e.g. TASK-042)"}},
            {"name": "update_task",
             "description": "Update task status/priority/complexity/agent/description",
             "params": {"task_id": "string (required)", "status": "string (optional)",
                        "priority": "string (optional)", "complexity": "int (optional)",
                        "agent": "string (optional)", "title": "string (optional)",
                        "description": "string (optional)", "blocked_reason": "string (optional)"}},
            {"name": "add_task",
             "description": "Create a new task",
             "params": {"title": "string (required)", "description": "string (optional)",
                        "priority": "string (default MEDIUM)", "complexity": "int (default 3)",
                        "agent": "string (optional)", "parent_task_id": "string (optional, display ID)"}},
            {"name": "reorder_tasks",
             "description": "Move task to new position within its status group",
             "params": {"task_id": "string (required)", "position": "int (required, 0-based)"}},
            {"name": "add_checklist_item",
             "description": "Add a subtask/checklist item to a task",
             "params": {"task_id": "string (required, parent task)", "title": "string (required)", "description": "string (optional)"}},
            {"name": "check_item",
             "description": "Mark checklist item complete or incomplete",
             "params": {"subtask_id": "string (required, display ID)", "checked": "bool (default true)"}},
            {"name": "query_sprint",
             "description": "Get sprint info with tasks and status breakdown",
             "params": {"sprint_number": "int (optional, default latest)"}},
            {"name": "query_sprint_summary",
             "description": "Get sprint overview (alias for query_sprint)",
             "params": {"sprint_number": "int (optional, default latest)"}},
            {"name": "query_history",
             "description": "Get action history filtered by task/actor/session",
             "params": {"task_id": "string (optional)", "actor": "string (optional)",
                        "session_id": "string (optional)", "limit": "int (optional, default 30)"}},
            {"name": "query_task_timeline",
             "description": "Get chronological dot-point summary of all actions on a task",
             "params": {"task_id": "string (required, display ID)"}},
            {"name": "link_file_to_task",
             "description": "Associate a file with a task",
             "params": {"task_id": "string (required)", "file_path": "string (required)",
                        "relationship": "string (optional: related/modified/created/reviewed/depends_on/output)",
                        "notes": "string (optional)"}},
            {"name": "log_file_modification",
             "description": "Record a file create/edit/delete linked to a task",
             "params": {"task_id": "string (required)", "file_path": "string (required)",
                        "action": "string (required: create/edit/delete)",
                        "before_snippet": "string (optional)", "after_snippet": "string (optional)"}},
            {"name": "reserve_task",
             "description": "Reserve a task so no other agent picks it up. Returns false if already reserved.",
             "params": {"task_id": "string (required)", "agent": "string (optional, defaults to current actor)"}},
            {"name": "release_task",
             "description": "Release a task reservation so others can pick it up.",
             "params": {"task_id": "string (required)"}},
            {"name": "assign_task",
             "description": "Assign a task to an agent/provider and start working on it.",
             "params": {"task_id": "string (required)", "agent": "string (required)",
                        "provider": "string (optional)"}},
            {"name": "reserve_subtask",
             "description": "Reserve a subtask for an agent.",
             "params": {"subtask_id": "int (required)", "agent": "string (optional)"}},
            {"name": "query_prompt_logs",
             "description": "Query stored prompt/response exchange logs for audit.",
             "params": {"session_id": "string (optional)", "task_id": "string (optional)",
                        "provider": "string (optional)", "limit": "int (optional, default 20)"}},
            {"name": "discard_task",
             "description": "Soft-delete a task (archive it, can be restored later).",
             "params": {"task_id": "string (required)", "reason": "string (optional)"}},
            {"name": "restore_task",
             "description": "Restore an archived/discarded task back to backlog.",
             "params": {"task_id": "string (required)", "restore_to": "string (optional, default 'backlog')"}},
            {"name": "discard_file",
             "description": "Move a file to _Discarded folder (safe delete, can restore). Records in DB.",
             "params": {"file_path": "string (required)", "reason": "string (optional)"}},
            {"name": "restore_file",
             "description": "Restore a previously discarded file to its original location.",
             "params": {"discard_id": "int (optional)", "original_path": "string (optional — finds most recent)"}},
            {"name": "query_discarded_files",
             "description": "List discarded files, optionally filtered by path/date range.",
             "params": {"path": "string (optional, partial match)", "since": "string (optional, ISO date)", "until": "string (optional)"}},
            {"name": "query_file_history",
             "description": "Get complete history of a file (modifications + discards + restores).",
             "params": {"file_path": "string (required)"}},
            # GUID code section tracking
            {"name": "create_section",
             "description": "Create a GUID-tagged code section for tracking. Auto-generates GUID.",
             "params": {"description": "string (required — what the code does)",
                        "section_type": "string (required: function/method/class/struct/enum/namespace/section/param/config/block)",
                        "file_path": "string (optional)", "language": "string (optional)",
                        "line_start": "int (optional)", "line_end": "int (optional)",
                        "task_id": "int (optional)", "replaces": "string (optional — GUID this replaces)"}},
            {"name": "edit_section",
             "description": "Replace a GUID-tagged section with a new revision. Creates new GUID, links old→new.",
             "params": {"guid": "string (required — GUID to replace)",
                        "new_description": "string (required — updated description of the section)",
                        "file_path": "string (optional)", "line_start": "int (optional)",
                        "line_end": "int (optional)", "task_id": "int (optional)"}},
            {"name": "find_section",
             "description": "Find GUID-tagged code sections by GUID, description search, file, or type.",
             "params": {"guid": "string (optional — exact lookup)",
                        "search": "string (optional — description text search)",
                        "file_path": "string (optional)", "section_type": "string (optional)",
                        "task_id": "int (optional)", "limit": "int (optional, default 20)"}},
            {"name": "get_section_history",
             "description": "Get the full revision chain for a GUID (all versions from original to current).",
             "params": {"guid": "string (required)"}},
            # Subtask management
            {"name": "create_subtask",
             "description": "Create a subtask under a parent task",
             "params": {"parent_task_id": "string (required, display ID)", "title": "string (required)", "description": "string (optional)"}},
            {"name": "complete_subtask",
             "description": "Mark a subtask as done with a result",
             "params": {"subtask_id": "string (required, display ID)", "result": "string (required)"}},
            {"name": "store_handoff",
             "description": "Store a handoff record on a task (for agent-to-agent handoffs)",
             "params": {"task_id": "string (required)", "files_changed": "string[] (optional)", "tests_written": "string[] (optional)", "notes": "string (optional)"}},
        ]


# ── Self-test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    from aixyad_db import init_project_db

    print("=== DbActionHandler self-test ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        pdb = init_project_db(Path(tmpdir))
        handler = DbActionHandler(pdb)

        # Test add_task
        r = handler.handle('add_task', {'title': 'Implement shadow mapping',
                                         'priority': 'HIGH', 'complexity': 13},
                           actor='Developer', session_id='test-session')
        print(f"[{'OK' if r['success'] else 'FAIL'}] add_task")
        assert r['success'], r['message']

        # Extract display_id from result
        import re as _re
        match = _re.search(r'ID: (TASK-\d+)', r['message'])
        task_id = match.group(1) if match else 'TASK-001'

        # Test add_checklist_item
        r = handler.handle('add_checklist_item',
                           {'task_id': task_id, 'title': 'Research shadow techniques'},
                           actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] add_checklist_item")
        assert r['success'], r['message']

        sub_match = _re.search(r'ID: (SUB-\d+)', r['message'])
        sub_id = sub_match.group(1) if sub_match else 'SUB-001'

        # Test check_item
        r = handler.handle('check_item', {'subtask_id': sub_id, 'checked': True},
                           actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] check_item")
        assert r['success'], r['message']

        # Test update_task
        r = handler.handle('update_task', {'task_id': task_id, 'status': 'in_progress'},
                           actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] update_task")
        assert r['success'], r['message']

        # Test query_tasks
        r = handler.handle('query_tasks', {'status': 'in_progress'}, actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] query_tasks")
        assert r['success'], r['message']

        # Test query_task_detail
        r = handler.handle('query_task_detail', {'task_id': task_id}, actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] query_task_detail")
        assert r['success'], r['message']

        # Test query_task_timeline
        r = handler.handle('query_task_timeline', {'task_id': task_id}, actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] query_task_timeline")
        assert r['success'], r['message']

        # Test query_history
        r = handler.handle('query_history', {'actor': 'Developer'}, actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] query_history")
        assert r['success'], r['message']

        # Test link_file_to_task
        r = handler.handle('link_file_to_task',
                           {'task_id': task_id, 'file_path': 'engine/graphics/ShadowMap.hpp',
                            'relationship': 'created'},
                           actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] link_file_to_task")
        assert r['success'], r['message']

        # Test log_file_modification
        r = handler.handle('log_file_modification',
                           {'task_id': task_id, 'file_path': 'engine/graphics/ShadowMap.hpp',
                            'action': 'create', 'after_snippet': 'class ShadowMap { ... }'},
                           actor='Developer')
        print(f"[{'OK' if r['success'] else 'FAIL'}] log_file_modification")
        assert r['success'], r['message']

        # Test unknown action
        r = handler.handle('fly_to_moon', {}, actor='Developer')
        print(f"[{'OK' if not r['success'] else 'FAIL'}] unknown action rejected")
        assert not r['success']

        # Test missing required params
        r = handler.handle('query_task_detail', {}, actor='Developer')
        print(f"[{'OK' if not r['success'] else 'FAIL'}] missing params rejected")
        assert not r['success']

        pdb.close()

    print("\n=== All tests passed ===")
