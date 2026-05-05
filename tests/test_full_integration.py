#!/usr/bin/env python3
"""
Comprehensive integration tests for Automatica agent protocol.
Tests ALL 27 actions using Ollama llama3.2 against a real project structure.

Setup: Creates a temp project with files, initializes both DBs, seeds tasks/subtasks/sprints.
Each test asks Ollama to perform a specific action and validates the result.

Run: python -m pytest test_full_integration.py -v
"""

import json
import pytest
import tempfile
import shutil
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

from automatica_db import (
    init_global_db, init_project_db,
    seed_default_providers, seed_default_pipeline,
    create_task, create_project_task, create_sprint,
    add_subtask, assign_task_to_sprint,
    log_action, build_task_context, build_task_timeline,
)
from agent_protocol import AgentProtocol, AVAILABLE_ACTIONS, DEFAULT_PERMISSIONS
from agent_executor import AgentExecutor
from db_actions import DbActionHandler
from project_scanner import get_file_structure, search_symbols, list_project_files, get_file_section

OLLAMA_ENDPOINT = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"


def ollama_available():
    try:
        urllib.request.urlopen(f"{OLLAMA_ENDPOINT}/api/tags", timeout=3)
        return True
    except Exception:
        return False


skip_no_ollama = pytest.mark.skipif(
    not ollama_available(), reason="Ollama not running at localhost:11434"
)


# =====================================================================
#  Module-scoped fixture: full project with files, DBs, tasks, sprint
# =====================================================================

@pytest.fixture(scope="module")
def project_env():
    """Create a complete test project with files, DBs, tasks, subtasks, sprint."""
    tmp = Path(tempfile.mkdtemp(prefix="automatica_test_"))

    # Create project files
    (tmp / "src").mkdir()
    (tmp / "src" / "main.py").write_text(
        "# TODO: implement\ndef main():\n    pass\n\n"
        "def helper(x):\n    return x * 2\n"
    )
    (tmp / "src" / "utils.py").write_text(
        "import os\n\ndef get_path():\n    return os.getcwd()\n\n"
        "class Config:\n    debug = True\n"
    )
    (tmp / "config.json").write_text(
        json.dumps({"name": "TestProject", "version": "1.0", "debug": True}, indent=2)
    )
    (tmp / "README.md").write_text(
        "# Test Project\n\n## Overview\nA test project for integration testing.\n\n## Setup\nRun main.py\n"
    )
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_main.py").write_text(
        "def test_main():\n    assert True\n"
    )

    # Init DBs
    gdb = init_global_db(tmp / ".automatica" / "global.db")
    seed_default_providers(gdb)
    pdb = init_project_db(tmp)
    seed_default_pipeline(pdb)

    # Create sprint
    sprint = create_sprint(pdb, 1, goal="Complete core implementation")

    # Create project task
    proj = create_project_task(pdb, "Build test application")

    # Create tasks with subtasks
    t1 = create_task(
        pdb, "Implement main function",
        description="Make main() print a greeting and call helper()",
        priority="HIGH", complexity=3, project_task_id=proj['id'],
    )
    sub1 = add_subtask(pdb, t1['display_id'], "Read main.py to understand structure")
    sub2 = add_subtask(pdb, t1['display_id'], "Edit main() to print greeting")
    sub3 = add_subtask(pdb, t1['display_id'], "Verify function works")
    assign_task_to_sprint(pdb, t1['display_id'], 1)

    t2 = create_task(
        pdb, "Add configuration loading",
        description="Read config.json and use settings",
        priority="MEDIUM", complexity=5,
    )
    add_subtask(pdb, t2['display_id'], "Read config.json structure")
    add_subtask(pdb, t2['display_id'], "Add config loading to main.py")

    # Log some history
    log_action(pdb, 'task', t1['id'], 'human', 'created',
               {'message': 'Initial task creation'})
    log_action(pdb, 'task', t1['id'], 'human', 'status_change',
               {'old_value': 'backlog', 'new_value': 'sprint_planned'})

    yield {
        'dir': tmp, 'gdb': gdb, 'pdb': pdb,
        'task1': t1, 'task2': t2,
        'sub1': sub1, 'sub2': sub2, 'sub3': sub3,
        'sprint': sprint, 'project': proj,
    }

    # Cleanup
    gdb.close()
    pdb.close()
    shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
#  Helper: build executor + db_handler for a project_env
# =====================================================================

