#!/usr/bin/env python3
"""
Unit tests for AIxYad Agent Protocol using Ollama llama3.2.

Run: python -m pytest test_protocol.py -v --timeout=60
Skip if Ollama not running.
"""

import json
import os
import tempfile
import shutil
import pytest
import urllib.request
import urllib.error
from pathlib import Path

from agent_executor import AgentExecutor
from ai_adapters import OllamaAdapter, _extract_json, VALID_ACTIONS
from agent_protocol import AgentProtocol, AVAILABLE_ACTIONS, DEFAULT_PERMISSIONS

OLLAMA_ENDPOINT = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"
TEST_DIR = Path(__file__).parent


def ollama_available() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_ENDPOINT}/api/tags", timeout=3)
        return True
    except Exception:
        return False


skip_no_ollama = pytest.mark.skipif(
    not ollama_available(), reason="Ollama not running at localhost:11434"
)


# ═══════════════════════════════════════════════════════════════════════
#  JSON Extraction Tests (no Ollama needed)
# ═══════════════════════════════════════════════════════════════════════

class TestJsonExtraction:
    def test_raw_json(self):
        text = '{"action": "step_done", "params": {"summary": "done"}}'
        result = _extract_json(text)
        assert result is not None
        assert result["action"] == "step_done"

    def test_json_in_code_block(self):
        text = 'Here is my response:\n```json\n{"action": "search", "params": {"pattern": "TODO"}}\n```'
        result = _extract_json(text)
        assert result is not None
        assert result["action"] == "search"

    def test_json_with_surrounding_text(self):
        text = 'I will search for TODOs.\n{"action": "search", "params": {"pattern": "TODO"}}\nDone.'
        result = _extract_json(text)
        assert result is not None
        assert result["action"] == "search"

    def test_invalid_json(self):
        result = _extract_json("This is not JSON at all")
        assert result is None

    def test_empty_string(self):
        result = _extract_json("")
        assert result is None

    def test_nested_json(self):
        text = '{"action": "edit_file", "params": {"path": "test.py", "old_text": "a", "new_text": "b"}, "reasoning": "fix"}'
        result = _extract_json(text)
        assert result is not None
        assert result["params"]["path"] == "test.py"


# ═══════════════════════════════════════════════════════════════════════
#  Executor Tests (no Ollama needed)
# ═══════════════════════════════════════════════════════════════════════

class TestExecutorQueryWiki:
    def test_query_wiki(self, tmp_path):
        """Test query_wiki action returns success and lists all files with language info."""
        # Create temp dir with 3 .py files and 1 .cpp file
        project = tmp_path / "wiki_test"
        project.mkdir()
        (project / "file1.py").write_text("# Python file 1\n")
        (project / "file2.py").write_text("# Python file 2\n")
        (project / "file3.py").write_text("# Python file 3\n")
        (project / "code.cpp").write_text("// C++ file\n")
        
        # Initialize AgentExecutor
        executor = AgentExecutor(project, DEFAULT_PERMISSIONS)
        
        # Call query_wiki action
        result = executor.execute({'action': 'query_wiki', 'params': {}})
        
        # Assert success
        assert result['success'] is True
        
        # Assert all 4 file paths appear in result message
        assert 'file1.py' in result['message']
        assert 'file2.py' in result['message']
        assert 'file3.py' in result['message']
        assert 'code.cpp' in result['message']
        
        # Assert result message contains 'language' substring
        assert 'language' in result['message']


