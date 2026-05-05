#!/usr/bin/env python3
#====== Automatica/test_automatica_db.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
"""
Comprehensive unit tests for automatica_db.py — covering schema v2 helpers,
config inheritance, manager/project registry, and existing sprint/task functions.

Run:  python -m pytest test_automatica_db.py -v
"""

import json
import sqlite3
import tempfile
import shutil
import pytest
from pathlib import Path

import automatica_db as adb


# =====================================================================
#  Fixtures
# =====================================================================

@pytest.fixture
def gconn():
    """Fresh in-memory global DB."""
    conn = adb.init_global_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def pconn(tmp_path):
    """Fresh project DB in a temp dir."""
    conn = adb.init_project_db(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def proj_dir(tmp_path):
    """Temp directory acting as a project root."""
    return tmp_path


# =====================================================================
#  Global Settings
# =====================================================================

class TestGlobalSettings:
    def test_get_default_returns_default(self, gconn):
        val = adb.get_global_setting(gconn, "nonexistent_key", default="fallback")
        assert val == "fallback"

    def test_set_and_get(self, gconn):
        adb.set_global_setting(gconn, "my_key", "my_value")
        assert adb.get_global_setting(gconn, "my_key") == "my_value"

    def test_overwrite(self, gconn):
        adb.set_global_setting(gconn, "k", "v1")
        adb.set_global_setting(gconn, "k", "v2")
        assert adb.get_global_setting(gconn, "k") == "v2"

    def test_empty_string_value(self, gconn):
        adb.set_global_setting(gconn, "empty_key", "")
        assert adb.get_global_setting(gconn, "empty_key") == ""

    def test_multiple_keys_independent(self, gconn):
        adb.set_global_setting(gconn, "a", "1")
        adb.set_global_setting(gconn, "b", "2")
        assert adb.get_global_setting(gconn, "a") == "1"
        assert adb.get_global_setting(gconn, "b") == "2"


# =====================================================================
#  Known Projects Registry
# =====================================================================

class TestKnownProjects:
    def test_register_project_returns_id(self, gconn, proj_dir):
        pid = adb.register_project(gconn, "TestProj", str(proj_dir))
        assert isinstance(pid, int)
        assert pid > 0

    def test_register_project_with_all_fields(self, gconn, proj_dir):
        pid = adb.register_project(
            gconn, "TestProj", str(proj_dir),
            goal="Build something great",
            empty_backlog_prompt="Scan codebase",
            empty_backlog_agent="developer",
            empty_sprint_prompt="Fill sprint",
            create_sprint_prompt="Create sprint",
        )
        projects = adb.get_known_projects(gconn)
        assert len(projects) == 1
        p = projects[0]
        assert p['name'] == "TestProj"
        assert p['goal'] == "Build something great"
        assert p['empty_backlog_agent'] == "developer"

    def test_register_project_upsert(self, gconn, proj_dir):
        """Registering same directory twice updates the record."""
        adb.register_project(gconn, "OldName", str(proj_dir), goal="Old goal")
        adb.register_project(gconn, "NewName", str(proj_dir), goal="New goal")
        projects = adb.get_known_projects(gconn)
        assert len(projects) == 1
        assert projects[0]['name'] == "NewName"
        assert projects[0]['goal'] == "New goal"

    def test_get_known_projects_empty(self, gconn):
        assert adb.get_known_projects(gconn) == []

    def test_get_known_projects_multiple(self, gconn, tmp_path):
        d1 = tmp_path / "proj1"
        d1.mkdir()
        d2 = tmp_path / "proj2"
        d2.mkdir()
        adb.register_project(gconn, "P1", str(d1))
        adb.register_project(gconn, "P2", str(d2))
        projects = adb.get_known_projects(gconn)
        assert len(projects) == 2
        names = {p['name'] for p in projects}
        assert names == {"P1", "P2"}

    def test_update_project_last_opened(self, gconn, proj_dir):
        adb.register_project(gconn, "P", str(proj_dir))
        adb.update_project_last_opened(gconn, str(proj_dir))
        row = gconn.execute(
            "SELECT last_opened FROM known_projects WHERE directory = ?",
            (str(proj_dir),)
        ).fetchone()
        assert row['last_opened'] is not None

    def test_update_known_project_fields(self, gconn, proj_dir):
        adb.register_project(gconn, "P", str(proj_dir), goal="Old")
        result = adb.update_known_project(gconn, str(proj_dir), goal="New", sort_order=5)
        assert result is True
        p = adb.get_known_projects(gconn)[0]
        assert p['goal'] == "New"
        assert p['sort_order'] == 5

    def test_update_known_project_nonexistent(self, gconn, proj_dir):
        result = adb.update_known_project(gconn, "/nonexistent/path", goal="X")
        # Returns False when no rows changed
        assert result is False

    def test_update_known_project_ignores_unknown_fields(self, gconn, proj_dir):
        adb.register_project(gconn, "P", str(proj_dir))
        result = adb.update_known_project(gconn, str(proj_dir), unknown_col="oops")
        assert result is False  # no allowed fields → no update

    def test_register_project_with_config_id(self, gconn, proj_dir):
        cid = adb.create_config(gconn, "MyConfig")
        pid = adb.register_project(gconn, "P", str(proj_dir), config_id=cid)
        p = adb.get_known_projects(gconn)[0]
        assert p['config_id'] == cid
        assert p['config_name'] == "MyConfig"


# =====================================================================
#  Configs
# =====================================================================

class TestConfigs:
    def test_create_config_returns_id(self, gconn):
        cid = adb.create_config(gconn, "MyCfg")
        assert isinstance(cid, int) and cid > 0

    def test_create_config_with_description(self, gconn):
        cid = adb.create_config(gconn, "CfgA", description="A test config")
        cfgs = adb.get_configs(gconn)
        assert len(cfgs) == 1
        assert cfgs[0]['description'] == "A test config"

    def test_get_configs_empty(self, gconn):
        assert adb.get_configs(gconn) == []

    def test_get_configs_ordered_by_name(self, gconn):
        adb.create_config(gconn, "Zebra")
        adb.create_config(gconn, "Alpha")
        adb.create_config(gconn, "Mango")
        names = [c['name'] for c in adb.get_configs(gconn)]
        assert names == sorted(names)

    def test_create_config_with_parent(self, gconn):
        parent_id = adb.create_config(gconn, "Parent")
        child_id = adb.create_config(gconn, "Child", parent_config_id=parent_id)
        cfgs = {c['name']: c for c in adb.get_configs(gconn)}
        assert cfgs['Child']['parent_config_id'] == parent_id
        assert cfgs['Child']['parent_name'] == "Parent"
        assert cfgs['Parent']['parent_name'] is None


# =====================================================================
#  Config Overrides
# =====================================================================

class TestConfigOverrides:
    def test_set_and_get_override(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.set_config_override(gconn, cid, 'settings', 'interval_minutes', '30')
        overrides = adb.get_config_overrides(gconn, cid)
        assert overrides['settings']['interval_minutes'] == '30'

    def test_override_upsert(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.set_config_override(gconn, cid, 'settings', 'interval_minutes', '30')
        adb.set_config_override(gconn, cid, 'settings', 'interval_minutes', '60')
        overrides = adb.get_config_overrides(gconn, cid)
        assert overrides['settings']['interval_minutes'] == '60'

    def test_override_multiple_scopes(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.set_config_override(gconn, cid, 'settings', 'auto_push', 'true')
        adb.set_config_override(gconn, cid, 'pipeline', 'build_command', 'make')
        adb.set_config_override(gconn, cid, 'agents', 'default_agent', 'Developer')
        overrides = adb.get_config_overrides(gconn, cid)
        assert 'settings' in overrides
        assert 'pipeline' in overrides
        assert 'agents' in overrides

    def test_override_empty_returns_empty(self, gconn):
        cid = adb.create_config(gconn, "C1")
        assert adb.get_config_overrides(gconn, cid) == {}

    def test_delete_config_override(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.set_config_override(gconn, cid, 'settings', 'auto_push', 'true')
        adb.delete_config_override(gconn, cid, 'settings', 'auto_push')
        overrides = adb.get_config_overrides(gconn, cid)
        assert 'auto_push' not in overrides.get('settings', {})

    def test_delete_nonexistent_override_does_not_raise(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.delete_config_override(gconn, cid, 'settings', 'nonexistent_key')

    def test_invalid_scope_raises(self, gconn):
        """config_overrides has a CHECK constraint on scope."""
        cid = adb.create_config(gconn, "C1")
        with pytest.raises(sqlite3.IntegrityError):
            adb.set_config_override(gconn, cid, 'invalid_scope', 'key', 'val')


# =====================================================================
#  Config Inheritance — get_resolved_config
# =====================================================================

class TestResolvedConfig:
    def test_no_config_returns_empty(self, gconn):
        result = adb.get_resolved_config(gconn, None)
        assert result == {}

    def test_flat_config_overrides(self, gconn):
        cid = adb.create_config(gconn, "C1")
        adb.set_config_override(gconn, cid, 'settings', 'auto_push', 'true')
        result = adb.get_resolved_config(gconn, cid)
        assert result['settings']['auto_push'] == 'true'

    def test_parent_child_inheritance(self, gconn):
        """Child overrides parent; parent values not overridden are inherited."""
        parent_id = adb.create_config(gconn, "Parent")
        adb.set_config_override(gconn, parent_id, 'settings', 'interval_minutes', '30')
        adb.set_config_override(gconn, parent_id, 'settings', 'auto_push', 'false')

        child_id = adb.create_config(gconn, "Child", parent_config_id=parent_id)
        adb.set_config_override(gconn, child_id, 'settings', 'interval_minutes', '60')

        result = adb.get_resolved_config(gconn, child_id)
        assert result['settings']['interval_minutes'] == '60'   # child wins
        assert result['settings']['auto_push'] == 'false'        # inherited from parent

    def test_three_level_inheritance(self, gconn):
        """Grandparent → Parent → Child chain."""
        gp_id = adb.create_config(gconn, "GrandParent")
        adb.set_config_override(gconn, gp_id, 'pipeline', 'build_command', 'make all')
        adb.set_config_override(gconn, gp_id, 'pipeline', 'test_command', 'make test')

        p_id = adb.create_config(gconn, "Parent", parent_config_id=gp_id)
        adb.set_config_override(gconn, p_id, 'pipeline', 'test_command', 'pytest')

        c_id = adb.create_config(gconn, "Child", parent_config_id=p_id)
        adb.set_config_override(gconn, c_id, 'pipeline', 'lint_command', 'ruff .')

        result = adb.get_resolved_config(gconn, c_id)
        assert result['pipeline']['build_command'] == 'make all'   # from grandparent
        assert result['pipeline']['test_command'] == 'pytest'       # parent overrides gp
        assert result['pipeline']['lint_command'] == 'ruff .'       # child adds new key

    def test_nonexistent_config_id_returns_empty(self, gconn):
        result = adb.get_resolved_config(gconn, 9999)
        assert result == {}


# =====================================================================
#  validate_config_parent — cycle detection
# =====================================================================

class TestValidateConfigParent:
    def test_self_reference_rejected(self, gconn):
        cid = adb.create_config(gconn, "C1")
        assert adb.validate_config_parent(gconn, cid, cid) is False

    def test_valid_parent(self, gconn):
        parent = adb.create_config(gconn, "Parent")
        child = adb.create_config(gconn, "Child")
        assert adb.validate_config_parent(gconn, child, parent) is True

    def test_direct_cycle_rejected(self, gconn):
        """A → B, trying to set B's parent to A creates A→B→A cycle."""
        a = adb.create_config(gconn, "A")
        b = adb.create_config(gconn, "B", parent_config_id=a)
        # Trying to set A's parent to B would create a cycle
        assert adb.validate_config_parent(gconn, a, b) is False

    def test_indirect_cycle_rejected(self, gconn):
        """A → B → C, trying to set C's parent to A creates A→B→C→A cycle."""
        a = adb.create_config(gconn, "A")
        b = adb.create_config(gconn, "B", parent_config_id=a)
        c = adb.create_config(gconn, "C", parent_config_id=b)
        assert adb.validate_config_parent(gconn, a, c) is False

    def test_unrelated_configs_valid(self, gconn):
        a = adb.create_config(gconn, "A")
        b = adb.create_config(gconn, "B")
        c = adb.create_config(gconn, "C")
        assert adb.validate_config_parent(gconn, c, a) is True
        assert adb.validate_config_parent(gconn, c, b) is True


# =====================================================================
#  Managers
# =====================================================================

class TestManagers:
    def test_create_manager_returns_id(self, gconn):
        mid = adb.create_manager(gconn, "Q2 Release")
        assert isinstance(mid, int) and mid > 0

    def test_create_manager_with_goal_and_config(self, gconn):
        cid = adb.create_config(gconn, "MyCfg")
        mid = adb.create_manager(gconn, "Q3", goal="Ship it", config_id=cid)
        managers = adb.get_managers(gconn)
        assert len(managers) == 1
        m = managers[0]
        assert m['goal'] == "Ship it"
        assert m['config_id'] == cid

    def test_get_managers_empty(self, gconn):
        assert adb.get_managers(gconn) == []

    def test_get_managers_multiple(self, gconn):
        adb.create_manager(gconn, "Mgr1")
        adb.create_manager(gconn, "Mgr2")
        assert len(adb.get_managers(gconn)) == 2

    def test_add_and_get_manager_projects(self, gconn, tmp_path):
        mid = adb.create_manager(gconn, "M1")
        d1 = str(tmp_path / "proj1")
        d2 = str(tmp_path / "proj2")
        adb.add_manager_project(gconn, mid, d1)
        adb.add_manager_project(gconn, mid, d2)
        projects = adb.get_manager_projects(gconn, mid)
        assert set(projects) == {d1, d2}

    def test_add_duplicate_manager_project_idempotent(self, gconn, tmp_path):
        mid = adb.create_manager(gconn, "M1")
        d1 = str(tmp_path / "proj1")
        adb.add_manager_project(gconn, mid, d1)
        adb.add_manager_project(gconn, mid, d1)  # should not raise
        assert len(adb.get_manager_projects(gconn, mid)) == 1

    def test_remove_manager_project(self, gconn, tmp_path):
        mid = adb.create_manager(gconn, "M1")
        d1 = str(tmp_path / "proj1")
        adb.add_manager_project(gconn, mid, d1)
        adb.remove_manager_project(gconn, mid, d1)
        assert adb.get_manager_projects(gconn, mid) == []

    def test_remove_nonexistent_project_does_not_raise(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        adb.remove_manager_project(gconn, mid, "/nonexistent")

    def test_manager_projects_isolated_per_manager(self, gconn, tmp_path):
        m1 = adb.create_manager(gconn, "M1")
        m2 = adb.create_manager(gconn, "M2")
        d = str(tmp_path / "shared")
        adb.add_manager_project(gconn, m1, d)
        assert adb.get_manager_projects(gconn, m1) == [d]
        assert adb.get_manager_projects(gconn, m2) == []


# =====================================================================
#  Management Tasks
# =====================================================================

class TestManagementTasks:
    def test_create_and_get_task(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        tid = adb.create_management_task(gconn, mid, "Deploy v1.0", priority='HIGH')
        tasks = adb.get_management_tasks(gconn, mid)
        assert len(tasks) == 1
        assert tasks[0]['title'] == "Deploy v1.0"
        assert tasks[0]['priority'] == 'HIGH'
        assert tasks[0]['status'] == 'backlog'

    def test_create_task_with_description(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        adb.create_management_task(gconn, mid, "Task A", description="Do something")
        tasks = adb.get_management_tasks(gconn, mid)
        assert tasks[0]['description'] == "Do something"

    def test_complete_task(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        tid = adb.create_management_task(gconn, mid, "Release")
        adb.complete_management_task(gconn, tid)
        tasks = adb.get_management_tasks(gconn, mid, status='done')
        assert len(tasks) == 1
        assert tasks[0]['completed_at'] is not None

    def test_get_tasks_filtered_by_status(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        t1 = adb.create_management_task(gconn, mid, "Active task")
        t2 = adb.create_management_task(gconn, mid, "Another")
        adb.complete_management_task(gconn, t1)
        backlog = adb.get_management_tasks(gconn, mid, status='backlog')
        done = adb.get_management_tasks(gconn, mid, status='done')
        assert len(backlog) == 1
        assert len(done) == 1

    def test_get_tasks_all_statuses(self, gconn):
        mid = adb.create_manager(gconn, "M1")
        t1 = adb.create_management_task(gconn, mid, "T1")
        t2 = adb.create_management_task(gconn, mid, "T2")
        adb.complete_management_task(gconn, t1)
        all_tasks = adb.get_management_tasks(gconn, mid)
        assert len(all_tasks) == 2

    def test_tasks_isolated_per_manager(self, gconn):
        m1 = adb.create_manager(gconn, "M1")
        m2 = adb.create_manager(gconn, "M2")
        adb.create_management_task(gconn, m1, "M1 Task")
        assert len(adb.get_management_tasks(gconn, m2)) == 0

    def test_manager_cascade_deletes_tasks(self, gconn):
        """Deleting a manager should cascade-delete its tasks."""
        mid = adb.create_manager(gconn, "M1")
        adb.create_management_task(gconn, mid, "T1")
        gconn.execute("DELETE FROM managers WHERE id = ?", (mid,))
        gconn.commit()
        rows = gconn.execute(
            "SELECT COUNT(*) FROM management_tasks WHERE manager_id = ?", (mid,)
        ).fetchone()[0]
        assert rows == 0


# =====================================================================
#  Schema Migration — v1 → v2
# =====================================================================

class TestSchemaMigration:
    def test_schema_version_is_2(self, gconn):
        ver = adb.get_schema_version(gconn)
        assert ver >= 2

    def test_known_projects_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='known_projects'"
        ).fetchone()
        assert row is not None

    def test_configs_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='configs'"
        ).fetchone()
        assert row is not None

    def test_config_overrides_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config_overrides'"
        ).fetchone()
        assert row is not None

    def test_managers_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='managers'"
        ).fetchone()
        assert row is not None

    def test_manager_projects_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='manager_projects'"
        ).fetchone()
        assert row is not None

    def test_management_tasks_table_exists(self, gconn):
        row = gconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='management_tasks'"
        ).fetchone()
        assert row is not None

    def test_known_projects_has_sprint_prompt_fields(self, gconn):
        cols = {r[1] for r in gconn.execute("PRAGMA table_info(known_projects)").fetchall()}
        assert 'empty_sprint_prompt' in cols
        assert 'create_sprint_prompt' in cols

    def test_configs_has_parent_config_id(self, gconn):
        cols = {r[1] for r in gconn.execute("PRAGMA table_info(configs)").fetchall()}
        assert 'parent_config_id' in cols

    def test_migrate_global_idempotent(self, gconn):
        """Running migrate_global a second time should not raise."""
        adb.migrate_global(gconn)
        adb.migrate_global(gconn)


# =====================================================================
#  Project DB — Task & Sprint operations
# =====================================================================

class TestProjectTaskOperations:
    def test_create_task(self, pconn):
        result = adb.create_task(pconn, "Implement login", priority="HIGH",
                                  agent_slug="Developer")
        assert isinstance(result, dict)
        assert 'id' in result and result['id'] > 0
        assert 'display_id' in result

    def test_get_backlog(self, pconn):
        adb.create_task(pconn, "Task A", priority="HIGH")
        adb.create_task(pconn, "Task B", priority="LOW")
        backlog = adb.get_backlog(pconn)
        assert len(backlog) == 2

    def test_backlog_excludes_sprint_planned(self, pconn):
        adb.create_task(pconn, "In Sprint", priority="HIGH")
        adb.create_task(pconn, "Backlog Only", priority="MEDIUM")
        # Move first task to sprint_planned
        pconn.execute(
            "UPDATE tasks SET status = 'sprint_planned' WHERE title = 'In Sprint'"
        )
        pconn.commit()
        backlog = adb.get_backlog(pconn)
        titles = [t['title'] for t in backlog]
        assert "Backlog Only" in titles
        assert "In Sprint" not in titles

    def test_create_sprint(self, pconn):
        result = adb.create_sprint(pconn, sprint_number=1, name="Sprint 1",
                                    goal="Ship auth")
        assert isinstance(result, dict)
        assert 'id' in result and result['id'] > 0
        assert result['sprint_number'] == 1

    def test_get_all_sprints(self, pconn):
        adb.create_sprint(pconn, 1, "Sprint 1", "Goal A")
        adb.create_sprint(pconn, 2, "Sprint 2", "Goal B")
        sprints = adb.get_all_sprints(pconn)
        assert len(sprints) == 2

    def test_sprint_task_assignment(self, pconn):
        task = adb.create_task(pconn, "Task X", priority="HIGH")
        did = task['display_id']
        sprint = adb.create_sprint(pconn, 1, "Sprint 1", "Goal")
        sp_num = sprint['sprint_number']
        adb.assign_task_to_sprint(pconn, did, sp_num)
        sprint_tasks = adb.get_sprint_tasks(pconn, sp_num)
        assert len(sprint_tasks) >= 1

    def test_update_sprint(self, pconn):
        adb.create_sprint(pconn, 1, "Sprint 1", "Goal A")
        result = adb.update_sprint(pconn, 1, goal="Updated Goal", status="active")
        assert result is True
        row = pconn.execute(
            "SELECT goal, status FROM sprints WHERE sprint_number = 1"
        ).fetchone()
        assert row['goal'] == "Updated Goal"
        assert row['status'] == "active"

    def test_add_subtask(self, pconn):
        task = adb.create_task(pconn, "Parent Task")
        did = task['display_id']
        result = adb.add_subtask(pconn, did, "Subtask 1")
        assert isinstance(result, dict)
        assert 'id' in result and result['id'] > 0

    def test_check_subtask(self, pconn):
        task = adb.create_task(pconn, "Parent Task")
        did = task['display_id']
        sub = adb.add_subtask(pconn, did, "Subtask 1")
        sub_id = sub['id']
        adb.check_subtask(pconn, sub_id, True)
        row = pconn.execute(
            "SELECT status FROM subtasks WHERE id = ?", (sub_id,)
        ).fetchone()
        assert row['status'] == 'done'

    def test_update_task(self, pconn):
        task = adb.create_task(pconn, "Old Title", priority="LOW")
        did = task['display_id']
        adb.update_task(pconn, did, title="New Title", priority="HIGH")
        row = pconn.execute(
            "SELECT title, priority FROM tasks WHERE display_id = ?", (did,)
        ).fetchone()
        assert row['title'] == "New Title"
        assert row['priority'] == "HIGH"

    def test_log_action(self, pconn):
        adb.log_action(pconn, scope="task", scope_id=1, actor="Developer",
                       action="task_complete", detail={"note": "Done"})
        rows = pconn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert rows == 1


# =====================================================================
#  Full Integration — project + global DB cross-referencing
# =====================================================================

class TestCrossDbIntegration:
    def test_project_with_config_full_flow(self, gconn, proj_dir):
        """Register project, assign config, resolve overrides."""
        # Create config hierarchy
        base_id = adb.create_config(gconn, "Base", description="Base defaults")
        adb.set_config_override(gconn, base_id, 'settings', 'interval_minutes', '30')
        adb.set_config_override(gconn, base_id, 'pipeline', 'test_command', 'pytest')

        project_cfg_id = adb.create_config(gconn, "MyProject Config", parent_config_id=base_id)
        adb.set_config_override(gconn, project_cfg_id, 'settings', 'interval_minutes', '45')
        adb.set_config_override(gconn, project_cfg_id, 'settings', 'auto_push', 'true')

        # Register project with config
        adb.register_project(gconn, "MyProject", str(proj_dir), config_id=project_cfg_id,
                              goal="Build features fast")

        # Resolve: should get project config merged over base
        resolved = adb.get_resolved_config(gconn, project_cfg_id)
        assert resolved['settings']['interval_minutes'] == '45'   # project overrides base
        assert resolved['settings']['auto_push'] == 'true'          # project adds
        assert resolved['pipeline']['test_command'] == 'pytest'     # inherited from base

        # Project is in registry
        projects = adb.get_known_projects(gconn)
        assert len(projects) == 1
        assert projects[0]['config_name'] == "MyProject Config"

    def test_manager_with_sub_projects_full_flow(self, gconn, tmp_path):
        """Create manager, register projects, assign projects to manager."""
        d1 = tmp_path / "p1"
        d1.mkdir()
        d2 = tmp_path / "p2"
        d2.mkdir()

        adb.register_project(gconn, "P1", str(d1))
        adb.register_project(gconn, "P2", str(d2))

        mid = adb.create_manager(gconn, "Q2 Release", goal="Release Q2 features")
        adb.add_manager_project(gconn, mid, str(d1))
        adb.add_manager_project(gconn, mid, str(d2))

        # Create management tasks
        adb.create_management_task(gconn, mid, "Coordinate deploys", priority='HIGH')
        adb.create_management_task(gconn, mid, "Review PRs")

        mgr_projects = adb.get_manager_projects(gconn, mid)
        assert set(mgr_projects) == {str(d1), str(d2)}

        tasks = adb.get_management_tasks(gconn, mid)
        assert len(tasks) == 2
        high_tasks = [t for t in tasks if t['priority'] == 'HIGH']
        assert len(high_tasks) == 1

    def test_global_goal_setting(self, gconn):
        adb.set_global_setting(gconn, 'global_goal', 'Automate everything')
        assert adb.get_global_setting(gconn, 'global_goal') == 'Automate everything'


# =====================================================================
#  Edge Cases & Validation
# =====================================================================

class TestEdgeCases:
    def test_create_task_display_id_format(self, pconn):
        """Display IDs should be non-empty strings assigned at insert."""
        result = adb.create_task(pconn, "Test task")
        assert result['display_id'] is not None
        assert len(result['display_id']) > 0

    def test_known_projects_unique_directory(self, gconn, tmp_path):
        """Two projects cannot share the same directory."""
        d = str(tmp_path / "proj")
        Path(d).mkdir()
        adb.register_project(gconn, "P1", d)
        # Registering same dir updates — does NOT create two rows
        adb.register_project(gconn, "P2", d)
        count = gconn.execute("SELECT COUNT(*) FROM known_projects").fetchone()[0]
        assert count == 1

    def test_config_overrides_cascade_delete(self, gconn):
        """Deleting a config should delete its overrides via CASCADE."""
        cid = adb.create_config(gconn, "TempCfg")
        adb.set_config_override(gconn, cid, 'settings', 'auto_push', 'true')
        gconn.execute("DELETE FROM configs WHERE id = ?", (cid,))
        gconn.commit()
        rows = gconn.execute(
            "SELECT COUNT(*) FROM config_overrides WHERE config_id = ?", (cid,)
        ).fetchone()[0]
        assert rows == 0

    def test_init_global_db_idempotent(self, tmp_path):
        """Calling init_global_db twice on the same path should not fail."""
        db_path = str(tmp_path / "global.db")
        c1 = adb.init_global_db(db_path)
        c1.close()
        c2 = adb.init_global_db(db_path)
        # Should have same tables
        tables = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'known_projects' in tables
        assert 'configs' in tables
        c2.close()

    def test_project_db_has_required_tables(self, pconn):
        tables = {r[0] for r in pconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for required in ['tasks', 'sprints', 'sprint_tasks', 'subtasks', 'action_log']:
            assert required in tables, f"Missing table: {required}"


# =====================================================================
#  Handoff JSON
# =====================================================================

class TestHandoffJson:
    def test_update_task_stores_handoff_json(self, pconn):
        """update_task should store handoff_json and allow retrieval."""
        result = adb.create_task(pconn, "Task with handoff")
        did = result['display_id']
        
        handoff_data = '{"files_changed":["a.py"]}'
        adb.update_task(pconn, did, handoff_json=handoff_data)
        
        row = pconn.execute(
            "SELECT handoff_json FROM tasks WHERE display_id = ?", (did,)
        ).fetchone()
        
        parsed = json.loads(row['handoff_json'])
        assert parsed['files_changed'] == ['a.py']
    
    def test_handoff_json_null_by_default(self, pconn):
        """Newly created tasks should have handoff_json as NULL."""
        result = adb.create_task(pconn, "Task without handoff")
        did = result['display_id']
        
        row = pconn.execute(
            "SELECT handoff_json FROM tasks WHERE display_id = ?", (did,)
        ).fetchone()
        
        assert row['handoff_json'] is None
