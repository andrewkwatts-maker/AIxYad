"""E2E test: start API server, connect as client, verify endpoints."""
import pytest, time, threading

@pytest.mark.integration
def test_api_server_full_lifecycle():
    from api_server import APIServer
    srv = APIServer(port=28765)
    srv.start()
    time.sleep(1.5)

    try:
        import requests

        # Health check
        r = requests.get('http://localhost:28765/api/health', timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert 'status' in data

        # Projects list
        r = requests.get('http://localhost:28765/api/projects', timeout=5)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        # Standards
        r = requests.get('http://localhost:28765/api/standards', timeout=5)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        # Settings
        r = requests.get('http://localhost:28765/api/settings', timeout=5)
        assert r.status_code == 200

        # Agents
        r = requests.get('http://localhost:28765/api/agents', timeout=5)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        # Providers
        r = requests.get('http://localhost:28765/api/providers', timeout=5)
        assert r.status_code == 200

        # OpenAPI
        r = requests.get('http://localhost:28765/docs', timeout=5)
        assert r.status_code == 200

    finally:
        srv.stop()