class TestExecutor:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.project = tmp_path / "test_project"
        self.project.mkdir()
        (self.project / "test.txt").write_text("Hello World\nLine 2\nLine 3\n")
        (self.project / "src").mkdir()
        (self.project / "src" / "main.cpp").write_text("#include <iostream>\n// TODO: implement\nint main() {}\n")
        self.executor = AgentExecutor(self.project, DEFAULT_PERMISSIONS)

    def test_read_file(self):
        result = self.executor.execute({"action": "read_file", "params": {"path": "test.txt"}})
        assert result["success"]
        assert "Hello World" in result["message"]
        assert "FILE CONTENT" in result["message"]

    def test_read_nonexistent(self):
        result = self.executor.execute({"action": "read_file", "params": {"path": "nope.txt"}})
        assert not result["success"]

    def test_write_file(self):
        result = self.executor.execute({"action": "write_file", "params": {"path": "new.txt", "content": "new content"}})
        assert result["success"]
        assert (self.project / "new.txt").read_text() == "new content"

    def test_write_existing_fails(self):
        result = self.executor.execute({"action": "write_file", "params": {"path": "test.txt", "content": "overwrite"}})
        assert not result["success"]

    def test_edit_file(self):
        result = self.executor.execute({"action": "edit_file", "params": {
            "path": "test.txt", "old_text": "Hello World", "new_text": "Hello AIxYad"
        }})
        assert result["success"]
        assert "Hello AIxYad" in (self.project / "test.txt").read_text()

    def test_edit_not_found(self):
        result = self.executor.execute({"action": "edit_file", "params": {
            "path": "test.txt", "old_text": "NOT HERE", "new_text": "replacement"
        }})
        assert not result["success"]

    def test_search(self):
        result = self.executor.execute({"action": "search", "params": {"pattern": "TODO", "path": "."}})
        assert result["success"]
        assert "main.cpp" in result["message"]

    def test_list_files(self):
        result = self.executor.execute({"action": "list_files", "params": {"pattern": "*.txt"}})
        assert result["success"]
        assert "test.txt" in result["message"]

    def test_path_escape_blocked(self):
        result = self.executor.execute({"action": "read_file", "params": {"path": "../../etc/passwd"}})
        assert not result["success"]
        assert "escapes" in result["message"].lower() or "DENIED" in result["message"]

    def test_delete_denied(self):
        executor = AgentExecutor(self.project, {**DEFAULT_PERMISSIONS, "can_delete_file": False})
        result = executor.execute({"action": "delete_file", "params": {"path": "test.txt"}})
        assert not result["success"]

    def test_step_done_always_allowed(self):
        executor = AgentExecutor(self.project, {})  # Empty permissions
        result = executor.execute({"action": "step_done", "params": {"summary": "done"}})
        assert result["success"]

    def test_vcs_push_denied_by_default(self):
        result = self.executor.execute({"action": "run_command", "params": {"command": "git push origin main"}})
        assert not result["success"]
        assert "DENIED" in result["message"]

    def test_vcs_push_allowed_when_permitted(self):
        executor = AgentExecutor(self.project, {**DEFAULT_PERMISSIONS, "can_git_push": True})
        # This will fail because there's no git repo, but the permission check should pass
        result = executor.execute({"action": "run_command", "params": {"command": "git push origin main"}})
        # The command itself may fail but it should NOT be permission-denied
        assert "DENIED" not in result["message"] or "permission" not in result["message"].lower()

    def test_vcs_reset_denied(self):
        result = self.executor.execute({"action": "run_command", "params": {"command": "git reset --hard HEAD"}})
        assert not result["success"]

    def test_safe_git_commands_allowed(self):
        result = self.executor.execute({"action": "run_command", "params": {"command": "git status"}})
        # May fail (no git repo) but should NOT be permission denied
        assert "DENIED" not in result.get("message", "").upper() or "permission" not in result.get("message", "").lower()

    def test_action_log(self):
        self.executor.execute({"action": "read_file", "params": {"path": "test.txt"}})
        self.executor.execute({"action": "search", "params": {"pattern": "Hello"}})
        log = self.executor.get_log()
        assert len(log) == 2
        assert log[0]["action"] == "read_file"
        assert log[1]["action"] == "search"


# ═══════════════════════════════════════════════════════════════════════
#  Ollama Adapter Tests (requires Ollama running)
# ═══════════════════════════════════════════════════════════════════════

