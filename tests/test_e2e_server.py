"""E2E test: add project → task → sprint → verify DB state."""
import sqlite3, pytest, tempfile
from pathlib import Path

def test_full_server_workflow(tmp_path):
    """Create project → add task → verify DB state without running the agent."""
    import sys
    sys.path.insert(0, r'H:\Github\Automatica')
    from automatica_db import init_project_db, create_task, get_tasks

    # Create a project DB
    proj_dir = tmp_path / 'test_project'
    proj_dir.mkdir()
    db_dir = proj_dir / '.automatica'
    db_dir.mkdir()

    pconn = sqlite3.connect(str(db_dir / 'project.db'))
    pconn.row_factory = sqlite3.Row
    init_project_db(proj_dir)

    # Re-open via init_project_db to get configured connection
    pconn.close()
    pconn = init_project_db(proj_dir)

    # Create a task
    task = create_task(pconn, 'Test task', 'A test task', complexity=3)
    assert task['display_id'].startswith('TASK-')

    # Verify task is in DB
    tasks = get_tasks(pconn)
    assert any(t['display_id'] == task['display_id'] for t in tasks)

    # Create sprint and assign
    from automatica_db import create_sprint
    sprint = create_sprint(pconn, sprint_number=1, goal='Test sprint')
    assert sprint['id'] > 0

    # Verify sprint in DB
    row = pconn.execute("SELECT * FROM sprints WHERE id=?", (sprint['id'],)).fetchone()
    assert row['goal'] == 'Test sprint'
    pconn.close()
