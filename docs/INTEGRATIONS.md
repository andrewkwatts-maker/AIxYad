# Automatica — Integrations Guide

This document covers all external integrations available in Automatica: VCS (Git),
email notifications, Slack, webhooks, GitHub/GitLab, and the unified CLI.

---

## 1. VCS Integration (Git)

Automatica can automatically commit and push after each completed task.

### Configuration

All git settings live in `~/.automatica/settings.json` under the following keys:

| Key                  | Type   | Default    | Description                              |
|----------------------|--------|------------|------------------------------------------|
| `git_auto_commit`    | bool   | `false`    | Commit after each successful task        |
| `git_auto_push`      | bool   | `false`    | Push to remote after auto-commit         |
| `git_commit_prefix`  | string | `"feat: "` | Prefix prepended to every commit message |
| `git_branch_prefix`  | string | `"auto/"`  | Prefix for new branches created via UI   |
| `git_remote`         | string | `"origin"` | Remote name for push/pull operations     |
| `git_default_branch` | string | `"main"`   | Target branch for push                   |

### Setting via UI

Open **Settings → VCS** in Automatica-App. Fill in the fields and toggle the
auto-commit / auto-push switches, then click **Save VCS Settings**.

### Setting via CLI

```bash
python scripts/cli.py config set git_auto_commit true
python scripts/cli.py config set git_commit_prefix "feat: "
python scripts/cli.py config set git_remote origin
```

### Project VCS Panel

Each project has a **VCS** tab with:

- Branch badge (current branch name)
- Dirty file count (amber if > 0)
- Ahead / behind remote indicator
- **Pull** / **Push** buttons (reads `git_remote` and `git_default_branch` from settings)
- **Generate Msg** — calls `/api/tasks/commit-message` to draft a commit message
- **Commit** — stages all changes and commits with the drafted message
- **View Diff** — shows `git diff --stat HEAD` output
- **New Branch** — opens a dialog pre-filled with `git_branch_prefix`

---

## 2. Email Notification Setup

Automatica can send an email after each sprint completes.

### Configuration

Add a `notifications` block to `~/.automatica/settings.json`:

```json
{
  "notifications": {
    "email_enabled": true,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "you@example.com",
    "smtp_password": "your-app-password",
    "notify_email": "team@example.com"
  }
}
```

> **Gmail**: Use an App Password (Google Account → Security → App passwords).
> **Outlook / Office 365**: Use `smtp.office365.com` port 587.

### Events that trigger email

| Event             | When                      |
|-------------------|---------------------------|
| `sprint_complete` | After `review_sprint()` completes |

### Testing

```bash
python -c "
from notifications import send_sprint_complete
send_sprint_complete({'sprint_number': 1, 'tasks_completed': 3,
                      'tasks_failed': 0, 'velocity': 5.2, 'goal': 'Test email'})
"
```

---

## 3. Webhook Integration

### Outgoing webhooks (Automatica → external)

Add a `webhook_url` to the `notifications` block:

```json
{
  "notifications": {
    "webhook_url": "https://your-server.com/automatica-hook"
  }
}
```

Automatica will POST JSON to this URL on the following events:

| Event              | When                        |
|--------------------|-----------------------------|
| `sprint_start`     | Sprint begins               |
| `sprint_complete`  | Sprint review finishes      |

Payload shape:

```json
{
  "event": "sprint_complete",
  "timestamp": "2026-04-09T12:00:00.000000",
  "sprint_number": 5,
  "tasks_completed": 4,
  "velocity": 6.1
}
```

### Incoming webhooks (external → Automatica)

POST to `POST /api/webhooks/receive` on the running Automatica server:

```bash
curl -X POST http://localhost:8765/api/webhooks/receive \
     -H "Content-Type: application/json" \
     -d '{"event": "ci_passed", "branch": "main"}'
```

The event is broadcast to all connected WebSocket clients (`/ws/logs`).

---

## 4. Slack Integration

Add `slack_webhook_url` to the `notifications` block:

```json
{
  "notifications": {
    "slack_webhook_url": "https://hooks.slack.com/services/T.../B.../..."
  }
}
```

### Events that trigger Slack messages

| Event              | Message example                                          |
|--------------------|----------------------------------------------------------|
| `sprint_complete`  | `Sprint 5 complete: 4/5 tasks, velocity 6.1 pts/hr`     |

### Setting up the Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App**
2. Choose **From scratch** → name it `Automatica`
3. Under **Incoming Webhooks**, activate and click **Add New Webhook to Workspace**
4. Copy the webhook URL and paste it into `settings.json`

---

## 5. GitHub / GitLab Setup Notes

### Auto-commit workflow

With `git_auto_commit: true`, Automatica commits after each successful task with
a message like `feat: implement authentication module`.

With `git_auto_push: true`, it immediately pushes to `git_remote`/`git_default_branch`.

### Recommended branch strategy

```
main          ← production-ready
auto/sprint-N ← Automatica creates this with "New Branch" per sprint
```

Set `git_branch_prefix` to `"auto/"` and create a branch per sprint before running.

### GitHub Actions integration (incoming webhook)

In your `.github/workflows/ci.yml`:

```yaml
- name: Notify Automatica
  if: success()
  run: |
    curl -X POST ${{ secrets.AUTOMATICA_URL }}/api/webhooks/receive \
      -H "Content-Type: application/json" \
      -d '{"event":"ci_passed","branch":"${{ github.ref_name }}"}'
```

Set `AUTOMATICA_URL` in your GitHub repository secrets.

---

## 6. CLI Usage Guide

The unified CLI lives at `scripts/cli.py` (engine root).

```bash
# From Automatica engine root:
python scripts/cli.py <command> <action> [options]
```

### Task commands

```bash
# List all tasks
python scripts/cli.py task list --project /path/to/project

# Filter by status
python scripts/cli.py task list --project /path/to/project --status backlog

# Create a task
python scripts/cli.py task create --project /path/to/project \
    --title "Add authentication" --complexity 5
```

### Sprint commands

```bash
# Start a sprint (runs planning + execution + review)
python scripts/cli.py sprint start --project /path/to/project

# Show latest sprint status
python scripts/cli.py sprint status --project /path/to/project
```

### Agent commands

```bash
# List all agents with success rates
python scripts/cli.py agent list

# Run a specific agent on a task
python scripts/cli.py agent run --slug developer --task T-001 --project /path/to/project

# Export agent definition as JSON
python scripts/cli.py agent export --slug developer
```

### Provider commands

```bash
# List all providers
python scripts/cli.py provider list

# Ping a provider (latency check)
python scripts/cli.py provider ping --name ollama-local
```

### Config commands

```bash
# Get a config value (supports dot notation)
python scripts/cli.py config get git_auto_commit
python scripts/cli.py config get notifications.smtp_host

# Set a config value
python scripts/cli.py config set git_auto_commit true
python scripts/cli.py config set git_commit_prefix "feat: "

# List all settings
python scripts/cli.py config list
```

---

## OpenAPI / REST API

The Automatica server exposes an OpenAPI spec at:

- **Swagger UI**: `http://localhost:8765/docs`
- **ReDoc**: `http://localhost:8765/redoc`
- **Raw JSON spec**: `http://localhost:8765/openapi.json` (also at `/api/openapi.json`)