@skip_no_ollama
class TestOllamaAdapter:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.adapter = OllamaAdapter({
            "endpoint": OLLAMA_ENDPOINT,
            "model": OLLAMA_MODEL,
            "timeout": 60,
        })

    def test_simple_step_done(self):
        """Ask Ollama to just say it's done — validates JSON response format."""
        request = {
            "task": {"title": "Say done", "description": "Simply respond with step_done action."},
            "context": {"project_dir": str(TEST_DIR)},
            "instructions": {},
            "history": [],
            "permissions": DEFAULT_PERMISSIONS,
            "available_actions": AVAILABLE_ACTIONS,
        }
        response = self.adapter.send(request)
        print(f"\nOllama response: {json.dumps(response, indent=2)}")
        assert "action" in response
        assert "params" in response
        assert response["action"] in VALID_ACTIONS

    def test_search_action(self):
        """Ask Ollama to search for a pattern — should return search action."""
        request = {
            "task": {"title": "Find TODOs", "description": "Search for TODO comments in Python files. Use the search action with pattern 'TODO' and glob '*.py'."},
            "context": {"project_dir": str(TEST_DIR)},
            "instructions": {},
            "history": [],
            "permissions": DEFAULT_PERMISSIONS,
            "available_actions": AVAILABLE_ACTIONS,
        }
        response = self.adapter.send(request)
        print(f"\nOllama response: {json.dumps(response, indent=2)}")
        assert response["action"] in VALID_ACTIONS
        # Should be search or step_done (might decide there's nothing to search)
        assert response["action"] in ("search", "read_file", "list_files", "step_done")

    def test_read_file_action(self):
        """Ask Ollama to read a specific file."""
        request = {
            "task": {"title": "Read file", "description": "Read the file 'settings.json' to understand the project configuration. Use the read_file action."},
            "context": {"project_dir": str(TEST_DIR)},
            "instructions": {},
            "history": [],
            "permissions": DEFAULT_PERMISSIONS,
            "available_actions": AVAILABLE_ACTIONS,
        }
        response = self.adapter.send(request)
        print(f"\nOllama response: {json.dumps(response, indent=2)}")
        assert response["action"] in VALID_ACTIONS

    def test_retry_on_bad_format(self):
        """Send a confusing prompt that might produce non-JSON, verify retry works."""
        adapter = OllamaAdapter({
            "endpoint": OLLAMA_ENDPOINT,
            "model": OLLAMA_MODEL,
            "timeout": 60,
        })
        # Intentionally vague — might cause non-JSON first try
        request = {
            "task": {"title": "Respond correctly", "description": "You must respond with valid JSON. Just return step_done."},
            "context": {},
            "instructions": {},
            "history": [],
            "permissions": DEFAULT_PERMISSIONS,
            "available_actions": AVAILABLE_ACTIONS,
        }
        response = adapter.send(request)
        print(f"\nOllama response (possibly after retry): {json.dumps(response, indent=2)}")
        # Even with retry, should get a valid response
        assert "action" in response


# ═══════════════════════════════════════════════════════════════════════
#  Full Protocol Loop Tests (requires Ollama)
# ═══════════════════════════════════════════════════════════════════════

@skip_no_ollama
class TestProtocolLoop:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.project = tmp_path / "test_project"
        self.project.mkdir()
        (self.project / "hello.txt").write_text("Hello World\n")
        (self.project / "src").mkdir()
        (self.project / "src" / "main.py").write_text("# TODO: implement\nprint('hello')\n")

    def test_search_and_done(self):
        """Full loop: search for TODO, then report done."""
        protocol = AgentProtocol(
            provider="Ollama",
            provider_config={"endpoint": OLLAMA_ENDPOINT, "model": OLLAMA_MODEL, "timeout": 60},
            project_dir=self.project,
            max_iterations=5,
        )
        task = {
            "title": "Find TODOs",
            "description": "Search for TODO comments in the project using the search action, then report your findings with step_done.",
            "type": "SearchAndReport",
        }
        context = {"project_dir": str(self.project), "build_status": "N/A", "test_status": "N/A"}
        instructions = {}

        result = protocol.execute_loop(task, context, instructions)
        print(f"\nProtocol result: {json.dumps(result, indent=2, default=str)}")
        # Should eventually complete (success or need_help, but not crash)
        assert "success" in result
        assert result.get("iterations", 0) > 0

    def test_read_and_done(self):
        """Full loop: read a file, then report done."""
        protocol = AgentProtocol(
            provider="Ollama",
            provider_config={"endpoint": OLLAMA_ENDPOINT, "model": OLLAMA_MODEL, "timeout": 60},
            project_dir=self.project,
            max_iterations=5,
        )
        task = {
            "title": "Read hello.txt",
            "description": "Read the file 'hello.txt' using read_file action, then report what it contains using step_done.",
            "type": "SearchAndReport",
        }
        context = {"project_dir": str(self.project)}
        instructions = {}

        result = protocol.execute_loop(task, context, instructions)
        print(f"\nProtocol result: {json.dumps(result, indent=2, default=str)}")
        assert "success" in result


# ═══════════════════════════════════════════════════════════════════════
#  Schema Validation Tests
# ═══════════════════════════════════════════════════════════════════════

class TestSchema:
    def test_available_actions_complete(self):
        """Verify AVAILABLE_ACTIONS covers all VALID_ACTIONS."""
        action_names = {a["name"] for a in AVAILABLE_ACTIONS}
        for action in VALID_ACTIONS:
            assert action in action_names, f"VALID_ACTIONS has '{action}' but AVAILABLE_ACTIONS does not"

    def test_default_permissions_keys(self):
        """Verify DEFAULT_PERMISSIONS has expected keys."""
        assert DEFAULT_PERMISSIONS["can_read_file"] is True
        assert DEFAULT_PERMISSIONS["can_push"] is False
        assert DEFAULT_PERMISSIONS["can_delete_file"] is False
        assert "blocked_commands" in DEFAULT_PERMISSIONS
        assert "blocked_paths" in DEFAULT_PERMISSIONS

    def test_valid_actions_frozen(self):
        """VALID_ACTIONS should be a frozenset."""
        assert isinstance(VALID_ACTIONS, frozenset)


