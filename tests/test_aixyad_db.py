"""
pytest unit tests for aixyad_db.create_task(), update_task(), subtasks,
handoff_json, and escalation_log.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aixyad_db import (
    init_project_db,
    create_task,
    update_task,
    create_subtask,
    complete_subtask,
    log_escalation,
)


@pytest.fixture
def db(tmp_path):
    """Initialise a fresh in-memory-style project DB in a temp dir."""
    conn = init_project_db(tmp_path)
    yield conn
    conn.close()


# ── create_task ────────────────────────────────────────────────────────────────

def test_create_task_returns_dict(db):
    result = create_task(db, title="Test task")
    assert isinstance(result, dict)


def test_create_task_has_id_and_display_id(db):
    result = create_task(db, title="Test task")
    assert "id" in result
    assert "display_id" in result


def test_create_task_id_is_int(db):
    result = create_task(db, title="Test task")
    assert isinstance(result["id"], int)


def test_create_task_display_id_is_string(db):
    result = create_task(db, title="Test task")
    assert isinstance(result["display_id"], str)
    assert len(result["display_id"]) > 0


def test_create_task_persists_in_db(db):
    result = create_task(db, title="Persisted task")
    display_id = result["display_id"]
    row = db.execute(
        "SELECT title FROM tasks WHERE display_id = ?", (display_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "Persisted task"


def test_create_multiple_tasks_unique_ids(db):
    r1 = create_task(db, title="Task A")
    r2 = create_task(db, title="Task B")
    assert r1["id"] != r2["id"]
    assert r1["display_id"] != r2["display_id"]


# ── update_task ────────────────────────────────────────────────────────────────

def test_update_task_returns_true_on_success(db):
    r = create_task(db, title="Updatable")
    ok = update_task(db, r["display_id"], status="in_progress")
    assert ok is True


def test_update_task_status_changes(db):
    r = create_task(db, title="Status test")
    update_task(db, r["display_id"], status="done")
    row = db.execute(
        "SELECT status FROM tasks WHERE display_id = ?", (r["display_id"],)
    ).fetchone()
    assert row[0] == "done"


def test_update_task_title_changes(db):
    r = create_task(db, title="Old title")
    update_task(db, r["display_id"], title="New title")
    row = db.execute(
        "SELECT title FROM tasks WHERE display_id = ?", (r["display_id"],)
    ).fetchone()
    assert row[0] == "New title"


def test_update_task_nonexistent_returns_false(db):
    ok = update_task(db, "TASK-9999", status="done")
    assert ok is False


# ── S6-W03: TestSubtaskSystem ──────────────────────────────────────────────────

class TestSubtaskSystem:

    def test_create_subtask_links_to_parent(self, tmp_path):
        """create_subtask should insert a row in subtasks linked to the parent task."""
        conn = init_project_db(tmp_path)
        parent = create_task(conn, title='parent task')
        sub = create_subtask(conn, parent['display_id'], 'sub title', '')

        assert 'display_id' in sub, "create_subtask should return display_id"
        row = conn.execute(
            "SELECT task_id FROM subtasks WHERE display_id = ?", (sub['display_id'],)
        ).fetchone()
        assert row is not None, "subtask row should exist in DB"
        assert row['task_id'] == parent['id'], \
            f"subtask task_id {row['task_id']} != parent id {parent['id']}"
        conn.close()

    def test_complete_subtask_updates_status(self, tmp_path):
        """complete_subtask should set status='done' and store the result."""
        conn = init_project_db(tmp_path)
        parent = create_task(conn, title='parent')
        sub = create_subtask(conn, parent['display_id'], 'write something', '')

        result = complete_subtask(conn, sub['display_id'], 'done result')

        assert result['success'] is True
        row = conn.execute(
            "SELECT status, description FROM subtasks WHERE display_id = ?",
            (sub['display_id'],)
        ).fetchone()
        assert row['status'] == 'done', f"Expected status='done', got: {row['status']}"
        assert row['description'] == 'done result', \
            f"Expected result stored in description, got: {row['description']}"
        conn.close()

    def test_all_subtasks_done_completes_parent(self, tmp_path):
        """Completing all subtasks should auto-complete the parent task."""
        conn = init_project_db(tmp_path)
        parent = create_task(conn, title='feature task')
        s1 = create_subtask(conn, parent['display_id'], 'Write code', '')
        s2 = create_subtask(conn, parent['display_id'], 'Write tests', '')

        r1 = complete_subtask(conn, s1['display_id'], 'code written')
        assert not r1.get('parent_completed'), \
            "Parent should not be completed when only first subtask is done"

        r2 = complete_subtask(conn, s2['display_id'], 'tests written')
        assert r2.get('parent_completed'), \
            "Parent should be completed when all subtasks are done"

        row = conn.execute(
            "SELECT status FROM tasks WHERE display_id = ?", (parent['display_id'],)
        ).fetchone()
        assert row['status'] == 'done', f"Parent status should be 'done', got: {row['status']}"
        conn.close()


# ── S6-W04: TestHandoffJson ────────────────────────────────────────────────────

class TestHandoffJson:

    def test_update_task_stores_handoff_json(self, tmp_path):
        """update_task with handoff_json should persist parseable JSON."""
        conn = init_project_db(tmp_path)
        t = create_task(conn, title='handoff task')
        handoff = json.dumps({'files_changed': ['a.py']})

        ok = update_task(conn, t['display_id'], handoff_json=handoff)

        assert ok is True
        row = conn.execute(
            "SELECT handoff_json FROM tasks WHERE display_id = ?", (t['display_id'],)
        ).fetchone()
        parsed = json.loads(row['handoff_json'])
        assert parsed['files_changed'] == ['a.py'], \
            f"Expected ['a.py'], got: {parsed['files_changed']}"
        conn.close()

    def test_handoff_json_null_by_default(self, tmp_path):
        """A freshly created task should have handoff_json as None."""
        conn = init_project_db(tmp_path)
        t = create_task(conn, title='plain task')

        row = conn.execute(
            "SELECT handoff_json FROM tasks WHERE display_id = ?", (t['display_id'],)
        ).fetchone()
        assert row['handoff_json'] is None, \
            f"Expected handoff_json=None, got: {row['handoff_json']!r}"
        conn.close()


# ── S6-W05: TestEscalationLog ─────────────────────────────────────────────────

class TestEscalationLog:

    def test_log_escalation_stores_reason(self, tmp_path):
        """log_escalation should insert a row with the given reason and iteration."""
        conn = init_project_db(tmp_path)
        t = create_task(conn, title='stuck task')

        log_escalation(conn, t['display_id'], 'session_x', 5, 'model stuck', '{}')

        row = conn.execute(
            "SELECT reason, iteration FROM escalation_log WHERE session_id = ?",
            ('session_x',)
        ).fetchone()
        assert row is not None, "escalation_log row should exist"
        assert row['reason'] == 'model stuck', \
            f"Expected reason='model stuck', got: {row['reason']!r}"
        assert row['iteration'] == 5, \
            f"Expected iteration=5, got: {row['iteration']}"
        conn.close()
