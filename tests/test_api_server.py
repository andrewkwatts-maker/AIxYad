"""
pytest unit tests for api_server.py — FastAPI REST endpoints.

Uses FastAPI's TestClient (synchronous) so no real server or DB is needed.
DB calls are patched with unittest.mock to keep tests isolated and fast.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_test_app():
    """Build a fresh FastAPI app instance for each test."""
    from api_server import APIServer
    srv = APIServer()
    return srv._build_app()


def _client(app=None):
    """Return a TestClient for the given (or freshly built) app."""
    from fastapi.testclient import TestClient
    return TestClient(app or _get_test_app())


# ── Fixture: shared test app + client ─────────────────────────────────────────

@pytest.fixture()
def app():
    return _get_test_app()


@pytest.fixture()
def tc(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


# ── S1-W01 /api/status ────────────────────────────────────────────────────────

def test_status_returns_200(tc):
    r = tc.get('/api/status')
    assert r.status_code == 200


def test_status_body_has_status_ok(tc):
    r = tc.get('/api/status')
    assert r.json()['status'] == 'ok'


def test_status_body_has_mode(tc):
    r = tc.get('/api/status')
    assert 'mode' in r.json()


# ── S1-W01 GET /api/projects ──────────────────────────────────────────────────

def test_projects_returns_200():
    _fake_conn = MagicMock()
    _fake_conn.execute.return_value.fetchall.return_value = [
        {'id': 1, 'name': 'Alpha', 'directory': '/tmp/alpha', 'created_at': '2026-01-01'},
    ]

    with patch('aixyad_db.init_global_db', return_value=_fake_conn):
        r = _client().get('/api/projects')
    assert r.status_code == 200


def test_projects_returns_list():
    _fake_conn = MagicMock()
    _fake_conn.execute.return_value.fetchall.return_value = [
        {'id': 1, 'name': 'Alpha', 'directory': '/tmp/alpha', 'created_at': '2026-01-01'},
        {'id': 2, 'name': 'Beta',  'directory': '/tmp/beta',  'created_at': '2026-01-02'},
    ]

    with patch('aixyad_db.init_global_db', return_value=_fake_conn):
        r = _client().get('/api/projects')
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]['name'] == 'Alpha'


# ── S1-W02 GET /api/projects/{id}/tasks ──────────────────────────────────────

def test_tasks_returns_list_for_known_project(tmp_path):
    import aixyad_db as adb
    import sqlite3

    # Pre-populate a project DB in the tmp dir with check_same_thread=False
    pdb = adb.init_project_db(tmp_path)
    adb.create_task(pdb, title='Task One')
    adb.create_task(pdb, title='Task Two')
    pdb.close()

    # init_project_db must be called inside the handler thread; use the real
    # function but point it at our tmp_path
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().get('/api/projects/1/tasks')

    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]['title'] == 'Task One'


def test_tasks_returns_404_for_unknown_project():
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = None

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().get('/api/projects/9999/tasks')

    assert r.status_code == 404


# ── S1-W03 POST /api/projects/{id}/tasks ─────────────────────────────────────

def test_create_task_returns_success(tmp_path):
    import aixyad_db as adb

    # Ensure project dir exists so init_project_db (called in handler) can open it
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().post('/api/projects/1/tasks', json={
            'title': 'New Task',
            'description': 'Details here',
            'complexity': 5,
        })

    assert r.status_code == 200
    body = r.json()
    assert body['success'] is True
    assert 'task_id' in body
    assert 'display_id' in body


def test_create_task_missing_project_returns_404():
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = None

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().post('/api/projects/9999/tasks', json={'title': 'X'})

    assert r.status_code == 404


# ── S1-W05 GET /api/logs ──────────────────────────────────────────────────────

def test_logs_returns_list(tmp_path):
    # init_project_db is called inside the handler thread — let it run for real
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().get('/api/logs')

    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_logs_respects_limit_param(tmp_path):
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().get('/api/logs?limit=10')

    assert r.status_code == 200


def test_logs_with_project_id(tmp_path):
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().get('/api/logs?project_id=1')

    assert r.status_code == 200


# ── S1-W04 POST /api/sprints/start ───────────────────────────────────────────

def test_start_sprint_missing_project_id():
    r = _client().post('/api/sprints/start', json={})
    assert r.status_code == 400


def test_start_sprint_unknown_project():
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = None

    with patch('aixyad_db.init_global_db', return_value=global_conn):
        r = _client().post('/api/sprints/start', json={'project_id': 9999})

    assert r.status_code == 404


def test_start_sprint_returns_sprint_id(tmp_path):
    global_conn = MagicMock()
    global_conn.execute.return_value.fetchone.return_value = {
        'directory': str(tmp_path), 'name': 'TestProj'}

    mock_runner = MagicMock()
    mock_runner_cls = MagicMock(return_value=mock_runner)

    with patch('aixyad_db.init_global_db', return_value=global_conn), \
         patch('sprint_runner.SprintRunner', mock_runner_cls):
        r = _client().post('/api/sprints/start', json={'project_id': 1})

    assert r.status_code == 200
    body = r.json()
    assert body['success'] is True
    assert 'sprint_id' in body


# ── S6-W08: GET /api/providers ───────────────────────────────────────────────

def test_providers_returns_200():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = []

    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/providers')

    assert r.status_code == 200


def test_providers_returns_list():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [
        {
            'id': 1, 'name': 'Ollama', 'provider_type': 'http_local_chat',
            'api_key_env': '', 'base_url': 'http://localhost:11434',
            'model_name': 'llama3', 'enabled': 1, 'max_tokens': 4096,
            'config_json': '{}', 'created_at': '2026-01-01',
            'updated_at': '2026-01-01',
        },
        {
            'id': 2, 'name': 'Gemini', 'provider_type': 'http_generate_content',
            'api_key_env': 'GEMINI_API_KEY', 'base_url': '',
            'model_name': 'gemini-pro', 'enabled': 1, 'max_tokens': 8192,
            'config_json': '{}', 'created_at': '2026-01-01',
            'updated_at': '2026-01-01',
        },
    ]

    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/providers')

    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list), f"Expected list, got: {type(data)}"
    assert len(data) == 2
    assert data[0]['name'] == 'Ollama'
    assert data[1]['name'] == 'Gemini'


# ── B-23: GET /api/agents ─────────────────────────────────────────────────────

def test_list_agents_returns_200():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = []
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/agents')
    assert r.status_code == 200


def test_list_agents_returns_list():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [
        {'slug': 'developer', 'role': 'Developer', 'description': 'Writes code',
         'created_at': '2026-01-01'},
    ]
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/agents')
    data = r.json()
    assert isinstance(data, list)
    assert data[0]['slug'] == 'developer'


# ── B-23: POST /api/agents ────────────────────────────────────────────────────

def test_create_agent_returns_success():
    fake_conn = MagicMock()
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().post('/api/agents', json={
            'slug': 'my-agent',
            'role': 'Developer',
            'description': 'Does things',
            'prompt': '',
        })
    assert r.status_code == 200
    body = r.json()
    assert body['success'] is True
    assert body['slug'] == 'my-agent'


def test_create_agent_invalid_slug_rejected():
    """Slug with spaces should fail Pydantic validation (422)."""
    r = _client().post('/api/agents', json={
        'slug': 'Invalid Slug!',
        'role': '',
    })
    assert r.status_code == 422


# ── B-23: DELETE /api/agents/{slug} ──────────────────────────────────────────

def test_delete_agent_returns_success():
    fake_conn = MagicMock()
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().delete('/api/agents/developer')
    assert r.status_code == 200
    assert r.json()['success'] is True


# ── B-23: GET /api/sprints ────────────────────────────────────────────────────

def test_list_sprints_no_project_returns_empty():
    r = _client().get('/api/sprints')
    assert r.status_code == 200
    assert r.json() == []


def test_list_sprints_with_project_id(tmp_path):
    import aixyad_db as adb
    pdb = adb.init_project_db(tmp_path)
    pdb.close()

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/sprints?project_id=1')
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_sprints_unknown_project_returns_404():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/sprints?project_id=9999')
    assert r.status_code == 404


# ── B-23: POST /api/sprints ───────────────────────────────────────────────────

def test_create_sprint_returns_success(tmp_path):
    import aixyad_db as adb
    pdb = adb.init_project_db(tmp_path)
    pdb.close()

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {'directory': str(tmp_path)}
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().post('/api/sprints', json={
            'project_id': 1,
            'goal': 'Ship feature X',
            'max_points': 100,
        })
    assert r.status_code == 200
    body = r.json()
    assert body['success'] is True


def test_create_sprint_missing_project_returns_404():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().post('/api/sprints', json={'project_id': 9999, 'goal': ''})
    assert r.status_code == 404


# ── B-23: PATCH /api/tasks/{id} ──────────────────────────────────────────────

def test_update_task_returns_success(tmp_path):
    import aixyad_db as adb
    pdb = adb.init_project_db(tmp_path)
    task = adb.create_task(pdb, title='Original Title')
    task_id = task['id']
    pdb.close()

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [
        {'id': 1, 'directory': str(tmp_path)},
    ]
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().patch(f'/api/tasks/{task_id}', json={'title': 'Updated Title'})
    assert r.status_code == 200
    assert r.json()['success'] is True


def test_update_task_unknown_id_returns_404(tmp_path):
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = []
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().patch('/api/tasks/9999', json={'title': 'X'})
    assert r.status_code == 404


# ── B-23: DELETE /api/tasks/{id} ─────────────────────────────────────────────

def test_delete_task_returns_success(tmp_path):
    import aixyad_db as adb
    pdb = adb.init_project_db(tmp_path)
    task = adb.create_task(pdb, title='To Delete')
    task_id = task['id']
    pdb.close()

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [
        {'id': 1, 'directory': str(tmp_path)},
    ]
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().delete(f'/api/tasks/{task_id}')
    assert r.status_code == 200
    assert r.json()['success'] is True


def test_delete_task_unknown_id_returns_404(tmp_path):
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = []
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().delete('/api/tasks/9999')
    assert r.status_code == 404


# ── B-23: PATCH /api/providers/{id} ──────────────────────────────────────────

def test_update_provider_returns_success():
    fake_conn = MagicMock()
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().patch('/api/providers/1', json={'name': 'NewName', 'enabled': 0})
    assert r.status_code == 200
    assert r.json()['success'] is True


# ── B-23: DELETE /api/providers/{id} ─────────────────────────────────────────

def test_delete_provider_returns_success():
    fake_conn = MagicMock()
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().delete('/api/providers/1')
    assert r.status_code == 200
    assert r.json()['success'] is True


# ── B-23: GET /api/standards ─────────────────────────────────────────────────

def test_list_standards_returns_200(tmp_path, monkeypatch):
    """Standards list returns 200 even when no standards directory exists."""
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    r = _client().get('/api/standards')
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_standards_returns_md_files(tmp_path, monkeypatch):
    import json
    std_dir = tmp_path / 'standards'
    std_dir.mkdir()
    (std_dir / 'coding-style.md').write_text('# Coding Style', encoding='utf-8')
    # settings.json must be in the .automatica subdir (where _get_settings reads it)
    settings_dir = tmp_path / '.automatica'
    settings_dir.mkdir()
    (settings_dir / 'settings.json').write_text(
        json.dumps({'standards_dir': str(std_dir)}))
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    r = _client().get('/api/standards')
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['name'] == 'coding-style'


# ── B-23: GET /api/settings ──────────────────────────────────────────────────

def test_get_settings_returns_200(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    r = _client().get('/api/settings')
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_get_settings_reads_existing_file(tmp_path, monkeypatch):
    import json
    settings_dir = tmp_path / '.automatica'
    settings_dir.mkdir()
    (settings_dir / 'settings.json').write_text(json.dumps({'api_key': 'secret'}))
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    r = _client().get('/api/settings')
    assert r.status_code == 200
    assert r.json().get('api_key') == 'secret'


# ── B-23: GET /api/health ─────────────────────────────────────────────────────

def test_health_returns_200():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (1,)
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/health')
    assert r.status_code == 200


def test_health_body_has_required_fields():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (1,)
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/health')
    body = r.json()
    for field in ('status', 'version', 'python', 'platform', 'db', 'uptime_s'):
        assert field in body, f"Missing field: {field}"


def test_health_status_ok_when_db_healthy():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (1,)
    with patch('aixyad_db.init_global_db', return_value=fake_conn):
        r = _client().get('/api/health')
    assert r.json()['status'] == 'ok'
    assert r.json()['db'] == 'ok'


def test_health_status_degraded_when_db_fails():
    with patch('aixyad_db.init_global_db', side_effect=Exception('db error')):
        r = _client().get('/api/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'degraded'
    assert r.json()['db'] == 'error'
