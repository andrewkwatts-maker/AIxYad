# Recursive Subtask System

## Overview

The recursive subtask system allows agents to break down complex tasks into smaller, manageable subtasks. An agent can create subtasks, work on them independently, and track their completion with results. The parent task should only be marked as done after all subtasks are completed or explicitly abandoned.

## Database Schema

The `subtasks` table includes the following key fields for recursive task management:

- `id` - Auto-incrementing primary key
- `display_id` - Unique human-readable ID (e.g., SUB-001)
- `task_id` - Foreign key to parent task
- `title` - Subtask title
- `description` - Optional detailed description
- `status` - One of: 'todo', 'in_progress', 'done', 'blocked'
- `iterations` - Counter tracking how many times the subtask was worked on
- `result` - Text field storing the completion result/summary
- `created_at` - Timestamp when subtask was created
- `completed_at` - Timestamp when marked done
- `completed_by` - Agent that completed the subtask

## Available Actions

### create_subtask

Create a new subtask under a parent task.

**Parameters:**
- `task_id` (string, required) - Parent task display ID (e.g., 'TASK-042')
- `title` (string, required) - Subtask title
- `description` (string, optional) - Subtask description

**Returns:**
```json
{
  "success": true,
  "message": "ACTION: create_subtask — Created subtask SUB-001 under TASK-042: Implement login validation"
}
```

**Example:**
```json
{
  "action": "create_subtask",
  "params": {
    "task_id": "TASK-042",
    "title": "Implement login validation",
    "description": "Add email and password validation to login form"
  }
}
```

### complete_subtask

Mark a subtask as complete with a result summary.

**Parameters:**
- `subtask_id` (string, required) - Subtask display ID (e.g., 'SUB-001')
- `result` (string, optional) - Completion result/summary

**Returns:**
```json
{
  "success": true,
  "message": "ACTION: complete_subtask — Completed SUB-001\n⋯ 2 subtask(s) remaining for TASK-042"
}
```

When all subtasks are complete:
```json
{
  "success": true,
  "message": "ACTION: complete_subtask — Completed SUB-003\n✓ All subtasks complete for TASK-042. Parent task can now be marked done."
}
```

**Example:**
```json
{
  "action": "complete_subtask",
  "params": {
    "subtask_id": "SUB-001",
    "result": "Added email regex validation and password length check (min 8 chars)"
  }
}
```

## Agent Workflow

### 1. Break Down Complex Tasks

When an agent encounters a complex task, it can break it into subtasks:

```json
// Agent creates subtasks for a complex feature
{"action": "create_subtask", "params": {"task_id": "TASK-100", "title": "Design database schema"}}
{"action": "create_subtask", "params": {"task_id": "TASK-100", "title": "Implement API endpoints"}}
{"action": "create_subtask", "params": {"task_id": "TASK-100", "title": "Write unit tests"}}
```

### 2. Work on Subtasks

The agent can then work on each subtask independently:

```json
// Read files, make changes, etc.
{"action": "read_file", "params": {"path": "src/models/schema.py"}}
{"action": "edit_file", "params": {...}}
```

### 3. Complete Subtasks

After completing work on a subtask, mark it done with a result:

```json
{
  "action": "complete_subtask",
  "params": {
    "subtask_id": "SUB-042",
    "result": "Created User and Post models with foreign key relationships"
  }
}
```

### 4. Check Completion Status

The `complete_subtask` action returns information about remaining subtasks. When all are done, the agent should call `step_done` on the parent task:

```json
// After all subtasks are complete
{
  "action": "update_task",
  "params": {
    "task_id": "TASK-100",
    "status": "done"
  }
}

{
  "action": "step_done",
  "params": {
    "summary": "Completed all subtasks for user authentication feature"
  }
}
```

## Permissions

Both actions require the `can_write_file` permission (same as other task mutation actions).

## Database Functions

### Python API

```python
from automatica_db import create_subtask, complete_subtask

# Create a subtask
result = create_subtask(
    conn,
    task_display_id="TASK-042",
    title="Implement validation",
    description="Add form validation logic"
)
# Returns: {'id': 1, 'display_id': 'SUB-001', 'task_id': 42}

# Complete a subtask
result = complete_subtask(
    conn,
    subtask_display_id="SUB-001",
    result="Added email and password validation",
    by="developer-agent"
)
# Returns: {
#   'success': True,
#   'subtask_id': 'SUB-001',
#   'remaining_count': 2,
#   'parent_task_id': 'TASK-042',
#   'task_id': 42
# }
```

## Migration

For existing databases, the schema migration is handled automatically when the database is opened. The migration adds:

- `iterations` INTEGER DEFAULT 0
- `result` TEXT DEFAULT ''

To manually migrate existing project databases:

```bash
python scripts/migrate_subtasks_schema.py [path/to/project.db]
```

Or migrate all project databases in the repository:

```bash
python scripts/migrate_subtasks_schema.py
```

## Testing

Run the test suite to verify the implementation:

```bash
python test_subtask_system.py
```

This tests:
- Creating subtasks linked to parent tasks
- Completing subtasks with results
- Tracking remaining subtasks
- Verifying parent task can be marked done when all subtasks complete
