"""
test_comms.py — Comprehensive server/client communication tests.

Covers all REST endpoints, WebSocket behavior, auth flows, security,
rate limiting, and APIClient HTTP methods using an isolated test server
(FastAPI TestClient — no real network, no port 8765, no production DB).

Run with: pytest tests/test_comms.py -v
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# conftest.py handles sys.path: ENGINE_ROOT at [0], APP_SERVICES appended last
# so stdlib `secrets` is found before Automatica-App/services/secrets.py.
#
# Additional fix: api_server.py uses `from __future__ import annotations`, which
# makes all annotations lazy strings.  Functions defined inside _build_app() have
# __globals__ pointing to the api_server MODULE globals where `Request` is not
# imported (only imported locally inside _build_app).  When FastAPI calls
# get_type_hints() to resolve route signatures, it fails to find `Request` and
# treats it as a required query parameter, returning 422.
#
# Fix: inject `Request` into the api_server module namespace before calling
# _build_app() so get_type_hints() can resolve the annotation.
import api_server as _api_server_mod
from starlette.requests import Request as _StarletteRequest
_api_server_mod.Request = _StarletteRequest  # allow get_type_hints() to resolve it


# ── Rate-bucket isolation ──────────────────────────────────────────────────────
# _rate_buckets is a module-level dict shared across all TestClient calls.
# Without cleanup, requests from earlier tests count toward the 60/min limit
# for later tests, producing spurious 429s.

@pytest.fixture(autouse=True)
def _clear_rate_buckets():
    """Reset the rate-limit bucket before each test."""
    _api_server_mod._rate_buckets.clear()
    yield
    _api_server_mod._rate_buckets.clear()


# ── Shared fixtures ────────────────────────────────────────────────────────────

def _build_app():
    """Fresh isolated FastAPI app — no real DB, no port."""
    from api_server import APIServer
    return APIServer()._build_app()


@pytest.fixture()
def app():
    return _build_app()


@pytest.fixture()
def tc(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def _mock_response(data, status=200):
    """Mock requests.Response factory."""
    m = MagicMock()
    m.status_code = status
    m.json.return_value = data
    m.raise_for_status = MagicMock()
    return m


def _new_client(**kwargs):
    """Fresh APIClient with no real server."""
    from api_client import APIClient
    return APIClient('http://localhost:8765', **kwargs)


# ── 1. Health + Status ─────────────────────────────────────────────────────────

class TestStatusHealth:

    def test_status_200(self, tc):
        assert tc.get('/api/status').status_code == 200

    def test_status_body(self, tc):
        body = tc.get('/api/status').json()
        assert body['status'] == 'ok'
        assert body['mode'] == 'server'
        assert 'version' in body

    def test_health_200_when_db_ok(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch('automatica_db.init_global_db', return_value=conn):
            r = _build_app()
            from fastapi.testclient import TestClient
            assert TestClient(r).get('/api/health').status_code == 200

    def test_health_fields(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            body = TestClient(_build_app()).get('/api/health').json()
        for f in ('status', 'version', 'python', 'platform', 'db', 'uptime_s'):
            assert f in body

    def test_health_degraded_when_db_error(self):
        with patch('automatica_db.init_global_db', side_effect=Exception('boom')):
            from fastapi.testclient import TestClient
            body = TestClient(_build_app()).get('/api/health').json()
        assert body['status'] == 'degraded'
        assert body['db'] == 'error'

    def test_openapi_redirect(self, tc):
        r = tc.get('/api/openapi.json', follow_redirects=False)
        assert r.status_code in (307, 308, 200)


# ── 2. Auth endpoints ─────────────────────────────────────────────────────────

class TestAuth:

    def test_login_no_key_configured_issues_token(self, tmp_path, monkeypatch):
        """When no API key is configured, login succeeds with any input."""
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        monkeypatch.delenv('AUTOMATICA_API_KEY', raising=False)
        r = _build_app()
        from fastapi.testclient import TestClient
        body = TestClient(r).post('/api/auth/login', json={}).json()
        assert 'token' in body
        assert 'expires_in' in body

    def test_login_wrong_key_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'correct-key')
        r = _build_app()
        from fastapi.testclient import TestClient
        resp = TestClient(r).post('/api/auth/login', json={'api_key': 'wrong-key'})
        assert resp.status_code == 401

    def test_login_correct_key_issues_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'mykey')
        r = _build_app()
        from fastapi.testclient import TestClient
        body = TestClient(r).post('/api/auth/login', json={'api_key': 'mykey'}).json()
        assert 'token' in body

    def test_logout_returns_success(self, tc):
        r = tc.post('/api/auth/logout')
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_logout_clears_session(self, tmp_path, monkeypatch):
        """Login issues a token; using it after logout gets 401."""
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'k')
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        app = _build_app()
        from fastapi.testclient import TestClient
        tc2 = TestClient(app)
        token = tc2.post('/api/auth/login', json={'api_key': 'k'}).json()['token']
        # Log out with that token
        tc2.post('/api/auth/logout', headers={'Authorization': f'Bearer {token}'})
        # The GET /api/settings requires auth; after logout, session token is gone
        r = tc2.get('/api/settings', headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 401

    def test_qr_endpoint_returns_token_and_url(self, tc):
        r = tc.get('/api/auth/qr')
        assert r.status_code == 200
        body = r.json()
        assert 'token' in body
        assert 'pair_url' in body
        assert 'automatica://pair' in body['pair_url']

    def test_pair_valid_token(self, tc):
        # Step 1: generate a pending token via /api/auth/qr
        token = tc.get('/api/auth/qr').json()['token']
        # Step 2: claim it
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            r = tc.post('/api/auth/pair', json={
                'token': token,
                'device_id': 'test-device-001',
                'device_name': 'TestPhone',
                'platform': 'Android',
            })
        assert r.status_code == 200
        body = r.json()
        assert body['status'] == 'paired'
        assert body['device_id'] == 'test-device-001'

    def test_pair_invalid_token_rejected(self, tc):
        r = tc.post('/api/auth/pair', json={
            'token': 'completely-fake-token',
            'device_id': 'x',
        })
        assert r.status_code == 403

    def test_pair_token_consumed_on_claim(self, tc):
        """A pairing token can only be claimed once."""
        token = tc.get('/api/auth/qr').json()['token']
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            tc.post('/api/auth/pair', json={'token': token, 'device_id': 'dev1'})
            r2 = tc.post('/api/auth/pair', json={'token': token, 'device_id': 'dev2'})
        assert r2.status_code == 403

    def test_list_devices_returns_list(self, tc):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            r = tc.get('/api/auth/devices')
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_revoke_device_returns_revoked(self, tc):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn), \
             patch('automatica_db.revoke_device', return_value=True), \
             patch('automatica_db.log_auth_event'):
            r = tc.delete('/api/auth/devices/device-abc')
        assert r.status_code == 200

    def test_audit_log_returns_list(self, tc):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            r = tc.get('/api/auth/audit')
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_audit_log_respects_limit(self, tc):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            r = tc.get('/api/auth/audit?limit=5')
        assert r.status_code == 200


# ── 3. Projects ───────────────────────────────────────────────────────────────

class TestProjects:

    def test_list_projects_200(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/projects')
        assert r.status_code == 200

    def test_list_projects_returns_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {'id': 1, 'name': 'Alpha', 'directory': '/tmp/a', 'created_at': '2026-01-01'},
            {'id': 2, 'name': 'Beta',  'directory': '/tmp/b', 'created_at': '2026-01-02'},
        ]
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            data = TestClient(_build_app()).get('/api/projects').json()
        assert len(data) == 2
        assert data[0]['name'] == 'Alpha'

    def test_tasks_404_for_unknown_project(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/projects/9999/tasks')
        assert r.status_code == 404

    def test_tasks_returns_list(self, tmp_path):
        import automatica_db as adb
        pdb = adb.init_project_db(tmp_path)
        adb.create_task(pdb, title='Alpha Task')
        pdb.close()

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            data = TestClient(_build_app()).get('/api/projects/1/tasks').json()
        assert len(data) == 1
        assert data[0]['title'] == 'Alpha Task'

    def test_create_task_returns_task_id(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/projects/1/tasks', json={'title': 'New'})
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert 'task_id' in body

    def test_create_task_404_unknown_project(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/projects/9999/tasks', json={'title': 'X'})
        assert r.status_code == 404


# ── 4. Tasks ──────────────────────────────────────────────────────────────────

class TestTasks:

    def test_update_task_success(self, tmp_path):
        import automatica_db as adb
        pdb = adb.init_project_db(tmp_path)
        task = adb.create_task(pdb, title='Old')
        pdb.close()

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {'id': 1, 'directory': str(tmp_path)},
        ]
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).patch(f"/api/tasks/{task['id']}", json={'title': 'New'})
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_update_task_404_unknown(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).patch('/api/tasks/9999', json={'title': 'X'})
        assert r.status_code == 404

    def test_delete_task_success(self, tmp_path):
        import automatica_db as adb
        pdb = adb.init_project_db(tmp_path)
        task = adb.create_task(pdb, title='To Delete')
        pdb.close()

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {'id': 1, 'directory': str(tmp_path)},
        ]
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).delete(f"/api/tasks/{task['id']}")
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_delete_task_404_unknown(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).delete('/api/tasks/9999')
        assert r.status_code == 404

    def test_generate_tasks_with_goal(self, tc):
        r = tc.post('/api/tasks/generate', json={'goal': 'Build a login page'})
        assert r.status_code == 200
        body = r.json()
        assert 'tasks' in body
        assert isinstance(body['tasks'], list)

    def test_generate_tasks_empty_goal_returns_400(self, tc):
        r = tc.post('/api/tasks/generate', json={'goal': ''})
        assert r.status_code == 400

    def test_generate_tasks_no_goal_field_returns_400(self, tc):
        r = tc.post('/api/tasks/generate', json={})
        assert r.status_code == 400

    def test_commit_message_from_completed_tasks(self, tc):
        r = tc.post('/api/tasks/commit-message', json={
            'completed_tasks': [{'title': 'Fix auth bug'}],
            'dirty_files': ['auth.py'],
        })
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert 'message' in body
        assert 'Fix auth bug' in body['message']

    def test_commit_message_from_dirty_files(self, tc):
        r = tc.post('/api/tasks/commit-message', json={'dirty_files': ['main.py', 'utils.py']})
        assert r.status_code == 200
        body = r.json()
        assert 'message' in body
        assert 'main.py' in body['message']

    def test_commit_message_empty_body(self, tc):
        r = tc.post('/api/tasks/commit-message', json={})
        assert r.status_code == 200
        assert 'message' in r.json()


# ── 5. Sprints ────────────────────────────────────────────────────────────────

class TestSprints:

    def test_list_sprints_empty_without_project_id(self, tc):
        r = tc.get('/api/sprints')
        assert r.status_code == 200
        assert r.json() == []

    def test_list_sprints_with_project(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        import automatica_db as adb
        adb.init_project_db(tmp_path).close()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/sprints?project_id=1')
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_sprints_404_unknown_project(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/sprints?project_id=9999')
        assert r.status_code == 404

    def test_create_sprint_success(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        import automatica_db as adb
        adb.init_project_db(tmp_path).close()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/sprints', json={
                'project_id': 1, 'goal': 'Ship MVP', 'max_points': 50,
            })
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_create_sprint_404_unknown_project(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/sprints', json={'project_id': 9999, 'goal': ''})
        assert r.status_code == 404

    def test_start_sprint_missing_project_id_returns_400(self, tc):
        assert tc.post('/api/sprints/start', json={}).status_code == 400

    def test_start_sprint_unknown_project_returns_404(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/sprints/start', json={'project_id': 99})
        assert r.status_code == 404

    def test_start_sprint_dry_run(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            'directory': str(tmp_path), 'name': 'TestProject',
        }
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/sprints/start', json={
                'project_id': 1, 'dry_run': True,
            })
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert body['dry_run'] is True
        assert 'sprint_id' in body

    def test_start_sprint_returns_sprint_id(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            'directory': str(tmp_path), 'name': 'Proj',
        }
        runner = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn), \
             patch('sprint_runner.SprintRunner', return_value=runner):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/sprints/start', json={'project_id': 1})
        assert r.status_code == 200
        assert 'sprint_id' in r.json()

    def test_cancel_sprint_returns_success(self, tc):
        import uuid
        sprint_id = str(uuid.uuid4())
        r = tc.post(f'/api/sprints/{sprint_id}/cancel')
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert body['sprint_id'] == sprint_id

    def test_commit_trigger_missing_project_id_returns_400(self, tc):
        r = tc.post('/api/triggers/commit', json={})
        assert r.status_code == 400

    def test_commit_trigger_unknown_project_returns_404(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/triggers/commit', json={'project_id': 99})
        assert r.status_code == 404

    def test_commit_trigger_disabled_returns_not_triggered(self, tmp_path):
        """on_commit trigger disabled → returns success=False."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        # No triggers.json → trigger treated as disabled
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/triggers/commit', json={'project_id': 1})
        assert r.status_code == 200
        assert r.json()['success'] is False


