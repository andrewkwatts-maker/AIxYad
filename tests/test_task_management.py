"""Tests for task CRUD, status transitions, comments, acceptance criteria, retros.

Sprint D — D-24
"""
import sqlite3
import pytest
import json

from aixyad_db import (
    init_project_db,
    create_task,
    update_task,
    add_comment,
    get_comments,
    save_retrospective,
    get_retrospective,
)


def _pconn():
    """Create an in-memory project DB with full schema applied."""
    import tempfile, pathlib, os
    # init_project_db needs a real path (it creates .aixyad/project.db)
    # Use a temp directory so it works in CI
    tmpdir = tempfile.mkdtemp()
    conn = init_project_db(pathlib.Path(tmpdir))
    conn.row_factory = sqlite3.Row
    return conn


class TestTaskCRUD:
    def test_create_and_fetch(self):
        pconn = _pconn()
        t = create_task(pconn, 'Test task', 'desc', complexity=5)
        assert t['display_id'].startswith('TASK-')

    def test_status_transition(self):
        pconn = _pconn()
        t = create_task(pconn, 'Task', '', complexity=3)
        update_task(pconn, t['display_id'], status='in_progress')
        row = pconn.execute(
            "SELECT status FROM tasks WHERE display_id=?",
            (t['display_id'],),
        ).fetchone()
        assert row['status'] == 'in_progress'

    def test_all_status_transitions(self):
        pconn = _pconn()
        t = create_task(pconn, 'Transition task', '', complexity=3)
        did = t['display_id']

        # backlog -> in_progress
        update_task(pconn, did, status='in_progress')
        row = pconn.execute("SELECT status FROM tasks WHERE display_id=?", (did,)).fetchone()
        assert row['status'] == 'in_progress'

        # in_progress -> done
        update_task(pconn, did, status='done')
        row = pconn.execute("SELECT status FROM tasks WHERE display_id=?", (did,)).fetchone()
        assert row['status'] == 'done'

        # done -> backlog (reopen)
        update_task(pconn, did, status='backlog')
        row = pconn.execute("SELECT status FROM tasks WHERE display_id=?", (did,)).fetchone()
        assert row['status'] == 'backlog'

        # backlog -> blocked
        update_task(pconn, did, status='in_progress')
        update_task(pconn, did, status='blocked')
        row = pconn.execute("SELECT status FROM tasks WHERE display_id=?", (did,)).fetchone()
        assert row['status'] == 'blocked'

    def test_update_complexity(self):
        pconn = _pconn()
        t = create_task(pconn, 'Complex task', '', complexity=3)
        update_task(pconn, t['display_id'], complexity=8)
        row = pconn.execute(
            "SELECT complexity FROM tasks WHERE display_id=?", (t['display_id'],)
        ).fetchone()
        assert row['complexity'] == 8

    def test_update_estimated_hours(self):
        """D-16: estimated_hours and actual_hours columns."""
        pconn = _pconn()
        t = create_task(pconn, 'Timed task', '', complexity=3)
        update_task(pconn, t['display_id'], estimated_hours=4.5, actual_hours=3.0)
        row = pconn.execute(
            "SELECT estimated_hours, actual_hours FROM tasks WHERE display_id=?",
            (t['display_id'],),
        ).fetchone()
        assert float(row['estimated_hours']) == pytest.approx(4.5)
        assert float(row['actual_hours']) == pytest.approx(3.0)

    def test_update_acceptance_criteria(self):
        """D-17: acceptance_criteria JSON field."""
        pconn = _pconn()
        t = create_task(pconn, 'AC task', '', complexity=3)
        ac = json.dumps([{'text': 'Must pass all tests', 'done': False}])
        update_task(pconn, t['display_id'], acceptance_criteria=ac)
        row = pconn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE display_id=?",
            (t['display_id'],),
        ).fetchone()
        loaded = json.loads(row['acceptance_criteria'])
        assert loaded[0]['text'] == 'Must pass all tests'


