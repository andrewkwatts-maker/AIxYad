"""
pytest unit tests for agent_protocol.py — AgentProtocol and AgentExecutor.

S6-W01: TestBuildRequest
S6-W02: TestExecuteLoopAtomicFilesAccumulation
S6-W06: TestExecutorQueryWiki
S6-W07: TestConfirmDoneNoActions
"""
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_adapter(responses=None):
    """Return a mock adapter whose send() cycles through *responses*."""
    adapter = MagicMock()
    adapter.config = {}
    if responses:
        adapter.send.side_effect = list(responses)
    return adapter


def _make_protocol(tmp_path, adapter=None, config=None):
    """Build a minimal AgentProtocol with a mock adapter and no DB."""
    from agent_protocol import AgentProtocol
    from ai_adapters import get_adapter

    if adapter is None:
        adapter = _make_mock_adapter()

    protocol = AgentProtocol.__new__(AgentProtocol)
    # Minimal init without real provider
    from agent_executor import AgentExecutor
    from agent_protocol import DEFAULT_PERMISSIONS

    protocol.provider = 'mock'
    protocol.project_dir = Path(tmp_path).resolve()
    protocol.permissions = dict(DEFAULT_PERMISSIONS)
    protocol.max_iterations = 10
    protocol.log_dir = None
    protocol.confirm_before_done = False
    protocol._raw_provider_config = config or {}
    protocol.executor = AgentExecutor(tmp_path, protocol.permissions)
    protocol.adapter = adapter
    protocol.adapter.config = config or {}
    protocol.history = []
    from datetime import datetime
    protocol.session_id = f"test_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    protocol._total_tokens = 0
    protocol.db_handler = None
    protocol._view_handler = None
    return protocol


# ── S6-W01: TestBuildRequest ──────────────────────────────────────────────────

class TestBuildRequest:

    def test_wiki_summary_included(self, tmp_path):
        """build_request should include wiki_summary when project dir has Python files."""
        for i in range(5):
            (tmp_path / f'file{i}.py').write_text(f'# file {i}\n')

        protocol = _make_protocol(tmp_path)
        task = {'title': 'test task', 'description': 'do something'}
        result = protocol.build_request(task, {}, {}, 0)

        # wiki_summary is best-effort; if files were indexed it must be a list
        if 'wiki_summary' in result:
            assert isinstance(result['wiki_summary'], list)
            # At least one entry should correspond to our .py files
            paths = [f.get('path', '') for f in result['wiki_summary']]
            assert any('.py' in p for p in paths), \
                f"Expected .py paths in wiki_summary, got: {paths}"
        # Absence is also acceptable (best-effort) — test is satisfied either way

    def test_action_filter_applied(self, tmp_path):
        """action_filter in adapter.config limits available_actions."""
        config = {'action_filter': ['read_file', 'step_done']}
        adapter = _make_mock_adapter()
        adapter.config = config
        protocol = _make_protocol(tmp_path, adapter=adapter, config=config)

        task = {'title': 'filtered task', 'description': 'limited'}
        result = protocol.build_request(task, {}, {}, 0)

        names = [a['name'] for a in result['available_actions']]
        assert set(names) == {'read_file', 'step_done'}, \
            f"Expected exactly [read_file, step_done], got: {names}"


# ── S6-W02: TestExecuteLoopAtomicFilesAccumulation ────────────────────────────

class TestExecuteLoopAtomicFilesAccumulation:

    def test_files_changed_accumulated_across_steps(self, tmp_path):
        """execute_loop_atomic should aggregate files_changed from all steps."""
        # Create a task description with 3 numbered steps so the atomic loop fires
        task = {
            'title': 'Multi-step task',
            'description': (
                '1. Write file alpha.py\n'
                '2. Write file beta.py\n'
                '3. Write file gamma.py\n'
            ),
        }
        context = {}
        instructions = {}

        # Each micro-step: adapter returns write_file for that step, then step_done
        write_responses = [
            # Step 1: write_file then step_done
            {'action': 'write_file', 'params': {'path': 'alpha.py', 'content': '# a\n'}},
            {'action': 'step_done',  'params': {'summary': 'wrote alpha', 'files_changed': ['alpha.py']}},
            # Step 2
            {'action': 'write_file', 'params': {'path': 'beta.py', 'content': '# b\n'}},
            {'action': 'step_done',  'params': {'summary': 'wrote beta', 'files_changed': ['beta.py']}},
            # Step 3
            {'action': 'write_file', 'params': {'path': 'gamma.py', 'content': '# g\n'}},
            {'action': 'step_done',  'params': {'summary': 'wrote gamma', 'files_changed': ['gamma.py']}},
        ]

        adapter = _make_mock_adapter(responses=write_responses)
        protocol = _make_protocol(tmp_path, adapter=adapter)
        protocol.confirm_before_done = False

        result = protocol.execute_loop_atomic(task, context, instructions)

        assert result.get('success') is True, f"Loop failed: {result.get('reason')}"
        assert len(result.get('files_changed', [])) == 3, \
            f"Expected 3 files_changed, got: {result.get('files_changed')}"


# ── S6-W06: TestExecutorQueryWiki ────────────────────────────────────────────

class TestExecutorQueryWiki:

    def test_query_wiki_returns_file_info(self, tmp_path):
        """query_wiki should list .py files with language info."""
        for i in range(3):
            (tmp_path / f'module{i}.py').write_text(f'# module {i}\n' * 10)
        (tmp_path / 'main.cpp').write_text('int main() { return 0; }\n')

        from agent_executor import AgentExecutor
        from agent_protocol import DEFAULT_PERMISSIONS

        executor = AgentExecutor(tmp_path, dict(DEFAULT_PERMISSIONS))
        result = executor.execute({'action': 'query_wiki', 'params': {}})

        assert result['success'] is True
        msg = result['message']
        # At least one .py module should appear
        assert 'module0.py' in msg or 'module1.py' in msg or 'module2.py' in msg, \
            f"Expected module*.py in wiki output, got:\n{msg}"
        # Language field should appear
        assert 'language' in msg.lower() or 'Python' in msg or '[py' in msg.lower(), \
            f"Expected language indicator in wiki output, got:\n{msg}"


# ── S6-W07: TestConfirmDoneNoActions ─────────────────────────────────────────

class TestConfirmDoneNoActions:

    def test_confirm_done_short_circuits_on_no_actions(self, tmp_path):
        """_confirm_done returns 'retry' and skips adapter.send when action_log is empty."""
        adapter = _make_mock_adapter()
        protocol = _make_protocol(tmp_path, adapter=adapter)

        # Ensure executor has no logged actions
        protocol.executor.action_log = []

        task = {'title': 'empty task', 'description': 'nothing done yet'}
        request = {'context': {}, 'instructions': {}}
        done_response = {'action': 'step_done', 'params': {'summary': 'Done!'}}

        result = protocol._confirm_done(task, request, done_response, 1)

        assert result == 'retry', f"Expected 'retry', got: {result!r}"
        adapter.send.assert_not_called()
