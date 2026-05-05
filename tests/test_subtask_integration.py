"""
Integration test: full subtask lifecycle.

S6-W11: test_subtask_lifecycle_auto_completes_parent
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automatica_db import init_project_db, create_task, create_subtask, complete_subtask


def _make_pconn(tmp_path):
    """Create a file-backed project DB (uses the real schema + triggers)."""
    conn = init_project_db(tmp_path)
    return conn


def test_subtask_lifecycle_auto_completes_parent(tmp_path):
    """
    Full subtask lifecycle:
      1. Create parent task
      2. Create two subtasks
      3. Complete first → parent still in_progress
      4. Complete second → parent auto-done
    """
    pconn = _make_pconn(tmp_path)

    # Create parent task
    t = create_task(pconn, 'Implement feature', description='desc', complexity=3)
    did = t['display_id']

    # Create two subtasks
    s1 = create_subtask(pconn, did, 'Write code', '')
    s2 = create_subtask(pconn, did, 'Write tests', '')

    # Complete first subtask — parent should still be in_progress (or todo)
    r1 = complete_subtask(pconn, s1['display_id'], 'code written')
    assert r1.get('success') is True, f"complete_subtask s1 failed: {r1}"
    assert not r1.get('parent_completed'), \
        "Parent should NOT be completed after first subtask"

    # Complete second subtask — parent should now be done
    r2 = complete_subtask(pconn, s2['display_id'], 'tests written')
    assert r2.get('success') is True, f"complete_subtask s2 failed: {r2}"
    assert r2.get('parent_completed'), \
        "Parent SHOULD be completed after all subtasks are done"

    # Verify parent task status in DB
    row = pconn.execute(
        "SELECT status FROM tasks WHERE display_id=?", (did,)
    ).fetchone()
    assert row is not None, "Parent task row should exist"
    assert row['status'] == 'done', \
        f"Expected parent status='done', got: {row['status']!r}"

    pconn.close()