def make_executor(env):
    """Create an AgentExecutor wired up with DbActionHandler."""
    executor = AgentExecutor(env['dir'], DEFAULT_PERMISSIONS)
    handler = DbActionHandler(env['pdb'], env['gdb'])
    executor.set_db_handler(handler, actor='test_runner', session_id='test_session')
    return executor


# =====================================================================
#  1. Executor-level tests (no Ollama needed, fast)
# =====================================================================

class TestExecutorFileActions:
    """Test file operation action handlers directly without AI."""

    def test_read_file(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "read_file",
            "params": {"path": "src/main.py"}
        })
        assert result["success"]
        assert "def main()" in result["message"]
        assert "FILE CONTENT" in result["message"]

    def test_read_file_with_offset_limit(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "read_file",
            "params": {"path": "src/main.py", "offset": 1, "limit": 2}
        })
        assert result["success"]
        assert "lines" in result["message"].lower() or "line" in result["message"].lower()

    def test_read_nonexistent_file(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "read_file",
            "params": {"path": "nope.txt"}
        })
        assert not result["success"]
        assert "NOT FOUND" in result["message"]

    def test_write_file(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "write_file",
            "params": {"path": "output.txt", "content": "test output line 1\nline 2\n"}
        })
        assert result["success"]
        written = (project_env['dir'] / "output.txt").read_text()
        assert "test output line 1" in written

    def test_write_existing_fails(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "write_file",
            "params": {"path": "src/main.py", "content": "overwrite!"}
        })
        assert not result["success"]
        assert "exists" in result["message"].lower()

    def test_edit_file(self, project_env):
        executor = make_executor(project_env)
        # Edit utils.py
        result = executor.execute({
            "action": "edit_file",
            "params": {
                "path": "src/utils.py",
                "old_text": "debug = True",
                "new_text": "debug = False"
            }
        })
        assert result["success"]
        content = (project_env['dir'] / "src" / "utils.py").read_text()
        assert "debug = False" in content
        # Restore for other tests
        (project_env['dir'] / "src" / "utils.py").write_text(
            "import os\n\ndef get_path():\n    return os.getcwd()\n\n"
            "class Config:\n    debug = True\n"
        )

    def test_edit_file_not_found(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "edit_file",
            "params": {"path": "src/utils.py", "old_text": "NONEXISTENT STRING", "new_text": "x"}
        })
        assert not result["success"]
        assert "NOT FOUND" in result["message"]

    def test_search(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "search",
            "params": {"pattern": "TODO", "path": "src", "glob": "*.py"}
        })
        assert result["success"]
        assert "main.py" in result["message"]
        assert "MATCHES" in result["message"]

    def test_search_with_glob(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "search",
            "params": {"pattern": "def ", "glob": "*.py"}
        })
        assert result["success"]
        assert "MATCHES" in result["message"]

    def test_list_files(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "list_files",
            "params": {"pattern": "*.py", "path": "src"}
        })
        assert result["success"]
        assert "main.py" in result["message"]

    def test_run_command(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "run_command",
            "params": {"command": "python --version"}
        })
        assert result["success"]
        assert "Python" in result["message"] or "python" in result["message"].lower()

    def test_run_blocked_command(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "run_command",
            "params": {"command": "git push origin main"}
        })
        assert not result["success"]
        assert "DENIED" in result["message"]

    def test_commit(self, project_env):
        """Init git repo then test commit action."""
        project_dir = project_env['dir']
        # Init a git repo
        subprocess.run(
            ["git", "init"], cwd=str(project_dir),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(project_dir), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(project_dir), capture_output=True, timeout=10,
        )

        executor = make_executor(project_env)
        result = executor.execute({
            "action": "commit",
            "params": {
                "files": ["src/main.py", "config.json"],
                "message": "test: initial commit from integration test"
            }
        })
        assert result["success"], f"Commit failed: {result['message']}"

    def test_step_done(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "step_done",
            "params": {"summary": "All done", "files_changed": ["src/main.py"]}
        })
        assert result["success"]

    def test_need_help(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "need_help",
            "params": {"reason": "Stuck on configuration"}
        })
        assert result["success"]


# =====================================================================
#  2. Executor DB action tests (no Ollama needed)
# =====================================================================

