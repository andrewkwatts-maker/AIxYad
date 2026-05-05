#!/usr/bin/env python3
"""
Automatica CLI — unified command-line interface.

Usage:
    automatica task list   --project PATH [--status STATUS]
    automatica task create --project PATH --title TITLE [--complexity N] [--description D]
    automatica sprint start  --project PATH [--max-tasks N]
    automatica sprint status --project PATH
    automatica agent list
    automatica agent run    --slug SLUG --task TASK_ID --project PATH
    automatica agent export --slug SLUG
    automatica provider list
    automatica provider ping --name NAME
    automatica config get  KEY
    automatica config set  KEY VALUE
    automatica config list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Ensure the engine package root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    return Path.home() / '.automatica' / 'settings.json'


def _load_settings() -> dict:
    p = _settings_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_settings(data: dict):
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            pass
    existing.update(data)
    p.write_text(json.dumps(existing, indent=2))


def _global_db():
    import automatica_db as adb
    db_path = Path.home() / '.automatica' / 'automatica.db'
    return adb.init_global_db(db_path)


def _project_db(project_path: str):
    import automatica_db as adb
    return adb.init_project_db(Path(project_path))


def _table(rows: list, cols: list):
    """Print a simple ASCII table."""
    if not rows:
        print('(no results)')
        return
    widths = [len(c) for c in cols]
    for row in rows:
        for i, c in enumerate(cols):
            widths[i] = max(widths[i], len(str(row.get(c, '') if isinstance(row, dict) else row[i])))
    fmt = '  '.join(f'{{:<{w}}}' for w in widths)
    print(fmt.format(*cols))
    print('  '.join('-' * w for w in widths))
    for row in rows:
        if isinstance(row, dict):
            vals = [str(row.get(c, '')) for c in cols]
        else:
            vals = [str(v) for v in row]
        print(fmt.format(*vals))


# ---------------------------------------------------------------------------
# F-19: task commands
# ---------------------------------------------------------------------------

def cmd_task_list(args):
    """List tasks for a project."""
    try:
        pdb = _project_db(args.project)
        import automatica_db as adb
        tasks = adb.list_project_tasks(pdb)
        if args.status:
            tasks = [t for t in tasks if t.get('status') == args.status]
        _table(tasks, ['display_id', 'title', 'status', 'complexity'])
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_task_create(args):
    """Create a new task in a project."""
    try:
        pdb = _project_db(args.project)
        import automatica_db as adb
        result = adb.create_task(
            pdb,
            title=args.title,
            description=args.description,
            complexity=args.complexity,
        )
        print(f"Created task {result['display_id']}: {args.title}")
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# F-19: sprint commands
# ---------------------------------------------------------------------------

def cmd_sprint_start(args):
    """Start a sprint for a project."""
    try:
        from sprint_runner import SprintRunner
        from agent_protocol import load_settings, get_provider_config

        settings = load_settings()
        provider_name = settings.get('provider', 'default')
        provider_cfg = get_provider_config(settings, provider_name)

        runner = SprintRunner(
            project_dir=Path(args.project),
            provider=provider_name,
            provider_config=provider_cfg,
        )

        # Determine next sprint number
        pdb = _project_db(args.project)
        row = pdb.execute("SELECT MAX(sprint_number) FROM sprints").fetchone()
        sprint_num = (row[0] or 0) + 1

        print(f"Starting sprint #{sprint_num} for {args.project}…")
        result = runner.run_sprint(
            sprint_number=sprint_num,
            max_points=getattr(args, 'max_tasks', 10) * 3,
        )
        print(f"Sprint #{sprint_num} done: {result.tasks_completed}/{result.tasks_planned} tasks, "
              f"velocity={result.velocity:.1f} pts/hr")
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_sprint_status(args):
    """Show latest sprint status for a project."""
    try:
        pdb = _project_db(args.project)
        row = pdb.execute(
            "SELECT sprint_number, status, goal, planned_points, completed_points "
            "FROM sprints ORDER BY sprint_number DESC LIMIT 1"
        ).fetchone()
        if not row:
            print('No sprints found.')
            return
        print(f"Sprint {row[0]}: {row[1]}")
        print(f"Goal:   {row[2]}")
        print(f"Points: {row[4] or 0}/{row[3] or 0} completed")
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# F-20: agent commands
# ---------------------------------------------------------------------------

def cmd_agent_list(args):
    """List all agent definitions."""
    try:
        conn = _global_db()
        rows = conn.execute(
            "SELECT slug, role, description, "
            "  COALESCE((SELECT ROUND(100.0*SUM(CASE WHEN success THEN 1 ELSE 0 END)/COUNT(*),1) "
            "            FROM usage_log ul JOIN agent_definitions ad2 ON ad2.slug = ul.agent_slug "
            "            WHERE ad2.slug = agent_definitions.slug), 0) AS success_rate "
            "FROM agent_definitions"
        ).fetchall()
        data = [dict(r) for r in rows]
        _table(data, ['slug', 'role', 'success_rate'])
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_agent_run(args):
    """Run a single agent on a task."""
    try:
        from sprint_runner import SprintRunner
        from agent_protocol import load_settings, get_provider_config

        settings = load_settings()
        provider_name = settings.get('provider', 'default')
        provider_cfg = get_provider_config(settings, provider_name)

        runner = SprintRunner(
            project_dir=Path(args.project),
            provider=provider_name,
            provider_config=provider_cfg,
        )
        print(f"Running agent '{args.slug}' on task '{args.task}'…")
        result = runner.run_single_task(args.task)
        status = 'OK' if result.success else 'FAIL'
        print(f"[{status}] {result.task_display_id} in {result.duration:.1f}s: {result.summary[:100]}")
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_agent_export(args):
    """Export an agent definition as JSON."""
    try:
        conn = _global_db()
        row = conn.execute(
            "SELECT * FROM agent_definitions WHERE slug = ?", (args.slug,)
        ).fetchone()
        if not row:
            print(f"Agent '{args.slug}' not found.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(dict(row), indent=2))
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# F-21: provider commands
# ---------------------------------------------------------------------------

def cmd_provider_list(args):
    """List all configured providers."""
    try:
        conn = _global_db()
        rows = conn.execute(
            "SELECT name, provider_type, base_url, model_name, enabled FROM providers ORDER BY id"
        ).fetchall()
        data = [dict(r) for r in rows]
        _table(data, ['name', 'provider_type', 'base_url', 'model_name', 'enabled'])
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_provider_ping(args):
    """Ping a provider to check reachability and latency."""
    try:
        conn = _global_db()
        row = conn.execute(
            "SELECT base_url FROM providers WHERE LOWER(name) = LOWER(?)", (args.name,)
        ).fetchone()
        if not row:
            print(f"Provider '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)
        url = (row['base_url'] or '').rstrip('/')
        if not url:
            print(f"Provider '{args.name}' has no base_url configured.")
            return
        import requests
        t0 = time.monotonic()
        try:
            r = requests.get(f'{url}/api/tags', timeout=10)
            latency = int((time.monotonic() - t0) * 1000)
            print(f"{args.name}: {r.status_code}  latency={latency}ms  url={url}")
        except Exception as e:
            latency = int((time.monotonic() - t0) * 1000)
            print(f"{args.name}: UNREACHABLE  latency={latency}ms  error={e}")
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# F-22: config commands
# ---------------------------------------------------------------------------

def cmd_config_get(args):
    """Print the value of a settings key."""
    settings = _load_settings()
    parts = args.key.split('.')
    val = settings
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            val = None
            break
    if val is None:
        print(f"(not set)")
    else:
        print(json.dumps(val, indent=2) if isinstance(val, (dict, list)) else str(val))


def cmd_config_set(args):
    """Set a settings key to a value (JSON or string)."""
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value  # treat as plain string
    _save_settings({args.key: value})
    print(f"Set {args.key} = {json.dumps(value)}")


def cmd_config_list(args):
    """List all settings as key=value."""
    settings = _load_settings()

    def _flatten(d, prefix=''):
        for k, v in d.items():
            full = f'{prefix}{k}' if prefix else k
            if isinstance(v, dict):
                yield from _flatten(v, full + '.')
            else:
                yield full, v

    for key, val in _flatten(settings):
        print(f"{key}={json.dumps(val) if isinstance(val, (list, dict)) else val}")


# ---------------------------------------------------------------------------
# Main / argument parser
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='automatica',
        description='Automatica CLI — manage tasks, sprints, agents, providers and config.',
    )
    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')

    # ── task ──────────────────────────────────────────────────────────────────
    task_p = subparsers.add_parser('task', help='Task management')
    task_sub = task_p.add_subparsers(dest='action', metavar='ACTION')

    tl = task_sub.add_parser('list', help='List tasks')
    tl.add_argument('--project', required=True, help='Path to project directory')
    tl.add_argument('--status', default=None, help='Filter by status')
    tl.set_defaults(func=cmd_task_list)

    tc = task_sub.add_parser('create', help='Create a task')
    tc.add_argument('--project', required=True)
    tc.add_argument('--title', required=True)
    tc.add_argument('--complexity', type=int, default=3)
    tc.add_argument('--description', default='')
    tc.set_defaults(func=cmd_task_create)

    # ── sprint ────────────────────────────────────────────────────────────────
    sprint_p = subparsers.add_parser('sprint', help='Sprint management')
    sprint_sub = sprint_p.add_subparsers(dest='action', metavar='ACTION')

    ss = sprint_sub.add_parser('start', help='Start a sprint')
    ss.add_argument('--project', required=True)
    ss.add_argument('--max-tasks', type=int, default=10)
    ss.set_defaults(func=cmd_sprint_start)

    sst = sprint_sub.add_parser('status', help='Show latest sprint status')
    sst.add_argument('--project', required=True)
    sst.set_defaults(func=cmd_sprint_status)

    # ── agent ─────────────────────────────────────────────────────────────────
    agent_p = subparsers.add_parser('agent', help='Agent management')
    agent_sub = agent_p.add_subparsers(dest='action', metavar='ACTION')

    al = agent_sub.add_parser('list', help='List agents')
    al.set_defaults(func=cmd_agent_list)

    ar = agent_sub.add_parser('run', help='Run an agent on a task')
    ar.add_argument('--slug', required=True)
    ar.add_argument('--task', required=True, dest='task')
    ar.add_argument('--project', required=True)
    ar.set_defaults(func=cmd_agent_run)

    ae = agent_sub.add_parser('export', help='Export agent definition as JSON')
    ae.add_argument('--slug', required=True)
    ae.set_defaults(func=cmd_agent_export)

    # ── provider ──────────────────────────────────────────────────────────────
    provider_p = subparsers.add_parser('provider', help='Provider management')
    provider_sub = provider_p.add_subparsers(dest='action', metavar='ACTION')

    pl = provider_sub.add_parser('list', help='List providers')
    pl.set_defaults(func=cmd_provider_list)

    pp = provider_sub.add_parser('ping', help='Ping a provider')
    pp.add_argument('--name', required=True)
    pp.set_defaults(func=cmd_provider_ping)

    # ── config ────────────────────────────────────────────────────────────────
    config_p = subparsers.add_parser('config', help='Configuration management')
    config_sub = config_p.add_subparsers(dest='action', metavar='ACTION')

    cg = config_sub.add_parser('get', help='Get a config value')
    cg.add_argument('key', help='Dot-separated key (e.g. notifications.smtp_host)')
    cg.set_defaults(func=cmd_config_get)

    cs = config_sub.add_parser('set', help='Set a config value')
    cs.add_argument('key', help='Dot-separated key')
    cs.add_argument('value', help='Value (JSON or plain string)')
    cs.set_defaults(func=cmd_config_set)

    clist = config_sub.add_parser('list', help='List all config key=value pairs')
    clist.set_defaults(func=cmd_config_list)

    # ── dispatch ──────────────────────────────────────────────────────────────
    args = parser.parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == '__main__':
    main()
