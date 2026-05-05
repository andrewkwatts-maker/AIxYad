"""Performance regression tests."""
import pytest, time, sqlite3

def test_1000_task_query_under_1s(tmp_path):
    """Inserting and querying 1000 tasks should complete in under 1s."""
    import sys
    sys.path.insert(0, r'H:\Github\Automatica')
    from automatica_db import init_project_db

    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    # Use a temp dir to init schema, then use in-memory for speed
    proj_dir = tmp_path / 'perf_project'
    proj_dir.mkdir()
    real_conn = init_project_db(proj_dir)

    # Use real_conn for schema, then test with it
    t0 = time.monotonic()
    for i in range(1000):
        real_conn.execute(
            "INSERT INTO tasks (display_id, title, description, status, complexity) VALUES (?,?,?,?,?)",
            (f'TASK-{i+9000:04d}', f'Task {i}', 'desc', 'backlog', 3)
        )
    real_conn.commit()

    # Query all
    tasks = real_conn.execute("SELECT * FROM tasks WHERE status='backlog'").fetchall()
    elapsed = time.monotonic() - t0

    assert len(tasks) >= 1000
    assert elapsed < 1.0, f'1000 task query took {elapsed:.2f}s (limit: 1.0s)'
    real_conn.close()

def test_action_log_query_under_500ms(tmp_path):
    """10000 action_log entries should query in under 500ms with proper index."""
    import sys
    sys.path.insert(0, r'H:\Github\Automatica')
    from automatica_db import init_project_db

    proj_dir = tmp_path / 'log_perf_project'
    proj_dir.mkdir()
    conn = init_project_db(proj_dir)

    # Insert 10000 log entries
    for i in range(10000):
        conn.execute(
            "INSERT INTO action_log (scope, scope_id, actor, action, detail, session_id) VALUES (?,?,?,?,?,?)",
            ('task', 1, 'developer', 'read_file', f'log {i}', f'sess-{i}')
        )
    conn.commit()

    t0 = time.monotonic()
    rows = conn.execute(
        "SELECT * FROM action_log WHERE actor='developer' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    elapsed = time.monotonic() - t0

    assert len(rows) == 50
    assert elapsed < 0.5, f'10k log query took {elapsed:.2f}s (limit: 0.5s)'
    conn.close()