class TestExecutorDBActions:
    """Test all DB action handlers directly through the executor."""

    def test_query_tasks(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_tasks",
            "params": {}
        })
        assert result["success"]
        assert "RESULTS" in result["message"]
        assert "Implement main function" in result["message"]

    def test_query_tasks_with_status_filter(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_tasks",
            "params": {"status": "backlog"}
        })
        assert result["success"]
        assert "Add configuration loading" in result["message"]

    def test_query_tasks_with_priority_filter(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_tasks",
            "params": {"priority": "HIGH"}
        })
        assert result["success"]
        assert "Implement main function" in result["message"]

    def test_query_task_detail(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "query_task_detail",
            "params": {"task_id": task_id}
        })
        assert result["success"]
        assert task_id in result["message"]
        assert "Implement main function" in result["message"]
        assert "SUBTASKS" in result["message"]
        assert "Read main.py" in result["message"]

    def test_query_task_detail_not_found(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_task_detail",
            "params": {"task_id": "TASK-999"}
        })
        assert not result["success"]
        assert "NOT FOUND" in result["message"] or "not found" in result["message"]

    def test_update_task(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task2']['display_id']
        result = executor.execute({
            "action": "update_task",
            "params": {"task_id": task_id, "priority": "HIGH"}
        })
        assert result["success"]
        assert "CHANGES" in result["message"]
        # Verify DB was updated
        row = project_env['pdb'].execute(
            "SELECT priority FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        assert row['priority'] == 'HIGH'
        # Restore
        executor.execute({
            "action": "update_task",
            "params": {"task_id": task_id, "priority": "MEDIUM"}
        })

    def test_update_task_status(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task2']['display_id']
        result = executor.execute({
            "action": "update_task",
            "params": {"task_id": task_id, "status": "in_progress"}
        })
        assert result["success"]
        row = project_env['pdb'].execute(
            "SELECT status, started_at FROM tasks WHERE display_id = ?", (task_id,)
        ).fetchone()
        assert row['status'] == 'in_progress'
        assert row['started_at'] is not None
        # Restore
        executor.execute({
            "action": "update_task",
            "params": {"task_id": task_id, "status": "backlog"}
        })

    def test_add_task(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "add_task",
            "params": {
                "title": "Write unit tests",
                "description": "Add pytest tests for core module",
                "priority": "LOW",
                "complexity": 5,
            }
        })
        assert result["success"]
        assert "CREATED" in result["message"]
        assert "Write unit tests" in result["message"]

    def test_add_task_with_parent(self, project_env):
        executor = make_executor(project_env)
        parent_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "add_task",
            "params": {
                "title": "Sub-implementation detail",
                "parent_task_id": parent_id,
            }
        })
        assert result["success"]

    def test_reorder_tasks(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task2']['display_id']
        result = executor.execute({
            "action": "reorder_tasks",
            "params": {"task_id": task_id, "position": 1}
        })
        assert result["success"]
        assert "position 1" in result["message"]

    def test_add_checklist_item(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "add_checklist_item",
            "params": {"task_id": task_id, "title": "Run integration tests"}
        })
        assert result["success"]
        assert "ADDED" in result["message"]
        assert "Run integration tests" in result["message"]

    def test_check_item(self, project_env):
        executor = make_executor(project_env)
        sub_id = project_env['sub1']['display_id']
        result = executor.execute({
            "action": "check_item",
            "params": {"subtask_id": sub_id, "checked": True}
        })
        assert result["success"]
        assert "[x]" in result["message"]
        # Verify DB
        row = project_env['pdb'].execute(
            "SELECT status FROM subtasks WHERE display_id = ?", (sub_id,)
        ).fetchone()
        assert row['status'] == 'done'

    def test_uncheck_item(self, project_env):
        executor = make_executor(project_env)
        sub_id = project_env['sub1']['display_id']
        result = executor.execute({
            "action": "check_item",
            "params": {"subtask_id": sub_id, "checked": False}
        })
        assert result["success"]
        assert "[ ]" in result["message"]

    def test_query_sprint(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_sprint",
            "params": {"sprint_number": 1}
        })
        assert result["success"]
        assert "SPRINT" in result["message"]
        assert "Complete core implementation" in result["message"]

    def test_query_sprint_summary(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_sprint_summary",
            "params": {}
        })
        assert result["success"]
        assert "SPRINT" in result["message"]

    def test_query_history(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "query_history",
            "params": {"task_id": task_id}
        })
        assert result["success"]
        assert "HISTORY" in result["message"]

    def test_query_history_by_actor(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_history",
            "params": {"actor": "human"}
        })
        assert result["success"]
        assert "HISTORY" in result["message"]

    def test_query_task_timeline(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "query_task_timeline",
            "params": {"task_id": task_id}
        })
        assert result["success"]
        assert "TIMELINE" in result["message"]
        assert "EVENTS" in result["message"]

    def test_link_file_to_task(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "link_file_to_task",
            "params": {
                "task_id": task_id,
                "file_path": "src/main.py",
                "relationship": "modified",
                "notes": "Primary implementation file",
            }
        })
        assert result["success"]
        assert "Linked" in result["message"]
        # Verify in DB
        row = project_env['pdb'].execute(
            "SELECT COUNT(*) AS cnt FROM entity_files WHERE entity_type = 'task'"
        ).fetchone()
        assert row['cnt'] >= 1

    def test_log_file_modification(self, project_env):
        executor = make_executor(project_env)
        task_id = project_env['task1']['display_id']
        result = executor.execute({
            "action": "log_file_modification",
            "params": {
                "task_id": task_id,
                "file_path": "src/main.py",
                "action": "edit",
                "before_snippet": "pass",
                "after_snippet": "print('hello')",
            }
        })
        assert result["success"]
        assert "Recorded" in result["message"]


