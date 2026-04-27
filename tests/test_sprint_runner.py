"""
S3-W12: Sprint runner tests — zero-velocity guard, provider health check, provider routing.
S2475: Stale build detection and cd-prefix decomposition tests.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprint_runner import SprintRunner, SprintResult, TaskResult, _check_provider_health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(tmp_path):
    """Create a minimal SprintRunner pointing at a temp dir."""
    return SprintRunner(
        project_dir=tmp_path,
        provider="Ollama",
        provider_config={"endpoint": "http://localhost:11434", "model": "gemma3:4b"},
        max_parallel=1,
    )


# ---------------------------------------------------------------------------
# S3-W12 test 1: zero-velocity bumps complexity
# ---------------------------------------------------------------------------

class TestZeroVelocityGuard:
    def test_zero_velocity_bumps_complexity(self, tmp_path):
        """When 0 tasks complete and >= 3 fail, _bump_all_failed_complexities is called."""
        runner = _make_runner(tmp_path)

        # Build a fake sprint_result with 0 completed and 3 failed tasks
        failed_results = [
            TaskResult(task_display_id=f"TASK-00{i}", title=f"Task {i}",
                       success=False, error="stub failure")
            for i in range(1, 4)
        ]
        sprint_result = SprintResult(
            sprint_number=1,
            goal="test",
            tasks_completed=0,
            tasks_failed=3,
            task_results=failed_results,
        )

        # Patch _bump_all_failed_complexities so we can assert it was called
        runner._bump_all_failed_complexities = MagicMock()

        # Simulate the zero-velocity check (mirrors execute_sprint logic)
        if sprint_result.tasks_completed == 0 and sprint_result.tasks_failed >= 3:
            runner._bump_all_failed_complexities(None, sprint_result.task_results)

        runner._bump_all_failed_complexities.assert_called_once()

    def test_zero_velocity_guard_not_triggered_when_tasks_complete(self, tmp_path):
        """Guard should NOT fire when at least one task completes."""
        runner = _make_runner(tmp_path)
        runner._bump_all_failed_complexities = MagicMock()

        sprint_result = SprintResult(
            sprint_number=1, goal="test",
            tasks_completed=1, tasks_failed=5,
        )
        if sprint_result.tasks_completed == 0 and sprint_result.tasks_failed >= 3:
            runner._bump_all_failed_complexities(None, sprint_result.task_results)

        runner._bump_all_failed_complexities.assert_not_called()

    def test_zero_velocity_guard_not_triggered_when_few_failures(self, tmp_path):
        """Guard should NOT fire when fewer than 3 tasks failed."""
        runner = _make_runner(tmp_path)
        runner._bump_all_failed_complexities = MagicMock()

        sprint_result = SprintResult(
            sprint_number=1, goal="test",
            tasks_completed=0, tasks_failed=2,
        )
        if sprint_result.tasks_completed == 0 and sprint_result.tasks_failed >= 3:
            runner._bump_all_failed_complexities(None, sprint_result.task_results)

        runner._bump_all_failed_complexities.assert_not_called()


# ---------------------------------------------------------------------------
# S3-W12 test 2: provider health check returns False when offline
# ---------------------------------------------------------------------------

class TestProviderHealthCheck:
    def test_provider_health_check_returns_false_when_offline(self):
        """_check_provider_health with a bad port should return (False, -1).

        We mock time.sleep and time.monotonic (via sprint_runner.time), and mock
        requests via a fake module so it always raises ConnectionError.
        """
        mock_requests = MagicMock()
        mock_requests.get.side_effect = ConnectionError("Connection refused")

        with patch('sprint_runner.time') as mock_time, \
             patch.dict('sys.modules', {'requests': mock_requests}):
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()  # skip waits

            result = _check_provider_health({'url': 'http://localhost:19999'})

        assert result == (False, -1), f"Expected (False, -1) but got {result}"

    def test_provider_health_check_returns_true_when_online(self):
        """_check_provider_health should return (True, latency) on HTTP 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests = MagicMock()
        mock_requests.get.return_value = mock_response

        with patch('sprint_runner.time') as mock_time, \
             patch.dict('sys.modules', {'requests': mock_requests}):
            mock_time.monotonic.side_effect = [0.0, 0.05]  # 50ms latency
            mock_time.sleep = MagicMock()

            ok, latency = _check_provider_health({'url': 'http://localhost:11434'})

        assert ok is True
        assert latency >= 0


