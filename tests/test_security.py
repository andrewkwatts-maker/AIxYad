#!/usr/bin/env python3
"""
Security tests for Sprint C hardening measures.

C-24: Covers path traversal prevention, slug sanitization, rate limiting,
      secrets encryption, log filtering, and settings validation.
"""
import importlib
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so local modules resolve
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── C-07: Path traversal prevention ──────────────────────────────────────────

class TestPathTraversal:
    def test_safe_path_blocks_traversal(self, tmp_path):
        from security import safe_path
        with pytest.raises(ValueError, match='traversal'):
            safe_path(tmp_path, '../../../etc/passwd')

    def test_safe_path_allows_valid(self, tmp_path):
        from security import safe_path
        (tmp_path / 'file.py').write_text('hello')
        result = safe_path(tmp_path, 'file.py')
        assert result.exists()

    def test_safe_path_blocks_absolute_escape(self, tmp_path):
        from security import safe_path
        with pytest.raises(ValueError):
            safe_path(tmp_path, '/etc/passwd')

    def test_safe_path_allows_subdir(self, tmp_path):
        from security import safe_path
        sub = tmp_path / 'subdir'
        sub.mkdir()
        (sub / 'data.txt').write_text('ok')
        result = safe_path(tmp_path, 'subdir/data.txt')
        assert result.exists()

    def test_safe_project_path_rejects_missing(self, tmp_path):
        from security import safe_project_path
        with pytest.raises(ValueError, match='not exist'):
            safe_project_path(str(tmp_path / 'nonexistent'))

    def test_safe_project_path_rejects_file(self, tmp_path):
        from security import safe_project_path
        f = tmp_path / 'file.txt'
        f.write_text('x')
        with pytest.raises(ValueError, match='not a directory'):
            safe_project_path(str(f))

    def test_safe_project_path_accepts_dir(self, tmp_path):
        from security import safe_project_path
        result = safe_project_path(str(tmp_path))
        assert result == tmp_path.resolve()


# ── C-08: Agent slug sanitization ────────────────────────────────────────────

class TestSlugSanitization:
    def test_slug_strips_sql_chars(self):
        from security import _sanitize_slug
        result = _sanitize_slug("agent'; DROP TABLE--")
        assert "'" not in result
        assert ';' not in result
        # Spaces and special chars replaced by dashes
        assert re.match(r'^[a-z0-9-]+$', result)

    def test_slug_length_capped(self):
        from security import _sanitize_slug
        assert len(_sanitize_slug('a' * 100)) <= 63

    def test_slug_lowercased(self):
        from security import _sanitize_slug
        assert _sanitize_slug('MyAgent') == 'myagent'

    def test_slug_empty_raises(self):
        from security import _sanitize_slug
        with pytest.raises(ValueError, match='Invalid'):
            _sanitize_slug('---')  # all dashes stripped

    def test_slug_valid_passthrough(self):
        from security import _sanitize_slug
        assert _sanitize_slug('my-agent-01') == 'my-agent-01'

    def test_slug_collapses_hyphens(self):
        from security import _sanitize_slug
        result = _sanitize_slug('a  b  c')
        assert '--' not in result


# ── C-01 / C-11: Encryption at rest ──────────────────────────────────────────

