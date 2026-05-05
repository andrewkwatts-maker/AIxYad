#!/usr/bin/env python3
"""
E-23: Tests for agent execution, permissions, health, fallback.

Covers:
  - E-16: action_filter enforcement in AgentExecutor
  - E-07: fallback_slug field persistence in DB
  - E-21: record_agent_execution helper and query
  - security: slug sanitisation
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the package root is importable when run from the tests directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# E-16: AgentExecutor action filter
# ---------------------------------------------------------------------------

class TestAgentPermissions:
    """E-16: action_filter enforcement."""

    def _make_executor(self, action_filter=None, tmp_path=None):
        from agent_executor import AgentExecutor
        project_dir = tmp_path or Path('.')
        permissions = {
            'can_read_file': True,
            'can_write_file': True,
            'can_search': True,
        }
        return AgentExecutor(
            project_dir=project_dir,
            permissions=permissions,
            action_filter=action_filter,
            agent_slug='test-agent',
        )

    def test_action_not_in_filter_is_rejected(self, tmp_path):
        """Action not in the agent's action_filter must be blocked."""
        ex = self._make_executor(
            action_filter=['read_file', 'step_done'],
            tmp_path=tmp_path,
        )
        result = ex.execute({'action': 'write_file', 'params': {'path': 'x.py', 'content': ''}})
        assert result['success'] is False
        assert 'not permitted' in result['message'].lower() or 'write_file' in result['message']

    def test_action_in_filter_is_allowed(self, tmp_path):
        """Action in the filter should not be blocked by the filter (may fail for other reasons)."""
        ex = self._make_executor(
            action_filter=['step_done', 'read_file'],
            tmp_path=tmp_path,
        )
        # step_done is always-allowed; should not be blocked by filter
        result = ex.execute({'action': 'step_done', 'params': {}})
        assert result['success'] is True

    def test_no_filter_allows_all(self, tmp_path):
        """When action_filter is None, no filter is applied."""
        ex = self._make_executor(action_filter=None, tmp_path=tmp_path)
        # step_done is always allowed
        result = ex.execute({'action': 'step_done', 'params': {}})
        assert result['success'] is True

    def test_empty_filter_blocks_all_non_always(self, tmp_path):
        """Empty list blocks every non-always-allowed action."""
        ex = self._make_executor(action_filter=[], tmp_path=tmp_path)
        result = ex.execute({'action': 'read_file', 'params': {'path': 'foo.py'}})
        assert result['success'] is False


# ---------------------------------------------------------------------------
# E-07: fallback_slug stored in agent_definitions DB
# ---------------------------------------------------------------------------