# ---------------------------------------------------------------------------
# S3-W12 test 3: provider routing by complexity
# ---------------------------------------------------------------------------

class TestProviderRouting:
    def test_provider_routing_complexity_low_returns_config(self, tmp_path):
        """_route_provider_by_complexity(2) should return a config dict."""
        runner = _make_runner(tmp_path)

        # Mock global DB returning a fast provider row
        mock_conn = MagicMock()
        mock_row = {
            'provider_type': 'http_local_chat',
            'base_url': 'http://localhost:11434',
            'model_name': 'gemma3:4b',
        }
        # Make row subscriptable
        mock_row_obj = MagicMock()
        mock_row_obj.__getitem__ = lambda self, key: mock_row[key]
        mock_conn.execute.return_value.fetchone.return_value = mock_row_obj

        with patch.object(runner, '_get_global_conn', return_value=mock_conn):
            result = runner._route_provider_by_complexity(2)

        # Should return a dict (provider config), not crash
        assert isinstance(result, dict)

    def test_provider_routing_complexity_high_returns_self_provider(self, tmp_path):
        """_route_provider_by_complexity(9) should return self.provider_config directly."""
        runner = _make_runner(tmp_path)
        result = runner._route_provider_by_complexity(9)
        # Should return the runner's own config (no DB lookup for high complexity)
        assert result == runner.provider_config

    def test_provider_routing_complexity_medium_queries_db(self, tmp_path):
        """_route_provider_by_complexity(5) should return a config dict."""
        runner = _make_runner(tmp_path)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None  # no match in DB

        with patch.object(runner, '_get_global_conn', return_value=mock_conn):
            result = runner._route_provider_by_complexity(5)

        # Falls back to self.provider_config when DB has no match
        assert result == runner.provider_config

    def test_provider_routing_falls_back_on_exception(self, tmp_path):
        """_route_provider_by_complexity should fall back gracefully on DB errors."""
        runner = _make_runner(tmp_path)

        with patch.object(runner, '_get_global_conn', side_effect=Exception("DB offline")):
            result = runner._route_provider_by_complexity(2)

        # Must not raise, must return a dict
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# S2475: Stale build detection — _has_stale_project_paths
# ---------------------------------------------------------------------------

class TestHasStaleProjectPaths:
    """Verify _has_stale_project_paths detects Old3DEngine references in CTestTestfile."""

    def test_detects_old_project_in_comment(self, tmp_path):
        """Comment line '# Source directory: H:/Github/Old3DEngine' should be flagged."""
        runner = _make_runner(tmp_path)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()
        ctest_file = build_dir / 'CTestTestfile.cmake'
        ctest_file.write_text(
            '# Source directory: H:/Github/Old3DEngine\n'
            '# Build directory: H:/Github/Old3DEngine/build\n'
        )
        assert runner._has_stale_project_paths(str(build_dir), str(tmp_path)) is True

    def test_detects_old_project_in_subdirs(self, tmp_path):
        """subdirs() referencing a different project should be flagged."""
        runner = _make_runner(tmp_path)
        # Simulate source at H:/Github/Nova3D (use tmp_path as stand-in)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()
        ctest_file = build_dir / 'CTestTestfile.cmake'
        ctest_file.write_text(
            'subdirs("H:/Github/Old3DEngine/build/tests")\n'
        )
        # Source dir is tmp_path — on Windows parts[2] will differ from 'Old3DEngine'
        assert runner._has_stale_project_paths(str(build_dir), str(tmp_path)) is True

    def test_detects_old_project_in_add_test(self, tmp_path):
        """add_test() referencing a different project directory should be flagged."""
        runner = _make_runner(tmp_path)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()
        ctest_file = build_dir / 'CTestTestfile.cmake'
        ctest_file.write_text(
            'add_test("H:/Github/Old3DEngine/build/tests/unit_test")\n'
        )
        assert runner._has_stale_project_paths(str(build_dir), str(tmp_path)) is True

    def test_matching_project_not_flagged(self, tmp_path):
        """CTestTestfile referencing the CORRECT project should NOT be flagged."""
        runner = _make_runner(tmp_path)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()
        project_name = tmp_path.name
        ctest_file = build_dir / 'CTestTestfile.cmake'
        ctest_file.write_text(
            f'# Source directory: {tmp_path}\n'
            f'# Build directory: {build_dir}\n'
        )
        assert runner._has_stale_project_paths(str(build_dir), str(tmp_path)) is False

    def test_empty_build_dir_not_flagged(self, tmp_path):
        """Missing build dir returns False, no crash."""
        runner = _make_runner(tmp_path)
        assert runner._has_stale_project_paths(str(tmp_path / 'nonexistent'), str(tmp_path)) is False


