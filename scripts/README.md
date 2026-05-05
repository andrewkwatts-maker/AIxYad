# Automatica Scripts Reference

This directory contains helper scripts that agents and developers can use to perform common operations efficiently.

## Quick Reference

| Script | Purpose | Common Usage |
|--------|---------|--------------|
| `pop-task.sh` | Get next task | `./pop-task.sh` |
| `end-task.sh` | Complete current task | `./end-task.sh --push` |
| `inject-task.sh` | Add task to TODO.md | `./inject-task.sh -t "Task title" -p HIGH` |
| `commit-task.sh` | Commit with task message | `./commit-task.sh --push` |
| `backup-file.sh` | Backup before editing | `./backup-file.sh src/file.js` |
| `diff-file.sh` | Diff against backup | `./diff-file.sh src/file.js --cleanup` |
| `run-tests.sh` | Run project tests | `./run-tests.sh --coverage` |
| `list-tasks.sh` | Show TODO backlog | `./list-tasks.sh --priority HIGH` |
| `cleanup-temp.sh` | Clean backup directory | `./cleanup-temp.sh --force` |

## Task Lifecycle

### Complete Workflow
```bash
# 1. Get next task
./scripts/pop-task.sh

# 2. Read CURRENT_TASK.md and implement

# 3. Backup, code, test, commit cycle (see Developer Agent)

# 4. Complete the task
./scripts/end-task.sh --push

# 5. Repeat
./scripts/pop-task.sh
```

### pop-task.sh

Extracts the next task from TODO.md into CURRENT_TASK.md.

```bash
# Get next task
./scripts/pop-task.sh

# Preview without extracting
./scripts/pop-task.sh --peek
```

### end-task.sh

Completes current task, archives to Completed.md, commits/pushes.

```bash
# Complete and commit
./scripts/end-task.sh

# Complete, commit, and push
./scripts/end-task.sh --push

# Complete without committing
./scripts/end-task.sh --no-commit
```

## Script Details

### inject-task.sh

Adds a new task to TODO.md in the standardized format.

```bash
./scripts/inject-task.sh \
  -t "Implement user authentication" \
  -d "Add OAuth2 login flow with Google and GitHub providers" \
  -p HIGH \
  -a Developer \
  -v "Security - enables user login" \
  -c "OAuth flow works,Tokens stored securely,Tests pass"
```

**Options:**
- `-t, --title` - Task title (required)
- `-d, --desc` - Detailed description
- `-p, --priority` - HIGH, MEDIUM, LOW (default: MEDIUM)
- `-a, --agent` - Recommended agent (default: Developer)
- `-v, --value` - Business value description
- `-c, --criteria` - Acceptance criteria (comma-separated)
- `--depends` - Dependencies (comma-separated)
- `--parallel` - Parallel group name
- `--position` - top or bottom (default: bottom)

### commit-task.sh

Commits changes using the current task as the commit message.

```bash
# Commit only
./scripts/commit-task.sh

# Commit and push
./scripts/commit-task.sh --push

# With custom message
./scripts/commit-task.sh --message "fix: resolve login bug"
```

**Options:**
- `--push` - Push after commit
- `--message, -m` - Override commit message

**Reads from settings.json:**
- `vcs.type` - git or svn
- `vcs.git.push_on_task_complete` - Auto-push setting
- `vcs.git.use_task_as_commit_message` - Use CURRENT_TASK.md

### backup-file.sh

Creates a backup copy of a file before editing.

```bash
./scripts/backup-file.sh src/components/Login.tsx
# Output: Backup created: ./temp/src/components/Login.tsx
```

**Returns:** Path to the backup file

### diff-file.sh

Shows differences between a file and its backup.

```bash
# Full diff
./scripts/diff-file.sh src/components/Login.tsx

# Just check if different
./scripts/diff-file.sh src/components/Login.tsx --brief

# Diff and delete backup
./scripts/diff-file.sh src/components/Login.tsx --cleanup
```

**Options:**
- `--brief` - Only show if files differ
- `--cleanup` - Delete backup after diff

### run-tests.sh

Runs project tests using configured or auto-detected test command.

```bash
# Run tests
./scripts/run-tests.sh

# Run with coverage
./scripts/run-tests.sh --coverage

# Custom threshold
./scripts/run-tests.sh --coverage --threshold 90
```

**Options:**
- `--coverage` - Run coverage analysis
- `--threshold` - Coverage percentage threshold

**Auto-detects:** npm, pytest, cargo, go, maven, gradle

### list-tasks.sh

Lists tasks from TODO.md with filtering.

```bash
# All tasks
./scripts/list-tasks.sh

# High priority only
./scripts/list-tasks.sh --priority HIGH

# Tasks for specific agent
./scripts/list-tasks.sh --agent Tester

# Brief output
./scripts/list-tasks.sh --brief
```

**Options:**
- `--priority, -p` - Filter by priority
- `--agent, -a` - Filter by agent
- `--brief, -b` - Show only titles

### cleanup-temp.sh

Cleans up the temp backup directory.

```bash
# Interactive cleanup
./scripts/cleanup-temp.sh

# Force cleanup
./scripts/cleanup-temp.sh --force

# Only old backups
./scripts/cleanup-temp.sh --older-than 7

# Preview what would be deleted
./scripts/cleanup-temp.sh --dry-run
```

**Options:**
- `--force, -f` - No confirmation
- `--older-than` - Days threshold
- `--dry-run` - Preview only

## Agent Usage Guidelines

### Developer Agent Workflow

```bash
# 1. Before editing files
./scripts/backup-file.sh src/module.js

# 2. Make changes...

# 3. Review changes
./scripts/diff-file.sh src/module.js

# 4. Run tests
./scripts/run-tests.sh

# 5. Commit (uses current task as message)
./scripts/commit-task.sh --push

# 6. Cleanup backup
./scripts/diff-file.sh src/module.js --cleanup
```

### Tester Agent Workflow

```bash
# Run tests with coverage
./scripts/run-tests.sh --coverage --threshold 80

# Add follow-up task if coverage low
./scripts/inject-task.sh \
  -t "Increase test coverage for auth module" \
  -p MEDIUM \
  -a Tester
```

### ScrumMaster Agent Workflow

```bash
# View current backlog
./scripts/list-tasks.sh

# Add broken-down tasks
./scripts/inject-task.sh -t "Sub-task 1" -p HIGH --position top
./scripts/inject-task.sh -t "Sub-task 2" -p HIGH --position top
```

## Configuration

Scripts read configuration from `settings.json` in the project root. Key settings:

```json
{
  "vcs": {
    "type": "git",
    "git": {
      "push_on_task_complete": true,
      "use_task_as_commit_message": true
    }
  },
  "paths": {
    "temp_backup": "./temp",
    "todo_file": "./TODO.md"
  },
  "testing": {
    "coverage_threshold": 80,
    "test_command": "npm test"
  }
}
```

## Logging

All scripts log operations to `./logs/`:
- `inject-task.log` - Task injection history
- `commit-task.log` - Commit history
- `backup.log` - Backup operations
- `tests.log` - Test run results
- `cleanup.log` - Cleanup operations