class TestSecretsEncryption:
    def _get_secrets(self):
        # Import from the services directory
        services_dir = _REPO_ROOT.parent / 'Automatica-App' / 'services'
        if str(services_dir) not in sys.path:
            sys.path.insert(0, str(services_dir))
        import importlib
        spec = importlib.util.spec_from_file_location(
            'secrets_mod', services_dir / 'secrets.py'
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_encrypt_returns_enc_prefix(self):
        sec = self._get_secrets()
        result = sec.encrypt('my-secret-key')
        assert result.startswith('enc:')

    def test_encrypt_decrypt_roundtrip(self):
        sec = self._get_secrets()
        original = 'super-secret-api-key-12345'
        encrypted = sec.encrypt(original)
        assert encrypted != original
        decrypted = sec.decrypt(encrypted)
        assert decrypted == original

    def test_encrypt_empty_passthrough(self):
        sec = self._get_secrets()
        assert sec.encrypt('') == ''

    def test_decrypt_plaintext_passthrough(self):
        sec = self._get_secrets()
        assert sec.decrypt('plaintext-value') == 'plaintext-value'

    def test_double_encrypt_is_idempotent(self):
        sec = self._get_secrets()
        once = sec.encrypt('value')
        twice = sec.encrypt(once)
        assert once == twice  # already encrypted — should not double-encrypt


# ── C-17 / C-18: Log secret filtering ────────────────────────────────────────

class TestLogFilter:
    def test_api_key_redacted(self):
        from log_filter import _scrub
        result = _scrub('api_key=sk-abc123xyz')
        assert 'sk-abc123xyz' not in result
        assert 'REDACTED' in result

    def test_password_redacted(self):
        from log_filter import _scrub
        result = _scrub('password: hunter2')
        assert 'hunter2' not in result
        assert 'REDACTED' in result

    def test_bearer_token_redacted(self):
        from log_filter import _scrub
        result = _scrub('Authorization: Bearer abc123token')
        assert 'abc123token' not in result
        assert 'REDACTED' in result

    def test_clean_text_unchanged(self):
        from log_filter import _scrub
        text = 'Task TASK-001 completed successfully in 2.3s'
        assert _scrub(text) == text

    def test_secret_filter_as_logging_filter(self):
        import logging
        from log_filter import SecretFilter
        logger = logging.getLogger('test_secret_filter')
        logger.addFilter(SecretFilter())
        # This should not raise; the filter scrubs the message
        logger.info('api_key=topsecret123')
        logger.removeFilter(logger.filters[-1])


# ── C-20: Settings schema validation ─────────────────────────────────────────

class TestSettingsValidation:
    def test_valid_settings_passthrough(self):
        from security_utils import validate_settings
        data = {'server_url': 'http://localhost:8765', 'mode': 'server', 'log_retention_days': 30}
        result = validate_settings(data)
        assert result['server_url'] == 'http://localhost:8765'
        assert result['log_retention_days'] == 30

    def test_type_coercion(self):
        from security_utils import validate_settings
        data = {'log_retention_days': '7'}  # string → int
        result = validate_settings(data)
        assert result['log_retention_days'] == 7

    def test_invalid_type_dropped(self):
        from security_utils import validate_settings
        data = {'log_retention_days': 'not-a-number'}
        result = validate_settings(data)
        # Invalid value dropped silently
        assert 'log_retention_days' not in result

    def test_unknown_keys_preserved(self):
        from security_utils import validate_settings
        data = {'custom_key': 'custom_value', 'mode': 'server'}
        result = validate_settings(data)
        assert result['custom_key'] == 'custom_value'

    def test_non_dict_returns_empty(self):
        from security_utils import validate_settings
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            result = validate_settings('not-a-dict')
            assert result == {}


# ── C-04: Rate limiting ───────────────────────────────────────────────────────

def _import_engine_api_server():
    """Import the engine api_server module directly (not the App wrapper)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'engine_api_server', _REPO_ROOT / 'api_server.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRateLimit:
    def test_rate_limit_middleware_exists(self):
        """Verify the rate limiter data structures are present."""
        api_server = _import_engine_api_server()
        assert hasattr(api_server, '_rate_buckets')
        assert hasattr(api_server, '_RATE_LIMIT')
        assert hasattr(api_server, '_RATE_WINDOW')
        assert api_server._RATE_LIMIT == 60
        assert api_server._RATE_WINDOW == 60.0

    def test_rate_limit_endpoint_200_under_limit(self):
        """First N requests within limit should succeed."""
        try:
            from fastapi.testclient import TestClient
            _as = _import_engine_api_server()
            # Clear buckets for a clean test
            _as._rate_buckets.clear()
            srv = _as.APIServer()
            client = TestClient(srv._build_app())
            # Make a handful of requests — all should be 200
            for i in range(5):
                r = client.get('/api/status')
            assert r.status_code == 200
        except ImportError:
            pytest.skip('fastapi[testclient] not installed')

    def test_session_ttl_constant(self):
        """Verify session TTL is set to a reasonable value."""
        api_server = _import_engine_api_server()
        assert hasattr(api_server, 'SESSION_TTL')
        assert api_server.SESSION_TTL == 3600


# ── C-06: Pydantic model validation ──────────────────────────────────────────

class TestPydanticModels:
    def test_task_create_title_required(self):
        api_server = _import_engine_api_server()
        if not api_server._PYDANTIC_AVAILABLE:
            pytest.skip('pydantic not installed')
        with pytest.raises(Exception):
            api_server.TaskCreate(title='', complexity=3)

    def test_task_create_complexity_bounds(self):
        api_server = _import_engine_api_server()
        if not api_server._PYDANTIC_AVAILABLE:
            pytest.skip('pydantic not installed')
        with pytest.raises(Exception):
            api_server.TaskCreate(title='Test', complexity=99)

    def test_agent_create_slug_pattern(self):
        api_server = _import_engine_api_server()
        if not api_server._PYDANTIC_AVAILABLE:
            pytest.skip('pydantic not installed')
        with pytest.raises(Exception):
            api_server.AgentCreate(slug='UPPERCASE-slug')

    def test_agent_create_valid_slug(self):
        api_server = _import_engine_api_server()
        if not api_server._PYDANTIC_AVAILABLE:
            pytest.skip('pydantic not installed')
        agent = api_server.AgentCreate(slug='my-agent-01')
        assert agent.slug == 'my-agent-01'


# ── Import helper for re module ───────────────────────────────────────────────

import re  # noqa: E402 — placed here intentionally for test module scope