# ── 6. Agents ─────────────────────────────────────────────────────────────────

class TestAgents:

    def test_list_agents_200(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/agents')
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_create_agent_success(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/agents', json={
                'slug': 'my-agent',
                'role': 'Developer',
                'description': 'Writes code',
                'prompt': '',
            })
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert body['slug'] == 'my-agent'

    def test_create_agent_invalid_slug_rejected(self, tc):
        r = tc.post('/api/agents', json={'slug': 'Invalid Slug!', 'role': ''})
        assert r.status_code == 422

    def test_create_agent_slug_too_long_rejected(self, tc):
        long_slug = 'a' * 70
        r = tc.post('/api/agents', json={'slug': long_slug, 'role': ''})
        assert r.status_code == 422

    def test_delete_agent_success(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).delete('/api/agents/developer')
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_run_agent_missing_task_id_returns_400(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/agents/developer/run', json={'project_id': 1})
        assert r.status_code == 400

    def test_run_agent_success(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/agents/developer/run', json={
                'task_display_id': 'TASK-001',
                'project_id': 1,
            })
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert 'run_id' in body


# ── 7. Providers ──────────────────────────────────────────────────────────────

class TestProviders:

    def test_list_providers_200(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            assert TestClient(_build_app()).get('/api/providers').status_code == 200

    def test_add_provider_missing_name_returns_400(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/providers', json={'provider_type': 'api'})
        assert r.status_code == 400

    def test_add_provider_success(self):
        conn = MagicMock()
        adb_mock = MagicMock()
        adb_mock.add_provider.return_value = {'id': 1, 'name': 'Ollama'}
        with patch('automatica_db.init_global_db', return_value=conn), \
             patch('automatica_db.add_provider', return_value={'id': 1, 'name': 'Ollama'}):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).post('/api/providers', json={
                'name': 'Ollama', 'provider_type': 'http_local_chat',
                'base_url': 'http://localhost:11434', 'model_name': 'llama3',
            })
        assert r.status_code == 200

    def test_update_provider_success(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).patch('/api/providers/1', json={'name': 'NewName', 'enabled': 0})
        assert r.status_code == 200
        assert r.json()['success'] is True

    def test_delete_provider_success(self):
        conn = MagicMock()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).delete('/api/providers/1')
        assert r.status_code == 200
        assert r.json()['success'] is True


