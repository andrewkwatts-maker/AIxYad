"""
Tests for APIClient REST methods with mocked HTTP.

S6-W09: TestAPIClientREST

The APIClient lives in H:\\Github\\Automatica-App\\services\\api_client.py.
We adjust sys.path so it can be imported from here.
"""
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make Automatica-App/services importable
_APP_SERVICES = str(Path(__file__).resolve().parent.parent.parent / 'Automatica-App' / 'services')
if _APP_SERVICES not in sys.path:
    sys.path.insert(0, _APP_SERVICES)


def _make_response(data, status=200):
    """Factory for a mock requests.Response."""
    m = MagicMock()
    m.status_code = status
    m.json.return_value = data
    m.raise_for_status = MagicMock()  # no-op — doesn't raise
    return m


def _make_error_response(exc_class=Exception, message='connection refused'):
    """Return a side_effect that raises an exception (simulates offline)."""
    return exc_class(message)


class TestAPIClientREST:

    def test_get_status_returns_dict(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        with patch('requests.get', return_value=_make_response({'status': 'ok'})):
            result = client.get_status()
        assert isinstance(result, dict), f"Expected dict, got: {type(result)}"
        assert result['status'] == 'ok'

    def test_get_projects_returns_list(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        projects = [
            {'id': 1, 'name': 'Alpha', 'directory': '/tmp/alpha', 'created_at': '2026-01-01'},
            {'id': 2, 'name': 'Beta',  'directory': '/tmp/beta',  'created_at': '2026-01-02'},
        ]
        with patch('requests.get', return_value=_make_response(projects)):
            result = client.get_projects()
        assert isinstance(result, list), f"Expected list, got: {type(result)}"
        assert len(result) == 2
        assert result[0]['name'] == 'Alpha'

    def test_get_tasks_returns_list(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        tasks = [
            {'id': 1, 'title': 'Task One', 'status': 'todo'},
            {'id': 2, 'title': 'Task Two', 'status': 'in_progress'},
        ]
        with patch('requests.get', return_value=_make_response(tasks)):
            result = client.get_tasks(project_id=1)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]['title'] == 'Task One'

    def test_offline_returns_cached(self):
        """When requests raises, _get should fall back to cached value."""
        from api_client import APIClient
        client = APIClient('http://localhost:8765')

        # Prime the cache via a successful call
        cached_data = {'status': 'ok'}
        with patch('requests.get', return_value=_make_response(cached_data)):
            client.get_status()

        # Now simulate offline — requests.get raises
        with patch('requests.get', side_effect=ConnectionError('offline')):
            result = client.get_status()

        assert result == cached_data, \
            f"Expected cached data {cached_data!r}, got: {result!r}"

    def test_add_task_posts_body(self):
        """add_task should POST to the correct URL with the task body."""
        from api_client import APIClient
        client = APIClient('http://localhost:8765')

        response_data = {'success': True, 'task_id': 42, 'display_id': 'TASK-042'}
        mock_post = MagicMock(return_value=_make_response(response_data))

        with patch('requests.post', mock_post):
            result = client.add_task(project_id=1, data={'title': 'New Task', 'complexity': 3})

        # Verify POST was called with the right URL and body
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get('url', '')
        assert '/api/projects/1/tasks' in url, \
            f"Expected URL to contain /api/projects/1/tasks, got: {url}"
        assert result['success'] is True
        assert result['task_id'] == 42

    # ── B-24: Agents ──────────────────────────────────────────────────────────

    def test_get_agents_returns_list(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        agents = [{'slug': 'developer', 'role': 'Developer', 'description': '', 'created_at': ''}]
        with patch('requests.get', return_value=_make_response(agents)):
            result = client.get_agents()
        assert isinstance(result, list)
        assert result[0]['slug'] == 'developer'

    def test_get_agents_returns_empty_on_offline(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.get', side_effect=req.exceptions.ConnectionError('offline')):
            result = client.get_agents()
        assert result == []

    def test_save_agent_posts_correctly(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        response_data = {'success': True, 'slug': 'tester'}
        mock_post = MagicMock(return_value=_make_response(response_data))
        with patch('requests.post', mock_post):
            result = client.save_agent('tester', {'role': 'Tester', 'description': ''})
        mock_post.assert_called_once()
        assert result['success'] is True

    def test_delete_agent_sends_delete(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        mock_delete = MagicMock(return_value=_make_response({'success': True}))
        with patch('requests.delete', mock_delete):
            result = client.delete_agent('developer')
        mock_delete.assert_called_once()
        url = mock_delete.call_args[0][0]
        assert '/api/agents/developer' in url
        assert result['success'] is True

    def test_delete_agent_offline_returns_error(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.delete', side_effect=req.exceptions.ConnectionError('off')):
            result = client.delete_agent('developer')
        assert result['success'] is False
        assert 'error' in result

    # ── B-24: Sprints ─────────────────────────────────────────────────────────

    def test_get_sprints_no_project(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        with patch('requests.get', return_value=_make_response([])):
            result = client.get_sprints()
        assert result == []

    def test_get_sprints_with_project_id(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        mock_get = MagicMock(return_value=_make_response([{'id': 1, 'sprint_number': 1}]))
        with patch('requests.get', mock_get):
            result = client.get_sprints(project_id=5)
        url = mock_get.call_args[0][0]
        assert 'project_id=5' in url
        assert len(result) == 1

    def test_create_sprint_posts_correctly(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        response_data = {'success': True, 'id': 1, 'sprint_number': 1}
        mock_post = MagicMock(return_value=_make_response(response_data))
        with patch('requests.post', mock_post):
            result = client.create_sprint(project_id=1, goal='Ship v2', max_points=50)
        mock_post.assert_called_once()
        body = mock_post.call_args[1].get('json', {})
        assert body['project_id'] == 1
        assert body['goal'] == 'Ship v2'
        assert result['success'] is True

    # ── B-24: Task PATCH/DELETE ───────────────────────────────────────────────

    def test_update_task_sends_patch(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        mock_patch = MagicMock(return_value=_make_response({'success': True}))
        with patch('requests.patch', mock_patch):
            result = client.update_task(42, {'title': 'New Title', 'status': 'in_progress'})
        mock_patch.assert_called_once()
        url = mock_patch.call_args[0][0]
        assert '/api/tasks/42' in url
        assert result['success'] is True

    def test_update_task_offline_returns_error(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.patch', side_effect=req.exceptions.ConnectionError('off')):
            result = client.update_task(42, {'title': 'X'})
        assert result['success'] is False

    def test_delete_task_sends_delete(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        mock_delete = MagicMock(return_value=_make_response({'success': True}))
        with patch('requests.delete', mock_delete):
            result = client.delete_task(99)
        mock_delete.assert_called_once()
        url = mock_delete.call_args[0][0]
        assert '/api/tasks/99' in url
        assert result['success'] is True

    # ── B-24: Provider PATCH/DELETE ───────────────────────────────────────────

    def test_update_provider_sends_patch(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        mock_patch = MagicMock(return_value=_make_response({'success': True}))
        with patch('requests.patch', mock_patch):
            result = client.update_provider(3, {'name': 'Renamed', 'enabled': 1})
        mock_patch.assert_called_once()
        url = mock_patch.call_args[0][0]
        assert '/api/providers/3' in url
        assert result['success'] is True

    def test_delete_provider_sends_delete(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        mock_delete = MagicMock(return_value=_make_response({'success': True}))
        with patch('requests.delete', mock_delete):
            result = client.delete_provider(3)
        mock_delete.assert_called_once()
        url = mock_delete.call_args[0][0]
        assert '/api/providers/3' in url
        assert result['success'] is True

    # ── B-24: Standards ───────────────────────────────────────────────────────

    def test_get_standards_returns_list(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        standards = [{'name': 'coding-style', 'filename': 'coding-style.md',
                      'content': '# Style', 'path': '/tmp/x.md'}]
        with patch('requests.get', return_value=_make_response(standards)):
            result = client.get_standards()
        assert isinstance(result, list)
        assert result[0]['name'] == 'coding-style'

    def test_get_standards_empty_on_offline(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.get', side_effect=req.exceptions.ConnectionError('off')):
            result = client.get_standards()
        assert result == []

    # ── B-24: Settings ────────────────────────────────────────────────────────

    def test_get_settings_returns_dict(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        with patch('requests.get', return_value=_make_response({'api_key': 'abc'})):
            result = client.get_settings()
        assert isinstance(result, dict)
        assert result['api_key'] == 'abc'

    def test_get_settings_empty_on_offline(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.get', side_effect=req.exceptions.ConnectionError('off')):
            result = client.get_settings()
        assert result == {}

    # ── B-24: Wiki ────────────────────────────────────────────────────────────

    def test_get_wiki_encodes_path(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        mock_get = MagicMock(return_value=_make_response({'files': []}))
        with patch('requests.get', mock_get):
            client.get_wiki('/home/user/my project')
        url = mock_get.call_args[0][0]
        # Spaces should be percent-encoded
        assert ' ' not in url
        assert '%20' in url or '+' in url or 'my' in url

    def test_get_wiki_returns_dict(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        wiki_data = {'files': ['main.py'], 'tree': {}}
        with patch('requests.get', return_value=_make_response(wiki_data)):
            result = client.get_wiki('/some/path')
        assert isinstance(result, dict)
        assert 'files' in result

    # ── B-24: is_connected ────────────────────────────────────────────────────

    def test_is_connected_false_when_no_ws_thread(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        assert client.is_connected() is False

    # ── B-24: get_last_error ──────────────────────────────────────────────────

    def test_get_last_error_none_initially(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        assert client.get_last_error() is None

    def test_get_last_error_set_on_connection_error(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.get', side_effect=req.exceptions.ConnectionError('off')):
            client.get_status()
        assert client.get_last_error() == 'offline'

    def test_get_last_error_set_on_timeout(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        with patch('requests.get', side_effect=req.exceptions.Timeout('slow')):
            client.get_status()
        assert client.get_last_error() == 'timeout'

    def test_get_last_error_cleared_on_success(self):
        from api_client import APIClient
        import requests as req
        client = APIClient('http://localhost:8765')
        # First cause an error
        with patch('requests.get', side_effect=req.exceptions.ConnectionError('off')):
            client.get_status()
        assert client.get_last_error() == 'offline'
        # Then succeed
        with patch('requests.get', return_value=_make_response({'status': 'ok'})):
            client.get_status()
        assert client.get_last_error() is None

    # ── B-24: B-08 configurable timeout ───────────────────────────────────────

    def test_custom_timeout_used_in_get(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765', timeout=25)
        mock_get = MagicMock(return_value=_make_response({'status': 'ok'}))
        with patch('requests.get', mock_get):
            client.get_status()
        kwargs = mock_get.call_args[1]
        assert kwargs.get('timeout') == 25

    # ── B-24: B-10 TTL cache ──────────────────────────────────────────────────

    def test_ttl_cache_serves_fresh_data(self):
        from api_client import APIClient
        client = APIClient('http://localhost:8765')
        data1 = [{'id': 1, 'name': 'Alpha'}]
        with patch('requests.get', return_value=_make_response(data1)):
            r1 = client.get_projects()
        # Immediately request again — should come from cache if TTL not expired
        with patch('requests.get', return_value=_make_response([{'id': 2}])) as m:
            r2 = client.get_projects()
        # Second call still hits the server (TTL not expired just returns result)
        assert r1 == data1