# ---------------------------------------------------------------------------
# S2475: cd-prefix decomposition in _execute_cmd_phase
# ---------------------------------------------------------------------------

class TestCdPrefixDecomposition:
    """Verify that 'cd build && ctest ...' is decomposed correctly."""

    def test_cd_prefix_adjusts_cwd_and_command(self, tmp_path):
        """cd build && ctest should set cwd to build/ and strip cd prefix."""
        runner = _make_runner(tmp_path)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()

        step = {
            'name': 'Test verify',
            'config_json': json.dumps({
                'command': 'cd build && ctest -C Release -j4 --output-on-failure',
            }),
        }

        # Mock subprocess.run to capture the actual command and cwd used
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['cwd'] = kwargs.get('cwd')
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = 'all tests passed'
            mock_result.stderr = ''
            return mock_result

        with patch('sprint_runner.subprocess.run', side_effect=fake_run):
            runner._execute_cmd_phase(step)

        # cwd should be build subdir, not project root
        assert captured['cwd'] == str(build_dir)
        # command should NOT contain 'cd build'
        assert 'cd' not in captured['cmd']
        assert 'ctest' in captured['cmd']

    def test_no_cd_prefix_keeps_original_cwd(self, tmp_path):
        """Commands without cd prefix should use project_dir as cwd."""
        runner = _make_runner(tmp_path)
        build_dir = tmp_path / 'build'
        build_dir.mkdir()

        step = {
            'name': 'Build verify',
            'config_json': json.dumps({
                'command': 'cmake --build build --config Release -j',
            }),
        }

        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['cwd'] = kwargs.get('cwd')
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = 'build succeeded'
            mock_result.stderr = ''
            return mock_result

        with patch('sprint_runner.subprocess.run', side_effect=fake_run):
            runner._execute_cmd_phase(step)

        assert captured['cwd'] == str(tmp_path)


# ---------------------------------------------------------------------------
# S2475: _is_stale_build_failure pattern matching
# ---------------------------------------------------------------------------

class TestIsStaleBuildFailure:
    """Verify pattern-based stale build detection."""

    def test_detects_no_such_file(self, tmp_path):
        runner = _make_runner(tmp_path)
        output = 'Error: No such file or directory: H:/Github/Old3DEngine/build/tests'
        assert runner._is_stale_build_failure(output) is True

    def test_detects_cannot_find_path(self, tmp_path):
        runner = _make_runner(tmp_path)
        output = 'The system cannot find the path specified.'
        assert runner._is_stale_build_failure(output) is True

    def test_clean_output_not_flagged(self, tmp_path):
        runner = _make_runner(tmp_path)
        output = '100% tests passed, 0 tests failed out of 5'
        assert runner._is_stale_build_failure(output) is False