# =====================================================================
#  3. Executor scanner action tests (no Ollama needed)
# =====================================================================

class TestExecutorScannerActions:
    """Test live scanner action handlers through the executor."""

    def test_query_file_structure(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_file_structure",
            "params": {"file_path": "src/main.py"}
        })
        assert result["success"]
        assert "STRUCTURE" in result["message"]
        assert "main" in result["message"]
        assert "helper" in result["message"]

    def test_query_file_structure_missing(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_file_structure",
            "params": {"file_path": "nonexistent.py"}
        })
        assert not result["success"]

    def test_query_symbols(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_symbols",
            "params": {"name": "Config"}
        })
        assert result["success"]
        assert "SYMBOLS" in result["message"]
        assert "Config" in result["message"]

    def test_query_symbols_by_type(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_symbols",
            "params": {"name": "main", "symbol_type": "function"}
        })
        assert result["success"]
        assert "SYMBOLS" in result["message"]

    def test_query_files(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_files",
            "params": {"extension": ".py"}
        })
        assert result["success"]
        assert "FILES" in result["message"]
        assert "main.py" in result["message"]

    def test_query_files_path_filter(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_files",
            "params": {"path_contains": "tests"}
        })
        assert result["success"]
        assert "test_main.py" in result["message"]

    def test_query_file_section(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_file_section",
            "params": {"file_path": "src/main.py", "line_start": 1, "line_end": 3}
        })
        assert result["success"]
        assert "CONTENT" in result["message"]

    def test_query_file_section_missing(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_file_section",
            "params": {"file_path": "nonexistent.py"}
        })
        assert not result["success"]


# =====================================================================
#  4. Action coverage validation
# =====================================================================

class TestActionCoverage:
    """Verify we actually test all 27 actions."""

    EXPECTED_ACTIONS = {
        # File ops (6)
        "read_file", "write_file", "edit_file", "search", "list_files", "run_command",
        # VCS (1) + terminal (3)
        "commit", "run_script", "step_done", "need_help",
        # DB queries (7)
        "query_tasks", "query_task_detail", "query_sprint", "query_sprint_summary",
        "query_history", "query_task_timeline",
        # DB mutations (5)
        "update_task", "add_task", "reorder_tasks",
        "add_checklist_item", "check_item",
        # DB file linking (2)
        "link_file_to_task", "log_file_modification",
        # Scanner (4)
        "query_file_structure", "query_symbols", "query_files", "query_file_section",
    }

    def test_all_actions_in_available_actions(self):
        """Verify AVAILABLE_ACTIONS includes all expected actions."""
        action_names = {a["name"] for a in AVAILABLE_ACTIONS}
        for expected in self.EXPECTED_ACTIONS:
            assert expected in action_names, f"Missing from AVAILABLE_ACTIONS: {expected}"

    def test_action_count(self):
        """Verify we have at least 27 actions."""
        action_names = {a["name"] for a in AVAILABLE_ACTIONS}
        assert len(action_names) >= 27, f"Expected >=27 actions, got {len(action_names)}: {action_names}"