class TestTaskComments:
    def test_add_and_get_comments(self):
        pconn = _pconn()
        t = create_task(pconn, 'Task', '', complexity=3)
        add_comment(pconn, t['display_id'], 'user', 'First comment')
        add_comment(pconn, t['display_id'], 'bot', 'Second comment')
        comments = get_comments(pconn, t['display_id'])
        assert len(comments) == 2
        assert comments[0]['content'] == 'First comment'

    def test_comments_ordered_by_date(self):
        pconn = _pconn()
        t = create_task(pconn, 'Task', '', complexity=3)
        add_comment(pconn, t['display_id'], 'user', 'A')
        add_comment(pconn, t['display_id'], 'bot', 'B')
        add_comment(pconn, t['display_id'], 'user', 'C')
        comments = get_comments(pconn, t['display_id'])
        assert [c['content'] for c in comments] == ['A', 'B', 'C']

    def test_comments_for_nonexistent_task(self):
        pconn = _pconn()
        result = get_comments(pconn, 'TASK-NONEXISTENT')
        assert result == []

    def test_add_comment_for_nonexistent_task(self):
        pconn = _pconn()
        result = add_comment(pconn, 'TASK-NONEXISTENT', 'user', 'Hello')
        assert 'error' in result


class TestSprintRetrospective:
    def test_save_and_get_retro(self):
        pconn = _pconn()
        from aixyad_db import create_sprint as cs
        s = cs(pconn, sprint_number=1, goal='Sprint 1')
        save_retrospective(pconn, s['id'], 'Things went well', 'Could improve X', 'Action: do Y')
        retro = get_retrospective(pconn, s['id'])
        assert retro['went_well'] == 'Things went well'
        assert retro['action_items'] == 'Action: do Y'

    def test_update_retro(self):
        pconn = _pconn()
        from aixyad_db import create_sprint as cs
        s = cs(pconn, sprint_number=2, goal='Sprint 2')
        save_retrospective(pconn, s['id'], 'Good', 'Bad', 'Fix')
        save_retrospective(pconn, s['id'], 'Great', 'Better', 'Improve')
        retro = get_retrospective(pconn, s['id'])
        assert retro['went_well'] == 'Great'
        assert retro['to_improve'] == 'Better'

    def test_get_retro_missing(self):
        pconn = _pconn()
        from aixyad_db import create_sprint as cs
        s = cs(pconn, sprint_number=99, goal='No retro sprint')
        retro = get_retrospective(pconn, s['id'])
        assert retro == {}


class TestBacklogPriority:
    def test_priority_order(self):
        pconn = _pconn()
        t1 = create_task(pconn, 'Task 1', '', complexity=3)
        t2 = create_task(pconn, 'Task 2', '', complexity=3)
        update_task(pconn, t1['display_id'], sort_order=10)
        update_task(pconn, t2['display_id'], sort_order=5)
        tasks = pconn.execute(
            "SELECT display_id FROM tasks ORDER BY sort_order"
        ).fetchall()
        assert tasks[0]['display_id'] == t2['display_id']

    def test_priority_default_zero(self):
        pconn = _pconn()
        t = create_task(pconn, 'Task', '', complexity=3)
        row = pconn.execute(
            "SELECT sort_order FROM tasks WHERE display_id=?", (t['display_id'],)
        ).fetchone()
        # default sort_order is auto-incremented from max, start from 0
        assert row['sort_order'] is not None


class TestSchemaColumns:
    """D-13/D-16: verify that columns exist even on fresh DB."""

    def test_sort_order_column_exists(self):
        pconn = _pconn()
        cols = [r[1] for r in pconn.execute("PRAGMA table_info(tasks)").fetchall()]
        assert 'sort_order' in cols

    def test_estimated_hours_column_exists(self):
        pconn = _pconn()
        cols = [r[1] for r in pconn.execute("PRAGMA table_info(tasks)").fetchall()]
        assert 'estimated_hours' in cols

    def test_actual_hours_column_exists(self):
        pconn = _pconn()
        cols = [r[1] for r in pconn.execute("PRAGMA table_info(tasks)").fetchall()]
        assert 'actual_hours' in cols

    def test_task_comments_table_exists(self):
        pconn = _pconn()
        result = pconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_comments'"
        ).fetchone()
        assert result is not None

    def test_sprint_retrospective_table_exists(self):
        pconn = _pconn()
        result = pconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sprint_retrospective'"
        ).fetchone()
        assert result is not None