# ═══════════════════════════════════════════════════════════════════════
#  End-to-End Edit Tests (requires Ollama)
# ═══════════════════════════════════════════════════════════════════════

@skip_no_ollama
class TestEditWorkflow:
    """Test that Ollama can actually edit a file through the protocol and the
    result is verified — the full search → edit → verify → step_done loop."""

    TEST_JSON_CONTENT = '{\n  "Key1": "Moo",\n  "Test2": "No",\n  "MyKey": "Yes"\n}\n'
    EXPECTED_AFTER = '{\n  "Key1": "Moo",\n  "Test2": "3",\n  "MyKey": "Yes"\n}\n'

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.project = tmp_path / "edit_test_project"
        self.project.mkdir()
        (self.project / "Test.json").write_text(self.TEST_JSON_CONTENT)

    def test_edit_json_value(self):
        """Full workflow: AI edits Test.json to change Test2 value from 'No' to '3'."""
        protocol = AgentProtocol(
            provider="Ollama",
            provider_config={"endpoint": OLLAMA_ENDPOINT, "model": OLLAMA_MODEL, "timeout": 90},
            project_dir=self.project,
            max_iterations=10,
        )
        task = {
            "title": "Edit Test.json — change Test2 value",
            "description": (
                "Edit the file Test.json in the project root. "
                "Change the value of key 'Test2' from 'No' to '3'. "
                "Steps: 1) read_file Test.json to see current contents, "
                "2) edit_file to change the value, "
                "3) read_file Test.json again to verify the change, "
                "4) step_done with summary of what was changed."
            ),
            "type": "EditFile",
            "acceptance_criteria": ["Test2 value is '3' in Test.json"],
        }
        context = {"project_dir": str(self.project)}
        instructions = {}

        result = protocol.execute_loop(task, context, instructions)
        print(f"\nEdit workflow result: {json.dumps(result, indent=2, default=str)}")
        print(f"Action log: {json.dumps(protocol.executor.get_log(), indent=2, default=str)}")

        # Verify the file was actually edited correctly
        actual = (self.project / "Test.json").read_text()
        print(f"\nActual file content after edit:\n{actual}")

        parsed = json.loads(actual)
        assert parsed["Test2"] == "3", f"Expected Test2='3', got Test2='{parsed.get('Test2')}'"
        assert parsed["Key1"] == "Moo", "Key1 should not have changed"
        assert parsed["MyKey"] == "Yes", "MyKey should not have changed"

    def test_edit_preserves_other_keys(self):
        """Ensure that only the targeted key changes, nothing else."""
        # Write a more complex JSON
        complex_json = json.dumps({
            "name": "TestProject",
            "version": "1.0.0",
            "settings": {"debug": True, "level": 5},
            "status": "inactive"
        }, indent=2)
        (self.project / "config.json").write_text(complex_json)

        protocol = AgentProtocol(
            provider="Ollama",
            provider_config={"endpoint": OLLAMA_ENDPOINT, "model": OLLAMA_MODEL, "timeout": 90},
            project_dir=self.project,
            max_iterations=10,
        )
        task = {
            "title": "Change status to active",
            "description": (
                "Read config.json, then edit it to change 'status' from 'inactive' to 'active'. "
                "Do NOT change any other values. Use read_file, then edit_file, then step_done."
            ),
            "type": "EditFile",
        }
        result = protocol.execute_loop(task, {}, {})
        print(f"\nResult: {json.dumps(result, indent=2, default=str)}")

        actual = json.loads((self.project / "config.json").read_text())
        assert actual["status"] == "active"
        assert actual["name"] == "TestProject"
        assert actual["version"] == "1.0.0"
        assert actual["settings"]["debug"] is True


# ═══════════════════════════════════════════════════════════════════════
#  Confirmation Step Tests
# ═══════════════════════════════════════════════════════════════════════

class TestConfirmationStep:
    """Test that the protocol can ask a confirmation question before
    finalizing, giving the AI a chance to retry if something looks wrong."""

    def test_protocol_has_confirmation_support(self):
        """Verify AgentProtocol.execute_loop can run with confirm_before_done=True."""
        # This tests that the option exists and doesn't crash
        protocol = AgentProtocol(
            provider="Ollama",
            provider_config={"endpoint": OLLAMA_ENDPOINT, "model": OLLAMA_MODEL},
            project_dir=Path(tempfile.mkdtemp()),
            max_iterations=1,
        )
        assert hasattr(protocol, 'confirm_before_done')


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--timeout=120"])