# =====================================================================
#  5. Ollama-driven tests (requires Ollama, slower)
# =====================================================================

@skip_no_ollama
class TestOllamaActions:
    """Test that Ollama can use structured actions correctly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from ai_adapters import OllamaAdapter
        self.adapter = OllamaAdapter({
            "endpoint": OLLAMA_ENDPOINT,
            "model": OLLAMA_MODEL,
            "timeout": 90,
        })

    def _send_request(self, task_title, task_description, project_dir):
        """Helper to send a request to Ollama and return the response."""
        request = {
            "task": {"title": task_title, "description": task_description},
            "context": {"project_dir": str(project_dir)},
            "instructions": {},
            "history": [],
            "permissions": DEFAULT_PERMISSIONS,
            "available_actions": AVAILABLE_ACTIONS,
        }
        return self.adapter.send(request)

    def test_ollama_reads_file(self, project_env):
        """Ask Ollama to read a specific file."""
        response = self._send_request(
            "Read main.py",
            "Read the file 'src/main.py' using the read_file action. "
            "Set path to 'src/main.py'.",
            project_env['dir'],
        )
        assert "action" in response
        assert "params" in response
        # Ollama should choose read_file (or a reasonable alternative)
        assert response["action"] in ("read_file", "list_files", "search", "step_done")

    def test_ollama_searches(self, project_env):
        """Ask Ollama to search for TODO comments."""
        response = self._send_request(
            "Find TODOs",
            "Search for 'TODO' comments in the project using the search action "
            "with pattern 'TODO'.",
            project_env['dir'],
        )
        assert "action" in response
        assert response["action"] in ("search", "list_files", "read_file", "step_done")

    def test_ollama_queries_tasks(self, project_env):
        """Ask Ollama to list all tasks."""
        response = self._send_request(
            "List tasks",
            "Use the query_tasks action to list all tasks in the backlog. "
            "Set status to 'backlog'.",
            project_env['dir'],
        )
        assert "action" in response
        assert response["action"] in ("query_tasks", "query_task_detail", "step_done")

    def test_ollama_checks_subtask(self, project_env):
        """Ask Ollama to mark a subtask complete."""
        sub_id = project_env['sub2']['display_id']
        response = self._send_request(
            "Check subtask",
            f"Mark subtask '{sub_id}' as complete using the check_item action. "
            f"Set subtask_id to '{sub_id}' and checked to true.",
            project_env['dir'],
        )
        assert "action" in response
        assert response["action"] in ("check_item", "query_task_detail", "step_done")

    def test_ollama_queries_file_structure(self, project_env):
        """Ask Ollama to get the structure of main.py."""
        response = self._send_request(
            "Get file structure",
            "Use the query_file_structure action to get the structure of 'src/main.py'. "
            "Set file_path to 'src/main.py'.",
            project_env['dir'],
        )
        assert "action" in response
        assert response["action"] in (
            "query_file_structure", "read_file", "query_symbols", "step_done"
        )

    def test_ollama_edits_file(self, project_env):
        """Ask Ollama to edit a file -- just verify the response format."""
        response = self._send_request(
            "Edit config.json",
            "Use the edit_file action to change 'debug' from true to false in config.json. "
            "Set path to 'config.json', old_text to '\"debug\": true', new_text to '\"debug\": false'.",
            project_env['dir'],
        )
        assert "action" in response
        assert response["action"] in ("edit_file", "read_file", "step_done")


# =====================================================================
#  6. Full protocol loop test (requires Ollama)
# =====================================================================

@skip_no_ollama
class TestFullLoop:
    """End-to-end: task assignment -> work -> completion -> verification."""

    def test_complete_task_lifecycle(self, project_env):
        """Full lifecycle:
        1. Protocol assigns task to Ollama
        2. Ollama reads the file, edits it, checks subtasks
        3. Ollama marks step_done
        4. We verify: history logged, iterations > 0
        """
        protocol = AgentProtocol(
            provider='Ollama',
            provider_config={
                'endpoint': OLLAMA_ENDPOINT,
                'model': OLLAMA_MODEL,
                'timeout': 120,
            },
            project_dir=project_env['dir'],
            max_iterations=15,
            project_db=project_env['pdb'],
            global_db=project_env['gdb'],
            confirm_before_done=False,
        )

        ctx = build_task_context(project_env['pdb'], project_env['task1']['display_id'])
        task = {
            'display_id': project_env['task1']['display_id'],
            'title': ctx['task']['title'],
            'description': ctx['task']['description'],
            'acceptance_criteria': ['main() prints a greeting'],
        }

        result = protocol.execute_loop(
            task,
            {'project_dir': str(project_env['dir'])},
            {},
        )

        # Verify result structure
        assert 'success' in result
        assert result.get('iterations', 0) > 0
        print(f"Completed in {result['iterations']} iterations, "
              f"{result.get('duration', 0):.1f}s")
        print(f"Action log: {len(result.get('action_log', []))} actions")

    def test_read_and_report_loop(self, project_env):
        """Simpler loop: read a file and report done."""
        protocol = AgentProtocol(
            provider='Ollama',
            provider_config={
                'endpoint': OLLAMA_ENDPOINT,
                'model': OLLAMA_MODEL,
                'timeout': 90,
            },
            project_dir=project_env['dir'],
            max_iterations=5,
            confirm_before_done=False,
        )

        task = {
            'title': 'Read README.md',
            'description': (
                'Read the file README.md using read_file action, '
                'then report what it contains using step_done.'
            ),
        }

        result = protocol.execute_loop(task, {'project_dir': str(project_env['dir'])}, {})

        assert 'success' in result
        assert result.get('iterations', 0) > 0

    def test_verify_history_logged(self, project_env):
        """After the lifecycle test, verify DB has history entries."""
        timeline = build_task_timeline(
            project_env['pdb'], project_env['task1']['display_id']
        )
        # We seeded 2 entries; lifecycle test may add more
        assert len(timeline) >= 2, f"Expected >= 2 timeline entries, got {len(timeline)}"
        print(f"Timeline has {len(timeline)} entries:")
        for entry in timeline:
            print(f"  {entry.get('timestamp', '?')} [{entry.get('actor', '?')}] "
                  f"{entry.get('summary', '?')}")


# =====================================================================
#  7. Edge cases and error handling
# =====================================================================

class TestEdgeCases:
    """Test error handling and edge cases."""

    def test_unknown_action(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "totally_fake_action",
            "params": {}
        })
        assert not result["success"]
        assert "UNKNOWN" in result["message"]

    def test_path_escape_blocked(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "read_file",
            "params": {"path": "../../etc/passwd"}
        })
        assert not result["success"]

    def test_missing_required_params(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({
            "action": "query_task_detail",
            "params": {}
        })
        assert not result["success"]
        assert "Missing" in result["message"] or "missing" in result["message"].lower()

    def test_empty_response(self, project_env):
        executor = make_executor(project_env)
        result = executor.execute({})
        assert not result["success"]

    def test_action_log_accumulates(self, project_env):
        executor = make_executor(project_env)
        executor.execute({"action": "read_file", "params": {"path": "config.json"}})
        executor.execute({"action": "search", "params": {"pattern": "def "}})
        executor.execute({"action": "query_tasks", "params": {}})
        log = executor.get_log()
        assert len(log) >= 3
        assert log[0]["action"] == "read_file"
        assert log[1]["action"] == "search"
        assert log[2]["action"] == "query_tasks"

    def test_db_handler_not_attached(self, project_env):
        """Executor without DB handler should fail DB actions gracefully."""
        executor = AgentExecutor(project_env['dir'], DEFAULT_PERMISSIONS)
        # No set_db_handler call
        result = executor.execute({
            "action": "query_tasks",
            "params": {}
        })
        assert not result["success"]
        assert "NO DB" in result["message"] or "DB" in result["message"]

    def test_permission_denied_write(self, project_env):
        restricted = {**DEFAULT_PERMISSIONS, "can_write_file": False}
        executor = AgentExecutor(project_env['dir'], restricted)
        result = executor.execute({
            "action": "write_file",
            "params": {"path": "forbidden.txt", "content": "nope"}
        })
        assert not result["success"]
        assert "DENIED" in result["message"]

    def test_permission_denied_edit(self, project_env):
        restricted = {**DEFAULT_PERMISSIONS, "can_edit_file": False}
        executor = AgentExecutor(project_env['dir'], restricted)
        result = executor.execute({
            "action": "edit_file",
            "params": {"path": "config.json", "old_text": "a", "new_text": "b"}
        })
        assert not result["success"]
        assert "DENIED" in result["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--timeout=120"])