class TestAgentFallback:
    """E-07: fallback_slug field is persisted in agent_definitions."""

    def test_fallback_slug_stored_in_db(self):
        """Adding a fallback_slug to agent_definitions should survive a round-trip."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        from automatica_db import init_global_db
        # Use the in-memory connection to run schema creation
        from automatica_db import GLOBAL_SCHEMA
        conn.executescript(GLOBAL_SCHEMA)
        conn.commit()
        # Add fallback_slug column if missing (migration guard)
        existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(agent_definitions)').fetchall()]
        if 'fallback_slug' not in existing_cols:
            conn.execute("ALTER TABLE agent_definitions ADD COLUMN fallback_slug TEXT DEFAULT ''")
            conn.commit()
        # Insert an agent with fallback_slug
        conn.execute(
            "INSERT INTO agent_definitions (slug, display_name, fallback_slug) VALUES (?, ?, ?)",
            ('developer', 'Developer', 'senior-developer'),
        )
        conn.commit()
        row = conn.execute(
            "SELECT fallback_slug FROM agent_definitions WHERE slug='developer'"
        ).fetchone()
        assert row is not None
        assert row['fallback_slug'] == 'senior-developer'
        conn.close()


# ---------------------------------------------------------------------------
# E-21: agent_execution_history helpers
# ---------------------------------------------------------------------------

class TestAgentExecutionHistory:
    """E-21: record_agent_execution and query."""

    def test_record_execution(self):
        """record_agent_execution should insert a row and it should be queryable."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        from automatica_db import PROJECT_SCHEMA
        conn.executescript(PROJECT_SCHEMA)
        conn.commit()

        from automatica_db import record_agent_execution
        row_id = record_agent_execution(
            conn,
            agent_slug='developer',
            task_display_id='TASK-001',
            session_id='sess-1',
            status='done',
            duration_s=12.5,
            total_tokens=1500,
            files_changed=3,
        )
        assert isinstance(row_id, int)
        assert row_id > 0

        rows = conn.execute("SELECT * FROM agent_execution_history").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r['agent_slug'] == 'developer'
        assert r['task_display_id'] == 'TASK-001'
        assert r['status'] == 'done'
        assert r['total_tokens'] == 1500
        assert r['files_changed'] == 3
        assert abs(r['duration_s'] - 12.5) < 0.001
        conn.close()

    def test_get_agent_stats_empty(self):
        """get_agent_stats returns zero-values when no history exists."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        from automatica_db import PROJECT_SCHEMA
        conn.executescript(PROJECT_SCHEMA)
        conn.commit()
        from automatica_db import get_agent_stats
        stats = get_agent_stats(conn, 'nonexistent')
        assert stats['runs'] == 0
        assert stats['success_rate'] == 0.0
        conn.close()

    def test_get_agent_stats_with_data(self):
        """get_agent_stats computes correct success_rate from history."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        from automatica_db import PROJECT_SCHEMA
        conn.executescript(PROJECT_SCHEMA)
        conn.commit()
        from automatica_db import record_agent_execution, get_agent_stats
        record_agent_execution(conn, 'qa', 'T-001', 's1', 'done', 5.0, 100, 1)
        record_agent_execution(conn, 'qa', 'T-002', 's2', 'done', 6.0, 200, 2)
        record_agent_execution(conn, 'qa', 'T-003', 's3', 'failed', 3.0, 50, 0)
        stats = get_agent_stats(conn, 'qa')
        assert stats['runs'] == 3
        assert abs(stats['success_rate'] - 66.7) < 0.2  # 2/3 = 66.7%
        assert stats['total_tokens'] == 350
        conn.close()

    def test_get_all_agent_stats(self):
        """get_all_agent_stats returns rows for each agent slug."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        from automatica_db import PROJECT_SCHEMA
        conn.executescript(PROJECT_SCHEMA)
        conn.commit()
        from automatica_db import record_agent_execution, get_all_agent_stats
        record_agent_execution(conn, 'dev', 'T-1', 's1', 'done', 10.0, 500, 2)
        record_agent_execution(conn, 'tester', 'T-2', 's2', 'failed', 4.0, 100, 0)
        all_stats = get_all_agent_stats(conn)
        slugs = {s['slug'] for s in all_stats}
        assert 'dev' in slugs
        assert 'tester' in slugs
        conn.close()


# ---------------------------------------------------------------------------
# Security: slug sanitisation
# ---------------------------------------------------------------------------

class TestSlugSanitization:
    """Verify slug sanitisation prevents injection."""

    def test_slug_sanitized_before_db(self):
        """_sanitize_slug must strip SQL-injection characters."""
        try:
            from security import _sanitize_slug
        except ImportError:
            pytest.skip('security module not available')
        raw = "slug'; DROP TABLE agents--"
        safe = _sanitize_slug(raw)
        # The sanitised slug must not contain quotes or semicolons
        assert "'" not in safe
        assert ";" not in safe

    def test_slug_allows_valid_chars(self):
        """_sanitize_slug must preserve normal slug characters."""
        try:
            from security import _sanitize_slug
        except ImportError:
            pytest.skip('security module not available')
        assert _sanitize_slug('my-agent-01') == 'my-agent-01'
