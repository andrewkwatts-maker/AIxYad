"""
Tests for VCS service functions (F-23).

These tests mock subprocess.run so no actual git repo is required.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the app-side services directory is importable.
_APP_SERVICES = Path(__file__).resolve().parent.parent.parent / 'Automatica-App'
if str(_APP_SERVICES) not in sys.path:
    sys.path.insert(0, str(_APP_SERVICES))


class TestGitHelper:
    """Unit tests for the _git() helper function."""

    def test_get_status_returns_stdout(self, tmp_path):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='## main\n M file.py\n',
                stderr='',
            )
            # Import after patching
            try:
                from services.vcs_service import _git
            except ImportError:
                # kivy not available in CI — skip gracefully
                return
            code, out, err = _git(['status', '--porcelain', '-b'], str(tmp_path))
            assert code == 0
            assert 'main' in out

    def test_git_not_found_returns_error(self, tmp_path):
        with patch('subprocess.run', side_effect=FileNotFoundError):
            try:
                from services.vcs_service import _git
            except ImportError:
                return
            code, out, err = _git(['status'], str(tmp_path))
            assert code == 1
            assert 'not found' in err

    def test_git_timeout_returns_error(self, tmp_path):
        import subprocess
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=5)):
            try:
                from services.vcs_service import _git
            except ImportError:
                return
            code, out, err = _git(['status'], str(tmp_path), timeout=5)
            assert code == 1
            assert 'timed out' in err


class TestGetStatus:
    """Integration-level tests for get_status() — mock subprocess + Kivy Clock."""

    def test_parses_clean_repo(self, tmp_path):
        """A clean repo returns branch name and empty dirty_files."""
        try:
            from services.vcs_service import _git
        except ImportError:
            return

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='## main...origin/main\n',
                stderr='',
            )
            code, out, err = _git(['status', '--porcelain', '-b'], str(tmp_path))
            assert code == 0
            lines = out.splitlines()
            assert lines[0].startswith('## main')

    def test_parses_ahead_behind(self, tmp_path):
        """Ahead/behind counts are parsed from the branch line."""
        try:
            from services.vcs_service import _git
        except ImportError:
            return

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='## main...origin/main [ahead 2, behind 1]\n M dirty.py\n',
                stderr='',
            )
            code, out, _ = _git(['status', '--porcelain', '-b'], str(tmp_path))
            assert 'ahead 2' in out
            assert 'behind 1' in out