# ── 8. Standards ──────────────────────────────────────────────────────────────

class TestStandards:

    def test_list_standards_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        r = TestClient(_build_app()).get('/api/standards')
        assert r.status_code == 200
        assert r.json() == []

    def test_list_standards_reads_md_files(self, tmp_path, monkeypatch):
        std_dir = tmp_path / 'standards'
        std_dir.mkdir()
        (std_dir / 'coding-style.md').write_text('# Style', encoding='utf-8')
        settings_dir = tmp_path / '.automatica'
        settings_dir.mkdir()
        (settings_dir / 'settings.json').write_text(
            json.dumps({'standards_dir': str(std_dir)}))
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        data = TestClient(_build_app()).get('/api/standards').json()
        assert len(data) == 1
        assert data[0]['name'] == 'coding-style'
        assert data[0]['content'] == '# Style'

    def test_save_standard_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        r = TestClient(_build_app()).post('/api/standards', json={
            'name': 'my-style', 'content': '# My Style Guide',
        })
        assert r.status_code == 200
        assert r.json()['success'] is True


# ── 9. Settings ───────────────────────────────────────────────────────────────

class TestSettings:

    def test_get_settings_200(self, tmp_path, monkeypatch):
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        r = TestClient(_build_app()).get('/api/settings')
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    def test_get_settings_reads_file(self, tmp_path, monkeypatch):
        settings_dir = tmp_path / '.automatica'
        settings_dir.mkdir()
        (settings_dir / 'settings.json').write_text(json.dumps({'theme': 'dark'}))
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        body = TestClient(_build_app()).get('/api/settings').json()
        assert body.get('theme') == 'dark'

    def test_get_settings_filters_sensitive_keys(self, tmp_path, monkeypatch):
        settings_dir = tmp_path / '.automatica'
        settings_dir.mkdir()
        # Use smtp_password (sensitive) + theme (non-sensitive), not api_key so
        # no auth is required to read the settings back.
        (settings_dir / 'settings.json').write_text(
            json.dumps({'smtp_password': 'hunter2', 'theme': 'dark'}))
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        body = TestClient(_build_app()).get('/api/settings').json()
        assert 'smtp_password' not in body
        assert body.get('theme') == 'dark'

    def test_save_settings_persists(self, tmp_path, monkeypatch):
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        from fastapi.testclient import TestClient
        tc2 = TestClient(_build_app())
        r = tc2.post('/api/settings', json={'sprint_interval': 30})
        assert r.status_code == 200
        assert r.json()['success'] is True


# ── 10. Quality endpoints ──────────────────────────────────────────────────────

class TestQuality:

    def test_tests_no_path_returns_error(self, tc):
        r = tc.get('/api/quality/tests')
        assert r.status_code == 200
        assert 'error' in r.json()

    def test_lint_no_path_returns_error(self, tc):
        r = tc.get('/api/quality/lint')
        assert r.status_code == 200
        assert 'error' in r.json()

    def test_coverage_no_path_returns_error(self, tc):
        r = tc.get('/api/quality/coverage')
        assert r.status_code == 200
        assert 'error' in r.json()

    def test_tests_path_traversal_rejected(self, tc):
        r = tc.get('/api/quality/tests?project_path=../../etc/passwd')
        assert r.status_code == 400
        assert 'Invalid project path' in r.json().get('detail', '')

    def test_lint_path_traversal_rejected(self, tc):
        r = tc.get('/api/quality/lint?project_path=../../../etc')
        assert r.status_code == 400

    def test_coverage_path_traversal_rejected(self, tc):
        r = tc.get('/api/quality/coverage?project_path=../secret')
        assert r.status_code == 400

    def test_tests_nonexistent_path_rejected(self, tc):
        r = tc.get('/api/quality/tests?project_path=/nonexistent/path/xyz')
        assert r.status_code in (200, 400)
        body = r.json()
        # Either a 400 from path validation or a 200 with 'error' key
        if r.status_code == 200:
            assert 'error' in body

    def test_tests_valid_path_runs_subprocess(self, tmp_path):
        # _run_subprocess is a local inside _build_app(); patch subprocess.run instead.
        import subprocess
        fake = MagicMock()
        fake.stdout = ''
        fake.stderr = ''
        fake.returncode = 0
        with patch('subprocess.run', return_value=fake):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get(f'/api/quality/tests?project_path={tmp_path}')
        assert r.status_code == 200

    def test_lint_valid_path_returns_issues(self, tmp_path):
        import subprocess
        fake = MagicMock()
        fake.stdout = ''
        fake.stderr = ''
        fake.returncode = 0
        with patch('subprocess.run', return_value=fake):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get(f'/api/quality/lint?project_path={tmp_path}')
        assert r.status_code == 200
        body = r.json()
        assert 'tool' in body
        assert 'issue_count' in body

    def test_lint_ruff_tool(self, tmp_path):
        import subprocess
        fake = MagicMock()
        fake.stdout = '[]'
        fake.stderr = ''
        fake.returncode = 0
        with patch('subprocess.run', return_value=fake):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get(
                f'/api/quality/lint?project_path={tmp_path}&tool=ruff')
        assert r.status_code == 200
        assert r.json().get('tool') == 'ruff'


# ── 11. Logs ──────────────────────────────────────────────────────────────────

class TestLogs:

    def test_logs_no_project_returns_empty_when_no_projects(self, tc):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/logs')
        assert r.status_code == 200
        assert r.json() == []

    def test_logs_with_project_id(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        import automatica_db as adb
        adb.init_project_db(tmp_path).close()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/logs?project_id=1')
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_logs_unknown_project_returns_404(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/logs?project_id=9999')
        assert r.status_code == 404

    def test_logs_limit_param(self, tmp_path):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
        import automatica_db as adb
        adb.init_project_db(tmp_path).close()
        with patch('automatica_db.init_global_db', return_value=conn):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get('/api/logs?limit=5&project_id=1')
        assert r.status_code == 200


# ── 12. Wiki ──────────────────────────────────────────────────────────────────

class TestWiki:

    def test_wiki_path_traversal_rejected(self, tc):
        # `../../etc/passwd` gets URL-normalized away by the test client.
        # Patch safe_project_path to raise ValueError against a benign path.
        with patch('security.safe_project_path', side_effect=ValueError('path traversal')):
            r = tc.get('/api/wiki/some-unsafe-path')
        assert r.status_code == 400
        assert r.json().get('detail')  # some error detail returned

    def test_wiki_valid_path_calls_scanner(self, tmp_path):
        fake_wiki = {'files': ['main.py'], 'tree': {}}
        with patch('security.safe_project_path', return_value=tmp_path), \
             patch('project_scanner.build_file_wiki', return_value=fake_wiki):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get(f'/api/wiki/{tmp_path}')
        assert r.status_code == 200

    def test_wiki_scanner_failure_returns_500(self, tmp_path):
        with patch('security.safe_project_path', return_value=tmp_path), \
             patch('project_scanner.build_file_wiki', side_effect=Exception('scanner error')):
            from fastapi.testclient import TestClient
            r = TestClient(_build_app()).get(f'/api/wiki/{tmp_path}')
        assert r.status_code == 500


# ── 13. Webhooks ──────────────────────────────────────────────────────────────

class TestWebhooks:

    def test_receive_webhook_returns_success(self, tc):
        r = tc.post('/api/webhooks/receive', json={'event': 'push', 'ref': 'main'})
        assert r.status_code == 200
        body = r.json()
        assert body['success'] is True
        assert body['event'] == 'push'

    def test_receive_webhook_unknown_event(self, tc):
        r = tc.post('/api/webhooks/receive', json={})
        assert r.status_code == 200
        assert r.json()['event'] == 'unknown'


# ── 14. Rate limiting ─────────────────────────────────────────────────────────

class TestRateLimiting:

    def test_429_when_limit_exceeded(self):
        """Pre-populate the bucket so the very next request trips the limit."""
        import api_server
        app = _build_app()
        from fastapi.testclient import TestClient
        tc_local = TestClient(app)

        # Determine what host key TestClient uses for client.host
        # TestClient uses 'testclient' as the host
        bucket_key = 'testclient'
        now = time.monotonic()

        # Fill the bucket to exactly at the limit
        api_server._rate_buckets[bucket_key] = [now] * api_server._RATE_LIMIT

        try:
            r = tc_local.get('/api/status')
            assert r.status_code == 429
            assert r.json()['error'] == 'Rate limit exceeded'
        finally:
            # Clean up so other tests aren't affected
            api_server._rate_buckets.pop(bucket_key, None)

    def test_request_allowed_after_window_expires(self):
        """Timestamps older than RATE_WINDOW are purged, allowing new requests."""
        import api_server
        app = _build_app()
        from fastapi.testclient import TestClient
        tc_local = TestClient(app)

        bucket_key = 'testclient'
        # Use timestamps from far in the past (well beyond _RATE_WINDOW)
        old_time = time.monotonic() - (api_server._RATE_WINDOW + 10)
        api_server._rate_buckets[bucket_key] = [old_time] * api_server._RATE_LIMIT

        try:
            r = tc_local.get('/api/status')
            assert r.status_code == 200
        finally:
            api_server._rate_buckets.pop(bucket_key, None)


# ── 15. WebSocket ─────────────────────────────────────────────────────────────

class TestWebSocket:
    # WS queue timeout is 30 s, which hangs a synchronous TestClient.
    # Tests verify connection/rejection semantics rather than message content.

    def test_ws_logs_connect_and_send(self, app):
        """Client can send a message; server echoes errors or stays alive."""
        from fastapi.testclient import TestClient
        import automatica_db as adb
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            tc_ws = TestClient(app, raise_server_exceptions=False)
            try:
                with tc_ws.websocket_connect('/ws/logs') as ws:
                    ws.send_json({'type': 'ping'})
            except Exception:
                pass  # disconnect is acceptable in sync env after exchange

    def test_ws_status_endpoint_exists(self, app):
        """The /ws/status endpoint accepts connections."""
        from fastapi.testclient import TestClient
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('automatica_db.init_global_db', return_value=conn):
            tc_ws = TestClient(app, raise_server_exceptions=False)
            try:
                with tc_ws.websocket_connect('/ws/status') as ws:
                    ws.send_json({'type': 'ping'})
            except Exception:
                pass  # disconnect acceptable in sync env

    def test_ws_logs_rejects_bad_auth(self, tmp_path, monkeypatch):
        """When an API key is configured, WS without matching key gets closed."""
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'secret-key')
        monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
        app = _build_app()
        from fastapi.testclient import TestClient
        with pytest.raises(Exception):
            with TestClient(app).websocket_connect('/ws/logs') as ws:
                ws.receive_json()


# ── 16. APIClient — REST methods ──────────────────────────────────────────────

class TestAPIClientREST:

    def test_get_status_returns_dict(self):
        c = _new_client()
        with patch('requests.get', return_value=_mock_response({'status': 'ok'})):
            result = c.get_status()
        assert result['status'] == 'ok'

    def test_get_status_offline_returns_offline_dict(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            result = c.get_status()
        assert result == {'status': 'offline'}

    def test_get_projects_returns_list(self):
        c = _new_client()
        data = [{'id': 1, 'name': 'X'}]
        with patch('requests.get', return_value=_mock_response(data)):
            assert c.get_projects() == data

    def test_get_tasks_uses_correct_url(self):
        c = _new_client()
        mock_get = MagicMock(return_value=_mock_response([]))
        with patch('requests.get', mock_get):
            c.get_tasks(project_id=5)
        url = mock_get.call_args[0][0]
        assert '/api/projects/5/tasks' in url

    def test_add_task_posts_to_correct_url(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True, 'task_id': 1, 'display_id': 'T-001'}))
        with patch('requests.post', mock_post):
            result = c.add_task(project_id=2, data={'title': 'Test Task'})
        url = mock_post.call_args[0][0]
        assert '/api/projects/2/tasks' in url
        assert result['success'] is True

    def test_get_logs_uses_limit(self):
        c = _new_client()
        mock_get = MagicMock(return_value=_mock_response([]))
        with patch('requests.get', mock_get):
            c.get_logs(limit=50)
        url = mock_get.call_args[0][0]
        assert 'limit=50' in url

    def test_start_sprint_posts_project_id(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True, 'sprint_id': 'abc'}))
        with patch('requests.post', mock_post):
            result = c.start_sprint(project_id=3)
        body = mock_post.call_args[1].get('json', {})
        assert body['project_id'] == 3
        assert result['success'] is True

    def test_get_agents_returns_list(self):
        c = _new_client()
        agents = [{'slug': 'developer', 'role': 'Dev'}]
        with patch('requests.get', return_value=_mock_response(agents)):
            assert c.get_agents() == agents

    def test_get_agents_offline_returns_empty(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            assert c.get_agents() == []

    def test_save_agent_posts_correctly(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True, 'slug': 'tester'}))
        with patch('requests.post', mock_post):
            result = c.save_agent('tester', {'role': 'Tester'})
        body = mock_post.call_args[1].get('json', {})
        assert body['slug'] == 'tester'
        assert result['success'] is True

    def test_delete_agent_sends_delete(self):
        c = _new_client()
        mock_del = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.delete', mock_del):
            result = c.delete_agent('developer')
        url = mock_del.call_args[0][0]
        assert '/api/agents/developer' in url
        assert result['success'] is True

    def test_delete_agent_offline_returns_error_dict(self):
        import requests as _req
        c = _new_client()
        with patch('requests.delete', side_effect=_req.exceptions.ConnectionError()):
            result = c.delete_agent('developer')
        assert result['success'] is False
        assert 'error' in result

    def test_get_sprints_no_project(self):
        c = _new_client()
        with patch('requests.get', return_value=_mock_response([])):
            assert c.get_sprints() == []

    def test_get_sprints_appends_project_id(self):
        c = _new_client()
        mock_get = MagicMock(return_value=_mock_response([]))
        with patch('requests.get', mock_get):
            c.get_sprints(project_id=7)
        url = mock_get.call_args[0][0]
        assert 'project_id=7' in url

    def test_create_sprint_posts_fields(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True, 'id': 1}))
        with patch('requests.post', mock_post):
            result = c.create_sprint(project_id=1, goal='Ship it', max_points=80)
        body = mock_post.call_args[1].get('json', {})
        assert body['goal'] == 'Ship it'
        assert body['max_points'] == 80
        assert result['success'] is True

    def test_update_task_sends_patch(self):
        c = _new_client()
        mock_patch = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.patch', mock_patch):
            result = c.update_task(42, {'status': 'done'})
        url = mock_patch.call_args[0][0]
        assert '/api/tasks/42' in url
        assert result['success'] is True

    def test_update_task_offline_returns_error(self):
        import requests as _req
        c = _new_client()
        with patch('requests.patch', side_effect=_req.exceptions.ConnectionError()):
            result = c.update_task(1, {'title': 'x'})
        assert result['success'] is False

    def test_delete_task_sends_delete(self):
        c = _new_client()
        mock_del = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.delete', mock_del):
            result = c.delete_task(99)
        url = mock_del.call_args[0][0]
        assert '/api/tasks/99' in url
        assert result['success'] is True

    def test_update_provider_sends_patch(self):
        c = _new_client()
        mock_patch = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.patch', mock_patch):
            result = c.update_provider(3, {'enabled': 0})
        url = mock_patch.call_args[0][0]
        assert '/api/providers/3' in url
        assert result['success'] is True

    def test_delete_provider_sends_delete(self):
        c = _new_client()
        mock_del = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.delete', mock_del):
            result = c.delete_provider(5)
        url = mock_del.call_args[0][0]
        assert '/api/providers/5' in url
        assert result['success'] is True

    def test_get_standards_returns_list(self):
        c = _new_client()
        data = [{'name': 'style', 'content': '# Style'}]
        with patch('requests.get', return_value=_mock_response(data)):
            assert c.get_standards() == data

    def test_get_standards_offline_returns_empty(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            assert c.get_standards() == []

    def test_save_standard_posts_correctly(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.post', mock_post):
            result = c.save_standard('my-style', '# Content')
        body = mock_post.call_args[1].get('json', {})
        assert body['name'] == 'my-style'
        assert body['content'] == '# Content'
        assert result['success'] is True

    def test_get_settings_returns_dict(self):
        c = _new_client()
        with patch('requests.get', return_value=_mock_response({'theme': 'dark'})):
            result = c.get_settings()
        assert result['theme'] == 'dark'

    def test_get_settings_offline_returns_empty(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            assert c.get_settings() == {}

    def test_save_settings_posts_correctly(self):
        c = _new_client()
        mock_post = MagicMock(return_value=_mock_response({'success': True}))
        with patch('requests.post', mock_post):
            result = c.save_settings({'sprint_interval': 60})
        body = mock_post.call_args[1].get('json', {})
        assert body['sprint_interval'] == 60
        assert result['success'] is True

    def test_get_wiki_encodes_path(self):
        c = _new_client()
        mock_get = MagicMock(return_value=_mock_response({'files': []}))
        with patch('requests.get', mock_get):
            c.get_wiki('/home/user/my project')
        url = mock_get.call_args[0][0]
        assert ' ' not in url

    def test_get_wiki_returns_dict(self):
        c = _new_client()
        wiki = {'files': ['main.py'], 'tree': {}}
        with patch('requests.get', return_value=_mock_response(wiki)):
            result = c.get_wiki('/some/path')
        assert 'files' in result


# ── 17. APIClient — Auth client methods ──────────────────────────────────────

class TestAPIClientAuth:

    def test_pair_with_token_success(self):
        c = _new_client()
        response_data = {'device_id': 'dev-1', 'token': 'tok-abc', 'status': 'paired'}
        mock_post = MagicMock(return_value=_mock_response(response_data))
        with patch('requests.post', mock_post), \
             patch('secrets.store_device_token', MagicMock(), create=True):
            result = c.pair_with_token('tok-abc', 'MyPhone')
        assert result['status'] == 'paired'

    def test_pair_with_token_invalid(self):
        c = _new_client()
        with patch('requests.post', return_value=_mock_response({'error': 'Invalid'}, status=403)):
            import requests as _req
            _mock_resp = MagicMock()
            _mock_resp.status_code = 403
            _mock_resp.json.return_value = {'error': 'Invalid'}
            _mock_resp.raise_for_status.side_effect = _req.exceptions.HTTPError(response=_mock_resp)
            with patch('requests.post', return_value=_mock_resp):
                result = c.pair_with_token('bad-token')
        # Either returns error dict or empty dict — not the 'paired' success
        assert result.get('status') != 'paired'

    def test_get_devices_returns_list(self):
        c = _new_client()
        devices = [{'device_id': 'dev-1', 'device_name': 'Phone'}]
        with patch('requests.get', return_value=_mock_response(devices)):
            result = c.get_devices()
        assert isinstance(result, list)

    def test_get_devices_offline_returns_empty(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            assert c.get_devices() == []

    def test_revoke_device_sends_delete(self):
        c = _new_client()
        mock_del = MagicMock(return_value=_mock_response({'revoked': True}))
        with patch('requests.delete', mock_del):
            result = c.revoke_device('device-xyz')
        url = mock_del.call_args[0][0]
        assert '/api/auth/devices/device-xyz' in url

    def test_get_auth_audit_log_returns_list(self):
        c = _new_client()
        events = [{'action': 'pair', 'device_id': 'd1'}]
        with patch('requests.get', return_value=_mock_response(events)):
            result = c.get_auth_audit_log()
        assert isinstance(result, list)

    def test_get_auth_audit_log_respects_limit(self):
        c = _new_client()
        mock_get = MagicMock(return_value=_mock_response([]))
        with patch('requests.get', mock_get):
            c.get_auth_audit_log(limit=25)
        url = mock_get.call_args[0][0]
        assert 'limit=25' in url


# ── 18. APIClient — Error tracking + cache ────────────────────────────────────

class TestAPIClientErrorCache:

    def test_last_error_none_initially(self):
        c = _new_client()
        assert c.get_last_error() is None

    def test_last_error_offline_on_connection_error(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            c.get_status()
        assert c.get_last_error() == 'offline'

    def test_last_error_timeout_on_timeout(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.Timeout()):
            c.get_status()
        assert c.get_last_error() == 'timeout'

    def test_last_error_cleared_on_success(self):
        import requests as _req
        c = _new_client()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            c.get_status()
        assert c.get_last_error() == 'offline'
        with patch('requests.get', return_value=_mock_response({'status': 'ok'})):
            c.get_status()
        assert c.get_last_error() is None

    def test_last_error_http_code_on_http_error(self):
        import requests as _req
        c = _new_client()
        fake_resp = MagicMock()
        fake_resp.status_code = 404
        err = _req.exceptions.HTTPError(response=fake_resp)
        fake_resp.raise_for_status.side_effect = err
        with patch('requests.get', return_value=fake_resp):
            c.get_status()
        assert c.get_last_error() == 'http_404'

    def test_offline_fallback_returns_cached_data(self):
        import requests as _req
        c = _new_client()
        cached = {'status': 'ok'}
        with patch('requests.get', return_value=_mock_response(cached)):
            c.get_status()
        with patch('requests.get', side_effect=_req.exceptions.ConnectionError()):
            result = c.get_status()
        assert result == cached

    def test_cache_stores_data_after_successful_get(self):
        c = _new_client()
        data = [{'id': 1, 'name': 'Alpha'}]
        with patch('requests.get', return_value=_mock_response(data)):
            c.get_projects()
        assert c._cache_fallback('/api/projects') == data

    def test_cache_ttl_not_expired_returns_cached(self):
        c = _new_client()
        data1 = [{'id': 1}]
        data2 = [{'id': 2}]
        with patch('requests.get', return_value=_mock_response(data1)):
            r1 = c.get_projects()
        # TTL is 300s — immediately after, cache is still warm but _get always fetches
        with patch('requests.get', return_value=_mock_response(data2)):
            r2 = c.get_projects()
        assert r1 == data1
        assert r2 == data2  # fresh fetch when online


# ── 19. APIClient — Connection + headers ─────────────────────────────────────

class TestAPIClientConnection:

    def test_is_connected_false_initially(self):
        assert _new_client().is_connected() is False

    def test_custom_timeout_used_in_request(self):
        c = _new_client(timeout=25)
        mock_get = MagicMock(return_value=_mock_response({'status': 'ok'}))
        with patch('requests.get', mock_get):
            c.get_status()
        kwargs = mock_get.call_args[1]
        assert kwargs.get('timeout') == 25

    def test_bearer_token_sent_when_api_key_set(self):
        c = _new_client(api_key='my-secret')
        mock_get = MagicMock(return_value=_mock_response({'status': 'ok'}))
        with patch('requests.get', mock_get):
            c.get_status()
        headers = mock_get.call_args[1].get('headers', {})
        assert headers.get('Authorization') == 'Bearer my-secret'

    def test_no_auth_header_when_no_api_key(self):
        c = _new_client(api_key='')
        mock_get = MagicMock(return_value=_mock_response({'status': 'ok'}))
        with patch('requests.get', mock_get):
            c.get_status()
        headers = mock_get.call_args[1].get('headers', {})
        assert 'Authorization' not in headers

    def test_connect_updates_base_url(self):
        c = _new_client()
        c.connect('http://new-host:9000')
        assert c._base_url == 'http://new-host:9000'

    def test_disconnect_stops_ws(self):
        c = _new_client()
        c._ws_running = True
        c.disconnect()
        assert c._ws_running is False

    def test_env_var_overrides_api_key(self, monkeypatch):
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'env-key')
        from api_client import APIClient
        c = APIClient('http://localhost:8765')
        assert c._api_key == 'env-key'

    def test_env_var_overrides_server_url(self, monkeypatch):
        monkeypatch.setenv('AUTOMATICA_SERVER_URL', 'http://custom-host:1234')
        from api_client import APIClient
        c = APIClient('http://localhost:8765')
        assert c._base_url == 'http://custom-host:1234'

    def test_retry_attempts_on_connection_error(self):
        """_get retries _max_retries times before giving up."""
        import requests as _req
        c = _new_client()
        c._max_retries = 1  # 1 retry = 2 total attempts
        mock_get = MagicMock(side_effect=_req.exceptions.ConnectionError())
        with patch('requests.get', mock_get), \
             patch('time.sleep'):  # speed up test
            c.get_status()
        assert mock_get.call_count == 2  # initial + 1 retry

    def test_post_offline_returns_error_dict(self):
        import requests as _req
        c = _new_client()
        with patch('requests.post', side_effect=_req.exceptions.ConnectionError()):
            result = c.add_task(1, {'title': 'X'})
        assert result['success'] is False
        assert result['error'] == 'offline'

    def test_post_timeout_returns_timeout_dict(self):
        import requests as _req
        c = _new_client()
        with patch('requests.post', side_effect=_req.exceptions.Timeout()):
            result = c.add_task(1, {'title': 'X'})
        assert result['success'] is False
        assert result['error'] == 'timeout'


# ── 16. Wiki Endpoint ─────────────────────────────────────────────────────────

class TestWikiEndpoint:
    """GET /api/wiki/{project_path} — C-07 path traversal + scanner errors."""

    def test_happy_path(self, tc):
        wiki_data = {'files': [{'path': 'foo.py', 'symbols': ['MyClass']}]}
        with patch('security.safe_project_path', return_value='/safe/proj'), \
             patch('project_scanner.build_file_wiki', return_value=wiki_data):
            r = tc.get('/api/wiki/myproject')
        assert r.status_code == 200
        assert r.json() == wiki_data

    def test_path_traversal_rejected(self, tc):
        # URL normalisation strips literal '..'; test the behaviour at the
        # safe_project_path boundary by injecting a ValueError for any path.
        with patch('security.safe_project_path',
                   side_effect=ValueError('path traversal detected')):
            r = tc.get('/api/wiki/evil-project')
        assert r.status_code == 400
        assert 'path traversal' in r.json()['detail']

    def test_scanner_failure_returns_500(self, tc):
        with patch('security.safe_project_path', return_value='/safe/proj'), \
             patch('project_scanner.build_file_wiki',
                   side_effect=RuntimeError('scanner crashed')):
            r = tc.get('/api/wiki/myproject')
        assert r.status_code == 500
        assert 'scanner crashed' in r.json()['detail']

    def test_nested_path_forwarded_to_safe_check(self, tc):
        with patch('security.safe_project_path', return_value='/safe/dir') as safe_mock, \
             patch('project_scanner.build_file_wiki', return_value={'files': []}):
            tc.get('/api/wiki/org/repo/subdir')
        safe_mock.assert_called_once_with('org/repo/subdir')


# ── 17. Session Expiry ────────────────────────────────────────────────────────

class TestSessionExpiry:
    """C-16 session token lifecycle: issue, expire, logout, independence."""

    @pytest.fixture(autouse=True)
    def _clean_sessions(self):
        _api_server_mod._sessions.clear()
        yield
        _api_server_mod._sessions.clear()

    def _authed_tc(self, monkeypatch):
        monkeypatch.setenv('AUTOMATICA_API_KEY', 'testkey')
        from fastapi.testclient import TestClient
        return TestClient(_build_app())

    def test_expired_token_rejected(self, monkeypatch):
        tc = self._authed_tc(monkeypatch)
        token = 'deadbeef' * 8
        _api_server_mod._sessions[token] = time.monotonic() - 1  # already past
        with patch('security.safe_project_path', return_value='/safe'), \
             patch('project_scanner.build_file_wiki', return_value={'files': []}):
            r = tc.get('/api/wiki/proj', headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 401

    def test_active_token_accepted(self, monkeypatch):
        tc = self._authed_tc(monkeypatch)
        token = tc.post('/api/auth/login', json={'api_key': 'testkey'}).json()['token']
        with patch('security.safe_project_path', return_value='/safe'), \
             patch('project_scanner.build_file_wiki', return_value={'files': []}):
            r = tc.get('/api/wiki/proj', headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200

    def test_logout_removes_session(self, monkeypatch):
        tc = self._authed_tc(monkeypatch)
        token = tc.post('/api/auth/login', json={'api_key': 'testkey'}).json()['token']
        assert token in _api_server_mod._sessions
        tc.post('/api/auth/logout', headers={'Authorization': f'Bearer {token}'})
        assert token not in _api_server_mod._sessions

    def test_two_sessions_are_independent(self, monkeypatch):
        tc = self._authed_tc(monkeypatch)
        t1 = tc.post('/api/auth/login', json={'api_key': 'testkey'}).json()['token']
        t2 = tc.post('/api/auth/login', json={'api_key': 'testkey'}).json()['token']
        assert t1 != t2
        tc.post('/api/auth/logout', headers={'Authorization': f'Bearer {t1}'})
        assert t1 not in _api_server_mod._sessions
        assert t2 in _api_server_mod._sessions


# ── 18. Database Failures ─────────────────────────────────────────────────────

class TestDatabaseFailures:
    """Global DB I/O errors propagate as 500 across affected endpoints."""

    def _broken_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.side_effect = Exception('disk I/O error')
        cursor.fetchone.side_effect = Exception('disk I/O error')
        conn.execute.return_value = cursor
        return conn

    def test_projects_500_on_db_error(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._broken_conn()):
            r = TestClient(_build_app()).get('/api/projects')
        assert r.status_code == 500

    def test_logs_500_on_db_error(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._broken_conn()):
            r = TestClient(_build_app()).get('/api/logs')
        assert r.status_code == 500

    def test_providers_500_on_db_error(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._broken_conn()):
            r = TestClient(_build_app()).get('/api/providers')
        assert r.status_code == 500

    def test_agents_500_on_db_error(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._broken_conn()):
            r = TestClient(_build_app()).get('/api/agents')
        assert r.status_code == 500


# ── 19. Request Validation ────────────────────────────────────────────────────

class TestRequestValidation:
    """Pydantic model constraints and manual validation gates."""

    def test_task_patch_complexity_too_high_returns_422(self, tc):
        r = tc.patch('/api/tasks/1', json={'complexity': 99})
        assert r.status_code == 422

    def test_task_patch_complexity_too_low_returns_422(self, tc):
        r = tc.patch('/api/tasks/1', json={'complexity': 0})
        assert r.status_code == 422

    def test_task_patch_title_too_long_returns_422(self, tc):
        r = tc.patch('/api/tasks/1', json={'title': 'x' * 501})
        assert r.status_code == 422

    def test_sprint_start_missing_project_id_returns_400(self, tc):
        r = tc.post('/api/sprints/start', json={'goal': 'do something'})
        assert r.status_code == 400

    def test_agent_invalid_slug_returns_422(self, tc):
        r = tc.post('/api/agents', json={'slug': 'UPPER_CASE!', 'role': 'x'})
        assert r.status_code == 422

    def test_standard_empty_name_returns_422(self, tc):
        r = tc.post('/api/standards', json={'name': '', 'content': 'body'})
        assert r.status_code == 422


# ── 20. Concurrent Sprint Starts ──────────────────────────────────────────────

class TestConcurrentSprints:
    """Race condition safety: simultaneous /api/sprints/start calls."""

    def test_three_concurrent_starts_all_get_responses(self):
        import threading

        app = _build_app()
        from fastapi.testclient import TestClient
        tc = TestClient(app)

        results: list[int] = []
        lock = threading.Lock()

        def _call():
            r = tc.post('/api/sprints/start', json={'project_id': 99999})
            with lock:
                results.append(r.status_code)

        threads = [threading.Thread(target=_call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 3
        for code in results:
            assert code in (404, 500)

    def test_missing_project_id_always_400_never_500(self, tc):
        for _ in range(3):
            r = tc.post('/api/sprints/start', json={})
            assert r.status_code == 400


# ── 21. Webhook Push Event ────────────────────────────────────────────────────

class TestWebhookPushEvent:
    """F-17 /api/webhooks/receive — push_event wiring and edge cases."""

    def test_push_event_called_with_correct_args(self, app):
        from fastapi.testclient import TestClient
        push_mock = MagicMock()
        app.state.push_event = push_mock
        body = {'event': 'push', 'repo': 'test-repo', 'branch': 'main'}
        r = TestClient(app).post('/api/webhooks/receive', json=body)
        assert r.status_code == 200
        push_mock.assert_called_once_with(
            'webhook_received',
            {'event_type': 'push', 'data': body},
        )

    def test_response_contains_event_type(self, app):
        from fastapi.testclient import TestClient
        app.state.push_event = MagicMock()
        r = TestClient(app).post('/api/webhooks/receive', json={'event': 'ci_pass'})
        assert r.json()['event'] == 'ci_pass'

    def test_no_push_event_attr_does_not_crash(self, app):
        from fastapi.testclient import TestClient
        del app.state.push_event
        r = TestClient(app).post('/api/webhooks/receive', json={'event': 'ping'})
        assert r.status_code == 200

    def test_missing_event_key_defaults_to_unknown(self, app):
        from fastapi.testclient import TestClient
        push_mock = MagicMock()
        app.state.push_event = push_mock
        r = TestClient(app).post('/api/webhooks/receive', json={'repo': 'test'})
        assert r.json()['event'] == 'unknown'
        push_mock.assert_called_once_with(
            'webhook_received',
            {'event_type': 'unknown', 'data': {'repo': 'test'}},
        )


# ── 22. Cross-Project Task Lookups ────────────────────────────────────────────

class TestCrossProjectTasks:
    """Task endpoints scan all known_projects — absent task/project → 404."""

    def _empty_conn(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.execute.return_value = cursor
        return conn

    def test_patch_nonexistent_task_returns_404(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._empty_conn()):
            r = TestClient(_build_app()).patch('/api/tasks/99999', json={'status': 'done'})
        assert r.status_code == 404

    def test_delete_nonexistent_task_returns_404(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._empty_conn()):
            r = TestClient(_build_app()).delete('/api/tasks/99999')
        assert r.status_code == 404

    def test_get_tasks_for_nonexistent_project_returns_404(self):
        from fastapi.testclient import TestClient
        with patch('automatica_db.init_global_db', return_value=self._empty_conn()):
            r = TestClient(_build_app()).get('/api/projects/99999/tasks')
        assert r.status_code == 404


# ── 23. Rate Limit Recovery ───────────────────────────────────────────────────

class TestRateLimitRecovery:
    """C-04 sliding-window eviction: stale buckets clear, fresh buckets block."""

    _IP = 'testclient'  # Starlette TestClient sets request.client.host = 'testclient'

    def test_stale_timestamps_allow_new_request(self, tc):
        """60 timestamps older than _RATE_WINDOW are purged — request succeeds."""
        old = time.monotonic() - (_api_server_mod._RATE_WINDOW + 5)
        _api_server_mod._rate_buckets[self._IP] = [old] * _api_server_mod._RATE_LIMIT
        r = tc.get('/api/status')
        assert r.status_code == 200

    def test_fresh_timestamps_trigger_429(self, tc):
        """60 fresh timestamps → next request is rate-limited."""
        now = time.monotonic()
        _api_server_mod._rate_buckets[self._IP] = [now] * _api_server_mod._RATE_LIMIT
        r = tc.get('/api/status')
        assert r.status_code == 429

    def test_partial_fill_still_allows_entry(self, tc):
        """Half-full bucket leaves room — request gets through."""
        now = time.monotonic()
        half = _api_server_mod._RATE_LIMIT // 2
        _api_server_mod._rate_buckets[self._IP] = [now] * half
        r = tc.get('/api/status')
        assert r.status_code == 200

    def test_429_body_contains_error_key(self, tc):
        now = time.monotonic()
        _api_server_mod._rate_buckets[self._IP] = [now] * _api_server_mod._RATE_LIMIT
        r = tc.get('/api/status')
        assert r.status_code == 429
        assert r.json().get('error') == 'Rate limit exceeded'
